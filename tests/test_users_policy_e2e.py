"""User administration and managed-key policy through the HTTP gateway."""

import pytest

from quickpool.cli_api import estimate_tokens
from tests.test_gateway import add

pytest_plugins = ['tests.test_gateway']


def test_user_routes(gateway):
    client, _, admin, key, _ = gateway
    assert client.get('/api/users', headers=key).status_code == 401
    created = client.post('/api/users', headers=admin, json={'name': 'Alice'})
    assert created.status_code == 201
    user_id = created.json()['id']
    assert client.get('/api/users', headers=admin).json()['users'][0]['id'] == user_id
    patched = client.patch(f'/api/users/{user_id}', headers=admin,
                           json={'smart_default_effort': 'off'})
    assert patched.status_code == 200
    assert patched.json()['smart_default_effort'] == 'off'
    assert client.patch('/api/users/missing', headers=admin, json={'name': 'Nobody'}).status_code == 404
    assert client.delete(f'/api/users/{user_id}', headers=admin).status_code == 204
    assert client.delete(f'/api/users/{user_id}', headers=admin).status_code == 404


def test_api_key_user_binding(gateway):
    client, _, admin, _, _ = gateway
    user_id = client.post('/api/users', headers=admin, json={'name': 'Owner'}).json()['id']
    created = client.post('/api/keys', headers=admin, json={'name': 'Bound', 'user_id': user_id})
    assert created.status_code == 201
    assert created.json()['user_id'] == user_id
    key_id = created.json()['id']
    patched = client.patch(f'/api/keys/{key_id}', headers=admin, json={'user_id': ''})
    assert patched.status_code == 200
    assert patched.json()['user_id'] == ''


def test_user_smart_default_overrides_global(gateway):
    client, app, admin, _, backend = gateway
    add(gateway, state='ready')
    assert app.state.store.get_settings()['smart_default_effort'] == 'high'
    user_id = client.post('/api/users', headers=admin, json={
        'name': 'A', 'smart_default_effort': 'off'}).json()['id']
    token = client.post('/api/keys', headers=admin, json={
        'name': 'A key', 'user_id': user_id}).json()['plaintext_key']
    response = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + token},
                           json={'model': 'amazon-smart', 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == 200
    sent = backend.connections[-1]['socket'].sent
    message = next(item['payload'] for item in sent if item['type'] == 'user_message')
    assert message['thinking_effort'] == 'off'
    log = app.state.store.logs(limit=1)[0]
    assert log['user_id'] == user_id
    assert log['input_tokens'] > estimate_tokens('hello')
    assert log['output_tokens'] == (len('你好，Quick') + 3) // 4


def test_user_effort_cap_applies_to_explicit_model(gateway):
    client, _, admin, _, backend = gateway
    add(gateway, state='ready')
    user_id = client.post('/api/users', headers=admin, json={
        'name': 'B', 'max_thinking_effort': 'low'}).json()['id']
    token = client.post('/api/keys', headers=admin, json={
        'name': 'B key', 'user_id': user_id}).json()['plaintext_key']
    response = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + token},
                           json={'model': 'amazon-smart-high', 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == 200
    sent = backend.connections[-1]['socket'].sent
    message = next(item['payload'] for item in sent if item['type'] == 'user_message')
    assert message['thinking_effort'] == 'low'


@pytest.mark.parametrize('protocol', ['responses', 'messages'])
def test_cli_protocols_propagate_user_policy(gateway, protocol):
    client, _, admin, _, backend = gateway
    add(gateway, state='ready')
    user_id = client.post('/api/users', headers=admin, json={
        'name': protocol, 'smart_default_effort': 'off'}).json()['id']
    token = client.post('/api/keys', headers=admin, json={
        'name': protocol + ' key', 'user_id': user_id}).json()['plaintext_key']
    body = {'model': 'amazon-smart', 'input': 'hello', 'store': False} if protocol == 'responses' else {
        'model': 'amazon-smart', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 1024}
    response = client.post('/v1/' + protocol, headers={'Authorization': 'Bearer ' + token}, json=body)
    assert response.status_code == 200
    sent = backend.connections[-1]['socket'].sent
    message = next(item['payload'] for item in sent if item['type'] == 'user_message')
    assert message['thinking_effort'] == 'off'


def test_user_allowed_models_rejects_smart(gateway):
    client, _, admin, _, backend = gateway
    add(gateway, state='ready')
    user_id = client.post('/api/users', headers=admin, json={
        'name': 'C', 'allowed_models': ['fast', 'balanced']}).json()['id']
    token = client.post('/api/keys', headers=admin, json={
        'name': 'C key', 'user_id': user_id}).json()['plaintext_key']
    response = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + token},
                           json={'model': 'amazon-smart', 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == 403
    assert response.json()['error']['type'] == 'permission_error'
    assert not backend.connections


def test_user_daily_request_and_token_limits(gateway):
    client, _, admin, _, _ = gateway
    add(gateway, state='ready')
    for name, limits in [('Requests', {'daily_request_limit': 1}),
                         ('Tokens', {'daily_token_limit': 1})]:
        user_id = client.post('/api/users', headers=admin, json={'name': name, **limits}).json()['id']
        token = client.post('/api/keys', headers=admin, json={
            'name': name + ' key', 'user_id': user_id}).json()['plaintext_key']
        headers = {'Authorization': 'Bearer ' + token}
        body = {'model': 'amazon-fast', 'messages': [{'role': 'user', 'content': 'hello'}]}
        assert client.post('/v1/chat/completions', headers=headers, json=body).status_code == 200
        assert client.post('/v1/chat/completions', headers=headers, json=body).status_code == 429


def test_overview_pool_and_account_tokens(gateway):
    client, app, admin, key, _ = gateway
    account = add(gateway, state='ready')
    assert client.post('/v1/chat/completions', headers=key, json={
        'model': 'amazon-fast', 'messages': [{'role': 'user', 'content': 'hello'}]}).status_code == 200
    overview = client.get('/api/overview', headers=admin)
    assert overview.status_code == 200
    data = overview.json()
    assert data['pool_quota']['tokens_total'] > 0
    token_stats = next(item['token_stats'] for item in data['accounts'] if item['id'] == account['id'])
    assert token_stats['tokens_total'] == data['pool_quota']['tokens_total']
    assert token_stats['input_tokens_total'] == app.state.store.logs(limit=1)[0]['input_tokens']
