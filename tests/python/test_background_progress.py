"""Queue preparation receipts use the actual post-cleanup ownership state."""
import copy
import threading
import unittest
from unittest.mock import Mock

from afterhours.dj_bridge import OSCReplyTimeout

from afterhours.vdj_session import DJSession


class BackgroundProgressTests(unittest.TestCase):
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



if __name__=='__main__':unittest.main()
