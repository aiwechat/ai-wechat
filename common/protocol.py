"""Application-layer protocol helpers for the chat system.

The wire format is:
    4-byte unsigned big-endian length prefix + UTF-8 JSON envelope

TCP already gives reliable ordered bytes, but it does not preserve message
boundaries. The length prefix provides application-layer framing so client and
server can safely parse sticky packets and split packets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any
from uuid import uuid4


PROTOCOL_VERSION = "1.0"
HEADER_SIZE = 4
MAX_FRAME_SIZE = 8 * 1024 * 1024


class MessageType(str, Enum):
    REGISTER = "register"
    LOGIN = "login"
    LOGOUT = "logout"
    PRIVATE_MSG = "private_msg"
    GROUP_MSG = "group_msg"
    CREATE_GROUP = "create_group"
    JOIN_GROUP = "join_group"
    LEAVE_GROUP = "leave_group"
    HEARTBEAT = "heartbeat"
    USER_STATUS = "user_status"
    HISTORY_REQUEST = "history_request"
    HISTORY_RESPONSE = "history_response"
    FILE_START = "file_start"
    FILE_CHUNK = "file_chunk"
    FILE_END = "file_end"
    AI_REQUEST = "ai_request"
    AI_RESPONSE = "ai_response"
    MODERATION_WARNING = "moderation_warning"
    ERROR = "error"


class ErrorCode(str, Enum):
    INVALID_JSON = "invalid_json"
    INVALID_FRAME = "invalid_frame"
    INVALID_MESSAGE_TYPE = "invalid_message_type"
    MISSING_FIELD = "missing_field"
    INVALID_FIELD = "invalid_field"
    AUTH_FAILED = "auth_failed"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    SERVER_ERROR = "server_error"


class ProtocolError(ValueError):
    """Raised when bytes or JSON cannot be parsed as a valid protocol message."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        *,
        detail: Any | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.detail = detail
        self.request_id = request_id

    def to_message(self) -> "ProtocolMessage":
        return make_error(
            self.error_code,
            self.message,
            request_id=self.request_id,
            detail=self.detail,
        )


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp ending in Z."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class ProtocolMessage:
    """Canonical JSON envelope exchanged by clients and server."""

    type: MessageType
    payload: dict[str, Any] = field(default_factory=dict)
    sender: str | None = None
    receiver: str | None = None
    group_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    version: str = PROTOCOL_VERSION
    request_id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": self.type.value,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "sender": self.sender,
            "receiver": self.receiver,
            "group_id": self.group_id,
            "payload": self.payload,
            "meta": self.meta,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProtocolMessage":
        if not isinstance(data, dict):
            raise ProtocolError(ErrorCode.INVALID_FIELD, "Message must be a JSON object")
        if "type" not in data:
            raise ProtocolError(ErrorCode.MISSING_FIELD, "Missing required field: type")

        try:
            msg_type = MessageType(data["type"])
        except ValueError as exc:
            raise ProtocolError(
                ErrorCode.INVALID_MESSAGE_TYPE,
                f"Unsupported message type: {data.get('type')}",
                detail={"type": data.get("type")},
            ) from exc

        payload = data.get("payload", {})
        meta = data.get("meta", {})
        if not isinstance(payload, dict):
            raise ProtocolError(ErrorCode.INVALID_FIELD, "Field payload must be an object")
        if not isinstance(meta, dict):
            raise ProtocolError(ErrorCode.INVALID_FIELD, "Field meta must be an object")

        return cls(
            version=str(data.get("version", PROTOCOL_VERSION)),
            type=msg_type,
            request_id=str(data.get("request_id") or uuid4().hex),
            timestamp=str(data.get("timestamp") or utc_now()),
            sender=data.get("sender"),
            receiver=data.get("receiver"),
            group_id=data.get("group_id"),
            payload=payload,
            meta=meta,
        )

    @classmethod
    def from_json(cls, raw: str | bytes | bytearray) -> "ProtocolMessage":
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = bytes(raw).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError(ErrorCode.INVALID_JSON, "Frame body is not valid UTF-8") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolError(
                ErrorCode.INVALID_JSON,
                "Frame body is not valid JSON",
                detail={"line": exc.lineno, "column": exc.colno},
            ) from exc
        return cls.from_dict(data)


