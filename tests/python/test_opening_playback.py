"""Real planning with simulated native I/O; never touches live decks."""
import copy
import threading
import unittest
from unittest.mock import Mock,patch
from afterhours import ai_mix
from afterhours import cue_planner
from afterhours.dj_bridge import BridgeError
from afterhours.opening_playback import OpeningHandoffHeld,measured_plan,start_and_prepare
from test_cue_planner import fixture
from afterhours.vdj_catalog import identity
from afterhours.vdj_session import DJSession,TrackMetadataChanged


class OpeningTests(unittest.TestCase):
    def setup_session(self):
        _,report,_=fixture(3)
        order=copy.deepcopy(report['tracks'])
        for i,t in enumerate(order):t['engine']={'selection_id':identity(str(i))}
        s=DJSession(threading.Lock());s.update(order=order,candidates=order,phase='ready')
        playing=[False];trace=[];beat=[0.]
        def telemetry(client):
            value={'decks':[{'deck':1,'id':'0','playing':playing[0],'bpm':155,
                            'beatPosition':beat[0],'position':beat[0]*60/155},
                           {'deck':2,'id':None if 'prepare-next' not in trace else '1','playing':False}],
                   'crossfader':0}
            s.update(telemetry=value);return value
        def send(client,script):
            trace.append(script)
            if script=='deck 1 play':playing[0]=True;beat[0]=1
        def q(client,script,kind=None):
            return {'deck 1 loaded':True,'deck 1 get_filepath':'0','deck 1 deck_has_error':False,
                    'deck 1 play':playing[0],'deck 2 play':False,'deck 1 get_bpm absolute':155,
                    'deck 1 get_time total absolute':400000}[script]
        s.send=send;s.q=q;s.telemetry=telemetry;s.sleep=lambda _:None;s.match_tempo=Mock()
        s.prepare_deck=Mock(side_effect=lambda *a,**k:trace.append('prepare-next'))
        return s,report,trace,playing,beat

    def test_play_precedes_analysis_and_only_opening_pair_blocks_first_handoff(self):
        self.check_opening('leading')

    def test_fast_opening_preserves_mixing_only_while_preparing_spare_decks(self):
        self.check_opening('mix')

    def test_rolling_opening_spends_the_first_preparation_slot_on_track_three(self):
        self.check_opening('leading',rolling=True)

    def test_timed_start_services_the_spares_after_native_play_is_confirmed(self):
        self.check_opening('leading', rolling=True, timed=True)

    def test_playing_opening_adopts_a_small_native_grid_refinement(self):
        self.check_opening('leading',refined=155.5)

    def test_playing_opening_holds_if_pitch_changed(self):
        self.check_opening('leading',refined=155.5,manual_pitch=True)

    def test_playing_opening_adopts_independently_measured_grid_over_initial_default(self):
        self.check_opening('leading',refined=151,corroborated=True)

    def test_start_first_uses_measured_strategy_after_a_quick_set(self):
        s,_,_,_,_=self.setup_session()
        s.update(mixStyle='quick',quickBlendSeconds=30,quickTargetBpm=122)
        client=Mock()
        client.status.return_value={'automix':False,'decks':[{'playing':False}]*2}
        client.query.side_effect={'get_decks':4,'deck 3 get_deck':3,'deck 4 get_deck':4}.__getitem__
        s.choose_rotation=Mock(return_value=(1,2))
        captured={}

        def prepare(session, client, tracks):
            snapshot=session.snapshot()
            captured.update(style=snapshot['mixStyle'],blend=snapshot['quickBlendSeconds'],
                            target=snapshot['quickTargetBpm'],seconds=snapshot['secondsPerTrack'],ids=[t['id'] for t in tracks])
            raise BridgeError('End fixture before native playback.')

        with patch.object(s,'_launch') as launch, patch('afterhours.opening_playback.start_and_prepare',prepare):
            s.start(immediate=True,cue_led_opening=True)
            worker=launch.call_args.args[0]
            with self.assertRaisesRegex(BridgeError,'End fixture'):
                worker(client)
        self.assertEqual(captured,{'style':'phrase','blend':None,'target':None,'seconds':60,'ids':['0','1','2']})

    def test_two_deck_voice_start_cues_the_next_track_before_play(self):
        s,_,trace,_,_=self.setup_session()
        client=Mock()
        client.status.return_value={'automix':False,'decks':[{'playing':False}]*2}
        client.query.return_value=2
        s.choose_rotation=Mock(return_value=(1,2))
        s.prepare_deck=Mock(side_effect=[None, BridgeError('End fixture before native playback.')])
        with patch.object(s,'_launch') as launch:
            s.start(immediate=True,allow_grid_opening=True)
            worker=launch.call_args.args[0]
            with self.assertRaisesRegex(BridgeError,'End fixture'):
                worker(client)
        self.assertEqual(s.snapshot()['mixStyle'],'quick')
        self.assertEqual([call.args[1]['id'] for call in s.prepare_deck.call_args_list],['0','1'])
        self.assertNotIn('deck 1 play', trace)

    def check_opening(self,mode,rolling=False,refined=None,manual_pitch=False,corroborated=False,timed=False):
        s,report,trace,playing,beat=self.setup_session()
        def recover_spare(*_):
            self.assertTrue(playing[0])
            trace.append('check-spare')
        s.service_late_spare_load = Mock(side_effect=recover_spare)
        if timed:
            s.update(entryCadence=[60,30])
        if refined:
            if corroborated:report['tracks'][0]['bpm']=refined
            read_telemetry=s.telemetry
            native_query=s.q
            def read_refined(client):
                value=read_telemetry(client)
                if 'close-analysis' in trace:value['decks'][0]['bpm']=refined
                return value
            def query_refined(client,script,kind=None):
                if script=='deck 1 get_bpm absolute' and not manual_pitch:return refined
                return native_query(client,script,kind)
            s.telemetry=read_refined;s.q=query_refined
        s.state['copilot']['mode']=mode
        if rolling:
            extra={**s.state['order'][2],'id':'3','title':'Fourth'}
            order=[*s.state['order'],extra]
            s.update(order=order,candidates=order,rollingQueue={'status':'ready'})
            s.state['setlist']['sourceById']={t['id']:'planner' for t in order}
        class Analysis:
            def __init__(job,session,revision,tracks,cache):
                self.assertTrue(playing[0]);self.assertEqual([t['id'] for t in tracks],['0','1'])
                trace.append('analyze');job.done=False;job.cache={};job.error=None
                job.result=copy.deepcopy(report['tracks'][:2])
            def step(job,c,remaining):job.done=True
            def snapshot(job):return {'message':'Checked','status':'complete','deck':3}
            def close(job,c):trace.append('close-analysis')
        with patch('afterhours.opening_playback.BackgroundAnalysis',Analysis),patch('afterhours.opening_playback.TransitionMixer') as mixer,patch('afterhours.cue_planner.verify_receipts'):
            mixer.return_value.baselines={}
            if manual_pitch:
                with self.assertRaisesRegex(BridgeError,'Opening tempo changed'):
                    start_and_prepare(s,None,s.snapshot()['order'])
                self.assertTrue(playing[0])
                self.assertNotIn('prepare-next',trace)
                return
            order,plan,t,window=start_and_prepare(s,None,s.snapshot()['order'])
        self.assertLess(trace.index('deck 1 play'),trace.index('analyze'))
        if timed:
            self.assertLess(trace.index('analyze'),trace.index('check-spare'))
            self.assertLess(trace.index('check-spare'),trace.index('prepare-next'))
        else:
            s.service_late_spare_load.assert_not_called()
        self.assertLess(trace.index('analyze'),trace.index('prepare-next'))
        self.assertEqual(trace.count('deck 1 play'),1)
        s.match_tempo.assert_not_called()
        self.assertIn('deck 1 pitch 100%', trace[0])
        self.assertFalse(any('deck 1 goto' in c for c in trace[trace.index('deck 1 play')+1:]))
        self.assertTrue(plan['applied']);self.assertEqual(window['entryBeat'],0)
        self.assertEqual([t['id'] for t in order],['0','1'])
        self.assertEqual([t['id'] for t in s.state['queuedUpdate']['tracks']],['0','1','2'])
        if rolling:
            self.assertEqual(s.state['queuedUpdate']['rollingTrackId'],'2')
            self.assertEqual([t['id'] for t in s.state['timedContinuation']],['3'])
        self.assertTrue(playing[0]);self.assertEqual(s.state['startup']['status'],'ready')
        self.assertEqual(s.state['startup']['targetBpm'],refined or 155)
        self.assertEqual(s.state['copilot']['mode'],mode)
        review=s.state['aiReview']
        recovered=ai_mix.validate_plan({k:review.get(k) for k in ai_mix.PLAN_SCHEMA['properties']},s.state['analysis'],30)
        self.assertEqual(recovered['selections'],review['selections'])
        self.assertEqual(recovered['missingEvidence'],[])

    def test_opening_uses_early_verified_exit_even_if_late_entrance_scores_better(self):
        s,report,_,_,_=self.setup_session()
        first=report['tracks'][0]
        early=first['cueCandidates'][0]
        early['entry']['levelVariationDb']=7
        later={**early,'id':'late','entry':{**early['entry'],'levelVariationDb':0}}
        for field in ('entryBeat','exitBeat','endBeat'):later[field]+=400
        for field in ('entrySeconds','introEndSeconds','exitSeconds','endSeconds'):later[field]+=400*60/155
        first['cueCandidates'].insert(0,later)
        prepared=measured_plan(s.snapshot(),s.state['order'][:2],report['tracks'][:2],{'beatPosition':12,'bpm':155})
        self.assertEqual(prepared['executionPlan']['selections'][0]['id'],early['id'])

    def test_rolling_set_leaves_verified_exit_runway_on_the_incoming_record(self):
        s,report,_,_,_=self.setup_session()
        incoming=report['tracks'][1]
        early=incoming['cueCandidates'][0]
        early['entry']['levelVariationDb']=7
        late=copy.deepcopy(early)
        late['id']='late-quiet-entrance'
        late['entry']['levelVariationDb']=0
        for field in ('entryBeat','exitBeat','endBeat'):late[field]+=320
        for field in ('entrySeconds','introEndSeconds','exitSeconds','endSeconds'):late[field]+=320*60/155
        incoming['cueCandidates'].append(late)
        args=(s.state['order'][:2],report['tracks'][:2],{'beatPosition':12,'bpm':155})
        ordinary=measured_plan(s.snapshot(),*args)
        rolling=measured_plan({**s.snapshot(),'rollingQueue':{'status':'ready'}},*args)
        self.assertEqual(ordinary['executionPlan']['selections'][1]['id'],late['id'])
        self.assertEqual(rolling['executionPlan']['selections'][1]['id'],early['id'])

    def test_analysis_timeout_holds_handoff_without_replaying_or_stopping_opening(self):
        s,_,trace,playing,_=self.setup_session()
        job=Mock(done=True,error='Audio analysis did not settle in time.',analysis_timed_out=True,cache={})
        with patch('afterhours.opening_playback.BackgroundAnalysis',return_value=job),patch('afterhours.opening_playback.TransitionMixer') as mixer:
            mixer.return_value.baselines={}
            with self.assertRaises(OpeningHandoffHeld):start_and_prepare(s,None,s.snapshot()['order'])
        self.assertTrue(playing[0])
        self.assertEqual(s.snapshot()['startup']['status'],'handoff-held')
        self.assertEqual(trace.count('deck 1 play'),1)
        self.assertNotIn('prepare-next',trace)

    def test_unknown_start_result_never_replays_play_or_starts_analysis(self):
        s,report,trace,playing,beat=self.setup_session()
        send=s.send
        def uncertain(c,v):
            send(c,v)
            if v=='deck 1 play':playing[0]=False
        s.send=uncertain
        with patch('afterhours.opening_playback.BackgroundAnalysis') as job,patch('afterhours.opening_playback.TransitionMixer') as mixer:
            mixer.return_value.baselines={}
            with self.assertRaisesRegex(BridgeError,'No second Play'):start_and_prepare(s,None,s.snapshot()['order'])
            job.assert_not_called()
        self.assertEqual(trace.count('deck 1 play'),1)

    def test_play_flag_with_a_stalled_native_clock_does_not_start_analysis(self):
        s,_,trace,_,beat=self.setup_session()
        send=s.send
        def stalled(client,script):
            send(client,script)
            if script=='deck 1 play':beat[0]=0
        s.send=stalled
        with patch('afterhours.opening_playback.BackgroundAnalysis') as job,patch('afterhours.opening_playback.TransitionMixer'):
            with self.assertRaisesRegex(BridgeError,'playhead did not advance'):
                start_and_prepare(s,None,s.snapshot()['order'])
            job.assert_not_called()
        self.assertEqual(s.snapshot()['startup']['status'],'blocked')
        self.assertIsNone(s.snapshot().get('startedAt'))
        self.assertEqual(trace.count('deck 1 play'),1)

    def test_no_future_measured_exit_never_fabricates_or_seeks_an_exit(self):
        s,report,_,_,_=self.setup_session()
        with self.assertRaisesRegex(ValueError,'no verified exit'):
            measured_plan(s.snapshot(),s.state['order'],report['tracks'],{'beatPosition':120,'bpm':155})

    def test_key_only_refinement_preserves_playing_cues_and_recalculates_future_handoff(self):
        s,report,_,_,_=self.setup_session();_,_,answer=fixture(3)
        review=ai_mix.validate_plan(answer,report,30)
        plan=cue_planner.compile_plan(review,report,s.state['order'],30);plan['applied']=True
        s.update(analysis=report,aiReview=review,executionPlan=plan)
        actual={**s.state['order'][2],'camelot':'3B','key':'C#'}
        with patch('afterhours.cue_planner.verify_receipts'):
            order,prepared=s.refresh_key_plan(s.snapshot(),actual,2)
        self.assertEqual(order[2]['camelot'],'3B')
        self.assertEqual(prepared['executionPlan']['selections'],plan['selections'])
        self.assertEqual(prepared['executionPlan']['transitions'][0],plan['transitions'][0])
        self.assertEqual(prepared['executionPlan']['tempoSchedule'][:2],plan['tempoSchedule'][:2])
        self.assertEqual(s.state['order'][2]['camelot'],'2A')
        for bad in ({**actual,'id':'other'},{**actual,'bpm':160},{**actual,'camelot':None}):
            with self.assertRaises(TrackMetadataChanged):s.refresh_key_plan(s.snapshot(),bad,2)

if __name__=='__main__':unittest.main()
