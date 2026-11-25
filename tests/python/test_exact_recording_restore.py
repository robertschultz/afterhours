"""Source-aware restoration and owned C/D cleanup using a synthetic native peer."""
import copy
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from afterhours.background_analysis import BackgroundAnalysis
from afterhours.dj_bridge import BridgeError, VirtualDJ
from afterhours.native_recording import ExactRecording, NativeRecordings
from test_native_recording import database
from afterhours.vdj_catalog import (identity, paused_load_target, load_exact_recording, dispatch_exact_load,
                         ExactRestoreCleanupUnconfirmed)
from afterhours.vdj_session import DJSession, Cancelled, RecordingLookupIncomplete


LOCATOR = 'netsearch://va347920'
TRACK = {'id': 'virtualdj:' + identity(LOCATOR), 'title': 'House Dawgs', 'artist': 'CamelPhat', 'bpm': 126,
         'engine': {'selection_id': identity(LOCATOR), 'folder_id': identity('beatport:/Top 100/Afro House')}}


class Peer:
    def __init__(self, deck=2):
        self.deck = deck
        self.sent = []
        self.stages = []
        self.clears = []
        self.variables = {}
        self.after_stage = None
        self.after_guards = None
        self.bad_stage = False
        self.clear_failure = False
        self.reads = []
        self.wrong = False
        self.lost = False
        self.after_send = None
        self.values = {'get_browsed_folder_path': 'beatport:', 'get_browsed_folder': 'Beatport',
                       'automix': False, 'get_decks': 4, f'deck {deck} get_deck': deck,
                       f'deck {deck} loaded': False, f'deck {deck} get_filepath': '',
                       f'deck {deck} play': False, f'deck {deck} volume': 0 if deck > 2 else 1,
                       f'deck {deck} pfl': False, f'deck {deck} pitch': 1,
                       f'deck {deck} deck_has_error': False,
                       f'deck {deck} get_effect_name 1': 'Flanger', f'deck {deck} effect_active 1': False}

    def query(self, script, kind=None):
        self.reads.append(script)
        if self.lost and self.sent:
            raise BridgeError('Synthetic lost readback')
        if script.startswith('get_var '):
            return self.variables[script.split("'")[1]]
        return self.values[script]

    def send(self, script):
        if script.startswith('set_var '):
            name = script.split("'")[1]
            self.clears.append(script)
            if not self.clear_failure:
                self.variables[name] = '0.0'
        elif " & param_cast 'text' & " in script and ' load ) & ' in script:
            name = f'$afterhours_restore_{self.deck}'
            clear = f"set_var '{name}' 0"
            expected = (f"deck {self.deck} play ? {clear} : "
                        f"( get_var '{name}' & param_cast 'text' & deck {self.deck} load ) & {clear}")
            if script != expected:
                raise AssertionError('Unexpected load expression')
            value = self.variables[name]
            self.sent.append((script, value))
            if self.after_guards:
                self.after_guards()
            if not self.values[f'deck {self.deck} play']:
                self.values[f'deck {self.deck} loaded'] = True
                self.values[f'deck {self.deck} get_filepath'] = 'netsearch://bpDifferent' if self.wrong else value
            if not self.clear_failure:
                self.variables[name] = '0.0'
            if self.after_send:
                self.after_send()
        else:
            raise AssertionError('Unexpected browser or transport mutation: ' + script)
        return '/vdj/' + VirtualDJ.path(script)

    def send_text(self, script, value):
        if script != f"set_var '$afterhours_restore_{self.deck}'":
            raise AssertionError('Text data must only be staged with set_var')
        self.stages.append((script, value))
        self.variables[script.split("'")[1]] = 'different' if self.bad_stage else value
        if self.after_stage:
            self.after_stage()
        return '/vdj/' + VirtualDJ.path(script)


