"""Fake planner + session. No model, video, audio or cloud requests."""
import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from afterhours.dj_agent import DJAgent,validate_decision
from test_dj_agent import Session,decision,settled

CTX={'ready':True,'runId':'a'*32,'ownerId':'browser-1','character':'nova'}
def gesture():
    result=decision();result.update(direction='',performance={'prompt':'Look up, smile and wave, then return to the mixer.','reason':'The operator requested a wave.'});return result

class PerformanceTests(unittest.TestCase):
    def make(self,result=None,context=CTX):
        s=Session();e=Mock(return_value=(result or gesture(),{'responseId':'fake'}));a=DJAgent(s,evaluator=e)
        a.state['direction']='Keep the house set steady.'
        a.message('Look up and wave',message_id='request-1',approved=True,performance_context=context);settled(a)
        return s,a,e
    def test_gesture_preserves_music_and_is_bound_to_requesting_session(self):
        s,a,e=self.make();c=a.snapshot()['performanceCommand']
        self.assertEqual(s.calls,[]);self.assertEqual(a.state['direction'],'Keep the house set steady.')
        self.assertEqual(c['status'],'pending');self.assertEqual(c['runId'],CTX['runId'])
        self.assertNotIn('ownerId',e.call_args.args[0]['performanceContext'])
        self.assertEqual(a.snapshot()['allowance']['remaining'],0)
    def test_music_selection_and_gesture_can_coexist(self):
        d=gesture();d.update(trackIds=['b','a','c'],direction='Warmer house next.')
        s,a,e=self.make(d);self.assertEqual(s.calls,[('plan',['b','a','c'])]);self.assertEqual(a.state['performanceCommand']['status'],'pending')
    def test_video_off_is_not_queued_for_later(self):
        s,a,e=self.make(context={'ready':False});c=a.state['performanceCommand']
        self.assertEqual(c['status'],'unavailable');self.assertEqual(s.calls,[])
        with self.assertRaises(ValueError):a.performance_delivery(c['id'],c['ownerId'],c['runId'],'dispatching')
    def test_atomic_claim_owner_session_expiry_and_ack_order(self):
        s,a,e=self.make();c=a.state['performanceCommand'];args=(c['id'],c['ownerId'],c['runId'])
        with self.assertRaises(ValueError):a.performance_delivery(c['id'],'different',c['runId'],'dispatching')
        a.performance_delivery(*args,'dispatching')
        with self.assertRaises(ValueError):a.performance_delivery(*args,'dispatching')
        a.performance_delivery(*args,'applied')
        with self.assertRaises(ValueError):a.performance_delivery(*args,'sent')
        s,a,e=self.make();c=a.state['performanceCommand'];a.clock=lambda:c['expiresAt']+1
        with self.assertRaises(ValueError):a.performance_delivery(c['id'],c['ownerId'],c['runId'],'dispatching')
        self.assertEqual(c['status'],'expired')
    def test_invalid_prompt_rejected_and_old_schema_still_supported(self):
        self.assertIsNone(validate_decision(decision(),Session().snapshot())['performance'])
        for value in ({'prompt':'x'*801,'reason':'No'}, {'prompt':'','reason':'No'}, 'wave'):
            d=gesture();d['performance']=value
            with self.assertRaises(ValueError):validate_decision(d,Session().snapshot())
    def test_orphaned_browser_command_expires_without_a_dispatch(self):
        s,a,e=self.make();c=a.state['performanceCommand'];a.clock=lambda:c['expiresAt']+1
        self.assertEqual(a.snapshot()['performanceCommand']['status'],'expired')
        self.assertEqual(s.calls,[])
    def test_restore_never_replays_and_fresh_clears_command(self):
        s,a,e=self.make()
        with tempfile.TemporaryDirectory() as d:
            a.path=Path(d)/'agent.json';a.save();b=DJAgent(s,a.path,evaluator=e)
            self.assertEqual(b.state['performanceCommand']['status'],'expired')
        a.fresh();self.assertIsNone(a.state['performanceCommand'])
    def test_refill_cannot_replay_performance(self):
        s=Session();s.update(active=True,phase='playing',index=0)
        e=Mock(return_value=(gesture(),{'responseId':'refill'}));a=DJAgent(s,evaluator=e)
        a.enable(True);a.state['direction']='Steady house';a.tick(force=True);settled(a)
        self.assertIsNone(a.state['performanceCommand'])

if __name__=='__main__':unittest.main()
