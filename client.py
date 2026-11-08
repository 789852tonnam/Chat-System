"""Tkinter window; application state and TCP I/O live in separate modules."""

from __future__ import annotations

import argparse
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from client_logic import ClientLogic


# AI output has no human sender account, but shares the same message widgets.
def _sender_name(message: dict) -> str:
    return "AI" if message["sender_kind"] == "ai" else message["sender"]["username"]


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
        # Public group count and editable creation form; metadata stays in logic.
        self.group_status = tk.StringVar(value="Groups (0)")
        # Cleared only after the server confirms successful group creation.
        self.group_name = tk.StringVar()
        # Updated from room-title events; room metadata belongs to logic.
        self.room_title = tk.StringVar(value="Select an online user and open a chat")
        # Selection is presentation state; message/reaction data belongs to logic.
        self._selected_message_id: int | None = None
        # Explains which message the Heart/Pin buttons will act on.
        self.selection_title = tk.StringVar(value="Click a message to select")
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
        ttk.Label(content, text="Conversation").grid(row=0, column=1, sticky="w", pady=(0, 4))
        # Native list/scrollbar controls keep large online lists keyboard-accessible.
        people = ttk.Frame(content)
        people.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        people.columnconfigure(0, weight=1)
        people.rowconfigure(0, weight=1)
        people.rowconfigure(2, weight=1)
        # Stable item IDs preserve selection across full directory snapshots.
        self.online_users = ttk.Treeview(people, show="tree", height=5, selectmode="browse")
        self.online_users.column("#0", width=200, minwidth=160, stretch=True)
        self.online_users.grid(row=0, column=0, sticky="nsew")
        # Scroll only the online list, independently of activity output.
        online_scroll = ttk.Scrollbar(people, orient="vertical", command=self.online_users.yview)
        online_scroll.grid(row=0, column=1, sticky="ns")
        self.online_users.configure(yscrollcommand=online_scroll.set)
        ttk.Label(people, textvariable=self.group_status).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 4))
        # All groups remain visible before joining; UUID item IDs preserve selection.
        self.groups = ttk.Treeview(people, show="tree", height=5, selectmode="browse")
        self.groups.column("#0", width=200, minwidth=160, stretch=True)
        self.groups.grid(row=2, column=0, sticky="nsew")
        group_scroll = ttk.Scrollbar(people, orient="vertical", command=self.groups.yview)
        group_scroll.grid(row=2, column=1, sticky="ns")
        self.groups.configure(yscrollcommand=group_scroll.set)
        self.groups.bind("<<TreeviewSelect>>", lambda event: self._show_group_members())
        ttk.Label(people, text="Group members").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 4))
        # Scrollable public member names do not expose the group's messages.
        self.group_members = ScrolledText(people, width=24, height=3, wrap="word", state="disabled")
        self.group_members.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(people, text="New group name").grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 4))
        self.group_name_entry = ttk.Entry(people, textvariable=self.group_name)
        self.group_name_entry.grid(row=6, column=0, columnspan=2, sticky="ew")
        group_buttons = ttk.Frame(people)
        group_buttons.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        # Creation starts with this user only; selection never auto-joins a group.
        self.create_group_button = ttk.Button(group_buttons, text="Create", command=self.create_group)
        self.create_group_button.pack(side="left")
        # Existing members reuse the same action to open their saved history.
        self.join_group_button = ttk.Button(group_buttons, text="Join / Open", command=self.open_group)
        self.join_group_button.pack(side="left", padx=(6, 0))
        # Chat grows with the window; the activity log stays a small diagnostic area.
        conversation = ttk.Frame(content)
        conversation.grid(row=1, column=1, sticky="nsew")
        conversation.columnconfigure(0, weight=1)
        conversation.rowconfigure(2, weight=1)
        # Keep the room name and older-history control above the chat view.
        header = ttk.Frame(conversation)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(header, textvariable=self.room_title).pack(side="left")
        # Ask for the nearest twenty messages before the current oldest cursor.
        self.older_button = ttk.Button(header, text="Load older (20)", command=self.load_older)
        self.older_button.pack(side="right")
        # One room-wide pin stays above history, including pins outside its range.
        self.pin_frame = ttk.Frame(conversation, padding=4, relief="solid")
        self.pin_frame.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.pin_frame.columnconfigure(0, weight=1)
        self.pin_text = ScrolledText(self.pin_frame, width=40, height=2, wrap="word", state="disabled")
        self.pin_text.grid(row=0, column=0, sticky="ew")
        self.unpin_button = ttk.Button(self.pin_frame, text="Unpin", command=lambda: self.logic.set_pin(None))
        self.unpin_button.grid(row=0, column=1, padx=(6, 0))
        self.pin_frame.grid_remove()
        # Only sync responses populate this chronological chat view.
        self.chat = ScrolledText(conversation, width=52, height=12, wrap="word", state="disabled")
        self.chat.grid(row=2, column=0, sticky="nsew")
        self.chat.tag_configure("selected_message", background="#e3efff")
        self.chat.bind("<Return>", self._select_at_cursor)
        # Native buttons also support keyboard activation after selecting a message.
        message_actions = ttk.Frame(conversation)
        message_actions.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.heart_button = ttk.Button(message_actions, text="Heart", command=self._heart_selected)
        self.heart_button.pack(side="left")
        self.pin_button = ttk.Button(message_actions, text="Pin message", command=self._pin_selected)
        self.pin_button.pack(side="left", padx=6)
        self.summary_button = ttk.Button(message_actions, text="Summarize", command=self.logic.request_summary)
        self.summary_button.pack(side="left", padx=(0, 6))
        ttk.Label(message_actions, textvariable=self.selection_title, wraplength=180).pack(side="left")
        # Message input remains separate from read-only history and diagnostic logs.
        composer = ttk.Frame(conversation)
        composer.grid(row=4, column=0, sticky="ew", pady=8)
        composer.columnconfigure(0, weight=1)
        # Multiline input is retained on errors and locked until a send is confirmed.
        self.message_input = tk.Text(composer, height=3, wrap="word")
        self.message_input.grid(row=0, column=0, sticky="ew")
        self.message_input.bind("<Control-Return>", lambda event: (self.send(), "break")[1])
        self.send_button = ttk.Button(composer, text="Send", command=self.send)
        self.send_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(conversation, text="Activity").grid(row=5, column=0, sticky="w")
        # Read-only output; _log() temporarily enables edits to append messages.
        self.activity = ScrolledText(conversation, width=52, height=5, wrap="word", state="disabled")
        self.activity.grid(row=6, column=0, sticky="ew")
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

    # Pass the plain creation form value to logic for shared name validation.
    def create_group(self) -> None:
        self.logic.create_group(self.group_name.get())

    # Joining requires an explicit button press after selecting a public group.
    def open_group(self) -> None:
        selected = self.groups.selection()
        if selected:
            self.logic.open_group(selected[0])

    # Read the composer; logic validates content and queues the send request.
    def send(self) -> None:
        self.logic.send(self.message_input.get("1.0", "end-1c"))

    # Forward the history action without computing or changing cursors in the UI.
    def load_older(self) -> None:
        self.logic.load_older()

    # Pass the selected message ID; logic derives the desired account heart state.
    def _heart_selected(self) -> None:
        if self._selected_message_id is not None:
            self.logic.toggle_heart(self._selected_message_id)

    # Pin the selected message without requiring the sender or creator's role.
    def _pin_selected(self) -> None:
        if self._selected_message_id is not None:
            self.logic.set_pin(self._selected_message_id)

    # Allow Enter at the text cursor to select the same message as a mouse click.
    def _select_at_cursor(self, event=None) -> str:
        for tag in self.chat.tag_names("insert"):
            if tag.startswith("message-"):
                self._select_message(int(tag.removeprefix("message-")))
                break
        return "break"

    # Keep selection across redraws using message IDs, without retaining old rooms.
    def _select_message(self, message_id: int | None) -> None:
        self._selected_message_id = message_id if message_id in self.logic.messages else None
        self.chat.tag_remove("selected_message", "1.0", "end")
        if self._selected_message_id is not None:
            self.chat.tag_add("selected_message", *self.chat.tag_ranges(f"message-{self._selected_message_id}"))
            self.chat.tag_raise("sel")
        self._update_message_controls()

    # Render selection text and action availability from the current logic state.
    # Busy requests cannot stack mutations, but the text remains selectable/copyable.
    def _update_message_controls(self) -> None:
        message = self.logic.messages.get(self._selected_message_id)
        enabled = message is not None and self.logic.state == "connected"
        self.heart_button.configure(state="normal" if enabled else "disabled",
                                    text="Unheart" if message and message["hearted_by_me"] else "Heart")
        self.pin_button.configure(state="normal" if enabled else "disabled")
        self.unpin_button.configure(state="normal" if self.logic.pinned_message is not None and self.logic.state == "connected" else "disabled")
        self.selection_title.set(f"Selected: {_sender_name(message)}: {message['content'][:35].replace(chr(10), ' ')}"
                                 if message else "Click a message to select")

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
        elif kind == "group_created":
            self.group_name.set("")
        elif kind == "pin":
            self._render_pin()
        elif kind == "closed":
            if self._poll_id is not None:
                self.after_cancel(self._poll_id)
            self.destroy()

    # Clear presentation for the previous room; logic has already dropped its cache.
    def _clear_room(self) -> None:
        self._selected_message_id = None
        self.room_title.set("Select an online user and open a chat")
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.message_input.configure(state="normal")
        self.message_input.delete("1.0", "end")
        for tag in self.chat.tag_names():
            if tag.startswith("message-"):
                self.chat.tag_delete(tag)
        self._render_pin()
        self._update_message_controls()

    # Show the full pinned text in a bounded, scrollable banner above the chat.
    # A layout change keeps the visible history anchor or the bottom position.
    def _render_pin(self) -> None:
        pinned = self.logic.pinned_message
        text = f"Pinned - {_sender_name(pinned)}: {pinned['content']}" if pinned else ""
        if self.pin_text.get("1.0", "end-1c") == text:
            return
        top = self.chat.index("@0,0")
        at_bottom = self.chat.yview()[1] >= 0.999
        self.pin_text.configure(state="normal")
        self.pin_text.delete("1.0", "end")
        self.pin_text.insert("1.0", text)
        self.pin_text.configure(state="disabled")
        if pinned:
            self.pin_frame.grid()
        else:
            self.pin_frame.grid_remove()
        self.update_idletasks()
        self.chat.see("end") if at_bottom else self.chat.yview(top)

    # Draw the merged history while retaining the user's visible text anchor.
    # Logic supplies newly prepended messages and whether history existed before.
    # Formatting, line counts and scrolling depend on widgets and stay here.
    def _render_messages(self, older: list[dict], had_messages: bool) -> None:
        top_line, top_column = (int(part) for part in self.chat.index("@0,0").split("."))
        at_bottom = self.chat.yview()[1] >= 0.999
        prepend_lines = sum(f"{_sender_name(message)}: {message['content']}\n".count("\n") for message in older)
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        for tag in self.chat.tag_names():
            if tag.startswith("message-"):
                self.chat.tag_delete(tag)
        for message in sorted(self.logic.messages.values(), key=lambda item: item["id"]):
            start = self.chat.index("end-1c")
            heart = "\u2665" if message["hearted_by_me"] else "\u2661"
            self.chat.insert("end", f"{_sender_name(message)}: {message['content']}  [{heart} {message['heart_count']}]\n")
            tag = f"message-{message['id']}"
            self.chat.tag_add(tag, start, "end-1c")
            self.chat.tag_bind(tag, "<Button-1>", lambda event, message_id=message["id"]: self._select_message(message_id))
        if had_messages and older:
            self.chat.yview(f"{top_line + prepend_lines}.{top_column}")
        elif at_bottom:
            self.chat.see("end")
        else:
            self.chat.yview(f"{top_line}.{top_column}")
        self.chat.configure(state="disabled")
        self._select_message(self._selected_message_id)

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
    def _update_directory(self, data: dict) -> None:
        users = data["online_users"]
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
        removed = set(self.groups.get_children())
        for position, group in enumerate(data["groups"]):
            key = group["room_key"]
            joined = self.logic.user is not None and any(member["id"] == self.logic.user["id"] for member in group["members"])
            text = group["name"] + (" (joined)" if joined else "")
            if self.groups.exists(key):
                self.groups.item(key, text=text)
            else:
                self.groups.insert("", "end", iid=key, text=text)
            self.groups.move(key, "", position)
            removed.discard(key)
        for key in removed:
            self.groups.delete(key)
        if not self.groups.selection() and self.logic.room is not None and self.logic.room["kind"] == "group":
            key = self.logic.room["room_key"]
            if self.groups.exists(key):
                self.groups.selection_set(key)
        self.group_status.set(f"Groups ({len(data['groups'])})")
        self._show_group_members()

    # Display names for the selected public group, even before this user joins.
    # Re-render after directory snapshots so new memberships appear while selected.
    def _show_group_members(self) -> None:
        selected = self.groups.selection()
        group = self.logic.groups.get(selected[0]) if selected else None
        text = ", ".join(member["username"] for member in group["members"]) if group else "Select a group to view members."
        self.group_members.configure(state="normal")
        self.group_members.delete("1.0", "end")
        self.group_members.insert("1.0", text)
        self.group_members.configure(state="disabled")

    # Keep status text, button availability, and form editability in sync.
    # Only the disconnected state allows a new address or connection attempt.
    # Waiting disables Ping while Disconnect remains available for cancellation.
    def _render_state(self) -> None:
        state = self.logic.state
        self.status.set({
            "waiting": "Waiting for pong...", "joining": "Joining...",
            "refreshing": "Refreshing online users...",
            "opening": "Opening conversation...", "syncing": "Loading messages...",
            "creating": "Creating group...",
            "sending": "Sending message...",
            "updating": "Updating message...", "reloading": "Refreshing room state...",
            "requesting_summary": "Requesting AI summary...", "checking_summary": "Checking AI summary...",
            "connected": f"Joined as {self.logic.user['username']}" if self.logic.user else "Connected",
        }.get(state, state.capitalize()))
        self.connect_button.configure(state="normal" if state == "disconnected" else "disabled")
        self.disconnect_button.configure(
            state="normal" if state not in {"disconnected", "disconnecting", "closing"} else "disabled"
        )
        self.ping_button.configure(state="normal" if state == "connected" else "disabled")
        active = self.logic.user is not None and state not in {"disconnected", "disconnecting", "closing"}
        self.open_button.configure(state="normal" if active else "disabled")
        self.join_group_button.configure(state="normal" if active else "disabled")
        self.create_group_button.configure(state="normal" if active and state == "connected" else "disabled")
        self.group_name_entry.configure(state="normal" if active and state != "creating" else "disabled")
        can_send = active and self.logic.room is not None and state == "connected"
        self.send_button.configure(state="normal" if can_send else "disabled")
        self.summary_button.configure(state="normal" if can_send else "disabled")
        self.older_button.configure(state="normal" if can_send and self.logic.has_more_older else "disabled")
        self.message_input.configure(state="normal" if active and self.logic.room is not None and state != "sending" else "disabled")
        entry_state = "normal" if state == "disconnected" else "readonly"
        self.host_entry.configure(state=entry_state)
        self.port_entry.configure(state=entry_state)
        self.username_entry.configure(state=entry_state)
        self._update_message_controls()

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