class ExactRestoreTests(unittest.TestCase):
    def session(self, locator=LOCATOR):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'database.xml'
        database(path, [locator])
        session = DJSession(threading.Lock(), recordings=NativeRecordings(path))
        session.folder = {'id': identity('beatport:')}
        session.positions[TRACK['id']] = {'query': 'CamelPhat', 'position': 0}
        session.update(candidates=[copy.deepcopy(TRACK)], order=[copy.deepcopy(TRACK)])
        session.wait_observed = lambda *_: None
        return session

    def test_saved_other_folder_restores_exact_namespace_without_browser_search_or_queue_change(self):
        session = self.session()
        peer = Peer()
        before = session.snapshot()
        with patch.object(session, 'select', side_effect=AssertionError('Unrelated source search')):
            session.load(peer, TRACK, 2)
        self.assertEqual(len(peer.sent), 1)
        self.assertEqual(peer.sent[0][1], LOCATOR)
        self.assertEqual(peer.variables['$afterhours_restore_2'], '0.0')
        self.assertNotIn(LOCATOR, peer.sent[0][0])
        self.assertFalse(peer.values['deck 2 play'])
        self.assertEqual(session.snapshot(), before)
        self.assertNotIn(LOCATOR, str(session.snapshot()))

    def test_current_source_search_miss_can_restore_only_the_exact_local_identity(self):
        session = self.session()
        peer = Peer()
        track = {**TRACK, 'engine': {**TRACK['engine'], 'folder_id': session.folder['id']}}
        with patch.object(session, 'select', side_effect=RecordingLookupIncomplete('Unconfirmed')) as search:
            session.load(peer, track, 2)
        search.assert_called_once()
        self.assertEqual(peer.values['deck 2 get_filepath'], LOCATOR)

    def test_missing_local_identity_keeps_existing_search_guard_and_does_not_substitute(self):
        session = self.session('netsearch://bpDifferent')
        peer = Peer()
        with patch.object(session, 'select', side_effect=RecordingLookupIncomplete('Availability is unconfirmed')):
            with self.assertRaises(RecordingLookupIncomplete):
                session.load(peer, TRACK, 2)
        self.assertFalse(peer.sent)
        self.assertEqual(session.snapshot()['order'][0]['id'], TRACK['id'])
        self.assertIsNone(session.availability.blocked(TRACK['id']))

    def test_native_target_or_controls_changed_during_lookup_prevent_load(self):
        for changes in ({'deck 2 play': True}, {'deck 2 volume': .4}, {'deck 2 pitch': .9},
                        {'deck 2 pfl': True}, {'deck 2 loaded': True, 'deck 2 get_filepath': 'operator-record'},
                        {'get_browsed_folder_path': 'operator-folder'}, {'automix': True}):
            with self.subTest(changes=changes):
                session = self.session()
                peer = Peer()
                def changed(_):
                    peer.values.update(changes)
                    return ExactRecording(identity(LOCATOR), LOCATOR)
                with patch.object(session.recordings, 'find', side_effect=changed), self.assertRaises(BridgeError):
                    session.load(peer, TRACK, 2)
                self.assertFalse(peer.sent)

    def test_manual_cancellation_during_lookup_prevents_load(self):
        session = self.session()
        peer = Peer()
        def cancelled(_):
            session.cancel.set()
            return ExactRecording(identity(LOCATOR), LOCATOR)
        with patch.object(session.recordings, 'find', side_effect=cancelled), self.assertRaises(Cancelled):
            session.load(peer, TRACK, 2)
        self.assertFalse(peer.sent)

    def test_wrong_identity_and_lost_confirmation_never_replay_load_or_fall_back(self):
        for failure in ('wrong', 'lost'):
            with self.subTest(failure=failure):
                session = self.session()
                peer = Peer()
                setattr(peer, failure, True)
                now = [100.0]
                session.wait_observed = lambda _client, seconds: now.__setitem__(0, now[0] + seconds)
                with patch('afterhours.vdj_session.time.monotonic', side_effect=lambda: now[0]), \
                     patch.object(session, 'select', side_effect=AssertionError('Load cannot fall back after dispatch')), \
                     self.assertRaisesRegex(BridgeError, 'unconfirmed'):
                    session.load(peer, TRACK, 2)
                self.assertEqual(len(peer.sent), 1)
                self.assertFalse(peer.values['deck 2 play'])
                self.assertEqual(session.snapshot()['order'][0]['id'], TRACK['id'])

    def test_public_load_receipt_contains_only_exact_digest(self):
        peer = Peer()
        target = paused_load_target(peer, 2)
        result = load_exact_recording(peer, 2, ExactRecording(identity(LOCATOR), LOCATOR),
                                      identity(LOCATOR), identity('beatport:'), target, wait=lambda _: None)
        self.assertTrue(result['verified'])
        self.assertFalse(result['playback_verified'])
        self.assertNotIn(LOCATOR, str(result))

    def test_cast_load_keeps_quoted_local_filename_out_of_script_and_receipt(self):
        locator = '/Users/example/Music/a "quoted" \'release\' & `nothing`.mp3'
        peer = Peer()
        target = paused_load_target(peer, 2)
        result = load_exact_recording(peer, 2, ExactRecording(identity(locator), locator),
                                      identity(locator), identity('beatport:'), target, wait=lambda _: None)
        self.assertTrue(result['verified'])
        self.assertEqual(peer.values['deck 2 get_filepath'], locator)
        self.assertEqual(len(peer.stages), 1)
        self.assertEqual(len(peer.sent), 1)
        self.assertNotIn(locator, peer.sent[0][0])
        self.assertNotIn(locator, str(result))
        self.assertNotIn('`', peer.sent[0][0])
        self.assertFalse(peer.values['deck 2 play'])
        self.assertEqual(peer.variables['$afterhours_restore_2'], '0.0')

    def background(self):
        session = self.session()
        peer = Peer(3)
        job = BackgroundAnalysis(session, 'revision', [TRACK])
        job.owned = True
        job.baseline = {'volume': 1, 'pfl': False, 'effect': 'Flanger'}
        job.thread = Mock()
        job.wait = lambda _: None
        restored = (ExactRecording(identity(LOCATOR), LOCATOR), paused_load_target(peer, 3))
        helper = SimpleNamespace(folder=session.folder, locate_for_load=Mock(return_value=restored))
        def request(kind, script, argument):
            response = queue.Queue()
            job.pending.put((kind, script, argument, response))
            job.step(peer, 40)
            result = response.get_nowait()
            if isinstance(result, Exception):
                raise result
            return result
        job.request = request
        return peer, job, helper

    def test_spare_deck_restoration_retains_exact_cleanup_ownership_without_browser_selection(self):
        peer, job, helper = self.background()
        with patch('afterhours.background_analysis.require_selection', side_effect=AssertionError('Unrelated selected row')):
            job.load(helper, peer, TRACK, 3)
        self.assertEqual(len(peer.sent), 1)
        self.assertEqual(peer.sent[0][1], LOCATOR)
        self.assertEqual(peer.variables['$afterhours_restore_3'], '0.0')
        self.assertEqual(job.loaded, TRACK['engine']['selection_id'])
        descriptor = job.cleanup_descriptors()[0]
        self.assertEqual(descriptor['selectionId'], TRACK['engine']['selection_id'])
        self.assertFalse(descriptor['loadPending'])
        self.assertFalse(peer.values['deck 3 play'])

    def test_uncertain_spare_send_and_catalog_interrupt_cannot_replay_load(self):
        peer, job, helper = self.background()
        def uncertain():
            job.invalidate_catalog()
            raise BridgeError('Synthetic uncertain send')
        peer.after_send = uncertain
        with self.assertRaisesRegex(BridgeError, 'uncertain send'):
            job.load(helper, peer, TRACK, 3)
        helper.locate_for_load.assert_called_once()
        self.assertEqual(len(peer.sent), 1)
        descriptor = job.cleanup_descriptors()[0]
        self.assertTrue(descriptor['loadPending'])
        self.assertEqual(descriptor['selectionId'], TRACK['engine']['selection_id'])
        self.assertNotIn(LOCATOR, str(descriptor))

    def test_spare_preflight_failure_does_not_invent_pending_load_ownership(self):
        peer, job, helper = self.background()
        peer.values['automix'] = True
        with self.assertRaises(BridgeError):
            job.load(helper, peer, TRACK, 3)
        self.assertFalse(peer.sent)
        self.assertIsNone(job.requested_load)
        self.assertFalse(job.cleanup_descriptors()[0]['loadPending'])

    def dispatch(self, peer, before_dispatch=None):
        target = paused_load_target(peer, peer.deck)
        return dispatch_exact_load(peer, peer.deck, ExactRecording(identity(LOCATOR), LOCATOR),
                                   identity(LOCATOR), identity('beatport:'), target, before_dispatch)

    def test_stage_mismatch_clears_data_without_load_or_ownership_latch(self):
        peer = Peer()
        peer.bad_stage = True
        latch = Mock()
        with self.assertRaisesRegex(BridgeError, 'data was not verified'):
            self.dispatch(peer, latch)
        latch.assert_not_called()
        self.assertFalse(peer.sent)
        self.assertEqual(peer.variables['$afterhours_restore_2'], '0.0')

    def test_target_change_after_staging_clears_data_without_load(self):
        peer = Peer()
        peer.after_stage = lambda: peer.values.update({'deck 2 volume': .4})
        with self.assertRaisesRegex(BridgeError, 'controls changed'):
            self.dispatch(peer)
        self.assertFalse(peer.sent)
        self.assertEqual(peer.variables['$afterhours_restore_2'], '0.0')

    def test_atomic_native_guard_blocks_physical_play_after_host_checks(self):
        peer = Peer()
        peer.after_guards = lambda: peer.values.update({'deck 2 play': True})
        self.dispatch(peer)
        self.assertEqual(len(peer.sent), 1)
        self.assertFalse(peer.values['deck 2 loaded'])
        self.assertTrue(peer.values['deck 2 play'])
        self.assertEqual(peer.variables['$afterhours_restore_2'], '0.0')

    def test_cancellation_immediately_before_dispatch_clears_staged_data(self):
        peer = Peer()
        with self.assertRaises(Cancelled):
            self.dispatch(peer, Mock(side_effect=Cancelled()))
        self.assertFalse(peer.sent)
        self.assertEqual(peer.variables['$afterhours_restore_2'], '0.0')

    def test_clear_failure_is_explicit_and_does_not_replay_dispatched_load(self):
        peer, job, helper = self.background()
        peer.clear_failure = True
        peer.after_send = job.invalidate_catalog
        with self.assertRaisesRegex(ExactRestoreCleanupUnconfirmed, 'cleanup remains unconfirmed'):
            job.load(helper, peer, TRACK, 3)
        self.assertEqual(len(peer.sent), 1)
        self.assertEqual(len(peer.clears), 1)
        helper.locate_for_load.assert_called_once()
        self.assertTrue(job.cleanup_descriptors()[0]['loadPending'])

    def test_clear_failure_before_load_does_not_retry_or_invent_ownership(self):
        peer, job, helper = self.background()
        peer.clear_failure = True
        peer.bad_stage = True
        peer.after_stage = job.invalidate_catalog
        with self.assertRaises(ExactRestoreCleanupUnconfirmed):
            job.load(helper, peer, TRACK, 3)
        self.assertFalse(peer.sent)
        helper.locate_for_load.assert_called_once()
        self.assertIsNone(job.requested_load)


if __name__ == '__main__':
    unittest.main()
