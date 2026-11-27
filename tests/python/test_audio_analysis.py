"""Simulated bridge boundaries, separate from the real paused scan evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from afterhours.audio_analysis import (
    AudioAnalysisTimeout, PLUGIN, read_scan, restore_effect, scan_track, validate_scan, verified_energy,
)
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_catalog import identity

LOCATOR = 'fixture-local-recording'
SID = identity(LOCATOR)
TRACK = {'engine': {'selection_id': SID}}


def report():
    return {'version': 2, 'recordingId': SID, 'scanStartedAtMs': 100000,
            'sampleRate': 44100, 'duration': 10, 'totalFrames': 441000,
            'coveredFrames': 441000, 'bufferReadComplete': True, 'audioSaved': False,
            'features': [{'seconds': n*.5, 'duration': .5, 'rmsDb': -20,
                          'lowDb': -25, 'highDb': -30, 'crestDb': 10} for n in range(20)]}


class Peer:
    def __init__(self, root, zero_first=False):
        self.root = root; self.now = 0; self.commands = []; self.effect = 'Flanger'
        self.active = False; self.playing = False; self.scans = 0
        self.zero_first = zero_first; self.changed = False
    def wait(self, seconds): self.now += seconds
    def clock(self): return self.now
    def query(self, q, kind=None):
        if q in ('deck 1 play', 'deck 2 play'): return self.playing
        if q == 'deck 1 get_filepath': return 'changed' if self.changed else LOCATOR
        if q == 'deck 1 get_effect_name 1': return self.effect
        if q == 'deck 1 effect_active 1': return self.active
        if q == 'deck 1 get_effect_string 1 1': return 'Buffer read finished. Afterhours must verify decoding.'
        raise AssertionError(q)
    def send(self, q):
        self.commands.append(q)
        if q.startswith('deck 1 effect_select 1 '): self.effect = q.split('"')[1]
        elif q == 'deck 1 effect_active 1 on': self.active = True
        elif q == 'deck 1 effect_active 1 off': self.active = False
        elif q == 'deck 1 effect_button 1 1':
            self.scans += 1; data = report(); data['scanStartedAtMs'] += self.scans
            if self.zero_first and self.scans == 1:
                for f in data['features'][10:]: f['rmsDb'] = f['lowDb'] = f['highDb'] = -120
            (self.root/(SID+'.json')).write_text(json.dumps(data))
        else: raise AssertionError(q)


class AudioAnalysisTests(unittest.TestCase):
    def test_restore_waits_for_native_deactivation_before_reselecting(self):
        class DelayedPeer(Peer):
            off_at=None
            def send(self,q):
                if q=='deck 1 effect_active 1 off':
                    self.commands.append(q);self.off_at=self.now+.2;return
                if q.startswith('deck 1 effect_select') and self.active:
                    raise AssertionError('Cannot change the selection before deactivation is acknowledged')
                super().send(q)
            def query(self,q,kind=None):
                if self.off_at is not None and self.now>=self.off_at:self.active=False
                return super().query(q,kind)
        peer=DelayedPeer(None);peer.effect=PLUGIN;peer.active=True
        restore_effect(peer,1,'Flanger',wait=peer.wait,clock=peer.clock)
        self.assertGreaterEqual(peer.now,.2);self.assertEqual(peer.effect,'Flanger');self.assertFalse(peer.active)
        self.assertEqual(peer.commands.count('deck 1 effect_active 1 off'),1)

    def test_unconfirmed_deactivation_never_selects_replacement_and_is_bounded(self):
        peer=Peer(None);peer.effect=PLUGIN;peer.active=True
        peer.send=lambda q:peer.commands.append(q)
        with self.assertRaisesRegex(BridgeError,'deactivation was not confirmed'):
            restore_effect(peer,1,'Flanger',wait=peer.wait,clock=peer.clock)
        self.assertEqual(peer.commands,['deck 1 effect_active 1 off']);self.assertLess(peer.now,3.1)

    def test_cleanup_does_not_touch_operator_replacement(self):
        peer=Peer(None);peer.effect='Echo';peer.active=True
        restore_effect(peer,1,'Flanger',wait=peer.wait,clock=peer.clock)
        self.assertEqual(peer.commands,[]);self.assertEqual(peer.effect,'Echo');self.assertTrue(peer.active)

    def test_zero_filled_early_buffer_never_passes_as_quiet_audio(self):
        data = report()
        for f in data['features'][10:]: f['rmsDb'] = -120
        with self.assertRaisesRegex(ValueError, 'still silent'): validate_scan(data, 9.8)
        data = report()
        for f in data['features'][3:14]: f['rmsDb'] = -120
        with self.assertRaisesRegex(ValueError, 'interior'): validate_scan(data, 9.8)

    def test_inconsistent_missing_or_nonfinite_measurements_are_rejected(self):
        for field, value in [('bufferReadComplete', False), ('coveredFrames', 440999),
                             ('duration', float('nan')), ('sampleRate', True)]:
            bad = report(); bad[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): validate_scan(bad, 9.8)
        bad = report(); bad['features'][4]['seconds'] += .1
        with self.assertRaises(ValueError): validate_scan(bad, 9.8)
        with self.assertRaises(ValueError): validate_scan(report(), None)

    def test_repeated_scans_confirm_settled_audio_and_restore_effect(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); peer = Peer(root, zero_first=True)
            result = scan_track(peer, TRACK, 9.8, root=root, clock=peer.clock, wait=peer.wait)
            self.assertEqual(result['passes'], 3)
            self.assertGreaterEqual(result['stableForSeconds'], 1)
            self.assertEqual(peer.effect, 'Flanger'); self.assertFalse(peer.active)
            self.assertTrue(all('effect_' in c for c in peer.commands))
            energy = verified_energy(SID, 10, root)
            self.assertEqual(energy['coverage'], 1)
            self.assertFalse(energy['vocalsDetected'])
            self.assertFalse(energy['semanticSections'])
            self.assertNotIn(LOCATOR, json.dumps(energy))

    def test_receipt_cannot_validate_changed_partial_or_different_duration_data(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); peer = Peer(root)
            scan_track(peer, TRACK, 9.8, root=root, clock=peer.clock, wait=peer.wait)
            self.assertIsNone(verified_energy(SID, 11, root))
            path = root/(SID+'.json'); data = json.loads(path.read_text())
            data['features'][0]['rmsDb'] = -40; path.write_text(json.dumps(data))
            self.assertIsNone(verified_energy(SID, 10, root))

    def test_spare_scan_receipt_accepts_a_bounded_delay_between_matching_passes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            peer = Peer(root)

            def wait_past_handoff(seconds):
                peer.wait(50.15 if seconds == 1.05 else seconds)

            receipt = scan_track(peer, TRACK, 9.8, root=root, clock=peer.clock,
                                 wait=wait_past_handoff, timeout=120)
            self.assertEqual(receipt['passes'], 2)
            self.assertGreater(receipt['stableForSeconds'], 40)
            self.assertIsNotNone(verified_energy(SID, 10, root))
            path = root/(SID+'.verified.json')
            receipt['scanWallSeconds'] = 121
            path.write_text(json.dumps(receipt))
            self.assertIsNone(verified_energy(SID, 10, root))

    def test_transport_recording_and_active_effect_guards_send_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            for field in ('playing', 'changed', 'active'):
                peer = Peer(Path(d)); setattr(peer, field, True)
                with self.subTest(field=field), self.assertRaises(BridgeError):
                    scan_track(peer, TRACK, 9.8, root=Path(d), clock=peer.clock, wait=peer.wait)
                self.assertEqual(peer.commands, [])

    def test_timeout_leaves_no_verified_receipt_and_restores_effect(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); peer = Peer(root)
            with self.assertRaises(BridgeError):
                scan_track(peer, TRACK, 9.8, root=root, clock=peer.clock, wait=peer.wait, timeout=.9)
            self.assertFalse(peer.active); self.assertEqual(peer.effect, 'Flanger')
            self.assertFalse((root/(SID+'.verified.json')).exists())

    def test_completed_native_scan_reports_split_storage_without_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            native = root / 'bridge-reports'
            native.mkdir()
            peer = Peer(native)
            with self.assertRaisesRegex(AudioAnalysisTimeout, 'same report folder'):
                scan_track(peer, TRACK, 9.8, root=root, clock=peer.clock, wait=peer.wait)
            self.assertTrue((native/(SID+'.json')).is_file())
            self.assertFalse((root/(SID+'.verified.json')).exists())
            self.assertLess(peer.now, 1)
            self.assertEqual(peer.effect, 'Flanger')
            self.assertFalse(peer.active)

    def test_legacy_identity_mismatch_and_symlink_are_not_trusted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); path = root/(SID+'.json'); data = report(); data['version'] = 1
            path.write_text(json.dumps(data)); self.assertIsNone(read_scan(SID, root))
            data = report(); data['recordingId'] = 'a'*64
            path.write_text(json.dumps(data)); self.assertIsNone(read_scan(SID, root))
            path.unlink(); other = root/'fixture'; other.write_text(json.dumps(report())); path.symlink_to(other)
            self.assertIsNone(read_scan(SID, root))


if __name__ == '__main__': unittest.main()
