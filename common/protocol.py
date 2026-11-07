"""Socket Chat v1 framing and request envelopes; no socket or application state."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

# Maximum request frame size before LF; an optional CR counts toward this limit.
MAX_REQUEST_BYTES = 65_536
# Larger reply limit allows bounded batches of history and directory information.
MAX_RESPONSE_BYTES = 4_194_304
# IDs stay within the integer range that JSON clients can represent exactly.
MAX_REQUEST_ID = 9_007_199_254_740_991

# Distinguish unknown actions from known actions whose handlers are not built yet.
KNOWN_ACTIONS = frozenset(
    {
        "ping", "login", "logout", "get_directory",
        "open_private", "create_group", "join_group", "send_message",
        "sync_room", "get_room_snapshot", "set_heart", "set_pin",
        "trigger_effect", "request_summary", "get_summary_status",
    }
)


class FrameTooLarge(ValueError):
    """A frame exceeds the byte limit before its LF delimiter."""


class ProtocolError(Exception):
    """A recoverable request error, unless the connection is explicitly fatal."""

    # Carry a machine-readable error code and a safe message for the response.
    # close marks request errors that require ending the offending connection.
    # The base exception also receives the message so str(error) remains useful.
    def __init__(self, code: str, message: str, *, close: bool = False):
        super().__init__(message)
        # Stable code used by callers to identify the kind of protocol failure.
        self.code = code
        # Human-readable explanation used by error_response() or the client log.
        self.message = message
        # Server I/O closes the connection after replying when this flag is True.
        self.close = close


# Frozen request envelope passed from a server I/O thread to its worker.
@dataclass(frozen=True, slots=True)
class Request:
    # Correlates a response with this request within its original TCP connection.
    request_id: int
    # Names the handler; action-specific payload validation happens afterward.
    action: str
    # Parsed action arguments; validating the envelope does not validate contents.
    payload: dict[str, Any]


class LineFramer:
    """Keep incomplete bytes; emit whole frames before decoding UTF-8.

    Iteration is deliberate: a valid frame preceding an oversized frame in one
    recv must still be processed before the fatal error is reported.
    """

    # Prepare an empty byte buffer and a per-frame limit without reading a socket.
    # Each connection needs its own framer so different byte streams never mix.
    # Clients pass MAX_RESPONSE_BYTES; the default is the server's request limit.
    def __init__(self, max_bytes: int = MAX_REQUEST_BYTES):
        # Accumulates incoming chunks and retains an unfinished line between calls.
        self._buffer = bytearray()
        # Maximum bytes before LF, checked before an optional CR is removed.
        self._max_bytes = max_bytes

    # Report whether bytes remain after the last emitted frame, without clearing.
    # The server uses this to track partial-frame timing; the client also checks
    # for unexpected data remaining after its one outstanding response.
    @property
    def has_pending(self) -> bool:
        return bool(self._buffer)

    # Consume a received chunk and yield complete frames without their CR/LF.
    # Remove each completed line but retain an incomplete tail for the next call.
    # Work happens during iteration: a valid earlier frame is yielded before a
    # later oversized frame raises FrameTooLarge, preserving response order.
    # Keep bytes undecoded here because a UTF-8 character may span two chunks.
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


# JSON object_pairs_hook receives key/value pairs before building a dictionary.
# Reject repeated keys here, including nested ones, instead of silently keeping
# the last value and losing evidence that the input was ambiguous.
def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


# JSON parse_constant hook rejects NaN and Infinity accepted by Python's parser.
# These values are outside our JSON contract; raising ValueError lets
# decode_document() turn the parser failure into a recoverable INVALID_JSON.
def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant")


# JSON parse_float hook also catches numeric overflow such as 1e999 becoming inf.
# parse_constant alone cannot catch this because the input is a numeric literal.
# Return only finite floats; decode_document() translates failures to INVALID_JSON.
def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside the finite float range")
    return number


# Decode one complete frame as UTF-8, then parse a strict JSON object.
# Invalid UTF-8 is fatal; malformed JSON and non-object roots are protocol errors.
# This function does not inspect request/response fields; callers validate those
# next and decide whether to reply, log the error, or close their connection.
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


# Extract a positive integer ID while rejecting bool and out-of-range values.
# The server calls this before envelope validation so a usable ID can be echoed
# and reserved even when other fields in the same request turn out to be invalid.
def read_request_id(document: dict[str, Any]) -> int:
    request_id = document.get("request_id")
    # bool is a subclass of int in Python, but is not an ID in this protocol.
    if type(request_id) is not int or not 1 <= request_id <= MAX_REQUEST_ID:
        raise ProtocolError("INVALID_REQUEST_ID", "request_id must be an integer between 1 and 9007199254740991")
    return request_id


# Validate the exact outer request fields and the types of action and payload.
# request_id has already passed read_request_id() and connection sequence checks.
# Return a Request for the worker; this does not validate an action's arguments,
# authenticate the caller, or check room permissions.
def validate_envelope(document: dict[str, Any], request_id: int) -> Request:
    if set(document) != {"request_id", "action", "payload"}:
        raise ProtocolError("INVALID_REQUEST", "Request must contain exactly request_id, action, and payload")
    if type(document["action"]) is not str or type(document["payload"]) is not dict:
        raise ProtocolError("INVALID_REQUEST", "action must be a string and payload must be an object")
    return Request(request_id, document["action"], document["payload"])


# Apply the same username rule to UI input and incoming server login payloads.
# Do not trim or normalize: identity matching happens in SQLite, while the first
# spelling is preserved for display. Username-only entry does not prove ownership.
def validate_username(username: Any) -> str:
    if type(username) is not str or re.fullmatch(r"[A-Za-z0-9_]{3,32}", username) is None:
        raise ProtocolError(
            "INVALID_PAYLOAD", "Username must contain 3-32 ASCII letters, digits, or underscores",
        )
    return username


# Validate a parsed response against the client's one outstanding request ID.
# Require boolean ok and the matching success/error fields, while allowing
# additional response fields for compatibility with the protocol contract.
# Return the original dictionary; callers display or process its data afterward.
def validate_response(document: dict[str, Any], request_id: int) -> dict[str, Any]:
    """Check required response fields and match the one outstanding request."""
    if type(document.get("request_id")) is not int or document["request_id"] != request_id:
        raise ProtocolError("INVALID_RESPONSE", "Response request_id does not match the request")
    if type(document.get("ok")) is not bool:
        raise ProtocolError("INVALID_RESPONSE", "Response ok must be a boolean")
    if document["ok"]:
        if type(document.get("data")) is not dict or "error" in document:
            raise ProtocolError("INVALID_RESPONSE", "Successful response must contain data and no error")
    else:
        error = document.get("error")
        if (
            type(error) is not dict
            or type(error.get("code")) is not str
            or type(error.get("message")) is not str
            or ("details" in error and type(error["details"]) is not dict)
            or "data" in document
        ):
            raise ProtocolError("INVALID_RESPONSE", "Failed response must contain a valid error and no data")
    return document


# Build the standard successful response envelope using the original request ID.
# The handler supplies its action-specific data; serialization happens later,
# so this helper neither writes to a socket nor changes application state.
def success_response(request_id: int, data: dict[str, Any]) -> dict[str, Any]:
    return {"request_id": request_id, "ok": True, "data": data}


# Build the standard failure envelope from a safe code/message supplied by caller.
# request_id may be None when malformed input did not contain a usable ID.
# Return a dictionary only; the connection owner decides how to send it and
# whether the connection must close after the error.
def error_response(
    request_id: int | None, code: str, message: str
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


# Serialize an envelope as compact UTF-8 JSON and append exactly one LF delimiter.
# The JSON encoder escapes newlines inside strings and rejects non-finite values.
# Enforce the byte limit before adding LF; raise FrameTooLarge before any socket
# write so callers can reject the request or replace an oversized response.
def encode_message(
    message: dict[str, Any], *, max_bytes: int = MAX_RESPONSE_BYTES
) -> bytes:
    body = json.dumps(
        message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(body) > max_bytes:
        raise FrameTooLarge("Encoded frame exceeds the byte limit")
    return body + b"\n"