def make_message(
    message_type: MessageType | str,
    *,
    payload: dict[str, Any] | None = None,
    sender: str | None = None,
    receiver: str | None = None,
    group_id: str | None = None,
    meta: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ProtocolMessage:
    """Build a protocol message with sensible defaults."""

    return ProtocolMessage(
        type=MessageType(message_type),
        payload=payload or {},
        sender=sender,
        receiver=receiver,
        group_id=group_id,
        meta=meta or {},
        request_id=request_id or uuid4().hex,
    )


def make_error(
    error_code: ErrorCode | str,
    message: str,
    *,
    request_id: str | None = None,
    detail: Any | None = None,
    sender: str = "server",
) -> ProtocolMessage:
    """Build a standard error response envelope."""

    code = ErrorCode(error_code)
    return ProtocolMessage(
        type=MessageType.ERROR,
        sender=sender,
        request_id=request_id or uuid4().hex,
        payload={
            "error_code": code.value,
            "message": message,
            "detail": detail,
        },
    )


def _json_bytes(message: ProtocolMessage | dict[str, Any] | str | bytes | bytearray) -> bytes:
    if isinstance(message, ProtocolMessage):
        body = message.to_json().encode("utf-8")
    elif isinstance(message, dict):
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    elif isinstance(message, str):
        body = message.encode("utf-8")
    elif isinstance(message, (bytes, bytearray)):
        body = bytes(message)
    else:
        raise TypeError(f"Unsupported message type for framing: {type(message)!r}")

    if len(body) > MAX_FRAME_SIZE:
        raise ProtocolError(
            ErrorCode.INVALID_FRAME,
            f"Frame is too large: {len(body)} bytes",
            detail={"max_frame_size": MAX_FRAME_SIZE},
        )
    return body


def encode_frame(message: ProtocolMessage | dict[str, Any] | str | bytes | bytearray) -> bytes:
    """Encode a JSON message as one TCP frame."""

    body = _json_bytes(message)
    return len(body).to_bytes(HEADER_SIZE, "big") + body


def decode_frame(buffer: bytes | bytearray) -> tuple[ProtocolMessage | None, bytes]:
    """Decode one frame from a byte buffer.

    Returns (None, original_remaining_bytes) when the buffer does not yet
    contain a complete frame.
    """

    raw = bytes(buffer)
    if len(raw) < HEADER_SIZE:
        return None, raw

    body_length = int.from_bytes(raw[:HEADER_SIZE], "big")
    if body_length <= 0:
        raise ProtocolError(ErrorCode.INVALID_FRAME, "Frame length must be positive")
    if body_length > MAX_FRAME_SIZE:
        raise ProtocolError(
            ErrorCode.INVALID_FRAME,
            f"Frame length exceeds limit: {body_length}",
            detail={"max_frame_size": MAX_FRAME_SIZE},
        )

    frame_end = HEADER_SIZE + body_length
    if len(raw) < frame_end:
        return None, raw

    message = ProtocolMessage.from_json(raw[HEADER_SIZE:frame_end])
    return message, raw[frame_end:]


def decode_frames(buffer: bytes | bytearray) -> tuple[list[ProtocolMessage], bytes]:
    """Decode as many complete frames as possible from a byte buffer."""

    messages: list[ProtocolMessage] = []
    remaining = bytes(buffer)
    while remaining:
        message, remaining_after = decode_frame(remaining)
        if message is None:
            return messages, remaining_after
        messages.append(message)
        remaining = remaining_after
    return messages, remaining

