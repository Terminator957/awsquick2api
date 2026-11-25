import asyncio
import json

import pytest
from fastapi.responses import StreamingResponse

from quickpool.cli_api import message_stream, prepare, response_stream
from quickpool.models import MODEL_IDS
from tests.test_gateway import add

pytest_plugins = ['tests.test_gateway']

FUNCTION = {'type': 'function', 'name': 'read_file', 'description': 'Read a file',
            'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                           'required': ['path'], 'additionalProperties': False}}
ANTHROPIC_TOOL = {'name': 'read_file', 'description': 'Read a file', 'input_schema': FUNCTION['parameters']}
TOOL_REPLY = '{"tool_calls":[{"name":"read_file","arguments":{"path":"marker.txt"}}]}'


def payload(protocol, stream=False, **extra):
    body = {'model': 'amazon-smart-high', 'stream': stream}
    body.update({'input': 'Hello', 'store': False} if protocol == 'responses' else
                {'messages': [{'role': 'user', 'content': 'Hello'}], 'max_tokens': 1024})
    return {**body, **extra}


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('model', MODEL_IDS + tuple(m.removeprefix('amazon-') for m in MODEL_IDS))
def test_model_vocabulary_reaches_upstream_unchanged(gateway, protocol, model):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready')
    response = client.post('/v1/' + protocol, headers=key, json=payload(protocol, model=model))
    assert response.status_code == 200, response.text
    assert response.json()['model'] == model
    mode, _, effort = model.removeprefix('amazon-').partition('-')
    native = next(m['payload'] for m in backend.connections[-1]['socket'].sent if m['type'] == 'user_message')
    default = 'high' if mode == 'smart' else 'off'
    assert native['agent_mode'] == mode and native['thinking_effort'] == (effort or default)


@pytest.mark.parametrize('protocol', ['chat/completions', 'responses', 'messages'])
@pytest.mark.parametrize('model, mode', [
    ('opus', 'smart'), ('sonnet', 'balanced'), ('haiku', 'fast'),
    ('claude-opus-4-6', 'smart'), ('claude-sonnet-4-6', 'balanced'),
    ('claude-haiku-4-5-20251001', 'fast'), ('claude-opus-latest', 'smart'),
    ('claude-3-7-sonnet-20250219', 'balanced'), ('claude-3-haiku-20240307', 'fast'),
])
def test_claude_model_alias_selects_native_mode(gateway, protocol, model, mode):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready')
    response = client.post('/v1/' + protocol, headers=key, json=payload(protocol, model=model))
    assert response.status_code == 200, response.text
    assert response.json()['model'] == model
    native = next(m['payload'] for m in backend.connections[-1]['socket'].sent if m['type'] == 'user_message')
    assert native['agent_mode'] == mode and native['thinking_effort'] == ('high' if mode == 'smart' else 'off')


@pytest.mark.parametrize('model, mode', [('claude-opus-4-6', 'smart'),
                                       ('claude-sonnet-4-6', 'balanced'), ('claude-haiku-4-5', 'fast')])
def test_claude_messages_stream_preserves_alias_and_thinking(gateway, model, mode):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready')
    body = payload('messages', True, model=model, thinking={'type': 'enabled', 'budget_tokens': 8192})
    response = client.post('/v1/messages', headers=key, json=body)
    assert response.status_code == 200, response.text
    data = events(response)
    assert data[0]['message']['model'] == model and data[-1]['type'] == 'message_stop'
    native = next(m['payload'] for m in backend.connections[-1]['socket'].sent if m['type'] == 'user_message')
    assert native['agent_mode'] == mode and native['thinking_effort'] == 'medium'
    before = len(backend.connections)
    counted = client.post('/v1/messages/count_tokens', headers=key, json=body)
    assert counted.status_code == 200 and counted.json()['input_tokens'] > 0
    assert len(backend.connections) == before


@pytest.mark.parametrize('extra,expected_effort', [
    ({'reasoning_effort': 'none'}, 'off'),
    ({'reasoning_effort': 'minimal'}, 'low'),
    ({'reasoning_effort': 'low'}, 'low'),
    ({'reasoning_effort': 'medium'}, 'medium'),
    ({'reasoning_effort': 'high'}, 'high'),
    ({'reasoning_effort': 'xhigh'}, 'high'),
    ({'reasoning_effort': 'max'}, 'high'),
    ({'reasoning': {'effort': 'low'}}, 'low'),
    ({'thinking': {'type': 'disabled'}}, 'off'),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 2048}}, 'low'),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 10000}}, 'medium'),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 20000}}, 'high'),
    ({'thinking': {'type': 'adaptive'}}, 'medium'),
    ({'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'low'}}, 'low'),
    ({'model': 'claude-opus-5-5-low'}, 'low'),
    ({'model': 'claude-sonnet-4-6-medium'}, 'medium'),
    ({'model': 'claude-opus-5-5-high'}, 'high'),
    ({'model': 'claude-opus-5-5-off'}, 'off'),
])
def test_chat_completions_maps_client_thinking_depth_variants(gateway, extra, expected_effort):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready')
    body = {'model': 'claude-opus-5-5', 'messages': [{'role': 'user', 'content': 'hello'}], **extra}
    response = client.post('/v1/chat/completions', headers=key, json=body)
    assert response.status_code == 200, response.text
    native = next(m['payload'] for m in backend.connections[-1]['socket'].sent if m['type'] == 'user_message')
    assert native['thinking_effort'] == expected_effort


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('old', ['amazon-quick', 'amazon-quick-high', 'quick-medium',
                               'amazon-balance', 'amazon-balance-high', 'balance-low', 'hark',
                               'claude-unknown-4-6', 'claude-sonnet-invalid', 'gpt-unknown'])
