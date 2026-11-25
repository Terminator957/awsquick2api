import asyncio
import json
import time
from collections import deque
from contextlib import asynccontextmanager

import cbor2
import httpx
import pytest
from fastapi.testclient import TestClient

from quickpool.main import create_app
from quickpool.models import AccountCreate
from quickpool.native_protocol import EventStreamDecoder, encode_eventstream
from quickpool.pool import Pool, PoolUnavailable
from quickpool.store import Store


def credentials(tenant="a"):
    return {
        "_kind": "desktop-oauth", "provider": "gateway", "clientId": "fixture",
        "tenantUrl": f"https://{tenant}.quick.aws.com",
        "access_token": f"PRIVATE_ACCESS_{tenant}", "refresh_token": f"PRIVATE_REFRESH_{tenant}",
        "expires_at": time.time() + 3600, "user_info": {},
    }


def output_frame(event):
    return encode_eventstream(
        {":event-type": "rawOutputEvent", ":content-type": "application/cbor", ":message-type": "event"},
        cbor2.dumps({"payload": json.dumps(event, ensure_ascii=False).encode()}),
    )


class NativeBackend:
    """Fixture discovery and desktop WS traffic without opening network sockets."""

    def __init__(self):
        self.behaviors = {}
        self.http_calls = []
        self.connections = []

    async def handle_http(self, request):
        assert request.method == "GET" and request.url.path == "/v1/discovery"
        tenant = request.url.host.split(".")[0]
        assert request.url.host == f"{tenant}.quick.aws.com"
        assert request.headers["authorization"] == f"Bearer PRIVATE_ACCESS_{tenant}"
        assert request.headers["x-amzn-qs-internal-trace-id"]
        self.http_calls.append(tenant)
        status = self.behaviors.get(tenant, {}).get("status", 200)
        return httpx.Response(status, json={"cellHost": "cell.quick.aws.com"},
                              headers={"Retry-After": "120"})

    def connect(self, url, **kwargs):
        # Match websockets.connect: regular function returning an async context manager.
        return self._connection(url, kwargs)

    @asynccontextmanager
    async def _connection(self, url, kwargs):
        assert url == ("wss://cell.quick.aws.com/ws/service/SpaceNeedleConversationService/"
                       "operation/ChatBidirectionalStream")
        headers = kwargs["additional_headers"]
        assert headers["X-Surface-Type"] == "QUICK_WORK_DESKTOP_WINDOWS"
        assert headers["X-Agent-Service-Type"] == "UNIFIED_QUICK"
        assert headers["X-Outpost-Id"] and headers["X-Working-Session-Id"]
        assert kwargs["proxy"] is None
        tenant = headers["Authorization"].removeprefix("Bearer PRIVATE_ACCESS_")
        socket = NativeSocket(self.behaviors.get(tenant, {}))
        record = {"tenant": tenant, "socket": socket, "closed": False}
        self.connections.append(record)
        try:
            yield socket
        finally:
            record["closed"] = True


class NativeSocket:
    def __init__(self, behavior):
        self.behavior = behavior
        self.sent = []
        self.frames = deque()
        self.decoder = EventStreamDecoder()

    async def send(self, data):
        assert isinstance(data, bytes)
        for frame in self.decoder.feed(data):
            assert frame.headers[":event-type"] == "rawInputEvent"
            message = json.loads(cbor2.loads(frame.payload)["payload"])
            self.sent.append(message)
            if message["type"] == "user_message":
                self.prepare_response(message["payload"]["thread_id"])

    def prepare_response(self, thread_id):
        def event(kind, payload):
            return {"type": kind, "thread_id": thread_id, "message_id": "fixture-reply",
                    "payload": payload}

        kind = self.behavior.get("kind", "delta")
        if kind == "late-error":
            events = [event("text_delta", {"delta": "start"}),
                      event("error", {"error_code": "InternalError", "message": "PRIVATE_UPSTREAM_BODY"})]
        elif kind == "snapshot":
            events = [event("text_complete", {"text": "你"}),
                      event("text_complete", {"text": "你好"}), event("message_complete", {})]
        elif kind == "empty":
            events = [event("message_complete", {})]
        elif kind == "throttled":
            events = [event("error", {"error_code": "ThrottlingException",
                                       "message": "PRIVATE_UPSTREAM_BODY"})]
        else:
            reply = self.behavior.get('reply', '你好，Quick')
            events = [event("text_delta", {"delta": reply[:2]}),
                      event("text_delta", {"delta": reply[2:]}), event("message_complete", {})]
        frames = [output_frame(item) for item in events]
        if kind == "fragmented":
            joined = b"".join(frames)
            self.frames.extend([joined[:7], joined[7:41], joined[41:]])
        else:
            self.frames.extend(frames)

    async def recv(self):
        assert self.frames, "Fixture response exhausted before native completion"
        return self.frames.popleft()


@pytest.fixture
def gateway(tmp_path):
    backend = NativeBackend()
    app = create_app(tmp_path, transport=httpx.MockTransport(backend.handle_http),
                     ws_connect=backend.connect)
    with TestClient(app) as client:
        admin = {"Authorization": "Bearer " + app.state.store.admin_key}
        key = {"Authorization": "Bearer " + app.state.store.api_key}
        yield client, app, admin, key, backend


