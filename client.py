"""Tkinter window; application state and TCP I/O live in separate modules."""

from __future__ import annotations

import argparse
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from client_logic import ClientLogic


class ClientWindow(tk.Tk):
    """Read and update widgets on the main thread; delegate actions to logic."""

    # Create widgets and schedule the main-thread logic tick without connecting.
    # Logic sends presentation events to _on_event(), never touches a widget,
    # and keeps socket I/O on the existing network worker.
    def __init__(self, host: str = "127.0.0.1", port: int = 5000):
        super().__init__()
        self.title("Socket Chat")
        self.minsize(880, 600)
        # Owns connection lifecycle, room state, request ordering and polling.
        self.logic = ClientLogic(self._on_event)
        # Editable server address passed as plain text when Join is pressed.
        self.host = tk.StringVar(value=host)
        # Logic validates and converts the port before starting a connection.
        self.port = tk.StringVar(value=str(port))
        # Username-only entry; the window holds only the editable form value.
        self.username = tk.StringVar()
        # Presentation text for the online list, room header and connection state.
        self.directory_status = tk.StringVar(value="Online users (0)")
        # Updated from room-title events; room metadata belongs to logic.
        self.room_title = tk.StringVar(value="Select an online user and open a chat")
        # Updated by _render_state() or a form validation error.
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
        self._render_state()
        self.username_entry.focus_set()
        self.protocol("WM_DELETE_WINDOW", self.close)
        # Tk callback ID lets close() cancel a pending poll before destroying UI.
        self._poll_id: str | None = self.after(50, self._poll_events)

    # Read the form once; logic validates it and starts the connection lifecycle.
    def connect(self) -> None:
        self.logic.connect(self.host.get(), self.port.get(), self.username.get())

    # Forward the button action; logic decides whether the connection is idle.
    def ping(self) -> None:
        self.logic.ping()

    # Translate the selected row into plain target data for the logic module.
    # The widget selection and display label remain window responsibilities.
    def open_chat(self) -> None:
        selected = self.online_users.selection()
        if selected:
            target = int(selected[0].removeprefix("user-"))
            self.logic.open_chat(target, self.online_users.item(selected[0], "text"))

    # Read the composer; logic validates content and queues the send request.
    def send(self) -> None:
        self.logic.send(self.message_input.get("1.0", "end-1c"))

    # Forward the history action without computing or changing cursors in the UI.
    def load_older(self) -> None:
        self.logic.load_older()

    # Request cancellation while leaving Tkinter free to show cleanup progress.
    def disconnect(self) -> None:
        self.logic.disconnect()

    # Logic requests worker shutdown; the closed event destroys the window only
    # after the worker exits, or immediately when no connection exists.
    def close(self) -> None:
        self.logic.close()

    # Consume synchronous presentation events on the same thread as the widgets.
    # These events describe display changes; network responses are handled by logic.
    def _on_event(self, kind: str, value) -> None:
        if kind == "state":
            self._render_state()
        elif kind == "log":
            self._log(value)
        elif kind == "input_error":
            field, message, log_message = value
            self.status.set(message)
            self._log(log_message)
            getattr(self, f"{field}_entry").focus_set()
        elif kind == "directory":
            self._update_directory(value)
        elif kind == "room_cleared":
            self._clear_room()
        elif kind == "room_title":
            self.room_title.set(value)
        elif kind == "messages":
            self._render_messages(*value)
        elif kind == "sent":
            self.message_input.configure(state="normal")
            self.message_input.delete("1.0", "end")
        elif kind == "closed":
            if self._poll_id is not None:
                self.after_cancel(self._poll_id)
            self.destroy()

    # Clear presentation for the previous room; logic has already dropped its cache.
    def _clear_room(self) -> None:
        self.room_title.set("Select an online user and open a chat")
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.message_input.configure(state="normal")
        self.message_input.delete("1.0", "end")

    # Draw the merged history while retaining the user's visible text anchor.
    # Logic supplies newly prepended messages and whether history existed before.
    # Formatting, line counts and scrolling depend on widgets and stay here.
    def _render_messages(self, older: list[dict], had_messages: bool) -> None:
        top_line, top_column = (int(part) for part in self.chat.index("@0,0").split("."))
        at_bottom = self.chat.yview()[1] >= 0.999
        prepend_lines = sum(f"{message['sender']['username']}: {message['content']}\n".count("\n") for message in older)
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        for message in sorted(self.logic.messages.values(), key=lambda item: item["id"]):
            self.chat.insert("end", f"{message['sender']['username']}: {message['content']}\n")
        if had_messages and older:
            self.chat.yview(f"{top_line + prepend_lines}.{top_column}")
        elif at_bottom:
            self.chat.see("end")
        else:
            self.chat.yview(f"{top_line}.{top_column}")
        self.chat.configure(state="disabled")

    # Tick logic every 50 ms without blocking the Tkinter main loop.
    # A close event ends scheduling after the network worker has finished.
    def _poll_events(self) -> None:
        self._poll_id = None
        self.logic.poll()
        if not self.logic.closing or self.logic.network is not None:
            self._poll_id = self.after(50, self._poll_events)

    # Apply a validated full snapshot on the main thread, marking this user's row.
    # Reuse existing Treeview items to retain selection and remove offline users.
    # The server has already sorted/deduplicated identities across connections.
    def _update_directory(self, users: list[dict]) -> None:
        removed = set(self.online_users.get_children())
        for position, user in enumerate(users):
            key = f"user-{user['id']}"
            own = self.logic.user is not None and user["id"] == self.logic.user["id"]
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
    def _render_state(self) -> None:
        state = self.logic.state
        self.status.set({
            "waiting": "Waiting for pong...", "joining": "Joining...",
            "refreshing": "Refreshing online users...",
            "opening": "Opening private chat...", "syncing": "Loading messages...",
            "sending": "Sending message...",
            "connected": f"Joined as {self.logic.user['username']}" if self.logic.user else "Connected",
        }.get(state, state.capitalize()))
        self.connect_button.configure(state="normal" if state == "disconnected" else "disabled")
        self.disconnect_button.configure(
            state="normal" if state not in {"disconnected", "disconnecting", "closing"} else "disabled"
        )
        self.ping_button.configure(state="normal" if state == "connected" else "disabled")
        active = self.logic.user is not None and state not in {"disconnected", "disconnecting", "closing"}
        self.open_button.configure(state="normal" if active else "disabled")
        can_send = active and self.logic.room is not None and state == "connected"
        self.send_button.configure(state="normal" if can_send else "disabled")
        self.older_button.configure(state="normal" if can_send and self.logic.has_more_older else "disabled")
        self.message_input.configure(state="normal" if active and self.logic.room is not None and state != "sending" else "disabled")
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
        window.logic.shutdown()
        try:
            window.close()
        except tk.TclError:
            pass


if __name__ == "__main__":
    main()
