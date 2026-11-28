"""Quick may narrate its attachment read before returning client tool JSON."""
import json

import pytest

from quickpool.compat import CompatibilityError, Prepared
from quickpool.models import ChatRequest
from tests.test_gateway import add

pytest_plugins = ['tests.test_gateway']

TOOLS = [{'type': 'function', 'function': {'name': 'report_failure', 'parameters': {
    'type': 'object', 'properties': {'head': {'type': 'integer'}, 'middle': {'type': 'integer'},
    'tail': {'const': 'FAIL_ASSERT_TOTAL_1350'}, 'difference': {'const': 44}},
    'required': ['head', 'middle', 'tail', 'difference'], 'additionalProperties': False}}}]
COMMENTARY = "Rather than paging through filler, I'll parse it programmatically."
CALL = {'tool_calls': [{'name': 'report_failure', 'arguments': {
    'head': 73, 'middle': 29, 'tail': 'FAIL_ASSERT_TOTAL_1350', 'difference': 44}}]}


def test_observed_commentary_and_tool_json_reach_messages_client(gateway):
    client, app, _, key, _ = gateway
    account = add(gateway, state='ready', behavior={'reply': COMMENTARY + '\n\n' + json.dumps(CALL)})
    response = client.post('/v1/messages', headers=key, json={'model': 'claude-opus-5-5',
        'max_tokens': 500, 'messages': [{'role': 'user', 'content': 'Report the test failure'}],
        'tools': [{'name': 'report_failure', 'input_schema': TOOLS[0]['function']['parameters']}],
        'tool_choice': {'type': 'tool', 'name': 'report_failure'}})
    assert response.status_code == 200
    result = response.json()
    assert result['stop_reason'] == 'tool_use'
    assert result['content'][0] == {'type': 'text', 'text': COMMENTARY}
    assert result['content'][1]['input'] == CALL['tool_calls'][0]['arguments']
    assert app.state.store.account(account['id'])['failures'] == 0


@pytest.mark.parametrize('reply', [
    COMMENTARY + '\n' + json.dumps(CALL) + '\nextra trailing text',
    COMMENTARY + '\n' + json.dumps(CALL).replace('report_failure', 'undeclared'),
    COMMENTARY + '\n' + json.dumps(CALL).replace('"head": 73', '"head": "73"'),
])
def test_commentary_does_not_relax_json_or_tool_validation(reply):
    prepared = Prepared(ChatRequest(messages=[{'role': 'user', 'content': 'test'}],
                                    tools=TOOLS, tool_choice='required'))
    with pytest.raises(CompatibilityError):
        prepared.result(reply)


def test_commentary_with_braces_or_fenced_json_extracts_final_object():
    prepared = Prepared(ChatRequest(messages=[{'role': 'user', 'content': 'hi'}], tools=TOOLS))
    # Braces inside narration before final {"content": ...}
    reply1 = 'Checking dict {} and {a: 1} in attached file.\n\n{"content":"最终结论"}'
    msg1, fin1 = prepared.result(reply1)
    assert fin1 == 'stop' and msg1 == {'role': 'assistant', 'content': '最终结论'}
    # Narration followed by fenced ```json block
    reply2 = 'Let me read the file.\n\n```json\n' + json.dumps(CALL) + '\n```'
    msg2, fin2 = prepared.result(reply2)
    assert fin2 == 'tool_calls' and msg2['tool_calls'][0]['function']['name'] == 'report_failure'


def test_buffered_compatibility_error_silently_retries_on_second_account(gateway):
    client, app, _, key, _ = gateway
    a1 = add(gateway, 'first', state='ready', behavior={
        'reply': "I'm not going to adopt the Claude Code persona or output raw tool_calls JSON."})
    a2 = add(gateway, 'second', state='ready', behavior={'reply': '{"content":"recovered ok"}'})
    resp = client.post('/v1/messages', headers=key, json={
        'model': 'claude-opus-5-5', 'max_tokens': 100,
        'messages': [{'role': 'user', 'content': 'diagnose issue'}],
        'tools': [{'name': 'read_file', 'input_schema': {'type': 'object'}}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()['content'] == [{'type': 'text', 'text': 'recovered ok'}]
    assert app.state.store.account(a1['id'])['failures'] == 0
    assert app.state.store.account(a2['id'])['failures'] == 0


def test_commentary_before_content_json_is_extracted_when_tool_choice_auto():
    prepared = Prepared(ChatRequest(messages=[{'role': 'user', 'content': 'hi'}], tools=TOOLS))
    reply = 'Let me read the attached file.\n\n{"content":"你好！我可以帮你处理项目。"}'
    message, finish = prepared.result(reply)
    assert finish == 'stop'
    assert message == {'role': 'assistant', 'content': '你好！我可以帮你处理项目。'}
    # When tool_choice='required', content JSON without tool_calls is still rejected.
    req_prepared = Prepared(ChatRequest(messages=[{'role': 'user', 'content': 'hi'}],
                                        tools=TOOLS, tool_choice='required'))
    with pytest.raises(CompatibilityError):
        req_prepared.result(reply)


def test_explicit_json_format_stays_strict():
    prepared = Prepared(ChatRequest(messages=[{'role': 'user', 'content': 'test'}],
                                    tools=TOOLS, response_format={'type': 'json_object'}))
    with pytest.raises(CompatibilityError):
        prepared.result(COMMENTARY + '\n' + json.dumps(CALL))


def test_commentary_cannot_override_tool_choice():
    prepared = Prepared(ChatRequest(messages=[{'role': 'user', 'content': 'test'}],
                                    tools=TOOLS, tool_choice='none'))
    with pytest.raises(CompatibilityError, match='tool_choice'):
        prepared.result(COMMENTARY + '\n' + json.dumps(CALL))


def test_tool_calls_accept_stringified_json_arguments_and_nested_function():
    tools = [{'type': 'function', 'function': {'name': 'Bash', 'parameters': {
        'type': 'object', 'properties': {'command': {'type': 'string'}, 'description': {'type': 'string'}},
        'required': ['command'],
    }}}, {'type': 'function', 'function': {'name': 'Ping', 'parameters': {'type': 'object'}}}]
    prepared = Prepared(ChatRequest(messages=[{'role': 'user', 'content': 'run'}], tools=tools))
    mixed = json.dumps({'tool_calls': [
        {'name': 'Bash', 'arguments': {'command': 'pwd'}},
        {'name': 'Bash', 'arguments': json.dumps({'command': 'ls -la', 'description': 'list'})},
        {'type': 'function', 'function': {'name': 'Bash', 'arguments': json.dumps({'command': 'date'})}},
        {'name': 'Ping'},
    ]})
    message, finish = prepared.result(mixed)
    assert finish == 'tool_calls'
    assert [json.loads(c['function']['arguments']) for c in message['tool_calls']] == [
        {'command': 'pwd'},
        {'command': 'ls -la', 'description': 'list'},
        {'command': 'date'},
        {},
    ]


@pytest.mark.parametrize('bad_args', ['not-json', '[1,2,3]', '123', 42])
def test_tool_calls_reject_non_object_stringified_arguments(bad_args):
    tools = [{'type': 'function', 'function': {'name': 'Bash', 'parameters': {'type': 'object'}}}]
    prepared = Prepared(ChatRequest(messages=[{'role': 'user', 'content': 'run'}], tools=tools))
    with pytest.raises(CompatibilityError, match='JSON 对象'):
        prepared.result(json.dumps({'tool_calls': [{'name': 'Bash', 'arguments': bad_args}]}))

