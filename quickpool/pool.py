from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from .store import Store


class PoolUnavailable(Exception):
    pass


class Pool:
    def __init__(self, store: Store):
        self.store = store
        self.lock = asyncio.Lock()
        self.inflight: dict[str, int] = defaultdict(int)

    async def acquire(self, excluded: set[str]):
        async with self.lock:
            eligible = [a for a in self.store.accounts()
                        if a["id"] not in excluded and a["enabled"] and a["configured"]
                        and a["state"] in ("ready", "cooldown") and a["cooldown_until"] <= time.time()
                        and self.inflight[a["id"]] < a["max_concurrency"]]
            if not eligible:
                raise PoolUnavailable("没有可用账号：请完成授权，或等待冷却 / 当前请求结束")
            eligible.sort(key=lambda a: ((self.inflight[a["id"]] + 1) / a["weight"], a["last_used"]))
            account = eligible[0]
            self.inflight[account["id"]] += 1
            self.store.patch_account(account["id"], {"last_used": time.time()})
            return account

    async def release(self, account_id: str, status=200, retry_after=60, message="", *, penalize=True):
        async with self.lock:
            self.inflight[account_id] = max(0, self.inflight[account_id] - 1)
            account = self.store.account(account_id)
            if not account:
                return
            if status == 200:
                values = {"successes": account["successes"] + 1}
                if account['state'] != 'expired':
                    values.update(state="ready", last_error="", cooldown_until=0)
            elif status == 499 or not penalize or (400 <= status < 500 and status not in (401, 403, 429)):
                return
            else:
                values = {"failures": account["failures"] + 1, "last_error": message[:200]}
                if status in (401, 403):
                    # Only NativeAuth can confirm that the OAuth grant is dead.
                    if account['state'] != 'expired':
                        values.update(state="cooldown", cooldown_until=time.time()+max(1, min(retry_after, 86400)))
                elif status == 429 or status >= 500:
                    values.update(state="cooldown", cooldown_until=time.time() + max(1, min(retry_after, 86400)))
            self.store.patch_account(account_id, values)

