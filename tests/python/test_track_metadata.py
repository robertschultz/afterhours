"""Normalization fixtures only; no real Beatport, VirtualDJ or audio access."""
import json
import unittest
from afterhours.track_metadata import FACT_FIELDS, WHEEL, finite_number, musical_key, vdj_track


class TrackMetadataTests(unittest.TestCase):
    def raw(self, **changes):
        return {"id": "a" * 64, "folder": {"id": "b" * 64}, "title": "Synthetic mix",
                "artist": "Fixture artist", "bpm": 128, "key": "Am", **changes}

    def test_all_24_keys_round_trip_through_canonical_names(self):
        for mode, codes in WHEEL.items():
            for code in codes:
                with self.subTest(code=code):
                    name, normalized = musical_key(code)
                    self.assertTrue(name.endswith(mode))
                    self.assertEqual(normalized, code)
                    self.assertEqual(musical_key(name), (name, code))

    def test_enharmonic_unicode_and_common_key_notation(self):
        for value, expected in (("A minor", "8A"), ("Am", "8A"), ("a MINOR", "8A"),
                                ("AM", "11B"), ("A", "11B"), ("08a", "8A"),
                                ("F♯ minor", "11A"), ("Gbmin", "11A"),
                                ("D♭ major", "3B"), ("C#maj", "3B"), ("B#", "8B")):
            with self.subTest(value=value):
                self.assertEqual(musical_key(value)[1], expected)

    def test_unknown_ambiguous_and_invalid_keys_are_not_guessed(self):
        for value in (None, True, 8, "", "unknown", "0A", "13B", "8m", "A/B", "C dorian", "Am & play"):
            with self.subTest(value=value):
                self.assertEqual(musical_key(value), (None, None))

    def test_numbers_reject_booleans_nonfinite_and_out_of_range_values(self):
        for value in (True, False, None, {}, float("nan"), float("inf"), "NaN", "120 BPM", 0, -1, 401):
            with self.subTest(value=value):
                self.assertIsNone(finite_number(value, 20, 400))
        self.assertEqual(finite_number("128.5", 20, 400), 128.5)

    def test_unit_conversion_is_explicit_and_source_is_not_provider_guessed(self):
        track = vdj_track(self.raw(duration_ms="372125", genre="House", year="2026"), observed_at="2026-10-25T12:00:00Z")
        self.assertEqual(track["duration"], 372.125)
        self.assertEqual(track["year"], 2026)
        self.assertEqual(track["id"], "virtualdj:" + "a" * 64)
        self.assertEqual(track["engine"]["selection_id"], "a" * 64)
        self.assertIsNone(track["catalog"]["provider"])
        self.assertFalse(track["playback_verified"])
        self.assertEqual(track["provenance"]["duration"]["transform"], "milliseconds-to-seconds")

    def test_bpm_does_not_imply_energy_or_phrase_analysis(self):
        track = vdj_track(self.raw(bpm=180, energy=.99, beatgrid=[0, .33], drop=30))
        self.assertIsNone(track["energy"])
        self.assertNotIn("beatgrid", track)
        self.assertNotIn("drop", track)
        self.assertIsNone(track["provenance"]["bpm"]["confidence"])

    def test_only_selected_facts_survive_and_input_is_not_mutated(self):
        raw = self.raw(token="private-token", filepath="/private/music", url="https://private.invalid", folder={"id":"b"*64,"path":"/private/folder"})
        before = json.dumps(raw)
        track = vdj_track(raw)
        self.assertEqual(json.dumps(raw), before)
        self.assertNotIn("private", json.dumps(track))
        self.assertEqual(set(track["provenance"]), set(FACT_FIELDS))

    def test_invalid_identity_is_rejected_and_missing_values_stay_null(self):
        for change in ({"id":None}, {"id":5}, {"id":"wrong"}, {"folder":None}, {"folder":{"id":True}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                vdj_track(self.raw(**change))
        track = vdj_track(self.raw(bpm=True, key="?", year="2026.5", duration_ms=0))
        for field in ("bpm", "key", "camelot", "year", "duration", "energy"):
            self.assertIsNone(track[field])
            self.assertIn(field, track["missing_fields"])
        json.dumps(track, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
