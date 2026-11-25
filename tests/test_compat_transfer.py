import base64
import json
import time

import httpx
import pytest
from pydantic import ValidationError

from quickpool.compat import CompatibilityError, Prepared
from quickpool.models import MODEL_IDS, AccountCreate, ChatRequest
from quickpool.native_upstream import NativeSession, Upstream, UpstreamError
from quickpool.store import Store
from quickpool.transfer import TransferError, export_accounts, import_accounts
from tests.test_gateway import add, request

pytest_plugins = ["tests.test_gateway"]


@pytest.mark.parametrize('model', MODEL_IDS + tuple(m.removeprefix('amazon-') for m in MODEL_IDS))
def test_each_model_reaches_native_payload(gateway, model):
    client, _app, _admin, key, backend = gateway
    add(gateway,state='ready')
    result = request(client,key,model=model)
    assert result.status_code == 200
    assert result.json()['model'] == model
    native = next(m['payload'] for m in backend.connections[-1]['socket'].sent if m['type']=='user_message')
    parts = model.removeprefix('amazon-').split('-')
    assert native['agent_mode'] == parts[0]
    assert native['thinking_effort'] == (parts[1] if len(parts)>1 else ('high' if parts[0]=='smart' else 'off'))


def test_models_match_upstream_vocabulary_and_default(gateway):
    client, _app, admin, key, _backend = gateway
    expected_public = ['claude-opus-5-5', 'claude-sonnet-4-6', 'claude-haiku-4-5']
    expected_internal = ['amazon-' + mode + suffix for mode in ('fast', 'balanced', 'smart')
                         for suffix in ('', '-low', '-medium', '-high')]
    models_data = client.get('/v1/models', headers=key).json()['data']
    assert [m['id'] for m in models_data] == expected_public
    assert all(m['owned_by'] == 'anthropic' for m in models_data)
    config = client.get('/api/config', headers=admin).json()
    assert config['models'] == expected_internal and config['model'] == 'amazon-fast'
    default = ChatRequest(messages=[{'role': 'user', 'content': 'Hello'}])
    assert (default.model, default.mode, default.effort) == ('amazon-fast', 'fast', 'off')


@pytest.mark.parametrize('mode', ['fast', 'balanced', 'smart'])
def test_explicit_agent_mode_is_forwarded_without_translation(gateway, mode):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready')
    result = request(client, key, model='amazon-smart-high', agent_mode=mode, thinking_effort='medium')
    assert result.status_code == 200
    native = next(m['payload'] for m in backend.connections[-1]['socket'].sent if m['type'] == 'user_message')
    assert native['agent_mode'] == mode and native['thinking_effort'] == 'medium'


@pytest.mark.parametrize('mode', ['quick', 'balance'])
def test_old_explicit_agent_mode_is_rejected(gateway, mode):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready')
    assert request(client, key, agent_mode=mode).status_code == 422
    assert not backend.connections


@pytest.mark.parametrize('old', [prefix + mode + suffix for prefix in ('amazon-', '')
                               for mode in ('quick', 'balance')
                               for suffix in ('', '-low', '-medium', '-high')])
def test_removed_model_names_do_not_fall_back(gateway, old):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready')
    assert request(client, key, model=old).status_code == 422
    assert not backend.connections


def test_bad_model_no_fallback_and_masked_config(gateway):
    client,app,admin,key,backend = gateway
    add(gateway,state='ready')
    result = request(client,key,model='hark')
    assert result.status_code == 422 and not backend.connections
    config = client.get('/api/config',headers=admin)
    assert app.state.store.api_key not in config.text and 'api_key' not in config.json()
    assert client.post('/api/config/reveal-key',headers=key).status_code == 401
    assert client.post('/api/config/reveal-key',headers=admin).json()['api_key'] == app.state.store.api_key


TOOLS = [{'type':'function','function':{'name':'weather','description':'Weather',
    'parameters':{'type':'object','properties':{'city':{'type':'string'}},'required':['city'],
                  'additionalProperties':False}}}]


@pytest.mark.parametrize('stream',[False,True])
def test_tool_adapter_real_model_output_conversion(gateway,stream):
    client,_app,_admin,key,_backend = gateway
    add(gateway,state='ready',behavior={'reply':'{"tool_calls":[{"name":"weather","arguments":{"city":"Paris"}}]}'})
    result = request(client,key,stream,tools=TOOLS,tool_choice='required')
    assert result.status_code == 200
    if stream:
        events = [json.loads(l[6:]) for l in result.text.splitlines() if l.startswith('data: {')]
        call = events[1]['choices'][0]['delta']['tool_calls'][0]
        assert call['index'] == 0 and events[-1]['choices'][0]['finish_reason']=='tool_calls'
        assert result.text.endswith('data: [DONE]\n\n')
    else:
        choice = result.json()['choices'][0]
        call = choice['message']['tool_calls'][0]
        assert choice['finish_reason']=='tool_calls' and choice['message']['content'] is None
    assert call['function']['name']=='weather' and json.loads(call['function']['arguments'])=={'city':'Paris'}


