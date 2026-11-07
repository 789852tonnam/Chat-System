"""TCP Socket Chat server: queued username login and persistent users."""

from __future__ import annotations

import argparse
import logging
import math
import queue
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from common.protocol import (
    KNOWN_ACTIONS,
    FrameTooLarge,
    LineFramer,
    ProtocolError,
    Request,
    decode_document,
    encode_message,
    error_response,
    read_request_id,
    success_response,
    validate_envelope,
    validate_content,
    validate_id,
    validate_room_key,
    validate_sync_request,
    validate_username,
)
from database import get_or_create_user, open_database, open_private, send_message, sync_room

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


# Session identity is its connection, even when two sessions share the same user.
@dataclass(slots=True, eq=False)
class ClientSession:
    # Only the application worker changes the identity bound to this connection.
    user: dict[str, Any] | None = None
    # I/O signals closure; the worker alone updates the online-session registry.
    closed: threading.Event = field(default_factory=threading.Event)


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

    # Prepare configuration, the shared work queue, locks, and shutdown state.
    # Socket and thread references start as None; construction does not open
    # a port or run background work. Call start() to create those resources.
    def __init__(self, config: ServerConfig | None = None):
        # Settings used by start() and connection I/O.
        self.config = config or ServerConfig()
        # _submit() enqueues work; _worker_loop() processes it.
        self._work: queue.Queue[WorkItem | object] = queue.Queue(self.config.queue_capacity)
        # stop() signals wait() and running loops to exit.
        self._stopping = threading.Event()
        # Protects the connection registry during updates and shutdown.
        self._connections_lock = threading.Lock()
        # Prevents overlapping start() and stop() calls.
        self._lifecycle_lock = threading.Lock()
        # Maps client sockets to I/O threads for tracking and shutdown.
        self._connections: dict[socket.socket, threading.Thread] = {}
        # Listening socket used by _accept_loop().
        self._listener: socket.socket | None = None
        # Thread running _worker_loop() to process queued work.
        self._worker: threading.Thread | None = None
        # start() waits until database initialization has succeeded or failed.
        self._worker_ready = threading.Event()
        # Carries an initialization error back to the thread calling start().
        self._worker_start_error: Exception | None = None
        # Created, used, and closed exclusively on the application worker thread.
        self._database: sqlite3.Connection | None = None
        # Worker-owned active logins; multiple sessions may refer to one user ID.
        self._sessions: set[ClientSession] = set()
        # Thread running _accept_loop() to accept new clients.
        self._acceptor: threading.Thread | None = None
        # Prevents starting this server instance more than once.
        self._started = False
        # Makes stop() idempotent and prevents restarting a closed instance.
        self._closed = False
        # Actual bound IP and port, exposed through the address property.
        self._address: tuple[str, int] | None = None
        # Maximum recv() wait before checking deadlines and shutdown.
        self._poll_interval = min(0.2, self.config.frame_timeout / 4, self.config.idle_timeout / 4)

    # Expose the actual host and port selected when start() binds the listener.
    # This is useful with port=0, where the OS chooses an available port.
    # Read as server.address; accessing it before start() raises an error.
    @property
    def address(self) -> tuple[str, int]:
        if self._address is None:
            raise RuntimeError("Server has not started")
        return self._address

    # Bind the listening socket and launch the worker and acceptor threads.
    # The lifecycle lock keeps startup and shutdown from running together.
    # Wait for the worker to initialize SQLite before accepting connections.
    # Failed initialization closes the listener and is raised to the caller.
    # Each instance starts once; main() calls wait() after successful startup.
    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._closed:
                raise RuntimeError("Create a new ChatServer for each run")
            listener = socket.create_server((self.config.host, self.config.port))
            listener.settimeout(0.2)
            self._listener = listener
            self._address = listener.getsockname()
            self._worker = threading.Thread(target=self._worker_loop, name="chat-worker")
            self._started = True
            self._worker.start()
            self._worker_ready.wait()
            if self._worker_start_error is not None:
                self._stopping.set()
                self._close_socket(listener)
                self._worker.join()
                self._closed = True
                raise self._worker_start_error
            self._acceptor = threading.Thread(target=self._accept_loop, name="chat-acceptor")
            self._acceptor.start()

    # Block the calling thread until the shutdown Event is set.
    # Background I/O and worker threads continue serving requests meanwhile.
    # In main(), Ctrl-C interrupts this wait and finally calls stop().
    def wait(self) -> None:
        self._stopping.wait()

    # Signal shutdown, close the listener and client sockets, and wake the worker.
    # Closing sockets unblocks pending I/O; join() waits for owned threads to exit.
    # Repeated calls are harmless. Shutdown does not guarantee that every queued
    # request finishes, and it avoids trying to join the thread calling stop().
    def stop(self) -> None:
        """Close sockets to unblock I/O, then join all owned threads."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            if self._listener is not None:
                self._close_socket(self._listener)
            with self._connections_lock:
                connections = list(self._connections.items())
            for connection, _ in connections:
                self._close_socket(connection)
            try:
                self._work.put_nowait(_STOP)
            except queue.Full:
                # The worker also checks _stopping between queued jobs.
                pass
            for thread in [self._acceptor, *(t for _, t in connections), self._worker]:
                if thread is not None and thread is not threading.current_thread():
                    thread.join()

    # Shut down both directions of a socket, then release its OS resources.
    # Shutdown errors are ignored when the peer has already disconnected.
    # This helper needs only the supplied socket, so it has no self parameter.
    @staticmethod
    def _close_socket(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    # Run on the acceptor thread, waiting for new TCP connections.
    # Register each accepted socket with its own I/O thread under the registry
    # lock, so stop() can find and close it. Accept timeouts allow shutdown
    # checks; an unexpected listener error signals the server to stop.
    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stopping.is_set():
                    LOGGER.exception("Listener failed")
                    self._stopping.set()
                return
            with self._connections_lock:
                if self._stopping.is_set():
                    self._close_socket(connection)
                    return
                thread = threading.Thread(
                    target=self._connection_loop,
                    args=(connection,),
                    name=f"chat-connection-{connection.fileno()}",
                )
                self._connections[connection] = thread
                thread.start()

    # Run on a server I/O thread dedicated to one client connection.
    # Assemble complete frames, enforce deadlines, and track request IDs locally.
    # Valid envelopes go through _submit(); this thread waits for the worker's
    # result and sends it back to this client before handling the next frame.
    # Recoverable errors keep the connection open; fatal errors close it.
    # Always close the socket and remove its registry entry when the loop exits.
    def _connection_loop(self, connection: socket.socket) -> None:
        framer = LineFramer()
        # I/O carries this session to the worker but never changes its user.
        session = ClientSession()
        last_request_id = 0
        last_complete = time.monotonic()
        frame_started: float | None = None
        try:
            connection.settimeout(self._poll_interval)
            while not self._stopping.is_set():
                now = time.monotonic()
                if now - last_complete >= self.config.idle_timeout:
                    return
                if frame_started is not None and now - frame_started >= self.config.frame_timeout:
                    return
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                received_at = time.monotonic()
                if received_at - last_complete >= self.config.idle_timeout:
                    return
                if frame_started is not None and received_at - frame_started >= self.config.frame_timeout:
                    return
                if not framer.has_pending:
                    frame_started = received_at
                try:
                    for frame in framer.feed(chunk):
                        last_complete = received_at
                        frame_started = received_at if framer.has_pending else None
                        request_id = None
                        close = False
                        try:
                            document = decode_document(frame)
                            request_id = read_request_id(document)
                            if request_id <= last_request_id:
                                raise ProtocolError("DUPLICATE_REQUEST", "request_id must increase with each request")
                            # Even a subsequent validation/action error consumes this ID.
                            last_request_id = request_id
                            request = validate_envelope(document, request_id)
                            response = self._submit(request, session)
                            if response is None:
                                return
                        except ProtocolError as exc:
                            response = error_response(request_id, exc.code, exc.message)
                            close = exc.close
                        if not self._send_response(connection, response) or close:
                            return
                except FrameTooLarge:
                    self._send_response(
                        connection,
                        error_response(None, "FRAME_TOO_LARGE", "Request exceeds the byte limit"),
                    )
                    return
        except OSError:
            # A disconnected client must not stop the acceptor or other clients.
            pass
        finally:
            session.closed.set()
            self._close_socket(connection)
            with self._connections_lock:
                self._connections.pop(connection, None)

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
                        item.response = error_response(item.request.request_id, exc.code, exc.message)
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
        if request.action not in {"ping", "login", "logout", "get_directory", "open_private", "send_message", "sync_room"}:
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
            # No groups exist yet; create_group remains explicitly unimplemented.
            return success_response(request.request_id, {"online_users": online_users, "groups": []})
        if request.action == "open_private":
            if set(request.payload) != {"target_user_id"}:
                raise ProtocolError("INVALID_PAYLOAD", "open_private requires only target_user_id")
            target = validate_id(request.payload["target_user_id"], "target_user_id")
            return success_response(request.request_id, open_private(self._database, session.user["id"], target))
        if request.action == "send_message":
            if set(request.payload) != {"room_key", "content"}:
                raise ProtocolError("INVALID_PAYLOAD", "send_message requires room_key and content")
            room_key = validate_room_key(request.payload["room_key"])
            content = validate_content(request.payload["content"])
            return success_response(request.request_id, send_message(self._database, room_key, session.user["id"], content))
        if request.action == "sync_room":
            validate_sync_request(request.payload)
            return success_response(request.request_id, sync_room(self._database, session.user["id"], request.payload))
        return success_response(request.request_id, {"pong": True})

    # Run on the server I/O thread assigned to this client and encode its response.
    # Replace oversized or unencodable results with an error response.
    # Apply the send timeout while writing, then restore the short receive timeout.
    # Return False if socket I/O fails so the caller can finish connection cleanup.
    def _send_response(self, connection: socket.socket, response: dict[str, Any]) -> bool:
        try:
            encoded = encode_message(response)
        except FrameTooLarge:
            encoded = encode_message(error_response(
                response.get("request_id"), "RESPONSE_TOO_LARGE", "Response exceeds the byte limit"
            ))
        except (TypeError, ValueError, RecursionError):
            LOGGER.exception("Worker returned an invalid response")
            encoded = encode_message(error_response(
                response.get("request_id"), "INTERNAL_ERROR", "Server could not encode this response"
            ))
        try:
            connection.settimeout(self.config.send_timeout)
            connection.sendall(encoded)
            return True
        except OSError:
            return False
        finally:
            try:
                connection.settimeout(self._poll_interval)
            except OSError:
                pass


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
