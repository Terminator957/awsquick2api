import asyncio
import csv
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Request

from quickpool import session_cache
from quickpool.admin_api import handle_batch_action, register_admin_routes
from quickpool.native_upstream import UpstreamError
from quickpool.pool import Pool, PoolUnavailable
from quickpool.session_cache import SessionCache
from tests.test_gateway import add

pytest_plugins = ['tests.test_gateway']


class SessionStore:
    def __init__(self):
        self.rows = {}

    def load_sessions(self, limit):
        return list(self.rows.values())[:limit]

    def save_session(self, thread_id, account_id, config_hash, msg_hashes, turns, ttl):
        self.rows[thread_id] = dict(thread_id=thread_id, account_id=account_id, config_hash=config_hash,
                                    msg_hashes=msg_hashes, turns=turns, remaining=ttl)

    def delete_session(self, thread_id):
        del self.rows[thread_id]


@pytest.mark.parametrize('persistent', [False, True])
def test_cache_listing_expiration_and_clear(monkeypatch, persistent):
    clock = SimpleNamespace(monotonic=Mock())
    monkeypatch.setattr(session_cache, 'time', clock)
    store = SessionStore() if persistent else None
    cache = SessionCache(store=store)
    for now, aid, tid in [(100, 'a', 'old'), (103, 'b', 'second'), (104, 'a', 'latest'), (105, 'a', 'latest')]:
        clock.monotonic.return_value = now
        cache.save(aid, tid, {}, 'smart', 'off')
    cache.set_ttl(10)
    assert cache.ttl == 10.0 and isinstance(cache.ttl, float)
    clock.monotonic.return_value = 110.25
    config_hash = session_cache.hash_config('smart', 'off', None, None, None, None)[:8]
    assert cache.list_sessions({'a': 'Alpha'}) == [
        dict(thread_id='latest', account_id='a', account_name='Alpha', turns=2,
             remaining_sec=4.8, config_hash=config_hash),
        dict(thread_id='second', account_id='b', account_name='', turns=1,
             remaining_sec=2.8, config_hash=config_hash),
    ]
    if store is not None:
        assert set(store.rows) == {'second', 'latest'}
    assert cache.clear() == 2
    assert cache.clear() == 0 and cache.list_sessions() == []
    if store is not None:
        assert store.rows == {}


async def test_pool_live_strategies_and_group_filter():
    common = dict(enabled=True, configured=True, state='ready', cooldown_until=0, max_concurrency=2)
    accounts = [
        dict(common, id='weighted', weight=4, last_used=2, group_name='red'),
        dict(common, id='quota', weight=2, last_used=3, group_name='blue'),
        dict(common, id='oldest', weight=1, last_used=1),
    ]
    store = SimpleNamespace(accounts=lambda: accounts, patch_account=Mock())
    used = {'weighted': 80, 'quota': 10, 'oldest': 40}
    quotas = SimpleNamespace(cached=lambda aid: {'status': 'available', 'session': {'usedPercentage': used[aid]}})
    pool = Pool(store, quotas)
    assert (await pool.acquire(set()))['id'] == 'weighted'
    pool.inflight['weighted'] = 0
    settings = {}
    store.get_settings = lambda: settings
    for strategy, expected in [('weighted', 'weighted'), ('quota_first', 'quota'),
                               ('round_robin', 'oldest'), ('unknown', 'weighted')]:
        settings['scheduling_strategy'] = strategy
        assert (await pool.acquire(set()))['id'] == expected
        assert pool.inflight[expected] == 1
        assert 'last_used' in store.patch_account.call_args.args[1]
        pool.inflight[expected] = 0

    assert pool.eligible_ids() == {'weighted', 'quota', 'oldest'}
    assert pool.eligible_ids(group_name='blue') == {'quota'}
    assert pool.eligible_ids({'quota'}, group_name='blue') == set()
    assert pool.eligible_ids(group_name='missing') == set()
    assert (await pool.acquire(set(), group_name='blue', preferred_account_id='weighted'))['id'] == 'quota'
    assert (await pool.acquire(set(), preferred_account_id='oldest'))['id'] == 'oldest'
    with pytest.raises(PoolUnavailable):
        await pool.acquire({'quota'}, group_name='blue')


