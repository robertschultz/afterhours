"""Exercise live shortlist advice without submitting provider requests."""
import os
import unittest
from unittest.mock import patch
from threading import Thread

from afterhours import rolling_advisor


class RollingAdvisorTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {'AFTERHOURS_ROLLING_ADVISOR': rolling_advisor.MODEL}):
            self.advisor = rolling_advisor.RollingAdvisor()
        class JoinedThread(Thread):
            def start(self):
                super().start()
                self.join(timeout=1)

        thread_patch = patch('afterhours.rolling_advisor.threading.Thread', JoinedThread)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        self.state = {'id': 'set', 'index': 0, 'startedAt': 123}
        self.tail = {'id': 'tail', 'title': 'Outgoing'}
        self.candidates = [{'id': 'native-one', 'title': 'First'}, {'id': 'native-two', 'title': 'Second'}]

    @patch('afterhours.rolling_advisor.request_pick', return_value='2')
    def test_advice_is_limited_to_eligible_exact_recordings(self, pick):
        facts = {'native-two': {'stemFit': {'vocalOverlapFraction': .1}}}
        self.assertEqual(self.advisor.advise(self.state, self.tail, self.candidates, facts, 40), (None, True))
        payload, aliases = pick.call_args.args
        self.assertEqual(aliases, ['1', '2'])
        self.assertNotIn('native-', repr(payload))
        self.assertEqual(payload['choices'][1]['stems'], facts['native-two']['stemFit'])
        self.assertEqual(self.advisor.advise(self.state, self.tail, self.candidates, facts, 40), ('native-two', False))
        self.assertEqual(pick.call_count, 1)

    @patch('afterhours.rolling_advisor.request_pick', return_value='2')
    def test_advice_for_an_old_queue_index_is_never_reused(self, pick):
        self.advisor.advise(self.state, self.tail, self.candidates, {}, 40)
        self.assertFalse(self.advisor.pending.is_set())
        updated = {**self.state, 'index': 1}
        self.assertEqual(self.advisor.advise(updated, self.tail, self.candidates, {}, 40), (None, False))
        self.assertEqual(pick.call_count, 1)

    @patch('afterhours.rolling_advisor.request_pick', side_effect=OSError)
    def test_failures_do_not_hold_live_refill(self, pick):
        self.assertEqual(self.advisor.advise(self.state, self.tail, self.candidates, {}, 22), (None, False))
        pick.assert_not_called()
        self.advisor.advise(self.state, self.tail, self.candidates, {}, 40)
        self.assertFalse(self.advisor.pending.is_set())
        self.assertEqual(self.advisor.advise(self.state, self.tail, self.candidates, {}, 22), (None, False))
