"""Stateless Responses and Anthropic Messages adapters over the shared chat engine.

Quick tools remain a validated prompt/JSON adapter. Clients execute their tools.
Anthropic requires numeric usage: these endpoints explicitly label local estimates.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .compat import CompatibilityError
from .models import ChatRequest


def estimate_tokens(value):
    """Local UTF-8 byte heuristic, never AWS metering or a tokenizer claim."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, (len(text.encode('utf-8')) + 3) // 4)


def require(condition, message):
    if not condition:
        raise CompatibilityError(message)


class Tools:
    def __init__(self, definitions, protocol):
        require(isinstance(definitions, list) and len(definitions) <= 64, 'tools 必须是最多 64 项的数组')
        self.chat, self.entries = [], {}
        for tool in definitions:
            require(isinstance(tool, dict), '工具定义无效')
            if protocol == 'responses' and tool.get('type') == 'namespace':
                namespace = tool.get('name')
                require(isinstance(namespace, str) and namespace, '工具 namespace 无效')
                require(isinstance(tool.get('tools'), list), 'namespace.tools 必须是数组')
                for child in tool['tools']:
                    self.add(child, protocol, namespace)
            else:
                self.add(tool, protocol)
        require(len(self.chat) <= 64, '展开后工具数不得超过 64')

    def add(self, tool, protocol, namespace=None):
        require(isinstance(tool, dict), '工具定义无效')
        kind = tool.get('type', 'function' if protocol == 'responses' else 'custom')
        custom = protocol == 'responses' and kind == 'custom'
        if protocol == 'messages':
            require(kind == 'custom', '仅支持客户端执行的自定义工具，不支持 Anthropic 服务端工具')
        else:
            require(kind in ('function', 'custom'), '仅支持 function/custom 客户端工具')
        name = tool.get('name')
        require(isinstance(name, str) and 0 < len(name) <= 256, '工具名无效')
        alias = name if not namespace and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) else (
            'tool_' + hashlib.sha256(((namespace or '') + '\0' + name).encode()).hexdigest()[:32])
        require(alias not in self.entries, '工具名重复')
        schema = tool.get('parameters' if protocol == 'responses' else 'input_schema', {'type': 'object'})
        description = tool.get('description', '')
        require(isinstance(description, str), '工具 description 无效')
        if custom:
            schema = {'type': 'object', 'properties': {'input': {'type': 'string'}},
                      'required': ['input'], 'additionalProperties': False}
            description += '\nReturn the exact freeform tool input in arguments.input.'
            if tool.get('format'):
                description += '\nRequired input format: ' + json.dumps(tool['format'], ensure_ascii=False)
        self.entries[alias] = {'name': name, 'namespace': namespace, 'custom': custom}
        self.chat.append({'type': 'function', 'function': {'name': alias, 'description': description,
                                                         'parameters': schema}})

    def alias(self, name, namespace=None):
        for alias, entry in self.entries.items():
            if entry['name'] == name and entry['namespace'] == namespace:
                return alias
        # History may contain a previously available tool which is no longer declared.
        require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name), '历史工具名无效')
        require(namespace is None, '历史 namespace 工具需在 tools 中声明')
        return name

    def choice(self, value, protocol):
        if value is None:
            return None
        if protocol == 'messages':
            require(isinstance(value, dict), 'tool_choice 必须是对象')
            kind = value.get('type')
            if kind in ('auto', 'none', 'any'):
                return {'any': 'required'}.get(kind, kind)
            require(kind == 'tool', 'tool_choice.type 无效')
        else:
            if isinstance(value, str):
                return value
            require(isinstance(value, dict) and value.get('type') in ('function', 'custom'),
                    'tool_choice 仅支持 auto/none/required 或命名工具')
        return {'type': 'function', 'function': {'name': self.alias(value.get('name'), value.get('namespace'))}}


def text_parts(content, protocol):
    if isinstance(content, str):
        return content
    require(isinstance(content, list), '消息 content 必须是字符串或内容数组')
    result = []
    for part in content:
        require(isinstance(part, dict), '内容分段无效')
        kind = part.get('type')
        if kind in ('text', 'input_text', 'output_text'):
            require(isinstance(part.get('text'), str), '文本分段无效')
            result.append({'type': 'text', 'text': part['text']})
        elif protocol == 'responses' and kind == 'input_image':
            result.append({'type': 'image_url', 'image_url': {'url': part.get('image_url'),
                                                             'detail': part.get('detail', 'auto')}})
        elif protocol == 'messages' and kind == 'image':
            source = part.get('source', {})
            require(isinstance(source, dict) and source.get('type') == 'base64', '图片仅支持 PNG/JPEG base64')
            require(isinstance(source.get('data'), str), '图片数据无效')
            result.append({'type': 'image_url', 'image_url': {
                'url': f"data:{source.get('media_type')};base64,{source['data']}"}})
        elif protocol == 'messages' and kind in ('thinking', 'redacted_thinking'):
            # Replay metadata only; the gateway never invents or exposes private reasoning.
            continue
        else:
            raise CompatibilityError('不支持此内容分段；仅支持文本、base64 图片和客户端工具结果')
    return result


