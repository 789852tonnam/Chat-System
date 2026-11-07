"""TCP transport: connection threads, newline framing and response delivery."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from common.protocol import (
    FrameTooLarge, LineFramer, ProtocolError, Request, decode_document,
    encode_message, error_response, read_request_id, validate_envelope,
)

LOGGER = logging.getLogger("socket_chat.server.network")


# Session identity is its connection, even when two sessions share the same user.
@dataclass(slots=True, eq=False)
class ClientSession:
    # Only the application worker changes the identity bound to this connection.
    user: dict[str, Any] | None = None
    # I/O signals closure; the worker alone updates the online-session registry.
    closed: threading.Event = field(default_factory=threading.Event)


class ServerNetwork:
    """Single-use TCP transport; application work is supplied as a callback."""

    # Prepare transport state without opening sockets or starting threads.
    # The owner supplies validated settings and shares its shutdown Event so
    # pending submit() calls can stop waiting when transport shutdown begins.
    def __init__(
        self,
        submit: Callable[[Request, ClientSession], dict[str, Any] | None],
        stopping: threading.Event,
        *,
        host: str,
        port: int,
        frame_timeout: float,
        idle_timeout: float,
        send_timeout: float,
    ):
        # Called by each I/O thread; None means no response during shutdown.
        self._submit = submit
        # Shared with the application worker and its response-wait loop.
        self._stopping = stopping
        # Bind host supplied by the application's ServerConfig.
        self._host = host
        # Requested listener port; zero lets the OS choose an available port.
        self._port = port
        # Maximum seconds to finish a frame after its first received byte.
        self._frame_timeout = frame_timeout
        # Maximum seconds since the last complete received frame.
        self._idle_timeout = idle_timeout
        # Maximum seconds allowed for one response sendall().
        self._send_timeout = send_timeout
        # Protects the registry while connections enter, exit or shut down.
        self._connections_lock = threading.Lock()
        # Owned client sockets and their dedicated I/O threads.
        self._connections: dict[socket.socket, threading.Thread] = {}
        # Created by start(); closing it wakes the acceptor during shutdown.
        self._listener: socket.socket | None = None
        # Accepts connections; it never handles application state or SQLite.
        self._acceptor: threading.Thread | None = None
        # Actual bound address, including the OS-selected port when port=0.
        self._address: tuple[str, int] | None = None
        # Short receive waits let I/O check deadlines and the shutdown Event.
        self._poll_interval = min(0.2, frame_timeout / 4, idle_timeout / 4)

    # Return the address after binding; reading it before startup is an error.
    @property
    def address(self) -> tuple[str, int]:
        if self._address is None:
            raise RuntimeError("Server has not started")
        return self._address

    # Bind and start accepting clients after the owner has initialized storage.
    # The owner serializes start()/stop() and calls stop() if startup fails.
    def start(self) -> None:
        if self._listener is not None or self._stopping.is_set():
            raise RuntimeError("Create a new ServerNetwork for each run")
        self._listener = socket.create_server((self._host, self._port))
        self._listener.settimeout(0.2)
        self._address = self._listener.getsockname()
        self._acceptor = threading.Thread(target=self._accept_loop, name="chat-acceptor")
        self._acceptor.start()

    # Signal shared cancellation before closing sockets and joining I/O threads.
    # This also releases I/O threads waiting inside the application's submit().
    # Registry locking prevents a newly accepted connection escaping the snapshot.
    def stop(self) -> None:
        self._stopping.set()
        if self._listener is not None:
            self._close_socket(self._listener)
        with self._connections_lock:
            connections = list(self._connections.items())
        for connection, _ in connections:
            self._close_socket(connection)
        for thread in [self._acceptor, *(t for _, t in connections)]:
            if thread is not None and thread.ident is not None and thread is not threading.current_thread():
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
                if now - last_complete >= self._idle_timeout:
                    return
                if frame_started is not None and now - frame_started >= self._frame_timeout:
                    return
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                received_at = time.monotonic()
                if received_at - last_complete >= self._idle_timeout:
                    return
                if frame_started is not None and received_at - frame_started >= self._frame_timeout:
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
            connection.settimeout(self._send_timeout)
            connection.sendall(encoded)
            return True
        except OSError:
            return False
        finally:
            try:
                connection.settimeout(self._poll_interval)
            except OSError:
                pass

