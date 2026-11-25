"""Catalog protocol guards, using synthetic data, not a real music service."""
import unittest
from unittest.mock import patch

from afterhours.dj_bridge import BridgeError, VirtualDJ, encode_osc, decode_osc
from afterhours.vdj_catalog import (catalog_search, catalog_status, count_value, identity,
                         load_selected, queue_selected, search_literal, selection)


class CatalogPeer:
    def __init__(self):
        self.folder = "synthetic://catalog"
        self.locator = "synthetic://track/42"
        self.loaded_locator = ""
        self.playing = False
        self.automix = False
        self.deck_error = False
        self.confirmation_error = False
        self.wrong_track = False
        self.queue_count = 2
        self.sent = []

    def query(self, script, kind=None):
        if self.sent and self.confirmation_error:
            raise BridgeError("Simulated lost acknowledgement")
        values = {
            "get_build": 9729,
            "get_browsed_folder_path": self.folder,
            "get_browsed_folder": "Synthetic test catalog",
            "file_count": 3,
            "file_count automix": self.queue_count,
            "get_browsed_filepath": self.locator,
            "get_browsed_title": "Synthetic track",
            "get_browsed_artist": "Test artist",
            "get_browsed_bpm": 120,
            "get_browsed_key": "8A",
            "get_browsed_album": "Synthetic release",
            "get_browsed_genre": "House",
            "get_browsed_song 'remix'": "Extended Mix",
            "get_browsed_song 'label'": "Fixture label",
            "get_browsed_song 'year'": "2026",
            "get_browsed_song 'length' 'ms'": 367250,
            "automix": self.automix,
            "deck 2 play": self.playing,
            "deck 2 volume": 1,
            "deck 2 pitch": 1,
            "deck 2 pfl": False,
            "deck 2 loaded": bool(self.loaded_locator),
            "deck 2 deck_has_error": self.deck_error,
            "deck 2 get_filepath": self.loaded_locator,
        }
        return values[script]

    def send(self, script):
        self.sent.append(script)
        if script.endswith("deck 2 load"):
            self.loaded_locator = "synthetic://wrong-track" if self.wrong_track else self.locator
        if script == "playlist_add":
            self.queue_count += 1
        return "/vdj/" + VirtualDJ.path(script)

    def options(self):
        return {"expected_id": identity(self.locator),
                "expected_folder": identity(self.folder), "execute": True}