def responses_input(body, tools):
    messages = []
    if body.get('instructions'):
        require(isinstance(body['instructions'], str), 'instructions 必须是字符串')
        messages.append({'role': 'system', 'content': body['instructions']})
    value = body.get('input')
    if isinstance(value, str):
        value = [{'role': 'user', 'content': value}]
    require(isinstance(value, list), 'input 必须是字符串或数组')
    for item in value:
        require(isinstance(item, dict), 'input 项必须是对象')
        kind = item.get('type', 'message')
        if kind == 'message':
            role = item.get('role')
            require(role in ('system', 'developer', 'user', 'assistant'), '消息 role 无效')
            messages.append({'role': role, 'content': text_parts(item.get('content'), 'responses')})
        elif kind in ('function_call', 'custom_tool_call'):
            arguments = item.get('arguments') if kind == 'function_call' else json.dumps({'input': item.get('input')})
            require(isinstance(arguments, str) and isinstance(item.get('call_id'), str), '工具历史缺少参数或 call_id')
            messages.append({'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': item['call_id'], 'type': 'function', 'function': {
                    'name': tools.alias(item.get('name'), item.get('namespace')), 'arguments': arguments}}]})
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            require(isinstance(item.get('call_id'), str) and item['call_id'], '工具结果缺少 call_id')
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'],
                             'content': text_parts(item.get('output'), 'responses')})
        elif kind == 'reasoning':
            continue
        else:
            raise CompatibilityError('不支持此 input 项；请发送完整消息及工具历史，不使用 item_reference')
    return messages


def anthropic_input(body):
    messages = []
    if body.get('system'):
        messages.append({'role': 'system', 'content': text_parts(body['system'], 'messages')})
    require(isinstance(body.get('messages'), list), 'messages 必须是数组')
    system_count = len(messages)
    for item in body['messages']:
        require(isinstance(item, dict) and item.get('role') in ('user', 'assistant', 'system'), '消息 role 无效')
        content = item.get('content')
        if item['role'] == 'system':
            # Claude Code also sends environment metadata as a trailing system message.
            messages.insert(system_count, {'role': 'system', 'content': text_parts(content, 'messages')})
            system_count += 1
            continue
        if isinstance(content, str):
            messages.append({'role': item['role'], 'content': content})
            continue
        require(isinstance(content, list), '消息 content 无效')
        pending, calls = [], []

        def flush(pending=pending, calls=calls, role=item['role']):
            if pending or calls:
                message = {'role': role, 'content': text_parts(pending, 'messages') if pending else None}
                if calls:
                    message['tool_calls'] = calls.copy()
                if message['content'] is not None or calls:
                    messages.append(message)
                pending.clear()
                calls.clear()

        for part in content:
            require(isinstance(part, dict), '内容分段无效')
            if part.get('type') == 'tool_result':
                require(item['role'] == 'user' and isinstance(part.get('tool_use_id'), str)
                        and part['tool_use_id'], 'tool_result 需位于 user 消息且含 tool_use_id')
                flush()
                result = text_parts(part.get('content', ''), 'messages')
                if part.get('is_error'):
                    result = [{'type': 'text', 'text': '[Client tool execution failed]'}] + (
                        [{'type': 'text', 'text': result}] if isinstance(result, str) else result)
                messages.append({'role': 'tool', 'tool_call_id': part['tool_use_id'], 'content': result})
            elif part.get('type') == 'tool_use':
                require(item['role'] == 'assistant' and isinstance(part.get('input'), dict)
                        and isinstance(part.get('id'), str), 'tool_use 分段无效')
                calls.append({'id': part['id'], 'type': 'function', 'function': {
                    'name': part.get('name'), 'arguments': json.dumps(part['input'], ensure_ascii=False)}})
            else:
                pending.append(part)
        flush()
    return messages


