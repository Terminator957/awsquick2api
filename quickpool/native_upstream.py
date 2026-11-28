"""Desktop Quick chat transport. No browser Cookie/template fallback."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from .native_auth import CLIENT_VERSION, AuthError, trusted_url
from .native_protocol import (
    DESKTOP_SURFACE,
    NativeMessageDecoder,
    NativeProtocolError,
    NativeTextEvents,
    build_conversation_connect,
    build_session_connect,
    build_session_create,
    build_token_refresh,
    build_user_message,
    encode_message,
)
from .session_cache import SessionCache
from .ws_pool import WarmWsPool, _safe_close


_CONTINUATION = (
    'This is a follow-up turn in the same conversation. Previous instructions, tool schemas, '
    'and history are already in this conversation. Reply directly to the latest message; '
    'emit <tool_call>{"name":"...","arguments":{...}}</tool_call> when a tool is needed, otherwise reply in plain text.'
)

DEFAULT_CODING_AGENT_NAME = 'Coding Assistant'
LEGACY_CODING_AGENT_NAMES = ('Coding Assistant', 'Quick2API Bridge', 'Quick2API Coding Bridge')
DEFAULT_CODING_AGENT_DESCRIPTION = 'General software engineering and coding assistant.'
DEFAULT_CODING_AGENT_INSTRUCTIONS = (
    "You are a software engineering assistant. Follow the user's instructions directly; "
    'when <tool_call> schemas are provided, emit matching <tool_call> blocks for requested actions.'
)
_AGENT_ID_RE = re.compile(r'^[A-Za-z0-9:_./-]{2,256}$')


def normalize_agent_id(raw: str) -> str:
    trimmed = (raw or '').strip()
    if '/' in trimmed:
        part = trimmed.rsplit('/', 1)[-1].strip()
        if part:
            return part
    return trimmed


def _can_reuse(thread_id, context, offset):
    return bool(thread_id and isinstance(context, dict) and context.get('messages')
                and 0 < offset < len(context['messages']))


class UpstreamError(Exception):
    def __init__(self, status: int, message: str, retry_after=60, *, retryable=None, code: str = ''):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after
        self.code = code
        self.retryable = status in (401, 403, 429, 502, 503, 504) if retryable is None else retryable


@dataclass
class NativeSession:
    provider: str
    bearer: str
    host: str
    path: str
    actor_arn: str
    outpost_id: str
    working_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def transport(self):
        return 'gateway' if self.provider in ('gateway', 'social_gateway') else 'aidp'

    def connection(self):
        working_id = self.working_id
        headers = {'Authorization': 'Bearer '+self.bearer}
        if self.transport == 'gateway':
            headers.update({'X-Surface-Type': DESKTOP_SURFACE, 'X-Outpost-Id': self.outpost_id,
                            'X-Agent-Service-Type': 'UNIFIED_QUICK', 'X-Working-Session-Id': working_id})
            path = self.path or '/ws/service/SpaceNeedleConversationService/operation/ChatBidirectionalStream'
        else:
            params = {'surfaceType': DESKTOP_SURFACE, 'outpostId': self.outpost_id,
                      'workingSessionId': working_id}
            if self.provider == 'social':
                params['token'] = self.bearer
                headers.pop('Authorization')
            path = '/streaming/quickwork-chat-bidirectional-stream?' + urlencode(params)
        return f'wss://{self.host}{path}', headers


class Upstream:
    def __init__(self, store, auth, *, connect=None, transport=None, session_cache=None,
                 app_ping_interval: float = 25.0):
        self.store, self.auth = store, auth
        self.connect = connect or websocket_connect
        self.http_transport = transport
        self.outpost_ids = {}
        self.session_cache = session_cache if session_cache is not None else SessionCache(store=store)
        self.ws_pool = WarmWsPool()
        self._park_in_tests = False
        self.app_ping_interval = app_ping_interval
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._agent_ensure_failed: dict[str, float] = {}

    async def session(self, account_id):
        credentials = await self.auth.credentials(account_id)
        provider = credentials['provider']
        bearer = credentials['access_token'] if provider in ('gateway', 'social_gateway', 'social') else credentials.get('id_token')
        if not bearer:
            raise UpstreamError(401, '原生授权缺少当前传输所需的会话令牌')
        tenant = trusted_url(credentials['tenantUrl'])
        discovery = {}
        if provider in ('gateway', 'social_gateway'):
            async with httpx.AsyncClient(transport=self.http_transport, timeout=15, trust_env=False,
                                         follow_redirects=False) as client:
                response = await client.get(tenant.rstrip('/')+'/v1/discovery',
                                            headers={'Authorization': 'Bearer '+bearer,
                                                     'x-amzn-qs-internal-trace-id': str(uuid.uuid4())})
            if response.status_code != 200:
                retry = response.headers.get('Retry-After', '')
                retry_after = min(3600, max(1, int(retry))) if retry.isdigit() else 60
                raise UpstreamError(response.status_code if response.status_code in (401, 403, 429) else 503,
                                    'Quick 原生服务发现失败', retry_after)
            discovery = response.json()
            cell = discovery.get('cellHost') or discovery.get('wsHost')
        else:
            # AIDP's native /auth/sncs-session uses tenant_url directly.
            # Only the Gateway transport performs /v1/discovery.
            cell = tenant
        if not isinstance(cell, str) or not cell:
            raise UpstreamError(502, 'Quick 服务发现未返回可用聊天主机')
        trusted_url(cell if '://' in cell else 'https://'+cell)
        host = urlsplit(cell if '://' in cell else 'https://'+cell).netloc
        info = credentials.get('user_info', {})
        path = discovery.get('wsPath', '')
        if not isinstance(path, str) or (path and (not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path)):
            raise UpstreamError(502, 'Quick 服务发现返回了无效的聊天路径')
        actor = info.get('quickSightExternalUserArn') or info.get('userArn', '')
        return NativeSession(provider, bearer, host, path, actor,
                             self.outpost_ids.setdefault(account_id, str(uuid.uuid4())))

    async def _run_agent_control(self, account_id: str, step_fn, *, timeout: float = 15.0):
        session = await self.session(account_id)
        decoder = NativeMessageDecoder(session.transport)
        url, headers = session.connection()
        async with self.connect(
            url, additional_headers=headers, proxy=None, max_size=16 * 1024 * 1024,
            open_timeout=15, close_timeout=5,
            ping_interval=20 if session.transport == 'gateway' else None,
            ping_timeout=20,
        ) as socket:
            async def send(msg):
                await socket.send(encode_message(session.transport, msg))

            await send(build_session_connect(
                session.transport, outpost_id=session.outpost_id,
                actor_arn=session.actor_arn, app_version=CLIENT_VERSION,
            ))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamError(504, 'AWS Quick 自定义 Agent 控制指令超时', 10)
                try:
                    raw = await asyncio.wait_for(socket.recv(), remaining)
                except ConnectionClosed:
                    raise UpstreamError(502, 'AWS Quick 自定义 Agent 连接提前关闭', 10) from None
                if isinstance(raw, str):
                    control = json.loads(raw)
                    events = [control] if isinstance(control, dict) else []
                else:
                    events = decoder.feed(raw)
                for event in events:
                    ev_type = event.get('type') or ''
                    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
                    done, result = await step_fn(ev_type, payload, send)
                    if done:
                        return result

    async def list_chat_agents(self, account_id: str) -> list[dict]:
        sent = False

        async def step(ev_type, payload, send):
            nonlocal sent
            if ev_type in ('session_established', 'connected') and not sent:
                sent = True
                await send({'type': 'get_chat_agents', 'payload': {}})
                return False, None
            if ev_type == 'chat_agents_updated':
                raw_agents = payload.get('agents') if isinstance(payload.get('agents'), list) else []
                agents = []
                for item in raw_agents:
                    if not isinstance(item, dict):
                        continue
                    raw_id = str(item.get('agentId') or '').strip()
                    if not raw_id:
                        continue
                    short_id = normalize_agent_id(raw_id)
                    is_system = (
                        short_id.upper() == 'SYSTEM'
                        or raw_id.endswith('/SYSTEM')
                        or item.get('isSystemDefault') is True
                    )
                    agents.append({
                        'agent_id': raw_id,
                        'short_id': short_id,
                        'name': str(item.get('name') or short_id),
                        'description': str(item.get('description') or ''),
                        'system_prompt': str(item.get('systemPrompt') or ''),
                        'is_system': is_system,
                    })
                return True, agents
            if ev_type == 'error':
                err = payload.get('error') or payload.get('message') or 'unknown'
                raise UpstreamError(502, f'获取自定义 Agent 列表失败：{err}', 10)
            return False, None

        return await self._run_agent_control(account_id, step, timeout=12.0)

    async def create_coding_agent(self, account_id: str, *, name: str | None = None,
                                  description: str | None = None,
                                  instructions: str | None = None) -> dict:
        target_name = (name or '').strip() or DEFAULT_CODING_AGENT_NAME
        target_desc = (description or '').strip() or DEFAULT_CODING_AGENT_DESCRIPTION
        target_inst = (instructions or '').strip() or DEFAULT_CODING_AGENT_INSTRUCTIONS
        stage = 'init'
        draft_id = ''
        saved_deadline = 0.0

        async def step(ev_type, payload, send):
            nonlocal stage, draft_id, saved_deadline
            if ev_type in ('session_established', 'connected') and stage == 'init':
                stage = 'creating'
                await send({'type': 'create_agent_draft', 'payload': {'new_agent': True}})
                return False, None
            if ev_type == 'agent_draft_created' and stage == 'creating':
                draft_id = str(payload.get('agent_id') or '').strip()
                if not draft_id:
                    raise UpstreamError(502, 'AWS Quick 未返回新建 Agent 草稿 ID', 10)
                stage = 'updating'
                await send({
                    'type': 'update_chat_agent_config',
                    'payload': {
                        'agent_id': draft_id,
                        'target': 'draft',
                        'name': target_name,
                        'description': target_desc,
                        'instructions': target_inst,
                        'skill_ids': [],
                        'capabilities': [],
                    },
                })
                saved_deadline = time.monotonic() + 1.5
                return False, None
            if stage == 'updating' and (ev_type == 'agent_draft_saved' or (saved_deadline and time.monotonic() >= saved_deadline)):
                stage = 'publishing'
                await send({'type': 'publish_agent_draft', 'payload': {'agent_id': draft_id}})
                return False, None
            if ev_type == 'publish_agent_draft_success':
                final_id = normalize_agent_id(str(payload.get('agent_id') or draft_id))
                return True, {'agent_id': final_id, 'name': target_name}
            if ev_type in ('publish_agent_draft_error', 'update_chat_agent_config_error', 'error'):
                err = payload.get('error') or payload.get('message') or ev_type
                raise UpstreamError(502, f'创建自定义 Agent 失败：{err}', 10)
            return False, None

        return await self._run_agent_control(account_id, step, timeout=18.0)

    async def update_chat_agent(self, account_id: str, agent_id: str, *, instructions: str,
                                name: str | None = None, description: str | None = None) -> dict:
        clean_id = normalize_agent_id(agent_id)
        if not clean_id or not _AGENT_ID_RE.match(clean_id):
            raise UpstreamError(400, '无效的自定义 Agent ID', retryable=False)
        clean_inst = (instructions or '').strip()
        if not clean_inst:
            raise UpstreamError(400, 'Agent 提示词不能为空', retryable=False)
        stage = 'init'
        saved_deadline = 0.0

        async def step(ev_type, payload, send):
            nonlocal stage, saved_deadline
            if ev_type in ('session_established', 'connected') and stage == 'init':
                stage = 'drafting'
                await send({'type': 'create_agent_draft',
                            'payload': {'agent_id': clean_id, 'ccp_files_supported': True}})
                return False, None
            if ev_type == 'agent_draft_created' and stage == 'drafting':
                stage = 'updating'
                cfg = {
                    'agent_id': clean_id,
                    'target': 'draft',
                    'instructions': clean_inst,
                    'skill_ids': [],
                    'capabilities': [],
                }
                if name and name.strip():
                    cfg['name'] = name.strip()
                if isinstance(description, str):
                    cfg['description'] = description.strip()
                await send({'type': 'update_chat_agent_config', 'payload': cfg})
                saved_deadline = time.monotonic() + 1.5
                return False, None
            if stage == 'updating' and (ev_type == 'agent_draft_saved' or (saved_deadline and time.monotonic() >= saved_deadline)):
                stage = 'publishing'
                await send({'type': 'publish_agent_draft', 'payload': {'agent_id': clean_id}})
                return False, None
            if ev_type == 'publish_agent_draft_success':
                return True, {'agent_id': clean_id}
            if ev_type in ('publish_agent_draft_error', 'update_chat_agent_config_error', 'error'):
                err = payload.get('error') or payload.get('message') or ev_type
                raise UpstreamError(502, f'更新自定义 Agent 失败：{err}', 10)
            return False, None

        res = await self._run_agent_control(account_id, step, timeout=18.0)
        self.session_cache.invalidate_account(account_id)
        return res

    async def ensure_coding_agent(self, account_id: str, *, force_create: bool = False,
                                  custom_prompt: str = '') -> dict:
        lock = self._agent_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            if not force_create:
                acct = self.store.account(account_id)
                if acct and acct.get('chat_agent_id'):
                    return {'agent_id': acct['chat_agent_id'], 'name': acct.get('chat_agent_name') or DEFAULT_CODING_AGENT_NAME, 'created': False}
            agents = await self.list_chat_agents(account_id)
            custom = [a for a in agents if not a.get('is_system')]
            chosen = None
            if not force_create:
                for preferred in LEGACY_CODING_AGENT_NAMES:
                    chosen = next((a for a in custom if a['name'].strip().lower() == preferred.lower()), None)
                    if chosen:
                        break
                if chosen is None and custom:
                    chosen = custom[0]
            created = False
            if chosen is not None:
                agent_id, agent_name = chosen['short_id'], chosen['name']
            else:
                res = await self.create_coding_agent(account_id, instructions=custom_prompt or DEFAULT_CODING_AGENT_INSTRUCTIONS)
                agent_id, agent_name, created = res['agent_id'], res['name'], True
            self.store.patch_account(account_id, {'chat_agent_id': agent_id, 'chat_agent_name': agent_name})
            self.session_cache.invalidate_account(account_id)
            self._agent_ensure_failed.pop(account_id, None)
            return {'agent_id': agent_id, 'name': agent_name, 'created': created}

    async def stream(self, account: dict, messages: list[dict], *, agent_mode='smart', thinking_effort='off',
                     images=None, context=None, reuse_thread_id: str | None = None, delta_offset: int = 0,
                     state_out: dict | None = None, stream_timeout_sec: int = 300,
                     web_search_enabled: bool = False, incognito: bool = False):
        refreshed = False
        chat_agent_id = (account.get('chat_agent_id') or '').strip() or None
        if not chat_agent_id and self.connect is websocket_connect and self.http_transport is None:
            settings = self.store.get_settings()
            if settings.get('auto_ensure_coding_agent', True) and self._agent_ensure_failed.get(account['id'], 0) < time.monotonic():
                try:
                    ensured = await self.ensure_coding_agent(
                        account['id'], custom_prompt=str(settings.get('custom_agent_prompt') or ''),
                    )
                    chat_agent_id = ensured['agent_id']
                except Exception as exc:
                    logging.getLogger(__name__).warning('auto_ensure_coding_agent failed for %s: %s', account['id'], exc)
                    self._agent_ensure_failed[account['id']] = time.monotonic() + 300
        try:
            while True:
                emitted = False
                try:
                    session = await self.session(account['id'])
                    try:
                        async for text in self._stream(
                            session, account['id'], messages, agent_mode=agent_mode,
                            thinking_effort=thinking_effort, images=images, context=context,
                            reuse_thread_id=reuse_thread_id, delta_offset=delta_offset,
                            state_out=state_out, stream_timeout_sec=stream_timeout_sec,
                            web_search_enabled=web_search_enabled, chat_agent_id=chat_agent_id,
                            incognito=incognito,
                        ):
                            emitted = True
                            yield text
                    except UpstreamError as err:
                        if err.status == 401 or emitted or not _can_reuse(reuse_thread_id, context, delta_offset):
                            raise
                        self.session_cache.invalidate(reuse_thread_id)
                        if err.code in ('service_error', 'ThrottlingException', 'rate_limited') or err.status == 429:
                            raise
                        reuse_thread_id, delta_offset = None, 0
                        continue
                    return
                except (InvalidStatus, UpstreamError) as error:
                    status = error.response.status_code if isinstance(error, InvalidStatus) else error.status
                    if status != 401 or refreshed or emitted or (isinstance(error, UpstreamError) and not error.retryable):
                        raise
                    self.auth._audit(account['id'], 'chat_refresh_retry', status=status)
                    await self.auth.credentials(account['id'], force=True)
                    refreshed = True
        except AuthError as error:
            raise UpstreamError(error.status, str(error), 20) from None
        except (TimeoutError, httpx.TimeoutException):
            raise UpstreamError(504, 'Quick 原生服务响应超时', 15) from None
        except httpx.HTTPError:
            raise UpstreamError(502, 'Quick 原生服务网络连接失败', 15) from None
        except InvalidStatus as error:
            status = error.response.status_code
            raise UpstreamError(status if status in (401, 403, 429) else 502,
                                f'Quick 原生 WebSocket 握手返回 HTTP {status}') from None
        except (WebSocketException, OSError):
            raise UpstreamError(502, 'Quick 原生 WebSocket 连接中断', 15) from None
        except NativeProtocolError as error:
            logging.getLogger(__name__).warning('Quick protocol error: %s', error)
            raise UpstreamError(502, 'Quick 原生响应与已核对的客户端协议不一致') from None
        except (ValueError, KeyError, TypeError):
            raise UpstreamError(502, 'Quick 原生响应与已核对的客户端协议不一致') from None

    async def upload_images(self, session, images, thread_id):
        files = [{'data': raw, 'mimeType': mime, 'type': 'image',
                  'name': f'image-{index+1}.' + ('png' if mime == 'image/png' else 'jpg')}
                 for index, (raw, mime) in enumerate(images)]
        return await self.upload_files(session, files, thread_id)

    async def upload_files(self, session, files, thread_id):
        if session.transport != 'aidp':
            raise UpstreamError(400, '附件原生上传目前只核对了个人 AIDP 传输')
        base = 'https://' + session.host + '/integration/quick-work/'
        headers = {'Authorization':'Bearer '+session.bearer, 'x-amzn-qs-internal-trace-id':str(uuid.uuid4())}
        attachments = []
        async with httpx.AsyncClient(transport=self.http_transport, timeout=60, trust_env=False,
                                     follow_redirects=False) as client:
            async def checked(method, path, **kwargs):
                for attempt in range(3):
                    response = await client.request(method, base+path, headers=headers, **kwargs)
                    if response.status_code == 200:
                        return response.json()
                    if response.status_code in (401, 403, 429) or attempt == 2:
                        raise UpstreamError(response.status_code if response.status_code in (401,403,429) else 502,
                                            'Quick 原生附件上传步骤失败')
                    await asyncio.sleep(0.4 * (attempt + 1))
            for file in files:
                raw, mime, name = file['data'], file['mimeType'], file['name']
                for attempt in range(3):
                    response = await client.post(
                            base+'upload-attachment', headers=headers | {'Content-Type':'application/octet-stream'},
                            params={'name':name,'contentType':mime,'contentLength':str(len(raw))},content=raw)
                    if response.status_code == 200:
                        break
                    if response.status_code in (401, 403, 429) or attempt == 2:
                        raise UpstreamError(response.status_code if response.status_code in (401,403,429) else 502,
                                            'Quick 原生附件上传失败')
                    await asyncio.sleep(0.4 * (attempt + 1))
                aid = response.json().get('attachmentId')
                if not isinstance(aid,str) or not aid:
                    raise UpstreamError(502, 'Quick 附件上传缺少附件标识')
                for step in range(90):
                    dlp = await checked('GET','attachment-dlp-status', params={'attachmentId':aid})
                    if dlp.get('dlpJobStatus') in ('COMPLETED','SKIPPED','NOT_APPLICABLE'):
                        if dlp.get('policyAction') != 'ALLOW':
                            raise UpstreamError(400, 'Quick 附件 DLP 未允许该附件；未绕过警告或阻止策略')
                        break
                    if dlp.get('dlpJobStatus') in ('FAILED','ERROR'):
                        raise UpstreamError(502, 'Quick 附件 DLP 检查失败')
                    await asyncio.sleep(0.3 if step < 3 else 0.7 if step < 8 else 1.5)
                else:
                    raise UpstreamError(504, 'Quick 附件 DLP 检查超时')
                body = {'eventType':'invokeAgent','eventBody':{'invokeAgent':{
                    'path':'files/upload/'+thread_id, 'queryParams':{'_method':'POST'},
                    'body':{'attachmentId':aid}, 'workingSessionId':session.working_id,
                    'outpostId':session.outpost_id}}}
                delivered = await checked('POST','qbs-event',json=body)
                result = delivered['eventResponse']['invokeAgentResponse']
                if isinstance(result,str):
                    result = json.loads(result)
                result = result.get('api_response',result)
                if not 200 <= result.get('status',200) < 300:
                    raise UpstreamError(502,'Quick 附件交付服务未接受附件')
                result = result.get('body',{})
                if isinstance(result,str):
                    result = json.loads(result)
                path = result.get('path') or (result.get('files') or [{}])[0].get('path')
                if not isinstance(path,str) or not path:
                    raise UpstreamError(502,'Quick 附件交付未返回附件路径')
                attachments.append({'type':file['type'],'name':name,'size':len(raw),'mimeType':mime,
                    'path':path,'attachmentId':aid,'contentSha256':hashlib.sha256(raw).hexdigest()})
        return attachments

    @staticmethod
    def _render_context_txt(payload_obj, *, continuation=False) -> bytes:
        """Render ONLY past conversation turns as plain reference text (never embed system/tool instructions in attachments)."""
        if not isinstance(payload_obj, dict):
            return str(payload_obj).encode('utf-8')
        msgs = payload_obj.get('messages') or []
        conv_msgs = [m for m in msgs if isinstance(m, dict) and m.get('role') not in ('system', 'developer')]
        lines = []
        for idx, m in enumerate(conv_msgs):
            role = m.get('role', 'user')
            content = m.get('content')
            meta = []
            if m.get('tool_call_id'):
                meta.append(f"tool_call_id={m['tool_call_id']}")
            if m.get('name'):
                meta.append(f"name={m['name']}")
            header = f'[Turn {idx} · {role}' + (f' ({", ".join(meta)})' if meta else '') + ']'
            body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if m.get('tool_calls'):
                tc = json.dumps(m['tool_calls'], ensure_ascii=False, separators=(',', ':'))
                body = (body + '\n' if body else '') + f'tool_calls={tc}'
            lines.append(f'{header}\n{body}')
        data = '\n\n'.join(lines).encode('utf-8')
        if len(data) > 5 * 1024 * 1024:
            data = data[-5 * 1024 * 1024:]
        return data

    @staticmethod
    def _build_custom_prompt(context, *, has_custom_agent: bool = False, agent_mode: str = 'smart') -> str | None:
        """Build native AWS Quick custom_prompt carrying system instructions and <tool_call> schemas."""
        if not isinstance(context, dict):
            return None
        parts = []
        msgs = context.get('messages') or []
        sys_msgs = [str(m.get('content') or '').strip() for m in msgs
                    if isinstance(m, dict) and m.get('role') in ('system', 'developer') and str(m.get('content') or '').strip()]
        if sys_msgs:
            sys_text = '\n\n'.join(sys_msgs)
            if len(sys_text) > 8000:
                sys_text = sys_text[:8000]
            parts.append(sys_text)
        tools = context.get('tools') or []
        choice = context.get('tool_choice', 'auto')
        if tools and choice != 'none':
            compact = []
            for t in tools:
                fn = t.get('function', t) if isinstance(t, dict) else {}
                params = fn.get('parameters') or {'type': 'object'}
                props = params.get('properties') if isinstance(params, dict) else None
                if isinstance(props, dict):
                    clean_props = {k: {pk: pv for pk, pv in (v.items() if isinstance(v, dict) else ())
                                       if pk in ('type', 'enum', 'items', 'properties', 'required')}
                                   for k, v in props.items()}
                    compact_schema = {'type': 'object', 'properties': clean_props}
                    if params.get('required'):
                        compact_schema['required'] = params['required']
                else:
                    compact_schema = params
                compact.append({
                    'name': fn.get('name', ''),
                    'description': (fn.get('description') or '')[:180],
                    'parameters': compact_schema,
                })
            serialized = json.dumps(compact, ensure_ascii=False, separators=(',', ':'))
            if len(serialized) > 18000:
                minimal = [{'name': c['name'], 'parameters': c['parameters']} for c in compact]
                serialized = json.dumps(minimal, ensure_ascii=False, separators=(',', ':'))[:18000]
            if choice == 'required':
                choice_rule = 'You must output at least one `<tool_call>` block from the schemas above in this turn.'
            elif isinstance(choice, dict):
                sel_name = (choice.get('function') or choice).get('name', '')
                choice_rule = f'Output the `<tool_call>` block named "{sel_name}" in this turn.'
            else:
                choice_rule = "When a tool above fits the user's request, emit the corresponding `<tool_call>` block; otherwise reply in plain text."
            heading = '## Client Tools (JSON Schema)' if has_custom_agent else '## Tool Schemas (JSON)'
            local_priority = (
                "IMPORTANT: For listing, searching, reading, creating, or editing local files/directories and running shell commands, "
                "ALWAYS emit a matching `<tool_call>` block from the schemas above so the caller executes it on the user's machine; "
                "NEVER use built-in cloud workspace/search tools when a matching client tool exists."
                if (agent_mode == 'fast' or choice != 'auto') else
                "For file, shell, and workspace actions, emit matching `<tool_call>` blocks from the schemas above — "
                "the user's local project files, directories, and shell run on their machine and are accessed directly through these client tools."
            )
            parts.append('\n'.join([
                heading,
                'The caller executes `<tool_call>` JSON blocks locally and returns results on the next turn.',
                serialized,
                choice_rule,
                local_priority,
                'Format (raw tags, no markdown fences; stop immediately after the block(s)):',
                '<tool_call>{"name":"<tool_name>","arguments":{...}}</tool_call>',
            ]))
        fmt = context.get('response_format') or {'type': 'text'}
        if isinstance(fmt, dict) and fmt.get('type') not in (None, 'text'):
            rule = 'Return a valid JSON object only, without markdown fences.'
            if fmt.get('type') == 'json_schema' and isinstance(fmt.get('json_schema'), dict):
                rule += ' Conform to JSON Schema: ' + json.dumps(fmt['json_schema'].get('schema'), ensure_ascii=False, separators=(',', ':'))
            parts.append(rule)
        return '\n\n'.join(parts) if parts else None

    @staticmethod
    def _offload_prompt(context, encode_fn, *, continuation=False):
        base = (
            'Earlier conversation history is attached in conversation-context.txt '
            '(preserving roles, turn order, and tool call IDs). '
            'Answer the latest user message directly; use the declared tools when needed.'
        )
        if not isinstance(context, dict):
            return base
        ctx_msgs = context.get('messages') or []
        conv_msgs = [m for m in ctx_msgs if isinstance(m, dict) and m.get('role') not in ('system', 'developer')]
        if continuation:
            base = _CONTINUATION + '\nFull incremental messages are attached in conversation-context.txt.'
            header = base + '\n\nINLINE SUMMARY (incremental messages only):\n'
        else:
            header = (
                base + f'\n\nINLINE SUMMARY (total messages in conversation-context.txt: {len(ctx_msgs)}): '
                'The most recent conversation turns are included inline below so you can respond immediately '
                'WITHOUT calling file_read or run_python unless you specifically need an older omitted turn '
                'or truncated output from conversation-context.txt.\n'
            )

        def trim_msg(m, limit=4000):
            copy = dict(m)
            c = copy.get('content')
            if isinstance(c, str) and len(c) > limit:
                copy['content'] = c[:limit // 2] + f'\n...[inline preview truncated {len(c) - limit} chars; full text in conversation-context.txt]...\n' + c[-limit // 2:]
            return copy

        selected = []
        for idx in range(len(conv_msgs) - 1, -1, -1):
            limit = 10000 if idx >= len(conv_msgs) - 2 else 3000
            candidate = [(idx, trim_msg(conv_msgs[idx], limit))] + selected
            omitted = idx
            prefix = f'\nRecent conversation turns ({omitted} older turns omitted from inline view; available in conversation-context.txt if needed):\n'
            body = '\n'.join(f'[{i}] ' + json.dumps(item, ensure_ascii=False, separators=(',', ':')) for i, item in candidate)
            if len(encode_fn(header + prefix + body)) <= 56_000:
                selected = candidate
            else:
                if not selected:
                    candidate = [(idx, trim_msg(conv_msgs[idx], 2500))]
                    body = '\n'.join(f'[{i}] ' + json.dumps(item, ensure_ascii=False, separators=(',', ':')) for i, item in candidate)
                    if len(encode_fn(header + prefix + body)) <= 56_000:
                        selected = candidate
                break
        if selected:
            omitted = selected[0][0]
            prefix = f'\nRecent conversation turns ({omitted} older turns omitted from inline view; available in conversation-context.txt if needed):\n'
            body = '\n'.join(f'[{i}] ' + json.dumps(item, ensure_ascii=False, separators=(',', ':')) for i, item in selected)
            if len(encode_fn(header + prefix + body)) <= 58_000:
                return header + prefix + body
        if len(encode_fn(header)) <= 58_000:
            return header
        return base

    async def _stream(self, session, account_id, messages, *, agent_mode='smart', thinking_effort='off',
                      images=None, context=None, reuse_thread_id: str | None = None, delta_offset: int = 0,
                      state_out: dict | None = None, stream_timeout_sec: int = 300,
                      web_search_enabled: bool = False, chat_agent_id: str | None = None,
                      incognito: bool = False):
        reuse = _can_reuse(reuse_thread_id, context, delta_offset)
        parked = await self.ws_pool.checkout(reuse_thread_id, account_id) if (reuse and reuse_thread_id) else None
        if incognito and reuse and parked is None:
            # Incognito threads live only on their parked WS; if it closed, fall back to a fresh cold turn.
            if reuse_thread_id:
                self.session_cache.invalidate(reuse_thread_id)
            reuse = False
        thread_id = reuse_thread_id if reuse else str(uuid.uuid4())
        if state_out is not None:
            state_out['thread_id'] = thread_id
            state_out['reused'] = reuse
        text = NativeTextEvents(thread_id)
        if reuse:
            delta_msgs = context['messages'][delta_offset:]
            visible_delta = [m for m in delta_msgs if m['role'] != 'system']
            prompt = _CONTINUATION + '\n\n' + '\n\n'.join(
                f"[{m['role']}]\n{json.dumps(m, ensure_ascii=False, separators=(',', ':'))}" for m in visible_delta)
            if images and not any('[图片附件' in (m.get('content') or '') for m in visible_delta):
                images = None
        else:
            # OpenAI history is serialized as text in a fresh Quick conversation.
            prompt = messages[0]['content'] if len(messages) == 1 else '\n\n'.join(
                f"[{m['role']}]\n{m['content']}" for m in messages)
        emitted, refresh_at = False, time.monotonic()+900
        should_park = self.connect is websocket_connect or self._park_in_tests
        cm = None
        parked_ok = False
        if parked is not None:
            socket, decoder, session = parked.socket, parked.decoder, parked.session
        else:
            decoder = NativeMessageDecoder(session.transport)
            url, headers = session.connection()
            # AIDP uses browser-style keepalives and doesn't answer WS Ping; Gateway does.
            cm = self.connect(url, additional_headers=headers, proxy=None, max_size=16*1024*1024,
                              open_timeout=15, close_timeout=5,
                              ping_interval=20 if session.transport == 'gateway' else None,
                              ping_timeout=20)
            socket = await cm.__aenter__()
        try:
            async def send(message):
                await socket.send(encode_message(session.transport, message))
            if parked is None:
                await send(build_session_connect(session.transport, outpost_id=session.outpost_id,
                                                 actor_arn=session.actor_arn, app_version=CLIENT_VERSION))
                await send({'type': 'federate_status', 'payload': {}})
                if not reuse:
                    await send(build_session_create(thread_id))
                await send(build_conversation_connect(thread_id, outpost_id=session.outpost_id,
                                                       app_version=CLIENT_VERSION))
            attachments = await self.upload_images(session, images, thread_id) if images else []
            custom_prompt = None
            if isinstance(context, dict) and (context.get('tools') or any(
                isinstance(m, dict) and m.get('role') in ('system', 'developer') for m in (context.get('messages') or [])
            )):
                custom_prompt = self._build_custom_prompt(context, has_custom_agent=bool(chat_agent_id), agent_mode=agent_mode)
                if not reuse:
                    # Keep user message text clean (only non-system conversation turns) when custom_prompt carries system+tools.
                    conv_only = [m for m in (context.get('messages') or []) if isinstance(m, dict) and m.get('role') not in ('system', 'developer')]
                    if len(conv_only) == 1 and set(conv_only[0]) == {'role', 'content'}:
                        prompt = str(conv_only[0]['content'])
                    elif conv_only:
                        prompt = '\n\n'.join(
                            f"[{m.get('role', 'user')}]\n" + (m['content'] if isinstance(m.get('content'), str) and len(m) == 2
                                                             else json.dumps(m, ensure_ascii=False, separators=(',', ':')))
                            for m in conv_only
                        )
                choice = context.get('tool_choice', 'auto')
                if isinstance(choice, dict):
                    forced_name = (choice.get('function') or choice).get('name', '')
                    if forced_name:
                        prompt = f'{prompt}\n\n[Use `<tool_call>` to invoke `{forced_name}` now.]'
                elif choice == 'required':
                    prompt = f'{prompt}\n\n[Use `<tool_call>` to invoke a required tool now.]'
            def user_message(text):
                return build_user_message(text, thread_id, agent_mode=agent_mode, thinking_effort=thinking_effort,
                    attachments=attachments, outpost_id=session.outpost_id, app_version=CLIENT_VERSION,
                    custom_prompt=custom_prompt, chat_agent_id=chat_agent_id,
                    web_search_enabled=web_search_enabled, incognito=incognito)
            message = user_message(prompt)
            encoded = encode_message(session.transport, message)
            inline_bytes = len(encoded)
            text_bytes = len(prompt.encode('utf-8'))
            json_bytes = len(json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
            # Conservative wire budget also covers AIDP base64 expansion below the observed 64 KB limit.
            if inline_bytes > 60_000:
                if reuse:
                    payload_obj = {**context, 'messages': delta_msgs}
                else:
                    payload_obj = context if context is not None else {'messages': messages}
                document = self._render_context_txt(payload_obj, continuation=reuse)
                attachments += await self.upload_files(session, [{'data': document, 'name': 'conversation-context.txt',
                    'mimeType': 'text/plain', 'type': 'file'}], thread_id)
                prompt = self._offload_prompt(payload_obj, lambda p: encode_message(session.transport, user_message(p)),
                                              continuation=reuse)
                message = user_message(prompt)
                encoded = encode_message(session.transport, message)
            logging.getLogger(__name__).info('quick_message text_bytes=%d json_bytes=%d inline_wire_bytes=%d wire_bytes=%d offloaded=%s parked=%s',
                text_bytes, json_bytes, inline_bytes, len(encoded), inline_bytes > 60_000, parked is not None)
            if len(encoded) > 60_000 and custom_prompt and len(custom_prompt) > 4000:
                # Auto-compact custom_prompt if attachments + custom_prompt pushed the frame past 60 KB.
                custom_prompt = custom_prompt[:4000]
                message = user_message(prompt)
                encoded = encode_message(session.transport, message)
            if len(encoded) > 60_000:
                raise UpstreamError(400, 'Quick 附件引用消息仍超过安全发送预算；请减少附件数量', retryable=False)
            try:
                await socket.send(encoded)
            except (ConnectionClosed, WebSocketException, OSError):
                if parked is not None:
                    raise UpstreamError(502, 'Quick 复用长连接已断开，自动切换新连接重试') from None
                raise
            started_at = time.monotonic()
            timeout_sec = max(60, min(900, int(stream_timeout_sec)))
            deadline = started_at + timeout_sec
            last_recv_at = started_at
            next_ping_at = started_at + self.app_ping_interval if self.app_ping_interval else float('inf')
            while time.monotonic() < deadline:
                now = time.monotonic()
                if session.transport == 'gateway' and now >= refresh_at:
                    fresh = await self.auth.credentials(account_id, force=True)
                    await socket.send(build_token_refresh(fresh['access_token']))
                    refresh_at = now + 900
                if self.app_ping_interval and now >= next_ping_at:
                    await send({'type': 'ping', 'payload': {'timestamp': int(time.time() * 1000)}})
                    next_ping_at = now + self.app_ping_interval
                recv_budget = min(90 - (now - last_recv_at), deadline - now)
                if recv_budget <= 0:
                    raise TimeoutError
                wait_for_sec = min(recv_budget, max(0.05, next_ping_at - now)) if self.app_ping_interval else recv_budget
                try:
                    raw = await asyncio.wait_for(socket.recv(), wait_for_sec)
                    last_recv_at = time.monotonic()
                except TimeoutError:
                    if time.monotonic() - last_recv_at >= 90 or time.monotonic() >= deadline:
                        raise
                    continue
                except ConnectionClosed:
                    decoder.finish()
                    raise UpstreamError(502, 'Quick 原生流在完成事件之前关闭') from None
                if isinstance(raw, str):
                    control = json.loads(raw)
                    events = [control] if isinstance(control, dict) else []
                else:
                    events = decoder.feed(raw)
                for event in events:
                    kind = event.get('type')
                    if kind == 'guardrail_blocked' and event.get('thread_id') in (None, '', thread_id):
                        raise UpstreamError(400, 'Quick 上游安全护栏拦截了本轮内容，请调整表述后重试', retryable=False)
                    if kind == 'turn_aborted' and event.get('thread_id') in (None, '', thread_id):
                        raise UpstreamError(502, 'Quick 上游中止了本轮生成，请重试', retryable=False)
                    if kind == 'error' and event.get('thread_id') in (None, '', thread_id):
                        payload = event.get('payload', {})
                        payload = payload if isinstance(payload, dict) else {}
                        code = event.get('code') or payload.get('error_code') or payload.get('code', '')
                        if code in ('FrameTooLarge', 'context_overflow', 'bad_request', 'conversation_format',
                                    'INVALID_REQUEST'):
                            raise UpstreamError(400, f'Quick 请求被拒绝：{code}；请检查请求大小或上下文格式',
                                                retryable=False)
                        if code == 'content_filtered':
                            raise UpstreamError(400, 'Quick 上游内容过滤拦截了本轮回复，请调整表述或开启新对话后重试',
                                                retryable=False, code='content_filtered')
                        status = 429 if code in ('ThrottlingException', 'rate_limited') else 502
                        retryable = event.get('retryable', payload.get('retryable'))
                        safe_code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', code) else ''
                        source = event.get('source') or payload.get('source') or ''
                        safe_source = source if isinstance(source, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', source) else ''
                        raw_msg = event.get('message') or payload.get('message') or payload.get('error') or ''
                        msg_summary = re.sub(r'\s+', ' ', str(raw_msg))[:160] if isinstance(raw_msg, str) else ''
                        logging.getLogger(__name__).warning(
                            'quick_error_event transport=%s code=%s source=%s retryable=%s emitted=%s reused=%s elapsed=%.2fs msg=%r',
                            session.transport, safe_code or 'unknown', safe_source or 'unknown', retryable, emitted,
                            reuse, time.monotonic() - started_at, msg_summary)
                        detail = f'Quick 原生聊天返回错误事件 ({safe_code})' if safe_code else 'Quick 原生聊天返回错误事件'
                        raise UpstreamError(status, detail,
                                            retryable=retryable if isinstance(retryable, bool) else None,
                                            code=safe_code)
                    update = text.feed(event)
                    if update.delta:
                        emitted = True
                        yield update.delta
                    if update.done:
                        decoder.finish()
                        if not emitted:
                            raise UpstreamError(502, 'Quick 原生流已结束，但没有文本回复')
                        if should_park:
                            await self.ws_pool.park(thread_id, account_id, socket, decoder, session)
                            parked_ok = True
                        return
            raise UpstreamError(504, 'Quick 原生聊天超过五分钟时限', 15)
        finally:
            if not parked_ok:
                if cm is not None:
                    await cm.__aexit__(None, None, None)
                else:
                    await _safe_close(socket)
