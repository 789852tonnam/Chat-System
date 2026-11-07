"""Tkinter interaction tests; skip when a graphical display is unavailable."""

import gc
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import client
from client import ClientWindow
from common.protocol import error_response
from server import ChatServer, ServerConfig


class ClientUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Collect destroyed Tk windows on this thread before later network tests
        # can trigger cyclic GC from a worker and run Tk finalizers off-thread.
        if gc.isenabled():
            cls.addClassCleanup(gc.enable)
        gc.disable()
        cls.addClassCleanup(gc.collect)
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        root.destroy()

    def window(self, server=None, username="Alice"):
        window = ClientWindow(*(server.address if server else ("127.0.0.1", 5000)))
        window.username.set(username)
        window.withdraw()
        self.addCleanup(self.close_window, window)
        return window

    def close_window(self, window):
        if window.network is not None:
            window.network.stop()
            window.network.join(3)
            self.assertFalse(window.network.is_alive())
            window.network = None
        try:
            window.close()
        except tk.TclError:
            pass

    def server(self, server_class=ChatServer):
        server = server_class(ServerConfig(host="127.0.0.1", port=0, database_path=":memory:"))
        server.start()
        self.addCleanup(server.stop)
        return server

    def pump_until(self, window, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            window.update()
            time.sleep(0.005)
        self.assertTrue(predicate(), "UI did not reach the expected state")

    def test_invalid_address_does_not_start_a_worker(self):
        window = self.window()
        window.port.set("0")
        window.connect_button.invoke()
        self.assertIsNone(window.network)
        self.assertIn("between 1 and 65535", window.status.get())
        self.assertEqual(window.host.get(), "127.0.0.1")
        self.assertTrue(window.ping_button.instate(["disabled"]))

    def test_connect_ping_disconnect_and_reconnect(self):
        window = self.window(self.server())
        users = []
        for _ in range(2):
            window.connect_button.invoke()
            self.assertTrue(window.connect_button.instate(["disabled"]))
            self.pump_until(window, lambda: window._state == "connected")
            self.assertTrue(window.host_entry.instate(["readonly"]))
            self.assertTrue(window.username_entry.instate(["readonly"]))
            self.assertEqual(window.status.get(), "Joined as Alice")
            users.append(window.user)
            window.ping_button.invoke()
            self.assertTrue(window.ping_button.instate(["disabled"]))
            self.pump_until(window, lambda: window._state == "connected")
            self.assertIn("{'pong': True}", window.activity.get("1.0", "end"))
            worker = window.network
            window.disconnect_button.invoke()
            self.pump_until(window, lambda: window.network is None)
            self.assertFalse(worker.is_alive())
            self.assertFalse(window.connect_button.instate(["disabled"]))
            self.assertIsNone(window.user)
        self.assertEqual(users[0], users[1])

    def test_invalid_username_does_not_start_a_worker(self):
        window = self.window()
        window.username.set("invalid name")
        window.connect_button.invoke()
        self.assertIsNone(window.network)
        self.assertIn("Username must", window.status.get())

    def test_rejected_login_returns_to_the_join_form(self):
        class RejectingServer(ChatServer):
            def _handle_request(self, request, session):
                return error_response(request.request_id, "INVALID_PAYLOAD", "Name rejected")

        window = self.window(self.server(RejectingServer))
        window.connect()
        self.pump_until(window, lambda: window.network is None)
        self.assertIsNone(window.user)
        self.assertIn("Name rejected", window.activity.get("1.0", "end"))
        self.assertFalse(window.username_entry.instate(["readonly"]))

    def test_close_during_join_stops_the_network_worker(self):
        entered = threading.Event()
        release = threading.Event()

        class JoiningServer(ChatServer):
            def _handle_request(self, request, session):
                entered.set()
                release.wait(3)
                return super()._handle_request(request, session)

        window = self.window(self.server(JoiningServer))
        try:
            window.connect()
            self.pump_until(window, entered.is_set)
            self.assertEqual(window._state, "joining")
            worker = window.network
            window.close()
            self.pump_until(window, lambda: window.network is None)
            self.assertFalse(worker.is_alive())
        finally:
            release.set()

    def test_ui_remains_responsive_and_closes_while_waiting(self):
        entered = threading.Event()
        release = threading.Event()

        class WaitingServer(ChatServer):
            def _handle_request(self, request, session):
                if request.action == "ping":
                    entered.set()
                    release.wait(3)
                return super()._handle_request(request, session)

        window = self.window(self.server(WaitingServer))
        try:
            window.connect()
            self.pump_until(window, lambda: window._state == "connected")
            window.ping()
            self.pump_until(window, entered.is_set)
            heartbeat = threading.Event()
            window.after(10, heartbeat.set)
            self.pump_until(window, heartbeat.is_set)
            self.assertEqual(window._state, "waiting")
            window._next_directory_poll = time.monotonic() - 1
            window._poll_directory()
            self.assertTrue(window.network.requests.empty())
            worker = window.network
            window.close()
            self.pump_until(window, lambda: window.network is None)
            self.assertFalse(worker.is_alive())
        finally:
            release.set()

    def test_terminal_interrupt_stops_the_network_worker(self):
        window = self.window(self.server())
        window.connect()
        self.pump_until(window, lambda: window._state == "connected")
        worker = window.network
        with patch("sys.argv", ["client.py"]), patch("client.ClientWindow", return_value=window), \
                patch.object(window, "mainloop", side_effect=KeyboardInterrupt):
            client.main()
        self.assertFalse(worker.is_alive())
        self.assertIsNone(window.network)

    def test_directory_poll_updates_two_windows_and_preserves_selection(self):
        with patch("client.DIRECTORY_POLL_INTERVAL", 0.05):
            server = self.server()
            alice = self.window(server)
            bob = self.window(server, "Bob")
            alice.connect()
            self.pump_until(alice, lambda: len(alice.online_users.get_children()) == 1)
            bob.connect()
            self.pump_until(alice, lambda: len(alice.online_users.get_children()) == 2
                            and len(bob.online_users.get_children()) == 2)
            texts = [alice.online_users.item(key, "text") for key in alice.online_users.get_children()]
            self.assertEqual(texts, ["Alice (you)", "Bob"])
            self.assertEqual(alice.directory_status.get(), "Online users (2)")
            selected = f"user-{bob.user['id']}"
            alice.online_users.selection_set(selected)
            alice._next_directory_poll = time.monotonic() - 1
            self.pump_until(alice, lambda: alice._state == "refreshing")
            self.pump_until(alice, lambda: alice._state == "connected")
            self.assertEqual(alice.online_users.selection(), (selected,))
            bob.disconnect()
            self.pump_until(alice, lambda: bob.network is None and len(alice.online_users.get_children()) == 1)
            self.assertEqual(bob.online_users.get_children(), ())
            self.assertEqual(alice.online_users.item(alice.online_users.get_children()[0], "text"), "Alice (you)")

    def test_slow_directory_does_not_stack_polls_or_block_window_close(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowDirectoryServer(ChatServer):
            def _handle_request(self, request, session):
                if request.action == "get_directory":
                    entered.set()
                    release.wait(3)
                return super()._handle_request(request, session)

        window = self.window(self.server(SlowDirectoryServer))
        try:
            window.connect()
            self.pump_until(window, entered.is_set)
            self.assertEqual(window._state, "refreshing")
            self.assertTrue(window.ping_button.instate(["disabled"]))
            window._next_directory_poll = time.monotonic() - 1
            for _ in range(3):
                window._poll_directory()
            self.assertTrue(window.network.requests.empty())
            heartbeat = threading.Event()
            window.after(10, heartbeat.set)
            self.pump_until(window, heartbeat.is_set)
            window.close()
            self.pump_until(window, lambda: window.network is None)
        finally:
            release.set()