def test_tool_schema_violations_and_tool_result_history(gateway):
    client,app,_admin,key,backend = gateway
    account = add(gateway,state='ready',behavior={'reply':'{"tool_calls":[{"name":"weather","arguments":{"city":5}}]}'})
    assert request(client,key,tools=TOOLS).status_code == 502
    assert app.state.store.logs()[0]['status']==502
    app.state.store.patch_account(account['id'],{'state':'ready','cooldown_until':0})
    response = request(client,key,messages=[{'role':'user','content':'weather'},
        {'role':'assistant','content':None,'tool_calls':[{'id':'call_fixture','type':'function','function':{'name':'weather','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'call_fixture','content':'sunny'}])
    assert response.status_code == 200
    prompt = backend.connections[-1]['socket'].sent[-1]['payload']['text']
    assert 'call_fixture' in prompt and 'sunny' in prompt


def test_json_schema_and_remote_reference_rejected():
    request = ChatRequest(messages=[{'role':'user','content':'JSON'}],response_format={'type':'json_schema',
        'json_schema':{'name':'fixture','schema':{'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}}})
    prepared = Prepared(request)
    assert prepared.result('{"ok":true}')[0]['content']=='{"ok":true}'
    with pytest.raises(CompatibilityError):
        prepared.result('{"ok":"yes"}')
    request.response_format['json_schema']['schema'] = {'$ref':'http://127.0.0.1/private'}
    with pytest.raises(CompatibilityError):
        Prepared(request)
    with pytest.raises(ValidationError):
        ChatRequest(model='hark',messages=[{'role':'user','content':'hello'}])


def test_image_content_preflight_and_remote_ssrf():
    url = 'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
    request = ChatRequest(messages=[{'role':'user','content':[{'type':'text','text':'What image?'},
        {'type':'image_url','image_url':{'url':url}}]}])
    p = Prepared(request)
    assert len(p.images)==1 and 'base64' not in p.prompt and 'What image?' in p.prompt
    request.messages[0].content[1]['image_url']['url']='http://169.254.169.254/latest/meta-data/'
    with pytest.raises(CompatibilityError):
        Prepared(request)


async def test_native_image_upload_dlp_and_session_binding():
    calls = []
    session = NativeSession('social','PRIVATE_TOKEN','fixture.quick.aws.com','','','outpost-fixture')
    def handler(request):
        calls.append(request)
        assert request.headers['authorization']=='Bearer PRIVATE_TOKEN'
        if request.url.path.endswith('/upload-attachment'):
            assert request.content==b'image-bytes'
            assert request.headers['content-type']=='application/octet-stream'
            return httpx.Response(200,json={'attachmentId':'attachment-fixture'})
        if request.url.path.endswith('/attachment-dlp-status'):
            return httpx.Response(200,json={'dlpJobStatus':'COMPLETED','policyAction':'ALLOW'})
        body = json.loads(request.content)['eventBody']['invokeAgent']
        assert body['path']=='files/upload/conversation-fixture'
        assert body['workingSessionId']==session.working_id
        assert body['outpostId']==session.outpost_id
        assert body['body']['attachmentId']=='attachment-fixture'
        return httpx.Response(200,json={'eventResponse':{'invokeAgentResponse':json.dumps({
            'type':'api_response','status':201,'body':json.dumps({'path':'/tmp/fixture.png'})})}})
    upstream = Upstream(None,None,transport=httpx.MockTransport(handler))
    result = await upstream.upload_images(session,[(b'image-bytes','image/png')],'conversation-fixture')
    assert len(calls)==3 and result[0]['path']=='/tmp/fixture.png'
    assert result[0]['attachmentId']=='attachment-fixture' and len(result[0]['contentSha256'])==64
    def blocked(request):
        if request.url.path.endswith('/upload-attachment'):
            return httpx.Response(200,json={'attachmentId':'x'})
        return httpx.Response(200,json={'dlpJobStatus':'COMPLETED','policyAction':'WARN'})
    upstream.http_transport=httpx.MockTransport(blocked)
    with pytest.raises(UpstreamError,match='DLP'):
        await upstream.upload_images(session,[(b'image-bytes','image/png')],'conversation-fixture')


def test_encrypted_transfer_atomicity_and_new_local_keys(tmp_path):
    source, destination = Store(tmp_path/'source'),Store(tmp_path/'dest')
    try:
        account = source.create_account(AccountCreate(name='fixture'))
        creds = {'_kind':'desktop-oauth','provider':'social','clientId':'fixture',
            'tokenEndpoint':'https://oidc.us-east-1.amazonaws.com/token','tenantUrl':'https://fixture.quick.aws.com',
            'access_token':'PRIVATE_ACCESS','refresh_token':'PRIVATE_REFRESH','expires_at':time.time()+3600}
        source.set_credentials(account['id'],creds,state='ready')
        envelope = export_accounts(source,'fixture-passphrase-strong')
        raw = json.dumps(envelope)
        assert all(secret not in raw for secret in ('PRIVATE_ACCESS','PRIVATE_REFRESH',source.api_key,source.admin_key))
        with pytest.raises(TransferError):
            import_accounts(destination,envelope,'wrong-passphrase-strong')
        assert destination.accounts()==[]
        result = import_accounts(destination,envelope,'fixture-passphrase-strong')
        assert result['imported']==[account['id']]
        assert destination.credentials(account['id'])==creds
        assert destination.account(account['id'])['state']=='unverified'
        assert destination.api_key != source.api_key and destination.admin_key != source.admin_key
        with pytest.raises(TransferError,match='冲突'):
            import_accounts(destination,envelope,'fixture-passphrase-strong')
        assert len(destination.accounts())==1
        envelope['ciphertext']='AAAA'
        with pytest.raises(TransferError):
            import_accounts(destination,envelope,'fixture-passphrase-strong')
    finally:
        source.close(); destination.close()
