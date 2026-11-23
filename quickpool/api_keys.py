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


EFFORT_RANK = {'off': 0, 'low': 1, 'medium': 2, 'high': 3}


def resolve_user_policy(model, mode, requested_effort, has_explicit_effort,
                        global_smart_effort, policy) -> str:
    allowed = policy.get('allowed_models')
    if allowed and model not in allowed and mode not in allowed and 'amazon-' + mode not in allowed:
        raise KeyAuthError(403, f'当前用户无权使用模型 {model}')
    effort = (policy.get('smart_default_effort') or global_smart_effort
              if mode == 'smart' and not has_explicit_effort else requested_effort)
    maximum = policy.get('max_thinking_effort')
    if maximum and EFFORT_RANK[effort] > EFFORT_RANK[maximum]:
        effort = maximum
    return effort


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
    if key['user_id']:
        user = store.get_user(key['user_id'])
        if not user or not user['enabled']:
            raise KeyAuthError(401, '用户已停用')
        if 0 < user['daily_request_limit'] <= user['requests_today']:
            raise KeyAuthError(429, '用户已达到每日请求上限')
        if 0 < user['daily_token_limit'] <= user['tokens_today']:
            raise KeyAuthError(429, '用户已达到每日 Token 上限')
        key = key | {'user_name': user['name'], 'effective_group_name': key['group_name'] or user['group_name'],
                     'allowed_models': user['allowed_models'], 'max_thinking_effort': user['max_thinking_effort'],
                     'smart_default_effort': user['smart_default_effort']}
    return key
