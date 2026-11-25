"""Set clock lifecycle regressions with synthetic native I/O only."""
from contextlib import nullcontext
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours.dj_bridge import BridgeError
from afterhours.opening_playback import start_and_prepare
import test_opening_playback as opening_fixture
from afterhours.vdj_session import DJSession


class InlineThread:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)

    def is_alive(self):
        return False


class TerminalStopClockTests(unittest.TestCase):
    def session(self, phase='completed', ended=200.0):
        peer = Mock()
        peer.query.return_value = False
        session = DJSession(threading.Lock(), client_factory=lambda *a, **kw: nullcontext(peer))
        session.update(phase=phase, startedAt=100.0, endedAt=ended,
                       telemetry={'sampledAt':200.0, 'crossfader':0.0, 'decks':[
                           {'deck':n, 'id':None, 'loaded':False, 'playing':False,
                            'bpm':127.0, 'beatPosition':0.0}
                           for n in (1, 2)]})
        return session, peer

    def test_repeated_stop_preserves_finished_time_and_published_elapsed(self):
        for phase in ('completed', 'stopped'):
            with self.subTest(phase=phase):
                session, peer = self.session(phase)
                for now in (250.0, 300.0):
                    with patch('afterhours.vdj_session.threading.Thread', InlineThread), patch('afterhours.vdj_session.time.time', return_value=now):
                        session.stop()
                        self.assertEqual(session.snapshot()['endedAt'], 200.0)
                        self.assertEqual(session.program()['setElapsedSeconds'], 100.0)
                self.assertEqual([call.args[0] for call in peer.send.call_args_list],
                                 ['deck 1 pause & deck 2 pause'] * 2)

    def test_first_active_stop_records_end_once_and_repeat_keeps_it(self):
        session, peer = self.session('playing', ended=None)
        def stop_active(client):
            session.stop()
            session.check()
        with patch('afterhours.vdj_session.threading.Thread', InlineThread), patch('afterhours.vdj_session.time.time', return_value=250.0):
            session._launch(stop_active, 'playing')
            self.assertEqual(session.snapshot()['endedAt'], 250.0)
        with patch('afterhours.vdj_session.threading.Thread', InlineThread), patch('afterhours.vdj_session.time.time', return_value=300.0):
            session.stop()
            self.assertEqual(session.snapshot()['endedAt'], 250.0)
            self.assertEqual(session.program()['setElapsedSeconds'], 150.0)
        self.assertEqual(peer.send.call_count, 2)

    def test_stop_before_any_playback_does_not_create_a_session_clock(self):
        session, _ = self.session('ready', ended=None)
        session.update(startedAt=None)
        with patch('afterhours.vdj_session.threading.Thread', InlineThread), patch('afterhours.vdj_session.time.time', return_value=250.0):
            session.stop()
            self.assertIsNone(session.snapshot().get('endedAt'))
            self.assertEqual(session.program()['setElapsedSeconds'], 0)

    def test_worker_error_still_records_its_current_end_time(self):
        for prior_end in (None, 200.0):
            with self.subTest(prior_end=prior_end):
                session, peer = self.session('ready', ended=prior_end)
                def fail(client):
                    raise BridgeError('Synthetic new operation failure')
                with patch('afterhours.vdj_session.threading.Thread', InlineThread), patch('afterhours.vdj_session.time.time', return_value=250.0):
                    session._launch(fail, 'loading')
                self.assertEqual(session.snapshot()['phase'], 'error')
                self.assertEqual(session.snapshot()['endedAt'], 250.0)
                peer.send.assert_not_called()

    def test_manual_takeover_leaves_running_session_time_unchanged(self):
        session, peer = self.session('playing', ended=None)
        def hand_over(client):
            session.stop(pause=False)
            session.check()
        with patch('afterhours.vdj_session.threading.Thread', InlineThread), patch('afterhours.vdj_session.time.time', return_value=250.0):
            session._launch(hand_over, 'playing')
        self.assertEqual(session.snapshot()['phase'], 'takeover')
        self.assertEqual(session.snapshot()['startedAt'], 100.0)
        self.assertIsNone(session.snapshot()['endedAt'])
        peer.send.assert_not_called()

    def test_new_confirmed_opening_resets_previous_finished_clock(self):
        session, _, commands, _, _ = opening_fixture.OpeningTests().setup_session()
        session.update(startedAt=100.0, endedAt=200.0)
        class OpeningConfirmed(Exception):
            pass
        with patch('afterhours.opening_playback.time.time', return_value=300.0), \
             patch('afterhours.opening_playback.TransitionMixer') as mixer, \
             patch('afterhours.opening_playback.BackgroundAnalysis', side_effect=OpeningConfirmed):
            mixer.return_value.baselines = {}
            with self.assertRaises(OpeningConfirmed):
                start_and_prepare(session, None, session.snapshot()['order'])
        self.assertEqual(commands.count('deck 1 play'), 1)
        self.assertEqual(session.snapshot()['startedAt'], 300.0)
        self.assertIsNone(session.snapshot()['endedAt'])


if __name__ == '__main__':
    unittest.main()
