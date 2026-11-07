"""Transport-first Socket Chat server: per-connection I/O, one queue/worker."""

from __future__ import annotations

import argparse
import logging
import math
import queue
import socket
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

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host must not be empty")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if type(self.queue_capacity) is not int or self.queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        for name in ("frame_timeout", "idle_timeout", "send_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(slots=True)
class WorkItem:
    request: Request
    done: threading.Event = field(default_factory=threading.Event)
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
    # Each instance can start only once. This method returns after launching
    # the threads; main() calls wait() to keep the process running afterward.
    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._closed:
                raise RuntimeError("Create a new ChatServer for each run")
            listener = socket.create_server((self.config.host, self.config.port))
            listener.settimeout(0.2)
            self._listener = listener
            self._address = listener.getsockname()
            self._worker = threading.Thread(target=self._worker_loop, name="chat-worker")
            self._acceptor = threading.Thread(target=self._accept_loop, name="chat-acceptor")
            self._started = True
            self._worker.start()
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
                            response = self._submit(request)
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
            self._close_socket(connection)
            with self._connections_lock:
                self._connections.pop(connection, None)

    # Wrap a request in a WorkItem with its own response slot and completion Event.
    # Called by an I/O thread: enqueue without blocking, then wait for the worker
    # to fill the response and signal done. A full queue returns SERVER_BUSY
    # without executing the request; shutdown while waiting returns None.
    def _submit(self, request: Request) -> dict[str, Any] | None:
        item = WorkItem(request)
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
    # Always mark dequeued items complete; the stop sentinel ends the loop.
    # Idle queue timeouts leave room for future maintenance work on this thread.
    def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                item = self._work.get(timeout=1.0)
            except queue.Empty:
                # Reserved for the agreed single-worker maintenance tick.
                continue
            try:
                if item is _STOP:
                    return
                assert isinstance(item, WorkItem)
                try:
                    item.response = self._handle_request(item.request)
                except Exception:
                    LOGGER.exception("Worker request failed")
                    item.response = error_response(
                        item.request.request_id, "INTERNAL_ERROR", "Server could not process this request"
                    )
                finally:
                    item.done.set()
            finally:
                self._work.task_done()

    # Dispatch an application request on the worker thread; do not send socket data.
    # Unknown names return UNKNOWN_ACTION; known but unfinished actions explicitly
    # return ACTION_NOT_IMPLEMENTED. Currently only ping has a real handler,
    # which requires an empty payload and returns the matching pong response.
    def _handle_request(self, request: Request) -> dict[str, Any]:
        """Only the central worker calls application handlers."""
        if request.action not in KNOWN_ACTIONS:
            return error_response(request.request_id, "UNKNOWN_ACTION", "Unknown action")
        if request.action != "ping":
            return error_response(
                request.request_id,
                "ACTION_NOT_IMPLEMENTED",
                "This server build does not implement this action yet",
            )
        if request.payload:
            return error_response(request.request_id, "INVALID_PAYLOAD", "ping requires an empty payload")
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        server = ChatServer(ServerConfig(host=args.host, port=args.port))
        server.start()
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot start server: {exc}\n")
    LOGGER.info("Listening on %s:%s (ping only)", *server.address)
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
