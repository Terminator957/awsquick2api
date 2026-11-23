"""Match native conversation threads against the prefix of a new request."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass


@dataclass
class SessionEntry:
    account_id: str
    thread_id: str
    config_hash: str
    msg_hashes: tuple[str, ...]
    last_used: float
    turns: int = 1


def hash_message(msg) -> str:
    return hashlib.sha256(json.dumps(msg, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def hash_config(agent_mode, thinking_effort, tools, response_format, tool_choice, parallel_tool_calls) -> str:
    return hash_message((agent_mode, thinking_effort, tools, response_format, tool_choice, parallel_tool_calls))


def _context_config(context, agent_mode, thinking_effort):
    return hash_config(agent_mode, thinking_effort, context.get('tools'), context.get('response_format'),
                       context.get('tool_choice'), context.get('parallel_tool_calls'))


class SessionCache:
    def __init__(self, max_entries=256, ttl=1800.0, store=None):
        self.max_entries, self.ttl = max_entries, ttl
        self.hits = self.misses = 0
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
                    turns=row['turns'],
                )

    def match(self, context, agent_mode, thinking_effort, available_account_ids=None) -> tuple[SessionEntry, int] | None:
        now = time.monotonic()
        messages = context.get('messages') or []
        current_hashes = tuple(map(hash_message, messages))
        config_hash = _context_config(context, agent_mode, thinking_effort)
        eligible = (
            entry for entry in self._entries.values()
            if now - entry.last_used <= self.ttl
            and entry.config_hash == config_hash
            and (available_account_ids is None or entry.account_id in available_account_ids)
            and len(entry.msg_hashes) < len(current_hashes)
            and current_hashes[:len(entry.msg_hashes)] == entry.msg_hashes
        )
        entry = max(eligible, key=lambda entry: (len(entry.msg_hashes), entry.last_used), default=None)
        if entry is None:
            return None
        offset = len(entry.msg_hashes)
        if offset < len(messages) - 1 and messages[offset].get('role') == 'assistant':
            offset += 1
        self.record_hit()
        return entry, offset

    def record_hit(self):
        self.hits += 1

    def record_miss(self):
        self.misses += 1

    def save(self, account_id, thread_id, context, agent_mode, thinking_effort, *, assistant_reply=None):
        now = time.monotonic()
        self._expire(now)
        previous = self._entries.get(thread_id)
        entry = SessionEntry(
            account_id, thread_id, _context_config(context, agent_mode, thinking_effort),
            tuple(map(hash_message, context.get('messages') or [])), now,
            turns=previous.turns + 1 if previous else 1,
        )
        self._entries[thread_id] = entry
        if self._store is not None:
            self._store.save_session(entry.thread_id, entry.account_id, entry.config_hash,
                                     entry.msg_hashes, entry.turns, self.ttl)
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries.values(), key=lambda entry: entry.last_used)
            self.invalidate(oldest.thread_id)

    def invalidate(self, thread_id):
        self._entries.pop(thread_id, None)
        if self._store is not None:
            self._store.delete_session(thread_id)

    def invalidate_account(self, account_id):
        for entry in list(self._entries.values()):
            if entry.account_id == account_id:
                self._entries.pop(entry.thread_id, None)
        if self._store is not None:
            self._store.delete_account_sessions(account_id)

    def stats(self):
        self._expire(time.monotonic())
        total = self.hits + self.misses
        return {'hits': self.hits, 'misses': self.misses, 'hit_rate': self.hits / total if total else 0.0,
                'active_sessions': len(self._entries)}

    def _expire(self, now):
        for entry in list(self._entries.values()):
            if now - entry.last_used > self.ttl:
                self.invalidate(entry.thread_id)