def prepare(body, protocol):
    require(isinstance(body, dict), '请求体必须是 JSON 对象')
    tools = Tools(body.get('tools', []), protocol)
    extra = {k: body[k] for k in ('model', 'stream', 'parallel_tool_calls') if k in body}
    require(isinstance(body.get('stream', False), bool), 'stream 必须是布尔值')
    if protocol == 'responses':
        require(not body.get('previous_response_id'), '无服务端会话存储；请在 input 中发送完整历史')
        require(not body.get('conversation'), '无服务端 conversation 存储；请发送完整历史')
        require(not body.get('background') and not body.get('store'), '仅支持 background=false、store=false')
        messages = responses_input(body, tools)
        reasoning = body.get('reasoning') or {}
        require(isinstance(reasoning, dict), 'reasoning 必须是对象')
        if reasoning.get('effort') is not None:
            extra['reasoning_effort'] = reasoning['effort']
        text = body.get('text') or {}
        require(isinstance(text, dict), 'text 必须是对象')
        if text.get('format'):
            form = text['format']
            require(isinstance(form, dict), 'text.format 无效')
            extra['response_format'] = ({'type': 'json_schema', 'json_schema': form}
                                        if form.get('type') == 'json_schema' else form)
    else:
        require(isinstance(body.get('max_tokens'), int) and not isinstance(body['max_tokens'], bool)
                and body['max_tokens'] > 0, 'max_tokens 必须是正整数')
        messages = anthropic_input(body)
        for message in messages:
            for call in message.get('tool_calls', []):
                call['function']['name'] = tools.alias(call['function']['name'])
        thinking, config = body.get('thinking') or {}, body.get('output_config') or {}
        require(isinstance(thinking, dict) and isinstance(config, dict), 'thinking/output_config 必须是对象')
        if thinking.get('type') == 'disabled':
            extra['thinking_effort'] = 'off'
        elif config.get('effort'):
            extra['thinking_effort'] = {'max': 'high', 'xhigh': 'high', 'none': 'off', 'minimal': 'low'}.get(
                config['effort'], config['effort'])
        elif thinking.get('type') == 'enabled':
            budget = thinking.get('budget_tokens', 1024)
            require(isinstance(budget, int) and not isinstance(budget, bool) and budget > 0, 'thinking.budget_tokens 无效')
            extra['thinking_effort'] = 'high' if budget >= 16384 else 'medium' if budget >= 8192 else 'low'
        elif thinking.get('type') == 'adaptive':
            extra['thinking_effort'] = 'medium'
        if config.get('format'):
            extra['response_format'] = {'type': 'json_schema', 'json_schema': config['format']}
        choice = body.get('tool_choice')
        if isinstance(choice, dict) and choice.get('disable_parallel_tool_use'):
            extra['parallel_tool_calls'] = False
    extra.update(messages=messages, tools=tools.chat or None,
                 tool_choice=tools.choice(body.get('tool_choice'), protocol))
    return ChatRequest.model_validate(extra), tools


def response_object(model, output, identity=None, status='completed'):
    return {'id': identity or 'resp_' + uuid.uuid4().hex, 'object': 'response', 'created_at': int(time.time()),
            'status': status, 'error': None, 'incomplete_details': None, 'model': model, 'output': output,
            'parallel_tool_calls': True, 'tool_choice': 'auto', 'tools': [], 'usage': None,
            'previous_response_id': None, 'store': False, 'metadata': {}}


def response_items(message, tools):
    output = []
    if message.get('content') is not None:
        output.append({'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
                       'status': 'completed', 'content': [{'type': 'output_text', 'text': message['content'],
                                                         'annotations': [], 'logprobs': []}]})
    for call in message.get('tool_calls', []):
        function = call['function']
        entry = tools.entries[function['name']]
        item = {'id': 'fc_' + uuid.uuid4().hex, 'type': 'custom_tool_call' if entry['custom'] else 'function_call',
                'call_id': call['id'], 'name': entry['name'], 'status': 'completed'}
        if entry['namespace']:
            item['namespace'] = entry['namespace']
        if entry['custom']:
            item['input'] = json.loads(function['arguments'])['input']
        else:
            item['arguments'] = function['arguments']
        output.append(item)
    return output


def message_object(model, message, tools, input_tokens, identity=None):
    content = []
    if message.get('content'):
        content.append({'type': 'text', 'text': message['content']})
    for call in message.get('tool_calls', []):
        content.append({'type': 'tool_use', 'id': call['id'],
                        'name': tools.entries[call['function']['name']]['name'],
                        'input': json.loads(call['function']['arguments'])})
    return {'id': identity or 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
            'content': content, 'model': model, 'stop_reason': 'tool_use' if message.get('tool_calls') else 'end_turn',
            'stop_sequence': None, 'usage': {'input_tokens': input_tokens, 'output_tokens': estimate_tokens(content)}}


