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
    msg = build_user_message('hi', 't-1', chat_agent_id='agent-123')
    assert msg['payload']['chat_agent_id'] == 'agent-123'
    msg_default = build_user_message('hi', 't-1')
    assert msg_default['payload']['chat_agent_id'] is None


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
