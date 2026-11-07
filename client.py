"""Tkinter username-only client; socket I/O lives in client_network.py."""

from __future__ import annotations

import argparse
import queue
import time
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from client_network import NetworkClient
from common.protocol import ProtocolError, validate_content, validate_username


# Schedule the next directory request after the previous snapshot is received.
DIRECTORY_POLL_INTERVAL = 3.0
# Only the currently open room is polled; history stays in client memory.
ROOM_POLL_INTERVAL = 1.0


class ClientWindow(tk.Tk):
    """Update widgets only on Tkinter's main thread."""

    # Run on the main thread to create the window, connection form, and log.
    # Construction schedules queue polling but does not open a TCP connection.
    # Button callbacks enqueue work; only this thread reads or changes widgets.
    def __init__(self, host: str = "127.0.0.1", port: int = 5000):
        super().__init__()
        self.title("Socket Chat")
        self.minsize(880, 600)
        # A new worker is created for each explicit connection attempt.
        self.network: NetworkClient | None = None
        # UI state controls buttons; only this thread reads or changes it.
        self._state = "disconnected"
        # Prevent new connections while close() waits for the worker to exit.
        self._closing = False
        # Editable server address; connect() passes a plain string to the worker.
        self.host = tk.StringVar(value=host)
        # Port stays text until connect() validates and converts it to an integer.
        self.port = tk.StringVar(value=str(port))
        # Username is sent as entered; login creates or reuses its persistent ID.
        self.username = tk.StringVar()
        # The validated server identity is present only after a successful join.
        self.user: dict | None = None
        # None while disconnected or refreshing; only one directory poll may wait.
        self._next_directory_poll: float | None = None
        # Visible online count includes this user and excludes duplicate sessions.
        self.directory_status = tk.StringVar(value="Online users (0)")
        # Exactly one UI request is outstanding, so its action identifies a reply.
        self._pending_action: str | None = None
        # Capture room generation at send time to discard replies after switching.
        self._pending_generation = 0
        # Incremented whenever the current room is cleared or replaced.
        self._room_generation = 0
        # Keep only the latest requested target while an earlier request finishes.
        self._queued_target: int | None = None
        # Room metadata and messages belong only to the currently open conversation.
        self._room: dict | None = None
        # Global IDs deduplicate patches without keeping other rooms in memory.
        self._messages: dict[int, dict] = {}
        # Cursors come from sync replies, never from send acknowledgements.
        self._oldest_id: int | None = None
        # Latest delivered history ID, including own messages only after syncing.
        self._latest_id: int | None = None
        # Applied room-state revision; special-feature mutations are still pending.
        self._room_revision: int | None = None
        # Older history is loaded on demand; new history uses the room poll timer.
        self._has_more_older = False
        # Monotonic deadline for the next active-room poll; None while it is pending.
        self._next_room_poll: float | None = None
        # Room heading is separate from connection status and diagnostic activity.
        self.room_title = tk.StringVar(value="Select an online user and open a chat")
        # Status label updated by callbacks and _poll_events() on the main thread.
        self.status = tk.StringVar(value="Disconnected")

        # Main container; the host field and log expand when the window grows.
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)

        ttk.Label(frame, text="Server host").grid(row=0, column=0, sticky="w", padx=(0, 8))
        # Address input becomes read-only while its connection is in use.
        self.host_entry = ttk.Entry(frame, textvariable=self.host)
        self.host_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(frame, text="Port").grid(row=0, column=2, padx=(16, 8))
        # Port input is kept separate so validation can focus the incorrect field.
        self.port_entry = ttk.Entry(frame, textvariable=self.port, width=8)
        self.port_entry.grid(row=0, column=3, sticky="ew")

        ttk.Label(frame, text="Username").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(12, 0))
        # A single username field replaces separate registration/password forms.
        self.username_entry = ttk.Entry(frame, textvariable=self.username)
        self.username_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(12, 0))

        # Keep connection controls together and separate from the expanding log.
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=4, sticky="w", pady=12)
        # Starts a new worker only when the previous connection has ended.
        self.connect_button = ttk.Button(buttons, text="Join", command=self.connect)
        self.connect_button.pack(side="left")
        # Cancels the worker while leaving the window available for reconnecting.
        self.disconnect_button = ttk.Button(buttons, text="Disconnect", command=self.disconnect)
        self.disconnect_button.pack(side="left", padx=8)
        # Enabled only when connected and no request is awaiting its response.
        self.ping_button = ttk.Button(buttons, text="Send Ping", command=self.ping)
        self.ping_button.pack(side="left")
        # Room switches may be requested while a previous read is still finishing.
        self.open_button = ttk.Button(buttons, text="Open Chat", command=self.open_chat)
        self.open_button.pack(side="left", padx=(8, 0))

        ttk.Label(frame, textvariable=self.status).grid(row=3, column=0, columnspan=4, sticky="w")
        # Keep the online directory beside the expanding activity area.
        content = ttk.Frame(frame)
        content.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=(12, 0))
        content.columnconfigure(1, weight=1)
        content.rowconfigure(1, weight=1)
        ttk.Label(content, textvariable=self.directory_status).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(content, text="Private chat").grid(row=0, column=1, sticky="w", pady=(0, 4))
        # Native list/scrollbar controls keep large online lists keyboard-accessible.
        people = ttk.Frame(content)
        people.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        # Stable item IDs preserve selection across full directory snapshots.
        self.online_users = ttk.Treeview(people, show="tree", height=12, selectmode="browse")
        self.online_users.column("#0", width=160, minwidth=120, stretch=False)
        self.online_users.pack(side="left", fill="both", expand=True)
        # Scroll only the online list, independently of activity output.
        online_scroll = ttk.Scrollbar(people, orient="vertical", command=self.online_users.yview)
        online_scroll.pack(side="right", fill="y")
        self.online_users.configure(yscrollcommand=online_scroll.set)
        # Chat grows with the window; the activity log stays a small diagnostic area.
        conversation = ttk.Frame(content)
        conversation.grid(row=1, column=1, sticky="nsew")
        conversation.columnconfigure(0, weight=1)
        conversation.rowconfigure(1, weight=1)
        # Keep the room name and older-history control above the chat view.
        header = ttk.Frame(conversation)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(header, textvariable=self.room_title).pack(side="left")
        # Ask for the nearest twenty messages before the current oldest cursor.
        self.older_button = ttk.Button(header, text="Load older (20)", command=self.load_older)
        self.older_button.pack(side="right")
        # Only sync responses populate this chronological chat view.
        self.chat = ScrolledText(conversation, width=52, height=12, wrap="word", state="disabled")
        self.chat.grid(row=1, column=0, sticky="nsew")
        # Message input remains separate from read-only history and diagnostic logs.
        composer = ttk.Frame(conversation)
        composer.grid(row=2, column=0, sticky="ew", pady=8)
        composer.columnconfigure(0, weight=1)
        # Multiline input is retained on errors and locked until a send is confirmed.
        self.message_input = tk.Text(composer, height=3, wrap="word")
        self.message_input.grid(row=0, column=0, sticky="ew")
        self.message_input.bind("<Control-Return>", lambda event: (self.send(), "break")[1])
        self.send_button = ttk.Button(composer, text="Send", command=self.send)
        self.send_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(conversation, text="Activity").grid(row=3, column=0, sticky="w")
        # Read-only output; _log() temporarily enables edits to append messages.
        self.activity = ScrolledText(conversation, width=52, height=5, wrap="word", state="disabled")
        self.activity.grid(row=4, column=0, sticky="ew")
        self._set_state("disconnected")
        self.username_entry.focus_set()
        self.protocol("WM_DELETE_WINDOW", self.close)
        # Tk callback ID lets close() cancel a pending poll before destroying UI.
        self._poll_id: str | None = self.after(50, self._poll_events)

    # Called by Join; validate the address and username before starting a worker.
    # After TCP connects, _poll_events() queues the username-only login request.
    # An existing worker must finish before another connection can be started.
    def connect(self) -> None:
        if self.network is not None or self._closing:
            return
        host = self.host.get().strip()
        try:
            port = int(self.port.get())
            if not host or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.status.set("Enter a server host and a port between 1 and 65535.")
            self._log("Invalid server address.")
            (self.host_entry if not host else self.port_entry).focus_set()
            return
        try:
            validate_username(self.username.get())
        except ProtocolError as exc:
            self.status.set(exc.message)
            self._log(exc.message)
            self.username_entry.focus_set()
            return
        self.network = NetworkClient(host, port)
        self._set_state("connecting")
        self._log(f"Connecting to {host}:{port}...")
        self.network.start()

    # Called by Send Ping; enqueue the action and an empty payload for the worker.
    # Mark the UI as waiting so repeated clicks cannot queue duplicate pings.
    # _poll_events() restores the connected state when a response arrives.
    def ping(self) -> None:
        if self.network is None or self._state != "connected":
            return
        self._log("Sending ping...")
        self._request("ping", {}, "waiting")

    # Record response context before queuing work on the one network connection.
    # Tkinter owns this bookkeeping; the network module still has no UI knowledge.
    def _request(self, action: str, payload: dict, state: str) -> None:
        self._pending_action = action
        self._pending_generation = self._room_generation
        self._set_state(state)
        self.network.requests.put((action, payload))

    # Select a destination from online users without cancelling in-flight TCP I/O.
    # Clear the old room immediately; the scheduler opens only the latest target.
    def open_chat(self) -> None:
        selected = self.online_users.selection()
        if self.user is None or not selected or self._state in {"disconnecting", "closing"}:
            return
        target = int(selected[0].removeprefix("user-"))
        if target == self.user["id"]:
            self._log("Choose another user for a private chat.")
            return
        name = self.online_users.item(selected[0], "text")
        self._clear_room()
        self._queued_target = target
        self.room_title.set(f"Opening chat with {name}...")
        self._set_state(self._state)
        self._schedule_requests()

    # Validate content locally, then let the server validate and persist it again.
    # Do not append the acknowledgement: sync may contain earlier unseen messages.
    def send(self) -> None:
        if self._room is None or self._state != "connected":
            return
        try:
            content = validate_content(self.message_input.get("1.0", "end-1c"))
        except ProtocolError as exc:
            self._log(exc.message)
            return
        self._request("send_message", {"room_key": self._room["room_key"], "content": content}, "sending")

    # Request older messages only when a room has more history and the socket is idle.
    def load_older(self) -> None:
        if self._room is not None and self._state == "connected" and self._has_more_older:
            self._sync_room(20)

    # Include both history bounds; initial open has null cursors and asks for twenty.
    def _sync_room(self, old_count: int) -> None:
        self._next_room_poll = None
        self._request("sync_room", {"room_key": self._room["room_key"], "old_count": old_count,
                                   "oldest_id": self._oldest_id, "latest_id": self._latest_id,
                                   "room_revision": self._room_revision}, "syncing")

    # Discard cached history on room change or disconnect; no other rooms are retained.
    def _clear_room(self) -> None:
        self._room_generation += 1
        self._queued_target = None
        self._room = None
        self._messages.clear()
        self._oldest_id = self._latest_id = self._room_revision = None
        self._has_more_older = False
        self._next_room_poll = None
        self.room_title.set("Select an online user and open a chat")
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.message_input.configure(state="normal")
        self.message_input.delete("1.0", "end")

    # Merge only validated history patches, retaining the user's visible anchor.
    # Replies from an earlier room generation are filtered before this is called.
    def _apply_room_sync(self, data: dict) -> None:
        if data["reload_required"]:
            self._log("Room state changed; reopen this chat. Revision reload is not implemented yet.")
            self._clear_room()
            return
        top_line, top_column = (int(part) for part in self.chat.index("@0,0").split("."))
        at_bottom = self.chat.yview()[1] >= 0.999
        older = [message for message in data["older_messages"] if message["id"] not in self._messages]
        prepend_lines = sum(f"{message['sender']['username']}: {message['content']}\n".count("\n") for message in older)
        had_messages = bool(self._messages)
        for message in data["older_messages"] + data["new_messages"]:
            self._messages[message["id"]] = message
        self._oldest_id, self._latest_id = data["oldest_id"], data["latest_id"]
        self._room_revision = data["room_revision"]
        self._has_more_older = data["has_more_older"]
        self._next_room_poll = time.monotonic() + (0 if data["has_more_new"] else ROOM_POLL_INTERVAL)
        if data["older_messages"] or data["new_messages"]:
            self.chat.configure(state="normal")
            self.chat.delete("1.0", "end")
            for message in sorted(self._messages.values(), key=lambda item: item["id"]):
                self.chat.insert("end", f"{message['sender']['username']}: {message['content']}\n")
            if had_messages and older:
                self.chat.yview(f"{top_line + prepend_lines}.{top_column}")
            elif at_bottom:
                self.chat.see("end")
            else:
                self.chat.yview(f"{top_line}.{top_column}")
            self.chat.configure(state="disabled")

    # Signal the current worker to stop without closing its socket from the UI.
    # Leave the event loop running; _poll_events() notices when cleanup finishes.
    # Reconnecting is enabled only after the previous worker has exited.
    def disconnect(self) -> None:
        if self.network is not None:
            self._set_state("disconnecting")
            self.network.stop()

    # Handle window close on the main thread and prevent further connections.
    # If no worker remains, cancel the pending poll and destroy the window now.
    # Otherwise signal cancellation; _poll_events() destroys it after worker exit.
    def close(self) -> None:
        self._closing = True
        if self.network is None:
            if self._poll_id is not None:
                self.after_cancel(self._poll_id)
            self.destroy()
        else:
            self._set_state("closing")
            self.network.stop()

    # Match the single pending request, then apply only the active room generation.
    # Background reads never overwrite another room, and send acknowledgements
    # only schedule a sync rather than advancing history past unseen messages.
    def _handle_response(self, response: dict) -> None:
        action, generation = self._pending_action, self._pending_generation
        self._pending_action = None
        if self._state in {"disconnecting", "closing"}:
            return
        if action in {"open_private", "send_message", "sync_room"} and generation != self._room_generation:
            self._set_state("connected")
            return
        if not response["ok"]:
            self._log(f"{response['error']['code']}: {response['error']['message']}")
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
                self._log(f"Joined as {self.user['username']} (user #{self.user['id']}).")
                self._next_directory_poll = time.monotonic()
            elif action == "get_directory":
                self._update_directory(data["online_users"])
                self._next_directory_poll = time.monotonic() + DIRECTORY_POLL_INTERVAL
            elif action == "open_private":
                self._room = data["room"]
                partner = next(member for member in self._room["members"] if member["id"] != self.user["id"])
                self.room_title.set(f"Chat with {partner['username']}")
                self._next_room_poll = time.monotonic()
            elif action == "send_message":
                self.message_input.configure(state="normal")
                self.message_input.delete("1.0", "end")
                self._next_room_poll = time.monotonic()
            elif action == "sync_room":
                self._apply_room_sync(data)
            else:
                self._log(f"Response #{response['request_id']}: {data}")
        self._set_state("connected")

    # User-requested room switches precede background polls; only one is sent.
    # Directory polling is serviced before room polling to avoid starving presence.
    def _schedule_requests(self) -> None:
        if self.network is None or self._state != "connected":
            return
        if self._queued_target is not None:
            target = self._queued_target
            self._queued_target = None
            self._request("open_private", {"target_user_id": target}, "opening")
        else:
            self._poll_directory()
            if (self._state == "connected" and self._room is not None and self._next_room_poll is not None
                    and time.monotonic() >= self._next_room_poll):
                self._sync_room(20 if self._latest_id is None else 0)

    # Run every 50 ms on the Tkinter thread and drain events without waiting.
    # Apply responses/errors to widgets; the worker never calls Tkinter itself.
    # Check thread completion before draining so its final events are not lost.
    # Join only an exited worker, then allow reconnecting or finish window close.
    def _poll_events(self) -> None:
        self._poll_id = None
        if self.network is not None:
            finished = not self.network.is_alive()
            while True:
                try:
                    kind, value = self.network.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "connected":
                    self._log("TCP connected.")
                    if self._state == "connecting":
                        self._request("login", {"username": self.username.get()}, "joining")
                elif kind == "response":
                    self._handle_response(value)
                elif kind == "error":
                    self._log(f"Network error: {value}")
            if finished:
                self.network.join()
                self.network = None
                self.user = None
                self._pending_action = None
                self._clear_room()
                self._next_directory_poll = None
                self._update_directory([])
                if self._closing:
                    self.destroy()
                    return
                self._set_state("disconnected")
                self._log("Disconnected.")
        self._schedule_requests()
        self._poll_id = self.after(50, self._poll_events)

    # Poll immediately after login and then after each interval, only while idle.
    # A ping already in flight takes precedence; missed polls are not accumulated.
    # Reuse the network queue and existing Tk callback rather than another thread.
    def _poll_directory(self) -> None:
        if (self.network is not None and self._state == "connected" and self._queued_target is None
                and self._next_directory_poll is not None
                and time.monotonic() >= self._next_directory_poll):
            self._next_directory_poll = None
            self._request("get_directory", {}, "refreshing")

    # Apply a validated full snapshot on the main thread, marking this user's row.
    # Reuse existing Treeview items to retain selection and remove offline users.
    # The server has already sorted/deduplicated identities across connections.
    def _update_directory(self, users: list[dict]) -> None:
        removed = set(self.online_users.get_children())
        for position, user in enumerate(users):
            key = f"user-{user['id']}"
            own = self.user is not None and user["id"] == self.user["id"]
            text = user["username"] + (" (you)" if own else "")
            if self.online_users.exists(key):
                self.online_users.item(key, text=text)
            else:
                self.online_users.insert("", "end", iid=key, text=text)
            self.online_users.move(key, "", position)
            removed.discard(key)
        for key in removed:
            self.online_users.delete(key)
        self.directory_status.set(f"Online users ({len(users)})")

    # Keep status text, button availability, and form editability in sync.
    # Only the disconnected state allows a new address or connection attempt.
    # Waiting disables Ping while Disconnect remains available for cancellation.
    def _set_state(self, state: str) -> None:
        self._state = state
        self.status.set({
            "waiting": "Waiting for pong...", "joining": "Joining...",
            "refreshing": "Refreshing online users...",
            "opening": "Opening private chat...", "syncing": "Loading messages...",
            "sending": "Sending message...",
            "connected": f"Joined as {self.user['username']}" if self.user else "Connected",
        }.get(state, state.capitalize()))
        self.connect_button.configure(state="normal" if state == "disconnected" else "disabled")
        self.disconnect_button.configure(
            state="normal" if state not in {"disconnected", "disconnecting", "closing"} else "disabled"
        )
        self.ping_button.configure(state="normal" if state == "connected" else "disabled")
        active = self.user is not None and state not in {"disconnected", "disconnecting", "closing"}
        self.open_button.configure(state="normal" if active else "disabled")
        can_send = active and self._room is not None and state == "connected"
        self.send_button.configure(state="normal" if can_send else "disabled")
        self.older_button.configure(state="normal" if can_send and self._has_more_older else "disabled")
        self.message_input.configure(state="normal" if active and self._room is not None and state != "sending" else "disabled")
        entry_state = "normal" if state == "disconnected" else "readonly"
        self.host_entry.configure(state=entry_state)
        self.port_entry.configure(state=entry_state)
        self.username_entry.configure(state=entry_state)

    # Append activity on the main thread, then restore the read-only widget state.
    # Limit each entry and retained line count so the log cannot grow indefinitely.
    # Scroll to the newest entry while still allowing text selection and copying.
    def _log(self, message: str) -> None:
        self.activity.configure(state="normal")
        self.activity.insert("end", message[:2000] + "\n")
        line_count = int(self.activity.index("end-1c").split(".")[0])
        if line_count > 200:
            self.activity.delete("1.0", f"{line_count - 199}.0")
        self.activity.see("end")
        self.activity.configure(state="disabled")


# Parse the initial server address and run Tkinter on the process's main thread.
# Report display startup errors through argparse instead of starting a worker.
# On terminal interruption or normal exit, stop/join any worker before cleanup.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    try:
        window = ClientWindow(args.host, args.port)
    except tk.TclError as exc:
        parser.exit(1, f"Cannot open client window: {exc}\n")
    try:
        window.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        if window.network is not None:
            window.network.stop()
            window.network.join()
            window.network = None
        try:
            window.close()
        except tk.TclError:
            pass


if __name__ == "__main__":
    main()
