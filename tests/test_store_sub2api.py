import hashlib
import re
import sqlite3

import pytest
from pydantic import ValidationError

from quickpool.api_keys import KeyAuthError, generate_api_key, hash_api_key, verify_managed_key
from quickpool.models import (
    AccountCreate, AccountPatch, ApiKeyCreate, ApiKeyPatch, BatchAccountAction, SettingsPatch,
)
from quickpool.store import Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path)
    yield store
    store.close()


@pytest.mark.parametrize("model,required", [
    (AccountCreate, {"name": "account"}), (AccountPatch, {}),
    (ApiKeyCreate, {"name": "key"}), (ApiKeyPatch, {}),
])
def test_group_and_notes_bounds(model, required):
    for field, limit in (("group_name", 40), ("notes", 300)):
        assert getattr(model(**required), field) == ("" if model in (AccountCreate, ApiKeyCreate) else None)
        for value in ("", "x" * limit):
            assert getattr(model(**(required | {field: value})), field) == value
        with pytest.raises(ValidationError, match=field):
            model(**(required | {field: "x" * (limit + 1)}))


@pytest.mark.parametrize("model", [ApiKeyCreate, ApiKeyPatch])
def test_key_model_bounds(model):
    for name in ("x", "x" * 80):
        assert model(name=name).name == name
    for name in ("", "x" * 81):
        with pytest.raises(ValidationError, match="name"):
            model(name=name)
    for limit in (0, 1000000):
        assert model(name="key", daily_limit=limit).daily_limit == limit
    for limit in (-1, 1000001):
        with pytest.raises(ValidationError, match="daily_limit"):
            model(name="key", daily_limit=limit)
    for expiry in (None, 0, 1.5):
        assert model(name="key", expires_at=expiry).expires_at == expiry

    assert ApiKeyCreate(name="key").daily_limit == 0
    assert ApiKeyCreate(name="key").expires_at is None
    assert ApiKeyPatch().model_dump() == dict.fromkeys(
        ("name", "notes", "group_name", "enabled", "daily_limit", "expires_at", "user_id"))
    assert ApiKeyPatch(enabled=False).enabled is False


@pytest.mark.parametrize("model,required", [
    (AccountCreate, {"name": "account"}), (AccountPatch, {}),
    (BatchAccountAction, {"action": "enable", "account_ids": ["a"]}),
    (ApiKeyCreate, {"name": "key"}), (ApiKeyPatch, {}), (SettingsPatch, {}),
])
def test_models_reject_extra_fields(model, required):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model(**required, unexpected=True)


@pytest.mark.parametrize("action", [
    "enable", "disable", "clear_cooldown", "refresh_quota", "test", "set_weight", "set_concurrency",
])
def test_batch_actions(action):
    assert BatchAccountAction(action=action, account_ids=["a"]).action == action


def test_batch_value_bounds():
    for value in (None, 1, 100):
        assert BatchAccountAction(action="set_weight", account_ids=["a"], value=value).value == value
    assert BatchAccountAction(action="enable", account_ids=["a"]).value is None
    for value in (0, 101):
        with pytest.raises(ValidationError, match="value"):
            BatchAccountAction(action="set_weight", account_ids=["a"], value=value)
    with pytest.raises(ValidationError, match="action"):
        BatchAccountAction(action="unknown", account_ids=["a"])


def test_settings_model_bounds():
    assert SettingsPatch().model_dump() == dict.fromkeys(
        ("scheduling_strategy", "session_cache_ttl", "max_attempts", "max_images", "max_image_mb",
         "max_messages", "max_tools", "max_body_mb", "stream_timeout_sec", "log_retention",
         "smart_default_effort", "client_restriction"))
    for strategy in ("weighted", "quota_first", "round_robin"):
        assert SettingsPatch(scheduling_strategy=strategy).scheduling_strategy == strategy
    with pytest.raises(ValidationError, match="scheduling_strategy"):
        SettingsPatch(scheduling_strategy="unknown")
    for field, lower, upper in (("session_cache_ttl", 60, 86400), ("max_attempts", 1, 5)):
        for value in (lower, upper):
            assert getattr(SettingsPatch(**{field: value}), field) == value
        for value in (lower - 1, upper + 1):
            with pytest.raises(ValidationError, match=field):
                SettingsPatch(**{field: value})


