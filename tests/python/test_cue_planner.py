"""Cue and execution boundaries. Synthetic signals and provider responses only."""
import copy
import io
import json
import threading
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch
from afterhours import ai_mix
from afterhours import cue_planner as cp
from afterhours.vdj_session import DJSession


def fixture(count=4,seconds=30):
    span,blend=(128,64) if seconds==60 else (64,32)
    rows=[{'seconds':i*.5,'duration':.5,'rmsDb':-14,'lowDb':-20,'highDb':-24,'crestDb':8} for i in range(800)]
    tracks=[]
    for i in range(count):
        track={'id':str(i),'title':str(i),'artist':'Fixture','bpm':155,'camelot':'2A','genre':'Drum & Bass',
               'duration':400,'points':[],'energyMap':{'coverage':1,'reportDigest':'fixture'}}
        native={'anchorBeat':0,'offsetBeats':64,'entryBeat':64,'entrySeconds':64*60/155,
                'introEndSeconds':(64+blend)*60/155,'exitBeat':64+span,'exitSeconds':(64+span)*60/155,
                'endBeat':64+span+blend,'endSeconds':(64+span+blend)*60/155}
        track['cueCandidates']=[cp.candidate(track,rows,native,{'spanBeats':span,'blendBeats':blend,'medianRmsDb':-14})]
        track['cueCandidates'][0]['replayVerified']=True  # Simulated native result.
        tracks.append(track)
    report=cp.attach_candidates({'tracks':tracks},{t['id']:t['cueCandidates'] for t in tracks},seconds,155)
    response={'summary':'Measured excerpt plan; listening check required.','decision':'propose','missingEvidence':[],
              'selections':[{'trackId':t['id'],'candidateId':t['cueCandidates'][0]['id'],'reason':'Steady measured entrance.'} for t in tracks],
              'transitions':[{'fromId':a['id'],'toId':b['id'],'technique':'restrained-bass-swap','blendBeats':blend,
                              'reason':'Limit midrange overlap; vocals unknown.'} for a,b in zip(tracks,tracks[1:])]}
    return rows,report,response


