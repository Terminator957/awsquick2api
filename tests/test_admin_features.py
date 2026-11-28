"""Tests for quota-aware pool scheduling, account detail, batch operations, log filtering, and session reuse wiring."""
import time

import pytest

from quickpool.pool import PoolUnavailable
from quickpool.quota import is_quota_exhausted, remaining_score
from tests.test_gateway import add

pytest_plugins = ['tests.test_gateway']


def test_quota_helpers():
    assert not is_quota_exhausted(None)
    assert not is_quota_exhausted({'status': 'unavailable'})
    assert not is_quota_exhausted({'status': 'available', 'session': {'usedPercentage': 50.0}})
    assert is_quota_exhausted({'status': 'available', 'session': {'usedPercentage': 99.5}})
    assert is_quota_exhausted({'status': 'available', 'monthly': {'availableUnits': 0}})
    assert remaining_score({'status': 'unavailable'}) == 50.0
    assert remaining_score({'status': 'available', 'session': {'usedPercentage': 20.0}}) == 80.0
    assert remaining_score({'status': 'available', 'monthly': {'usedPercentage': 70.0}}) == 30.0


def test_pool_skips_exhausted_and_prefers_higher_remaining_quota(gateway):
    client, app, _, key, backend = gateway
    low = add(gateway, 'low-quota', state='ready')
    full = add(gateway, 'full-quota', state='ready')
    high = add(gateway, 'high-quota', state='ready')
    app.state.quotas.cache[low['id']] = {'status': 'available', 'session': {'usedPercentage': 80.0}}
    app.state.quotas.cache[full['id']] = {'status': 'available', 'session': {'usedPercentage': 100.0}}
    app.state.quotas.cache[high['id']] = {'status': 'available', 'session': {'usedPercentage': 10.0}}

    resp = client.post('/v1/chat/completions', headers=key, json={
        'model': 'amazon-fast', 'messages': [{'role': 'user', 'content': 'hello'}],
    })
    assert resp.status_code == 200
    logs = app.state.store.logs(limit=5)
    assert logs[0]['account_id'] == high['id']

    # Mark all remaining accounts exhausted -> 503
    app.state.quotas.cache[low['id']] = {'status': 'available', 'session': {'usedPercentage': 100.0}}
    app.state.quotas.cache[high['id']] = {'status': 'available', 'session': {'usedPercentage': 99.8}}
    resp2 = client.post('/v1/chat/completions', headers=key, json={
        'model': 'amazon-fast', 'messages': [{'role': 'user', 'content': 'hello again'}],
    })
    assert resp2.status_code == 503


def test_account_detail_batch_ops_and_log_filters(gateway):
    client, app, admin, key, _ = gateway
    a1 = add(gateway, 'acc-1', state='ready')
    a2 = add(gateway, 'acc-2', state='cooldown')
    app.state.store.patch_account(a2['id'], {'cooldown_until': time.time() + 600, 'last_error': 'temp 502'})

    # Run one successful request on a1
    r = client.post('/v1/chat/completions', headers=key, json={
        'model': 'amazon-smart', 'messages': [{'role': 'user', 'content': 'ping'}],
    })
    assert r.status_code == 200

    # Overview exposes cache_stats and quota_exhausted
    ov = client.get('/api/overview', headers=admin)
    assert ov.status_code == 200
    data = ov.json()
    assert 'cache_stats' in data
    assert all('quota_exhausted' in acc for acc in data['accounts'])

    # Detail endpoint
    detail = client.get(f"/api/accounts/{a1['id']}/detail", headers=admin)
    assert detail.status_code == 200
    dd = detail.json()
    assert dd['account']['id'] == a1['id']
    assert dd['stats']['total'] == 1 and dd['stats']['success'] == 1
    assert len(dd['recent_logs']) == 1

    # Batch disable & enable
    b1 = client.post('/api/accounts/batch', headers=admin, json={
        'action': 'disable', 'account_ids': [a1['id'], a2['id']],
    })
    assert b1.status_code == 200 and b1.json()['affected'] == 2
    assert not app.state.store.account(a1['id'])['enabled']

    b2 = client.post('/api/accounts/batch', headers=admin, json={
        'action': 'enable', 'account_ids': [a1['id'], a2['id']],
    })
    assert b2.status_code == 200 and b2.json()['affected'] == 2
    assert app.state.store.account(a1['id'])['enabled']

    # Batch clear cooldown
    b3 = client.post('/api/accounts/batch', headers=admin, json={
        'action': 'clear_cooldown', 'account_ids': [a2['id']],
    })
    assert b3.status_code == 200 and b3.json()['affected'] == 1
    a2_now = app.state.store.account(a2['id'])
    assert a2_now['state'] == 'ready' and a2_now['cooldown_until'] == 0 and a2_now['last_error'] == ''

    # Log filters
    app.state.store.log('err-1', a2['id'], 502, 1200, 0, 'bad gateway', model='amazon-fast')
    ok_logs = client.get('/api/logs?status=ok', headers=admin).json()['requests']
    assert all(item['status'] == 200 for item in ok_logs)
    err_logs = client.get('/api/logs?status=error', headers=admin).json()['requests']
    assert len(err_logs) == 1 and err_logs[0]['id'] == 'err-1'
    a1_logs = client.get(f"/api/logs?account_id={a1['id']}", headers=admin).json()['requests']
    assert all(item['account_id'] == a1['id'] for item in a1_logs)


def test_end_to_end_session_reuse_affinity_and_cache_stats(gateway):
    client, app, admin, key, backend = gateway
    client.patch('/api/settings', headers=admin, json={'incognito': False})
    a1 = add(gateway, 'acc-1', state='ready')
    a2 = add(gateway, 'acc-2', state='ready')

    # Turn 1
    r1 = client.post('/v1/chat/completions', headers=key, json={
        'model': 'amazon-smart',
        'messages': [{'role': 'user', 'content': 'Turn 1 question'}],
    })
    assert r1.status_code == 200
    reply1 = r1.json()['choices'][0]['message']['content']
    first_conn = backend.connections[-1]
    thread1 = next(m['payload']['thread_id'] for m in first_conn['socket'].sent if m['type'] == 'user_message')

    # Turn 2: includes Turn 1 user + assistant + new user -> should reuse thread1 on same account without session_create
    r2 = client.post('/v1/chat/completions', headers=key, json={
        'model': 'amazon-smart',
        'messages': [
            {'role': 'user', 'content': 'Turn 1 question'},
            {'role': 'assistant', 'content': reply1},
            {'role': 'user', 'content': 'Turn 2 follow-up'},
        ],
    })
    assert r2.status_code == 200
    second_conn = backend.connections[-1]
    sent_types = [m['type'] for m in second_conn['socket'].sent]
    assert 'session_create' not in sent_types
    user_msg = next(m['payload'] for m in second_conn['socket'].sent if m['type'] == 'user_message')
    assert user_msg['thread_id'] == thread1
    assert 'Turn 2 follow-up' in user_msg['text']
    assert 'Turn 1 question' not in user_msg['text']

    # Verify cache_stats and log session_hit
    ov = client.get('/api/overview', headers=admin).json()
    assert ov['cache_stats']['hits'] >= 1
    assert ov['cache_stats']['saved_tokens'] > 0
    logs = client.get('/api/logs', headers=admin).json()['requests']
    assert logs[0]['session_hit'] is True
    assert logs[1]['session_hit'] is False