def test_generate_and_hash_api_key():
    plaintext, key_hash, prefix = generate_api_key()
    assert re.fullmatch(r"sk-qp-[A-Za-z0-9_-]{43}", plaintext)
    assert key_hash == hash_api_key(plaintext) == hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    assert hash_api_key("密钥") == hashlib.sha256("密钥".encode("utf-8")).hexdigest()
    assert prefix == plaintext[:10] + "…" + plaintext[-4:]


def test_api_key_crud(store, monkeypatch):
    now = 1700000000
    monkeypatch.setattr("quickpool.store.time.time", lambda: now)
    created = store.create_api_key(ApiKeyCreate(
        name="key", notes="note", group_name="group", daily_limit=20, expires_at=now + 60))
    key_id = created["id"]
    plaintext = created["plaintext_key"]
    key_hash = hash_api_key(plaintext)
    metadata = {
        "id": key_id, "name": "key", "key_prefix": plaintext[:10] + "…" + plaintext[-4:],
        "notes": "note", "group_name": "group", "enabled": True, "daily_limit": 20,
        "expires_at": now + 60, "requests_total": 0, "requests_today": 0, "last_used": 0, "created_at": now,
        "user_id": "", "user_name": None,
    }
    assert created == metadata | {"plaintext_key": plaintext}
    assert created["enabled"] is True
    assert store.db.execute("SELECT key_hash FROM api_keys WHERE id=?", (key_id,)).fetchone()[0] == key_hash

    now += 1
    second = store.create_api_key(ApiKeyCreate(name="second"))
    assert second["expires_at"] == 0
    assert store.list_api_keys() == [metadata, store.get_api_key(second["id"])]
    assert set(store.list_api_keys()[1]) == set(metadata)
    assert store.get_api_key(key_id) == store.get_api_key_by_hash(key_hash) == metadata

    changes = {"name": "renamed", "notes": "", "group_name": "other", "enabled": False,
               "daily_limit": 0, "expires_at": None}
    patched = store.patch_api_key(key_id, ApiKeyPatch(**changes).model_dump(exclude_unset=True))
    assert patched == metadata | changes | {"expires_at": 0}
    assert patched["enabled"] is False
    assert store.patch_api_key(key_id, {"key_hash": "not allowed"}) == patched

    store.record_api_key_use(key_id)
    now += 5
    store.record_api_key_use(key_id)
    assert store.get_api_key(key_id) == patched | {"requests_total": 2, "last_used": now}

    store.delete_api_key(key_id)
    assert store.get_api_key(key_id) is None
    assert store.get_api_key_by_hash(key_hash) is None
    assert store.patch_api_key(key_id, {"name": "absent"}) is None
    assert [row["id"] for row in store.list_api_keys()] == [second["id"]]


def test_verify_managed_key(store, monkeypatch):
    now = 1700000000
    monkeypatch.setattr("quickpool.store.time.time", lambda: now)
    created = store.create_api_key(ApiKeyCreate(name="key", daily_limit=1))
    key_id, token = created["id"], created["plaintext_key"]
    assert verify_managed_key(store, "unknown") is None
    assert verify_managed_key(store, token) == store.get_api_key(key_id)

    for patch, status, message in [
        ({"enabled": False}, 401, "停用"),
        ({"enabled": True, "expires_at": now}, 401, "过期"),
        ({"expires_at": now + 1}, 429, "上限"),
    ]:
        if status == 429:
            store.log("counted", None, 503, 12, 0, api_key_id=key_id)
        store.patch_api_key(key_id, patch)
        with pytest.raises(KeyAuthError, match=message) as exc:
            verify_managed_key(store, token)
        assert exc.value.status == status
        assert exc.value.message == str(exc.value)

    store.patch_api_key(key_id, {"daily_limit": 0})
    assert verify_managed_key(store, token) == store.get_api_key(key_id)


def test_requests_today_uses_utc_midnight_and_key_id(store, monkeypatch):
    midnight = 20000 * 86400
    now = midnight + 60
    monkeypatch.setattr("quickpool.store.time.time", lambda: now)
    first, second = [store.create_api_key(ApiKeyCreate(name=name)) for name in ("first", "second")]
    for request_id, key in [("before", first), ("at", first), ("after", first), ("other", second)]:
        store.log(request_id, None, 500, 1, 0, api_key_id=key["id"])
    store.log("main", None, 200, 1, 0)
    with store.db:
        store.db.execute("UPDATE requests SET created_at=? WHERE id='before'", (midnight - 0.5,))
        store.db.execute("UPDATE requests SET created_at=? WHERE id='at'", (midnight,))

    assert {key["id"]: key["requests_today"] for key in store.list_api_keys()} == {first["id"]: 2, second["id"]: 1}
    assert store.get_api_key(first["id"])["requests_today"] == 2
    assert store.get_api_key_by_hash(hash_api_key(first["plaintext_key"]))["requests_today"] == 2
    now = midnight + 86400
    assert [key["requests_today"] for key in store.list_api_keys()] == [0, 0]


