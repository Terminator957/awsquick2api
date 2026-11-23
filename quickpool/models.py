from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_LOGIN_URL = "https://quick.aws.com/sn"


def allowed_upstream(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment
            or not (host in ("quick.aws.com", "quicksight.aws.amazon.com")
                    or host.endswith((".quick.aws.com", ".quicksight.aws.amazon.com", ".amazonaws.com")))):
        raise ValueError("只允许 HTTPS Amazon Quick / AWS 上游地址")
    return url


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    login_url: str = DEFAULT_LOGIN_URL
    weight: int = Field(default=1, ge=1, le=100)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    group_name: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=300)

    @field_validator("login_url")
    @classmethod
    def login_host(cls, value: str) -> str:
        allowed_upstream(value)
        host = urlparse(value).hostname or ""
        if not (host == "quick.aws.com" or host.endswith(".quick.aws.com")):
            raise ValueError("登录地址必须是 quick.aws.com")
        return value


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    weight: int | None = Field(default=None, ge=1, le=100)
    max_concurrency: int | None = Field(default=None, ge=1, le=8)
    group_name: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=300)


class BatchAccountAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["enable", "disable", "clear_cooldown", "refresh_quota", "test",
                    "set_weight", "set_concurrency"]
    account_ids: list[str] = Field(min_length=1, max_length=100)
    value: int | None = Field(default=None, ge=1, le=100)


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=300)
    group_name: str = Field(default="", max_length=40)
    daily_limit: int = Field(default=0, ge=0, le=1000000)
    expires_at: float | None = None


class ApiKeyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    notes: str | None = Field(default=None, max_length=300)
    group_name: str | None = Field(default=None, max_length=40)
    enabled: bool | None = None
    daily_limit: int | None = Field(default=None, ge=0, le=1000000)
    expires_at: float | None = None


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduling_strategy: Literal["weighted", "quota_first", "round_robin"] | None = None
    session_cache_ttl: int | None = Field(default=None, ge=60, le=86400)
    max_attempts: int | None = Field(default=None, ge=1, le=5)


class NativeLoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Literal['enterprise', 'social'] = 'social'
    region: str = Field(default='us-east-1', pattern=r'^[a-z]{2}(?:-[a-z]+)+-\d$')


class Message(BaseModel):
    model_config = ConfigDict(extra='allow')
    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    function_call: dict[str, Any] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @model_validator(mode='after')
    def validate_parts(self):
        if self.content is None and not (self.role == 'assistant' and (self.tool_calls or self.function_call)):
            raise ValueError('消息必须含 content，或 assistant.tool_calls')
        if isinstance(self.content, list):
            if len(self.content) > 32:
                raise ValueError('消息内容分段过多')
            for part in self.content:
                if part.get('type') == 'text':
                    if not isinstance(part.get('text'), str):
                        raise ValueError('text 分段无效')
                elif part.get('type') == 'image_url':
                    image = part.get('image_url')
                    if not isinstance(image, dict) or not isinstance(image.get('url'), str):
                        raise ValueError('image_url 分段无效')
                    if image.get('detail', 'auto') not in ('auto', 'low', 'high'):
                        raise ValueError('image_url.detail 无效')
                else:
                    raise ValueError('仅支持 text 和 image_url 内容分段')
        if self.role == 'tool' and not self.tool_call_id:
            raise ValueError('tool 消息缺少 tool_call_id')
        return self


DEFAULT_MODEL = 'amazon-fast'
NATIVE_MODES = ('fast', 'balanced', 'smart')

MODEL_IDS = tuple('amazon-' + mode + suffix for mode in NATIVE_MODES
                  for suffix in ('', '-low', '-medium', '-high'))
MODEL_ALIASES = {mode + suffix: 'amazon-' + mode + suffix
                 for mode in NATIVE_MODES for suffix in ('', '-low', '-medium', '-high')}
MODEL_ALIASES.update(opus='amazon-smart', sonnet='amazon-balanced', haiku='amazon-fast')


def model_options(model: str):
    claude = re.fullmatch(r'claude-(?:\d+(?:-\d+)*-)?(opus|sonnet|haiku)(?:-\d+(?:-\d+)*|-latest)?', model)
    canonical = MODEL_ALIASES.get(claude[1] if claude else model, model)
    if canonical not in MODEL_IDS:
        raise ValueError('未知模型名，请从 GET /v1/models 选择')
    parts = canonical.removeprefix('amazon-').split('-')
    return parts[0], parts[1] if len(parts) > 1 else 'off'


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=128)
    messages: list[Message] = Field(min_length=1, max_length=500)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    reasoning_effort: Literal['none', 'minimal', 'low', 'medium', 'high', 'off'] | None = None
    agent_mode: Literal['fast', 'balanced', 'smart'] | None = None
    thinking_effort: Literal['off', 'low', 'medium', 'high'] | None = None
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=64)
    tool_choice: str | dict[str, Any] | None = None
    functions: list[dict[str, Any]] | None = Field(default=None, max_length=64)
    function_call: str | dict[str, Any] | None = None
    parallel_tool_calls: bool = True
    response_format: dict[str, Any] | None = None

    @property
    def mode(self):
        return self.agent_mode or model_options(self.model)[0]

    @property
    def effort(self):
        return self.thinking_effort or {'none':'off', 'minimal':'low', 'off':'off'}.get(
            self.reasoning_effort, self.reasoning_effort or model_options(self.model)[1])

    @model_validator(mode="after")
    def supported(self):
        model_options(self.model)
        if self.messages[-1].role not in ('user', 'tool', 'function'):
            raise ValueError('最后一条消息必须为 user 或工具执行结果')
        if self.model_extra and 'audio' in self.model_extra:
            raise ValueError('暂不支持音频输出')
        if self.tools and self.functions:
            raise ValueError('tools 与 functions 请只传一种')
        if self.tools:
            for tool in self.tools:
                function = tool.get('function')
                if tool.get('type') != 'function' or not isinstance(function,dict):
                    raise ValueError('仅支持 function 工具')
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(function.get('name',''))):
                    raise ValueError('工具函数名无效')
        if self.functions:
            for function in self.functions:
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(function.get('name',''))):
                    raise ValueError('工具函数名无效')
        if self.response_format and self.response_format.get('type') not in ('text','json_object','json_schema'):
            raise ValueError('response_format.type 无效')
        if self.model_extra and self.model_extra.get("n", 1) != 1:
            raise ValueError("仅支持 n=1")
        return self


def account_number(url: str) -> str:
    match = re.search(r"/account/(\d{12})(?:/|$)", url)
    return match.group(1) if match else ""

