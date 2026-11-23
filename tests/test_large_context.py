"""Regressions from the native desktop FrameTooLarge trace and attachment protocol."""
import json

import httpx
import pytest

from quickpool.native_upstream import NativeSession, Upstream, UpstreamError
from tests.test_gateway import NativeSocket, add, output_frame

pytest_plugins = ['tests.test_gateway']


def test_top_level_frame_error_is_not_retried_or_account_cooldown(gateway, monkeypatch):
    client, app, _, key, backend = gateway
    accounts = [add(gateway, str(i), state='ready') for i in range(3)]

    def reject(socket, thread_id):
        socket.frames.append(output_frame({'type': 'error', 'thread_id': thread_id,
            'code': 'FrameTooLarge', 'retryable': False, 'source': 'sncs',
            'message': 'PRIVATE_UPSTREAM_BODY', 'failed_frame_type': 'user_message'}))

    monkeypatch.setattr(NativeSocket, 'prepare_response', reject)
    response = client.post('/v1/messages', headers=key, json={
        'model': 'claude-opus-5-5', 'max_tokens': 100,
        'messages': [{'role': 'user', 'content': 'hi'}]})
    assert response.status_code == 400
    assert response.json()['error']['type'] == 'invalid_request_error'
    assert 'FrameTooLarge' in response.text and 'PRIVATE_UPSTREAM_BODY' not in response.text
    assert len(backend.connections) == 1
    for account in accounts:
        current = app.state.store.account(account['id'])
        assert current['state'] == 'ready' and current['cooldown_until'] == 0
        assert current['failures'] == 0


@pytest.mark.parametrize('action', ['ALLOW', 'WARN', 'BLOCK', None])
async def test_generic_context_attachment_preserves_bytes_and_dlp(action):
    raw = ('head\n' + '上下文\n' * 9000 + 'tail').encode()
    session = NativeSession('social', 'FIXTURE', 'fixture.quick.aws.com', '', '', 'outpost')
    delivered = []

    def handle(request):
        if request.url.path.endswith('/upload-attachment'):
            assert request.content == raw
            assert dict(request.url.params) == {
                'name': 'context.txt', 'contentType': 'text/plain', 'contentLength': str(len(raw))}
            return httpx.Response(200, json={'attachmentId': 'attachment'})
        if request.url.path.endswith('/attachment-dlp-status'):
            return httpx.Response(200, json={'dlpJobStatus': 'NOT_APPLICABLE', 'policyAction': action})
        body = json.loads(request.content)['eventBody']['invokeAgent']
        assert body['path'] == 'files/upload/thread'
        delivered.append(body)
        return httpx.Response(200, json={'eventResponse': {'invokeAgentResponse': {
            'status': 200, 'body': {'path': '/workspace/attached_files/context.txt'}}}})

    upstream = Upstream(None, None, transport=httpx.MockTransport(handle))
    files = [{'data': raw, 'name': 'context.txt', 'mimeType': 'text/plain', 'type': 'file'}]
    if action != 'ALLOW':
        with pytest.raises(UpstreamError, match='DLP'):
            await upstream.upload_files(session, files, 'thread')
        assert not delivered
    else:
        attachments = await upstream.upload_files(session, files, 'thread')
        assert len(delivered) == 1
        assert attachments[0]['type'] == 'file'
        assert attachments[0]['size'] == len(raw)
        assert attachments[0]['name'] == 'context.txt'
        assert 'data' not in attachments[0]


@pytest.mark.parametrize('late', [False, True])
def test_nonretryable_upstream_error_preserves_account_and_stream_failure(gateway, monkeypatch, late):
    client, app, _, key, backend = gateway
    account = add(gateway, state='ready')
    add(gateway, 'b', state='ready')

    def reject(socket, thread_id):
        if late:
            socket.frames.append(output_frame({'type':'text_delta','thread_id':thread_id,
                'message_id':'answer','payload':{'delta':'partial'}}))
        socket.frames.append(output_frame({'type':'error','thread_id':thread_id,
            'code':'UnknownTerminalError','retryable':False,'message':'PRIVATE_UPSTREAM_BODY'}))

    monkeypatch.setattr(NativeSocket, 'prepare_response', reject)
    response = client.post('/v1/messages', headers=key, json={
        'model':'amazon-smart','max_tokens':100,'stream':late,
        'messages':[{'role':'user','content':'hi'}]})
    assert response.status_code == (200 if late else 502)
    assert 'PRIVATE_UPSTREAM_BODY' not in response.text
    if late:
        assert 'event: error' in response.text
        assert 'event: message_stop' not in response.text
    assert len(backend.connections) == 1
    assert app.state.store.account(account['id'])['state'] == 'ready'
    assert app.state.store.account(account['id'])['failures'] == 0