def add(gateway, tenant="a", *, state="unverified", behavior=None):
    client, app, admin, _, backend = gateway
    response = client.post("/api/accounts", headers=admin, json={"name": tenant})
    assert response.status_code == 201, response.text
    account = response.json()
    app.state.store.set_credentials(account["id"], credentials(tenant), state=state)
    backend.behaviors[tenant] = behavior or {}
    return app.state.store.account(account["id"])


def request(client, key, stream=False, **extra):
    return client.post("/v1/chat/completions", headers=key, json={
        "model": "amazon-fast", "messages": [{"role": "user", "content": "测试输入"}],
        "stream": stream, **extra,
    })


def test_auth_and_key_separation(gateway):
    client, _, admin, key, backend = gateway
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/config", headers=key).status_code == 401
    assert client.get("/v1/models", headers=admin).status_code == 401
    assert client.get("/v1/models", headers=key).json()["data"][0]["id"] == "claude-opus-5-5"
    assert request(client, key).status_code == 503
    assert not backend.http_calls and not backend.connections


def test_account_validation(gateway):
    client, _, admin, _, backend = gateway
    for body in ({"name": "bad", "login_url": "http://localhost"},
                 {"name": "bad", "max_concurrency": 0}, {"name": "bad", "provider": "cookie"}):
        assert client.post("/api/accounts", headers=admin, json=body).status_code == 422
    assert client.get("/api/overview", headers=admin).json()["accounts"] == []
    assert not backend.http_calls


def test_unverified_requires_successful_test(gateway):
    client, app, admin, key, backend = gateway
    account = add(gateway)
    assert request(client, key).status_code == 503
    assert not backend.http_calls
    response = client.post(f'/api/accounts/{account["id"]}/test', headers=admin)
    assert response.status_code == 200 and response.json()["ok"]
    assert response.json()["output"] == "你好，Quick"
    assert app.state.store.account(account["id"])["state"] == "ready"
    response = request(client, key)
    assert response.status_code == 200
    assert response.json()["object"] == "chat.completion"
    assert response.json()["choices"][0]["message"] == {"role": "assistant", "content": "你好，Quick"}
    sent = backend.connections[-1]["socket"].sent
    user = next(item["payload"] for item in sent if item["type"] == "user_message")
    assert user["text"] == "测试输入"
    assert user["agent_mode"] == "fast" and user["thinking_effort"] == "off"
    assert user["thread_id"] and user["message_id"]
    assert app.state.pool.inflight[account["id"]] == 0
    assert all(connection["closed"] for connection in backend.connections)


@pytest.mark.parametrize("behavior,expected", [({"status": 401}, "cooldown"),
                                              ({"kind": "empty"}, "cooldown")])
def test_failed_validation_never_enters_pool(gateway, behavior, expected):
    client, app, admin, key, backend = gateway
    account = add(gateway, behavior=behavior)
    response = client.post(f'/api/accounts/{account["id"]}/test', headers=admin)
    assert response.status_code == 502
    assert app.state.store.account(account["id"])["state"] == expected
    attempts = len(backend.http_calls)
    assert request(client, key).status_code == 503
    assert len(backend.http_calls) == attempts
    assert app.state.pool.inflight[account["id"]] == 0


@pytest.mark.parametrize("kind,answer", [("delta", "你好，Quick"), ("snapshot", "你好"),
                                         ("fragmented", "你好，Quick")])
def test_openai_streaming(gateway, kind, answer):
    client, app, _, key, backend = gateway
    account = add(gateway, state="ready", behavior={"kind": kind})
    response = request(client, key, True)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    assert "".join(event["choices"][0]["delta"].get("content", "") for event in events) == answer
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert response.text.endswith("data: [DONE]\n\n")
    assert all(event["object"] == "chat.completion.chunk" for event in events)
    assert len({event["id"] for event in events}) == 1
    assert app.state.pool.inflight[account["id"]] == 0
    assert backend.connections[0]["closed"]


@pytest.mark.parametrize("status,expected", [(401, "cooldown"), (403, "cooldown"),
                                             (429, "cooldown"), (503, "cooldown")])
def test_failover_and_quarantine(gateway, status, expected):
    client, app, _, key, backend = gateway
    first = add(gateway, "a", state="ready", behavior={"status": status})
    second = add(gateway, "b", state="ready")
    response = request(client, key)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "你好，Quick"
    assert backend.http_calls == ["a", "b"]
    assert [connection["tenant"] for connection in backend.connections] == ["b"]
    failed = app.state.store.account(first["id"])
    assert failed["state"] == expected
    if status == 429:
        assert failed["cooldown_until"] > time.time() + 50
    assert app.state.pool.inflight[first["id"]] == app.state.pool.inflight[second["id"]] == 0