@pytest.fixture
def admin_gateway(gateway):
    _, app, _, _, _ = gateway

    def admin_dep(request: Request):
        if request.headers.get('authorization') != 'Bearer ' + request.app.state.store.admin_key:
            raise HTTPException(401)

    register_admin_routes(app, admin_dep)
    return gateway


def test_admin_routes_require_admin(admin_gateway):
    client, _, _, key, _ = admin_gateway
    for method, path in [
        ('GET', 'keys'), ('POST', 'keys'), ('PATCH', 'keys/k'), ('DELETE', 'keys/k'),
        ('GET', 'settings'), ('PATCH', 'settings'), ('GET', 'analytics'), ('GET', 'sessions'),
        ('DELETE', 'sessions'), ('DELETE', 'sessions/t'), ('GET', 'logs/export'),
    ]:
        assert client.request(method, '/api/' + path, json={}).status_code == 401
    assert client.get('/api/keys', headers=key).status_code == 401


def test_api_key_crud(admin_gateway, monkeypatch):
    client, app, admin, _, _ = admin_gateway
    store = app.state.store
    keys = {}

    def create(**values):
        keys['k1'] = dict(values, id='k1', enabled=True)
        return dict(keys['k1'], plaintext_key='sk-test')

    def update(key_id, values):
        if key_id in keys:
            keys[key_id].update(values)
            return keys[key_id]

    monkeypatch.setattr(store, 'list_api_keys', lambda: list(keys.values()), raising=False)
    monkeypatch.setattr(store, 'create_api_key', create, raising=False)
    monkeypatch.setattr(store, 'update_api_key', update, raising=False)
    monkeypatch.setattr(store, 'delete_api_key', lambda key_id: keys.pop(key_id, None), raising=False)

    path = '/api/keys'
    response = client.post(path, headers=admin, json={'name': 'Build'})
    key = dict(id='k1', name='Build', notes='', group_name='', daily_limit=0, expires_at=None, enabled=True)
    assert response.status_code == 201 and response.json() == dict(key, plaintext_key='sk-test')
    assert client.get(path, headers=admin).json() == {'keys': [key]}
    response = client.patch(path + '/k1', headers=admin, json={'enabled': False, 'name': None, 'daily_limit': 15})
    assert response.status_code == 200 and response.json() == dict(key, enabled=False, daily_limit=15)
    for body in [{'name': ''}, {'name': 'x' * 101}, {'name': 'x', 'notes': 'x' * 501},
                 {'name': 'x', 'group_name': 'x' * 101}, {'name': 'x', 'daily_limit': -1},
                 {'name': 'x', 'enabled': True}]:
        assert client.post(path, headers=admin, json=body).status_code == 422
    assert client.patch(path + '/k1', headers=admin, json={'unknown': 1}).status_code == 422
    assert client.patch(path + '/missing', headers=admin, json={}).status_code == 404
    response = client.delete(path + '/k1', headers=admin)
    assert response.status_code == 204 and not response.content
    assert client.delete(path + '/k1', headers=admin).status_code == 404
    assert client.get(path, headers=admin).json() == {'keys': []}


def test_settings_and_analytics(admin_gateway, monkeypatch):
    client, app, admin, _, _ = admin_gateway
    store = app.state.store
    settings = {'scheduling_strategy': 'weighted', 'session_cache_ttl': 1800, 'max_attempts': 3}

    def update(values):
        settings.update(values)
        return settings

    monkeypatch.setattr(store, 'get_settings', lambda: settings, raising=False)
    monkeypatch.setattr(store, 'update_settings', update, raising=False)
    monkeypatch.setattr(store, 'analytics', lambda *, hours: {'hours': hours}, raising=False)
    path = '/api/settings'
    assert client.get(path, headers=admin).json() == settings
    response = client.patch(path, headers=admin, json={'session_cache_ttl': 75, 'scheduling_strategy': 'quota_first',
                                                     'max_attempts': None})
    assert response.status_code == 200
    assert response.json() == {'scheduling_strategy': 'quota_first', 'session_cache_ttl': 75, 'max_attempts': 3}
    assert app.state.upstream.session_cache.ttl == 75.0
    for body in [{'session_cache_ttl': 59}, {'session_cache_ttl': 86401}, {'max_attempts': 0},
                 {'max_attempts': 6}, {'scheduling_strategy': 'unknown'}, {'unknown': 1}]:
        assert client.patch(path, headers=admin, json=body).status_code == 422
    path = '/api/analytics'
    assert client.get(path, headers=admin).json() == {'hours': 24}
    assert client.get(path, headers=admin, params={'hours': 168}).json() == {'hours': 168}
    for hours in (0, 169):
        assert client.get(path, headers=admin, params={'hours': hours}).status_code == 422


