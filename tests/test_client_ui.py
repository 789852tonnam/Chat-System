"""Tkinter interaction tests; skip when a graphical display is unavailable."""

import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import client
from client import ClientWindow
from server import ChatServer, ServerConfig


class ClientUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        root.destroy()

    def window(self, server=None):
        window = ClientWindow(*(server.address if server else ("127.0.0.1", 5000)))
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
        server = server_class(ServerConfig(host="127.0.0.1", port=0))
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
        for _ in range(2):
            window.connect_button.invoke()
            self.assertTrue(window.connect_button.instate(["disabled"]))
            self.pump_until(window, lambda: window._state == "connected")
            self.assertTrue(window.host_entry.instate(["readonly"]))
            window.ping_button.invoke()
            self.assertTrue(window.ping_button.instate(["disabled"]))
            self.pump_until(window, lambda: window._state == "connected")
            self.assertIn("Response #1: {'pong': True}", window.activity.get("1.0", "end"))
            worker = window.network
            window.disconnect_button.invoke()
            self.pump_until(window, lambda: window.network is None)
            self.assertFalse(worker.is_alive())
            self.assertFalse(window.connect_button.instate(["disabled"]))

    def test_ui_remains_responsive_and_closes_while_waiting(self):
        entered = threading.Event()
        release = threading.Event()

        class WaitingServer(ChatServer):
            def _handle_request(self, request):
                entered.set()
                release.wait(3)
                return super()._handle_request(request)

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
