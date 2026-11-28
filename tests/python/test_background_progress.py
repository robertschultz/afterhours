"""Queue preparation receipts use the actual post-cleanup ownership state."""
import copy
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours.dj_bridge import BridgeError, OSCReplyTimeout

from afterhours.vdj_session import DJSession


class BackgroundProgressTests(unittest.TestCase):
    def test_changed_muted_spare_is_quarantined_and_only_its_candidate_is_replaced(self):
        for isolated in (True, False):
            with self.subTest(isolated=isolated):
                s = DJSession(threading.Lock())
                tracks = [{'id': str(i), 'title': str(i)} for i in range(3)]
                s.update(order=tracks[:2], active=True, phase='playing', index=0,
                         rollingQueue={'status':'preparing'},
                         queuedUpdate={'id':'refill','status':'analyzing','tracks':tracks,
                                       'protectedIds':['0','1'],'rollingTrackId':'2'})
                job = Mock(revision='refill', done=True, cache={}, analysis_miss='2', catalog_miss=None)
                job.error = 'Different recording identity.'
                job.snapshot.return_value = {'message':job.error}
                job.close.side_effect = BridgeError('Spare identity changed.')
                job.cleanup_descriptors.return_value = [{'version':1,'deck':3,'ownerId':'a'*32,
                    'selectionId':'b'*64,'baseline':{'volume':1,'pfl':False,'effect':'Echo'}}]
                s.background_job = job
                client = Mock()
                with patch('afterhours.vdj_session.guard_isolation',
                           side_effect=None if isolated else BridgeError('Deck is not muted.')):
                    if isolated:
                        s.service_background(client,24)
                        self.assertEqual(s.state['queuedUpdate']['analysisMissId'],'2')
                        self.assertEqual(s.preparation_decks(),(4,))
                    else:
                        with self.assertRaisesRegex(BridgeError,'not muted'):
                            s.service_background(client,24)
                self.assertTrue(s.state['resumeBlocked'])
                self.assertEqual(s.state['order'],tracks[:2])
                client.send.assert_not_called()

    def test_missing_future_record_keeps_selected_queue_and_clears_released_deck_display(self):
        s=DJSession(threading.Lock());tracks=[{'id':str(i),'title':str(i)} for i in range(4)]
        pending={'id':'revision','status':'analyzing','tracks':copy.deepcopy(tracks),
                 'protectedIds':['0','1'],'reason':'Keep the selected continuation'}
        s.update(order=tracks[:2],queuedUpdate=pending,
                 setlist={'allowReorder':True,'lockedIds':['3'],'sourceById':{'2':'operator','3':'operator'}})
        class Job:
            revision='revision';done=True;error='House Dawgs — CamelPhat was not found; availability is unconfirmed.'
            cache={};owned=True
            def step(self,client,seconds):pass
            def close(self,client):self.owned=False
            def snapshot(self):
                return {'status':'blocked','message':self.error,'decks':[{'deck':3,'owned':True}] if self.owned else []}
        job=Job();s.background_job=job
        s.service_background(None,20)
        state=s.snapshot()
        self.assertEqual(state['backgroundAnalysis']['status'],'blocked')
        self.assertEqual(state['backgroundAnalysis']['decks'],[])
        self.assertEqual(state['queuedUpdate']['tracks'],tracks)
        self.assertEqual(state['queuedUpdate']['status'],'blocked')
        self.assertEqual(state['setlist']['lockedIds'],['3'])
        self.assertEqual([t['id'] for t in state['order']],['0','1'])
        self.assertIsNone(s.background_job)
        self.assertIn('House Dawgs',state['backgroundAnalysis']['message'])
        self.assertFalse(state.get('resumeBlocked'))


    def test_read_timeout_retries_when_another_spare_is_already_isolated(self):
        s = DJSession(threading.Lock())
        tracks = [{'id': str(i), 'title': str(i)} for i in range(3)]
        s.update(order=tracks[:2], active=True, phase='playing', resumeBlocked=True,
                 cleanupPending={'unknown': False, 'analysis': [{'deck': 3}], 'ramps': []},
                 queuedUpdate={'id': 'refill', 'status': 'analyzing', 'tracks': tracks,
                               'protectedIds': ['0', '1'], 'rollingTrackId': '2'})
        s.state['copilot']['mode'] = 'leading'
        job = Mock(revision='refill', cache={})
        job.step.side_effect = OSCReplyTimeout('Lost spare-deck reply.')
        job.snapshot.return_value = {'message': 'Preparing a measured refill.', 'decks': []}
        s.background_job = job
        client = Mock()
        with patch('afterhours.vdj_session.guard_isolation') as guard:
            s.service_background(client, 20)
            guard.assert_called_once_with(client, 3)
        job.close.assert_called_once_with(client)
        self.assertTrue(s.state['resumeBlocked'])
        self.assertEqual(s.preparation_decks(), (4,))
        self.assertEqual(s.state['queuedUpdate']['status'], 'needs-analysis')
        client.send.assert_not_called()

    def test_missed_native_read_retries_only_the_cleaned_rolling_job(self):
        s=DJSession(threading.Lock())
        tracks=[{'id':str(i),'title':str(i)} for i in range(3)]
        pending={'id':'refill','status':'analyzing','tracks':tracks,
                 'protectedIds':['0','1'],'rollingTrackId':'2'}
        s.update(order=tracks[:2], queuedUpdate=pending, active=True, phase='playing')
        s.state['copilot']['mode']='leading'
        for attempt in range(3):
            job=Mock(revision='refill', cache={})
            job.step.side_effect=OSCReplyTimeout('Spare-deck OSC packet timed out.')
            job.snapshot.return_value={'message':'Preparing a measured refill.','decks':[]}
            s.background_job=job
            s.service_background(None, 20)
            job.close.assert_called_once()
            status=s.snapshot()['queuedUpdate']
            self.assertEqual(status['tracks'],tracks)
            self.assertEqual(status['oscReadRetries'],min(2,attempt+1))
            self.assertEqual(status['status'],'needs-analysis' if attempt<2 else 'blocked')
            self.assertIsNone(s.background_job)
        self.assertEqual(s.state['order'],tracks[:2])



    def test_exhausted_rolling_read_timeout_only_replaces_an_attributed_safe_pick(self):
        tracks = [{'id': str(i), 'title': str(i)} for i in range(4)]
        for scoped_id, locked, from_planner, replace in (
            ('3', [], True, True), (None, [], True, False),
            ('3', ['3'], True, False), ('3', ['2'], True, False),
            ('3', [], False, False),
        ):
            with self.subTest(scoped_id=scoped_id, locked=locked, from_planner=from_planner):
                s = DJSession(threading.Lock())
                s.update(order=tracks[:2], active=True, phase='playing',
                         rollingQueue={'status': 'preparing'},
                         queuedUpdate={'id': 'refill', 'status': 'analyzing', 'tracks': tracks,
                                       'protectedIds': ['0', '1'], 'oscReadRetries': 2,
                                       'rollingTrackId': '2', 'rollingTrackIds': ['2', '3'],
                                       'sourceById': {'2': 'planner', '3': 'planner' if from_planner else 'operator'}})
                s.state['setlist']['lockedIds'] = locked
                s.state['copilot']['mode'] = 'leading'
                job = Mock(revision='refill', cache={}, analysis_miss=None, catalog_miss=None,
                           osc_timeout_track_id=scoped_id)
                job.step.side_effect = OSCReplyTimeout('Spare-deck OSC packet timed out.')
                job.snapshot.return_value = {'message': 'Preparing a measured refill.', 'decks': []}
                s.background_job = job
                s.service_background(None, 20)
                job.close.assert_called_once()
                pending = s.state['queuedUpdate']
                self.assertEqual(pending['status'], 'blocked')
                self.assertEqual(pending.get('analysisMissId'), '3' if replace else None)
                self.assertEqual(s.state['order'], tracks[:2])


if __name__=='__main__':unittest.main()