def test_sessions_routes(admin_gateway):
    client, app, admin, _, _ = admin_gateway
    account = add(admin_gateway)
    cache = app.state.upstream.session_cache
    cache.save(account['id'], 't1', {}, 'smart', 'off')
    cache.save(account['id'], 't2', {}, 'smart', 'off')
    path = '/api/sessions'
    data = client.get(path, headers=admin).json()
    assert data['stats']['active_sessions'] == 2
    assert [(s['thread_id'], s['account_name']) for s in data['sessions']] == [('t2', account['name']), ('t1', account['name'])]
    assert client.delete(path + '/t1', headers=admin).json() == {'deleted': True}
    assert client.delete(path, headers=admin).json() == {'cleared': 1}
    assert client.get(path, headers=admin).json()['sessions'] == []
    assert app.state.store.load_sessions() == []


def test_csv_export(admin_gateway, monkeypatch):
    client, app, admin, _, _ = admin_gateway
    store = app.state.store
    account = add(admin_gateway)
    store.log('good', account['id'], 200, 13, 2)
    store.log('bad', account['id'], 502, 7, 0, '=SUM(1,2)')
    path = '/api/logs/export'
    response = client.get(path, headers=admin, params={'account_id': account['id'], 'status': 'error', 'model': ''})
    assert response.status_code == 200
    assert response.headers['content-type'] == 'text/csv; charset=utf-8'
    assert response.headers['content-disposition'] == 'attachment; filename=quickpool-logs.csv'
    reader = csv.DictReader(io.StringIO(response.text))
    assert reader.fieldnames == ['created_at', 'id', 'account_id', 'account_name', 'api_key_id', 'model', 'status',
                                 'duration_ms', 'output_chars', 'session_hit', 'error']
    row, = reader
    assert row['id'] == 'bad' and row['error'] == "'=SUM(1,2)" and row['api_key_id'] == ''

    logs = Mock(return_value={'requests': [{'id': '+id', 'model': '-model', 'account_name': '@name'}]})
    monkeypatch.setattr(store, 'logs', logs)
    response = client.get(path, headers=admin, params={'status': '', 'model': 'm', 'api_key_id': 'k', 'q': 'text'})
    logs.assert_called_once_with(limit=5000, model='m', api_key_id='k', q='text')
    row, = csv.DictReader(io.StringIO(response.text))
    assert (row['id'], row['model'], row['account_name']) == ("'+id", "'-model", "'@name")


async def test_batch_mutations(gateway, monkeypatch):
    _, app, _, _, _ = gateway
    aid = add(gateway, state='cooldown')['id']
    store = app.state.store
    store.patch_account(aid, {'cooldown_until': 9999999999, 'last_error': 'retry'})
    cache = app.state.upstream.session_cache
    cache.save(aid, 'thread', {}, 'smart', 'off')
    ids = [aid, 'missing', aid]
    success = {'results': [{'account_id': aid, 'ok': True}]}
    assert await handle_batch_action(app, 'set_weight', ids, 5) == success
    assert await handle_batch_action(app, 'set_concurrency', ids, 3) == success
    assert await handle_batch_action(app, 'disable', ids) == success
    assert not store.account(aid)['enabled'] and not cache.list_sessions()
    assert await handle_batch_action(app, 'enable', ids) == success
    assert await handle_batch_action(app, 'clear_cooldown', ids) == success
    account = store.account(aid)
    assert (account['weight'], account['max_concurrency'], account['enabled']) == (5, 3, True)
    assert (account['state'], account['cooldown_until'], account['last_error']) == ('ready', 0, '')

    refresh = AsyncMock(return_value={'status': 'available'})
    monkeypatch.setattr(app.state.quotas, 'refresh', refresh)
    assert await handle_batch_action(app, 'refresh_quota', ids) == {
        'results': [{'account_id': aid, 'ok': True, 'detail': {'status': 'available'}}],
    }
    refresh.assert_awaited_once_with(aid)
    for action, status in [('set_weight', 422), ('set_concurrency', 422), ('unknown', 400)]:
        with pytest.raises(HTTPException) as exc:
            await handle_batch_action(app, action, ids)
        assert exc.value.status_code == status


