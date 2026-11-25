"""Protocol and action checks. The UDP peer is simulated, never VirtualDJ."""
import socket
import threading
import unittest
from unittest.mock import patch

from afterhours.dj_bridge import BridgeError, VirtualDJ, as_bool, decode_osc, encode_osc, perform


class FakeDJ:
    def __init__(self, title="Example local track", automix=False, reply=True):
        self.title, self.automix, self.reply = title, automix, reply
        self.sent = []

    def status(self):
        return {"decks": [{"title": self.title}, {"title": self.title}], "automix": self.automix}

    def send(self, script):
        self.sent.append(script)
        return "/vdj/" + script.replace(" ", "/")

    def query(self, *_):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class BridgeTests(unittest.TestCase):
    def test_osc_wire_encoding(self):
        self.assertEqual(encode_osc("/x", 1), b"/x\0\0,i\0\0\0\0\0\1")

    def test_decode_types_and_unicode(self):
        address, values = decode_osc(encode_osc("/test", 128.5, -2, "Beyoncé", True, False, None))
        self.assertEqual(address, "/test")
        self.assertEqual(values, [128.5, -2, "Beyoncé", True, False, None])

    def test_malformed_messages_do_not_become_state(self):
        for data in (b"", b"/missing-null", b"/x\0\0,f\0\0\0", b"/x\0\0,z\0\0"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_osc(data)
        for value in (None, "", "playing", 0.4, float("nan")):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                as_bool(value)

    def test_preview_requires_no_socket_or_audio_application(self):
        result = perform(None, "automix-start", 1)
        self.assertFalse(result["sent"])
        self.assertEqual(result["mode"], "preview")

    def test_empty_deck_cannot_be_started(self):
        client = FakeDJ(title="")
        with self.assertRaises(BridgeError):
            perform(client, "play", 1, execute=True)
        self.assertEqual(client.sent, [])

    def test_connection_failure_prevents_action(self):
        client = FakeDJ()
        with patch.object(client, "status", side_effect=BridgeError("offline")):
            with self.assertRaises(BridgeError):
                perform(client, "play", 1, execute=True)
        self.assertEqual(client.sent, [])

    def test_action_not_retried_when_confirmation_is_lost(self):
        client = FakeDJ(reply=BridgeError("reply lost"))
        with patch("afterhours.dj_bridge.time.sleep"):
            result = perform(client, "play", 1, execute=True)
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"])
        self.assertEqual(client.sent, ["deck 1 play"])

    def test_takeover_only_disables_automix(self):
        client = FakeDJ(automix=True, reply=False)
        with patch("afterhours.dj_bridge.time.sleep"):
            result = perform(client, "takeover", 1, execute=True)
        self.assertTrue(result["verified"])
        self.assertEqual(client.sent, ["automix off"])

    def test_next_is_dispatched_once_and_not_claimed_completed(self):
        client = FakeDJ(automix=True)
        result = perform(client, "next", 1, execute=True)
        self.assertFalse(result["verified"])
        self.assertEqual(client.sent, ["automix_skip"])

    def test_next_requires_automix(self):
        client = FakeDJ(automix=False)
        with self.assertRaises(BridgeError):
            perform(client, "next", 1, execute=True)
        self.assertEqual(client.sent, [])

    def test_query_over_real_loopback_udp_to_simulated_peer(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(("127.0.0.1", 0))
            peer.settimeout(2)
            failures, observed = [], []
            def respond():
                try:
                    data, origin = peer.recvfrom(65535)
                    observed.append(decode_osc(data))
                    peer.sendto(encode_osc("/vdj/deck/1/get_title", "Simulated title"), origin)
                except Exception as exc:
                    failures.append(exc)
            thread = threading.Thread(target=respond, daemon=True)
            thread.start()
            with VirtualDJ(peer.getsockname()[1], timeout=1, reply_port=0) as client:
                result = client.query("deck 1 get_title", "text")
            thread.join(2)
            self.assertFalse(failures)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, "Simulated title")
            self.assertEqual(observed, [("/vdj/query/(text)/deck/1/get_title", [])])

    def test_boolean_wire_query_uses_explicit_numeric_branches(self):
        for result in (0, 1):
            with self.subTest(result=result), patch("afterhours.dj_bridge.socket.socket") as factory:
                address = "/vdj/deck/1/play/?/get_constant/1/:/get_constant/0"
                sock = factory.return_value
                sock.recvfrom.return_value = (encode_osc(address, result), ("127.0.0.1",7474))
                with VirtualDJ() as client:
                    self.assertEqual(as_bool(client.query("deck 1 play", "bool")), bool(result))
                request, values = decode_osc(sock.sendto.call_args.args[0])
                self.assertEqual(request, address.replace("/vdj/", "/vdj/query/", 1))
                self.assertEqual(values, [])

    def test_empty_decks_do_not_expose_placeholder_titles_or_enable_play(self):
        with patch("afterhours.dj_bridge.socket.socket"), VirtualDJ() as client:
            def read(script, kind=None):
                if script == "get_bpm": return 120
                if kind == "bool": return 0
                raise AssertionError("Empty deck metadata must not be read")
            with patch.object(client, "query", side_effect=read), patch.object(client, "send") as send:
                state = client.status()
                self.assertEqual([d["loaded"] for d in state["decks"]], [False,False])
                self.assertEqual([d["title"] for d in state["decks"]], ["", ""])
                with self.assertRaisesRegex(BridgeError, "Load a track"):
                    perform(client, "play", 1, execute=True)
                send.assert_not_called()

    def test_busy_reply_port_closes_socket_without_dispatch(self):
        with patch("afterhours.dj_bridge.socket.socket") as factory:
            sock = factory.return_value
            sock.bind.side_effect = OSError("already in use")
            with self.assertRaisesRegex(BridgeError, "No command was sent"):
                VirtualDJ()
            sock.close.assert_called_once()
            sock.sendto.assert_not_called()

    def test_reply_port_validation_happens_before_socket_creation(self):
        with patch("afterhours.dj_bridge.socket.socket") as factory:
            for value in (-1, 80, 65536, True, 7474, "7475"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    VirtualDJ(reply_port=value)
            factory.assert_not_called()


class DeadlineTests(unittest.TestCase):
    """A virtual clock makes slow replies deterministic without sleeping."""
    def setUp(self):
        self.now = 10.0
        self.clock_patch = patch("afterhours.dj_bridge.time.monotonic", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.socket_patch = patch("afterhours.dj_bridge.socket.socket")
        self.socket_factory = self.socket_patch.start()
        self.sock = self.socket_factory.return_value
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.socket_patch.stop)

    def test_several_queries_share_one_deadline(self):
        def reply(_size):
            remaining = self.sock.settimeout.call_args.args[0]
            self.now += min(.6, remaining)
            if remaining < .6:
                raise socket.timeout()
            return encode_osc("/vdj/get_bpm", 120), ("127.0.0.1", 7474)
        self.sock.recvfrom.side_effect = reply
        with VirtualDJ(timeout=.8, budget_seconds=1) as client:
            self.assertEqual(client.query("get_bpm"), 120)
            with self.assertRaisesRegex(BridgeError, "deadline"):
                client.query("get_bpm")
        self.assertAlmostEqual(self.now, 11)
        self.assertEqual(self.sock.sendto.call_count, 2)

    def test_expired_budget_blocks_queries_and_mutations_before_dispatch(self):
        with VirtualDJ(budget_seconds=1) as client:
            self.now += 1
            for operation in (lambda: client.query("get_bpm"), lambda: client.send("automix off")):
                with self.assertRaisesRegex(BridgeError, "deadline"):
                    operation()
        self.sock.sendto.assert_not_called()

    def test_timeout_after_dispatch_is_unverified_and_never_retried(self):
        with VirtualDJ(budget_seconds=1) as client:
            with patch.object(client, "status", return_value={"decks":[{"title":"Test"}]*2, "automix":False}):
                def expire(_seconds):
                    self.now += 1
                with patch("afterhours.dj_bridge.time.sleep", side_effect=expire):
                    result = perform(client, "play", 1, execute=True)
        self.assertTrue(result["sent"])
        self.assertFalse(result["verified"])
        self.assertIn("deadline", result["error"])
        self.assertEqual(self.sock.sendto.call_count, 1)
        self.assertEqual(decode_osc(self.sock.sendto.call_args.args[0])[0], "/vdj/deck/1/play")

    def test_invalid_budgets_fail_before_socket_creation(self):
        for value in (0, -1, True, float("nan"), float("inf"), "3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                VirtualDJ(budget_seconds=value)
        self.socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