def test_prepared_context_keeps_roles_tools_and_large_tool_result():
    from quickpool.compat import Prepared
    from quickpool.models import ChatRequest
    schema = {'type':'object','properties':{'mode':{'enum':['read','write']}},'required':['mode']}
    messages = [{'role':'system','content':'System instruction'},
        {'role':'user','content':'Read then test'},
        {'role':'assistant','tool_calls':[{'id':'call_exact','type':'function',
            'function':{'name':'check','arguments':'{"mode":"read"}'}}]},
        {'role':'tool','tool_call_id':'call_exact','content':'start\n'+'日志\n'*80000+'\nFAILURE_AT_TAIL'}]
    tools = [{'type':'function','function':{'name':'check','parameters':schema,'description':'Keep this'}}]
    prepared = Prepared(ChatRequest(messages=messages, tools=tools, tool_choice='required', parallel_tool_calls=False))
    assert prepared.context['messages'] == messages
    assert prepared.context['tools'] == tools
    assert prepared.context['tool_choice'] == 'required'
    assert prepared.context['parallel_tool_calls'] is False
    assert 'tool_calls' in prepared.context['adapter_instructions']


@pytest.mark.parametrize('prompt,offload,with_image', [('small',False,False), ('a'*43000,False,False),
    ('a'*45000,True,False), ('中文'*8000,True,False), ('"\\\n'*8000,True,False), ('a'*43000,True,True)])
async def test_stream_offloads_by_encoded_bytes_without_truncation(monkeypatch, prompt, offload, with_image):
    import base64
    from contextlib import asynccontextmanager
    from collections import deque
    session = NativeSession('social','FIXTURE','fixture.quick.aws.com','','','outpost')
    frames, sent, uploaded = deque(), [], []
    class Socket:
        async def send(self, raw):
            event = json.loads(base64.b64decode(json.loads(raw)['payload']['payload']))
            sent.append((raw,event))
            if event['type']=='user_message':
                tid = event['payload']['thread_id']
                frames.extend([json.dumps({'type':'text_delta','thread_id':tid,'message_id':'reply','payload':{'delta':'OK'}}),
                    json.dumps({'type':'message_complete','thread_id':tid,'message_id':'reply','payload':{}})])
        async def recv(self): return frames.popleft()
    @asynccontextmanager
    async def connect(*args, **kwargs): yield Socket()
    upstream = Upstream(None,None,connect=connect)
    async def upload(session, files, thread):
        uploaded.extend(files)
        return [{'type':f['type'],'name':f['name'],'size':len(f['data']),'mimeType':f['mimeType'],
                 'path':'/workspace/'+('i'*2000 if f['type']=='image' else '')+f['name'],
                 'attachmentId':'fixture'} for f in files]
    monkeypatch.setattr(upstream,'upload_files',upload)
    context = {'messages':[{'role':'user','content':prompt}],'tools':[]}
    result = ''.join([part async for part in upstream._stream(session,'id',
        [{'role':'user','content':prompt}],context=context,
        images=[(b'fixture_image','image/png')] if with_image else None)])
    assert result == 'OK'
    raw, message = next((raw,e) for raw,e in sent if e['type']=='user_message')
    assert len(raw) <= 60000
    documents = [f for f in uploaded if f['type']=='file']
    assert bool(documents) is offload
    if with_image:
        assert uploaded[0]['data']==b'fixture_image'
        assert message['payload']['attachments'][0]['type']=='image'
    if offload:
        assert json.loads(documents[0]['data']) == context
        assert documents[0]['mimeType']=='application/json'
        assert message['payload']['attachments'][-1]['name']=='api-context.json'
    else:
        assert message['payload']['text']==prompt


def test_large_gateway_request_fails_clearly_without_account_penalty(gateway):
    client, app, _, key, backend = gateway
    account = add(gateway,state='ready')
    response = client.post('/v1/messages',headers=key,json={'model':'amazon-smart','max_tokens':100,
        'messages':[{'role':'user','content':'a'*90000}]})
    assert response.status_code == 400
    assert 'AIDP' in response.json()['error']['message']
    assert len(backend.connections)==1
    assert not any(m['type']=='user_message' for m in backend.connections[0]['socket'].sent)
    assert app.state.store.account(account['id'])['state']=='ready'


