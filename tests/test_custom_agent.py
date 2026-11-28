import json
from quickpool.compat import Prepared, sanitize_outbound_text
from quickpool.models import ChatRequest
from quickpool.native_protocol import build_user_message, encode_eventstream
from quickpool.native_upstream import DEFAULT_CODING_AGENT_NAME, normalize_agent_id


def test_sanitize_outbound_text_strips_billing_header_and_neutralizes_system_reminder():
    raw = (
        'x-anthropic-billing-header: cc_version=2.1.7; cch=abc123;\n'
        'You are a helpful coding assistant.\n'
        '<system-reminder>Check git status before editing.</system-reminder>\n'
        'Done.'
    )
    cleaned = sanitize_outbound_text(raw)
    assert 'x-anthropic-billing-header' not in cleaned
    assert '<system-reminder>' not in cleaned
    assert '[Context note: Check git status before editing.]' in cleaned
    assert 'You are a helpful coding assistant.' in cleaned

    softened = sanitize_outbound_text(
        "You are Claude Code, Anthropic's official CLI for Claude.\nYou are an interactive CLI tool.",
        soften_identity=True,
    )
    assert 'Claude Code' not in softened
    assert 'external coding client' in softened


def test_prepared_parses_tool_call_xml_blocks():
    req = ChatRequest(
        model='amazon-smart',
        messages=[{'role': 'user', 'content': 'read compat.py'}],
        tools=[{'type': 'function', 'function': {
            'name': 'Read', 'parameters': {'type': 'object', 'properties': {'file_path': {'type': 'string'}}, 'required': ['file_path']},
        }}],
    )
    prep = Prepared(req)
    msg, finish = prep.result('Let me check.\n<tool_call>{"name":"Read","arguments":{"file_path":"quickpool/compat.py"}}</tool_call>')
    assert finish == 'tool_calls'
    assert msg['content'] == 'Let me check.'
    assert msg['tool_calls'][0]['function']['name'] == 'Read'
    assert json.loads(msg['tool_calls'][0]['function']['arguments']) == {'file_path': 'quickpool/compat.py'}


def test_build_user_message_passes_chat_agent_id():
    msg = build_user_message('hi', 't-1', chat_agent_id='agent-123', incognito=True)
    assert msg['payload']['chat_agent_id'] == 'agent-123'
    assert msg['payload']['incognito'] is True
    msg_default = build_user_message('hi', 't-1')
    assert msg_default['payload']['chat_agent_id'] is None
    assert 'incognito' not in msg_default['payload']


def test_session_cache_isolates_scope_and_system_only_prefix():
    from quickpool.session_cache import SessionCache

    cache = SessionCache()
    sys_only = {'messages': [{'role': 'system', 'content': 'same system'}]}
    cache.save('acc-1', 't-sys', sys_only, 'smart', 'high', scope='k1')
    assert cache.match({'messages': [{'role': 'system', 'content': 'same system'}, {'role': 'user', 'content': 'hi'}]},
                       'smart', 'high', scope='k1') is None

    conv = {'messages': [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u1'}]}
    cache.save('acc-1', 't-k1', conv, 'smart', 'high', scope='k1')
    follow = {'messages': [*conv['messages'], {'role': 'assistant', 'content': 'a1'}, {'role': 'user', 'content': 'u2'}]}
    assert cache.match(follow, 'smart', 'high', scope='k2') is None
    assert cache.match(follow, 'smart', 'high', scope='k1')[0].thread_id == 't-k1'


def test_render_context_txt_is_plain_text_without_api_wrapper():
    from quickpool.native_upstream import Upstream

    payload = {
        'messages': [
            {'role': 'system', 'content': 'Work cleanly.'},
            {'role': 'user', 'content': 'Check status'},
        ],
        'tools': [{'type': 'function', 'function': {'name': 'Read', 'description': 'Read file', 'parameters': {'type': 'object'}}}],
    }
    txt = Upstream._render_context_txt(payload).decode('utf-8')
    assert '[Turn 0 · user]' in txt
    assert 'Check status' in txt
    assert 'Work cleanly.' not in txt
    assert 'api-context.json' not in txt
    assert 'API request' not in txt


async def test_warm_ws_pool_park_checkout_and_invalidate():
    from quickpool.ws_pool import WarmWsPool

    class FakeSocket:
        close_code = None
        def __init__(self):
            self.closed = False
            self.sent = []
        async def send(self, data):
            self.sent.append(data)
        async def close(self):
            self.closed = True
            self.close_code = 1000

    class FakeSession:
        transport = 'aidp'

    pool = WarmWsPool(max_sockets=4, idle_ttl=60.0, ping_interval=0)
    s1 = FakeSocket()
    await pool.park('t-1', 'acc-1', s1, object(), FakeSession())
    assert pool.has('t-1')
    assert 't-1' in pool.thread_ids()
    # Exclusive checkout pops socket from pool
    checked = await pool.checkout('t-1', 'acc-1')
    assert checked is not None and checked.socket is s1
    assert not pool.has('t-1')
    # Re-park and invalidate account closes socket
    await pool.park('t-1', 'acc-1', s1, object(), FakeSession())
    await pool.invalidate_account('acc-1')
    assert s1.closed
    assert not pool.has('t-1')


def test_normalize_agent_id():
    assert normalize_agent_id('arn:aws:quicksight:us-east-1:123:agent/abc-456') == 'abc-456'
    assert normalize_agent_id('  abc-456  ') == 'abc-456'


def test_settings_and_account_agent_select_api(gateway):
    client, app, admin, _, _ = gateway
    res = client.post('/api/accounts', headers=admin, json={
        'name': 'acct-agent', 'login_url': 'https://quick.aws.com/sn',
    })
    assert res.status_code == 201
    aid = res.json()['id']

    # Default settings include auto_ensure_coding_agent=False (opt-in)
    s = client.get('/api/settings', headers=admin).json()
    assert s['auto_ensure_coding_agent'] is False
    assert s['custom_agent_prompt'] == ''

    # Update custom agent settings
    patched = client.patch('/api/settings', headers=admin, json={
        'auto_ensure_coding_agent': True,
        'custom_agent_prompt': 'Custom coding prompt',
    })
    assert patched.status_code == 200
    assert patched.json()['auto_ensure_coding_agent'] is True
    assert patched.json()['custom_agent_prompt'] == 'Custom coding prompt'

    # Select & clear custom agent on account
    sel = client.post(f'/api/accounts/{aid}/agents/select', headers=admin, json={
        'agent_id': 'arn:aws:quicksight:us-east-1:111:agent/custom-001',
        'agent_name': DEFAULT_CODING_AGENT_NAME,
    })
    assert sel.status_code == 200
    assert sel.json() == {'bound_agent_id': 'custom-001', 'bound_agent_name': DEFAULT_CODING_AGENT_NAME}
    assert app.state.store.account(aid)['chat_agent_id'] == 'custom-001'

    cleared = client.post(f'/api/accounts/{aid}/agents/select', headers=admin, json={
        'agent_id': '', 'agent_name': '',
    })
    assert cleared.status_code == 200
    assert cleared.json() == {'bound_agent_id': '', 'bound_agent_name': ''}