class CueTests(unittest.TestCase):
    def test_limited_audio_needs_verified_sound_extent_and_native_marker_agreement(self):
        rows,report,_=fixture();t=report['tracks'][0]
        t['points']=[{'label':'realEnd','seconds':30}]
        for row in rows[60:]:row['rmsDb']=-120
        self.assertIn('30 seconds',cp.limited_audio(t,rows))
        t['points'][0]['seconds']=399
        self.assertIsNone(cp.limited_audio(t,rows))
        t['points'][0]['seconds']=30;rows[200]['rmsDb']=-15
        self.assertIsNone(cp.limited_audio(t,rows))
        t['duration']=30
        self.assertIsNone(cp.limited_audio(t,rows))

    def test_quiet_and_unstable_entries_are_excluded(self):
        rows,report,_=fixture();t=report['tracks'][0]
        for row in rows[:80]:row.update(rmsDb=-50,lowDb=-60,highDb=-65)
        p=cp.propose_offsets(t,rows,.2,155,155,30)
        self.assertTrue(p['offsets']);self.assertEqual(p['spanBeats'],64)
        self.assertTrue(all(.2+v*60/155>=40 for v in p['offsets']))
        for row in rows:row.update(rmsDb=-120)
        with self.assertRaisesRegex(ValueError,'no usable signal'):cp.propose_offsets(t,rows,.2,155,155,30)

    def test_timed_cue_policy_keeps_nearby_early_alternatives(self):
        rows, report, _ = fixture()
        track = report['tracks'][0]
        standard = cp.propose_offsets(track, rows, .2, 126, 126, 30)
        timed = cp.propose_offsets(track, rows, .2, 126, 126, 30, limit=10, timed=True)
        self.assertEqual(timed['offsets'][:7], list(range(0, 112, 16)))
        self.assertNotIn(16, standard['offsets'])
        self.assertLessEqual(len(timed['offsets']), 10)
        self.assertEqual(timed['spanBeats'], standard['spanBeats'])

    def test_timed_cue_policy_spreads_recovery_checks_across_a_long_track(self):
        rows, report, _ = fixture()
        track = report['tracks'][0]
        policy = cp.propose_offsets(track, rows, .2, 126, 126, 30, limit=10, timed=True)
        offsets = policy['offsets']
        latest = int(((track['duration']-.7)*126/60-policy['spanBeats']-policy['blendBeats'])//16)*16
        self.assertEqual(offsets[:7], list(range(0, 112, 16)))
        self.assertEqual(offsets[7], offsets[6]+32)
        self.assertEqual(len(offsets), 10)
        self.assertLessEqual(max(b-a for a,b in zip(offsets, offsets[1:]+[latest])), 224)

    def test_native_readbacks_cannot_move_into_silence_or_outside_track(self):
        rows,report,_=fixture();t=report['tracks'][0];native=t['cueCandidates'][0]
        policy={'spanBeats':64,'blendBeats':32,'medianRmsDb':-14}
        for row in rows:row['rmsDb']=-60
        with self.assertRaises(ValueError):cp.candidate(t,rows,native,policy)
        with self.assertRaises(ValueError):cp.window_stats(rows,390,401)

    def test_receipt_digest_changes_require_preparation(self):
        _,report,_=fixture();t=report['tracks'][0]
        with patch('afterhours.cue_planner.read_scan',return_value=({'features':[]},'changed')):
            with self.assertRaisesRegex(ValueError,'changed'):cp.measured_rows(t)

    def test_identity_staleness_and_overlap_guards(self):
        _,report,response=fixture();review=ai_mix.validate_plan(response,report,30)
        plan=cp.compile_plan(review,report,report['tracks'],30)
        self.assertFalse(plan['applied']);self.assertFalse(plan['transitions'][0]['tonalOverlap'])
        mutations=[lambda v:v.update(analysisFingerprint='stale'),
                   lambda v:v['selections'][0].update(candidateId='invented'),
                   lambda v:v['transitions'][0].update(blendBeats=64),
                   lambda v:v['transitions'][0].update(technique='short-handoff'),
                   lambda v:v['selections'].reverse()]
        for mutate in mutations:
            v=copy.deepcopy(review);mutate(v)
            with self.assertRaises(ValueError):cp.compile_plan(v,report,report['tracks'],30)
        with self.assertRaises(ValueError):cp.compile_plan(review,report,report['tracks'],60)
        with self.assertRaises(ValueError):cp.compile_plan(review,report,report['tracks'],30,'quick')

    def test_missed_candidate_has_no_arbitrary_later_fallback(self):
        _,report,_=fixture();cue=report['tracks'][0]['cueCandidates'][0]
        self.assertEqual(cp.execution_window(cue,80,32)['mixAtBeat'],128)
        with self.assertRaisesRegex(ValueError,'missed'):cp.execution_window(cue,126,32)

    def test_measured_music_fit_controls_blend_length(self):
        for bass_gap, bright, expected in ((0,False,32),(0,True,16),(10,False,8)):
            with self.subTest(bass_gap=bass_gap,bright=bright):
                _,report,_=fixture(2,60)
                order=report['tracks']
                order[1]['cueCandidates'][0]['entry']['bassDb']+=bass_gap
                for cue in (order[0]['cueCandidates'][0]['exit'], order[1]['cueCandidates'][0]['entry']):
                    cue['highDb']=cue['rmsDb']-(8 if bright else 24)
                snapshot={'analysis':report,'mixStyle':'phrase','secondsPerTrack':60,
                          'executionPlan':{'orderIds':[],'targetBpm':155},
                          'aiReview':{'decision':'propose','secondsBetweenEntries':60,'selections':[],'transitions':[]}}
                result=cp.complete_background_queue(snapshot,order,[],'Measured fit fixture.',0)
                recipe=result['executionPlan']['transitions'][0]
                self.assertEqual(recipe['fadeBeats'],expected)
                self.assertFalse(recipe['tonalOverlap'])

    def test_rolling_cue_fit_preserves_a_full_minute_when_a_shorter_cue_has_more_runway(self):
        rows, report, _ = fixture(2,60)
        order = report['tracks']
        report['targetBpm'] = 128
        for track in order:
            track['bpm'] = 128

        def measured(entry, span, blend):
            return {**cp.candidate(order[1], rows, {
                'anchorBeat':0, 'offsetBeats':entry, 'entryBeat':entry, 'entrySeconds':entry*60/128,
                'introEndSeconds':(entry+blend)*60/128,
                'exitBeat':entry+span, 'exitSeconds':(entry+span)*60/128,
                'endBeat':entry+span+blend, 'endSeconds':(entry+span+blend)*60/128,
            }, {'spanBeats':span,'blendBeats':blend,'medianRmsDb':-14}), 'replayVerified':True}

        short, full = measured(0,64,32), measured(131,128,64)
        order[1]['cueCandidates'] = [short, full]
        snapshot = {'analysis':report,'mixStyle':'phrase','secondsPerTrack':60,'entryCadence':[30,60],
                    'rollingQueue':{'status':'preparing'},'executionPlan':{'orderIds':[],'targetBpm':128},
                    'aiReview':{'decision':'propose','secondsBetweenEntries':60,'selections':[],'transitions':[]}}
        result = cp.complete_background_queue(snapshot,order,[],'Measured fit fixture.',0)
        self.assertEqual(result['executionPlan']['selections'][1]['id'],full['id'])

    def test_rolling_cue_fit_prefers_a_real_recovery_bar_over_a_distant_outro(self):
        rows, report, _ = fixture(2)
        order = report['tracks']
        report['targetBpm'] = 125
        for track in order:
            track['bpm'] = 125
        measured = []
        for offset in (0,16,32,48,64,80,96,400):
            start = offset + 64
            native = {'anchorBeat':0, 'offsetBeats':offset, 'entryBeat':offset,
                      'entrySeconds':offset*60/125+.2, 'introEndSeconds':(offset+32)*60/125+.2,
                      'exitBeat':start, 'exitSeconds':start*60/125+.2,
                      'endBeat':start+32, 'endSeconds':(start+32)*60/125+.2}
            cue = cp.candidate(order[0], rows, native,
                               {'spanBeats':64,'blendBeats':32,'medianRmsDb':-14})
            cue['replayVerified'] = True
            cue['entry']['levelVariationDb'] = 0 if offset == 80 else 1
            measured.append(cue)
        order[0]['cueCandidates'] = measured
        self.assertTrue(cp.has_recovery_bar(0, measured, 62.5, 125))
        self.assertFalse(cp.has_recovery_bar(80, measured, 62.5, 125))
        for rolling, expected_entry in ((False,80), (True,0)):
            with self.subTest(rolling=rolling):
                snapshot = {'analysis':report, 'mixStyle':'phrase', 'secondsPerTrack':30,
                            'entryCadence':[30,60], 'rollingQueue':rolling,
                            'executionPlan':{'orderIds':[], 'targetBpm':125},
                            'aiReview':{'decision':'propose','secondsBetweenEntries':30,
                                        'selections':[],'transitions':[]}}
                result = cp.complete_background_queue(snapshot, order, [], 'Fit recovery around cadence.', 0)
                self.assertEqual(result['executionPlan']['selections'][0]['entryBeat'], expected_entry)

    def test_timed_rhythm_windows_allow_four_bars_only_with_matching_signal(self):
        for loops, bright, stem_overlap, bass_gap, expected in (
            (True, False, 0, 0, 16), (False, False, 0, 0, 8),
            (True, True, 0, 0, 8), (True, False, .7, 0, 8),
            (True, False, 0, 8, 8),
        ):
            with self.subTest(loops=loops, bright=bright, stem_overlap=stem_overlap, bass_gap=bass_gap):
                _, report, _ = fixture(2, 60)
                order = report['tracks']
                order[0].update(genre='Dance', camelot='2A')
                order[1].update(genre='Dance', camelot='7B')
                exits, entry = (track['cueCandidates'][0] for track in order)
                exits['loopBridgeBeats'] = 8
                entry['loopBridgeBeats'] = 8 if loops else None
                entry['entry']['bassDb'] += bass_gap
                for window in (exits['exit'], entry['entry']):
                    window['highDb'] = window['rmsDb'] - (8 if bright else 24)
                snapshot = {'analysis':report, 'mixStyle':'phrase', 'secondsPerTrack':60, 'entryCadence':[60, 30],
                            'executionPlan':{'orderIds':[], 'targetBpm':155},
                            'aiReview':{'decision':'propose', 'secondsBetweenEntries':60, 'selections':[], 'transitions':[]}}
                stems = {'scoreAdjustment':0, 'vocalOverlapFraction':stem_overlap,'lowEndOverlap':0}
                with patch.object(cp, 'cue_pair', return_value=stems):
                    result = cp.complete_background_queue(snapshot, order, [], 'Measured fit fixture.', 0)
                recipe = result['executionPlan']['transitions'][0]
                self.assertEqual(recipe['fadeBeats'], expected)
                self.assertFalse(recipe['tonalOverlap'])
                self.assertEqual('four-bar single-lead blend' in recipe['reason'], expected == 16)

    def test_loop_bridge_rejects_a_level_or_bass_jump_at_the_exit(self):
        rows=[{'seconds':.5*i,'duration':.5,'rmsDb':-15,'lowDb':-19,
               'highDb':-26,'crestDb':8} for i in range(16)]
        cue={'exitSeconds':0,'endSeconds':8,'exitBeat':128,'endBeat':144}
        self.assertEqual(cp.loop_bridge_beats(rows,cue),8)
        for row in rows[1:]:
            row['lowDb']=-30
        self.assertIsNone(cp.loop_bridge_beats(rows,cue))

    def test_compatible_keys_do_not_override_unknown_lead_guard_or_rewrite_ai_response(self):
        _,report,response=fixture();response['transitions'][0]['technique']='bass-swap'
        review=ai_mix.validate_plan(response,report,30);original=copy.deepcopy(review)
        plan=cp.compile_plan(review,report,report['tracks'],30);recipe=plan['transitions'][0]
        self.assertEqual(review,original)
        self.assertEqual(recipe['modelReason'],review['transitions'][0]['reason'])
        self.assertEqual(recipe['leadPolicy'],'single-lead-v1')
        self.assertFalse(recipe['tonalOverlap']);self.assertIn('One lead at a time',recipe['executionAdjustment'])

    def test_hold_has_no_executable_cues(self):
        _,report,response=fixture();response.update(decision='hold',missingEvidence=['Poor cue coverage.'])
        with self.assertRaises(ValueError):ai_mix.validate_plan(response,report,30)
        response.update(selections=[],transitions=[])
        self.assertEqual(ai_mix.validate_plan(response,report,30)['decision'],'hold')

    def test_one_bounded_provider_request_and_no_implicit_application(self):
        _,report,response=fixture()
        opener=Mock(return_value=io.BytesIO(json.dumps({'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(response)}]}]}).encode()))
        with patch('afterhours.ai_mix.api_key',return_value='sk-fixture-only') as key:
            with self.assertRaises(ValueError):ai_mix.plan(report,30,opener=opener)
            key.assert_not_called();opener.assert_not_called()
            result=ai_mix.plan(report,30,approved=True,opener=opener)
        opener.assert_called_once();self.assertFalse(result['appliedToPlayback'])
        body=json.loads(opener.call_args.args[0].data)
        self.assertFalse(body['store']);self.assertLess(len(opener.call_args.args[0].data),ai_mix.MAX_BODY)
        self.assertFalse(json.loads(body['input'])['audioIncluded'])
        # A mistyped hash or another track's cue must be impossible under the
        # schema sent to the model, not merely caught after a billed response.
        schema=body['text']['format']['schema']['properties']
        selection_options=schema['selections']['items']['anyOf']
        transition_options=schema['transitions']['items']['anyOf']
        def allowed(row,options,fields):
            return any(all(row[field] in option['properties'][field]['enum'] for field in fields) for option in options)
        for row in response['selections']:
            self.assertTrue(allowed(row,selection_options,('trackId','candidateId')))
        bad=copy.deepcopy(response['selections'][0]);bad['candidateId']=response['selections'][1]['candidateId']
        self.assertFalse(allowed(bad,selection_options,('trackId','candidateId')))
        for row in response['transitions']:
            self.assertTrue(allowed(row,transition_options,('fromId','toId')))
        for to_id in ('mistyped-recording',report['tracks'][2]['id']):
            bad={**response['transitions'][0],'toId':to_id}
            self.assertFalse(allowed(bad,transition_options,('fromId','toId')))
        self.assertEqual(json.loads(body['input'])['transitionPairs'],[
            {'fromId':a['id'],'toId':b['id']} for a,b in zip(report['tracks'],report['tracks'][1:])])

    def test_provider_failure_or_invalid_identity_does_not_retry(self):
        _,report,response=fixture()
        response['transitions'][0]['toId']='not-in-this-set'
        bad=json.dumps({'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(response)}]}]}).encode()
        openers=[Mock(return_value=io.BytesIO(bad)),
                 Mock(side_effect=HTTPError('private',429,'',{},io.BytesIO(b'{"error":{"code":"credit_balance_exhausted"}}')))]
        for opener in openers:
            with patch('afterhours.ai_mix.api_key',return_value='sk-fixture-only'),self.assertRaises(ValueError):
                ai_mix.plan(report,30,approved=True,opener=opener)
            opener.assert_called_once()

    def test_missing_cue_schema_is_rejected_before_key_or_network(self):
        _,report,_=fixture();report['tracks'][0]['cueCandidates']=[]
        with patch('afterhours.ai_mix.api_key') as key:
            opener=Mock()
            with self.assertRaisesRegex(ValueError,'no request was sent'):
                ai_mix.plan(report,30,approved=True,opener=opener)
            key.assert_not_called();opener.assert_not_called()

    def test_unapplied_or_changed_plan_cannot_start_transport(self):
        _,report,response=fixture();review=ai_mix.validate_plan(response,report,30)
        s=DJSession(threading.Lock());s.update(candidates=report['tracks'],order=report['tracks'],phase='ready',analysis=report,aiReview=review)
        with patch.object(s,'_launch') as launch:
            with self.assertRaisesRegex(ValueError,'Apply'):s.start()
            p=cp.compile_plan(review,report,report['tracks'],30);p.update(applied=True,id='changed');s.update(executionPlan=p)
            with self.assertRaisesRegex(ValueError,'changed'):s.start()
            launch.assert_not_called()

    def test_apply_requires_current_receipt_and_never_starts_audio(self):
        _,report,response=fixture();review=ai_mix.validate_plan(response,report,30)
        s=DJSession(threading.Lock());s.update(candidates=report['tracks'],order=report['tracks'],analysis=report,aiReview=review)
        peer=Mock();peer.status.return_value={'automix':False,'decks':[{'playing':False},{'playing':False}]}
        with patch.object(s,'_launch',side_effect=lambda f,p:f(peer)),patch.object(s,'prepare_deck') as prepare,patch.object(s,'send') as send,patch.object(s,'telemetry'),patch('afterhours.cue_planner.verify_receipts') as verify:
            s.apply_plan();verify.assert_called_once();self.assertEqual(prepare.call_count,2)
            send.assert_called_once_with(peer,'crossfader 0%')
            self.assertTrue(s.snapshot()['executionPlan']['applied'])
            verify.side_effect=ValueError('changed');prepare.reset_mock();send.reset_mock()
            with self.assertRaises(ValueError):s.apply_plan()
            prepare.assert_not_called();send.assert_not_called()

if __name__=='__main__':unittest.main()