@pytest.mark.parametrize('buffered', [False, True])
def test_late_request_error_keeps_http_classification_before_headers(gateway, monkeypatch, buffered):
    client, app, _, key, _ = gateway
    account = add(gateway,state='ready')
    def reject(socket, thread_id):
        socket.frames.append(output_frame({'type':'text_delta','thread_id':thread_id,
            'message_id':'answer','payload':{'delta':'partial'}}))
        socket.frames.append(output_frame({'type':'error','thread_id':thread_id,'code':'FrameTooLarge','retryable':False}))
    monkeypatch.setattr(NativeSocket,'prepare_response',reject)
    body={'model':'amazon-smart','max_tokens':100,'messages':[{'role':'user','content':'hi'}]}
    if buffered: body['tools']=[{'name':'check','input_schema':{'type':'object'}}]
    response=client.post('/v1/messages',headers=key,json=body)
    assert response.status_code==400
    assert response.json()['error']['type']=='invalid_request_error'
    assert app.state.store.account(account['id'])['state']=='ready'


def test_request_body_limit_remains_bounded(gateway):
    client, app, _, key, backend = gateway
    app.state.store.patch_settings({'max_body_mb': 5})
    response=client.post('/v1/messages',headers=key,json={'model':'amazon-smart','max_tokens':1,
        'messages':[{'role':'user','content':'x'*(5*1024*1024)}]})
    assert response.status_code==413
    assert not backend.connections


def test_invalid_tool_json_is_not_an_account_failure(gateway):
    client, app, _, key, _ = gateway
    account=add(gateway,state='ready',behavior={'reply':'Not a JSON response'})
    response=client.post('/v1/messages',headers=key,json={'model':'amazon-smart','max_tokens':100,
        'messages':[{'role':'user','content':'hi'}], 'tools':[{'name':'check','input_schema':{'type':'object'}}]})
    assert response.status_code==502
    assert 'JSON' in response.json()['error']['message']
    assert app.state.store.account(account['id'])['state']=='ready'
    assert app.state.store.account(account['id'])['failures']==0


async def test_offload_includes_inline_recent_turns_and_multiline_attachment(monkeypatch):
    import base64
    from contextlib import asynccontextmanager
    from collections import deque
    session = NativeSession('social', 'FIXTURE', 'fixture.quick.aws.com', '', '', 'outpost')
    frames, sent, uploaded = deque(), [], []

    class Socket:
        async def send(self, raw):
            event = json.loads(base64.b64decode(json.loads(raw)['payload']['payload']))
            sent.append((raw, event))
            if event['type'] == 'user_message':
                tid = event['payload']['thread_id']
                frames.extend([
                    json.dumps({'type': 'text_delta', 'thread_id': tid, 'message_id': 'reply', 'payload': {'delta': 'OK'}}),
                    json.dumps({'type': 'message_complete', 'thread_id': tid, 'message_id': 'reply', 'payload': {}}),
                ])

        async def recv(self):
            return frames.popleft()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield Socket()

    upstream = Upstream(None, None, connect=connect)

    async def upload(session, files, thread):
        uploaded.extend(files)
        return [{'type': f['type'], 'name': f['name'], 'size': len(f['data']), 'mimeType': f['mimeType'],
                 'path': '/workspace/' + f['name'], 'attachmentId': 'fixture'} for f in files]

    monkeypatch.setattr(upstream, 'upload_files', upload)
    messages = [
        {'role': 'system', 'content': 'SYSTEM_RULE_MARKER'},
        {'role': 'user', 'content': 'older_turn_' + ('x' * 50000)},
        {'role': 'assistant', 'content': 'mid_turn_answer'},
        {'role': 'user', 'content': 'LATEST_USER_QUESTION_MARKER'},
    ]
    tools = [{'type': 'function', 'function': {
        'name': 'EditFile', 'description': 'Edit a file' * 40,
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string', 'description': 'long' * 50}}, 'required': ['path']}
    }}]
    context = {'messages': messages, 'tools': tools, 'adapter_instructions': 'ADAPTER_MARKER'}
    result = ''.join([part async for part in upstream._stream(
        session, 'id', [{'role': 'user', 'content': 'fallback' * 10000}], context=context)])
    assert result == 'OK'
    raw, message = next((raw, e) for raw, e in sent if e['type'] == 'user_message')
    assert len(raw) <= 60000
    inline_text = message['payload']['text']
    assert 'LATEST_USER_QUESTION_MARKER' in inline_text
    assert 'EditFile' in inline_text
    assert 'SYSTEM_RULE_MARKER' in inline_text
    assert 'WITHOUT calling file_read or run_python' in inline_text
    assert uploaded and b'\n' in uploaded[0]['data']
    assert json.loads(uploaded[0]['data']) == context

