"""Synthetic tempo routing/native command checks. No real music or model use."""
import copy,threading,unittest
from unittest.mock import Mock,patch
from afterhours.tempo_planner import schedule,estimate,TempoRouteError,TempoRamp
from afterhours.vdj_catalog import identity
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_session import DJSession,validate_order,TempoFitError
from test_cue_planner import fixture
from afterhours import ai_mix, cue_planner

def tracks(bpms):
    return [{'id':str(i),'title':f'Track {i}','bpm':b,'engine':{'selection_id':identity(str(i))}} for i,b in enumerate(bpms)]

class Routes(unittest.TestCase):
    def test_native_float_bpm_does_not_add_a_bar_at_the_ramp_boundary(self):
        pool=tracks([118.76122283935547,121.94110107421875,127.99922943115234])
        route=estimate(pool,30)
        self.assertEqual(route[0]['durationBeats'],52)
        self.assertEqual(route[1]['durationBeats'],20)
        for row in route:
            self.assertLessEqual(abs(row['toBpm']-row['fromBpm']),row['durationBeats']*1/32+1e-6)
            self.assertLessEqual(row['startOffsetBeats']+row['durationBeats'],56)

    def test_long_route_can_progress_from_120_to_150_without_overpitch(self):
        pool=tracks([120,124,128,132,136,140,144,148,150])
        route=estimate(pool,300)
        self.assertEqual(route[0]['fromBpm'],120);self.assertEqual(route[-1]['toBpm'],150)
        for i,(r,t) in enumerate(zip(route,pool)):
            self.assertLessEqual(abs(r['toBpm']-r['fromBpm']),4)
            self.assertLessEqual(abs(r['toBpm']/t['bpm']-1),.08)
            if i:self.assertEqual(r['fromBpm'],route[i-1]['toBpm'])
        self.assertEqual(validate_order([t['id'] for t in pool],pool,8,300),pool)
    def test_direct_120_to_150_and_impossible_short_request_are_held(self):
        with self.assertRaises(TempoRouteError):estimate(tracks([120,150]),300)
        with self.assertRaises(TempoFitError):validate_order(['0','1'],tracks([120,150]))
        with self.assertRaises(TempoRouteError):estimate(tracks([125,128]),30,targets=[{'trackId':'0','bpm':128}])
    def test_quick_preview_cannot_accept_a_route_it_will_not_execute(self):
        pool=tracks([120,124,128,132,136,140,144,148,150])
        with self.assertRaises(TempoFitError):validate_order([t['id'] for t in pool],pool,8,300,fixed=True)
        with self.assertRaises(ValueError):validate_order(['0','1'],pool[:2],tempo_targets=[{'trackId':'0','bpm':121}],fixed=True)
    def test_actual_protected_timing_is_not_replaced_by_generic_selection_overlap(self):
        pool=tracks([125,126.5,128]);cues=[{'entryBeat':0,'exitBeat':64}]*3
        actual=schedule(pool,cues,[{'fadeBeats':8}]*2)
        self.assertEqual(actual[1]['durationBeats'],44)
        generic=estimate(pool,30,frozen=actual)
        self.assertEqual(generic,actual)
    def test_ramps_exclude_previous_blend_and_leave_settling_space(self):
        pool=tracks([125,127,129]);cues=[{'entryBeat':80,'exitBeat':208}]*3
        trans=[{'fadeBeats':64}]*2;route=schedule(pool,cues,trans)
        for i,r in enumerate(route):
            self.assertGreaterEqual(r['startOffsetBeats'],(64 if i else 0)+4)
            self.assertLessEqual(r['startOffsetBeats']+r['durationBeats'],120)
            self.assertLessEqual(abs(r['toBpm']-r['fromBpm']),r['durationBeats']/32+1e-6)
    def test_protected_targets_and_invalid_numbers_cannot_be_overridden(self):
        pool=tracks([125,126,127]);route=estimate(pool,60)
        again=estimate(pool,60,frozen=route[:2]);self.assertEqual(again[:2],route[:2])
        with self.assertRaises(ValueError):estimate(pool,60,frozen=route[:2],targets=[{'trackId':'0','bpm':124}])
        for n in (float('nan'),float('inf'),True,1000):
            with self.subTest(n=n),self.assertRaises(ValueError):estimate(pool,60,targets=[{'trackId':'1','bpm':n}])
    def test_legacy_approved_plans_keep_fixed_tempo(self):
        _,report,response=fixture();report.pop('tempoVersion')
        review=ai_mix.validate_plan(response,report,30);plan=cue_planner.compile_plan(review,report,report['tracks'],30)
        self.assertNotIn('tempoSchedule',plan)
    def test_restart_preserves_a_measured_route_that_needs_a_shorter_actual_overlap(self):
        _,report,answer=fixture()
        ids={t['id']:'virtualdj:'+identity(t['id']) for t in report['tracks']}
        for i,t in enumerate(report['tracks']):
            t.update(id=ids[t['id']],bpm=155+i,engine={'selection_id':ids[str(i)].removeprefix('virtualdj:'),'folder_id':identity('folder')})
        for r in answer['selections']:r['trackId']=ids[r['trackId']]
        for r in answer['transitions']:r.update(fromId=ids[r['fromId']],toId=ids[r['toId']],blendBeats=8)
        targets=[{'trackId':ids['0'],'bpm':156},{'trackId':ids['1'],'bpm':157.25}]
        report.update(fingerprint=cue_planner.fingerprint(report['tracks']),tempoTargets=targets)
        review=ai_mix.validate_plan(answer,report,30)
        with self.assertRaises(TempoFitError):validate_order(list(ids.values()),report['tracks'],tempo_targets=targets)
        plan=cue_planner.compile_plan(review,report,report['tracks'],30)
        saved={'candidates':report['tracks'],'order':report['tracks'],'secondsPerTrack':30,
               'mixStyle':'phrase','analysis':report,'aiReview':review,'executionPlan':plan,'tempoTargets':targets}
        session=DJSession(threading.Lock())
        with patch('afterhours.cue_planner.verify_receipts'),patch.object(session,'factory') as native:
            restored=session.restore(saved)
        native.assert_not_called();self.assertFalse(restored['active']);self.assertIsNone(restored['executionPlan'])
        self.assertEqual(restored['tempoTargets'],targets)
    def test_background_plan_preserves_protected_cues_and_discloses_local_policy(self):
        _,report,response=fixture();review=ai_mix.validate_plan(response,report,30)
        plan=cue_planner.compile_plan(review,report,report['tracks'],30);plan['applied']=True
        s={'executionPlan':plan,'analysis':report,'aiReview':review,'secondsPerTrack':30,'mixStyle':'phrase','maxTempoShift':8}
        result=cue_planner.complete_background_queue(s,report['tracks'],[], 'continue',2)
        self.assertEqual(result['executionPlan']['selections'][:2],plan['selections'][:2])
        self.assertEqual(result['executionPlan']['tempoSchedule'][:2],plan['tempoSchedule'][:2])
        self.assertIn('local measured',result['aiReview']['source'])
        self.assertTrue(all(t['fadeBeats']<=16 for t in result['executionPlan']['transitions'][1:]))

