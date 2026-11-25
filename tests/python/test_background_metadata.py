"""Checked future metadata must travel with its plan into playback.

Synthetic native evidence; no engine, provider or audio commands.
"""
import copy
import threading
import unittest
from unittest.mock import patch

from afterhours import ai_mix
from afterhours import cue_planner
from test_cue_planner import fixture
from afterhours.vdj_session import DJSession, TrackMetadataChanged


class CompletedJob:
    revision = 'checked-revision'
    done = True
    error = None
    cache = {}

    def __init__(self, result):
        self.result = copy.deepcopy(result)
        self.owned = True

    def step(self, client, seconds):
        pass

    def close(self, client):
        self.owned = False

    def snapshot(self):
        return {'status': 'complete', 'message': 'Checked future recordings',
                'decks': [{'deck': 3, 'owned': True}] if self.owned else []}


class BackgroundMetadataTests(unittest.TestCase):
    def prepared(self):
        _, opening, response = fixture(count=2)
        review = ai_mix.validate_plan(response, opening, 30)
        plan = cue_planner.compile_plan(review, opening, opening['tracks'], 30)
        plan['applied'] = True
        _, full, _ = fixture()
        catalog = copy.deepcopy(full['tracks'])
        for track in catalog:
            track['key'] = 'E-flat minor'
        checked = copy.deepcopy(full['tracks'][2:])
        checked[0].update(bpm=155.450508, camelot='6A', duration=399.98)
        checked[1].update(camelot=None)
        pending = {'id': 'checked-revision', 'status': 'analyzing',
                   'tracks': copy.deepcopy(catalog), 'protectedIds': ['0', '1'],
                   'reason': 'Retain the requested continuation'}
        session = DJSession(threading.Lock())
        session.update(candidates=copy.deepcopy(catalog), order=copy.deepcopy(catalog[:2]),
                       analysis=opening, aiReview=review, executionPlan=plan,
                       active=True, phase='playing', index=0, queuedUpdate=pending)
        session.background_job = CompletedJob(checked)
        return session, catalog, checked, copy.deepcopy(plan)

    def test_checked_metadata_survives_ready_commit_and_handoff_adoption(self):
        session, catalog, checked, original_plan = self.prepared()
        with patch('afterhours.cue_planner.verify_receipts') as verify, \
             patch.object(session, 'send') as send, patch.object(session, 'factory') as native:
            session.service_background(None, 30)
            ready = session.snapshot()
            queue = ready['queuedUpdate']
            self.assertEqual(queue['status'], 'ready')
            self.assertEqual(queue['tracks'][:2], catalog[:2])
            self.assertEqual(ready['order'], catalog[:2])
            future = queue['tracks'][2]
            self.assertEqual((future['bpm'], future['camelot'], future['key'], future['duration']),
                             (155.450508, '6A', 'G minor', 399.98))
            self.assertTrue(future['engineChecked'])
            self.assertIsNone(queue['tracks'][3]['key'])
            self.assertIsNone(queue['tracks'][3]['camelot'])
            self.assertEqual(ready['candidates'], queue['tracks'])
            evidence = queue['prepared']['analysis']['tracks'][2]
            self.assertEqual((future['bpm'], future['camelot'], future['duration']),
                             (evidence['bpm'], evidence['camelot'], evidence['duration']))
            prepared_plan = queue['prepared']['executionPlan']
            self.assertEqual(prepared_plan['selections'][:2], original_plan['selections'])
            self.assertEqual(prepared_plan['tempoSchedule'][:2], original_plan['tempoSchedule'])
            self.assertEqual(prepared_plan['transitions'][0], original_plan['transitions'][0])
            order, plan = session.adopt_queue(1)
            self.assertEqual(order, queue['tracks'])
            self.assertEqual(session.snapshot()['order'], order)
            self.assertTrue(plan['applied'])
            self.assertEqual(verify.call_count, 2)
            self.assertIsNone(session.snapshot()['queuedUpdate'])
            self.assertEqual(session.snapshot()['backgroundAnalysis']['decks'], [])
            send.assert_not_called()
            native.assert_not_called()

    def test_failed_receipt_does_not_publish_partially_checked_tracks(self):
        session, catalog, _, _ = self.prepared()
        with patch('afterhours.cue_planner.verify_receipts', side_effect=ValueError('changed receipt')):
            session.service_background(None, 30)
        state = session.snapshot()
        self.assertEqual(state['queuedUpdate']['status'], 'blocked')
        self.assertEqual(state['queuedUpdate']['tracks'], catalog)
        self.assertNotIn('prepared', state['queuedUpdate'])
        self.assertEqual(state['candidates'], catalog)
        self.assertEqual(state['order'], catalog[:2])

    def test_adopted_checked_recording_prepares_but_a_new_key_change_still_holds(self):
        session, _, checked, _ = self.prepared()
        with patch('afterhours.cue_planner.verify_receipts'):
            session.service_background(None, 30)
            order, _ = session.adopt_queue(1)
        track = order[2]
        with patch.object(session, 'analyze_deck', return_value=checked[0]), \
             patch.object(session, 'send') as send, patch.object(session, 'sleep'), \
             patch.object(session, 'match_tempo') as match:
            session.prepare_deck(None, track, 2, 155)
            match.assert_called_once_with(None, track, 2, 155)
            self.assertTrue(send.called)
        changed = {**checked[0], 'camelot': '8A'}
        with patch.object(session, 'analyze_deck', return_value=changed), \
             patch.object(session, 'send') as send, \
             self.assertRaises(TrackMetadataChanged):
            session.prepare_deck(None, track, 2, 155)
        send.assert_not_called()

    def test_superseded_job_cannot_publish_into_new_revision(self):
        session, catalog, _, _ = self.prepared()
        job = session.background_job
        def close(client):
            job.owned = False
            session.state['queuedUpdate']['id'] = 'newer-revision'
        job.close = close
        with patch('afterhours.cue_planner.verify_receipts') as verify:
            session.service_background(None, 30)
        state = session.snapshot()
        self.assertEqual(state['queuedUpdate']['id'], 'newer-revision')
        self.assertEqual(state['queuedUpdate']['status'], 'analyzing')
        self.assertEqual(state['queuedUpdate']['tracks'], catalog)
        self.assertEqual(state['candidates'], catalog)
        verify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
