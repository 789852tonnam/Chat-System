"""Socket Chat application: queued handlers, sessions and persistent storage."""

from __future__ import annotations

import argparse
import logging
import math
import queue
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any

from common.protocol import (
    KNOWN_ACTIONS,
    ProtocolError,
    Request,
    error_response,
    success_response,
    validate_content,
    validate_group_name,
    validate_id,
    validate_room_key,
    validate_snapshot_request,
    validate_sync_request,
    validate_username,
)
from server_network import ClientSession, ServerNetwork
from database import (
    create_group, get_or_create_user, get_room_snapshot, join_group, list_groups,
    open_database, open_private, send_message, set_heart, set_pin, sync_room,
)

LOGGER = logging.getLogger("socket_chat.server")
_STOP = object()


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    queue_capacity: int = 256
    frame_timeout: float = 10.0
    idle_timeout: float = 120.0
    send_timeout: float = 10.0
    # Persistent user storage; tests pass :memory: to avoid sharing disk state.
    database_path: str = "data/chat.sqlite3"

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host must not be empty")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if type(self.queue_capacity) is not int or self.queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        if type(self.database_path) is not str or not self.database_path:
            raise ValueError("database_path must be a non-empty string")
        for name in ("frame_timeout", "idle_timeout", "send_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(slots=True)
class WorkItem:
    # Parsed wire data and the connection-local session travel in the same job.
    request: Request
    session: ClientSession
    # Wake the originating I/O thread after its response is ready.
    done: threading.Event = field(default_factory=threading.Event)
    # Filled by the worker before it sets done.
    response: dict[str, Any] | None = None


class ChatServer:
    """Single-use server with explicit start/stop for CLI and TCP integration tests."""

    # Prepare application state and connect the transport to _submit().
    # Construction opens no sockets or database; start() owns that lifecycle.
    def __init__(self, config: ServerConfig | None = None):
        # Shared configuration validated before any resources are allocated.
        self.config = config or ServerConfig()
        # I/O enqueues WorkItems; only the application worker handles them.
        self._work: queue.Queue[WorkItem | object] = queue.Queue(self.config.queue_capacity)
        # Shared cancellation wakes wait(), transport and queued response waits.
        self._stopping = threading.Event()
        # Prevents overlapping application startup and shutdown.
        self._lifecycle_lock = threading.Lock()
        # Owns sockets and I/O threads; calls _submit(request, session).
        self._network = ServerNetwork(
            self._submit, self._stopping,
            host=self.config.host, port=self.config.port,
            frame_timeout=self.config.frame_timeout,
            idle_timeout=self.config.idle_timeout,
            send_timeout=self.config.send_timeout,
        )
        # Runs application handlers and owns SQLite from open through close.
        self._worker: threading.Thread | None = None
        # Reports database initialization before transport starts accepting.
        self._worker_ready = threading.Event()
        # Propagates database startup errors to the caller of start().
        self._worker_start_error: Exception | None = None
        # Accessed exclusively by the application worker.
        self._database: sqlite3.Connection | None = None
        # Worker-owned logins; several connections can share the same user ID.
        self._sessions: set[ClientSession] = set()
        # Each instance is single-use, including after failed startup.
        self._started = False
        # Makes repeated stop() calls harmless.
        self._closed = False

    # Expose the transport's bound host and port, including port=0 allocation.
    # Access before binding raises RuntimeError in the transport.
    @property
    def address(self) -> tuple[str, int]:
        return self._network.address

    # Initialize storage on its worker before accepting TCP connections.
    # Failed storage or listener startup releases all allocated resources.
    # BaseException also covers Ctrl-C during startup; cleanup then re-raises it.
    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._closed:
                raise RuntimeError("Create a new ChatServer for each run")
            self._started = True
            self._worker = threading.Thread(target=self._worker_loop, name="chat-worker")
            try:
                self._worker.start()
                self._worker_ready.wait()
                if self._worker_start_error is not None:
                    raise self._worker_start_error
                self._network.start()
            except BaseException:
                self._closed = True
                self._stopping.set()
                self._network.stop()
                self._stop_worker()
                raise

    # Block the calling thread until the shutdown Event is set.
    # Background I/O and worker threads continue serving requests meanwhile.
    # In main(), Ctrl-C interrupts this wait and finally calls stop().
    def wait(self) -> None:
        self._stopping.wait()

    # Cancel response waits, stop transport I/O, then finish the worker's cleanup.
    # Already-running handlers finish before SQLite closes; queued requests are
    # not guaranteed to execute. Repeated calls are safe for CLI finally blocks.
    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            self._network.stop()
            self._stop_worker()

    # Wake an idle worker and wait for its database/session cleanup to finish.
    # A full queue needs no sentinel because the loop also checks shutdown.
    # Handle partial startup where a worker object exists but never started.
    def _stop_worker(self) -> None:
        try:
            self._work.put_nowait(_STOP)
        except queue.Full:
            pass
        if self._worker is not None and self._worker.ident is not None and self._worker is not threading.current_thread():
            self._worker.join()

    # Wrap a request in a WorkItem with its own response slot and completion Event.
    # Called by an I/O thread: enqueue without blocking, then wait for the worker
    # to fill the response and signal done. A full queue returns SERVER_BUSY
    # without executing the request; shutdown while waiting returns None.
    def _submit(self, request: Request, session: ClientSession) -> dict[str, Any] | None:
        item = WorkItem(request, session)
        try:
            self._work.put_nowait(item)
        except queue.Full:
            return error_response(request.request_id, "SERVER_BUSY", "Server work queue is full")
        while not item.done.wait(0.1):
            if self._stopping.is_set():
                return None
        return item.response

    # Run on the single worker thread and process queued requests one at a time.
    # Store each result on its WorkItem and set done to wake the correct I/O thread.
    # Handler exceptions become INTERNAL_ERROR without killing this worker.
    # Own SQLite on this thread from initialization through final close.
    # Report startup failure before accepting clients, and always mark dequeued
    # items complete; the stop sentinel ends the loop.
    # Idle queue timeouts leave room for future maintenance work on this thread.
    def _worker_loop(self) -> None:
        try:
            self._database = open_database(self.config.database_path)
        except Exception as exc:
            self._worker_start_error = exc
            self._worker_ready.set()
            return
        self._worker_ready.set()
        try:
            while not self._stopping.is_set():
                try:
                    item = self._work.get(timeout=1.0)
                except queue.Empty:
                    self._prune_sessions()
                    continue
                try:
                    if item is _STOP:
                        return
                    assert isinstance(item, WorkItem)
                    try:
                        self._prune_sessions()
                        item.response = self._handle_request(item.request, item.session)
                    except ProtocolError as exc:
                        item.response = error_response(item.request.request_id, exc.code, exc.message, details=exc.details)
                    except Exception:
                        LOGGER.exception("Worker request failed")
                        item.response = error_response(
                            item.request.request_id, "INTERNAL_ERROR", "Server could not process this request"
                        )
                    finally:
                        item.done.set()
                finally:
                    self._work.task_done()
        finally:
            self._sessions.clear()
            self._database.close()
            self._database = None

    # Remove closed connections on the worker, without sharing a mutable registry
    # with I/O threads. Run before requests and on the idle maintenance tick.
    # Persistent users stay in SQLite; this only changes online visibility.
    def _prune_sessions(self) -> None:
        self._sessions = {session for session in self._sessions if not session.closed.is_set()}

    # Dispatch an application request on the worker thread; do not send socket data.
    # Unknown names return UNKNOWN_ACTION; known but unfinished actions explicitly
    # return ACTION_NOT_IMPLEMENTED. Login creates/reuses a persistent username
    # and binds it to this connection; logout clears only this connection's user.
    # Passwords and a separate registration step are intentionally absent.
    def _handle_request(self, request: Request, session: ClientSession) -> dict[str, Any]:
        """Only the central worker calls application handlers."""
        if request.action not in KNOWN_ACTIONS:
            return error_response(request.request_id, "UNKNOWN_ACTION", "Unknown action")
        if request.action not in {"ping", "login", "logout", "get_directory", "open_private",
                                  "create_group", "join_group", "send_message", "sync_room",
                                  "get_room_snapshot", "set_heart", "set_pin"}:
            return error_response(
                request.request_id,
                "ACTION_NOT_IMPLEMENTED",
                "This server build does not implement this action yet",
            )
        if request.action == "login":
            if session.user is not None:
                raise ProtocolError("ALREADY_AUTHENTICATED", "This connection has already joined")
            if set(request.payload) != {"username"}:
                raise ProtocolError("INVALID_PAYLOAD", "login requires only username")
            username = validate_username(request.payload["username"])
            assert self._database is not None
            session.user = get_or_create_user(self._database, username)
            self._sessions.add(session)
            return success_response(request.request_id, {"user": session.user})
        if request.action not in {"ping", "logout"} and session.user is None:
            raise ProtocolError("UNAUTHENTICATED", "Join with a username before using this action")
        if request.action in {"ping", "logout", "get_directory"} and request.payload:
            raise ProtocolError("INVALID_PAYLOAD", f"{request.action} requires an empty payload")
        if request.action == "logout":
            self._sessions.discard(session)
            session.user = None
            return success_response(request.request_id, {"logged_out": True})
        if request.action == "get_directory":
            users = {active.user["id"]: active.user for active in self._sessions}
            online_users = sorted(users.values(), key=lambda user: (user["username"].lower(), user["id"]))
            return success_response(request.request_id, {"online_users": online_users, "groups": list_groups(self._database)})
        if request.action == "open_private":
            if set(request.payload) != {"target_user_id"}:
                raise ProtocolError("INVALID_PAYLOAD", "open_private requires only target_user_id")
            target = validate_id(request.payload["target_user_id"], "target_user_id")
            return success_response(request.request_id, open_private(self._database, session.user["id"], target))
        if request.action == "create_group":
            if set(request.payload) != {"name"}:
                raise ProtocolError("INVALID_PAYLOAD", "create_group requires only name")
            name = validate_group_name(request.payload["name"])
            return success_response(request.request_id, create_group(self._database, session.user["id"], name))
        if request.action == "join_group":
            if set(request.payload) != {"room_key"}:
                raise ProtocolError("INVALID_PAYLOAD", "join_group requires only room_key")
            room_key = validate_room_key(request.payload["room_key"])
            return success_response(request.request_id, join_group(self._database, session.user["id"], room_key))
        if request.action == "send_message":
            if set(request.payload) != {"room_key", "content"}:
                raise ProtocolError("INVALID_PAYLOAD", "send_message requires room_key and content")
            room_key = validate_room_key(request.payload["room_key"])
            content = validate_content(request.payload["content"])
            return success_response(request.request_id, send_message(self._database, room_key, session.user["id"], content))
        if request.action == "sync_room":
            validate_sync_request(request.payload)
            return success_response(request.request_id, sync_room(self._database, session.user["id"], request.payload))
        if request.action == "get_room_snapshot":
            validate_snapshot_request(request.payload)
            return success_response(request.request_id, get_room_snapshot(self._database, session.user["id"], request.payload))
        if request.action in {"set_heart", "set_pin"}:
            expected = {"room_key", "message_id", "hearted"} if request.action == "set_heart" else {"room_key", "message_id"}
            if set(request.payload) != expected:
                raise ProtocolError("INVALID_PAYLOAD", "Invalid mutation fields")
            room_key = validate_room_key(request.payload["room_key"])
            message_id = request.payload["message_id"]
            if request.action == "set_heart" or message_id is not None:
                validate_id(message_id, "message_id")
            if request.action == "set_heart":
                if type(request.payload["hearted"]) is not bool:
                    raise ProtocolError("INVALID_PAYLOAD", "hearted must be a boolean")
                data = set_heart(self._database, room_key, session.user["id"], message_id, request.payload["hearted"])
            else:
                data = set_pin(self._database, room_key, session.user["id"], message_id)
            return success_response(request.request_id, data)
        return success_response(request.request_id, {"pong": True})



# Parse listener/storage settings and keep the process alive after startup.
# Always stop an allocated server, including interruption while SQLite opens.
# Startup errors become CLI errors; Ctrl-C closes owned sockets and threads.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--database", default="data/chat.sqlite3")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    server = None
    try:
        server = ChatServer(ServerConfig(host=args.host, port=args.port, database_path=args.database))
        server.start()
        LOGGER.info("Listening on %s:%s", *server.address)
        server.wait()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Cannot start server: {exc}\n")
    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    main()
