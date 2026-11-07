"""Client application state, actions and polling; independent of Tkinter."""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from typing import Any

from client_network import NetworkClient
from common.protocol import ProtocolError, validate_content, validate_username


# Start the next directory poll after the previous response, without stacking.
DIRECTORY_POLL_INTERVAL = 3.0
# Only the active room is polled and retained in memory.
ROOM_POLL_INTERVAL = 1.0


class ClientLogic:
    """Run on the main thread; emit display changes without knowing any widgets.

    The owner calls actions and poll(), reads state, and handles synchronous
    events: state, log, input_error, directory, room_cleared, room_title,
    messages (new older messages, previous-history flag), sent and closed.
    Only NetworkClient performs socket I/O on its existing worker thread.
    """

    # Prepare plain application data; do not open a connection during construction.
    # emit() runs synchronously on the calling thread, so the window can update
    # widgets directly without a second queue between UI and application logic.
    def __init__(self, emit: Callable[[str, Any], None]):
        # Receives presentation events after the corresponding data is updated.
        self._emit = emit
        # One worker per explicit connection attempt, replaced after it exits.
        self.network: NetworkClient | None = None
        # Drives action guards and the window's enabled/disabled controls.
        self.state = "disconnected"
        # Prevents reconnecting while the window waits for worker shutdown.
        self.closing = False
        # Captured during Join, then sent when TCP connection succeeds.
        self._username = ""
        # Validated server identity, present only after successful login.
        self.user: dict | None = None
        # Next directory deadline; None while a directory request is pending.
        self._next_directory_poll: float | None = None
        # Exactly one application request is outstanding on the connection.
        self._pending_action: str | None = None
        # Room generation captured when the outstanding request was sent.
        self._pending_generation = 0
        # Incremented on room clear/switch to reject replies for earlier rooms.
        self._room_generation = 0
        # Latest requested target takes priority after in-flight work finishes.
        self._queued_target: int | None = None
        # Metadata for the single active conversation; other rooms are not cached.
        self.room: dict | None = None
        # Deduplicate history patches by global message ID within the active room.
        self.messages: dict[int, dict] = {}
        # History bounds come only from sync replies, never send acknowledgements.
        self.oldest_id: int | None = None
        # Latest delivered history ID, including own messages after sync only.
        self.latest_id: int | None = None
        # Applied revision; special-feature snapshot reload remains unimplemented.
        self.room_revision: int | None = None
        # Controls whether an older-history action can request another batch.
        self.has_more_older = False
        # Active-room poll deadline; None while a room sync is pending.
        self._next_room_poll: float | None = None

    # Validate plain form values before allocating a network worker.
    # Capture the username now; TCP connection completion queues login later.
    # An existing worker must finish before another connection can be started.
    def connect(self, host: str, port: str, username: str) -> None:
        if self.network is not None or self.closing:
            return
        host = host.strip()
        try:
            port_number = int(port)
            if not host or not 1 <= port_number <= 65535:
                raise ValueError
        except ValueError:
            self._emit("input_error", (
                "host" if not host else "port",
                "Enter a server host and a port between 1 and 65535.",
                "Invalid server address.",
            ))
            return
        try:
            validate_username(username)
        except ProtocolError as exc:
            self._emit("input_error", ("username", exc.message, exc.message))
            return
        self._username = username
        self.network = NetworkClient(host, port_number)
        self._set_state("connecting")
        self._emit("log", f"Connecting to {host}:{port_number}...")
        self.network.start()

    # Queue one ping only while idle; state guards reject repeated clicks.
    def ping(self) -> None:
        if self.network is None or self.state != "connected":
            return
        self._emit("log", "Sending ping...")
        self._request("ping", {}, "waiting")

    # Record reply context before enqueuing work on the one network connection.
    # Only this main-thread logic changes application request bookkeeping.
    def _request(self, action: str, payload: dict, state: str) -> None:
        self._pending_action = action
        self._pending_generation = self._room_generation
        self._set_state(state)
        self.network.requests.put((action, payload))

    # Clear the old room immediately and retain only the latest requested target.
    # A room switch does not cancel the response already being received over TCP.
    def open_chat(self, target: int, name: str) -> None:
        if self.user is None or self.state in {"disconnecting", "closing"}:
            return
        if target == self.user["id"]:
            self._emit("log", "Choose another user for a private chat.")
            return
        self._clear_room()
        self._queued_target = target
        self._emit("room_title", f"Opening chat with {name}...")
        self._set_state(self.state)
        self._schedule_requests()

    # Validate message text locally; the server validates and persists it again.
    # A send acknowledgement schedules sync instead of advancing history cursors.
    def send(self, content: str) -> None:
        if self.room is None or self.state != "connected":
            return
        try:
            content = validate_content(content)
        except ProtocolError as exc:
            self._emit("log", exc.message)
            return
        self._request("send_message", {"room_key": self.room["room_key"], "content": content}, "sending")

    # Request older messages only when history remains and the connection is idle.
    def load_older(self) -> None:
        if self.room is not None and self.state == "connected" and self.has_more_older:
            self._sync_room(20)

    # Include both history bounds; an initial open uses null cursors and twenty.
    def _sync_room(self, old_count: int) -> None:
        self._next_room_poll = None
        self._request("sync_room", {"room_key": self.room["room_key"], "old_count": old_count,
                                   "oldest_id": self.oldest_id, "latest_id": self.latest_id,
                                   "room_revision": self.room_revision}, "syncing")

    # Drop the active-room cache on room switch or disconnect and notify the view.
    # Incrementing generation makes already-pending replies harmless to the new room.
    def _clear_room(self) -> None:
        self._room_generation += 1
        self._queued_target = None
        self.room = None
        self.messages.clear()
        self.oldest_id = self.latest_id = self.room_revision = None
        self.has_more_older = False
        self._next_room_poll = None
        self._emit("room_cleared", None)

    # Merge validated patches, deduplicate messages and apply server-delivered bounds.
    # Tell the view which older messages were added so it can retain its scroll anchor.
    def _apply_room_sync(self, data: dict) -> None:
        if data["reload_required"]:
            self._emit("log", "Room state changed; reopen this chat. Revision reload is not implemented yet.")
            self._clear_room()
            return
        older = [message for message in data["older_messages"] if message["id"] not in self.messages]
        had_messages = bool(self.messages)
        for message in data["older_messages"] + data["new_messages"]:
            self.messages[message["id"]] = message
        self.oldest_id, self.latest_id = data["oldest_id"], data["latest_id"]
        self.room_revision = data["room_revision"]
        self.has_more_older = data["has_more_older"]
        self._next_room_poll = time.monotonic() + (0 if data["has_more_new"] else ROOM_POLL_INTERVAL)
        if data["older_messages"] or data["new_messages"]:
            self._emit("messages", (older, had_messages))

    # Request cancellation without operating on the worker's socket or blocking.
    # poll() observes worker completion and makes reconnecting available afterward.
    def disconnect(self) -> None:
        if self.network is not None:
            self._set_state("disconnecting")
            self.network.stop()

    # Stop accepting new connection attempts and close after the worker has exited.
    # The closed event lets the window cancel its Tk timer and destroy widgets.
    def close(self) -> None:
        self.closing = True
        if self.network is None:
            self._emit("closed", None)
        else:
            self._set_state("closing")
            self.network.stop()

    # Final process cleanup when the Tk main loop can no longer call poll().
    # Unlike close(), this waits for worker exit and emits no widget updates.
    def shutdown(self) -> None:
        self.closing = True
        if self.network is not None:
            self.network.stop()
            self.network.join()
            self.network = None

    # Match the one pending action and discard results from an earlier room.
    # Definite errors preserve input; only a successful send clears the composer.
    def _handle_response(self, response: dict) -> None:
        action, generation = self._pending_action, self._pending_generation
        self._pending_action = None
        if self.state in {"disconnecting", "closing"}:
            return
        if action in {"open_private", "send_message", "sync_room"} and generation != self._room_generation:
            self._set_state("connected")
            return
        if not response["ok"]:
            self._emit("log", f"{response['error']['code']}: {response['error']['message']}")
            if action == "login":
                self.disconnect()
                return
            if action == "get_directory":
                self._next_directory_poll = time.monotonic() + DIRECTORY_POLL_INTERVAL
            if action == "open_private" or response["error"]["code"] in {"ROOM_NOT_FOUND", "NOT_ROOM_MEMBER"}:
                self._clear_room()
            elif action == "sync_room":
                self._next_room_poll = time.monotonic() + ROOM_POLL_INTERVAL
        else:
            data = response["data"]
            if action == "login":
                self.user = data["user"]
                self._emit("log", f"Joined as {self.user['username']} (user #{self.user['id']}).")
                self._next_directory_poll = time.monotonic()
            elif action == "get_directory":
                self._emit("directory", data["online_users"])
                self._next_directory_poll = time.monotonic() + DIRECTORY_POLL_INTERVAL
            elif action == "open_private":
                self.room = data["room"]
                partner = next(member for member in self.room["members"] if member["id"] != self.user["id"])
                self._emit("room_title", f"Chat with {partner['username']}")
                self._next_room_poll = time.monotonic()
            elif action == "send_message":
                self._emit("sent", None)
                self._next_room_poll = time.monotonic()
            elif action == "sync_room":
                self._apply_room_sync(data)
            else:
                self._emit("log", f"Response #{response['request_id']}: {data}")
        self._set_state("connected")

    # Prioritize the latest room switch, then directory and active-room polling.
    # State changes at enqueue time ensure that no second request is stacked.
    def _schedule_requests(self) -> None:
        if self.network is None or self.state != "connected":
            return
        if self._queued_target is not None:
            target = self._queued_target
            self._queued_target = None
            self._request("open_private", {"target_user_id": target}, "opening")
        else:
            self._poll_directory()
            if (self.state == "connected" and self.room is not None and self._next_room_poll is not None
                    and time.monotonic() >= self._next_room_poll):
                self._sync_room(20 if self.latest_id is None else 0)

    # Drain network events without waiting; the window calls this on its main thread.
    # Check completion before draining so final responses are not lost, then join
    # only an exited worker. No Tkinter calls or extra threads are needed here.
    def poll(self) -> None:
        if self.network is not None:
            finished = not self.network.is_alive()
            while True:
                try:
                    kind, value = self.network.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "connected":
                    self._emit("log", "TCP connected.")
                    if self.state == "connecting":
                        self._request("login", {"username": self._username}, "joining")
                elif kind == "response":
                    self._handle_response(value)
                elif kind == "error":
                    self._emit("log", f"Network error: {value}")
            if finished:
                self.network.join()
                self.network = None
                self.user = None
                self._pending_action = None
                self._clear_room()
                self._next_directory_poll = None
                self._emit("directory", [])
                if self.closing:
                    self._emit("closed", None)
                    return
                self._set_state("disconnected")
                self._emit("log", "Disconnected.")
        self._schedule_requests()

    # Poll after each completed directory response; missed polls never accumulate.
    # An existing user request takes precedence over this background refresh.
    def _poll_directory(self) -> None:
        if (self.network is not None and self.state == "connected" and self._queued_target is None
                and self._next_directory_poll is not None
                and time.monotonic() >= self._next_directory_poll):
            self._next_directory_poll = None
            self._request("get_directory", {}, "refreshing")

    # Publish the state after changing it so the view reads consistent action guards.
    def _set_state(self, state: str) -> None:
        self.state = state
        self._emit("state", None)