class CatalogTests(unittest.TestCase):
    def test_preview_never_needs_an_application(self):
        for result in (catalog_search(None, "Blue Hour"), load_selected(None, 2), queue_selected(None)):
            self.assertFalse(result["sent"])
            self.assertEqual(result["mode"], "preview")

    def test_unsafe_keywords_cannot_become_script_or_osc_patterns(self):
        for value in ('x" & play', "`play`", "AC/DC", "foo\\bar", "foo\nplay", "foo*", "foo?", "[abc]", "{a,b}", "", "x" * 161):
            with self.subTest(value=value), self.assertRaises(ValueError):
                search_literal(value)

    def test_unicode_and_quoted_normal_keywords_round_trip(self):
        script = "search " + search_literal("Beyoncé  Don't R&B")
        address = "/vdj/" + VirtualDJ.path(script)
        wire_address, values = decode_osc(encode_osc(address))
        self.assertEqual(wire_address.replace("/", " ").strip(), 'vdj search "Beyoncé Don\'t R&B"')
        self.assertEqual(values, [])

    def test_read_only_catalog_snapshot_excludes_private_locators(self):
        peer = CatalogPeer()
        result = catalog_status(peer)
        self.assertEqual(result["selected"]["id"], identity(peer.locator))
        self.assertEqual(result["visible_count"], 3)
        self.assertFalse(result["playback_verified"])
        self.assertNotIn(peer.locator, str(result))
        self.assertNotIn(peer.folder, str(result))
        self.assertEqual(peer.sent, [])

    def test_catalog_metadata_is_normalized_without_claiming_beatport_or_audio(self):
        peer = CatalogPeer()
        result = catalog_status(peer)
        track = result["selected"]["metadata"]
        self.assertEqual(track["bpm"], 120)
        self.assertEqual(track["key"], "A minor")
        self.assertEqual(track["camelot"], "8A")
        self.assertEqual(track["duration"], 367.25)
        self.assertEqual(track["genre"], "House")
        self.assertEqual(track["remix"], "Extended Mix")
        self.assertEqual(track["year"], 2026)
        self.assertEqual(track["catalog"], {"provider": None, "track_id": None})
        self.assertIsNone(track["energy"])
        self.assertIsNone(track["provenance"]["bpm"]["confidence"])
        self.assertFalse(result["complete_catalog"])
        self.assertFalse(result["snapshot_atomic"])
        self.assertEqual(peer.sent, [])

    def test_selection_change_during_details_discards_the_entire_snapshot(self):
        peer = CatalogPeer()
        original = peer.query
        def query(script, kind=None):
            if script == "get_browsed_genre":
                peer.locator = "synthetic://changed-during-details"
            return original(script, kind)
        with patch.object(peer, "query", side_effect=query), self.assertRaises(BridgeError):
            catalog_status(peer)
        self.assertEqual(peer.sent, [])

    def test_transport_selection_guards_do_not_read_optional_metadata(self):
        peer = CatalogPeer()
        original = peer.query
        def query(script, kind=None):
            if script in ("get_browsed_genre", "get_browsed_album") or script.startswith("get_browsed_song"):
                raise AssertionError("Optional metadata delayed a transport guard")
            return original(script, kind)
        with patch.object(peer, "query", side_effect=query):
            selected = selection(peer)
        self.assertNotIn("metadata", selected)

    def test_missing_detail_values_do_not_become_zero_or_fabricated_analysis(self):
        peer = CatalogPeer()
        original = peer.query
        def query(script, kind=None):
            if script == "get_browsed_song 'length' 'ms'":
                return -1
            if script.startswith("get_browsed_song") or script in ("get_browsed_album", "get_browsed_genre"):
                return ""
            return original(script, kind)
        with patch.object(peer, "query", side_effect=query):
            track = catalog_status(peer)["selected"]["metadata"]
        self.assertIsNone(track["duration"])
        self.assertIsNone(track["genre"])
        self.assertIn("duration", track["missing_fields"])

    def test_unsupported_optional_details_leave_guarded_basic_metadata_available(self):
        peer = CatalogPeer()
        original = peer.query
        queries = []
        def query(script, kind=None):
            queries.append(script)
            if script == "get_browsed_genre":
                raise BridgeError("Unsupported optional query")
            return original(script, kind)
        with patch.object(peer, "query", side_effect=query):
            selected = catalog_status(peer)["selected"]
        self.assertFalse(selected["detail_read_complete"])
        self.assertEqual(selected["metadata"]["bpm"], 120)
        self.assertEqual(selected["metadata"]["album"], "Synthetic release")
        self.assertIn("genre", selected["unread_fields"])
        self.assertNotIn("get_browsed_song 'length' 'ms'", queries)
        self.assertGreater(queries.count("get_browsed_filepath"), 1)
        self.assertEqual(peer.sent, [])

    def test_changed_selection_does_not_produce_a_mixed_snapshot(self):
        peer = CatalogPeer()
        original = peer.query
        seen = 0
        def query(script, kind=None):
            nonlocal seen
            if script == "get_browsed_filepath":
                seen += 1
                if seen == 2:
                    return "synthetic://different-row"
            return original(script, kind)
        with patch.object(peer, "query", side_effect=query), self.assertRaises(BridgeError):
            selection(peer)

    def test_search_refuses_a_different_folder(self):
        peer = CatalogPeer()
        with self.assertRaises(BridgeError):
            catalog_search(peer, "Blue Hour", "wrong-folder", True)
        self.assertEqual(peer.sent, [])

    def test_search_dispatch_is_not_misreported_as_results(self):
        peer = CatalogPeer()
        result = catalog_search(peer, "Blue Hour", identity(peer.folder), True)
        self.assertFalse(result["verified"])
        self.assertEqual(peer.sent, ['search "Blue Hour"'])
        self.assertNotIn("selected", result)

    def test_loading_refuses_playing_decks_and_automix(self):
        for field in ("playing", "automix"):
            peer = CatalogPeer()
            setattr(peer, field, True)
            with self.subTest(field=field), self.assertRaises(BridgeError):
                load_selected(peer, 2, **peer.options())
            self.assertEqual(peer.sent, [])

    def test_loading_requires_current_selected_track(self):
        peer = CatalogPeer()
        options = peer.options()
        options["expected_id"] = "stale"
        with self.assertRaises(BridgeError):
            load_selected(peer, 2, **options)
        self.assertEqual(peer.sent, [])

    def test_expected_loaded_identity_is_verified_without_claiming_audio(self):
        peer = CatalogPeer()
        with patch("afterhours.vdj_catalog.time.sleep"):
            result = load_selected(peer, 2, **peer.options())
        self.assertTrue(result["verified"])
        self.assertFalse(result["playback_verified"])
        self.assertEqual(peer.sent, ["deck 2 play ? nothing : deck 2 load"])

    def test_wrong_track_and_lost_reply_are_not_retried_or_started(self):
        for field in ("wrong_track", "confirmation_error", "deck_error"):
            peer = CatalogPeer()
            setattr(peer, field, True)
            with self.subTest(field=field), patch("afterhours.vdj_catalog.time.sleep"):
                result = load_selected(peer, 2, **peer.options())
            self.assertTrue(result["sent"])
            self.assertFalse(result["verified"])
            self.assertEqual(len(peer.sent), 1)
            self.assertFalse(any(s.endswith(" play") for s in peer.sent))

    def test_queue_count_is_not_identity_confirmation(self):
        peer = CatalogPeer()
        result = queue_selected(peer, **peer.options())
        self.assertEqual(result["queue_count_after"], 3)
        self.assertFalse(result["verified"])
        self.assertEqual(peer.sent, ["playlist_add"])

    def test_invalid_counts_are_not_treated_as_catalog_state(self):
        for value in (True, -1, 1.2, float("nan"), "unknown"):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                count_value(value)


if __name__ == "__main__":
    unittest.main()
