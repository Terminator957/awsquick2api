from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .admin_api import handle_batch_action, register_admin_routes
from .api_keys import KeyAuthError, verify_managed_key
from .cli_api import register_cli_routes
from .compat import CompatibilityError, Prepared
from .models import (
    DEFAULT_MODEL,
    MODEL_IDS,
    AccountCreate,
    AccountPatch,
    BatchAccountAction,
    ChatRequest,
    NativeLoginRequest,
    model_options,
)
from .native_auth import AuthError, NativeAuth
from .native_upstream import Upstream, UpstreamError
from .pool import Pool, PoolUnavailable
from .quota import Quotas, is_quota_exhausted
from .store import Store
from .transfer import TransferError, export_accounts, import_accounts


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    passphrase: str = Field(min_length=12,max_length=1024,repr=False)
    account_ids: list[str] | None = None
    bundle: dict | None = None


def create_app(data_dir: Path | None = None, transport=None, ws_connect=None):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(data_dir or Path(os.getenv("QUICKPOOL_DATA_DIR", "data")))
        auth = NativeAuth(store, port=int(os.getenv("QUICKPOOL_CALLBACK_PORT", "18080")), transport=transport)
        app.state.store, app.state.auth = store, auth
        app.state.quotas = Quotas(auth, transport)
        app.state.pool = Pool(store, app.state.quotas)
        app.state.upstream = Upstream(store, auth, connect=ws_connect, transport=transport)
        app.state.upstream.session_cache.set_ttl(store.get_settings().get("session_cache_ttl", 1800))
        auth.start()
        quota_task = None
        if transport is None and ws_connect is None:
            async def refresh_quotas_loop():
                await asyncio.sleep(5)
                while True:
                    sem = asyncio.Semaphore(2)
                    async def _one(aid):
                        async with sem:
                            try:
                                await app.state.quotas.refresh(aid)
                            except Exception:
                                pass
                    configured = [a['id'] for a in store.accounts() if a['configured'] and a['enabled']]
                    if configured:
                        await asyncio.gather(*(_one(aid) for aid in configured))
                    await asyncio.sleep(300)
            quota_task = asyncio.create_task(refresh_quotas_loop())
        print(f"QuickPool {__version__} | Admin key: {store.directory / 'admin.key'}", flush=True)
        try:
            yield
        finally:
            if quota_task is not None:
                quota_task.cancel()
                try:
                    await quota_task
                except asyncio.CancelledError:
                    pass
            await auth.close()
            store.close()

    app = FastAPI(title="QuickPool · Amazon Quick Gateway", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):
        # Bound both Content-Length and streamed/chunked request bodies.
        limit = 5 * 1024 * 1024
        total = 0
        chunks = []
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                return JSONResponse({"error": {"message": "请求体超过 5 MiB", "type": "invalid_request_error"}}, 413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default details can echo credentials or the complete request.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"error": {"message": "请求参数无效", "type": "invalid_request_error",
                                       "details": errors}}, 422)

    def admin(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(token.encode(), request.app.state.store.admin_key.encode()):
            raise HTTPException(401, "管理密钥无效")

    current_key_ctx: ContextVar[tuple[str, str]] = ContextVar("quickpool_api_key", default=("", ""))

    def api_access(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.url.path in ('/messages', '/v1/messages', '/messages/count_tokens', '/v1/messages/count_tokens'):
            token = token or request.headers.get('x-api-key', '')
        store = request.app.state.store
        if hmac.compare_digest(token.encode(), store.api_key.encode()):
            current_key_ctx.set(("", ""))
            return
        try:
            managed = verify_managed_key(store, token)
        except KeyAuthError as exc:
            raise HTTPException(exc.status, exc.message) from None
        if managed is None:
            raise HTTPException(401, "API 密钥无效")
        store.record_api_key_use(managed["id"])
        current_key_ctx.set((managed["id"], managed.get("group_name") or ""))

    def get_account(account_id):
        account = app.state.store.account(account_id)
        if not account:
            raise HTTPException(404, "账号不存在")
        return account

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/overview", dependencies=[Depends(admin)])
    async def overview():
        accounts = app.state.store.accounts()
        for account in accounts:
            account["inflight"] = app.state.pool.inflight[account["id"]]
            quota = app.state.quotas.cached(account['id'])
            account['quota'] = quota
            account['quota_exhausted'] = is_quota_exhausted(quota)
        cache = getattr(app.state.upstream, 'session_cache', None)
        cache_stats = cache.stats() if cache is not None else {
            "hits": 0, "misses": 0, "hit_rate": 0.0, "active_sessions": 0}
        return {"accounts": accounts, "stats": app.state.store.stats(), "cache_stats": cache_stats,
                "version": __version__, "auth_mode": "desktop-oauth"}

    @app.get("/api/accounts/{account_id}/detail", dependencies=[Depends(admin)])
    async def account_detail(account_id: str):
        account = get_account(account_id)
        account["inflight"] = app.state.pool.inflight[account_id]
        quota = app.state.quotas.cached(account_id)
        account["quota"] = quota
        account["quota_exhausted"] = is_quota_exhausted(quota)
        return {
            "account": account,
            "stats": app.state.store.account_stats(account_id),
            "recent_logs": app.state.store.logs(limit=20, account_id=account_id),
            "auth_events": app.state.store.auth_events(account_id, limit=20),
        }

    @app.post("/api/accounts/batch", dependencies=[Depends(admin)])
    async def batch_accounts(body: BatchAccountAction):
        res = await handle_batch_action(app, body.action, body.account_ids, body.value)
        affected = sum(1 for item in res["results"] if item.get("ok"))
        return {"action": body.action, "affected": affected, **res}

    @app.get("/api/config", dependencies=[Depends(admin)])
    async def config():
        settings = app.state.store.get_settings()
        return {"api_key_masked": 'sk-quick-••••••••', "model": DEFAULT_MODEL, "models": list(MODEL_IDS), "version": __version__,
                "strategy": settings.get("scheduling_strategy", "weighted"),
                "max_attempts": settings.get("max_attempts", 3)}

    @app.post('/api/config/reveal-key', dependencies=[Depends(admin)])
    async def reveal_key():
        return {'api_key': app.state.store.api_key}

    @app.post("/api/accounts", dependencies=[Depends(admin)], status_code=201)
    async def add_account(account: AccountCreate):
        if len(app.state.store.accounts()) >= 100:
            raise HTTPException(409, "账号数已达到 100 个")
        return app.state.store.create_account(account)

    @app.exception_handler(TransferError)
    async def transfer_error(request,exc):
        return JSONResponse({'detail':str(exc)},400)

    @app.post('/api/accounts/export', dependencies=[Depends(admin)])
    async def account_export(value: TransferRequest):
        if any(app.state.pool.inflight.values()):
            raise HTTPException(409,'请等待账号池中的请求结束再导出')
        return export_accounts(app.state.store,value.passphrase,value.account_ids)

    @app.post('/api/accounts/import', dependencies=[Depends(admin)])
    async def account_import(value: TransferRequest):
        return import_accounts(app.state.store,value.bundle or {},value.passphrase)

    @app.patch("/api/accounts/{account_id}", dependencies=[Depends(admin)])
    async def patch_account(account_id: str, patch: AccountPatch):
        get_account(account_id)
        values = patch.model_dump(exclude_none=True)
        return app.state.store.patch_account(account_id, values)

    @app.delete("/api/accounts/{account_id}", dependencies=[Depends(admin)], status_code=204)
    async def remove_account(account_id: str):
        get_account(account_id)
        if app.state.pool.inflight[account_id]:
            raise HTTPException(409, "账号仍有运行中的请求")
        app.state.auth.cancel(account_id)
        app.state.store.delete_account(account_id)

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        return JSONResponse({"detail": str(exc)}, exc.status)

    @app.post("/api/accounts/{account_id}/login", dependencies=[Depends(admin)])
    async def login(account_id: str, request: Request, value: NativeLoginRequest | None = None):
        value = value or NativeLoginRequest()
        get_account(account_id)
        if not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(403, "桌面授权须从回调服务所在本机发起")
        if app.state.pool.inflight[account_id]:
            raise HTTPException(409, "请等待当前账号请求结束")
        return await app.state.auth.begin(account_id, value.provider, value.region)

    @app.get("/api/accounts/{account_id}/auth/status", dependencies=[Depends(admin)])
    async def auth_status(account_id: str):
        get_account(account_id)
        return app.state.auth.status(account_id)

    @app.post('/api/accounts/{account_id}/quota/refresh', dependencies=[Depends(admin)])
    async def refresh_quota(account_id: str):
        get_account(account_id)
        return await app.state.quotas.refresh(account_id)

    @app.get("/api/accounts/{account_id}/auth/events", dependencies=[Depends(admin)])
    async def auth_events(account_id: str):
        get_account(account_id)
        return {"events": app.state.store.auth_events(account_id)}

    @app.post("/api/accounts/{account_id}/auth/cancel", dependencies=[Depends(admin)])
    async def auth_cancel(account_id: str):
        get_account(account_id)
        app.state.auth.cancel(account_id)
        return {"cancelled": True}

    @app.post("/api/accounts/{account_id}/auth/refresh", dependencies=[Depends(admin)])
    async def auth_refresh(account_id: str):
        get_account(account_id)
        await app.state.auth.credentials(account_id, force=True)
        return app.state.auth.status(account_id)

    @app.api_route("/api/accounts/{account_id}/template", methods=["GET", "PUT"], dependencies=[Depends(admin)])
    @app.post("/api/accounts/{account_id}/har", dependencies=[Depends(admin)])
    async def removed_browser_auth(account_id: str):
        get_account(account_id)
        raise HTTPException(410, "网页请求模板已停用，请使用桌面原生授权")

    @app.post("/api/accounts/{account_id}/test", dependencies=[Depends(admin)])
    async def test_account(account_id: str):
        account = get_account(account_id)
        async with app.state.pool.lock:
            if app.state.pool.inflight[account_id]:
                raise HTTPException(409, "请等待运行中的请求结束")
            app.state.pool.inflight[account_id] += 1
        start = time.monotonic()
        status, message, chars, retry_after = 200, "", 0, 60
        output = []
        request_id = "test-" + uuid.uuid4().hex
        try:
            async for delta in app.state.upstream.stream(account, [{"role": "user", "content": "仅回复 OK"}]):
                chars += len(delta)
                output.append(delta)
            return {"ok": True, "output": "".join(output)[:2000], "duration_ms": round((time.monotonic() - start) * 1000), "output_chars": chars}
        except UpstreamError as exc:
            status, message, retry_after = exc.status, str(exc), exc.retry_after
            raise HTTPException(502, message) from exc
        except asyncio.CancelledError:
            status = 499
            raise
        finally:
            await app.state.pool.release(account_id, status, retry_after, message)
            app.state.store.log(request_id, account_id, status, round((time.monotonic() - start) * 1000), chars, message)

    @app.get("/api/logs", dependencies=[Depends(admin)])
    async def logs(account_id: str | None = None, status: str | None = None, model: str | None = None,
                   api_key_id: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0):
        bounded = max(1, min(limit, 200))
        safe_offset = max(0, offset)
        return app.state.store.logs_page(
            limit=bounded, offset=safe_offset,
            account_id=account_id or None, status=status or None,
            model=model or None, api_key_id=api_key_id or None, q=q or None,
        )

    @app.get("/v1/models", dependencies=[Depends(api_access)])
    async def models():
        return {"object": "list", "data": [{"id": name, "object": "model", "created": 0,
                    "owned_by": "amazon", "agent_mode": model_options(name)[0],
                    "thinking_effort": model_options(name)[1],
                    "thinking_support": 'desktop-normalizes-to-off' if model_options(name)[0] == 'fast' else 'optional'}
                    for name in MODEL_IDS]}

    @app.post("/v1/chat/completions", dependencies=[Depends(api_access)])
    async def completions(value: ChatRequest):
        request_id = "chatcmpl-" + uuid.uuid4().hex
        started = time.monotonic()
        created = int(time.time())
        try:
            prepared = Prepared(value)
        except CompatibilityError as exc:
            return JSONResponse({'error':{'message':str(exc),'type':'invalid_request_error'}},400)
        messages = [{'role':'user','content':prepared.prompt}]
        tried = set()
        last_error = None
        account = None
        iterator = None
        first = ""
        api_key_id, key_group = current_key_ctx.get()
        cache = getattr(app.state.upstream, 'session_cache', None)
        matched_entry, delta_offset = None, 0
        if cache is not None and prepared.context and len(prepared.context.get('messages') or []) > 1:
            eligible_ids = app.state.pool.eligible_ids(tried, group_name=key_group)
            match = cache.match(prepared.context, value.mode, value.effort, available_account_ids=eligible_ids)
            if match is not None:
                matched_entry, delta_offset = match
            else:
                cache.record_miss()
        stream_state: dict = {}
        max_attempts = max(1, min(5, int(app.state.store.get_settings().get("max_attempts", 3))))
        for attempt_idx in range(max_attempts):
            preferred = matched_entry.account_id if (attempt_idx == 0 and matched_entry is not None) else None
            try:
                account = await app.state.pool.acquire(tried, preferred_account_id=preferred, group_name=key_group)
            except PoolUnavailable:
                break
            tried.add(account["id"])
            use_reuse = matched_entry is not None and account["id"] == matched_entry.account_id
            stream_state = {}
            iterator = app.state.upstream.stream(
                account, messages, agent_mode=value.mode,
                thinking_effort=value.effort, images=prepared.images,
                context=prepared.context,
                reuse_thread_id=matched_entry.thread_id if use_reuse else None,
                delta_offset=delta_offset if use_reuse else 0,
                state_out=stream_state,
            )
            try:
                first = await anext(iterator)
                break
            except UpstreamError as exc:
                last_error = exc
                await iterator.aclose()
                await app.state.pool.release(account["id"], exc.status, exc.retry_after, str(exc),
                                             penalize=exc.retryable)
                app.state.store.log(uuid.uuid4().hex, account["id"], exc.status,
                                    round((time.monotonic() - started) * 1000), 0, str(exc),
                                    model=value.model, session_hit=bool(stream_state.get('reused')),
                                    api_key_id=api_key_id)
                if not exc.retryable:
                    break
            except BaseException:
                await iterator.aclose()
                await app.state.pool.release(account["id"], 499)
                raise
        if not first or account is None or iterator is None:
            message = str(last_error) if last_error else "没有可用账号：请先添加并授权账号，或等待冷却结束"
            if last_error and not last_error.retryable:
                kind = "invalid_request_error" if last_error.status < 500 else "upstream_error"
                return JSONResponse({"error": {"message": message, "type": kind}}, last_error.status)
            return JSONResponse({"error": {"message": message, "type": "upstream_unavailable",
                                           "code": "no_available_account"}}, 503, headers={"Retry-After": "10"})

        validated_result = []
        session_reused = bool(stream_state.get('reused'))
        session_thread_id = stream_state.get('thread_id')

        async def output():
            penalize = True
            status, error, chars, retry_after = 200, "", 0, 60
            buffered = [first] if prepared.buffered else None
            try:
                chars += len(first)
                yield first
                async for delta in iterator:
                    chars += len(delta)
                    if buffered is not None:
                        buffered.append(delta)
                    yield delta
                if buffered is not None:
                    validated_result.append(prepared.result(''.join(buffered)))
            except CompatibilityError as exc:
                penalize = False
                status, error = 502, str(exc)
                raise
            except UpstreamError as exc:
                status, error, retry_after = exc.status, str(exc), exc.retry_after
                penalize = exc.retryable
                raise
            except (asyncio.CancelledError, GeneratorExit):
                status = 499
                raise
            finally:
                await iterator.aclose()
                await app.state.pool.release(account["id"], status, retry_after, error, penalize=penalize)
                if status == 200 and cache is not None and session_thread_id and prepared.context:
                    cache.save(account["id"], session_thread_id, prepared.context, value.mode, value.effort)
                elif status != 200 and cache is not None and session_thread_id:
                    cache.invalidate(session_thread_id)
                app.state.store.log(request_id, account["id"], status,
                                    round((time.monotonic() - started) * 1000), chars, error,
                                    model=value.model, session_hit=session_reused,
                                    api_key_id=api_key_id)

        def chunk(delta, finish=None):
            return {"id": request_id, "object": "chat.completion.chunk", "created": created,
                    "model": value.model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        if prepared.buffered:
            try:
                text = ''.join([delta async for delta in output()])
                message, finish = validated_result[0]
            except (UpstreamError, CompatibilityError) as exc:
                status = exc.status if isinstance(exc, UpstreamError) else 502
                kind = 'invalid_request_error' if status < 500 else 'upstream_error'
                return JSONResponse({'error':{'message':str(exc),'type':kind}},status)
            if value.stream:
                async def buffered_sse():
                    delta = {k:v for k,v in message.items() if k != 'role'}
                    if 'tool_calls' in delta:
                        delta['tool_calls'] = [dict(call,index=i) for i,call in enumerate(delta['tool_calls'])]
                    for event in (chunk({'role':'assistant'}),chunk(delta),chunk({},finish)):
                        yield 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                    yield 'data: [DONE]\n\n'
                return StreamingResponse(buffered_sse(),media_type='text/event-stream',
                    headers={'X-Accel-Buffering':'no','X-Request-ID':request_id,'X-QuickPool-Adapter':'buffered-json'})
            return {'id':request_id,'object':'chat.completion','created':created,'model':value.model,
                    'choices':[{'index':0,'message':message,'finish_reason':finish}]}

        if value.stream:
            async def sse():
                def encode(event):
                    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                generator = output()
                try:
                    # Start the owning generator before the first network yield so
                    # disconnects immediately after the role frame still release the lease.
                    initial = await anext(generator)
                    yield encode(chunk({"role": "assistant"}))
                    yield encode(chunk({"content": initial}))
                    async for text in generator:
                        yield encode(chunk({"content": text}))
                    yield encode(chunk({}, "stop"))
                    yield "data: [DONE]\n\n"
                except UpstreamError as exc:
                    # Never switch account after sending text: avoid duplicate/cross-account responses.
                    yield encode({"error": {"message": str(exc), "type": "upstream_error", "code": exc.status}})
                finally:
                    await generator.aclose()
            return StreamingResponse(sse(), media_type="text/event-stream",
                                     headers={"X-Accel-Buffering": "no", "X-Request-ID": request_id})

        try:
            text = "".join([delta async for delta in output()])
        except UpstreamError as exc:
            return JSONResponse({"error": {"message": str(exc),
                "type": "invalid_request_error" if exc.status < 500 else "upstream_error"}}, exc.status)
        return {"id": request_id, "object": "chat.completion", "created": created, "model": value.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}]}

    register_admin_routes(app, admin)
    register_cli_routes(app, completions, api_access)

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static / "index.html")

    return app