def test_settings_defaults_and_patch(store):
    defaults = {
        "scheduling_strategy": "weighted", "session_cache_ttl": 1800, "max_attempts": 3,
        "max_images": 50, "max_image_mb": 5, "max_messages": 2000, "max_tools": 256,
        "max_body_mb": 20, "stream_timeout_sec": 300, "log_retention": 10000,
        "smart_default_effort": "high", "client_restriction": "off",
        "auto_ensure_coding_agent": True, "custom_agent_prompt": "", "incognito": True,
    }
    assert store.get_settings() == defaults
    updated = store.patch_settings({
        "scheduling_strategy": "quota_first", "session_cache_ttl": 60, "max_attempts": None, "unknown": 1,
    })
    assert updated == defaults | {"scheduling_strategy": "quota_first", "session_cache_ttl": 60}
    assert dict(store.db.execute("SELECT key,value FROM settings")) == {
        "scheduling_strategy": '"quota_first"', "session_cache_ttl": "60",
    }
    patch = {"scheduling_strategy": "round_robin", "max_attempts": 5}
    assert store.patch_settings(patch | {"session_cache_ttl": None}) == updated | patch


def test_account_group_and_notes(store):
    account = store.create_account(AccountCreate(name="grouped", group_name="  team  ", notes=" note "))
    assert (account["group_name"], account["notes"]) == ("team", " note ")
    changes = AccountPatch(group_name="", notes="new note").model_dump(exclude_none=True)
    account = store.patch_account(account["id"], changes)
    assert (account["group_name"], account["notes"]) == ("", "new note")
    assert store.account(account["id"]) == account


def test_store_migrates_old_database(tmp_path):
    db = sqlite3.connect(tmp_path / "quickpool.db")
    db.executescript("""
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, login_url TEXT NOT NULL,
            aws_account_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            weight INTEGER NOT NULL DEFAULT 1, max_concurrency INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'unconfigured', cooldown_until REAL NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', last_used REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, secret BLOB
        );
        CREATE TABLE requests (
            id TEXT PRIMARY KEY, created_at REAL NOT NULL, account_id TEXT,
            model TEXT NOT NULL, status INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
            output_chars INTEGER NOT NULL, error TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO accounts(id,name,login_url,aws_account_id,created_at,weight)
        VALUES ('old-account','Existing','https://quick.aws.com/sn','',1000,3);
        INSERT INTO requests VALUES ('old-request',2000,'old-account','amazon-fast',502,123,0,'old error');
    """)
    db.close()

    store = Store(tmp_path)
    try:
        assert {row[1] for row in store.db.execute("PRAGMA table_info(accounts)")} >= {"group_name", "notes"}
        request_columns = {row[1] for row in store.db.execute("PRAGMA table_info(requests)")}
        assert request_columns >= {"api_key_id", "session_hit"}
        assert [row[2] for row in store.db.execute("PRAGMA index_info(requests_api_key_created)")] == [
            "api_key_id", "created_at",
        ]
        account = store.account("old-account")
        assert (account["name"], account["weight"]) == ("Existing", 3)
        assert (account["group_name"], account["notes"]) == ("", "")
        assert store.logs() == [{
            "id": "old-request", "created_at": 2000, "account_id": "old-account", "model": "amazon-fast",
            "status": 502, "duration_ms": 123, "output_chars": 0, "error": "old error", "session_hit": False,
            "api_key_id": "", "input_tokens": 0, "output_tokens": 0, "user_id": "", "thinking_effort": "",
            "account_name": "Existing", "api_key_name": None,
        }]
    finally:
        store.close()


