"""Opening preparation and musical queue judgments. Fake model/native engine only."""
import copy,json,unittest,tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from afterhours.dj_agent import DJAgent,music_context,validate_decision,evaluate,MAX_REQUEST_BYTES
from test_dj_agent import Session,decision,settled

class PreparationTests(unittest.TestCase):
    def make(self):
        s=Session();a=DJAgent(s,evaluator=Mock(side_effect=AssertionError('No chat call allowed')))
        return s,a
    def finish(self,a):
        for _ in range(6):a.tick(force=True)
    def test_explicit_prepare_plans_applies_once_and_never_starts_audio(self):
        s,a=self.make()
        with self.assertRaises(ValueError):a.prepare_set(False,'req')
        self.assertEqual(s.calls,[])
        a.prepare_set(True,'req');self.finish(a)
        self.assertEqual(s.calls,[('analyze',),('review',True),('apply',)])
        self.assertIsNone(a.snapshot()['preparation']);self.assertIn('Playback is paused',a.snapshot()['message'])
        a.prepare_set(True,'req');self.finish(a)
        self.assertEqual(s.calls,[('analyze',),('review',True),('apply',)])
        self.assertFalse(s.state['active']);a.evaluator.assert_not_called()
    def test_preparation_does_not_use_or_grant_chat_allowance(self):
        s,a=self.make();a.enable(True,1800,5);a.prepare_set(True,'req');self.finish(a)
        self.assertEqual(a.snapshot()['allowance']['remaining'],5)
    def test_selected_audio_and_review_are_reused(self):
        s,a=self.make();s.update(analysis={'tracks':s.state['order']})
        a.prepare_set(True,'req');self.finish(a)
        self.assertEqual(s.calls,[('review',True),('apply',)])
        s,a=self.make();s.update(analysis={'tracks':s.state['order']},aiReview={'decision':'propose'})
        a.prepare_set(True,'req');self.finish(a);self.assertEqual(s.calls,[('apply',)])
        s,a=self.make();s.update(executionPlan={'applied':True})
        a.prepare_set(True,'req');self.finish(a);self.assertEqual(s.calls,[])
    def test_native_analysis_waits_until_complete(self):
        s,a=self.make()
        def begin():s.calls.append(('analyze',));s.update(active=True,phase='analyzing',analysis=None)
        s.prepare_analysis=begin;a.prepare_set(True,'req');self.finish(a)
        self.assertEqual(s.calls,[('analyze',)]);self.assertIsNotNone(a.snapshot()['preparation'])
        s.update(active=False,phase='ready',analysis={'tracks':s.state['order']});self.finish(a)
        self.assertEqual(s.calls,[('analyze',),('review',True),('apply',)])
    def test_model_failure_is_not_retried_and_no_cues_are_applied(self):
        s,a=self.make();s.review_mix=Mock(side_effect=ValueError('Provider unavailable.'))
        a.prepare_set(True,'req');self.finish(a);a.prepare_set(True,'req');self.finish(a)
        s.review_mix.assert_called_once_with(approved=True)
        self.assertEqual(a.snapshot()['status'],'error');self.assertIsNone(a.snapshot()['preparation'])
        self.assertEqual(s.calls,[('analyze',)])
    def test_hold_does_not_apply_or_start(self):
        s,a=self.make()
        s.review_mix=lambda **kw:s.update(aiReview={'decision':'hold'})
        a.prepare_set(True,'req');self.finish(a)
        self.assertEqual(s.calls,[('analyze',)]);self.assertIn('held',a.snapshot()['message'])
    def test_playback_and_new_direction_cancel_preparation(self):
        s,a=self.make();s.update(active=True)
        with self.assertRaises(ValueError):a.prepare_set(True,'req')
        s.update(active=False,telemetry={'decks':[{'playing':True}]})
        with self.assertRaises(ValueError):a.prepare_set(True,'req')
        s.update(telemetry={'decks':[]});a.prepare_set(True,'req')
        a.state['revision']+=1;self.finish(a);self.assertEqual(s.calls,[('analyze',)])
    def test_retry_explicitly_reenters_after_native_error_and_is_scoped_to_queue(self):
        s,a=self.make();s.update(phase='error',message='Old error.')
        def analyze():s.calls.append(('analyze',));s.update(phase='ready',analysis={'tracks':s.state['order']})
        s.prepare_analysis=analyze;a.prepare_set(True,'req');self.finish(a)
        self.assertTrue(s.state['executionPlan']['applied'])
        s,a=self.make();a.prepare_set(True,'req');s.update(order=s.state['order'][:2]);self.finish(a)
        self.assertEqual(s.calls,[('analyze',)])
    def test_explicit_allowance_not_restored_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';s=Session();a=DJAgent(s,path);a.prepare_set(True,'req')
            b=DJAgent(s,path);self.finish(b);self.assertIsNone(b.snapshot()['preparation']);self.assertEqual(s.calls,[('analyze',)])