def test_removed_model_names_are_rejected(gateway, protocol, old):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready')
    response = client.post('/v1/' + protocol, headers=key, json=payload(protocol, model=old))
    assert response.status_code == 400
    assert not backend.connections


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('stream', [False, True])
def test_text_protocol_and_alias(gateway, protocol, stream):
    client, app, _admin, key, backend = gateway
    account = add(gateway, state='ready')
    response = client.post('/v1/' + protocol, headers=key, json=payload(protocol, stream))
    assert response.status_code == 200, response.text
    if stream:
        data = events(response)
        if protocol == 'responses':
            assert data[0]['type'] == 'response.created'
            assert data[-1]['type'] == 'response.completed'
            assert [e['sequence_number'] for e in data] == list(range(len(data)))
            assert ''.join(e['delta'] for e in data if e['type'] == 'response.output_text.delta') == '你好，Quick'
            assert data[-1]['response']['output'][0]['content'][0]['text'] == '你好，Quick'
        else:
            assert data[0]['type'] == 'message_start' and data[-1]['type'] == 'message_stop'
            assert data[-2]['delta']['stop_reason'] == 'end_turn'
            assert ''.join(e['delta']['text'] for e in data if e['type'] == 'content_block_delta') == '你好，Quick'
    elif protocol == 'responses':
        assert response.json()['output'][0]['content'][0]['text'] == '你好，Quick'
        assert response.json()['usage'] is None and response.json()['store'] is False
    else:
        assert response.json()['content'] == [{'type': 'text', 'text': '你好，Quick'}]
        assert response.json()['usage']['input_tokens'] > 0
        assert response.headers['x-quickpool-usage'] == 'estimated'
    assert app.state.pool.inflight[account['id']] == 0 and backend.connections[-1]['closed']
    assert client.post('/' + protocol, headers=key, json=payload(protocol)).status_code == 200


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('stream', [False, True])
def test_function_tools_stream_and_roundtrip(gateway, protocol, stream):
    client, _app, _admin, key, backend = gateway
    add(gateway, state='ready', behavior={'reply': TOOL_REPLY})
    body = payload(protocol, stream, tools=[FUNCTION if protocol == 'responses' else ANTHROPIC_TOOL],
                   tool_choice='required' if protocol == 'responses' else {'type': 'any'})
    response = client.post('/v1/' + protocol, headers=key, json=body)
    assert response.status_code == 200, response.text
    if stream:
        data = events(response)
        if protocol == 'responses':
            call = data[-1]['response']['output'][0]
            assert any(e['type'] == 'response.function_call_arguments.delta' for e in data)
        else:
            call = next(e['content_block'] for e in data if e['type'] == 'content_block_start')
            delta = next(e['delta']['partial_json'] for e in data if e['type'] == 'content_block_delta')
            call['input'] = json.loads(delta)
            assert data[-2]['delta']['stop_reason'] == 'tool_use'
    else:
        call = response.json()['output' if protocol == 'responses' else 'content'][0]
    assert call['name'] == 'read_file'
    if protocol == 'responses':
        assert json.loads(call['arguments']) == {'path': 'marker.txt'}
        history = [{'role': 'user', 'content': 'Read the file'}, call,
                   {'type': 'function_call_output', 'call_id': call['call_id'], 'output': 'MARKER_OK'}]
        body = payload(protocol, input=history)
    else:
        assert call['input'] == {'path': 'marker.txt'}
        body = payload(protocol, messages=[{'role': 'user', 'content': 'Read the file'},
            {'role': 'assistant', 'content': [call]}, {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': call['id'], 'content': 'MARKER_OK'}]}])
    backend.behaviors['a'] = {'reply': 'MARKER_OK'}
    result = client.post('/v1/' + protocol, headers=key, json=body)
    assert result.status_code == 200, result.text
    native = next(m['payload'] for m in backend.connections[-1]['socket'].sent if m['type'] == 'user_message')
    assert 'MARKER_OK' in json.dumps(native) and ('call_id' in json.dumps(native) or 'tool_call_id' in json.dumps(native))


def test_responses_custom_tool_and_namespace(gateway):
    client, _app, _admin, key, backend = gateway
    definition = {'type': 'namespace', 'name': 'functions', 'tools': [FUNCTION]}
    _request, registry = prepare(payload('responses', tools=[definition]), 'responses')
    alias = next(iter(registry.entries))
    add(gateway, state='ready', behavior={'reply': json.dumps({'tool_calls': [
        {'name': alias, 'arguments': {'path': 'marker.txt'}}]})})
    response = client.post('/v1/responses', headers=key, json=payload('responses', tools=[definition]))
    call = response.json()['output'][0]
    assert call['namespace'] == 'functions' and call['name'] == 'read_file'
    custom = {'type': 'custom', 'name': 'apply_patch', 'format': {'type': 'text'}}
    backend.behaviors['a'] = {'reply': '{"tool_calls":[{"name":"apply_patch","arguments":{"input":"PATCH"}}]}'}
    response = client.post('/v1/responses', headers=key, json=payload('responses', True, tools=[custom]))
    data = events(response)
    call = data[-1]['response']['output'][0]
    assert call['type'] == 'custom_tool_call' and call['input'] == 'PATCH'
    assert any(e['type'] == 'response.custom_tool_call_input.delta' for e in data)
    body = payload('responses', tools=[custom], input=[call,
        {'type': 'custom_tool_call_output', 'call_id': call['call_id'], 'output': 'applied'}])
    chat, _ = prepare(body, 'responses')
    assert chat.messages[0].tool_calls[0]['function']['arguments'] == '{"input": "PATCH"}'


def test_messages_auth_count_and_errors(gateway):
    client, app, admin, key, backend = gateway
    x_key = {'x-api-key': app.state.store.api_key, 'anthropic-version': '2023-06-01'}
    assert client.post('/v1/messages', headers=admin, json=payload('messages')).status_code == 401
    assert client.post('/v1/messages', json=payload('messages')).json()['type'] == 'error'
    before = len(backend.connections)
    body = {'model': 'amazon-smart', 'messages': [{'role': 'user', 'content': 'Hello'}]}
    result = client.post('/v1/messages/count_tokens', headers=x_key, json=body)
    assert result.status_code == 200 and result.json()['input_tokens'] > 0
    assert result.headers['x-quickpool-usage'] == 'estimated' and len(backend.connections) == before
    add(gateway, state='ready')
    assert client.post('/v1/messages', headers=x_key, json=payload('messages')).status_code == 200
    assert client.post('/v1/chat/completions', headers=x_key,
                       json={'model': 'amazon-smart', 'messages': body['messages']}).status_code == 401
    bad = client.post('/v1/messages', headers=key, json=payload('messages', model='hark'))
    assert bad.status_code == 400 and bad.json()['error']['type'] == 'invalid_request_error'


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
@pytest.mark.parametrize('bad', [None, [], 'private prompt', {'stream': 'yes'}, {'tools': None}, {'model': 'hark'}])
def test_bad_request_is_sanitized(gateway, protocol, bad):
    client, _app, _admin, key, backend = gateway
    body = payload(protocol, **bad) if isinstance(bad, dict) else bad
    response = client.post('/v1/' + protocol, headers=key, json=body)
    assert response.status_code == 400 and 'private prompt' not in response.text
    assert not backend.connections


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
def test_error_before_stream_and_midstream(gateway, protocol):
    client, app, _admin, key, backend = gateway
    unavailable = client.post('/v1/' + protocol, headers=key, json=payload(protocol, True))
    assert unavailable.status_code == 503 and unavailable.headers['retry-after'] == '10'
    account = add(gateway, state='ready', behavior={'kind': 'late-error'})
    response = client.post('/v1/' + protocol, headers=key, json=payload(protocol, True))
    data = events(response)
    assert response.status_code == 200
    assert data[-1]['type'] == ('response.failed' if protocol == 'responses' else 'error')
    assert not any(e['type'] in ('response.completed', 'message_stop') for e in data)
    assert 'PRIVATE_UPSTREAM_BODY' not in response.text and app.state.pool.inflight[account['id']] == 0
    assert len(backend.connections) == 1 and backend.connections[0]['closed']


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
async def test_disconnect_closes_shared_stream(protocol):
    closed = asyncio.Event()

    async def source():
        try:
            yield 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            yield 'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        finally:
            closed.set()

    request, tools = prepare(payload(protocol, True), protocol)
    result = StreamingResponse(source())
    stream = response_stream(result, request, tools) if protocol == 'responses' else (
        message_stream(result, request, tools, 10))
    await anext(stream)
    await stream.aclose()
    assert closed.is_set()


def test_history_images_reasoning_and_structured_output():
    image = 'data:image/png;base64,iVBORw0KGgo='
    body = payload('responses', instructions='System', reasoning={'effort': 'high'},
        text={'format': {'type': 'json_schema', 'name': 'value', 'schema': {'type': 'object'}}},
        input=[{'type': 'reasoning', 'summary': []}, {'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Describe'}, {'type': 'input_image', 'image_url': image}]}])
    request, _ = prepare(body, 'responses')
    assert request.messages[0].role == 'system' and request.effort == 'high'
    assert request.messages[1].content[1]['image_url']['url'] == image
    assert request.response_format['json_schema']['schema'] == {'type': 'object'}
    request, _ = prepare(payload('messages', thinking={'type': 'enabled', 'budget_tokens': 8192},
                                system=[{'type': 'text', 'text': 'System', 'cache_control': {'type': 'ephemeral'}}]),
                         'messages')
    assert request.effort == 'medium'
    request, _ = prepare(payload('messages', messages=[{'role': 'user', 'content': 'Question'},
        {'role': 'system', 'content': [{'type': 'text', 'text': 'CLI environment'}]}]), 'messages')
    assert [m.role for m in request.messages] == ['system', 'user']


@pytest.mark.parametrize('extra', [{'previous_response_id': 'resp_secret'}, {'store': True},
    {'background': True}, {'input': [{'type': 'item_reference', 'id': 'secret'}]},
    {'tools': [{'type': 'web_search'}]}])
def test_no_silent_state_or_builtin_tool_fallback(gateway, extra):
    client, _app, _admin, key, backend = gateway
    response = client.post('/v1/responses', headers=key, json=payload('responses', **extra))
    assert response.status_code == 400 and not backend.connections
