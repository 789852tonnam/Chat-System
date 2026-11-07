"""Tkinter username-only client; socket I/O lives in client_network.py."""

from __future__ import annotations

import argparse
import queue
import time
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from client_network import NetworkClient
from common.protocol import ProtocolError, validate_username


# Schedule the next directory request after the previous snapshot is received.
DIRECTORY_POLL_INTERVAL = 3.0


class ClientWindow(tk.Tk):
    """Update widgets only on Tkinter's main thread."""

    # Run on the main thread to create the window, connection form, and log.
    # Construction schedules queue polling but does not open a TCP connection.
    # Button callbacks enqueue work; only this thread reads or changes widgets.
    def __init__(self, host: str = "127.0.0.1", port: int = 5000):
        super().__init__()
        self.title("Socket Chat - Join")
        self.minsize(720, 400)
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

        ttk.Label(frame, textvariable=self.status).grid(row=3, column=0, columnspan=4, sticky="w")
        # Keep the online directory beside the expanding activity area.
        content = ttk.Frame(frame)
        content.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=(12, 0))
        content.columnconfigure(1, weight=1)
        content.rowconfigure(1, weight=1)
        ttk.Label(content, textvariable=self.directory_status).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(content, text="Activity").grid(row=0, column=1, sticky="w", pady=(0, 4))
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
        # Read-only output; _log() temporarily enables edits to append messages.
        self.activity = ScrolledText(content, width=52, height=12, wrap="word", state="disabled")
        self.activity.grid(row=1, column=1, sticky="nsew")
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
        self._set_state("waiting")
        self._log("Sending ping...")
        self.network.requests.put(("ping", {}))

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
                        self._set_state("joining")
                        self.network.requests.put(("login", {"username": self.username.get()}))
                elif kind == "response":
                    if self._state == "joining":
                        if value["ok"]:
                            self.user = value["data"]["user"]
                            self._set_state("connected")
                            self._log(f"Joined as {self.user['username']} (user #{self.user['id']}).")
                            self._next_directory_poll = time.monotonic()
                        else:
                            self._log(f"{value['error']['code']}: {value['error']['message']}")
                            self.disconnect()
                        continue
                    if self._state == "refreshing":
                        if value["ok"]:
                            self._update_directory(value["data"]["online_users"])
                        else:
                            self._log(f"{value['error']['code']}: {value['error']['message']}")
                        self._set_state("connected")
                        self._next_directory_poll = time.monotonic() + DIRECTORY_POLL_INTERVAL
                        continue
                    if value["ok"]:
                        self._log(f"Response #{value['request_id']}: {value['data']}")
                    else:
                        self._log(f"{value['error']['code']}: {value['error']['message']}")
                    if self._state == "waiting":
                        self._set_state("connected")
                elif kind == "error":
                    self._log(f"Network error: {value}")
            if finished:
                self.network.join()
                self.network = None
                self.user = None
                self._next_directory_poll = None
                self._update_directory([])
                if self._closing:
                    self.destroy()
                    return
                self._set_state("disconnected")
                self._log("Disconnected.")
        self._poll_directory()
        self._poll_id = self.after(50, self._poll_events)

    # Poll immediately after login and then after each interval, only while idle.
    # A ping already in flight takes precedence; missed polls are not accumulated.
    # Reuse the network queue and existing Tk callback rather than another thread.
    def _poll_directory(self) -> None:
        if (self.network is not None and self._state == "connected"
                and self._next_directory_poll is not None
                and time.monotonic() >= self._next_directory_poll):
            self._next_directory_poll = None
            self._set_state("refreshing")
            self.network.requests.put(("get_directory", {}))

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
            "connected": f"Joined as {self.user['username']}" if self.user else "Connected",
        }.get(state, state.capitalize()))
        self.connect_button.configure(state="normal" if state == "disconnected" else "disabled")
        self.disconnect_button.configure(
            state="normal" if state in {"connecting", "joining", "connected", "waiting", "refreshing"} else "disabled"
        )
        self.ping_button.configure(state="normal" if state == "connected" else "disabled")
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
