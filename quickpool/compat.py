"""OpenAI message and tool adapter. Client tools are never executed by QuickPool.

Tool/JSON responses use a documented prompt adapter, not native tool registration.
Only validated model output becomes an OpenAI tool call.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import uuid


_BILLING_HEADER_RE = re.compile(r'^x-anthropic-billing-header[^\n]*$', re.IGNORECASE | re.MULTILINE)
_SYSTEM_REMINDER_RE = re.compile(r'<system-reminder>([\s\S]*?)</system-reminder>', re.IGNORECASE)
_TOOL_CALL_TAG_RE = re.compile(r'<tool_call>\s*([\s\S]*?)\s*</tool_call>', re.IGNORECASE)
_LOOSE_CONTENT_RE = re.compile(r'^\s*\{\s*"content"\s*:\s*"([\s\S]*)"\s*\}\s*$')
_JSON_ESC_RE = re.compile(r'\\(["\\/bfnrt]|u[0-9a-fA-F]{4})')
_JSON_ESC_MAP = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}


def _extract_loose_content_json(text: str) -> str | None:
    """Recover {"content": "..."} when the model forgot to escape ASCII quotes inside the string."""
    match = _LOOSE_CONTENT_RE.match(text)
    if not match:
        return None
    raw = match.group(1)

    def _unescape(m):
        tok = m.group(1)
        if tok.startswith('u'):
            return chr(int(tok[1:], 16))
        return _JSON_ESC_MAP.get(tok, m.group(0))

    return _JSON_ESC_RE.sub(_unescape, raw)
_REFUSAL_RE = re.compile(
    r"(?:not going to adopt|won't adopt|cannot adopt|refuse to adopt|output raw [`'\"]?tool_calls[`'\"]?|"
    r"prompt[- ]injection|提示注入|不是我的身份|不是我实际可用的工具|AWS Quick\s*桌面应用|Coding agents|<decision\b)",
    re.IGNORECASE,
)
_IDENTITY_PATTERNS = (
    (re.compile(
        r'\bYou are (?:Claude Code|Codex|Cline|Roo Code|Roo|Kilo Code|Kilo|Cursor|OpenCode|'
        r'OpenChamber|Hermes|Antigravity|ZCode|Windsurf|Aider|Augment|Trae|DeepSeek Harness|'
        r'Witan|Devin|Goose|Continue)\b(?:[^\n.]|\.(?=\d|[a-z0-9-]+\.[a-z]{2,}))*\.?',
        re.IGNORECASE,
    ), 'You are assisting a developer through an external coding client.'),
    (re.compile(
        r"\bYou are Claude,\s+Anthropic's(?:[^\n.]|\.(?=\d|[a-z0-9-]+\.[a-z]{2,}))*\.?",
        re.IGNORECASE,
    ), 'You are assisting a developer through an external coding client.'),
    (re.compile(
        r'\bYou are an interactive CLI tool\b',
        re.IGNORECASE,
    ), 'You are assisting a developer using an interactive CLI client'),
    (re.compile(
        r'\bYou are a coding agent running in(?:[^\n.]|\.(?=\d|[a-z0-9-]+\.[a-z]{2,}))*\.?',
        re.IGNORECASE,
    ), 'You are assisting a developer working in a local coding environment.'),
    (re.compile(
        r'(^|\n)You are (?:running|operating) inside [^\n.]*?\b(?:environment|harness|orchestration|agent|CLI|IDE)\b'
        r'(?:[^\n.]|\.(?=\d|[a-z0-9-]+\.[a-z]{2,}))*\.?',
        re.IGNORECASE,
    ), r'\1You are assisting a developer working in a local coding environment.'),
    (re.compile(
        r'(^|\n)你是[^\n。]{1,28}(?:研发|开发|打造|推出)的[^\n。]{0,28}(?:代理|助手|智能体|工具)[^\n。]*。?',
    ), r'\1你正在协助开发者在本地代码仓库中完成软件工程任务。'),
    (re.compile(r'\bClaude Code\b', re.IGNORECASE), 'the coding client'),
    (re.compile(r"\bAnthropic's official CLI\b", re.IGNORECASE), 'the local coding CLI'),
)


def sanitize_outbound_text(text: str, *, soften_identity: bool = False) -> str:
    """Strip high-risk CLI billing headers, neutralize <system-reminder>, and optionally soften conflicting CLI identities."""
    if not text:
        return text
    if 'x-anthropic-billing-header' in text.lower():
        text = re.sub(r'\n{3,}', '\n\n', _BILLING_HEADER_RE.sub('', text)).strip()
    if '<system-reminder>' in text.lower():
        text = _SYSTEM_REMINDER_RE.sub(lambda m: f'[Context note: {m.group(1).strip()}]', text)
    if soften_identity:
        for pattern, repl in _IDENTITY_PATTERNS:
            text = pattern.sub(repl, text)
    return text

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable


class CompatibilityError(ValueError):
    pass


def validate_schema(schema):
    try:
        if not isinstance(schema, dict):
            raise TypeError
        Draft202012Validator.check_schema(schema)
        def local_refs(node):
            if isinstance(node, dict):
                if '$ref' in node and not str(node['$ref']).startswith('#'):
                    raise ValueError
                for item in node.values():
                    local_refs(item)
            elif isinstance(node, list):
                for item in node:
                    local_refs(item)
        local_refs(schema)
    except (SchemaError, ValueError, TypeError, RecursionError):
        raise CompatibilityError('JSON Schema 无效或含不受支持的外部引用') from None


def check_instance(instance, schema):
    try:
        Draft202012Validator(schema, registry=Registry()).validate(instance)
    except (ValidationError, SchemaError, Unresolvable, ValueError, TypeError, RecursionError):
        raise CompatibilityError('Quick 回复不符合请求的 JSON Schema') from None


def image_data(url, *, max_image_mb: int = 5):
    # No remote fetch: prevents SSRF and leaking caller credentials to redirects.
    try:
        header, data = url.split(',', 1)
        mime = {'data:image/png;base64':'image/png', 'data:image/jpeg;base64':'image/jpeg'}[header.lower()]
        raw = base64.b64decode(data, validate=True)
        if not raw or len(raw) > max_image_mb * 1024 * 1024:
            raise ValueError
        if mime == 'image/png' and not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError
        if mime == 'image/jpeg' and not raw.startswith(b'\xff\xd8\xff'):
            raise ValueError
        return raw, mime
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise CompatibilityError(f'图片须为 PNG/JPEG base64 data URL，单图不超过 {max_image_mb} MiB；暂不抓取远程 URL') from None


class Prepared:
    def __init__(self, request, *, max_images: int = 50, max_image_mb: int = 5,
                 max_messages: int = 2000, max_tools: int = 256, max_context_mb: int = 20):
        self.request = request
        self.max_tools = max_tools
        if len(request.messages) > max_messages:
            raise CompatibilityError(f'每次请求最多 {max_messages} 条消息')
        self.images = []
        self.tools = request.tools or ([{'type':'function', 'function':f} for f in request.functions]
                                       if request.functions else [])
        if len(self.tools) > max_tools:
            raise CompatibilityError(f'每次请求最多 {max_tools} 个工具')
        self.functions = {t['function']['name']: t['function'] for t in self.tools}
        if len(self.functions) != len(self.tools):
            raise CompatibilityError('函数名不能重复')
        self.choice = request.tool_choice if request.tool_choice is not None else request.function_call
        self.choice = self.choice if self.choice is not None else 'auto'
        if isinstance(self.choice, dict):
            selected = self.choice.get('function', self.choice)
            self.selected = selected.get('name') if isinstance(selected,dict) else None
            if self.selected not in self.functions:
                raise CompatibilityError('tool_choice 指定了未声明的函数')
        else:
            self.selected = None
            if self.choice not in ('auto', 'none', 'required'):
                raise CompatibilityError('tool_choice 无效')
        if (self.selected or self.choice == 'required') and not self.tools:
            raise CompatibilityError('tool_choice 需要提供 tools/functions')
        for function in self.functions.values():
            validate_schema(function.get('parameters', {'type':'object'}))
        self.format = request.response_format or {'type':'text'}
        if self.format['type'] == 'json_schema':
            validate_schema(self.format.get('json_schema', {}).get('schema'))
        self.buffered = bool(self.tools) or self.format['type'] != 'text'
        messages = []
        for message in request.messages:
            item = message.model_dump(exclude_none=True)
            soften = bool(self.tools) and item.get('role') in ('system', 'developer')
            parts = item.get('content')
            if isinstance(parts, str):
                item['content'] = sanitize_outbound_text(parts, soften_identity=soften)
            elif isinstance(parts, list):
                texts = []
                for part in parts:
                    if part['type'] == 'text':
                        texts.append(sanitize_outbound_text(part['text'], soften_identity=soften))
                    else:
                        self.images.append(image_data(part['image_url']['url'], max_image_mb=max_image_mb))
                        texts.append(f'[图片附件 {len(self.images)}]')
                item['content'] = '\n'.join(texts)
            messages.append(item)
        if len(self.images) > max_images:
            raise CompatibilityError(f'每次请求最多 {max_images} 张图片')
        if len(messages) == 1 and set(messages[0]) == {'role','content'}:
            self.prompt = messages[0]['content']
        else:
            self.prompt = ('Conversation history (role and tool call IDs are significant):\n'
                           + json.dumps(messages, ensure_ascii=False))
        instructions = ''
        if self.tools:
            instructions += ('CLIENT FUNCTION ADAPTER: Return only one JSON object. '
                'To request client-side execution return {"tool_calls":[{"name":"declared_name","arguments":{}}]}. '
                'Otherwise return {"content":"your answer"}. Never claim a function was executed. '
                'Use only the declared functions, conform to their JSON schemas. '
                'tool_choice=' + json.dumps(self.choice) + '; parallel_tool_calls=' + str(request.parallel_tool_calls)
                + '. choice=none forbids tool_calls; required requires them; a named choice requires that function. '
                + 'Declared functions are in tools below.')
        if self.format['type'] != 'text':
            instructions += '\nReturn a JSON object only, without markdown fences.'
            if self.format['type'] == 'json_schema':
                instructions += ' Conform to JSON Schema: ' + json.dumps(self.format['json_schema']['schema'])
        self.context = {'messages': messages, 'tools': self.tools, 'tool_choice': self.choice,
                        'parallel_tool_calls': request.parallel_tool_calls, 'response_format': self.format,
                        'adapter_instructions': instructions}
        if instructions:
            self.prompt += '\n\n' + instructions
        if self.tools:
            self.prompt += '\nDeclared functions: ' + json.dumps(self.tools, ensure_ascii=False)
        if len(json.dumps(self.context, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > max_context_mb*1024*1024:
            raise CompatibilityError(f'转换后的上下文附件超过 {max_context_mb} MiB')

    def result(self, text):
        message, finish = {'role':'assistant','content':text}, 'stop'
        if not self.buffered:
            return message, finish
        commentary = None
        try:
            stripped = text.strip()
            if stripped.startswith('```') and stripped.endswith('```'):
                stripped = stripped.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            try:
                value = json.loads(stripped)
            except ValueError:
                # Also accept <tool_call>{...}</tool_call> blocks (emitted by Coding Assistant custom agents).
                if self.tools and self.format['type'] == 'text' and '<tool_call>' in stripped.lower():
                    tag_calls = []
                    for match in _TOOL_CALL_TAG_RE.finditer(stripped):
                        parsed_call = json.loads(match.group(1))
                        if isinstance(parsed_call, dict):
                            tag_calls.append(parsed_call)
                    if tag_calls:
                        value = {'tool_calls': tag_calls}
                        commentary = _TOOL_CALL_TAG_RE.sub('', stripped).strip() or None
                    else:
                        value = None
                else:
                    value = None
            if value is None:
                # Quick can narrate native attachment reads (even with braces or fenced JSON) before the final JSON.
                if not self.tools or self.format['type'] != 'text':
                    raise ValueError
                candidate_text = stripped
                if candidate_text.endswith('```'):
                    fence = candidate_text.rfind('```json')
                    if fence == -1:
                        fence = candidate_text.rfind('```', 0, len(candidate_text) - 3)
                    if fence > 0:
                        inner = candidate_text[fence:].split('\n', 1)[-1].rsplit('```', 1)[0].strip()
                        candidate_text = candidate_text[:fence] + inner
                start = -1
                search_from = 0
                while True:
                    pos = candidate_text.find('{', search_from)
                    if pos < 1:
                        break
                    try:
                        parsed = json.loads(candidate_text[pos:])
                    except ValueError:
                        search_from = pos + 1
                        continue
                    if isinstance(parsed, dict) and (parsed.get('tool_calls') or isinstance(parsed.get('content'), str)):
                        value, start = parsed, pos
                        break
                    search_from = pos + 1
                if value is None:
                    if self.tools and self.choice in ('auto', 'none') and self.format['type'] == 'text':
                        loose = _extract_loose_content_json(stripped)
                        if loose is None:
                            # Also check trailing {"content":"..."} after commentary or fenced block.
                            tail_pos = candidate_text.rfind('{"content"')
                            if tail_pos >= 0:
                                loose = _extract_loose_content_json(candidate_text[tail_pos:])
                        if loose is not None:
                            value = {'content': loose}
                if value is None:
                    if (self.tools and self.choice in ('auto', 'none') and self.format['type'] == 'text'
                            and stripped and not _REFUSAL_RE.search(stripped)
                            and '"tool_calls"' not in stripped and not _TOOL_CALL_TAG_RE.search(stripped)):
                        return {'role': 'assistant', 'content': text}, 'stop'
                    raise ValueError
                if value.get('tool_calls'):
                    commentary = candidate_text[:start].strip() or None
            if not isinstance(value, dict):
                raise TypeError
        except (ValueError, TypeError, IndexError):
            logging.getLogger(__name__).warning(
                'compat_json_parse_failed len=%d choice=%s format=%s head=%r',
                len(text), self.choice, self.format.get('type'), text.strip()[:180],
            )
            raise CompatibilityError('Quick 未返回有效 JSON；工具/结构化适配失败') from None
        if self.tools:
            calls = value.get('tool_calls')
            if calls:
                if self.choice == 'none' or not isinstance(calls,list) or len(calls) > self.max_tools:
                    raise CompatibilityError('Quick 回复违反 tool_choice')
                if not self.request.parallel_tool_calls and len(calls) > 1:
                    raise CompatibilityError('Quick 回复包含不允许的并行调用')
                converted = []
                for call in calls:
                    if not isinstance(call,dict):
                        raise CompatibilityError('Quick 工具调用结构无效')
                    fn = call.get('function') if isinstance(call.get('function'), dict) else None
                    name = call.get('name') if 'name' in call else (fn.get('name') if fn else None)
                    arguments = call.get('arguments') if 'arguments' in call else (fn.get('arguments') if fn else None)
                    if not isinstance(name,str) or name not in self.functions or (self.selected and name != self.selected):
                        raise CompatibilityError('Quick 请求了未声明或未选中的函数')
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            raise CompatibilityError('Quick 工具参数必须为 JSON 对象') from None
                    elif arguments is None:
                        arguments = {}
                    if not isinstance(arguments,dict):
                        raise CompatibilityError('Quick 工具参数必须为 JSON 对象')
                    check_instance(arguments, self.functions[name].get('parameters', {'type':'object'}))
                    converted.append({'id':'call_'+uuid.uuid4().hex, 'type':'function',
                        'function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}})
                message = {'role':'assistant','content':commentary, 'tool_calls':converted}
                finish = 'tool_calls'
                if self.request.functions:
                    if len(converted) != 1:
                        raise CompatibilityError('旧版 functions 接口只允许一个调用')
                    message = {'role':'assistant','content':commentary,'function_call':converted[0]['function']}
                    finish = 'function_call'
                return message, finish
            if self.choice == 'required' or self.selected:
                raise CompatibilityError('Quick 未返回必需的工具调用')
            if not isinstance(value.get('content'),str):
                raise CompatibilityError('Quick 适配回复缺少 content')
            message['content'] = value['content']
            if self.format['type'] != 'text':
                try:
                    value = json.loads(message['content'])
                except ValueError:
                    raise CompatibilityError('Quick content 不是有效 JSON') from None
        if self.format['type'] == 'json_schema':
            check_instance(value, self.format['json_schema']['schema'])
        return message, finish