def api_error(protocol, message, status=400, headers=None):
    kind = {401: 'authentication_error', 403: 'permission_error', 404: 'not_found_error',
            429: 'rate_limit_error', 503: 'overloaded_error'}.get(status,
                    'api_error' if status >= 500 else 'invalid_request_error')
    error = {'error': {'type': kind, 'message': message}}
    if protocol == 'messages':
        error['type'] = 'error'
    return JSONResponse(error, status, headers=headers)


async def chat_chunks(response):
    """Parse shared engine frames, closing its owning generator on disconnect."""
    iterator, buffer = response.body_iterator, ''
    try:
        async for frame in iterator:
            buffer += frame.decode() if isinstance(frame, bytes) else frame
            while '\n\n' in buffer:
                block, buffer = buffer.split('\n\n', 1)
                data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
                if data and data != '[DONE]':
                    yield json.loads(data)
    finally:
        await iterator.aclose()


def sse(name, value):
    return 'event: ' + name + '\ndata: ' + json.dumps(value, ensure_ascii=False) + '\n\n'


async def response_stream(result, request, tools):
    identity = 'resp_' + uuid.uuid4().hex
    sequence, output, text_index = 0, [], None

    def event(name, **fields):
        nonlocal sequence
        value = {'type': name, 'sequence_number': sequence, **fields}
        sequence += 1
        return sse(name, value)

    chunks = chat_chunks(result)
    try:
        # Prime the shared stream before the first network yield, retaining lease ownership.
        first = await anext(chunks, None)
        yield event('response.created', response=response_object(request.model, [], identity, 'in_progress'))
        yield event('response.in_progress', response=response_object(request.model, [], identity, 'in_progress'))

        async def all_chunks():
            if first is not None:
                yield first
            async for chunk in chunks:
                yield chunk

        async for chunk in all_chunks():
            if 'error' in chunk:
                yield event('response.failed', response={**response_object(request.model, output, identity, 'failed'),
                                                         'error': chunk['error']})
                return
            for choice in chunk.get('choices', []):
                delta = choice.get('delta', {})
                if delta.get('content'):
                    if text_index is None:
                        text_index = len(output)
                        item = {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
                                'status': 'in_progress', 'content': []}
                        output.append(item)
                        yield event('response.output_item.added', output_index=text_index, item=dict(item))
                        part = {'type': 'output_text', 'text': '', 'annotations': [], 'logprobs': []}
                        item['content'].append(part)
                        yield event('response.content_part.added', item_id=item['id'], output_index=text_index,
                                    content_index=0, part=dict(part))
                    item = output[text_index]
                    item['content'][0]['text'] += delta['content']
                    yield event('response.output_text.delta', item_id=item['id'], output_index=text_index,
                                content_index=0, delta=delta['content'], logprobs=[])
                for call in delta.get('tool_calls', []):
                    # The shared tool adapter validates/buffers complete JSON before emitting a call.
                    item = response_items({'tool_calls': [call]}, tools)[0]
                    index = len(output)
                    field = 'input' if item['type'] == 'custom_tool_call' else 'arguments'
                    text = item[field]
                    output.append(item)
                    yield event('response.output_item.added', output_index=index,
                                item={**item, field: '', 'status': 'in_progress'})
                    prefix = 'response.custom_tool_call_input' if field == 'input' else 'response.function_call_arguments'
                    yield event(prefix + '.delta', item_id=item['id'], output_index=index, delta=text)
                    yield event(prefix + '.done', item_id=item['id'], output_index=index, **{field: text})
                    yield event('response.output_item.done', output_index=index, item=item)
        if text_index is not None:
            item = output[text_index]
            part = item['content'][0]
            item['status'] = 'completed'
            yield event('response.output_text.done', item_id=item['id'], output_index=text_index,
                        content_index=0, text=part['text'], logprobs=[])
            yield event('response.content_part.done', item_id=item['id'], output_index=text_index,
                        content_index=0, part=part)
            yield event('response.output_item.done', output_index=text_index, item=item)
        yield event('response.completed', response=response_object(request.model, output, identity))
    finally:
        await chunks.aclose()


