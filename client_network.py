"""Client TCP worker and queues; independent of Tkinter."""

from __future__ import annotations

import queue
import socket
import threading
import time
from typing import Any

from common.protocol import (
    MAX_REQUEST_BYTES,
    MAX_REQUEST_ID,
    MAX_RESPONSE_BYTES,
    FrameTooLarge,
    LineFramer,
    ProtocolError,
    decode_document,
    encode_message,
    validate_directory,
    validate_group_name,
    validate_message,
    validate_mutation_response,
    validate_response,
    validate_room,
    validate_sync_response,
    validate_snapshot_response,
    validate_id,
    validate_user,
)


class NetworkClient(threading.Thread):
    """Own one connection; process queued requests and publish results in order."""

    # Prepare one worker's connection settings, queues, and cancellation Event.
    # Logic constructs this worker; start() runs socket I/O inside run().
    # Use a new instance for reconnecting; a Thread instance cannot start twice.
    def __init__(self, host: str, port: int, *, connect_timeout: float = 5.0,
                 request_timeout: float = 10.0):
        super().__init__(name="chat-client-network")
        # Server hostname or IP passed to create_connection() inside run().
        self.host = host
        # Server TCP port, already validated by application logic.
        self.port = port
        # Socket connection timeout; hostname lookup is handled by the OS.
        self.connect_timeout = connect_timeout
        # Send timeout and total response-wait limit used by _exchange().
        self.request_timeout = request_timeout
        # Logic queues (action, payload); None wakes the worker for shutdown.
        self.requests: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        # Logic consumes connected, response, error, and disconnected events.
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        # Checked between short recv waits so Disconnect does not freeze the UI.
        self._stopping = threading.Event()

    # Request shutdown from logic without operating on the worker socket.
    # The Event cancels an active receive loop; None wakes a blocked queue.get().
    # Returns immediately; logic checks for thread exit on each UI tick.
    def stop(self) -> None:
        self._stopping.set()
        self.requests.put(None)

    # Thread.start() calls this method on the network thread, never the UI thread.
    # Own the socket in a with block, consume queued (action, payload) requests,
    # and allocate increasing IDs locally for this connection, starting at 1.
    # Publish validated responses or connection errors through the events queue.
    # Errors/cancellation close the socket; always publish disconnected on exit.
    # No request or connection is retried automatically after an uncertain result.
    def run(self) -> None:
        try:
            with socket.create_connection((self.host, self.port), self.connect_timeout) as connection:
                if self._stopping.is_set():
                    return
                self.events.put(("connected", None))
                request_id = 0
                user_id = None
                while not self._stopping.is_set():
                    command = self.requests.get()
                    if command is None or self._stopping.is_set():
                        return
                    if request_id == MAX_REQUEST_ID:
                        raise ProtocolError("INVALID_REQUEST_ID", "Request IDs exhausted; reconnect")
                    request_id += 1
                    action, payload = command
                    response = self._exchange(connection, {
                        "request_id": request_id, "action": action, "payload": payload,
                    })
                    if response is not None:
                        if action == "login" and response["ok"]:
                            user_id = validate_user(response["data"].get("user"))["id"]
                        elif action == "get_directory" and response["ok"]:
                            validate_directory(response["data"])
                        elif action == "logout" and response["ok"]:
                            user_id = None
                        elif action == "open_private" and response["ok"]:
                            room = validate_room(response["data"].get("room"))
                            if (room["kind"] != "private"
                                    or {member["id"] for member in room["members"]} != {user_id, payload["target_user_id"]}
                                    or type(response["data"].get("created")) is not bool):
                                raise ProtocolError("INVALID_RESPONSE", "Private room has unexpected participants")
                        elif action in {"create_group", "join_group"} and response["ok"]:
                            room = validate_room(response["data"].get("room"))
                            ids = [member["id"] for member in room["members"]]
                            if room["kind"] != "group" or user_id not in ids:
                                raise ProtocolError("INVALID_RESPONSE", "Group response must include this user")
                            if action == "create_group":
                                if (ids != [user_id] or room["created_by_user_id"] != user_id
                                        or room["name"] != validate_group_name(payload["name"])):
                                    raise ProtocolError("INVALID_RESPONSE", "Created group has unexpected name or members")
                            elif room["room_key"] != payload["room_key"] or type(response["data"].get("joined")) is not bool:
                                raise ProtocolError("INVALID_RESPONSE", "Invalid group join response")
                        elif action == "send_message" and response["ok"]:
                            message = validate_message(response["data"].get("message"), payload["room_key"])
                            if message["sender"]["id"] != user_id:
                                raise ProtocolError("INVALID_RESPONSE", "Message acknowledgement has an unexpected sender")
                        elif action == "sync_room" and response["ok"]:
                            validate_sync_response(response["data"], payload)
                        elif action == "get_room_snapshot":
                            if response["ok"]:
                                validate_snapshot_response(response["data"], payload)
                            elif response["error"]["code"] == "SNAPSHOT_STALE":
                                revision = response["error"].get("details", {}).get("room_revision")
                                validate_id(revision, "room_revision", zero=True)
                                if revision == payload["room_revision"]:
                                    raise ProtocolError("INVALID_RESPONSE", "Stale snapshot must report a different revision")
                        elif action in {"set_heart", "set_pin"} and response["ok"]:
                            validate_mutation_response(response["data"], payload, action)
                        self.events.put(("response", response))
        except (OSError, ProtocolError, FrameTooLarge, ValueError) as exc:
            if not self._stopping.is_set():
                self.events.put(("error", str(exc)))
        finally:
            self.events.put(("disconnected", None))

    # Called only by run(): send one bounded JSON line and wait for its response.
    # recv() may return part of a frame; LineFramer assembles bytes before parsing.
    # Short receive waits allow cancellation; partial bytes do not extend the
    # total response deadline, which starts after the full request is sent.
    # Reject extra buffered data or a mismatched response; run() then closes TCP.
    # Return None when cancellation is requested instead of publishing a result.
    def _exchange(self, connection: socket.socket, request: dict[str, Any]) -> dict[str, Any] | None:
        connection.settimeout(self.request_timeout)
        connection.sendall(encode_message(request, max_bytes=MAX_REQUEST_BYTES))
        deadline = time.monotonic() + self.request_timeout
        framer = LineFramer(MAX_RESPONSE_BYTES)
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Response timed out; the request outcome is unknown")
            connection.settimeout(min(0.2, remaining))
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("Server closed the connection before a complete response")
            if time.monotonic() >= deadline:
                raise TimeoutError("Response timed out; the request outcome is unknown")
            for frame in framer.feed(chunk):
                if framer.has_pending:
                    raise ProtocolError("INVALID_RESPONSE", "Received data beyond the expected response")
                return validate_response(decode_document(frame), request["request_id"])
        return None
