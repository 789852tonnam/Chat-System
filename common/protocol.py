"""Socket Chat v1 framing and request envelopes; no socket or application state."""

from __future__ import annotations

import json
import math
import re
import unicodedata
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
    def __init__(self, code: str, message: str, *, close: bool = False, details: dict | None = None):
        super().__init__(message)
        # Stable code used by callers to identify the kind of protocol failure.
        self.code = code
        # Human-readable explanation used by error_response() or the client log.
        self.message = message
        # Server I/O closes the connection after replying when this flag is True.
        self.close = close
        # Structured recovery information, such as the current snapshot revision.
        self.details = details


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


# Trim group names while rejecting blank, oversized, control or multiline input.
# The same rule is used for local forms, server requests and room responses.
def validate_group_name(name: Any) -> str:
    if (type(name) is not str or not 1 <= len(name.strip()) <= 80
            or any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in name)):
        raise ProtocolError("INVALID_PAYLOAD", "Group name must contain 1-80 characters without controls or line breaks")
    return name.strip()


# Validate action-specific IDs without accepting bool, float, or numeric strings.
# Zero is reserved for empty-history cursors and revision counters.
def validate_id(value: Any, field: str, *, zero: bool = False) -> int:
    if type(value) is not int or not (0 if zero else 1) <= value <= MAX_REQUEST_ID:
        raise ProtocolError("INVALID_PAYLOAD", f"{field} must be an integer in the allowed ID range")
    return value


