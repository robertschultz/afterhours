"""The local HTTP boundary, with a fake DJ implementation and no audio output."""
import http.client
import json
import socket
import threading
import unittest
from unittest.mock import MagicMock, patch
from http.server import ThreadingHTTPServer

from afterhours import server
from afterhours.dj_bridge import VirtualDJ, BridgeError, decode_osc, encode_osc
from afterhours.vdj_api import request
from test_vdj_queue import QueuePeer
from test_vdj_catalog import CatalogPeer


class APIValidationTests(unittest.TestCase):
    def test_preview_uses_no_socket(self):
        with patch("afterhours.vdj_api.VirtualDJ", side_effect=AssertionError("Unexpected connection")):
            result = request({"action":"load-selected", "deck":2})
        self.assertEqual(result["mode"], "preview")
        self.assertFalse(result["sent"])

    def test_unknown_commands_and_destinations_are_rejected(self):
        for data in ({"action":"script", "query":"play"},
                     {"action":"play", "port":9999},
                     {"action":"play", "execute":"true"},
                     {"action":"play", "deck":True},
                     {"action":"play", "folder":"bad"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                request(data)

    def test_http_operations_have_whole_request_budgets(self):
        for action, budget in (("status", 2.5), ("catalog-status", 4), ("queue-status", 4), ("takeover", 6)):
            with self.subTest(action=action):
                factory = MagicMock()
                with patch("afterhours.vdj_api.catalog_status"), patch("afterhours.vdj_api.queue_status"), patch("afterhours.vdj_api.perform"):
                    request({"action":action, "execute":True}, client_factory=factory)
                factory.assert_called_once_with(7474, timeout=.8, budget_seconds=budget)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fixtures reply to the sender. Never contend with a live DJ's fixed
        # 7475 reply port while testing the HTTP adapter.
        cls.adapter = patch('afterhours.server.vdj_request', side_effect=lambda data,port: request(
            data,port,client_factory=lambda *args,**kwargs: VirtualDJ(*args,**kwargs,reply_port=0)))
        cls.adapter.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
        cls.port = cls.httpd.server_port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(2)
        cls.adapter.stop()

    def call(self, method, path, data=None, token=True, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type":"application/json"}
        if token:
            headers["X-DJ-Token"] = server.TOKEN
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, body=None if data is None else json.dumps(data), headers=headers)
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        return response.status, body

    def test_reads_require_local_session_token(self):
        with patch("afterhours.server.vdj_request") as operation:
            for path in ("status", "catalog", "queue"):
                self.assertEqual(self.call("GET", f"/api/vdj/{path}", token=False)[0],403)
            operation.assert_not_called()

    def test_queue_preview_uses_only_queries_through_real_http_and_udp(self):
        synthetic = QueuePeer()
        stop = threading.Event()
        failures = []
        before = list(server.COMMANDS)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(("127.0.0.1", 0))
            peer.settimeout(.1)
            self.httpd.vdj_port = peer.getsockname()[1]
            def respond():
                while not stop.is_set():
                    try:
                        packet, origin = peer.recvfrom(65535)
                    except socket.timeout:
                        continue
                    try:
                        address, values = decode_osc(packet)
                        self.assertTrue(address.startswith("/vdj/query/"))
                        self.assertEqual(values, [])
                        script = address.removeprefix("/vdj/query/")
                        kind = None
                        for marker in ("bool", "text"):
                            if script.startswith(f"({marker})/"):
                                kind = marker
                                script = script.removeprefix(f"({marker})/")
                        suffix = "/?/get_constant/1/:/get_constant/0"
                        if script.endswith(suffix):
                            script = script.removesuffix(suffix)
                            kind = "bool"
                        value = synthetic.query(script.replace("/", " "), kind)
                        peer.sendto(encode_osc(address.replace("/vdj/query/", "/vdj/"), value), origin)
                    except Exception as exc:
                        failures.append(exc)
                        return
            thread = threading.Thread(target=respond, daemon=True)
            thread.start()
            try:
                status, body = self.call("GET", "/api/vdj/queue")
                self.assertEqual(status, 200)
                self.assertEqual(body["availability"], "available")
                self.assertEqual(body["upcoming"][0]["title"], "Synthetic next")
                self.assertEqual(len(synthetic.queries), 21)
                self.assertEqual(server.COMMANDS, before)
            finally:
                stop.set()
                thread.join(2)
                del self.httpd.vdj_port
        self.assertEqual(failures, [])
        self.assertFalse(thread.is_alive())

    def test_failed_queue_read_returns_no_partial_rows_and_releases_lock(self):
        with patch("afterhours.server.vdj_request", side_effect=BridgeError("Synthetic changed queue")):
            status, body = self.call("GET", "/api/vdj/queue")
        self.assertEqual(status, 503)
        self.assertNotIn("upcoming", body)
        self.assertFalse(server.VDJ_LOCK.locked())

    def test_rich_catalog_over_real_http_and_udp_is_read_only(self):
        synthetic = CatalogPeer()
        stop, failures, queries = threading.Event(), [], []
        before = list(server.COMMANDS)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(("127.0.0.1", 0))
            peer.settimeout(.1)
            self.httpd.vdj_port = peer.getsockname()[1]
            def respond():
                while not stop.is_set():
                    try:
                        packet, origin = peer.recvfrom(65535)
                    except socket.timeout:
                        continue
                    try:
                        address, values = decode_osc(packet)
                        self.assertTrue(address.startswith("/vdj/query/"))
                        self.assertEqual(values, [])
                        script = address.removeprefix("/vdj/query/")
                        kind = "text" if script.startswith("(text)/") else None
                        if kind:
                            script = script.removeprefix("(text)/")
                        script = script.replace("/", " ")
                        queries.append(script)
                        value = synthetic.query(script, kind)
                        peer.sendto(encode_osc(address.replace("/vdj/query/", "/vdj/"), value), origin)
                    except Exception as exc:
                        failures.append(exc)
                        return
            thread = threading.Thread(target=respond, daemon=True)
            thread.start()
            try:
                status, body = self.call("GET", "/api/vdj/catalog")
                self.assertEqual(status, 200)
                track = body["selected"]["metadata"]
                self.assertEqual(track["duration"], 367.25)
                self.assertEqual(track["camelot"], "8A")
                self.assertIsNone(track["energy"])
                self.assertFalse(track["playback_verified"])
                self.assertIn("get_browsed_song 'length' 'ms'", queries)
                self.assertEqual(synthetic.sent, [])
                self.assertEqual(server.COMMANDS, before)
                self.assertNotIn(synthetic.locator, json.dumps(body))
            finally:
                stop.set()
                thread.join(2)
                del self.httpd.vdj_port
        self.assertEqual(failures, [])
        self.assertFalse(thread.is_alive())

    def test_foreign_origins_cannot_dispatch(self):
        with patch("afterhours.server.vdj_request") as operation:
            status, _ = self.call("POST", "/api/vdj/command", {"action":"play","execute":True}, origin="https://example.invalid")
            self.assertEqual(status,403)
            operation.assert_not_called()

    def test_vdj_requests_do_not_enter_web_audio_command_queue(self):
        before = list(server.COMMANDS)
        status, body = self.call("POST", "/api/vdj/command", {"action":"play"})
        self.assertEqual(status,200)
        self.assertEqual(body["mode"], "preview")
        self.assertEqual(server.COMMANDS, before)

    def test_connection_failures_are_visible(self):
        with patch("afterhours.server.vdj_request", side_effect=BridgeError("Simulated offline engine")):
            status, body = self.call("GET", "/api/vdj/status")
        self.assertEqual(status,503)
        self.assertFalse(body["connected"])

    def test_simultaneous_requests_are_rejected_instead_of_queued(self):
        with server.VDJ_LOCK, patch("afterhours.server.vdj_request") as operation:
            status, _ = self.call("POST", "/api/vdj/command", {"action":"play"})
            self.assertEqual(status,409)
            operation.assert_not_called()

    def test_bad_requests_are_errors(self):
        status, body = self.call("POST", "/api/vdj/command", {"action":"arbitrary_script"})
        self.assertEqual(status,400)
        self.assertIn("error", body)

    def test_individual_transport_cannot_compete_with_scheduled_set(self):
        with patch.object(server.DJ_SESSION, 'state', {'active': True}), patch('afterhours.server.vdj_request') as operation:
            for action in ('play', 'pause'):
                status, body = self.call('POST', '/api/vdj/command', {'action':action,'deck':1,'execute':True})
                self.assertEqual(status,409)
                self.assertIn('Take over',body['error'])
                # A response can arrive before the handler's finally block.
                # Wait for that release before the next independent request.
                self.assertTrue(server.VDJ_LOCK.acquire(timeout=1))
                server.VDJ_LOCK.release()
            operation.assert_not_called()
        self.assertFalse(server.VDJ_LOCK.locked())

    def test_slow_osc_read_releases_http_lock_at_whole_request_deadline(self):
        # Real loopback HTTP and UDP, but a deliberately slow synthetic engine.
        stop = threading.Event()
        failures = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(("127.0.0.1", 0))
            peer.settimeout(.1)
            self.httpd.vdj_port = peer.getsockname()[1]
            def respond():
                while not stop.is_set():
                    try:
                        packet, origin = peer.recvfrom(65535)
                    except socket.timeout:
                        continue
                    try:
                        address, _ = decode_osc(packet)
                        if stop.wait(.35):
                            return
                        # Loaded=true makes this exercise the full status read,
                        # which must stop at the whole-request deadline.
                        value = 1 if "/?/get_constant/" in address else "Synthetic" if "(text)" in address else 120
                        peer.sendto(encode_osc(address.replace("/vdj/query/", "/vdj/"), value), origin)
                    except Exception as exc:
                        failures.append(exc)
                        return
            thread = threading.Thread(target=respond, daemon=True)
            thread.start()
            try:
                status, body = self.call("GET", "/api/vdj/status")
                self.assertEqual(status, 503)
                self.assertIn("deadline", body["error"])
                status, body = self.call("POST", "/api/vdj/command", {"action":"takeover"})
                self.assertEqual(status, 200)
                self.assertFalse(body["sent"])
            finally:
                stop.set()
                thread.join(2)
                del self.httpd.vdj_port
        self.assertFalse(failures)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