class MusicalJudgmentTests(unittest.TestCase):
    def make(self,result):
        s=Session()
        for t in s.state['candidates']:t['bpm']=120
        s.update(order=copy.deepcopy(s.state['candidates']),maxTempoShift=8)
        s.state['candidates'].append({'id':'fast','title':'Fast Request','artist':'Fixture','bpm':150,'camelot':'8A'})
        e=Mock(return_value=(result,{'responseId':'fake-judgment'}));a=DJAgent(s,evaluator=e)
        return s,a,e
    def test_actual_target_pitch_and_limits_reach_model(self):
        s,a,e=self.make(decision());s.update(active=True,phase='playing',index=0,executionPlan={'targetBpm':120,'applied':True})
        a.message('Add something faster',message_id='one',approved=True);settled(a)
        music=e.call_args.args[0]['music'];fast=next(t for t in music['catalog'] if t['id']=='fast')
        self.assertEqual(fast['pitchAtSetTempo'],-20);self.assertFalse(fast['withinTempoLimit'])
        self.assertEqual(music['protectedIds'],['a','b']);self.assertTrue(music['tempoPolicy']['tempoRampSupported'])
    def test_extra_musical_context_stays_inside_the_approved_request_budget(self):
        s,a,e=self.make(decision())
        s.state['candidates'] += [{'id':f'other-{i}','title':f'Track {i} '+'Extended original title '*10,
            'artist':'An artist '*10,'bpm':124,'camelot':'8A','genre':'House'} for i in range(30)]
        packet={'typedTurn':True,'music':music_context(s.snapshot()),'pastSets':[{'direction':'Old style '*400} for _ in range(4)],
            'messages':[{'role':'user','text':'Old context '*150} for _ in range(10)]+[{'role':'user','text':'Keep this house groove'}]}
        seen=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps({'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(decision())}]}]}).encode()
        def opener(request,**kwargs):seen.append(request.data);return Response()
        with patch('afterhours.ai_mix.api_key',return_value='fake-key'):evaluate(packet,opener)
        self.assertEqual(len(seen),1);self.assertLessEqual(len(seen[0]),MAX_REQUEST_BYTES)
        bounded=json.loads(json.loads(seen[0])['input']);self.assertEqual(bounded['messages'][-1]['text'],'Keep this house groove')
        self.assertTrue(set(bounded['music']['currentOrder']) <= {t['id'] for t in bounded['music']['catalog']})
    def test_model_holds_track_with_reason_without_modifying_playing_order(self):
        d=decision();d['reply']='That would jump away from this house groove. I would save it for a faster set.'
        d['deferredTracks']=[{'trackId':'fast','reason':'150 BPM breaks the 120 BPM house direction; save it for a faster set.'}]
        s,a,e=self.make(d);s.update(active=True,phase='playing',index=0)
        a.message('Add Fast Request',message_id='one',approved=True);settled(a)
        self.assertEqual(s.calls,[]);held=a.snapshot()['deferredTracks'][0]
        self.assertEqual(held['trackId'],'fast');self.assertEqual(held['source'],'planner')
        self.assertIn('faster',held['reason']);self.assertIn('at',held)
    def test_guard_holds_impossible_model_selection_with_no_paid_retry(self):
        s,a,e=self.make(decision(['a','b','fast']));s.update(active=True,phase='playing',index=0)
        a.message('Add Fast Request next',message_id='one',approved=True);settled(a)
        for _ in range(6):a.tick(force=True)
        self.assertEqual(s.calls,[]);e.assert_called_once()
        self.assertEqual([t['id'] for t in s.state['order']],['a','b','c'])
        held=a.snapshot()['deferredTracks'][0];self.assertEqual(held['source'],'tempo-guard')
        self.assertIn('-20.0%',held['reason']);self.assertIn('Queue unchanged',a.snapshot()['message'])
    def test_holds_persist_can_be_cleared_and_cannot_invent_recordings(self):
        s,a,e=self.make(decision());d=decision();d['deferredTracks']=[{'trackId':'missing','reason':'Unknown'}]
        with self.assertRaises(ValueError):validate_decision(d,s.snapshot())
        d['deferredTracks']=[{'trackId':'a','reason':'Later'}];d['trackIds']=['a','b']
        self.assertEqual(validate_decision(d,s.snapshot())['deferredTracks'],[])
        a.state['deferredTracks']=[{'trackId':'fast','title':'Fast Request','bpm':150,'reason':'Later','at':123}]
        e.return_value=({**decision(),'deferredTracks':[]},{'responseId':'clear'})
        a.message('Remove my held requests',message_id='clear',approved=True);settled(a)
        self.assertEqual(a.snapshot()['deferredTracks'],[])
    def test_compatible_tail_update_keeps_playing_and_cued_tracks(self):
        s,a,e=self.make(decision(['a','b','fast']));s.state['candidates'][-1]['bpm']=124
        s.update(active=True,phase='playing',index=0)
        a.message('Add that next',message_id='one',approved=True);settled(a)
        self.assertEqual(s.calls,[('plan',['a','b','fast'])]);self.assertIsNone(a.snapshot()['preparation'])

if __name__=='__main__':unittest.main()
