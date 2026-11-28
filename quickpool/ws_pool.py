"""Exclusive parked WebSocket pool for incognito-safe multi-turn hot resume."""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any

from websockets.exceptions import ConnectionClosed, WebSocketException

from .native_protocol import encode_message


@dataclass
class ParkedSocket:
    thread_id: str
    account_id: str
    socket: Any
    decoder: Any
    session: Any
    parked_at: float
    keepalive_task: asyncio.Task | None = None


class WarmWsPool:
    """Keeps idle AWS Quick WebSockets alive between turns so incognito threads can hot-resume."""

    def __init__(self, *, max_sockets: int = 64, max_per_account: int = 12,
                 idle_ttl: float = 1500.0, ping_interval: float = 25.0):
        self.max_sockets = max(1, int(max_sockets))
        self.max_per_account = max(1, int(max_per_account))
        self.idle_ttl = float(idle_ttl)
        self.ping_interval = float(ping_interval)
        self._parked: dict[str, ParkedSocket] = {}

    def thread_ids(self) -> set[str]:
        self._prune_dead()
        return set(self._parked.keys())

    def has(self, thread_id: str) -> bool:
        entry = self._parked.get(thread_id)
        if entry is None:
            return False
        if not self._is_alive(entry):
            self._drop_sync(thread_id)
            return False
        return True

    def stats(self) -> dict[str, int]:
        self._prune_dead()
        return {'parked_sockets': len(self._parked)}

    def _is_alive(self, entry: ParkedSocket) -> bool:
        if time.monotonic() - entry.parked_at > self.idle_ttl:
            return False
        if getattr(entry.socket, 'close_code', None) is not None:
            return False
        state = getattr(entry.socket, 'state', None)
        if state is not None and getattr(state, 'name', '') in ('CLOSING', 'CLOSED'):
            return False
        return True

    def _drop_sync(self, thread_id: str) -> ParkedSocket | None:
        entry = self._parked.pop(thread_id, None)
        if entry is None:
            return None
        if entry.keepalive_task and not entry.keepalive_task.done():
            entry.keepalive_task.cancel()
        return entry

    def _prune_dead(self) -> None:
        for tid in list(self._parked):
            entry = self._parked.get(tid)
            if entry and not self._is_alive(entry):
                dropped = self._drop_sync(tid)
                if dropped is not None:
                    self._schedule_close(dropped.socket)

    @staticmethod
    def _schedule_close(socket: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_safe_close(socket))
        except RuntimeError:
            pass

    async def checkout(self, thread_id: str, account_id: str) -> ParkedSocket | None:
        """Exclusively pop a live parked socket for (thread_id, account_id), cancelling its keepalive."""
        entry = self._parked.pop(thread_id, None)
        if entry is None:
            return None
        if entry.keepalive_task and not entry.keepalive_task.done():
            entry.keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await entry.keepalive_task
            entry.keepalive_task = None
        if entry.account_id != account_id or not self._is_alive(entry):
            await _safe_close(entry.socket)
            return None
        return entry

    async def park(self, thread_id: str, account_id: str, socket: Any, decoder: Any, session: Any) -> None:
        """Park a healthy socket for reuse on the next turn of thread_id."""
        old = self._drop_sync(thread_id)
        if old is not None and old.socket is not socket:
            await _safe_close(old.socket)
        entry = ParkedSocket(
            thread_id=thread_id, account_id=account_id,
            socket=socket, decoder=decoder, session=session,
            parked_at=time.monotonic(),
        )
        if self.ping_interval > 0:
            entry.keepalive_task = asyncio.create_task(self._keepalive(entry))
        self._parked[thread_id] = entry
        await self._enforce_limits(account_id)

    async def _keepalive(self, entry: ParkedSocket) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                if self._parked.get(entry.thread_id) is not entry:
                    return
                if not self._is_alive(entry):
                    break
                payload = {'type': 'ping', 'payload': {'timestamp': int(time.time() * 1000)}}
                await entry.socket.send(encode_message(entry.session.transport, payload))
        except (asyncio.CancelledError, ConnectionClosed, WebSocketException, OSError):
            pass
        except Exception:
            pass
        if self._parked.get(entry.thread_id) is entry:
            self._parked.pop(entry.thread_id, None)
            await _safe_close(entry.socket)

    async def _enforce_limits(self, account_id: str) -> None:
        self._prune_dead()
        same_account = [e for e in self._parked.values() if e.account_id == account_id]
        while len(same_account) > self.max_per_account:
            oldest = min(same_account, key=lambda e: e.parked_at)
            same_account.remove(oldest)
            dropped = self._drop_sync(oldest.thread_id)
            if dropped:
                await _safe_close(dropped.socket)
        while len(self._parked) > self.max_sockets:
            oldest = min(self._parked.values(), key=lambda e: e.parked_at)
            dropped = self._drop_sync(oldest.thread_id)
            if dropped:
                await _safe_close(dropped.socket)

    async def invalidate(self, thread_id: str) -> None:
        entry = self._drop_sync(thread_id)
        if entry is not None:
            await _safe_close(entry.socket)

    async def invalidate_account(self, account_id: str) -> None:
        for tid in [t for t, e in self._parked.items() if e.account_id == account_id]:
            await self.invalidate(tid)

    async def close_all(self) -> None:
        for tid in list(self._parked):
            await self.invalidate(tid)


async def _safe_close(socket: Any) -> None:
    with contextlib.suppress(Exception):
        await socket.close()
