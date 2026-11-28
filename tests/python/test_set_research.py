"""Keep research suggestions scoped to native discovery and out of playback decisions."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from afterhours import set_research


CATALOG_TRACK = {
    'artist': 'The Martinez Brothers', 'title': 'H2daizzo',
    'source_url': 'https://music.apple.com/us/song/1815904182?tracking=1',
    'bpm': 122, 'engine': {'selection_id': 'untrusted'},
}


class SetResearchTests(unittest.TestCase):
    def test_only_public_track_hints_and_unique_recordings(self):
        rogue = {**CATALOG_TRACK, 'source_url': 'https://music.apple.com@other.example/us/song/1815904182'}
        release = {**CATALOG_TRACK, 'source_url': 'https://music.apple.com/us/album/1815904182'}
        rows = set_research.candidates({'candidates': [rogue, release, CATALOG_TRACK, CATALOG_TRACK]})
        self.assertEqual(rows, [{
            'artist': 'The Martinez Brothers', 'title': 'H2daizzo',
            'source_url': 'https://music.apple.com/us/song/1815904182', 'version': None,
        }])

    def test_catalog_reply_accepts_one_json_block(self):
        body = json.dumps({'candidates': [CATALOG_TRACK]})
        self.assertEqual(len(set_research.parse_catalog_reply('```json\n' + body + '\n```')), 1)
        with self.assertRaisesRegex(ValueError, 'candidate list'):
            set_research.parse_catalog_reply('{}')

    def test_stale_unrelated_and_completed_hints_are_never_used(self):
        with tempfile.TemporaryDirectory() as root, patch('afterhours.set_research.data_root', return_value=Path(root)):
            path = Path(root) / 'set-research.json'
            path.write_text(json.dumps({'style': 'house', 'anchor': 'Dom Dolla', 'researched_at': 100,
                                        'candidates': [CATALOG_TRACK]}))
            set_order = [{'artist': 'Dom Dolla & Go Freek', 'title': 'Define'}]
            self.assertEqual(len(set_research.hints('house', set_order, (), now=105)), 1)
            self.assertEqual(set_research.hints('tech house', set_order, (), now=105), [])
            self.assertEqual(set_research.hints('house', [], (), now=105), [])
            self.assertEqual(set_research.hints('house', set_order, (), now=105 + set_research.TTL), [])
            self.assertEqual(set_research.hints('house', set_order + [CATALOG_TRACK], (), now=105), [])
            self.assertEqual(set_research.hints('house', set_order, {'the martinez brothers h2daizzo'}, now=105), [])
