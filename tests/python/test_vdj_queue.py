"""Synthetic Automix metadata, not real VirtualDJ or provider validation."""
import unittest

from afterhours.dj_bridge import BridgeError
from afterhours.vdj_queue import queue_status


class QueuePeer:
    def __init__(self):
        self.active = True
        self.count = 8
        self.position = 4
        self.rows = [("Synthetic next", "Test artist A"),
                     ("Synthetic second", "Test artist B"),
                     ("Synthetic third", "Test artist C")]
        self.queries = []
        self.before_query = None

    def query(self, script, kind=None):
        self.queries.append((script, kind))
        if self.before_query:
            self.before_query(self, script, kind)
        if script == "automix":
            return self.active
        if script == "file_count automix":
            return self.count
        if script == "get_automix_position":
            return self.position
        parts = script.split()
        if parts[0] == "get_automix_song":
            offset = int(parts[2]) if len(parts) > 2 else 1
            field = {"'title'": 0, "'artist'": 1}[parts[1]]
            return self.rows[offset - 1][field]
        raise AssertionError(f"Unexpected query: {script}")

    def send(self, script):
        raise AssertionError(f"Preview must not dispatch: {script}")


class QueueTests(unittest.TestCase):
    def test_three_relative_rows_without_browser_focus_or_identity_claim(self):
        peer = QueuePeer()
        result = queue_status(peer)
        self.assertEqual(result["upcoming"], [
            {"offset": 1, "title": "Synthetic next", "artist": "Test artist A"},
            {"offset": 2, "title": "Synthetic second", "artist": "Test artist B"},
            {"offset": 3, "title": "Synthetic third", "artist": "Test artist C"},
        ])
        self.assertEqual(result["queue_count"], 8)
        self.assertEqual(result["engine_position"], 4)
        self.assertTrue(result["read_only"])
        for field in ("complete", "snapshot_atomic", "playback_verified"):
            self.assertFalse(result[field])
        self.assertEqual(len(peer.queries), 21)
        self.assertFalse(any("brows" in script or "filepath" in script for script, _ in peer.queries))

    def test_automix_off_preserves_count_without_querying_rows_or_starting(self):
        peer = QueuePeer()
        peer.active = False
        result = queue_status(peer)
        self.assertEqual(result["availability"], "automix-off")
        self.assertEqual(result["queue_count"], 8)
        self.assertEqual(result["upcoming"], [])
        self.assertIsNone(result["engine_position"])
        self.assertEqual({q for q, _ in peer.queries}, {"automix", "file_count automix"})

    def test_zero_count_is_distinct_from_missing_metadata(self):
        peer = QueuePeer()
        peer.count = 0
        self.assertEqual(queue_status(peer)["availability"], "empty")
        self.assertFalse(any(q.startswith("get_automix") for q, _ in peer.queries))
        peer = QueuePeer()
        peer.rows = [("", "")] * 3
        result = queue_status(peer)
        self.assertEqual(result["availability"], "metadata-unavailable")
        self.assertEqual(result["queue_count"], 8)
        self.assertEqual(result["missing_offsets"], [1, 2, 3])

    def test_blank_fields_do_not_renumber_offsets_or_infer_end_of_queue(self):
        peer = QueuePeer()
        peer.rows = [("", ""), ("", "Artist only"), ("Title only", "")]
        result = queue_status(peer)
        self.assertEqual(result["availability"], "available")
        self.assertEqual([r["offset"] for r in result["upcoming"]], [2, 3])
        self.assertEqual(result["missing_offsets"], [1])
        self.assertFalse(result["complete"])

    def test_duplicate_titles_remain_separate_upcoming_offsets(self):
        peer = QueuePeer()
        peer.rows = [("Same title", "Same artist")] * 3
        self.assertEqual(len(queue_status(peer)["upcoming"]), 3)

    def test_changed_automix_count_or_position_discards_snapshot(self):
        for field, value in (("active", False), ("count", 9), ("position", 5)):
            for checkpoint in (2, 3):
                with self.subTest(field=field, checkpoint=checkpoint):
                    peer = QueuePeer()
                    def change(p, script, kind):
                        if script == "automix" and p.queries.count(("automix", "bool")) == checkpoint:
                            setattr(p, field, value)
                    peer.before_query = change
                    with self.assertRaisesRegex(BridgeError, "changed"):
                        queue_status(peer)

    def test_same_length_reorder_between_passes_discards_snapshot(self):
        peer = QueuePeer()
        def reorder(p, script, kind):
            if script == "get_automix_song 'title'" and p.queries.count((script, kind)) == 2:
                p.rows[0], p.rows[1] = p.rows[1], p.rows[0]
        peer.before_query = reorder
        with self.assertRaisesRegex(BridgeError, "Upcoming tracks changed"):
            queue_status(peer)

    def test_invalid_values_fail_without_partial_result(self):
        for field, value in (("active", "unknown"), ("count", -1), ("position", 1.5), ("position", float("nan"))):
            with self.subTest(field=field):
                peer = QueuePeer()
                setattr(peer, field, value)
                with self.assertRaises(BridgeError):
                    queue_status(peer)
        peer = QueuePeer()
        peer.rows[1] = (123, "Test artist")
        with self.assertRaises(BridgeError):
            queue_status(peer)

    def test_lost_reply_discards_partial_metadata_without_retry(self):
        peer = QueuePeer()
        def fail(p, script, kind):
            if script == "get_automix_song 'artist' 2":
                raise BridgeError("Synthetic lost reply")
        peer.before_query = fail
        with self.assertRaisesRegex(BridgeError, "lost reply"):
            queue_status(peer)
        self.assertEqual(peer.queries.count(("get_automix_song 'artist' 2", "text")), 1)


if __name__ == "__main__":
    unittest.main()
