"""Amazon Quick 1.2.4660 desktop wire formats, without network or credential I/O.

Gateway: CBOR payload inside AWS EventStream. AIDP: base64 JSON input,
AWS EventStream/base64 JSON output. These formats are intentionally distinct.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import struct
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import cbor2

Transport = Literal["gateway", "aidp"]
MAX_FRAME_BYTES = 16 * 1024 * 1024
DESKTOP_SURFACE = "QUICK_WORK_DESKTOP_WINDOWS"


class NativeProtocolError(ValueError):
    """Safe to surface: messages never contain upstream headers or payloads."""


@dataclass(frozen=True)
class EventStreamFrame:
    headers: dict[str, Any]
    payload: bytes


def _transport(transport: str) -> None:
    if transport not in ("gateway", "aidp"):
        raise NativeProtocolError("Unsupported Quick transport")


def _json_bytes(message: Mapping[str, Any] | str) -> bytes:
    try:
        obj = json.loads(message) if isinstance(message, str) else message
        if not isinstance(obj, Mapping):
            raise TypeError
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise NativeProtocolError("Invalid Quick JSON message") from None


def _json_object(raw: bytes | str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise TypeError
        return obj
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise NativeProtocolError("Invalid Quick JSON response") from None


def encode_eventstream(headers: Mapping[str, str], payload: bytes) -> bytes:
    """Encode string-valued headers exactly as the desktop Gateway encoder."""
    encoded = bytearray()
    try:
        for name, value in headers.items():
            nb, vb = name.encode("utf-8"), value.encode("utf-8")
            if not 1 <= len(nb) <= 255 or len(vb) > 65535:
                raise ValueError
            encoded.extend(bytes((len(nb),)) + nb + b"\x07" + struct.pack(">H", len(vb)) + vb)
        if not isinstance(payload, bytes):
            raise TypeError
        total = 16 + len(encoded) + len(payload)
        if total > MAX_FRAME_BYTES:
            raise ValueError
        prelude = struct.pack(">II", total, len(encoded))
        frame = prelude + struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF) + encoded + payload
        return bytes(frame) + struct.pack(">I", zlib.crc32(frame) & 0xFFFFFFFF)
    except (AttributeError, TypeError, ValueError, UnicodeError, struct.error):
        raise NativeProtocolError("Invalid AWS EventStream output") from None


def _headers(raw: bytes) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    pos = 0

    def take(size: int) -> bytes:
        nonlocal pos
        if pos + size > len(raw):
            raise NativeProtocolError("Truncated AWS EventStream header")
        value = raw[pos:pos + size]
        pos += size
        return value

    try:
        while pos < len(raw):
            name_size = take(1)[0]
            if not name_size:
                raise NativeProtocolError("Invalid AWS EventStream header")
            name = take(name_size).decode("utf-8")
            if name in headers:
                raise NativeProtocolError("Duplicate AWS EventStream header")
            kind = take(1)[0]
            if kind in (0, 1):
                value = kind == 0
            elif kind in (2, 3, 4, 5, 8):
                fmt = {2: ">b", 3: ">h", 4: ">i", 5: ">q", 8: ">q"}[kind]
                value = struct.unpack(fmt, take(struct.calcsize(fmt)))[0]
            elif kind in (6, 7):
                value = take(struct.unpack(">H", take(2))[0])
                if kind == 7:
                    value = value.decode("utf-8")
            elif kind == 9:
                value = uuid.UUID(bytes=take(16))
            else:
                raise NativeProtocolError("Unknown AWS EventStream header type")
            headers[name] = value
    except (UnicodeError, struct.error, ValueError):
        raise NativeProtocolError("Invalid AWS EventStream header") from None
    return headers


class EventStreamDecoder:
    """Incremental, fail-closed decoder; a WS message may split or join frames."""

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES):
        if not 16 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("Invalid EventStream frame limit")
        self.max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()
        self._failed = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[EventStreamFrame]:
        if self._failed:
            raise NativeProtocolError("AWS EventStream decoder is failed")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise NativeProtocolError("Expected binary AWS EventStream data")
        self._buffer.extend(chunk)
        frames = []
        consumed = 0
        try:
            while len(self._buffer) - consumed >= 12:
                total, header_size, crc = struct.unpack_from(">III", self._buffer, consumed)
                if total < 16 or total > self.max_frame_bytes or header_size > total - 16:
                    raise NativeProtocolError("Invalid AWS EventStream lengths")
                if zlib.crc32(self._buffer[consumed:consumed + 8]) & 0xFFFFFFFF != crc:
                    raise NativeProtocolError("AWS EventStream prelude CRC mismatch")
                if len(self._buffer) - consumed < total:
                    break
                raw = bytes(self._buffer[consumed:consumed + total])
                if zlib.crc32(raw[:-4]) & 0xFFFFFFFF != struct.unpack(">I", raw[-4:])[0]:
                    raise NativeProtocolError("AWS EventStream message CRC mismatch")
                frames.append(EventStreamFrame(_headers(raw[12:12 + header_size]), raw[12 + header_size:-4]))
                consumed += total
            if consumed:
                del self._buffer[:consumed]
            if len(self._buffer) > self.max_frame_bytes:
                raise NativeProtocolError("AWS EventStream buffer limit exceeded")
        except NativeProtocolError:
            self._failed = True
            self._buffer.clear()
            raise
        return frames

    def finish(self) -> None:
        if self._failed:
            raise NativeProtocolError("AWS EventStream decoder is failed")
        if self._buffer:
            self._failed = True
            self._buffer.clear()
            raise NativeProtocolError("Truncated AWS EventStream response")


def encode_gateway_message(message: Mapping[str, Any] | str) -> bytes:
    return encode_eventstream({":event-type": "rawInputEvent", ":content-type": "application/cbor",
                               ":message-type": "event"}, cbor2.dumps({"payload": _json_bytes(message)}))


def encode_aidp_message(message: Mapping[str, Any] | str) -> bytes:
    payload = base64.b64encode(_json_bytes(message)).decode("ascii")
    return _json_bytes({"eventType": "rawInputEvent", "payload": {"payload": payload}})


def encode_message(transport: Transport, message: Mapping[str, Any] | str) -> bytes:
    _transport(transport)
    return encode_gateway_message(message) if transport == "gateway" else encode_aidp_message(message)


def decode_frame(transport: Transport, frame: EventStreamFrame) -> dict[str, Any] | None:
    _transport(transport)
    if frame.headers.get(":message-type") in ("error", "exception"):
        # Error messages/codes may contain user data or credentials: never echo them.
        raise NativeProtocolError("Quick upstream returned an error frame")
    if not frame.payload:
        return None
    if transport == "aidp":
        if frame.headers.get(":event-type") != "rawOutputEvent":
            return None
        envelope = _json_object(frame.payload)
        try:
            raw = envelope["payload"]
            if not isinstance(raw, str):
                raise TypeError
            inner = base64.b64decode(raw, validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise NativeProtocolError("Invalid Quick AIDP payload") from None
    else:
        try:
            source = io.BytesIO(frame.payload)
            envelope = cbor2.CBORDecoder(source).decode()
            if source.read(1) or not isinstance(envelope, dict):
                raise ValueError
            inner = envelope["payload"]
            if not isinstance(inner, (bytes, str)):
                raise TypeError
        except (cbor2.CBORDecodeError, KeyError, TypeError, ValueError, RecursionError):
            raise NativeProtocolError("Invalid Quick Gateway payload") from None
    return _json_object(inner)


class NativeMessageDecoder:
    def __init__(self, transport: Transport, max_frame_bytes: int = MAX_FRAME_BYTES):
        _transport(transport)
        self.transport = transport
        self.eventstream = EventStreamDecoder(max_frame_bytes)

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        result = []
        for frame in self.eventstream.feed(chunk):
            event = decode_frame(self.transport, frame)
            if event is not None:
                result.append(event)
        return result

    def finish(self) -> None:
        self.eventstream.finish()


def _client_fields(surface: str, outpost_id: str, app_version: str | None) -> dict[str, Any]:
    fields = {"surfaceType": surface, "acp_first_text": 1, "user_notification": 1,
              "subtask_progress": 1, "external_agent_sign_in": 1, "recents_promotion": 1}
    if outpost_id:
        fields["outpostId"] = outpost_id
    if app_version:
        fields["appVersion"] = app_version
    return fields


def build_session_connect(transport: Transport, *, outpost_id: str = "", actor_arn: str = "",
                          surface: str = DESKTOP_SURFACE, app_version: str | None = None) -> dict[str, Any]:
    _transport(transport)
    if transport == "gateway":
        return {"type": "session_connect", "session_id": outpost_id}
    return {"type": "session_connect", "payload": {
        "actor_id": actor_arn, **_client_fields(surface, outpost_id, app_version)}}


def build_conversation_connect(thread_id: str, *, last_event_id: str | None = None,
                               focus_only: bool = True, surface: str = DESKTOP_SURFACE,
                               outpost_id: str = "", app_version: str | None = None) -> dict[str, Any]:
    payload = {"session_id": thread_id, "focus_only": focus_only,
               **_client_fields(surface, outpost_id, app_version)}
    if last_event_id is not None:
        payload["last_event_id"] = last_event_id
    return {"type": "session_connect", "payload": payload}


def build_session_create(thread_id: str, title: str = "New Conversation") -> dict[str, Any]:
    return {"type": "session_create", "payload": {"session_id": thread_id, "title": title}}


def build_user_message(text: str, thread_id: str, *, message_id: str | None = None,
                       trace_id: str | None = None, agent_mode: str = "smart",
                       thinking_effort: str = "off", user_timezone: str = "UTC",
                       user_locale: str = "en-US", surface: str = DESKTOP_SURFACE,
                       outpost_id: str = "", app_version: str | None = None,
                       custom_prompt: str | None = None, chat_agent_id: str | None = None,
                       web_search_enabled: bool = False,
                       auto_routing: bool = False, attachments: list[dict] | None = None) -> dict[str, Any]:
    return {"type": "user_message", "payload": {
        "traceId": trace_id or str(uuid.uuid4()), "text": text, "send_origin": "programmatic",
        **_client_fields(surface, outpost_id, app_version),
        "message_id": message_id or str(uuid.uuid4()), "thread_id": thread_id,
        "agent_mode": agent_mode, "thinking_effort": thinking_effort,
        "user_timezone": user_timezone, "user_locale": user_locale,
        "custom_prompt": custom_prompt, "chat_agent_id": chat_agent_id or None, "auto_routing": auto_routing,
        "web_search_enabled": web_search_enabled, "direct_chat": False, "voice_mode": False,
        **({'attachments': attachments} if attachments else {})}}


def build_token_refresh(token: str) -> str:
    """Return a Gateway WS TEXT message; do not pass it to encode_message."""
    if not isinstance(token, str) or not token:
        raise NativeProtocolError("Missing Quick refresh bearer")
    return _json_bytes({"type": "token_refresh", "token": token}).decode("utf-8")


@dataclass(frozen=True)
class TextUpdate:
    delta: str = ""
    done: bool = False


class NativeTextEvents:
    """Extract one thread's text; cumulative snapshots only emit their new suffix.

    A conflicting snapshot cannot repair text already sent to an API consumer,
    so it raises instead of duplicating or silently returning corrupt content.
    """

    def __init__(self, thread_id: str, max_text_chars: int = MAX_FRAME_BYTES):
        self.thread_id = thread_id
        self.max_text_chars = max_text_chars
        self.text = ""
        self.done = False
        self.message_id: str | None = None
        self._segments: dict[str, str] = {}
        self._event_ids: OrderedDict[str | int, None] = OrderedDict()

    def feed(self, event: Mapping[str, Any]) -> TextUpdate:
        if self.done or event.get("thread_id") != self.thread_id:
            return TextUpdate(done=self.done)
        event_id = event.get("event_id")
        if isinstance(event_id, (str, int)) and not isinstance(event_id, bool):
            if event_id in self._event_ids:
                return TextUpdate()
            self._event_ids[event_id] = None
            if len(self._event_ids) > 4096:
                self._event_ids.popitem(last=False)
        kind, payload = event.get("type"), event.get("payload", {})
        if not isinstance(payload, Mapping):
            raise NativeProtocolError("Invalid Quick text event")
        if kind in ("error", "guardrail_blocked", "turn_aborted"):
            raise NativeProtocolError("Quick upstream rejected the chat turn")
        message_id = event.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return TextUpdate()
        if kind == "message_start":
            if message_id in self._segments and message_id != self.message_id:
                return TextUpdate()  # An earlier bubble replayed after its successor.
            self.message_id = message_id
            self._segments.setdefault(message_id, "")
            return TextUpdate()
        if kind not in ("text_delta", "text_complete", "stream_snapshot", "message_complete"):
            return TextUpdate()
        if self.message_id is None:
            self.message_id = message_id
            self._segments.setdefault(message_id, "")
        if message_id != self.message_id:
            return TextUpdate()
        old, delta = self._segments[message_id], ""
        if kind == "text_delta":
            delta = payload.get("delta", "")
            if not isinstance(delta, str):
                raise NativeProtocolError("Invalid Quick text delta")
        elif isinstance(payload.get("text"), str):
            snapshot = payload["text"]
            if snapshot.startswith(old):
                delta = snapshot[len(old):]
            elif not old.startswith(snapshot):
                raise NativeProtocolError("Conflicting Quick text snapshot")
        if len(self.text) + len(delta) > self.max_text_chars:
            raise NativeProtocolError("Quick text response limit exceeded")
        self._segments[message_id] = old + delta
        self.text += delta
        if kind == "message_complete" and payload.get("continuation_expected") is not True:
            self.done = True
        return TextUpdate(delta, self.done)
