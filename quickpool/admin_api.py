from __future__ import annotations

import asyncio
import csv
import io
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from .models import UserCreate, UserPatch
from .native_upstream import UpstreamError


class APIKeyCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    notes: str = Field(default='', max_length=500)
    group_name: str = Field(default='', max_length=100)
    daily_limit: int = Field(default=0, ge=0)
    expires_at: float | None = None
    user_id: str = Field(default='', max_length=64)


class APIKeyPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = None
    notes: str | None = None
    group_name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = None
    expires_at: float | None = None
    user_id: str | None = Field(default=None, max_length=64)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scheduling_strategy: Literal['weighted', 'quota_first', 'round_robin'] | None = None
    session_cache_ttl: int | None = Field(default=None, ge=60, le=86400)
    max_attempts: int | None = Field(default=None, ge=1, le=5)
    max_images: int | None = Field(default=None, ge=1, le=500)
    max_image_mb: int | None = Field(default=None, ge=1, le=20)
    max_messages: int | None = Field(default=None, ge=10, le=10000)
    max_tools: int | None = Field(default=None, ge=1, le=1000)
    max_body_mb: int | None = Field(default=None, ge=1, le=100)
    stream_timeout_sec: int | None = Field(default=None, ge=60, le=900)
    log_retention: int | None = Field(default=None, ge=1000, le=100000)
    smart_default_effort: Literal['off', 'low', 'medium', 'high'] | None = None
    client_restriction: Literal['off', 'claude_only'] | None = None


