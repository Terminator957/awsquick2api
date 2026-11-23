import sqlite3

import pytest
from pydantic import ValidationError

from quickpool.api_keys import KeyAuthError, resolve_user_policy, verify_managed_key
from quickpool.models import ApiKeyCreate, UserCreate, UserPatch
from quickpool.quota import summarize_pool_quota
from quickpool.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    yield value
    value.close()


def test_migration(tmp_path):
    db = sqlite3.connect(tmp_path / 'quickpool.db')
    db.executescript('''
        CREATE TABLE api_keys (id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
            key_prefix TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', group_name TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, daily_limit INTEGER NOT NULL DEFAULT 0,
            expires_at REAL NOT NULL DEFAULT 0, requests_total INTEGER NOT NULL DEFAULT 0,
            last_used REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        CREATE TABLE requests (id TEXT PRIMARY KEY, created_at REAL NOT NULL, account_id TEXT,
            model TEXT NOT NULL, status INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
            output_chars INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '');
    ''')
    db.close()
    value = Store(tmp_path)
    for table, names in [('users', {'id', 'name', 'allowed_models', 'daily_token_limit'}),
                         ('api_keys', {'user_id'}),
                         ('requests', {'input_tokens', 'output_tokens', 'user_id'})]:
        assert names <= {row[1] for row in value.db.execute(f'PRAGMA table_info({table})')}
    value.close()


@pytest.mark.parametrize('field,value', [
    ('name', ''), ('name', 'x' * 81), ('notes', 'x' * 301), ('group_name', 'x' * 41),
    ('allowed_models', ['x'] * 33), ('daily_request_limit', -1), ('daily_token_limit', 1000000001),
])
def test_user_model_bounds(field, value):
    with pytest.raises(ValidationError):
        UserCreate(**({'name': 'valid'} | {field: value}))


def test_user_crud_and_key_unbind(store):
    user = store.create_user(UserCreate(name='Alice', group_name='team', allowed_models=['amazon-smart']))
    key = store.create_api_key(ApiKeyCreate(name='key', user_id=user['id']))
    assert store.list_users()[0]['key_count'] == 1
    assert store.get_api_key(key['id'])['user_name'] == 'Alice'
    assert store.update_user(user['id'], UserPatch(notes='new', enabled=False).model_dump(exclude_unset=True))['enabled'] is False
    assert store.get_user(user['id'])['notes'] == 'new'
    assert store.delete_user(user['id']) is True
    assert store.delete_user(user['id']) is False
    assert store.get_api_key(key['id'])['user_id'] == ''


def test_user_daily_counts_and_tokens(store, monkeypatch):
    midnight = 20000 * 86400
    monkeypatch.setattr('quickpool.store.time.time', lambda: midnight + 60)
    user = store.create_user(name='Alice')
    uid = user['id']
    store.log('old', 'a', 200, 1, 0, input_tokens=10, output_tokens=5, user_id=uid)
    store.log('new', 'a', 200, 1, 0, input_tokens=20, output_tokens=7, user_id=uid)
    with store.db:
        store.db.execute('UPDATE requests SET created_at=? WHERE id=?', (midnight - 1, 'old'))
    assert {k: store.get_user(uid)[k] for k in ('requests_today', 'requests_total', 'tokens_today', 'tokens_total')} == {
        'requests_today': 1, 'requests_total': 2, 'tokens_today': 27, 'tokens_total': 42}


@pytest.mark.parametrize('field,limit,tokens,status', [
    ('enabled', False, 0, 401), ('daily_request_limit', 1, 0, 429),
    ('daily_token_limit', 3, 3, 429),
])
def test_user_auth_limits(store, field, limit, tokens, status):
    user = store.create_user(name='Alice')
    key = store.create_api_key(name='key', user_id=user['id'])
    store.patch_user(user['id'], {field: limit})
    store.log('one', 'a', 200, 1, 0, input_tokens=tokens, user_id=user['id'])
    with pytest.raises(KeyAuthError) as exc:
        verify_managed_key(store, key['plaintext_key'])
    assert exc.value.status == status


@pytest.mark.parametrize('policy,expected', [
    ({'smart_default_effort': 'off'}, 'off'),
    ({'max_thinking_effort': 'low'}, 'low'),
    ({'smart_default_effort': 'off', 'max_thinking_effort': 'low'}, 'off'),
])
def test_smart_policy(policy, expected):
    assert resolve_user_policy('amazon-smart', 'smart', 'off', False, 'high', policy) == expected


def test_allowed_models_rejects():
    with pytest.raises(KeyAuthError) as exc:
        resolve_user_policy('amazon-fast', 'fast', 'off', False, 'high', {'allowed_models': ['amazon-smart']})
    assert exc.value.status == 403


def test_quota_observed_and_heuristic(store):
    accounts = [{'id': 'a', 'quota': {'monthly': {'availableUnits': 2, 'provisionedUnits': 4}}},
                {'id': 'b', 'quota': {'monthly': {'availableUnits': 3, 'provisionedUnits': 3}}}]
    fallback = summarize_pool_quota(accounts, store)
    assert fallback['pool_quota']['estimate_basis'] == 'heuristic'
    assert fallback['pool_quota']['estimated_remaining_tokens'] == 75000
    store.log('one', 'a', 200, 1, 0, input_tokens=100, output_tokens=50)
    result = summarize_pool_quota(accounts, store)
    assert result['pool_quota']['total_used_units'] == 2
    assert result['pool_quota']['tokens_per_unit'] == 75
    assert result['pool_quota']['estimated_remaining_tokens'] == 375
    assert result['account_tokens']['a']['input_tokens_total'] == 100
    assert result['account_tokens']['b']['tokens_total'] == 0
