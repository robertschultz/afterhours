"""Independent handback regressions. Synthetic peer only; no native audio."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from afterhours import ai_mix
from afterhours import copilot_commands
from afterhours import cue_planner
from afterhours.dj_bridge import BridgeError
from test_cue_planner import fixture
from afterhours.vdj_catalog import identity
from afterhours.vdj_session import DJSession


class ProbeComplete(Exception):
    pass


class HandbackTimingValidation(unittest.TestCase):
    def test_empty_crate_checkpoint_preserves_cleanup_without_restoring_a_set(self):
        from afterhours.session_recovery import SessionRecovery
        source = DJSession(threading.Lock())
        source.update(resumeBlocked=True,
                      cleanupPending={'ramps': ['afterhours_out_low'], 'unknown': False})
        with tempfile.TemporaryDirectory() as directory:
            store = SessionRecovery(Path(directory)/'queue.json')
            store.save(source)
            record = json.loads(store.path.read_text())
            self.assertIsNone(record['session'])
            self.assertTrue(record['cleanup']['resumeBlocked'])
            recovered = DJSession(threading.Lock())
            with patch.object(recovered, 'factory') as native, patch.object(recovered, 'restore') as restore:
                result = store.restore(recovered)
            native.assert_not_called()
            restore.assert_not_called()
            self.assertEqual(result['status'], 'cleanup-required')
            self.assertFalse(result['restored'])
            self.assertFalse(result['autoplay'])
            self.assertEqual(recovered.state['cleanupPending'], source.state['cleanupPending'])
            self.assertTrue(recovered.state['resumeBlocked'])
            self.assertEqual(recovered.state['order'], [])

    def test_top_level_cleanup_merges_existing_ownership_and_never_clears_unknown(self):
        from afterhours.session_recovery import SessionRecovery
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'queue.json'
            path.write_text(json.dumps({'version': 1, 'session': None, 'cleanup': {
                'resumeBlocked': True,
                'cleanupPending': {'ramps': ['afterhours_tempo'], 'unknown': False}}}))
            s = DJSession(threading.Lock())
            s.update(resumeBlocked=True,
                     cleanupPending={'ramps': ['afterhours_out_low'], 'unknown': True})
            with patch.object(s, 'factory') as native:
                SessionRecovery(path).restore(s)
            native.assert_not_called()
            self.assertEqual(s.state['cleanupPending'], {
                'ramps': ['afterhours_out_low', 'afterhours_tempo'], 'unknown': True})
            self.assertTrue(s.state['resumeBlocked'])
            # A legacy empty checkpoint cannot clear current ownership either.
            path.write_text(json.dumps({'version': 1, 'session': None}))
            SessionRecovery(path).restore(s)
            self.assertTrue(s.state['resumeBlocked'])
            self.assertTrue(s.state['cleanupPending']['unknown'])

    def test_explicit_stop_retries_only_known_cleanup_and_requires_paused_barrier(self):
        for outcome in ('confirmed', 'send-failed', 'started-during-cleanup', 'not-paused', 'unknown'):
            with self.subTest(outcome=outcome):
                s = DJSession(threading.Lock())
                s.update(phase='takeover', resumeBlocked=True,
                         cleanupPending={'ramps': ['afterhours_out_low'],
                                         'unknown': outcome == 'unknown'})
                client = Mock()
                sent = []
                queried = [0]

                def send(script):
                    sent.append(script)
                    if script.startswith('repeat_stop') and outcome == 'send-failed':
                        raise BridgeError('Synthetic cancellation acknowledgement unavailable')

                def query(script, kind=None):
                    queried[0] += 1
                    return (outcome == 'not-paused' or
                            outcome == 'started-during-cleanup' and queried[0] > 2)

                client.send.side_effect = send
                client.query.side_effect = query
                s.factory = lambda *a, **kw: nullcontext(client)

                class InlineThread:
                    def __init__(self, target, args, **kw): self.target, self.args = target, args
                    def start(self): self.target(*self.args)
                    def is_alive(self): return False

                with patch('afterhours.vdj_session.threading.Thread', InlineThread):
                    s.stop(pause=True)

                self.assertEqual(sent[0], 'deck 1 pause & deck 2 pause')
                self.assertTrue(all(value == 'deck 1 pause & deck 2 pause' or
                                    value == "repeat_stop 'afterhours_out_low'" for value in sent))
                self.assertEqual(sum(value.startswith('repeat_stop') for value in sent),
                                 0 if outcome == 'not-paused' else 1)
                self.assertEqual(bool(s.snapshot().get('resumeBlocked')), outcome != 'confirmed')
                self.assertFalse(s.active)

    def test_explicit_paused_cleanup_recovers_a_known_owned_loop(self):
        from afterhours.vdj_catalog import identity
        s = DJSession(threading.Lock())
        path = '/fixture/outgoing.wav'
        active = [True]
        client = Mock()
        query = {'deck 1 get_filepath': path, 'deck 1 get_active_loop': 4,
                 'deck 1 get_loop_in_time': 8000.0}
        client.query.side_effect = lambda action, kind=None: active[0] if action == 'deck 1 loop' else query.get(action, False)
        client.send.side_effect = lambda action: active.__setitem__(0, False)
        s.update(resumeBlocked=True, cleanupPending={'loops': [
            {'deck':1,'recording':identity(path),'beats':4,'startMs':8000.0}]})
        s.retry_paused_cleanup(client)
        client.send.assert_called_once_with('deck 1 loop_exit')
        self.assertFalse(s.snapshot()['resumeBlocked'])

    def test_recovered_cleanup_cannot_supply_an_arbitrary_engine_command(self):
        s = DJSession(threading.Lock())
        s.update(resumeBlocked=True,
                 cleanupPending={'ramps': ["afterhours_out_low' & deck 1 play & repeat_stop 'x"],
                                 'unknown': False})
        client = Mock()
        s.retry_paused_cleanup(client)
        client.send.assert_not_called()
        self.assertTrue(s.snapshot()['resumeBlocked'])

    def test_cleanup_latch_and_owned_names_survive_durable_queue_checkpoint(self):
        from afterhours.session_recovery import SessionRecovery
        from test_session_recovery import session
        source = session()
        source.update(resumeBlocked=True,
                      cleanupPending={'ramps': ['afterhours_out_low', 'afterhours_tempo'], 'unknown': False})
        with tempfile.TemporaryDirectory() as directory:
            store = SessionRecovery(Path(directory)/'queue.json')
            self.assertTrue(store.save(source))
            saved = json.loads(store.path.read_text())['session']
            self.assertTrue(saved['resumeBlocked'])
            self.assertEqual(saved['cleanupPending'], source.state['cleanupPending'])
            recovered = session()  # Its client factory forbids all native calls.
            self.assertTrue(store.restore(recovered)['restored'])
            self.assertTrue(recovered.state['resumeBlocked'])
            self.assertEqual(recovered.state['cleanupPending'], source.state['cleanupPending'])
            self.assertFalse(recovered.active)

    def test_new_set_cannot_erase_unconfirmed_cleanup(self):
        s = DJSession(threading.Lock())
        s.update(active=False, phase='takeover', resumeBlocked=True)
        try:
            s.new_set()
        except ValueError:
            pass  # Rejecting replacement and preserving the latch is valid.
        self.assertTrue(s.snapshot().get('resumeBlocked'),
                        'A fresh set erased unresolved native automation ownership')

    def test_new_playback_cannot_bypass_unconfirmed_cleanup(self):
        s = DJSession(threading.Lock())
        pool = [dict(id=str(i), title=str(i), bpm=120) for i in range(2)]
        s.update(candidates=pool)
        s.plan(['0', '1'], mix_style='quick')
        s.update(resumeBlocked=True)
        with patch.object(s, '_launch') as launch:
            with self.assertRaises(ValueError):
                s.start(initialize_mixer=True)
            launch.assert_not_called()

    def test_checkpoint_restore_preserves_unconfirmed_cleanup(self):
        pool = [dict(id='virtualdj:'+identity(str(i)), title=str(i), artist='Fixture', bpm=120,
                     engine={'selection_id': identity(str(i)), 'folder_id': identity('folder')})
                for i in range(2)]
        for existing, saved in ((False, True), (True, False)):
            with self.subTest(existing=existing, saved=saved):
                s = DJSession(threading.Lock())
                s.update(resumeBlocked=existing)
                with patch.object(s, 'factory') as factory:
                    s.restore(dict(candidates=pool, order=pool, resumeBlocked=saved))
                factory.assert_not_called()
                self.assertTrue(s.snapshot().get('resumeBlocked'),
                                'Restore erased unresolved native automation ownership')

    def test_paused_handback_checks_transport_and_keeps_current_controls(self):
        from test_vdj_mixing import MixerTests
        for changed_deck in (None, 1, 2):
            with self.subTest(changed_deck=changed_deck):
                mixer, readings, sent = MixerTests().mixer()
                baseline = copy.deepcopy(mixer.baselines)
                readings[1].update(eq_low_freq=.77, eq_mid_freq=.86, level=.48)
                before = mixer.read(None, 1)
                if changed_deck is not None:
                    readings[changed_deck]['play'] = True
                    with self.assertRaisesRegex(BridgeError, 'playing pair changed'):
                        mixer.resume_preflight(None, 1, baseline,
                                               adopt_playing=True, paused_current=True)
                    self.assertFalse(sent)
                else:
                    mixer.resume_preflight(None, 1, baseline,
                                           adopt_playing=True, paused_current=True)
                    self.assertEqual(mixer.read(None, 1), before)
                    self.assertTrue(all('deck 1' not in command for command in sent))
                    self.assertFalse(any(' play' in command for command in sent))
                    self.assertEqual(mixer.baselines, baseline)

    def test_control_lock_timeout_finishes_pending_handback_receipt(self):
        """A denied bridge reservation must not leave every next command stuck."""
        from test_copilot_commands import running
        s = running()
        s.state['copilot']['command'] = {
            'id': 'waiting-handback', 'action': 'resume', 'status': 'pending',
            'context': copilot_commands.context(s.snapshot()),
            'message': 'Checking the pair.'}
        s.control_lock = Mock(acquire=Mock(return_value=False))
        work = Mock()

        s._wait_for_control(work)

        work.assert_not_called()
        self.assertFalse(s.active)
        self.assertEqual(s.state['phase'], 'error')
        self.assertNotEqual(s.state['copilot']['command']['status'], 'pending',
                            'A finished bridge wait leaves the handback permanently pending')

    def test_catalog_search_does_not_clear_unconfirmed_automation_cleanup(self):
        """Read-only discovery cannot prove that old native ramps stopped."""
        from test_copilot_commands import running
        s = running()
        s.update(active=False, phase='takeover', resumeBlocked=True,
                 catalogSearch={'message': 'Catalog checked.'})
        client = Mock()
        s.factory = lambda *a, **kw: nullcontext(client)

        class FinishedSearch:
            def __init__(self, *a, **kw): self.done = True

        class InlineThread:
            def __init__(self, target, args, **kw):
                self.target, self.args = target, args
            def start(self): self.target(*self.args)
            def is_alive(self): return False

        with patch('afterhours.vdj_session.CatalogSearch', FinishedSearch), \
                patch('afterhours.vdj_session.threading.Thread', InlineThread):
            result = s.discover_more('Daft Punk')

        client.send.assert_not_called()
        self.assertTrue(result['resumeBlocked'],
                        'Metadata discovery cleared unconfirmed native ramp ownership')
        self.assertFalse(copilot_commands.resumable(result))

    def test_playing_handback_retimes_after_incoming_preparation(self):
        """Five seconds preparing B must not confirm A's expired exit."""
        _, report, response = fixture()
        later = copy.deepcopy(report['tracks'][0]['cueCandidates'][0])
        later.update(id='verified-later', entryBeat=128, exitBeat=192, endBeat=224,
                     entrySeconds=128*60/155, exitSeconds=192*60/155,
                     endSeconds=224*60/155)
        report['tracks'][0]['cueCandidates'].append(later)
        report['fingerprint'] = cue_planner.fingerprint(report['tracks'])
        review = ai_mix.validate_plan(response, report, 30)
        plan = cue_planner.compile_plan(review, report, report['tracks'], 30)
        plan['applied'] = True
        s = DJSession(threading.Lock())
        controls = {d: dict(eq_low_freq=.5, eq_mid_freq=.5, eq_high_freq=.5,
                            level=.5, **{'stem Vocal': .5, 'stem Instru': .5})
                    for d in (1, 2)}
        decks = [dict(deck=1, id='0', playing=True, bpm=155, beatPosition=118),
                 dict(deck=2, id='1', playing=False, bpm=155, beatPosition=64)]
        now = [100.0]
        sent = []
        prepared = []
        s.update(phase='takeover', active=False, index=0, startedAt=10,
                 candidates=copy.deepcopy(report['tracks']),
                 order=copy.deepcopy(report['tracks']), analysis=report,
                 aiReview=review, executionPlan=plan,
                 mixerBaselines=copy.deepcopy(controls))
        s.state['copilot']['mode'] = 'off'

        def telemetry(client, *, clock_only=False):
            reading = dict(sampledAt=now[0], crossfader=0,
                           decks=copy.deepcopy(decks))
            s.update(telemetry=reading)
            if ((s.state['copilot'].get('command') or {}).get('status') == 'confirmed'
                    and s.state['phase'] == 'playing'):
                raise ProbeComplete()
            return reading

        def prepare(client, track, deck, target, cue=None):
            prepared.append((track['id'], deck))
            now[0] += 5
            decks[0]['beatPosition'] += 5*155/60
            decks[deck-1].update(id=track['id'], bpm=target, playing=False,
                                  beatPosition=cue['entryBeat'])

        def query(client, script, kind=None):
            if script == 'crossfader': return 0
            if script == 'mixermode': return True
            if script in ('fake_eq', 'fake_mixer', 'pitch_lock'): return False
            _, deck, key = script.split(maxsplit=2)
            deck = int(deck)
            if key == 'play': return decks[deck-1]['playing']
            if key.startswith('has_stems'): return True
            return controls[deck][key]

        def send(client, script):
            sent.append(script)
            for command in script.split(' & '):
                head, amount = command.rsplit(' ', 1)
                _, deck, key = head.split(maxsplit=2)
                controls[int(deck)][key] = float(amount.rstrip('%'))/100

        class Peer:
            def status(self): return dict(automix=False, decks=decks)

        def launch(fn, phase):
            s.update(active=True)
            fn(Peer())

        with patch('time.time', side_effect=lambda: now[0]), \
                patch('afterhours.cue_planner.verify_receipts'), \
                patch.object(s, '_launch', side_effect=launch), \
                patch.object(s, 'telemetry', side_effect=telemetry), \
                patch.object(s, 'prepare_deck', side_effect=prepare), \
                patch.object(s, 'q', side_effect=query), \
                patch.object(s, 'send', side_effect=send):
            telemetry(Peer())
            with self.assertRaises(ProbeComplete):
                s.copilot_command('resume', 'delayed-handback',
                                  copilot_commands.context(s.snapshot()))

        self.assertEqual(prepared, [('1', 2)])
        self.assertFalse(any('deck 1 play' in value or 'deck 1 goto' in value
                             or 'deck 1 load' in value for value in sent))
        self.assertAlmostEqual(decks[0]['beatPosition'], 118+5*155/60)
        self.assertEqual(s.state['timing']['mixAtBeat'], 192,
                         'Handback confirmed an exit already passed during preparation')


if __name__ == '__main__':
    unittest.main()