def register_admin_routes(app, admin):
    router = APIRouter(prefix='/api', dependencies=[Depends(admin)])

    @router.get('/users')
    async def list_users():
        return {'users': app.state.store.list_users()}

    @router.post('/users', status_code=201)
    async def create_user(body: UserCreate):
        return app.state.store.create_user(body)

    @router.patch('/users/{user_id}')
    async def update_user(user_id: str, body: UserPatch):
        user = app.state.store.patch_user(user_id, body.model_dump(exclude_none=True))
        if user is None:
            raise HTTPException(404, '用户不存在')
        return user

    @router.delete('/users/{user_id}', status_code=204)
    async def delete_user(user_id: str):
        if not app.state.store.delete_user(user_id):
            raise HTTPException(404, '用户不存在')

    @router.get('/keys')
    async def list_keys():
        return {'keys': app.state.store.list_api_keys()}

    @router.post('/keys', status_code=201)
    async def create_key(body: APIKeyCreate):
        return app.state.store.create_api_key(**body.model_dump())

    @router.patch('/keys/{key_id}')
    async def update_key(key_id: str, body: APIKeyPatch):
        key = app.state.store.update_api_key(key_id, body.model_dump(exclude_none=True))
        if key is None:
            raise HTTPException(404, 'API 密钥不存在')
        return key

    @router.delete('/keys/{key_id}', status_code=204)
    async def delete_key(key_id: str):
        if not app.state.store.delete_api_key(key_id):
            raise HTTPException(404, 'API 密钥不存在')

    @router.get('/settings')
    async def get_settings():
        return app.state.store.get_settings()

    @router.patch('/settings')
    async def update_settings(body: SettingsPatch):
        values = body.model_dump(exclude_none=True)
        result = app.state.store.update_settings(values)
        if 'session_cache_ttl' in values:
            app.state.upstream.session_cache.set_ttl(values['session_cache_ttl'])
        return result

    @router.get('/analytics')
    async def analytics(hours: int = Query(default=24, ge=1, le=720)):
        return app.state.store.analytics(hours=hours)

    @router.get('/sessions')
    async def list_sessions():
        cache = app.state.upstream.session_cache
        names = {a['id']: a['name'] for a in app.state.store.accounts()}
        return {'sessions': cache.list_sessions(names), 'stats': cache.stats()}

    @router.delete('/sessions')
    async def clear_sessions():
        return {'cleared': app.state.upstream.session_cache.clear()}

    @router.delete('/sessions/{thread_id}')
    async def delete_session(thread_id: str):
        app.state.upstream.session_cache.invalidate(thread_id)
        return {'deleted': True}

    @router.get('/logs/export')
    async def export_logs(account_id: str = '', status: str = '', model: str = '',
                          api_key_id: str = '', q: str = '', hours: int = 0):
        filters = dict(account_id=account_id, status=status, model=model, api_key_id=api_key_id, q=q)
        kwargs = {key: value for key, value in filters.items() if value}
        if hours > 0:
            kwargs['hours'] = min(hours, 720)
        logs = app.state.store.logs(limit=5000, **kwargs)
        rows = logs['requests'] if isinstance(logs, dict) else logs
        columns = ('created_at', 'id', 'account_id', 'account_name', 'api_key_id', 'model', 'thinking_effort', 'status',
                   'duration_ms', 'output_chars', 'session_hit', 'error')
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in rows:
            cells = (row.get(key, '') for key in columns)
            writer.writerow("'" + cell if isinstance(cell, str) and cell.startswith(('=', '+', '-', '@'))
                            else cell for cell in cells)
        return Response(output.getvalue(), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename=quickpool-logs.csv'})

    app.include_router(router)


async def handle_batch_action(app, action, account_ids, value=None):
    if action not in ('enable', 'disable', 'clear_cooldown', 'refresh_quota',
                      'set_weight', 'set_concurrency', 'test'):
        raise HTTPException(400, '未知批量操作')
    if action in ('set_weight', 'set_concurrency') and value is None:
        raise HTTPException(422, '缺少设置值')

    store = app.state.store
    accounts = [account for aid in dict.fromkeys(account_ids) if (account := store.account(aid))]
    if action == 'test':
        sem = asyncio.Semaphore(3)
        pool, upstream = app.state.pool, app.state.upstream

        async def test_one(account):
            aid = account['id']
            async with sem:
                async with pool.lock:
                    if pool.inflight[aid]:
                        return {'account_id': aid, 'ok': False, 'detail': '账号仍有运行中的请求'}
                    pool.inflight[aid] += 1
                start = time.monotonic()
                status, message, chars, retry_after = 200, '', 0, 60
                request_id = 'test-' + uuid.uuid4().hex
                try:
                    async for delta in upstream.stream(account, [{'role': 'user', 'content': '仅回复 OK'}]):
                        chars += len(delta)
                    return {'account_id': aid, 'ok': True}
                except UpstreamError as exc:
                    status, message, retry_after = exc.status, str(exc), exc.retry_after
                    return {'account_id': aid, 'ok': False, 'detail': message}
                except asyncio.CancelledError:
                    status = 499
                    raise
                finally:
                    await pool.release(aid, status, retry_after, message)
                    store.log(request_id, aid, status, round((time.monotonic() - start) * 1000), chars, message)

        return {'results': await asyncio.gather(*(test_one(account) for account in accounts))}

    results = []
    for account in accounts:
        aid = account['id']
        result = {'account_id': aid, 'ok': True}
        if action in ('enable', 'disable'):
            store.patch_account(aid, {'enabled': action == 'enable'})
            if action == 'disable':
                app.state.upstream.session_cache.invalidate_account(aid)
        elif action == 'clear_cooldown':
            if account['state'] == 'cooldown' or account['cooldown_until'] > 0 or account['last_error']:
                patch = {'cooldown_until': 0, 'last_error': ''}
                if account['state'] == 'cooldown':
                    patch['state'] = 'ready'
                store.patch_account(aid, patch)
        elif action == 'refresh_quota':
            result['detail'] = await app.state.quotas.refresh(aid)
        else:
            field = 'weight' if action == 'set_weight' else 'max_concurrency'
            store.patch_account(aid, {field: value})
        results.append(result)
    return {'results': results}
