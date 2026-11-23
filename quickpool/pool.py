from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from .quota import is_quota_exhausted, remaining_score
from .store import Store


class PoolUnavailable(Exception):
    pass


class Pool:
    def __init__(self, store: Store, quotas=None):
        self.store = store
        self.quotas = quotas
        self.lock = asyncio.Lock()
        self.inflight: dict[str, int] = defaultdict(int)

    def eligible_ids(self, excluded: set[str] | None = None) -> set[str]:
        excluded = excluded or set()
        now = time.time()
        ids = set()
        for a in self.store.accounts():
            if (a["id"] not in excluded and a["enabled"] and a["configured"]
                    and a["state"] in ("ready", "cooldown") and a["cooldown_until"] <= now
                    and self.inflight[a["id"]] < a["max_concurrency"]):
                if self.quotas and is_quota_exhausted(self.quotas.cached(a["id"])):
                    continue
                ids.add(a["id"])
        return ids

    async def acquire(self, excluded: set[str], *, preferred_account_id: str | None = None):
        async with self.lock:
            now = time.time()
            base_eligible = [a for a in self.store.accounts()
                             if a["id"] not in excluded and a["enabled"] and a["configured"]
                             and a["state"] in ("ready", "cooldown") and a["cooldown_until"] <= now
                             and self.inflight[a["id"]] < a["max_concurrency"]]
            if not base_eligible:
                raise PoolUnavailable("没有可用账号：请完成授权，或等待冷却 / 当前请求结束")
            if self.quotas:
                eligible = [a for a in base_eligible if not is_quota_exhausted(self.quotas.cached(a["id"]))]
                if not eligible:
                    raise PoolUnavailable("可用账号会话额度已满，请等待重置或启用其他账号")
            else:
                eligible = base_eligible
            if preferred_account_id:
                preferred = next((a for a in eligible if a["id"] == preferred_account_id), None)
                if preferred is not None:
                    self.inflight[preferred["id"]] += 1
                    self.store.patch_account(preferred["id"], {"last_used": now})
                    return preferred
            eligible.sort(key=lambda a: (
                (self.inflight[a["id"]] + 1) / a["weight"],
                -remaining_score(self.quotas.cached(a["id"])) if self.quotas else 0.0,
                a["last_used"],
            ))
            account = eligible[0]
            self.inflight[account["id"]] += 1
            self.store.patch_account(account["id"], {"last_used": now})
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