async def test_batch_test_limits_streams_and_releases_accounts(gateway, monkeypatch):
    _, app, _, _, _ = gateway
    ids = [add(gateway, tenant, state='ready')['id'] for tenant in 'abcde']
    busy, failing = ids[-2:]
    pool = app.state.pool
    pool.inflight[busy] = 1
    peak = active = 0

    async def stream(account, messages):
        nonlocal peak, active
        assert messages == [{'role': 'user', 'content': '仅回复 OK'}]
        assert pool.inflight[account['id']] == 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        if account['id'] == failing:
            raise UpstreamError(429, 'rate limited', 120)
        yield 'OK'

    monkeypatch.setattr(app.state.upstream, 'stream', stream)
    assert await handle_batch_action(app, 'test', ids + [ids[0], 'missing']) == {'results': [
        *[{'account_id': aid, 'ok': True} for aid in ids[:3]],
        {'account_id': busy, 'ok': False, 'detail': '账号仍有运行中的请求'},
        {'account_id': failing, 'ok': False, 'detail': 'rate limited'},
    ]}
    assert peak == 3
    assert [pool.inflight[aid] for aid in ids] == [0, 0, 0, 1, 0]
    logs = {row['account_id']: row for row in app.state.store.logs()}
    assert logs.keys() == set(ids) - {busy}
    assert (logs[ids[0]]['status'], logs[ids[0]]['output_chars']) == (200, 2)
    assert (logs[failing]['status'], logs[failing]['error']) == (429, 'rate limited')
    assert all(row['id'].startswith('test-') for row in logs.values())


def test_real_app_sub2api_e2e(tmp_path):
    from fastapi.testclient import TestClient
    from quickpool.main import create_app

    app = create_app(data_dir=tmp_path)
    with TestClient(app) as client:
        admin = {'Authorization': f'Bearer {app.state.store.admin_key}'}
        # Create managed API key
        created = client.post('/api/keys', headers=admin, json={
            'name': 'claude-cli', 'notes': 'dev laptop', 'group_name': 'prod', 'daily_limit': 2,
        })
        assert created.status_code == 201
        key_data = created.json()
        plaintext = key_data['plaintext_key']
        key_id = key_data['id']
        assert plaintext.startswith('sk-qp-') and 'key_hash' not in key_data

        # Managed key works on /v1/models
        r1 = client.get('/v1/models', headers={'Authorization': f'Bearer {plaintext}'})
        assert r1.status_code == 200

        # Disable managed key -> 401
        p1 = client.patch(f'/api/keys/{key_id}', headers=admin, json={'enabled': False})
        assert p1.status_code == 200 and p1.json()['enabled'] is False
        r2 = client.get('/v1/models', headers={'Authorization': f'Bearer {plaintext}'})
        assert r2.status_code == 401

        # Re-enable + settings + analytics + sessions + logs export
        assert client.patch(f'/api/keys/{key_id}', headers=admin, json={'enabled': True}).status_code == 200
        s = client.patch('/api/settings', headers=admin, json={
            'scheduling_strategy': 'quota_first', 'session_cache_ttl': 900, 'max_attempts': 2,
        })
        assert s.status_code == 200 and s.json()['scheduling_strategy'] == 'quota_first'
        assert app.state.upstream.session_cache.ttl == 900

        an = client.get('/api/analytics?hours=24', headers=admin)
        assert an.status_code == 200 and len(an.json()['hourly']) == 24

        sess = client.get('/api/sessions', headers=admin)
        assert sess.status_code == 200 and 'sessions' in sess.json()

        exp = client.get('/api/logs/export', headers=admin)
        assert exp.status_code == 200 and 'text/csv' in exp.headers['content-type']

        assert client.delete(f'/api/keys/{key_id}', headers=admin).status_code == 204
        assert client.delete(f'/api/keys/{key_id}', headers=admin).status_code == 404
