"""Socket Chat v1 framing and request envelopes; no socket or application state."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 4_194_304
MAX_REQUEST_ID = 9_007_199_254_740_991

KNOWN_ACTIONS = frozenset(
    {
        "ping", "register", "login", "logout", "get_directory",
        "open_private", "create_group", "join_group", "send_message",
        "sync_room", "get_room_snapshot", "set_heart", "set_pin",
        "trigger_effect", "request_summary", "get_summary_status",
    }
)


class FrameTooLarge(ValueError):
    """A frame exceeds the byte limit before its LF delimiter."""


class ProtocolError(Exception):
    """A recoverable request error, unless the connection is explicitly fatal."""

    def __init__(self, code: str, message: str, *, close: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.close = close


@dataclass(frozen=True, slots=True)
class Request:
    request_id: int
    action: str
    payload: dict[str, Any]


class LineFramer:
    """Keep incomplete bytes; emit whole frames before decoding UTF-8.

    Iteration is deliberate: a valid frame preceding an oversized frame in one
    recv must still be processed before the fatal error is reported.
    """

    def __init__(self, max_bytes: int = MAX_REQUEST_BYTES):
        self._buffer = bytearray()
        self._max_bytes = max_bytes

    @property
    def has_pending(self) -> bool:
        return bool(self._buffer)

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        self._buffer.extend(chunk)
        while True:
            end = self._buffer.find(b"\n")
            size = len(self._buffer) if end < 0 else end
            if size > self._max_bytes:
                raise FrameTooLarge("Frame exceeds the byte limit")
            if end < 0:
                return
            frame = bytes(self._buffer[:end])
            del self._buffer[:end + 1]
            # Count CR toward the byte limit, but accept CRLF on input.
            if frame.endswith(b"\r"):
                frame = frame[:-1]
            yield frame


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside the finite float range")
    return number


def decode_document(frame: bytes) -> dict[str, Any]:
    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("INVALID_UTF8", "Data must be valid UTF-8", close=True) from None
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError):
        raise ProtocolError("INVALID_JSON", "Invalid JSON") from None
    if type(document) is not dict:
        raise ProtocolError("INVALID_REQUEST", "Request must be a JSON object")
    return document


def read_request_id(document: dict[str, Any]) -> int:
    request_id = document.get("request_id")
    # bool is a subclass of int in Python, but is not an ID in this protocol.
    if type(request_id) is not int or not 1 <= request_id <= MAX_REQUEST_ID:
        raise ProtocolError("INVALID_REQUEST_ID", "request_id must be an integer between 1 and 9007199254740991")
    return request_id


def validate_envelope(document: dict[str, Any], request_id: int) -> Request:
    if set(document) != {"request_id", "action", "payload"}:
        raise ProtocolError("INVALID_REQUEST", "Request must contain exactly request_id, action, and payload")
    if type(document["action"]) is not str or type(document["payload"]) is not dict:
        raise ProtocolError("INVALID_REQUEST", "action must be a string and payload must be an object")
    return Request(request_id, document["action"], document["payload"])


def success_response(request_id: int, data: dict[str, Any]) -> dict[str, Any]:
    return {"request_id": request_id, "ok": True, "data": data}


def error_response(
    request_id: int | None, code: str, message: str
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def encode_message(
    message: dict[str, Any], *, max_bytes: int = MAX_RESPONSE_BYTES
) -> bytes:
    body = json.dumps(
        message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(body) > max_bytes:
        raise FrameTooLarge("Encoded frame exceeds the byte limit")
    return body + b"\n"