def test_logs_filters_and_pagination(store, monkeypatch):
    now = 1700000000
    monkeypatch.setattr("quickpool.store.time.time", lambda: now)
    account = store.create_account(AccountCreate(name="account"))
    key = store.create_api_key(ApiKeyCreate(name="managed"))
    aid, kid = account["id"], key["id"]
    store.log("match-id", aid, 502, 10, 0, model="amazon-fast", api_key_id=kid)
    now += 1
    store.log("error-id", aid, 429, 20, 0, "prefix match suffix", model="amazon-fast", api_key_id=kid)
    now += 1
    store.log("other-model", aid, 200, 30, 1, model="amazon-fast-high", session_hit=True, api_key_id=kid)
    now += 1
    store.log("main-id", None, 200, 40, 1, model="amazon-fast")

    filter_cases = [
        ({"account_id": aid}, ["other-model", "error-id", "match-id"]),
        ({"status": "ok"}, ["main-id", "other-model"]),
        ({"status": "error"}, ["error-id", "match-id"]),
        ({"status": "502"}, ["match-id"]),
        ({"status": "unknown"}, ["main-id", "other-model", "error-id", "match-id"]),
        ({"model": "amazon-fast"}, ["main-id", "error-id", "match-id"]),
        ({"api_key_id": kid}, ["other-model", "error-id", "match-id"]),
        ({"api_key_id": ""}, ["main-id"]),
        ({"q": "match"}, ["error-id", "match-id"]),
        ({"q": "absent"}, []),
    ]
    for filters, expected in filter_cases:
        assert [row["id"] for row in store.logs(**filters)] == expected

    main, managed = store.logs(limit=2)
    assert (main["account_name"], main["api_key_name"], main["session_hit"]) == (None, None, False)
    assert (managed["account_name"], managed["api_key_name"]) == ("account", "managed")
    assert managed["session_hit"] is True
    assert store.logs(limit=1, offset=1) == [managed]

    filters = {"account_id": aid, "status": "error", "model": "amazon-fast", "api_key_id": kid, "q": "match"}
    page = store.logs_page(limit=1, offset=1, **filters)
    assert page == {"requests": store.logs(limit=1, offset=1, **filters), "total": 2, "limit": 1, "offset": 1}
    assert [row["id"] for row in page["requests"]] == ["match-id"]
    assert store.logs_page(offset=2, **filters) == {"requests": [], "total": 2, "limit": 50, "offset": 2}

    store.delete_api_key(kid)
    assert store.logs(api_key_id=kid, limit=1)[0]["api_key_name"] is None


def test_analytics_fills_hours_and_aggregates_requests(store, monkeypatch):
    current_hour = 500000 * 3600
    start = current_hour - 3 * 3600
    monkeypatch.setattr("quickpool.store.time.time", lambda: current_hour + 1200)
    kid = store.create_api_key(ApiKeyCreate(name="managed"))["id"]
    events = [
        ("before", start - 0.25, "out-of-window", 200, 1, False, kid),
        ("start", start, "amazon-smart", 200, 20, True, kid),
        ("failed", current_hour - 1, "amazon-balanced", 503, 800, False, ""),
        ("current-1", current_hour, "amazon-smart", 200, 30, False, kid),
        ("current-2", current_hour + 60, "amazon-smart", 429, 900, True, kid),
        ("current-3", current_hour + 90, "amazon-fast", 200, 50, False, ""),
        ("next", current_hour + 3600, "out-of-window", 200, 1, False, ""),
    ]
    for rid, timestamp, model, status, duration, hit, key_id in events:
        store.log(rid, None, status, duration, 0, model=model, session_hit=hit, api_key_id=key_id)
        with store.db:
            store.db.execute("UPDATE requests SET created_at=? WHERE id=?", (timestamp, rid))

    analytics = store.analytics(hours=4)
    assert analytics["hours"] == 4
    assert analytics["hourly"] == [
        {"hour_ts": start, "total": 1, "success": 1, "failed": 0, "cache_hits": 1, "avg_ms": 20},
        {"hour_ts": start + 3600, "total": 0, "success": 0, "failed": 0, "cache_hits": 0, "avg_ms": 0},
        {"hour_ts": current_hour - 3600, "total": 1, "success": 0, "failed": 1, "cache_hits": 0, "avg_ms": 0},
        {"hour_ts": current_hour, "total": 3, "success": 2, "failed": 1, "cache_hits": 1, "avg_ms": 40},
    ]
    assert analytics["models"][0] == {"model": "amazon-smart", "total": 3, "success": 2, "avg_ms": 25}
    assert {row["model"]: row for row in analytics["models"][1:]} == {
        "amazon-balanced": {"model": "amazon-balanced", "total": 1, "success": 0, "avg_ms": 0},
        "amazon-fast": {"model": "amazon-fast", "total": 1, "success": 1, "avg_ms": 50},
    }
    assert analytics["keys"] == [
        {"api_key_id": kid, "name": "managed", "total": 3, "success": 2},
        {"api_key_id": "", "name": "主 API Key", "total": 2, "success": 1},
    ]
    assert len(store.analytics()["hourly"]) == 24