def test_ws_throttling_fails_over_before_output(gateway):
    client, app, _, key, backend = gateway
    first = add(gateway, "a", state="ready", behavior={"kind": "throttled"})
    add(gateway, "b", state="ready")
    assert request(client, key).status_code == 200
    assert [connection["tenant"] for connection in backend.connections] == ["a", "b"]
    assert app.state.store.account(first["id"])["state"] == "cooldown"
    assert all(connection["closed"] for connection in backend.connections)


@pytest.mark.parametrize("stream", [False, True])
def test_no_failover_after_first_output(gateway, stream):
    client, app, _, key, backend = gateway
    first = add(gateway, "a", state="ready", behavior={"kind": "late-error"})
    add(gateway, "b", state="ready")
    response = request(client, key, stream)
    assert response.status_code == (200 if stream else 502)
    assert "upstream_error" in response.text
    if stream:
        assert "start" in response.text and "[DONE]" not in response.text
    assert "PRIVATE_UPSTREAM_BODY" not in response.text
    assert backend.http_calls == ["a"]
    assert [connection["tenant"] for connection in backend.connections] == ["a"]
    assert app.state.pool.inflight[first["id"]] == 0
    assert backend.connections[0]["closed"]


def test_encrypt_secrets_and_redact_logs(gateway):
    client, app, admin, key, _ = gateway
    account = add(gateway, state="ready", behavior={"kind": "late-error"})
    row = app.state.store.db.execute("SELECT secret FROM accounts WHERE id=?", (account["id"],)).fetchone()
    assert b"PRIVATE_ACCESS_a" not in row["secret"]
    assert b"PRIVATE_REFRESH_a" not in row["secret"]
    assert request(client, key, True).status_code == 200
    listing = client.get("/api/overview", headers=admin).text
    logs = client.get("/api/logs", headers=admin).text
    status = client.get(f'/api/accounts/{account["id"]}/auth/status', headers=admin).text
    for secret in ("PRIVATE_ACCESS_a", "PRIVATE_REFRESH_a", "PRIVATE_UPSTREAM_BODY", "测试输入"):
        assert secret not in listing + logs + status
    records = json.loads(logs)["requests"]
    assert len(records) == 1 and records[0]["status"] == 502
    assert records[0]["output_chars"] == len("start")
    assert client.get(f'/api/accounts/{account["id"]}/template').status_code == 401


def test_unsupported_and_validation_redaction(gateway):
    client, _, admin, key, backend = gateway
    assert request(client, key, tools=[]).status_code == 503
    assert request(client, key, model="PRIVATE_MODEL_INPUT").status_code == 422
    response = client.post("/api/accounts", headers=admin,
                           json={"name": "bad", "login_url": "https://evil.test/PRIVATE_URL_INPUT"})
    assert response.status_code == 422 and "PRIVATE_URL_INPUT" not in response.text
    assert not backend.http_calls


@pytest.mark.parametrize("method,suffix", [("GET", "template"), ("PUT", "template"), ("POST", "har")])
def test_removed_browser_template_routes_are_gone(gateway, method, suffix):
    client, app, admin, _, backend = gateway
    account = add(gateway)
    old = app.state.store.credentials(account["id"])
    response = client.request(method, f'/api/accounts/{account["id"]}/{suffix}', headers=admin,
                              json={"Cookie": "PRIVATE_LEGACY_COOKIE", "url": "https://evil.test/"})
    assert response.status_code == 410
    assert "PRIVATE_LEGACY_COOKIE" not in response.text
    assert app.state.store.credentials(account["id"]) == old
    assert app.state.store.account(account["id"])["state"] == "unverified"
    assert not backend.http_calls


async def test_pool_concurrency_disabled_and_cooldown(tmp_path):
    store = Store(tmp_path)
    pool = Pool(store)
    account = store.create_account(AccountCreate(name="test"))
    account_id = account["id"]
    store.set_credentials(account_id, credentials(), state="ready")
    try:
        lease = await pool.acquire(set())
        assert lease["id"] == account_id
        with pytest.raises(PoolUnavailable):
            await pool.acquire(set())
        await pool.release(account_id, 429, 120, "rate limited")
        assert store.account(account_id)["cooldown_until"] > time.time() + 110
        with pytest.raises(PoolUnavailable):
            await pool.acquire(set())
        store.patch_account(account_id, {"cooldown_until": time.time() - 1})
        assert (await pool.acquire(set()))["id"] == account_id
        await pool.release(account_id)
        store.patch_account(account_id, {"enabled": False})
        with pytest.raises(PoolUnavailable):
            await pool.acquire(set())
    finally:
        store.close()


async def test_pool_parallel_acquire_is_atomic(tmp_path):
    store = Store(tmp_path)
    pool = Pool(store)
    account = store.create_account(AccountCreate(name="only", max_concurrency=1))
    store.set_credentials(account["id"], credentials(), state="ready")
    try:
        results = await asyncio.gather(*(pool.acquire(set()) for _ in range(12)), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, PoolUnavailable) for result in results) == 11
        assert pool.inflight[account["id"]] == 1
        await pool.release(account["id"])
        assert pool.inflight[account["id"]] == 0
    finally:
        store.close()
