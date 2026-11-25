"""Native grid-anchor regression; simulated readbacks, real measured cue policy."""
import threading
import unittest
from unittest.mock import patch

from afterhours.dj_bridge import BridgeError
from test_cue_planner import fixture
from afterhours.vdj_catalog import identity
from afterhours.vdj_session import DJSession


class PausedGridPeer:
    """A fixed native grid can begin before the first playable sample."""
    locator = '/fixture/negative-grid-anchor.flac'

    def __init__(self, anchor=-.002, negative_at=None):
        self.anchor = anchor
        self.position = anchor
        self.beat = 0
        self.commands = []
        self.readbacks = []
        self.negative_at = negative_at
        self.stage = 'anchor'
        self.playing = False
        self.volume = 0
        self.pfl = False
        self.wrong_recording = False

    def query(self, script, kind=None):
        fixed = {'get_decks': 4, 'deck 3 get_deck': 3,
                 'deck 3 play': self.playing, 'deck 3 volume': self.volume,
                 'deck 3 pfl': self.pfl,
                 'deck 3 get_filepath': 'changed.flac' if self.wrong_recording else self.locator}
        if script in fixed:
            return fixed[script]
        if script == 'deck 3 get_beatpos':
            return self.beat
        if script == 'deck 3 get_time elapsed absolute':
            seconds = -.001 if self.negative_at == self.stage else self.position
            self.readbacks.append((self.stage, seconds))
            return seconds * 1000
        raise AssertionError(f'Unexpected native query: {script}')

    def send(self, script):
        self.commands.append(script)
        if script == 'deck 3 goto_first_beat':
            self.position, self.beat, self.stage = self.anchor, 0, 'anchor'
            return
        prefix = 'deck 3 goto '
        if not script.startswith(prefix):
            raise AssertionError(f'Unexpected native mutation: {script}')
        amount = script[len(prefix):]
        if amount.startswith('+'):
            self.beat = float(amount)
            self.position = self.anchor + self.beat * 60 / 155
            self.stage = 'candidate'
        elif amount.endswith('%'):
            self.position = float(amount[:-1]) * 400 / 100
            self.beat = (self.position - self.anchor) * 155 / 60
            self.stage = 'replay'
        else:
            raise AssertionError(f'Unexpected native seek: {script}')


class NativeCueAnchorTests(unittest.TestCase):
    def measure(self, peer):
        rows, report, _ = fixture(count=1)
        track = {**report['tracks'][0], 'engine': {'selection_id': identity(peer.locator)}}
        session = DJSession(threading.Lock())
        with patch('afterhours.cue_planner.measured_rows', return_value=rows), patch.object(session, 'sleep'):
            return session.measure_cues(peer, track, report['tracks'][0], 155, 30, deck=3)

    def test_negative_native_anchor_keeps_actual_grid_and_all_cues_in_audio(self):
        peer = PausedGridPeer()
        cues = self.measure(peer)
        self.assertTrue(cues)
        self.assertTrue(all(cue['replayVerified'] for cue in cues))
        for cue in cues:
            # Preserve the native -2ms grid; do not silently clamp it to zero.
            self.assertAlmostEqual(cue['entrySeconds'], -.002 + cue['offsetBeats'] * 60 / 155, places=5)
            self.assertGreater(cue['entrySeconds'], 0)
            self.assertLess(cue['entrySeconds'], cue['introEndSeconds'])
            self.assertLessEqual(cue['introEndSeconds'], cue['exitSeconds'])
            self.assertLess(cue['exitSeconds'], cue['endSeconds'])
            self.assertLessEqual(cue['endSeconds'], 400)
        self.assertTrue(all(seconds >= 0 for stage, seconds in peer.readbacks if stage != 'anchor'))
        self.assertTrue(all(command.startswith('deck 3 goto') for command in peer.commands))
        self.assertFalse(peer.playing)

    def test_anchor_outside_native_bounds_or_nonfinite_stops_before_candidate_seek(self):
        for anchor in (-6.001, float('nan'), float('inf'), float('-inf')):
            with self.subTest(anchor=anchor):
                peer = PausedGridPeer(anchor=anchor)
                with self.assertRaises(BridgeError):
                    self.measure(peer)
                self.assertEqual(peer.commands, ['deck 3 goto_first_beat'])

    def test_positive_native_anchor_preserves_existing_cue_behavior(self):
        peer = PausedGridPeer(anchor=.25)
        cues = self.measure(peer)
        self.assertTrue(cues)
        for cue in cues:
            self.assertTrue(cue['replayVerified'])
            self.assertAlmostEqual(cue['entrySeconds'], .25 + cue['offsetBeats'] * 60 / 155, places=5)

    def test_negative_candidate_and_replay_positions_are_still_rejected(self):
        for stage in ('candidate', 'replay'):
            with self.subTest(stage=stage):
                peer = PausedGridPeer(negative_at=stage)
                with self.assertRaisesRegex(BridgeError, 'outside its expected range'):
                    self.measure(peer)
                self.assertTrue(any(kind == stage for kind, _ in peer.readbacks))

    def test_lost_isolation_or_recording_identity_prevents_seek(self):
        for field, value in (('playing', True), ('volume', .2), ('pfl', True), ('wrong_recording', True)):
            with self.subTest(field=field):
                peer = PausedGridPeer()
                setattr(peer, field, value)
                with self.assertRaises(BridgeError):
                    self.measure(peer)
                self.assertEqual(peer.commands, [])


if __name__ == '__main__':
    unittest.main()
