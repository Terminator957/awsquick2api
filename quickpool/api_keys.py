from __future__ import annotations

import hashlib
import secrets
import time


def generate_api_key() -> tuple[str, str, str]:
    plaintext = "sk-qp-" + secrets.token_urlsafe(32)
    return plaintext, hash_api_key(plaintext), plaintext[:10] + "…" + plaintext[-4:]


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class KeyAuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def verify_managed_key(store, token: str) -> dict | None:
    key = store.get_api_key_by_hash(hash_api_key(token))
    if key is None:
        return None
    if not key["enabled"]:
        raise KeyAuthError(401, "API 密钥已停用")
    if 0 < key["expires_at"] <= time.time():
        raise KeyAuthError(401, "API 密钥已过期")
    if 0 < key["daily_limit"] <= key["requests_today"]:
        raise KeyAuthError(429, "API 密钥已达到每日请求上限")
    return key
