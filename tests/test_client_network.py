"""Real TCP tests for the Tkinter-independent client network worker."""

import threading
import time
import unittest
from unittest.mock import patch

from client_network import NetworkClient
from common.protocol import MAX_RESPONSE_BYTES, encode_message
from server import ChatServer, ServerConfig


class ClientNetworkTests(unittest.TestCase):
    def server(self, server_class=ChatServer):
        server = server_class(ServerConfig(host="127.0.0.1", port=0, database_path=":memory:"))
        server.start()
        self.addCleanup(server.stop)
        return server

    def client(self, server, **options):
        client = NetworkClient(*server.address, **options)
        self.addCleanup(self.stop_client, client)
        client.start()
        self.event(client, "connected")
        return client

    def stop_client(self, client):
        client.stop()
        client.join(3)
        self.assertFalse(client.is_alive(), "Client worker did not stop")

    def event(self, client, expected):
        kind, value = client.events.get(timeout=3)
        self.assertEqual(kind, expected, value)
        return value

    def test_pings_share_one_connection_with_increasing_ids(self):
        client = self.client(self.server())
        for request_id in [1, 2]:
            client.requests.put(("ping", {}))
            self.assertEqual(self.event(client, "response"), {
                "request_id": request_id, "ok": True, "data": {"pong": True},
            })

    def test_server_error_does_not_disconnect_a_valid_connection(self):
        client = self.client(self.server())
        client.requests.put(("unknown", {}))
        self.assertEqual(self.event(client, "response")["error"]["code"], "UNKNOWN_ACTION")
        client.requests.put(("ping", {}))
        self.assertEqual(self.event(client, "response")["request_id"], 2)

    def test_username_login_logout_and_rejoin_on_one_connection(self):
        client = self.client(self.server())
        client.requests.put(("login", {"username": "Alice"}))
        alice = self.event(client, "response")["data"]["user"]
        self.assertEqual(alice["username"], "Alice")
        client.requests.put(("logout", {}))
        self.assertEqual(self.event(client, "response")["data"], {"logged_out": True})
        client.requests.put(("login", {"username": "Bob"}))
        self.assertNotEqual(self.event(client, "response")["data"]["user"]["id"], alice["id"])

    def test_invalid_login_user_response_closes_connection(self):
        for user in [None, {"id": True, "username": "Alice"},
                     {"id": 1, "username": "invalid name"}]:
            with self.subTest(user=user):
                class InvalidUserServer(ChatServer):
                    def _send_response(self, connection, response):
                        connection.sendall(encode_message({
                            "request_id": response["request_id"], "ok": True, "data": {"user": user},
                        }))
                        return True

                client = self.client(self.server(InvalidUserServer))
                client.requests.put(("login", {"username": "Alice"}))
                self.assertIn("invalid", self.event(client, "error"))
                self.event(client, "disconnected")
                self.stop_client(client)

    def test_explicit_reconnect_resets_request_ids(self):
        server = self.server()
        for _ in range(2):
            client = self.client(server)
            client.requests.put(("ping", {}))
            self.assertEqual(self.event(client, "response")["request_id"], 1)
            self.stop_client(client)
            self.event(client, "disconnected")

    def test_connection_failure_is_reported_without_retry(self):
        client = NetworkClient("127.0.0.1", 5000)
        self.addCleanup(self.stop_client, client)
        with patch("client_network.socket.create_connection", side_effect=OSError("Connection refused")) as connect:
            client.start()
            self.assertIn("Connection refused", self.event(client, "error"))
            self.event(client, "disconnected")
            client.join(3)
            connect.assert_called_once()

    def test_stop_wakes_an_idle_worker(self):
        client = self.client(self.server())
        self.stop_client(client)
        self.event(client, "disconnected")

    def test_stop_interrupts_waiting_for_a_response(self):
        entered = threading.Event()
        release = threading.Event()

        class WaitingServer(ChatServer):
            def _handle_request(self, request, session):
                entered.set()
                release.wait(3)
                return super()._handle_request(request, session)

        client = self.client(self.server(WaitingServer))
        try:
            client.requests.put(("ping", {}))
            self.assertTrue(entered.wait(2))
            self.stop_client(client)
            self.event(client, "disconnected")
        finally:
            release.set()

    def test_fragmented_utf8_response_is_reassembled(self):
        wire = encode_message({"request_id": 1, "ok": True, "data": {"text": "\u0e44\u0e17\u0e22"}})
        split = wire.index("\u0e44".encode()) + 1

        class FragmentedServer(ChatServer):
            def _send_response(self, connection, response):
                connection.sendall(wire[:split])
                time.sleep(0.02)
                connection.sendall(wire[split:])
                return True

        client = self.client(self.server(FragmentedServer))
        client.requests.put(("ping", {}))
        self.assertEqual(self.event(client, "response")["data"]["text"], "\u0e44\u0e17\u0e22")

    def test_invalid_oversized_and_incomplete_responses_close_connection(self):
        for wire in [
            b'{"request_id":2,"ok":true,"data":{}}\n',
            b'{"request_id":1,"ok":1,"data":{}}\n',
            b'{"request_id":1,"ok":true,"data":{},"data":{}}\n',
            b'[]\n', b'\xff\n', b'{"request_id":1}',
            b'x' * (MAX_RESPONSE_BYTES + 1),
            b'{"request_id":1,"ok":true,"data":{}}\n{}\n',
        ]:
            with self.subTest(wire_prefix=wire[:80]):
                class InvalidServer(ChatServer):
                    def _send_response(self, connection, response):
                        try:
                            connection.sendall(wire)
                        except OSError:
                            pass
                        return False

                client = self.client(self.server(InvalidServer))
                client.requests.put(("ping", {}))
                self.assertTrue(self.event(client, "error"))
                self.event(client, "disconnected")
                self.stop_client(client)

    def test_partial_bytes_do_not_extend_total_response_deadline(self):
        class SlowServer(ChatServer):
            def _send_response(self, connection, response):
                try:
                    connection.sendall(b'{')
                    for _ in range(10):
                        time.sleep(0.02)
                        connection.sendall(b' ')
                except OSError:
                    pass
                return False

        client = self.client(self.server(SlowServer), request_timeout=0.08)
        client.requests.put(("ping", {}))
        self.assertIn("timed out", self.event(client, "error"))
        self.event(client, "disconnected")
