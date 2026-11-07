"""Real loopback TCP tests for connection isolation, queues and error recovery."""

import concurrent.futures
import contextlib
import json
import socket
import threading
import time
import unittest

from common.protocol import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, encode_message
from server import ChatServer, ServerConfig


class Peer:
    def __init__(self, address):
        self.socket = socket.create_connection(address, timeout=3)
        self.reader = self.socket.makefile("rb")

    def send(self, request_id=1, action="ping", payload=None):
        self.socket.sendall(encode_message({
            "request_id": request_id, "action": action,
            "payload": {} if payload is None else payload,
        }))

    def read(self):
        line = self.reader.readline()
        if not line:
            raise EOFError("Server closed before a response")
        return json.loads(line)

    def is_closed(self):
        try:
            return self.reader.read(1) == b""
        except ConnectionResetError:
            return True

    def close(self):
        self.reader.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)

    def start(self, server_class=ChatServer, **config):
        server = server_class(ServerConfig(host="127.0.0.1", port=0, **config))
        server.start()
        self.addCleanup(server.stop)
        return server

    def peer(self, server):
        return self.resources.enter_context(Peer(server.address))

    def assert_pong(self, peer, request_id):
        self.assertEqual(peer.read(), {"request_id": request_id, "ok": True, "data": {"pong": True}})

    def test_multiple_buffered_requests_keep_response_order(self):
        peer = self.peer(self.start())
        peer.socket.sendall(b''.join(
            encode_message({"request_id": i, "action": "ping", "payload": {}})
            for i in range(1, 5)
        ))
        for i in range(1, 5):
            self.assert_pong(peer, i)

    def test_fragmented_utf8_frame_and_next_request(self):
        peer = self.peer(self.start())
        wire = encode_message({"request_id": 1, "action": "ทดสอบ", "payload": {}})
        split = wire.index("ท".encode()) + 1
        peer.socket.sendall(wire[:split])
        peer.socket.sendall(wire[split:])
        self.assertEqual(peer.read()["error"]["code"], "UNKNOWN_ACTION")
        peer.send(2)
        self.assert_pong(peer, 2)

    def test_recoverable_json_error_does_not_consume_an_id(self):
        peer = self.peer(self.start())
        peer.socket.sendall(b'{"request_id":1,"request_id":2}\n')
        response = peer.read()
        self.assertIsNone(response["request_id"])
        self.assertEqual(response["error"]["code"], "INVALID_JSON")
        peer.send(1)
        self.assert_pong(peer, 1)

    def test_bad_envelope_consumes_valid_id(self):
        peer = self.peer(self.start())
        peer.socket.sendall(b'{"request_id":5,"action":"ping"}\n')
        self.assertEqual(peer.read()["error"]["code"], "INVALID_REQUEST")
        peer.send(5)
        self.assertEqual(peer.read()["error"]["code"], "DUPLICATE_REQUEST")
        peer.send(6)
        self.assert_pong(peer, 6)

    def test_invalid_boolean_id_is_not_reserved(self):
        peer = self.peer(self.start())
        peer.send(True)
        response = peer.read()
        self.assertIsNone(response["request_id"])
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST_ID")
        peer.send(1)
        self.assert_pong(peer, 1)

    def test_unknown_payload_and_unimplemented_actions_are_explicit(self):
        peer = self.peer(self.start())
        for request_id, action, payload, code in [
            (1, "unknown", {}, "UNKNOWN_ACTION"),
            (2, "ping", {"extra": 1}, "INVALID_PAYLOAD"),
            (3, "login", {"username": "Alice", "password": "example-pass-123"}, "ACTION_NOT_IMPLEMENTED"),
        ]:
            peer.send(request_id, action, payload)
            response = peer.read()
            self.assertEqual(response["request_id"], request_id)
            self.assertEqual(response["error"]["code"], code)
        peer.send(4)
        self.assert_pong(peer, 4)

    def test_crlf_is_accepted(self):
        peer = self.peer(self.start())
        peer.socket.sendall(b'{"request_id":1,"action":"ping","payload":{}}\r\n')
        self.assert_pong(peer, 1)

    def test_oversized_unterminated_frame_closes_only_its_client(self):
        server = self.start()
        peer = self.peer(server)
        peer.socket.sendall(b'a' * (MAX_REQUEST_BYTES + 1))
        response = peer.read()
        self.assertIsNone(response["request_id"])
        self.assertEqual(response["error"]["code"], "FRAME_TOO_LARGE")
        self.assertTrue(peer.is_closed())
        other = self.peer(server)
        other.send(1)
        self.assert_pong(other, 1)

    def test_valid_frame_before_oversized_frame_gets_its_reply(self):
        peer = self.peer(self.start())
        peer.socket.sendall(encode_message({"request_id": 1, "action": "ping", "payload": {}})
                            + b'x' * (MAX_REQUEST_BYTES + 1) + b'\n')
        self.assert_pong(peer, 1)
        self.assertEqual(peer.read()["error"]["code"], "FRAME_TOO_LARGE")
        self.assertTrue(peer.is_closed())

    def test_invalid_utf8_reports_fatal_error(self):
        peer = self.peer(self.start())
        peer.socket.sendall(b'\xff\n')
        response = peer.read()
        self.assertIsNone(response["request_id"])
        self.assertEqual(response["error"]["code"], "INVALID_UTF8")
        self.assertTrue(peer.is_closed())

    def test_partial_frame_deadline(self):
        peer = self.peer(self.start(frame_timeout=0.1, idle_timeout=1))
        peer.socket.sendall(b'{"request_id":')
        self.assertTrue(peer.is_closed())

    def test_idle_deadline(self):
        peer = self.peer(self.start(idle_timeout=0.1))
        self.assertTrue(peer.is_closed())

    def test_eof_in_partial_frame_is_clean(self):
        server = self.start()
        peer = self.peer(server)
        peer.socket.sendall(b'{"request_id":')
        peer.socket.shutdown(socket.SHUT_WR)
        self.assertTrue(peer.is_closed())
        other = self.peer(server)
        other.send(1)
        self.assert_pong(other, 1)

    def test_concurrent_clients_use_one_worker_and_isolated_ids(self):
        class RecordingServer(ChatServer):
            def __init__(self, config):
                super().__init__(config)
                self.worker_ids = set()

            def _handle_request(self, request):
                self.worker_ids.add(threading.get_ident())
                return super()._handle_request(request)

        server = self.start(RecordingServer)
        def exchange(request_id):
            with Peer(server.address) as peer:
                peer.send(request_id)
                first = peer.read()
                peer.send(request_id + 100)
                return first, peer.read()
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(exchange, range(1, 13)))
        for request_id, (first, second) in enumerate(results, 1):
            self.assertEqual(first, {"request_id": request_id, "ok": True, "data": {"pong": True}})
            self.assertEqual(second["request_id"], request_id + 100)
        self.assertEqual(len(server.worker_ids), 1)

    def test_queue_full_does_not_execute_rejected_request(self):
        class BlockingServer(ChatServer):
            def __init__(self, config):
                super().__init__(config)
                self.entered = threading.Event()
                self.release = threading.Event()
                self.handled = []

            def _handle_request(self, request):
                self.handled.append(request.request_id)
                if request.request_id == 1:
                    self.entered.set()
                    self.release.wait(3)
                return super()._handle_request(request)

        server = self.start(BlockingServer, queue_capacity=1)
        a, b, c = [self.peer(server) for _ in range(3)]
        try:
            a.send(1)
            self.assertTrue(server.entered.wait(2))
            b.send(2)
            deadline = time.monotonic() + 2
            while server._work.qsize() != 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(server._work.qsize(), 1)
            c.send(3)
            self.assertEqual(c.read()["error"]["code"], "SERVER_BUSY")
        finally:
            server.release.set()
        self.assert_pong(a, 1)
        self.assert_pong(b, 2)
        self.assertEqual(server.handled, [1, 2])

    def test_worker_exception_does_not_kill_the_worker(self):
        class FailingServer(ChatServer):
            def _handle_request(self, request):
                if request.request_id == 1:
                    raise RuntimeError("simulated failure")
                return super()._handle_request(request)

        peer = self.peer(self.start(FailingServer))
        with self.assertLogs("socket_chat.server", level="ERROR"):
            peer.send(1)
            self.assertEqual(peer.read()["error"]["code"], "INTERNAL_ERROR")
        peer.send(2)
        self.assert_pong(peer, 2)

    def test_shutdown_unblocks_connected_clients_and_joins_threads(self):
        server = self.start()
        peer = self.peer(server)
        peer.send(1)
        self.assert_pong(peer, 1)
        server.stop()
        self.assertTrue(peer.is_closed())
        self.assertFalse(server._worker.is_alive())
        self.assertFalse(server._acceptor.is_alive())
        server.stop()  # Idempotent shutdown is safe for finally/cleanup callers.

    def test_oversized_response_becomes_a_bounded_error(self):
        class OversizedServer(ChatServer):
            def _handle_request(self, request):
                if request.request_id == 1:
                    return {"request_id": 1, "ok": True, "data": {"text": "x" * MAX_RESPONSE_BYTES}}
                return super()._handle_request(request)

        peer = self.peer(self.start(OversizedServer))
        peer.send(1)
        self.assertEqual(peer.read()["error"]["code"], "RESPONSE_TOO_LARGE")
        peer.send(2)
        self.assert_pong(peer, 2)

    def test_invalid_response_does_not_break_a_connection(self):
        class InvalidResponseServer(ChatServer):
            def _handle_request(self, request):
                if request.request_id == 1:
                    return {"request_id": 1, "ok": True, "data": {"number": float("nan")}}
                return super()._handle_request(request)

        peer = self.peer(self.start(InvalidResponseServer))
        with self.assertLogs("socket_chat.server", level="ERROR"):
            peer.send(1)
            self.assertEqual(peer.read()["error"]["code"], "INTERNAL_ERROR")
        peer.send(2)
        self.assert_pong(peer, 2)