# Enforce canonical room keys before database lookup; access still needs membership.
# Group keys use lowercase UUID v4; private keys use sorted account IDs.
def validate_room_key(value: Any) -> str:
    if type(value) is str:
        match = re.fullmatch(r"private:([1-9][0-9]*):([1-9][0-9]*)", value)
        if match and len(value) <= 50 and 1 <= int(match[1]) < int(match[2]) <= MAX_REQUEST_ID:
            return value
        if re.fullmatch(r"group:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value):
            return value
    raise ProtocolError("INVALID_PAYLOAD", "Invalid room_key")


# Preserve user whitespace while normalizing CRLF and rejecting blank/control text.
# Surrogates are invalid Unicode scalars even when escaped JSON can represent them.
def validate_content(value: Any) -> str:
    if type(value) is not str:
        raise ProtocolError("INVALID_PAYLOAD", "content must be a string")
    content = value.replace("\r\n", "\n")
    if (not 1 <= len(content) <= 4000 or not content.strip()
            or any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\t" for char in content)):
        raise ProtocolError("INVALID_PAYLOAD", "content must contain 1-4000 characters without unsupported controls")
    return content


# Validate the three supported sync states: initial, empty room, or loaded range.
# Check each bound independently before relationships so malformed cursors cannot
# silently become a different query or make bool act as a numeric ID.
def validate_sync_request(payload: dict[str, Any]) -> None:
    if set(payload) != {"room_key", "old_count", "oldest_id", "latest_id", "room_revision"}:
        raise ProtocolError("INVALID_PAYLOAD", "sync_room requires room_key, old_count, oldest_id, latest_id, room_revision")
    validate_room_key(payload["room_key"])
    count = payload["old_count"]
    if type(count) is not int or not 0 <= count <= 100:
        raise ProtocolError("INVALID_PAYLOAD", "old_count must be an integer between 0 and 100")
    oldest, latest, revision = payload["oldest_id"], payload["latest_id"], payload["room_revision"]
    if oldest is not None:
        validate_id(oldest, "oldest_id")
    if latest is not None:
        validate_id(latest, "latest_id", zero=True)
    if revision is not None:
        validate_id(revision, "room_revision", zero=True)
    initial = oldest is None and latest is None and revision is None and count > 0
    empty = oldest is None and latest == 0 and revision is not None and count == 0
    loaded = oldest is not None and latest is not None and revision is not None and oldest <= latest
    if not (initial or empty or loaded):
        raise ProtocolError("INVALID_PAYLOAD", "Invalid history cursor combination")


# Validate a bounded inclusive snapshot range without allowing mixed null bounds.
# Later pages continue strictly after a delivered ID inside that same range.
def validate_snapshot_request(payload: dict[str, Any]) -> None:
    required = {"room_key", "from_id", "through_id", "after_id", "room_revision"}
    if not required <= payload.keys() or payload.keys() - required - {"limit"}:
        raise ProtocolError("INVALID_PAYLOAD", "Invalid snapshot fields")
    validate_room_key(payload["room_key"])
    validate_id(payload["room_revision"], "room_revision", zero=True)
    limit = payload.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ProtocolError("INVALID_PAYLOAD", "Snapshot limit must be between 1 and 100")
    first, last, after = payload["from_id"], payload["through_id"], payload["after_id"]
    if first is None and last is None and after is None:
        return
    validate_id(first, "from_id")
    validate_id(last, "through_id")
    if first > last:
        raise ProtocolError("INVALID_PAYLOAD", "Snapshot bounds must be increasing")
    if after is not None:
        validate_id(after, "after_id")
        if not first <= after <= last:
            raise ProtocolError("INVALID_PAYLOAD", "Snapshot cursor must be inside its range")


# Validate the public user fields returned by login and directory responses.
# Share this check so both actions reject invalid IDs and display names before
# the UI receives them; additional user fields may be ignored by older clients.
def validate_user(user: Any) -> dict[str, Any]:
    if type(user) is not dict or type(user.get("id")) is not int or not 1 <= user["id"] <= MAX_REQUEST_ID:
        raise ProtocolError("INVALID_RESPONSE", "Response contains an invalid user")
    try:
        validate_username(user.get("username"))
    except ProtocolError:
        raise ProtocolError("INVALID_RESPONSE", "Response contains an invalid username") from None
    return user


# Check the full public directory before application logic receives a snapshot.
# Names and IDs must be unique; multiple connections are represented once.
# Public group documents include members but never private-room history.
def validate_directory(data: dict[str, Any]) -> dict[str, Any]:
    if type(data.get("online_users")) is not list or type(data.get("groups")) is not list:
        raise ProtocolError("INVALID_RESPONSE", "Directory must contain online_users and groups lists")
    ids = set()
    names = set()
    for value in data["online_users"]:
        user = validate_user(value)
        name = user["username"].lower()
        if user["id"] in ids or name in names:
            raise ProtocolError("INVALID_RESPONSE", "Directory contains duplicate users")
        ids.add(user["id"])
        names.add(name)
    group_keys = set()
    for group in data["groups"]:
        validate_room(group)
        if group["kind"] != "group" or group["room_key"] in group_keys:
            raise ProtocolError("INVALID_RESPONSE", "Directory contains invalid or duplicate groups")
        group_keys.add(group["room_key"])
    return data


# Check common room fields, sorted unique members and the creator's membership.
# Private keys must name exactly two members; groups require a canonical key/name.
def validate_room(room: Any) -> dict[str, Any]:
    if (type(room) is not dict or not {"room_key", "kind", "name", "created_by_user_id", "created_at", "members"} <= room.keys()
            or room.get("kind") not in ("private", "group")):
        raise ProtocolError("INVALID_RESPONSE", "Invalid room")
    members = room.get("members")
    if type(members) is not list or not members:
        raise ProtocolError("INVALID_RESPONSE", "Room requires members")
    ids = [validate_user(user)["id"] for user in members]
    if ids != sorted(set(ids)):
        raise ProtocolError("INVALID_RESPONSE", "Room members must be sorted and unique")
    if room["kind"] == "private":
        if len(ids) != 2 or room["name"] is not None or room["room_key"] != f"private:{ids[0]}:{ids[1]}":
            raise ProtocolError("INVALID_RESPONSE", "Private room key does not match its members")
    else:
        try:
            validate_room_key(room["room_key"])
            if not room["room_key"].startswith("group:") or validate_group_name(room["name"]) != room["name"]:
                raise ProtocolError("INVALID_RESPONSE", "Invalid group key or name")
        except ProtocolError:
            raise ProtocolError("INVALID_RESPONSE", "Invalid group key or name") from None
    if (type(room.get("created_by_user_id")) is not int or room["created_by_user_id"] not in ids
            or type(room.get("created_at")) is not str):
        raise ProtocolError("INVALID_RESPONSE", "Invalid room creator or timestamp")
    return room


# Validate the human-message format implemented by the current chat milestone.
# Heart state is viewer-specific; AI fields retain their empty state for now.
def validate_message(message: Any, room_key: str) -> dict[str, Any]:
    if (type(message) is not dict or not {"id", "room_key", "sender_kind", "sender", "content", "created_at",
                                        "heart_count", "hearted_by_me", "ai"} <= message.keys()
            or message.get("room_key") != room_key):
        raise ProtocolError("INVALID_RESPONSE", "Message belongs to an unexpected room")
    validate_id(message.get("id"), "message.id")
    validate_user(message.get("sender"))
    if validate_content(message.get("content")) != message["content"]:
        raise ProtocolError("INVALID_RESPONSE", "Message content must already use normalized newlines")
    if (message.get("sender_kind") != "human" or type(message.get("created_at")) is not str
            or message.get("ai") is not None):
        raise ProtocolError("INVALID_RESPONSE", "Invalid message fields")
    _validate_heart_fields(message)
    return message


# A viewer-owned heart must contribute to the non-negative total count.
def _validate_heart_fields(data: dict) -> None:
    if (type(data.get("heart_count")) is not int or data["heart_count"] < 0
            or type(data.get("hearted_by_me")) is not bool
            or (data["hearted_by_me"] and data["heart_count"] == 0)):
        raise ProtocolError("INVALID_RESPONSE", "Invalid heart state")


# Pins can be outside the visible history, but must still belong to this room.
def _validate_pin(data: dict, room_key: str) -> None:
    if "pinned_message" not in data:
        raise ProtocolError("INVALID_RESPONSE", "Missing pinned_message")
    if data["pinned_message"] is not None:
        validate_message(data["pinned_message"], room_key)


# Match desired-state acknowledgements to the exact room/message request.
# These revisions confirm a mutation, not that the client has reloaded its range.
def validate_mutation_response(data: dict, request: dict, action: str) -> None:
    if data.get("room_key") != request["room_key"]:
        raise ProtocolError("INVALID_RESPONSE", "Mutation belongs to an unexpected room")
    validate_id(data.get("room_revision"), "room_revision", zero=True)
    if action == "set_heart":
        validate_id(data.get("message_id"), "message_id")
        _validate_heart_fields(data)
        if data["message_id"] != request["message_id"] or data["hearted_by_me"] != request["hearted"]:
            raise ProtocolError("INVALID_RESPONSE", "Heart acknowledgement does not match its request")
    else:
        _validate_pin(data, request["room_key"])
        pinned = data["pinned_message"]
        if (None if pinned is None else pinned["id"]) != request["message_id"]:
            raise ProtocolError("INVALID_RESPONSE", "Pin acknowledgement does not match its request")


# Check the fixed range, revision and pagination cursor before staging a page.
def validate_snapshot_response(data: dict, request: dict) -> None:
    if data.get("room_key") != request["room_key"]:
        raise ProtocolError("INVALID_RESPONSE", "Snapshot belongs to an unexpected room")
    validate_id(data.get("room_revision"), "room_revision", zero=True)
    if data["room_revision"] != request["room_revision"] or type(data.get("has_more")) is not bool:
        raise ProtocolError("INVALID_RESPONSE", "Snapshot revision or continuation is invalid")
    _validate_pin(data, request["room_key"])
    values = data.get("messages")
    limit = request.get("limit", 100)
    if type(values) is not list or len(values) > limit or "next_after_id" not in data:
        raise ProtocolError("INVALID_RESPONSE", "Invalid snapshot batch")
    ids = [validate_message(message, request["room_key"])["id"] for message in values]
    if ids != sorted(set(ids)):
        raise ProtocolError("INVALID_RESPONSE", "Snapshot IDs must be unique and increasing")
    first, last, after = request["from_id"], request["through_id"], request["after_id"]
    if ids and (first is None or ids[0] < first or ids[-1] > last or (after is not None and ids[0] <= after)):
        raise ProtocolError("INVALID_RESPONSE", "Snapshot messages are outside the requested range")
    if data["has_more"]:
        validate_id(data["next_after_id"], "next_after_id")
        if len(ids) != limit or data["next_after_id"] != ids[-1] or ids[-1] >= last:
            raise ProtocolError("INVALID_RESPONSE", "Snapshot continuation must advance within its range")
    elif data["next_after_id"] is not None:
        raise ProtocolError("INVALID_RESPONSE", "Completed snapshot must not have a next cursor")


# Check room ownership, ordered patches and returned cursors before UI application.
# Computing expected bounds from delivered IDs prevents skipping unseen messages.
def validate_sync_response(data: dict[str, Any], request: dict[str, Any]) -> None:
    if not {"oldest_id", "latest_id", "pinned_message", "effect"} <= data.keys():
        raise ProtocolError("INVALID_RESPONSE", "History response is missing required fields")
    if data.get("room_key") != request["room_key"]:
        raise ProtocolError("INVALID_RESPONSE", "History belongs to an unexpected room")
    for key in ("has_more_older", "has_more_new", "reload_required"):
        if type(data.get(key)) is not bool:
            raise ProtocolError("INVALID_RESPONSE", f"History {key} must be a boolean")
    validate_id(data.get("room_revision"), "room_revision", zero=True)
    validate_id(data.get("effect_revision"), "effect_revision", zero=True)
    if data["oldest_id"] is not None:
        validate_id(data["oldest_id"], "oldest_id")
    validate_id(data["latest_id"], "latest_id", zero=True)
    _validate_pin(data, request["room_key"])
    if data.get("effect") is not None or data["effect_revision"] != 0:
        raise ProtocolError("INVALID_RESPONSE", "This client build does not support sound state yet")
    expected_reload = request["room_revision"] is not None and request["room_revision"] != data["room_revision"]
    if data["reload_required"] != expected_reload:
        raise ProtocolError("INVALID_RESPONSE", "Reload flag does not match the requested revision")
    arrays = []
    for key, limit in (("older_messages", request["old_count"]), ("new_messages", 100)):
        values = data.get(key)
        if type(values) is not list or len(values) > limit:
            raise ProtocolError("INVALID_RESPONSE", "Invalid history batch size")
        ids = [validate_message(message, request["room_key"])["id"] for message in values]
        if ids != sorted(set(ids)):
            raise ProtocolError("INVALID_RESPONSE", "History IDs must be unique and increasing")
        arrays.append(ids)
    older, newer = arrays
    if not data["reload_required"] and data["has_more_new"] and len(newer) != 100:
        raise ProtocolError("INVALID_RESPONSE", "Catch-up flag requires a full new-message batch")
    oldest, latest = request["oldest_id"], request["latest_id"]
    if data["reload_required"]:
        if older or newer:
            raise ProtocolError("INVALID_RESPONSE", "Reload response must not include history patches")
    elif latest is None:
        if newer:
            raise ProtocolError("INVALID_RESPONSE", "Initial history must be in older_messages")
        oldest, latest = (older[0], older[-1]) if older else (None, 0)
    else:
        if ((older and (oldest is None or older[-1] >= oldest)) or (newer and newer[0] <= latest)):
            raise ProtocolError("INVALID_RESPONSE", "History patch is outside its requested bounds")
        oldest = older[0] if older else oldest
        if newer:
            oldest = oldest if oldest is not None else newer[0]
            latest = newer[-1]
    if data.get("oldest_id") != oldest or data.get("latest_id") != latest:
        raise ProtocolError("INVALID_RESPONSE", "History cursors do not match delivered messages")


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
    request_id: int | None, code: str, message: str, *, details: dict | None = None
) -> dict[str, Any]:
    response = {
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }
    if details is not None:
        response["error"]["details"] = details
    return response


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