async def message_stream(result, request, tools, input_tokens):
    identity = 'msg_' + uuid.uuid4().hex
    chunks, content, index, text_open = chat_chunks(result), [], 0, False
    tool_used = False
    try:
        first = await anext(chunks, None)
        message = message_object(request.model, {}, tools, input_tokens, identity)
        message.update(stop_reason=None, usage={'input_tokens': input_tokens, 'output_tokens': 0})
        yield sse('message_start', {'type': 'message_start', 'message': message})

        async def all_chunks():
            if first is not None:
                yield first
            async for chunk in chunks:
                yield chunk

        async for chunk in all_chunks():
            if 'error' in chunk:
                yield sse('error', {'type': 'error', 'error': {'type': 'api_error',
                                                             'message': chunk['error']['message']}})
                return
            for choice in chunk.get('choices', []):
                delta = choice.get('delta', {})
                if delta.get('content'):
                    if not text_open:
                        text_open = True
                        yield sse('content_block_start', {'type': 'content_block_start', 'index': index,
                                                         'content_block': {'type': 'text', 'text': ''}})
                    content.append(delta['content'])
                    yield sse('content_block_delta', {'type': 'content_block_delta', 'index': index,
                                                     'delta': {'type': 'text_delta', 'text': delta['content']}})
                for call in delta.get('tool_calls', []):
                    if text_open:
                        yield sse('content_block_stop', {'type': 'content_block_stop', 'index': index})
                        index += 1
                        text_open = False
                    tool_used = True
                    function = call['function']
                    content.append(function['arguments'])
                    yield sse('content_block_start', {'type': 'content_block_start', 'index': index,
                        'content_block': {'type': 'tool_use', 'id': call['id'],
                                          'name': tools.entries[function['name']]['name'], 'input': {}}})
                    yield sse('content_block_delta', {'type': 'content_block_delta', 'index': index,
                        'delta': {'type': 'input_json_delta', 'partial_json': function['arguments']}})
                    yield sse('content_block_stop', {'type': 'content_block_stop', 'index': index})
                    index += 1
        if text_open:
            yield sse('content_block_stop', {'type': 'content_block_stop', 'index': index})
        yield sse('message_delta', {'type': 'message_delta',
            'delta': {'stop_reason': 'tool_use' if tool_used else 'end_turn', 'stop_sequence': None},
            'usage': {'output_tokens': estimate_tokens(''.join(content))}})
        yield sse('message_stop', {'type': 'message_stop'})
    finally:
        await chunks.aclose()


def register_cli_routes(app, complete, api_access):
    async def handle(request, protocol, count=False):
        try:
            await api_access(request)
            body = await request.json()
            if count and isinstance(body, dict):
                body = {**body, 'max_tokens': body.get('max_tokens', 1), 'stream': False}
            chat, tools = prepare(body, protocol)
        except HTTPException as error:
            return api_error(protocol, error.detail, error.status_code)
        except CompatibilityError as error:
            return api_error(protocol, str(error))
        except (ValidationError, ValueError, TypeError, KeyError, RecursionError):
            # Do not echo raw CLI prompts, tool arguments, tokens or Pydantic input values.
            return api_error(protocol, '请求格式或模型不支持；请检查消息、工具类型和模型名（/v1/models）')
        headers = {'X-QuickPool-Usage': 'estimated' if protocol == 'messages' else 'unavailable',
                   'X-QuickPool-State': 'stateless', 'X-Accel-Buffering': 'no'}
        input_tokens = estimate_tokens(body)
        if count:
            return JSONResponse({'input_tokens': input_tokens}, headers=headers)
        result = await complete(chat)
        if isinstance(result, JSONResponse):
            value = json.loads(result.body)
            return api_error(protocol, value.get('error', {}).get('message', '上游请求失败'),
                             result.status_code, {k: v for k, v in result.headers.items()
                                                  if k.lower() in ('retry-after', 'x-request-id')})
        if isinstance(result, StreamingResponse):
            stream = (response_stream(result, chat, tools) if protocol == 'responses' else
                      message_stream(result, chat, tools, input_tokens))
            return StreamingResponse(stream, media_type='text/event-stream', headers={
                **headers, **{k: v for k, v in result.headers.items() if k.startswith('x-')}})
        message = result['choices'][0]['message']
        value = (response_object(chat.model, response_items(message, tools)) if protocol == 'responses' else
                 message_object(chat.model, message, tools, input_tokens))
        return JSONResponse(value, headers=headers)

    @app.post('/v1/responses')
    @app.post('/responses')
    async def responses(request: Request):
        return await handle(request, 'responses')

    @app.post('/v1/messages')
    @app.post('/messages')
    async def messages(request: Request):
        return await handle(request, 'messages')

    @app.post('/v1/messages/count_tokens')
    @app.post('/messages/count_tokens')
    async def count_tokens(request: Request):
        return await handle(request, 'messages', count=True)
