"""Display projection tests. No native audio or provider requests."""
import copy
import math
import time
import unittest
from unittest.mock import patch
from afterhours import ai_mix
from afterhours import cue_planner
from test_cue_planner import fixture
from afterhours.program_timeline import set_timeline


class TimelineTests(unittest.TestCase):
    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_opening_playhead_keeps_the_audio_played_before_the_plan_was_ready(self,_):
        s=self.state();s['executionPlan']['applied']=True
        s.update(startup={'status':'ready','openingId':'0','entryBeat':0,'entrySeconds':0},active=True,phase='playing')
        plan=copy.deepcopy(s['executionPlan'])
        result=set_timeline(s,{'id':'0','beatPosition':80})
        self.assertAlmostEqual(result['positionSeconds'],80*60/155)
        self.assertEqual(result['segments'][0]['entryBeat'],0)
        self.assertEqual(s['executionPlan'],plan)

    @patch('afterhours.program_timeline.detailed_contour',return_value=((0,20,-12),(20,80,-18)))
    @patch('afterhours.program_timeline.verified_energy',return_value={'reportDigest':'verified','contour':[]})
    def test_manual_playhead_moves_without_a_prepared_set_and_never_invents_a_queue(self,energy,contour):
        sid='virtualdj:'+'a'*64
        deck={'id':sid,'deck':1,'title':'Manual recording','loaded':True,'playing':True,'position':12,
              'controls':{'durationMs':100000,'sampledAt':time.time()}}
        state={'id':'manual-set','order':[],'executionPlan':None}
        first=set_timeline(state,deck);deck['position']=23;second=set_timeline(state,deck)
        self.assertEqual(first['kind'],'observed-deck');self.assertEqual(first['positionSeconds'],12)
        self.assertEqual(second['positionSeconds'],23);self.assertEqual(first['runId'],second['runId'])
        self.assertEqual(len(second['segments']),1);self.assertEqual(second['segments'][0]['trackId'],sid)
        self.assertEqual(second['segments'][0]['contour'],[[0,20,-12],[20,80,-18]])
        self.assertIsNone(state['executionPlan']);energy.assert_called_with('a'*64,100)
        # Even an old prepared plan must not pin a manual playback clock to zero.
        state.update(phase='takeover',executionPlan={'applied':True,'targetBpm':125})
        self.assertEqual(set_timeline(state,deck)['positionSeconds'],23)

    @patch('afterhours.program_timeline.verified_energy',return_value=None)
    def test_missing_manual_waveform_stays_empty_and_stale_playhead_is_rejected(self,energy):
        deck={'id':'virtualdj:'+'a'*64,'deck':1,'loaded':True,'position':4,
              'controls':{'durationMs':100000,'sampledAt':time.time()}}
        data=set_timeline({'id':'manual'},deck)
        self.assertEqual(data['segments'][0]['contour'],[])
        self.assertFalse(data['segments'][0]['waveformAvailable'])
        deck['controls']['sampledAt']-=10;self.assertIsNone(set_timeline({'id':'manual'},deck))
        self.assertEqual(energy.call_count,1)

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_hold_keeps_current_ownership_without_inventing_audio_or_rewriting_cues(self,_):
        s=self.state();before=copy.deepcopy(s)
        original=set_timeline(s,{'id':'0','beatPosition':180})
        s.update(active=True,phase='playing',copilot={'held':True},index=0)
        held=set_timeline(s,{'id':'0','beatPosition':180})
        self.assertIsNotNone(held)
        rows=held['displaySegments'];position=held['positionSeconds']
        self.assertGreaterEqual(rows[0]['endSeconds'],position+60)
        self.assertEqual(rows[0]['timing'],'held-open')
        self.assertEqual(rows[1]['startSeconds'],rows[0]['endSeconds'])
        self.assertTrue(all(r['timing']=='unscheduled-while-held' for r in rows[1:]))
        self.assertEqual(held['segments'],original['segments'])
        self.assertEqual(s['executionPlan'],before['executionPlan'])
        self.assertEqual(rows[0]['contour'],original['displaySegments'][0]['contour'])
        s['copilot']['held']=False
        self.assertEqual(set_timeline(s,{'id':'0','beatPosition':180}),original)

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_operator_retime_moves_queue_and_survives_handoff_without_changing_source_plan(self,_):
        from afterhours import copilot_commands
        s=self.state();original=copy.deepcopy(s['executionPlan']);s['index']=0
        cue=copy.deepcopy(s['analysis']['tracks'][0]['cueCandidates'][0])
        cue.update(id='later',entryBeat=128,entrySeconds=128*60/155,exitBeat=192,
                   exitSeconds=192*60/155,endBeat=224,endSeconds=224*60/155)
        s['analysis']['tracks'][0]['cueCandidates'].append(cue)
        s['timing']=copilot_commands.retime(s,{'id':'0','beatPosition':140},32)
        first=set_timeline(s,{'id':'0','beatPosition':140})
        self.assertIsNotNone(first)
        self.assertAlmostEqual(first['segments'][1]['startSeconds'],128*60/155)
        self.assertAlmostEqual(first['positionSeconds'],76*60/155)
        receipt={'from':'0','to':'1','observedStartBeat':192.2,'timing':s['timing'],'incomingCueBeat':64}
        s['activeHandoff']=receipt
        during=set_timeline(s,{'id':'1','beatPosition':80})
        s.update(activeHandoff=None,transitions=[receipt],index=1,timing={})
        after=set_timeline(s,{'id':'1','beatPosition':80})
        self.assertEqual(after,during);self.assertEqual(s['executionPlan'],original)

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_retime_cannot_invent_an_unverified_timeline_exit(self,_):
        s=self.state();s['index']=0;cue=s['executionPlan']['selections'][0]
        s['timing']={'basis':'operator-steered-measured-exit','originalCandidateId':cue['id'],
                     'candidateId':'made-up','mixAtBeat':192}
        self.assertIsNone(set_timeline(s,{'id':'0','beatPosition':140}))
        s['timing']['candidateId']=cue['id']
        self.assertIsNone(set_timeline(s,{'id':'0','beatPosition':140}))

    def state(self):
        _,report,response=fixture()
        for t in report['tracks']:
            t['energyMap'].update(contour=[[0,400,-14]])
        report['fingerprint']=cue_planner.fingerprint(report['tracks'])
        review=ai_mix.validate_plan(response,report,30)
        plan=cue_planner.compile_plan(review,report,report['tracks'],30);plan['applied']=True
        return {'id':'fixture','order':report['tracks'],'analysis':report,'executionPlan':plan,'startedAt':100,'transitions':[]}

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_crossfade_keeps_continuous_clock_and_projects_all_queued_recordings(self,_):
        s=self.state();a,b=s['executionPlan']['selections'][:2]
        before=set_timeline(s,{'id':a['trackId'],'beatPosition':a['exitBeat']+16})
        after=set_timeline(s,{'id':b['trackId'],'beatPosition':b['entryBeat']+16})
        self.assertAlmostEqual(before['positionSeconds'],after['positionSeconds'])
        self.assertEqual(len(after['segments']),4)
        self.assertEqual(before['segments'],after['segments'])
        self.assertGreater(after['segments'][0]['endSeconds'],after['segments'][1]['startSeconds'])
        self.assertTrue(all(row[0]>=segment['startSeconds'] and row[0]+row[1]<=segment['endSeconds']+.001
                            for segment in after['segments'] for row in segment['contour']))

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_observed_anchor_survives_handoff_completion_and_new_queue_tail(self,_):
        s=self.state();a,b=s['executionPlan']['selections'][:2]
        observed={'from':a['trackId'],'to':b['trackId'],'observedStartBeat':a['exitBeat']+.25}
        s['activeHandoff']=observed
        active=set_timeline(s,{'id':b['trackId'],'beatPosition':b['entryBeat']+8})
        s['transitions']=[observed];s['activeHandoff']=None
        complete=set_timeline(s,{'id':b['trackId'],'beatPosition':b['entryBeat']+8})
        self.assertEqual(active,complete)
        shorter=copy.deepcopy(s);shorter['order']=s['order'][:3];shorter['executionPlan']['selections']=s['executionPlan']['selections'][:3]
        shorter['executionPlan']['transitions']=s['executionPlan']['transitions'][:2]
        if 'tempoSchedule' in shorter['executionPlan']:
            shorter['executionPlan']['tempoSchedule']=s['executionPlan']['tempoSchedule'][:3]
        three=set_timeline(shorter,{'id':b['trackId'],'beatPosition':b['entryBeat']+8})
        self.assertEqual(three['segments'][:2],complete['segments'][:2])
        self.assertEqual(three['positionSeconds'],complete['positionSeconds'])

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_unprepared_or_missing_measurements_do_not_invent_energy(self,_):
        s=self.state();s.pop('startedAt');s['analysis']['tracks'][0]['energyMap']=None
        data=set_timeline(s,{'id':'0','beatPosition':128})
        self.assertEqual(data['positionSeconds'],0);self.assertEqual(data['segments'][0]['contour'],[])
        s['executionPlan']=None;self.assertIsNone(set_timeline(s,{}))

    def ramped_state(self, falling=False):
        s=self.state()
        opening=155
        target=154 if falling else 156
        s['executionPlan']['tempoSchedule']=[
            {'trackId':track['id'],'fromBpm':opening if i==0 else target,
             'toBpm':target,'startOffsetBeats':4 if i==0 else 36,
             'durationBeats':32 if i==0 else 0}
            for i,track in enumerate(s['order'])]
        return s

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_ramp_projection_integrates_tempo_instead_of_using_opening_bpm(self,_):
        s=self.ramped_state();cue=s['executionPlan']['selections'][0]
        data=set_timeline(s,{'id':cue['trackId'],'beatPosition':cue['entryBeat']+20})
        # Independently integrate 16 ramp beats using the midpoint rule.
        steps=10000;step=16/steps
        expected=4*60/155+sum(60/(155+(i+.5)*step/32)*step for i in range(steps))
        self.assertAlmostEqual(data['positionSeconds'],expected,places=8)
        self.assertNotAlmostEqual(data['positionSeconds'],20*60/155,places=4)
        self.assertEqual(data['timingBasis'],'prepared-tempo-route')

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_descending_and_flat_tempo_routes_are_finite_and_positive(self,_):
        for falling in (False,True):
            s=self.ramped_state(falling);cue=s['executionPlan']['selections'][0]
            results=[set_timeline(s,{'id':cue['trackId'],'beatPosition':cue['entryBeat']+b})['positionSeconds']
                     for b in (0,4,20,36,60)]
            self.assertTrue(all(math.isfinite(t) for t in results))
            self.assertEqual(results,sorted(results));self.assertEqual(results[0],0)
            target=154 if falling else 156
            self.assertAlmostEqual(results[-1]-results[-2],24*60/target)

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_tempo_route_handoff_keeps_the_same_clock_from_either_deck(self,_):
        s=self.ramped_state();a,b=s['executionPlan']['selections'][:2]
        observed=a['exitBeat']+.25
        s['activeHandoff']={'from':a['trackId'],'to':b['trackId'],'observedStartBeat':observed,
                            'incomingCueBeat':b['entryBeat']+.1}
        outgoing=set_timeline(s,{'id':a['trackId'],'beatPosition':observed+12})
        incoming=set_timeline(s,{'id':b['trackId'],'beatPosition':b['entryBeat']+.1+12})
        self.assertAlmostEqual(outgoing['positionSeconds'],incoming['positionSeconds'])
        self.assertEqual(outgoing['segments'],incoming['segments'])
        self.assertEqual(incoming['segments'][1]['entryBeat'],b['entryBeat']+.1)

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_loudness_windows_follow_local_tempo_not_uniform_stretch(self,_):
        s=self.ramped_state();cue=s['executionPlan']['selections'][0]
        seconds_per_source_beat=(cue['exitSeconds']-cue['entrySeconds'])/(cue['exitBeat']-cue['entryBeat'])
        energy=s['analysis']['tracks'][0]['energyMap']
        energy['contour']=[[cue['entrySeconds'],4*seconds_per_source_beat,-10],
            [cue['entrySeconds']+40*seconds_per_source_beat,4*seconds_per_source_beat,-12]]
        segment=set_timeline(s,{})['segments'][0]
        first,second=segment['contour']
        self.assertAlmostEqual(first[1],4*60/155)
        self.assertAlmostEqual(second[1],4*60/156)
        self.assertEqual((first[2],second[2]),(-10,-12))

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_display_segments_partition_overlaps_without_changing_audio_intervals(self,_):
        s=self.ramped_state();data=set_timeline(s,{})
        audio=data['segments'];display=data['displaySegments']
        self.assertGreater(audio[0]['endSeconds'],audio[1]['startSeconds'])
        self.assertEqual([v['trackId'] for v in audio],[v['trackId'] for v in display])
        self.assertEqual(display[0]['startSeconds'],audio[0]['startSeconds'])
        self.assertEqual(display[-1]['endSeconds'],audio[-1]['endSeconds'])
        for i,item in enumerate(display):
            self.assertGreater(item['endSeconds'],item['startSeconds'])
            if i:self.assertAlmostEqual(display[i-1]['endSeconds'],item['startSeconds'])
            self.assertTrue(all(item['startSeconds']<=r[0] and r[0]+r[1]<=item['endSeconds']+1e-7
                                for r in item['contour']))
            self.assertEqual(item['timing'],'display-ownership')
        self.assertEqual(s,self.ramped_state())  # Projection must not mutate the engine plan.

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_invalid_or_partial_routes_do_not_publish_plausible_wrong_timing(self,_):
        for change in ('short','identity','nan','zero','negative','discontinuous','instant-change'):
            s=self.ramped_state();rows=s['executionPlan']['tempoSchedule']
            if change=='short':rows.pop()
            elif change=='identity':rows[0]['trackId']='wrong'
            elif change=='nan':rows[0]['toBpm']=float('nan')
            elif change=='zero':rows[0]['fromBpm']=0
            elif change=='negative':rows[0]['durationBeats']=-4
            elif change=='discontinuous':rows[1]['fromBpm']=120
            elif change=='instant-change':rows[0]['durationBeats']=0
            with self.subTest(change=change):self.assertIsNone(set_timeline(s,{}))

    @patch('afterhours.program_timeline.detailed_contour',return_value=())
    def test_bad_cues_and_contours_cannot_crash_the_program_endpoint(self,_):
        for field in ('entryBeat','entrySeconds','exitBeat','maxBlendBeats'):
            s=self.state();s['executionPlan']['selections'][0][field]=None
            with self.subTest(field=field):self.assertIsNone(set_timeline(s,{}))
        s=self.state()
        s['analysis']['tracks'][0]['energyMap']['contour']=[None,[],[0,1,float('nan')],[0,-1,-12]]
        self.assertEqual(set_timeline(s,{})['segments'][0]['contour'],[])


if __name__=='__main__':unittest.main()
