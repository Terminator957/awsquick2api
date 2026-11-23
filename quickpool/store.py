from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

from .api_keys import generate_api_key
from .models import DEFAULT_MODEL, AccountCreate, ApiKeyCreate, account_number


_API_KEY_SELECT = """
    SELECT id, name, key_prefix, notes, group_name, enabled, daily_limit, expires_at,
           requests_total, last_used, created_at,
           (SELECT COUNT(*) FROM requests WHERE api_key_id=k.id AND created_at>=?) AS requests_today
    FROM api_keys k
"""
_SETTINGS_DEFAULTS = {
    "scheduling_strategy": "weighted",
    "session_cache_ttl": 1800,
    "max_attempts": 3,
    "max_images": 50,
    "max_messages": 2000,
    "max_body_mb": 20,
}


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cipher = Fernet(self._key_file("encryption.key", lambda: Fernet.generate_key().decode()).encode())
        self.admin_key = os.getenv("QUICKPOOL_ADMIN_KEY") or self._key_file(
            "admin.key", lambda: secrets.token_urlsafe(36))
        self.api_key = os.getenv("QUICKPOOL_API_KEY") or self._key_file(
            "api.key", lambda: "sk-quick-" + secrets.token_urlsafe(32))
        self.db = sqlite3.connect(self.directory / "quickpool.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, login_url TEXT NOT NULL,
                aws_account_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                weight INTEGER NOT NULL DEFAULT 1, max_concurrency INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'unconfigured', cooldown_until REAL NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', last_used REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, secret BLOB
            );
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, created_at REAL NOT NULL, account_id TEXT,
                model TEXT NOT NULL, status INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
                output_chars INTEGER NOT NULL, error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS requests_created ON requests(created_at);
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY, created_at REAL NOT NULL, account_id TEXT NOT NULL,
                event TEXT NOT NULL, details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_cache (
                thread_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                config_hash TEXT NOT NULL, msg_hashes TEXT NOT NULL,
                turns INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS session_cache_expires ON session_cache(expires_at);
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                group_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                daily_limit INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL DEFAULT 0,
                requests_total INTEGER NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.commit()
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(accounts)')}
        for name, definition in {
            'auth_type': "TEXT NOT NULL DEFAULT 'legacy'",
            'auth_provider': "TEXT NOT NULL DEFAULT ''",
            'token_expires_at': 'REAL NOT NULL DEFAULT 0',
            'has_refresh_token': 'INTEGER NOT NULL DEFAULT 0',
            'group_name': "TEXT NOT NULL DEFAULT ''",
            'notes': "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE accounts ADD COLUMN {name} {definition}')
        # Cookie/template credentials must never silently become native accounts.
        self.db.execute("UPDATE accounts SET state='unconfigured' WHERE auth_type!='native'")
        req_columns = {row[1] for row in self.db.execute('PRAGMA table_info(requests)')}
        if 'session_hit' not in req_columns:
            self.db.execute('ALTER TABLE requests ADD COLUMN session_hit INTEGER NOT NULL DEFAULT 0')
        if 'api_key_id' not in req_columns:
            self.db.execute("ALTER TABLE requests ADD COLUMN api_key_id TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS requests_api_key_created ON requests(api_key_id, created_at)")
        self.db.commit()

    def _key_file(self, name, factory):
        path = self.directory / name
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(factory())
            path.chmod(0o600)
        except FileExistsError:
            pass
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError(f"密钥文件为空: {path}")
        return value

    @staticmethod
    def public(row):
        if row is None:
            return None
        result = dict(row)
        result["configured"] = bool(result.pop("secret", None)) and result['auth_type'] == 'native'
        result['has_refresh_token'] = bool(result['has_refresh_token'])
        result["enabled"] = bool(result["enabled"])
        if result["state"] == "cooldown" and result["cooldown_until"] <= time.time():
            result["state"] = "ready"
        return result

    def accounts(self):
        with self.lock:
            return [self.public(row) for row in self.db.execute("SELECT * FROM accounts ORDER BY created_at")]

    def account(self, account_id):
        with self.lock:
            return self.public(self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())

    def create_account(self, account: AccountCreate):
        account_id = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO accounts"
                "(id,name,login_url,aws_account_id,weight,max_concurrency,created_at,group_name,notes) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (account_id, account.name.strip(), account.login_url, account_number(account.login_url),
                 account.weight, account.max_concurrency, time.time(),
                 account.group_name.strip(), account.notes),
            )
        return self.account(account_id)

    def patch_account(self, account_id, values):
        allowed = {"name", "enabled", "weight", "max_concurrency", "state", "cooldown_until", "last_error",
                   "last_used", "successes", "failures", "group_name", "notes"}
        if not values or set(values) - allowed:
            return self.account(account_id)
        with self.lock, self.db:
            self.db.execute(
                "UPDATE accounts SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), account_id),
            )
        return self.account(account_id)

    def _decrypt_secret(self, account_id):
        with self.lock:
            row = self.db.execute("SELECT secret FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row or not row["secret"]:
            return None
        return json.loads(self.cipher.decrypt(row["secret"]))

    def set_credentials(self, account_id, credentials, *, state='unverified'):
        if credentials.get('_kind') != 'desktop-oauth':
            raise ValueError('Only native desktop OAuth credentials are accepted')
        encoded = self.cipher.encrypt(json.dumps(credentials, ensure_ascii=False).encode())
        identity_account = credentials.get('user_info', {}).get('awsAccountId', '')
        if not isinstance(identity_account, str) or not re.fullmatch(r'\d{12}', identity_account):
            identity_account = ''
        with self.lock, self.db:
            self.db.execute(
                "UPDATE accounts SET secret=?,auth_type='native',auth_provider=?,token_expires_at=?,"
                "has_refresh_token=?,state=?,last_error='',cooldown_until=0,"
                "aws_account_id=CASE WHEN aws_account_id='' THEN ? ELSE aws_account_id END WHERE id=?",
                (encoded, credentials['provider'], credentials.get('expires_at', 0),
                 bool(credentials.get('refresh_token')), state, identity_account, account_id))

    def credentials(self, account_id):
        value = self._decrypt_secret(account_id)
        return value if value and value.get('_kind') == 'desktop-oauth' else None

    def auth_event(self, account_id, event, details):
        """Internal callers supply only fixed metadata and SHA-256 fingerprints."""
        with self.lock, self.db:
            self.db.execute('INSERT INTO auth_events(created_at,account_id,event,details) VALUES(?,?,?,?)',
                            (time.time(), account_id, event, json.dumps(details, sort_keys=True)))
            self.db.execute('DELETE FROM auth_events WHERE id IN (SELECT id FROM auth_events '
                            'ORDER BY id DESC LIMIT -1 OFFSET 2000)')

    def auth_events(self, account_id, limit=50):
        with self.lock:
            rows = self.db.execute('SELECT created_at,event,details FROM auth_events WHERE account_id=? '
                                   'ORDER BY id DESC LIMIT ?', (account_id, limit)).fetchall()
        return [dict(row) | {'details': json.loads(row['details'])} for row in rows]

    def delete_account(self, account_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM session_cache WHERE account_id=?", (account_id,))
            self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def load_sessions(self, limit: int = 256):
        now = time.time()
        with self.lock, self.db:
            self.db.execute("DELETE FROM session_cache WHERE expires_at<=?", (now,))
            rows = self.db.execute(
                "SELECT thread_id,account_id,config_hash,msg_hashes,turns,expires_at "
                "FROM session_cache ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            try:
                hashes = tuple(json.loads(row['msg_hashes']))
            except (ValueError, TypeError):
                continue
            result.append({
                'thread_id': row['thread_id'],
                'account_id': row['account_id'],
                'config_hash': row['config_hash'],
                'msg_hashes': hashes,
                'turns': int(row['turns']),
                'remaining': max(0.0, float(row['expires_at']) - now),
            })
        return result

    def save_session(self, thread_id: str, account_id: str, config_hash: str,
                     msg_hashes: tuple[str, ...], turns: int, ttl: float):
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO session_cache"
                "(thread_id,account_id,config_hash,msg_hashes,turns,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (thread_id, account_id, config_hash, json.dumps(list(msg_hashes)),
                 int(turns), now, now + max(0.0, float(ttl))),
            )

    def delete_session(self, thread_id: str):
        with self.lock, self.db:
            self.db.execute("DELETE FROM session_cache WHERE thread_id=?", (thread_id,))

    def delete_account_sessions(self, account_id: str):
        with self.lock, self.db:
            self.db.execute("DELETE FROM session_cache WHERE account_id=?", (account_id,))

    def list_api_keys(self):
        with self.lock:
            rows = self.db.execute(_API_KEY_SELECT + " ORDER BY created_at", (int(time.time() // 86400 * 86400),))
            return [dict(row) | {"enabled": bool(row["enabled"])} for row in rows]

    def get_api_key(self, key_id):
        with self.lock:
            row = self.db.execute(
                _API_KEY_SELECT + " WHERE id=?", (int(time.time() // 86400 * 86400), key_id)).fetchone()
        return dict(row) | {"enabled": bool(row["enabled"])} if row is not None else None

    def get_api_key_by_hash(self, h):
        with self.lock:
            row = self.db.execute(
                _API_KEY_SELECT + " WHERE key_hash=?", (int(time.time() // 86400 * 86400), h)).fetchone()
        return dict(row) | {"enabled": bool(row["enabled"])} if row is not None else None

    def create_api_key(self, data: ApiKeyCreate | None = None, **kwargs):
        if data is None:
            data = ApiKeyCreate(**kwargs)
        key_id = str(uuid.uuid4())
        plaintext, key_hash, key_prefix = generate_api_key()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO api_keys(id,name,key_hash,key_prefix,notes,group_name,daily_limit,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (key_id, data.name, key_hash, key_prefix, data.notes, data.group_name,
                 data.daily_limit, data.expires_at or 0, time.time()),
            )
            return self.get_api_key(key_id) | {"plaintext_key": plaintext}

    def patch_api_key(self, key_id, values):
        allowed = {"name", "notes", "group_name", "enabled", "daily_limit", "expires_at"}
        if not values or set(values) - allowed:
            return self.get_api_key(key_id)
        if "expires_at" in values and values["expires_at"] is None:
            values = values | {"expires_at": 0}
        with self.lock, self.db:
            self.db.execute(
                "UPDATE api_keys SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), key_id),
            )
        return self.get_api_key(key_id)

    update_api_key = patch_api_key

    def delete_api_key(self, key_id):
        with self.lock, self.db:
            cur = self.db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
            return cur.rowcount > 0

    def record_api_key_use(self, key_id):
        with self.lock, self.db:
            self.db.execute("UPDATE api_keys SET requests_total=requests_total+1, last_used=? WHERE id=?",
                            (time.time(), key_id))

    def get_settings(self):
        with self.lock:
            return _SETTINGS_DEFAULTS | {key: json.loads(value) for key, value in
                                         self.db.execute("SELECT key,value FROM settings")}

    def patch_settings(self, values):
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                ((key, json.dumps(value)) for key, value in values.items()
                 if key in _SETTINGS_DEFAULTS and value is not None),
            )
        return self.get_settings()

    update_settings = patch_settings

    def log(self, request_id, account_id, status, duration_ms, output_chars, error="", *,
            model=DEFAULT_MODEL, session_hit: bool = False, api_key_id: str = ""):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO requests"
                "(id,created_at,account_id,model,status,duration_ms,output_chars,error,session_hit,api_key_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (request_id, time.time(), account_id, model, status,
                 duration_ms, output_chars, error[:200], 1 if session_hit else 0, api_key_id))
            # Bounded local retention; never log prompts, cookies, or raw upstream bodies.
            self.db.execute("DELETE FROM requests WHERE id IN (SELECT id FROM requests "
                            "ORDER BY created_at DESC LIMIT -1 OFFSET 10000)")

    @staticmethod
    def _logs_where(account_id, status, model, api_key_id, q):
        clauses, params = [], []
        if account_id:
            clauses.append("r.account_id=?")
            params.append(account_id)
        if status == 'ok':
            clauses.append("r.status=200")
        elif status == 'error':
            clauses.append("r.status!=200")
        elif status and status.isdigit():
            clauses.append("r.status=?")
            params.append(int(status))
        if model is not None:
            clauses.append("r.model=?")
            params.append(model)
        if api_key_id is not None:
            clauses.append("r.api_key_id=?")
            params.append(api_key_id)
        if q:
            clauses.append("(r.id LIKE ? OR r.error LIKE ?)")
            params.extend((f"%{q}%", f"%{q}%"))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def logs(self, limit=100, *, offset=0, account_id: str | None = None, status: str | None = None,
             model: str | None = None, api_key_id: str | None = None, q: str | None = None):
        where, params = self._logs_where(account_id, status, model, api_key_id, q)
        with self.lock:
            rows = self.db.execute(
                "SELECT r.*,a.name AS account_name,k.name AS api_key_name FROM requests r "
                "LEFT JOIN accounts a ON a.id=r.account_id LEFT JOIN api_keys k ON k.id=r.api_key_id"
                + where + " ORDER BY r.created_at DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['session_hit'] = bool(item.get('session_hit', 0))
            result.append(item)
        return result

    def logs_page(self, limit=50, *, offset=0, account_id: str | None = None, status: str | None = None,
                  model: str | None = None, api_key_id: str | None = None, q: str | None = None):
        where, params = self._logs_where(account_id, status, model, api_key_id, q)
        with self.lock:
            requests = self.logs(limit, offset=offset, account_id=account_id, status=status, model=model,
                                 api_key_id=api_key_id, q=q)
            total = self.db.execute("SELECT COUNT(*) FROM requests r" + where, params).fetchone()[0]
        return {"requests": requests, "total": total, "limit": limit, "offset": offset}

    def analytics(self, hours=24):
        end = (int(time.time() // 3600) + 1) * 3600
        start = end - hours * 3600
        window = " WHERE r.created_at>=? AND r.created_at<?"
        avg_ms = "COALESCE(AVG(CASE WHEN status=200 THEN duration_ms END),0) AS avg_ms"
        with self.lock:
            buckets = {row["hour_ts"]: dict(row) for row in self.db.execute(
                "SELECT CAST(created_at / 3600 AS INTEGER) * 3600 AS hour_ts, COUNT(*) AS total, "
                "SUM(status=200) AS success, SUM(status!=200) AS failed, SUM(session_hit=1) AS cache_hits, "
                + avg_ms + " FROM requests r" + window + " GROUP BY hour_ts", (start, end))}
            models = [dict(row) for row in self.db.execute(
                "SELECT model, COUNT(*) AS total, SUM(status=200) AS success, "
                + avg_ms + " FROM requests r" + window + " GROUP BY model ORDER BY total DESC", (start, end))]
            keys = [dict(row) for row in self.db.execute(
                "SELECT r.api_key_id, CASE WHEN r.api_key_id='' THEN '主 API Key' ELSE k.name END AS name, "
                "COUNT(*) AS total, SUM(status=200) AS success "
                "FROM requests r LEFT JOIN api_keys k ON k.id=r.api_key_id"
                + window + " GROUP BY r.api_key_id ORDER BY total DESC", (start, end))]
        empty = {"total": 0, "success": 0, "failed": 0, "cache_hits": 0, "avg_ms": 0}
        hourly = [buckets.get(hour, {"hour_ts": hour, **empty}) for hour in range(start, end, 3600)]
        return {"hours": hours, "hourly": hourly, "models": models, "keys": keys}

    def account_stats(self, account_id: str):
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) total, COALESCE(SUM(status=200),0) success, "
                "COALESCE(AVG(CASE WHEN status=200 THEN duration_ms END),0) avg_ms, "
                "COALESCE(SUM(session_hit=1),0) session_hits "
                "FROM requests WHERE account_id=? AND created_at>=?",
                (account_id, time.time() - 86400)).fetchone()
        return dict(row)

    def stats(self):
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) total, COALESCE(SUM(status=200),0) success, "
                "COALESCE(AVG(CASE WHEN status=200 THEN duration_ms END),0) avg_ms, "
                "COALESCE(SUM(session_hit=1),0) session_hits "
                "FROM requests WHERE created_at>=?", (time.time() - 86400,)).fetchone()
        return dict(row)

    def close(self):
        self.db.close()

