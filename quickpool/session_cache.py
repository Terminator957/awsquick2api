"""Match native conversation threads against the prefix of a new request."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

# 命中后续期时，最多每 30 秒把新的过期时间回写一次 SQLite。
PERSIST_INTERVAL = 30.0


@dataclass
class SessionEntry:
    account_id: str
    thread_id: str
    config_hash: str
    msg_hashes: tuple[str, ...]
    last_used: float
    turns: int = 1
    last_persisted: float = 0.0


def hash_message(msg) -> str:
    return hashlib.sha256(json.dumps(msg, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def hash_config(agent_mode, thinking_effort, tools, response_format, tool_choice, parallel_tool_calls, scope='') -> str:
    payload = (agent_mode, thinking_effort, tools, response_format, tool_choice, parallel_tool_calls)
    if scope:
        payload = (*payload, scope)
    return hash_message(payload)


def _context_config(context, agent_mode, thinking_effort, scope=''):
    return hash_config(agent_mode, thinking_effort, context.get('tools'), context.get('response_format'),
                       context.get('tool_choice'), context.get('parallel_tool_calls'), scope=scope)


class SessionCache:
    def __init__(self, max_entries=256, ttl=1800.0, store=None, max_per_account=64):
        self.max_entries, self.ttl = max_entries, ttl
        self.max_per_account = max(1, int(max_per_account))
        self.hits = self.misses = 0
        self.saved_tokens = 0
        self.evictions = 0
        self._store = store if store is not None and hasattr(store, 'load_sessions') else None
        self._entries: dict[str, SessionEntry] = {}
        if self._store is not None:
            now = time.monotonic()
            for row in self._store.load_sessions(limit=max_entries):
                remaining = min(self.ttl, max(0.0, row['remaining']))
                if remaining <= 0:
                    continue
                self._entries[row['thread_id']] = SessionEntry(
                    row['account_id'], row['thread_id'], row['config_hash'],
                    tuple(row['msg_hashes']), now - (self.ttl - remaining),
                    turns=row['turns'], last_persisted=now,
                )

    def match(self, context, agent_mode, thinking_effort, available_account_ids=None, *, scope='',
              allowed_thread_ids: set[str] | None = None) -> tuple[SessionEntry, int] | None:
        now = time.monotonic()
        messages = context.get('messages') or []
        current_hashes = tuple(map(hash_message, messages))
        config_hash = _context_config(context, agent_mode, thinking_effort, scope=scope)
        eligible = (
            entry for entry in self._entries.values()
            if now - entry.last_used <= self.ttl
            and entry.config_hash == config_hash
            and (available_account_ids is None or entry.account_id in available_account_ids)
            and (allowed_thread_ids is None or entry.thread_id in allowed_thread_ids)
            and 1 <= len(entry.msg_hashes) < len(current_hashes)
            and current_hashes[:len(entry.msg_hashes)] == entry.msg_hashes
            and any(messages[i].get('role') not in ('system', 'developer') for i in range(len(entry.msg_hashes)))
        )
        entry = max(eligible, key=lambda entry: (len(entry.msg_hashes), entry.last_used), default=None)
        if entry is None:
            self.misses += 1
            return None
        offset = len(entry.msg_hashes)
        if offset < len(messages) - 1 and messages[offset].get('role') == 'assistant':
            offset += 1
        self.hits += 1
        entry.last_used = now
        if self._store is not None and now - entry.last_persisted >= PERSIST_INTERVAL:
            self._store.touch_session(entry.thread_id, time.time() + self.ttl)
            entry.last_persisted = now
        return entry, offset

    def record_hit(self):
        self.hits += 1

    def record_miss(self):
        self.misses += 1

    def record_saved(self, tokens) -> None:
        """累计缓存复用省下的输入 token（本地估算值）。"""
        try:
            value = int(tokens)
        except (TypeError, ValueError):
            return
        if value > 0:
            self.saved_tokens += value

    def save(self, account_id, thread_id, context, agent_mode, thinking_effort, *, assistant_reply=None, scope=''):
        now = time.monotonic()
        self._expire(now)
        previous = self._entries.get(thread_id)
        entry = SessionEntry(
            account_id, thread_id, _context_config(context, agent_mode, thinking_effort, scope=scope),
            tuple(map(hash_message, context.get('messages') or [])), now,
            turns=previous.turns + 1 if previous else 1, last_persisted=now,
        )
        self._entries[thread_id] = entry
        if self._store is not None:
            self._store.save_session(entry.thread_id, entry.account_id, entry.config_hash,
                                     entry.msg_hashes, entry.turns, self.ttl)
        self._enforce_limits(account_id)

    def _enforce_limits(self, account_id):
        if self.max_per_account:
            same_account = [entry for entry in self._entries.values() if entry.account_id == account_id]
            while len(same_account) > self.max_per_account:
                oldest = min(same_account, key=lambda entry: entry.last_used)
                self._drop(oldest.thread_id)
                same_account.remove(oldest)
                self.evictions += 1
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries.values(), key=lambda entry: entry.last_used)
            self._drop(oldest.thread_id)
            self.evictions += 1

    def _drop(self, thread_id):
        self._entries.pop(thread_id, None)
        if self._store is not None:
            self._store.delete_session(thread_id)

    def invalidate(self, thread_id):
        self._drop(thread_id)

    def invalidate_account(self, account_id):
        for entry in list(self._entries.values()):
            if entry.account_id == account_id:
                self._entries.pop(entry.thread_id, None)
        if self._store is not None:
            self._store.delete_account_sessions(account_id)

    def list_sessions(self, account_names: dict[str, str] | None = None) -> list[dict]:
        now = time.monotonic()
        self._expire(now)
        names = account_names or {}
        return [
            {'thread_id': entry.thread_id, 'account_id': entry.account_id,
             'account_name': names.get(entry.account_id, ''), 'turns': entry.turns,
             'remaining_sec': round(max(0.0, self.ttl - (now - entry.last_used)), 1),
             'config_hash': entry.config_hash[:8]}
            for entry in sorted(self._entries.values(), key=lambda entry: entry.last_used, reverse=True)
        ]

    def clear(self) -> int:
        thread_ids = list(self._entries)
        for thread_id in thread_ids:
            self._drop(thread_id)
        return len(thread_ids)

    def set_ttl(self, ttl: float):
        self.ttl = float(ttl)

    def stats(self):
        self._expire(time.monotonic())
        total = self.hits + self.misses
        return {'hits': self.hits, 'misses': self.misses, 'hit_rate': self.hits / total if total else 0.0,
                'active_sessions': len(self._entries), 'saved_tokens': self.saved_tokens,
                'evictions': self.evictions}

    def _expire(self, now):
        for entry in list(self._entries.values()):
            if now - entry.last_used > self.ttl:
                self._drop(entry.thread_id)