class NativeRamp(unittest.TestCase):
    def setup_ramp(self):
        self.sent=[];self.readings={'pitch_lock':False,'deck 1 get_bpm absolute':125,
            'deck 1 pitch_range':.08,'deck 1 pitch':.5,'deck 1 get_filepath':'0'}
        self.client=Mock();self.client.send.side_effect=self.sent.append
        self.ramp=TempoRamp(lambda c,s,k=None:self.readings[s],lambda c,s:self.sent.append(s))
        self.row={'fromBpm':125,'toBpm':126,'startOffsetBeats':4,'durationBeats':32,'maxPitchPercent':8,'reason':'Gradual lift'}
        self.ramp.arm(tracks([125])[0],1,self.row,0)
    def tick(self,beat,bpm):return self.ramp.tick(self.client,{'id':'0','playing':True,'beatPosition':beat,'bpm':bpm},{'playing':False})
    def test_one_native_ramp_and_verified_endpoint(self):
        self.setup_ramp();self.tick(1,125);self.assertEqual(self.sent,[])
        self.tick(4,125);self.tick(20,125.5);self.tick(36,126)
        self.assertTrue(self.ramp.done);self.assertEqual(len(self.sent),2)
        self.assertIn('32bt & deck 1 pitch',self.sent[0]);self.assertIn('0.550000000',self.sent[0])
        self.assertTrue(self.sent[0].startswith('deck 1 repeat_start_fade'))
        self.assertTrue(self.sent[1].startswith('deck 1 repeat_stop'))
        self.tick(38,126);self.assertEqual(len(self.sent),2)
    def test_near_endpoint_does_not_stop_native_ramp_early(self):
        self.setup_ramp();self.tick(4,125);self.tick(36,125.88)
        self.assertFalse(self.ramp.done);self.assertEqual(len(self.sent),1)
        self.tick(37.5,126)
        self.assertTrue(self.ramp.done);self.assertEqual(len(self.sent),2)
    def test_missed_window_bad_range_and_linked_pitch_send_no_ramp(self):
        for field,value in [('pitch_lock',True),('deck 1 get_filepath','swapped'),('deck 1 pitch_range',.001)]:
            self.setup_ramp();self.readings[field]=value
            with self.subTest(field=field),self.assertRaises((BridgeError,ValueError)):self.tick(4,125)
            self.assertFalse(self.sent)
        self.setup_ramp()
        with self.assertRaises(BridgeError):self.tick(7,125)
        self.assertFalse(self.sent)
    def test_operator_pitch_change_cancels_instead_of_catching_up(self):
        self.setup_ramp();self.tick(4,125)
        with self.assertRaises(BridgeError):self.tick(20,128)
        self.assertEqual(len(self.sent),2);self.assertTrue(self.sent[-1].startswith('deck 1 repeat_stop'))
    def test_cancellation_survives_session_cancel_flag(self):
        self.setup_ramp();self.tick(4,125);self.ramp.send=Mock(side_effect=AssertionError('session stopped'))
        self.ramp.cancel(self.client);self.assertFalse(self.ramp.running)
        self.assertTrue(self.sent[-1].startswith('deck 1 repeat_stop'))

if __name__=='__main__':unittest.main()
