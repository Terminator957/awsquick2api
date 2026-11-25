"""Tests for request log thinking_effort field, configurable limits, and Claude-only client restriction."""
from tests.test_gateway import add

pytest_plugins = ['tests.test_gateway']


def test_request_logs_record_thinking_effort_and_claude_only_client_restriction(gateway):
    client, _app, admin, key, _backend = gateway
    add(gateway, state='ready')

    # 1. Verify thinking_effort recorded in logs & CSV export
    r = client.post('/v1/chat/completions', headers=key, json={
        'model': 'claude-opus-5-5', 'reasoning_effort': 'medium',
        'messages': [{'role': 'user', 'content': 'hi'}],
    })
    assert r.status_code == 200
    logs = client.get('/api/logs', headers=admin).json()['requests']
    assert logs[0]['thinking_effort'] == 'medium'
    csv_resp = client.get('/api/logs/export', headers=admin)
    assert csv_resp.status_code == 200 and 'thinking_effort' in csv_resp.text.splitlines()[0]

    # 2. Enable claude_only restriction via admin settings
    p = client.patch('/api/settings', headers=admin, json={'client_restriction': 'claude_only'})
    assert p.status_code == 200 and p.json()['client_restriction'] == 'claude_only'

    # Generic curl / python client rejected with 403
    r_curl = client.post('/v1/chat/completions', headers={**key, 'User-Agent': 'curl/8.5.0'}, json={
        'model': 'claude-opus-5-5', 'messages': [{'role': 'user', 'content': 'hi'}],
    })
    assert r_curl.status_code == 403

    # Claude Code CLI allowed
    r_cli = client.post('/v1/messages', headers={**key, 'User-Agent': 'claude-cli/2.1.322'}, json={
        'model': 'claude-opus-5-5', 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'hi'}],
    })
    assert r_cli.status_code == 200

    # Claude Desktop (anthropic-js + anthropic-version) allowed
    r_desk = client.post('/v1/messages', headers={
        **key, 'User-Agent': 'Anthropic/JS 0.50.0 claude-desktop/1.2.0',
        'anthropic-version': '2023-06-01', 'x-stainless-lang': 'js',
    }, json={
        'model': 'claude-opus-5-5', 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'hi'}],
    })
    assert r_desk.status_code == 200

    # Built-in Admin Playground allowed with X-QuickPool-Admin header
    admin_token = admin['Authorization'].removeprefix('Bearer ')
    r_play = client.post('/v1/chat/completions', headers={
        **key, 'User-Agent': 'Mozilla/5.0', 'X-QuickPool-Admin': admin_token,
    }, json={'model': 'claude-opus-5-5', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r_play.status_code == 200
