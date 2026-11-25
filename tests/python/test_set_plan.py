"""Tonight planning with actual local metadata and synthetic scan receipts."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from afterhours import dj_settings
from afterhours import cue_planner
from afterhours import set_plan
from afterhours.dj_agent import DJAgent,validate_decision,music_context,planning_request,MAX_REQUEST_BYTES
from afterhours.record_crate import RecordCrate
from afterhours.vdj_session import DJSession
from test_dj_agent import decision
from test_recording_availability import records
from test_provider_eligibility import restrict,decoded_proof


def plan():
    return {'startAt':2000,'endAt':16400,'peakAt':9800,'warm':True,'onePeak':True,'darkLate':False}


def prepared_report(tracks):
    return {'cueVersion':cue_planner.VERSION,'secondsBetweenEntries':30,'tracks':tracks,
            'fingerprint':cue_planner.fingerprint(tracks)}


def replayed_cue():
    return {'id':'cue','replayVerified':True,'entryBeat':8,'exitBeat':72,'entrySeconds':4,
            'introEndSeconds':12,'exitSeconds':36,'endSeconds':44,'recordingDuration':335}


class SetPlanTests(unittest.TestCase):
    def make(self,path=None,crate=None):
        native=Mock(side_effect=AssertionError('Native connection'));s=DJSession(threading.Lock(),client_factory=native,crate=crate)
        tracks=records();s.update(candidates=tracks,order=tracks[:3],phase='ready')
        provider=Mock(side_effect=AssertionError('Provider request'));a=DJAgent(s,path,evaluator=provider,clock=lambda:1000)
        return s,a,provider,native
    def change(self,a,kind,value,request_id='change',expected=None):
        return a.settings_action(request_id,{'kind':kind,'value':value},expected or a.snapshot()['settings']['expected'])

    def test_missing_schedule_shows_only_chosen_shortlist_and_no_invented_preparation(self):
        s,a,p,n=self.make();s.state['candidates'][0].update(engineChecked=True,duration=335)
        result=a.snapshot()['setPlan']
        self.assertNotIn('schedule',result);self.assertNotIn('name',result);self.assertNotIn('venue',result)
        self.assertEqual(result['phases'],[]);self.assertEqual(result['energySamples'],[])
        self.assertEqual(result['preparation'],{'shortlistCount':3,'preparedCount':0,'status':'idle'})
        self.assertEqual([r['id'] for r in result['shortlist']],[r['id'] for r in s.snapshot()['order']])
        p.assert_not_called();n.assert_not_called()

    def test_warm_length_peak_and_energy_use_real_schedule_without_mutating_audio(self):
        s,a,p,n=self.make();before=copy.deepcopy(s.snapshot()['order'])
        initial=self.change(a,'plan',plan())['setPlan']
        self.assertNotIn('warmLen',initial['schedule']);self.assertEqual(initial['phases'],[])
        self.assertGreater(len(initial['energySamples']),2)
        warm=self.change(a,'warmLength',3600,'warm')['setPlan']
        self.assertEqual(warm['schedule']['warmLen'],3600)
        self.assertEqual([(x['start'],x['end']) for x in warm['phases']],[(0,3600),(3600,6600),(6600,9600),(9600,14400)])
        self.assertTrue(all(not row['records'] for row in warm['phases']))
        self.assertTrue(all('bpmText' not in row for row in warm['phases']))
        shifted=self.change(a,'peakShift',900,'peak')['setPlan']
        self.assertEqual(shifted['schedule']['peakAt'],10700)
        self.assertNotEqual(warm['energySamples'],shifted['energySamples'])
        self.assertEqual(s.snapshot()['order'],before);self.assertIsNone(s.snapshot()['executionPlan'])
        p.assert_not_called();n.assert_not_called()

    def test_invalid_warm_and_outside_peak_are_atomic(self):
        s,a,_,_=self.make();self.change(a,'plan',plan());self.change(a,'warmLength',3600,'warm')
        before=copy.deepcopy(a.settings.state)
        for kind,value in [('warmLength',True),('warmLength',0),('warmLength',7000),('warmLength',60.5),('peakShift',-3600),('peakShift',7200)]:
            with self.subTest(kind=kind,value=value),self.assertRaises(ValueError):self.change(a,kind,value,'bad')
            self.assertEqual(a.settings.state,before)

    def test_twelve_exact_pins_and_receipts_are_bounded_idempotent_and_context_checked(self):
        s,a,p,n=self.make();tid=records()[3]['id'];expected=a.snapshot()['settings']['expected']
        first=self.change(a,'pin',{'trackId':tid,'on':True},'pin',expected)
        self.assertEqual(first['setPlan']['pins'],[{'trackId':tid}])
        self.assertIn(tid,[r['id'] for r in first['setPlan']['shortlist']])
        self.assertEqual(first['setPlan']['expected'],first['settings']['expected'])
        second=self.change(a,'brief','House all night','brief')
        replay=self.change(a,'pin',{'trackId':tid,'on':True},'pin',expected)
        self.assertEqual(replay['settings']['receipt'],first['settings']['receipt'])
        self.assertEqual(replay['setPlan']['expected'],second['settings']['expected'])
        with self.assertRaises(ValueError):self.change(a,'pin',{'trackId':tid,'on':False},'stale',expected)
        self.assertEqual(a.settings.state['pins'],[tid]);self.assertEqual(s.snapshot()['setlist']['lockedIds'],[])
        self.change(a,'pin',{'trackId':tid,'on':False},'unpin')
        self.assertEqual(a.snapshot()['setPlan']['pins'],[]);p.assert_not_called();n.assert_not_called()

    def test_archived_pins_survive_working_pool_rotation_without_increasing_context(self):
        s,a,_,_=self.make()
        tracks=[{**records()[0],'id':'virtualdj:'+format(i+100,'064x'),'title':f'Archive {i}',
                 'engine':{'selection_id':format(i+100,'064x'),'folder_id':'f'*64}} for i in range(100)]
        for t in tracks:s.remember_record(t)
        for i,t in enumerate(tracks[:12]):self.change(a,'pin',{'trackId':t['id'],'on':True},f'pin{i}')
        with self.assertRaisesRegex(ValueError,'12'):self.change(a,'pin',{'trackId':tracks[12]['id'],'on':True},'too-many')
        self.assertEqual(len(s.snapshot()['candidates']),48)
        context=music_context(s.snapshot());self.assertEqual(len(context['catalog']),32)
        self.assertTrue(set(a.settings.state['pins'])<={r['id'] for r in context['catalog']})
        encoded,packet=planning_request({'music':context,'settings':a.settings.planner_context(s.snapshot(),1000),'messages':[],
            'searchPolicy':{'canSearch':False}})
        self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
        self.assertTrue(set(a.settings.state['pins'])<={r['id'] for r in packet['music']['catalog']})

    def test_pins_cannot_be_silently_omitted_but_can_be_explicitly_deferred(self):
        s,a,_,_=self.make();ids=[t['id'] for t in records()]
        self.change(a,'pin',{'trackId':ids[3],'on':True})
        with self.assertRaisesRegex(ValueError,'pinned'):validate_decision(decision(ids[:3]),s.snapshot())
        result=validate_decision({**decision(ids[:3]),'deferredTracks':[{'trackId':ids[3],'reason':'Needs a later tempo route.'}]},s.snapshot())
        self.assertEqual(result['deferredTracks'][0]['trackId'],ids[3])
        self.assertEqual(validate_decision(decision(ids),s.snapshot())['trackIds'],ids)
        self.assertEqual(validate_decision(decision(),s.snapshot())['trackIds'],[])

    def test_final_completed_pin_stays_fulfilled_after_replacement_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);crate=RecordCrate(root/'crate.db');s,a,p,n=self.make(root/'agent.json',crate)
            ids=[t['id'] for t in records()];self.change(a,'pin',{'trackId':ids[2],'on':True})
            s.update(phase='playing',index=2,active=False)
            with self.assertRaisesRegex(ValueError,'pinned'):validate_decision(decision([ids[0],ids[1],ids[3]]),s.snapshot())
            s.update(phase='completed',index=2,active=False)
            self.assertEqual(validate_decision(decision([ids[0],ids[1],ids[3]]),s.snapshot())['trackIds'],[ids[0],ids[1],ids[3]])
            a.tick();s.plan([ids[0],ids[1],ids[3]])
            self.assertEqual(validate_decision(decision([ids[0],ids[1],ids[3]]),s.snapshot())['trackIds'],[ids[0],ids[1],ids[3]])
            self.assertEqual(a.settings.state['playedPins'],[ids[2]])
            self.assertEqual(music_context(s.snapshot())['fulfilledPinnedIds'],[ids[2]])
            restored_session=DJSession(threading.Lock(),crate=crate,client_factory=n)
            restored=DJAgent(restored_session,root/'agent.json',p,clock=lambda:1000);restored.restore_settings()
            self.assertEqual(restored_session.snapshot()['setPlayedPins'],[ids[2]])
            self.change(restored,'pin',{'trackId':ids[2],'on':False},'unpin')
            self.assertEqual(restored.settings.state['playedPins'],[])
            p.assert_not_called();n.assert_not_called()

    def test_pins_never_override_eligibility_protected_pair_or_positional_locks(self):
        s,a,_,_=self.make();ids=[t['id'] for t in records()]
        restrict(s.availability,ids[3])
        with self.assertRaisesRegex(ValueError,'excluded'):self.change(a,'pin',{'trackId':ids[3],'on':True})
        self.assertEqual(a.settings.state['pins'],[])
        self.change(a,'pin',{'trackId':ids[2],'on':True},'valid')
        s.update(active=True,phase='playing',index=0,telemetry={'decks':[{'playing':True}]})
        with self.assertRaisesRegex(ValueError,'playing and cued'):validate_decision(decision([ids[0],ids[2],ids[1]]),s.snapshot())
        s.state['setlist']['lockedIds']=[ids[2]]
        with self.assertRaisesRegex(ValueError,'locked'):validate_decision(decision(ids[:2]),s.snapshot())
        restrict(s.availability,ids[2])
        with self.assertRaisesRegex(ValueError,'unavailable'):validate_decision(decision(ids[:3]),s.snapshot())

    def test_later_pin_restriction_stays_visible_for_explicit_deferral(self):
        s,a,_,_=self.make();tid=records()[3]['id']
        self.change(a,'pin',{'trackId':tid,'on':True});restrict(s.availability,tid)
        context=music_context(s.snapshot())
        row=next(t for t in context['catalog'] if t['id']==tid)
        self.assertFalse(row['selectionEligible'])
        body,_=planning_request({'music':context})
        schema=json.loads(body)['text']['format']['schema']
        self.assertIn(tid,schema['$defs']['recordingId']['enum'])
        self.assertNotIn(tid,schema['$defs']['selectableId']['enum'])
        self.assertEqual(context['unavailableRecordings'][0]['trackId'],tid)
        self.assertIn(tid,[t['id'] for t in a.snapshot()['setPlan']['shortlist']])
        result=validate_decision({**decision([t['id'] for t in records()[:3]]),
            'deferredTracks':[{'trackId':tid,'reason':'This exact provider variant is preview only.'}]},s.snapshot())
        self.assertEqual(result['deferredTracks'][0]['trackId'],tid)
        with self.assertRaisesRegex(ValueError,'currently unavailable'):
            validate_decision(decision([records()[0]['id'],records()[1]['id'],tid]),s.snapshot())

    def test_typed_and_voice_set_commands_are_local_and_ambiguous_names_do_not_mutate(self):
        for source in ('typed','voice'):
            s,a,p,n=self.make();self.change(a,'plan',plan())
            a.message('peak later',source=source,message_id='later',surface='set')
            self.assertEqual(a.settings.state['plan']['peakAt'],10700)
            a.message('warm-up 45 minutes',source=source,message_id='warm',surface='set')
            self.assertEqual(a.settings.state['plan']['warmLen'],2700)
            a.message('pin Track d',source=source,message_id='pin',surface='set')
            self.assertEqual(a.settings.state['pins'],[records()[3]['id']])
            before=copy.deepcopy(a.settings.state)
            for i,text in enumerate(('pin Fixture artist','can you explain peak later?','do not pin Track c','Track d by Fixture artist','start playing','peak later?')):
                a.message(text,source=source,message_id=f'none{i}',surface='set',approved=True)
                self.assertEqual(a.settings.state,before)
            p.assert_not_called();n.assert_not_called();self.assertEqual(a.remaining,0)

    def test_pin_and_warm_length_survive_restart_but_fresh_set_clears_tonight(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);crate=RecordCrate(root/'crate.db');s,a,p,n=self.make(root/'agent.json',crate)
            self.change(a,'plan',plan());self.change(a,'warmLength',3600,'warm')
            tid=records()[3]['id'];self.change(a,'pin',{'trackId':tid,'on':True},'pin')
            restored_session=DJSession(threading.Lock(),crate=crate,client_factory=n)
            restored=DJAgent(restored_session,root/'agent.json',p,clock=lambda:1000);restored.restore_settings()
            self.assertEqual(restored.snapshot()['setPlan']['schedule']['warmLen'],3600)
            self.assertEqual(restored_session.snapshot()['setPins'],[tid]);self.assertEqual(restored.remaining,0)
            restored.fresh();self.assertEqual(restored.settings.state['pins'],[]);self.assertIsNone(restored.settings.state['plan'])
            p.assert_not_called();n.assert_not_called()

    def test_prepared_counts_use_actual_current_receipts_and_invalidate_changed_audio(self):
        s,a,_,_=self.make();track=records()[0]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);receipt=decoded_proof(root,track['id'])
            evidence={**track,'duration':335,'cueCandidates':[replayed_cue()],
                      'energyMap':{'coverage':1,'reportDigest':receipt['reportDigest']}}
            s.update(analysis=prepared_report([evidence]))
            a.set_projection=set_plan.Projection(root)
            self.assertEqual(a.snapshot()['setPlan']['preparation']['preparedCount'],1)
            with patch('afterhours.set_plan.verified_energy',side_effect=AssertionError('Unchanged proof read again')):
                self.assertEqual(a.snapshot()['setPlan']['preparation']['preparedCount'],1)
            path=root/(track['engine']['selection_id']+'.json');data=json.loads(path.read_text());data['features'][0]['rmsDb']=-19;path.write_text(json.dumps(data))
            self.assertEqual(a.snapshot()['setPlan']['preparation']['preparedCount'],0)
            decoded_proof(root,track['id']);restrict(s.availability,track['id'])
            self.assertEqual(a.snapshot()['setPlan']['preparation']['preparedCount'],0)

    def test_flags_alone_invalid_cues_or_changed_report_are_not_preparation(self):
        s,a,_,_=self.make();track=records()[0]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);receipt=decoded_proof(root,track['id']);a.set_projection=set_plan.Projection(root)
            evidence={**track,'duration':335,'cueCandidates':[{'replayVerified':True}],
                      'energyMap':{'coverage':1,'reportDigest':receipt['reportDigest']}}
            s.update(analysis=prepared_report([evidence]))
            self.assertEqual(a.snapshot()['setPlan']['preparation']['preparedCount'],0)
            for change in ({'endSeconds':336},{'recordingDuration':30},{'entryBeat':True}):
                evidence['cueCandidates']=[{**replayed_cue(),**change}]
                s.update(analysis=prepared_report([evidence]))
                self.assertEqual(a.snapshot()['setPlan']['preparation']['preparedCount'],0)
            evidence['cueCandidates']=[replayed_cue()];report=prepared_report([evidence])
            report['tracks'][0]['cueCandidates'][0]['entryBeat']=12
            s.update(analysis=report)
            self.assertEqual(a.snapshot()['setPlan']['preparation']['preparedCount'],0)

    def test_prepared_execution_offsets_are_explicit_not_guessed_from_catalog_duration(self):
        s,a,_,_=self.make();self.change(a,'plan',plan());self.change(a,'warmLength',3600,'warm')
        tracks=records()[:3];ids=[t['id'] for t in tracks]
        evidence=[{**t,'duration':335,'cueCandidates':[{'replayVerified':True}],'energyMap':{'coverage':1}} for t in tracks]
        execution={'targetBpm':120,'selections':[{'trackId':tid,'entryBeat':0,'exitBeat':64} for tid in ids]}
        s.update(analysis=prepared_report(evidence),executionPlan=execution)
        with patch.object(a.set_projection.prepared,'valid',return_value=True):
            projected=a.snapshot()['setPlan']
            self.assertEqual([r['plannedOffset'] for r in projected['shortlist']],[0,32,64])
            self.assertEqual([r['id'] for r in projected['phases'][0]['records']],ids)
            s.update(startedAt=2100,active=True,index=0,phase='playing')
            self.assertEqual(a.snapshot()['setPlan']['shortlist'][0]['plannedOffset'],100)
            s.state['copilot']['held']=True
            self.assertEqual([r.get('plannedOffset') for r in a.snapshot()['setPlan']['shortlist']],[100,None,None])

    def test_fast_opening_and_mismatched_tempo_route_do_not_invent_early_placements(self):
        s,a,_,_=self.make();tracks=records()[:3];ids=[t['id'] for t in tracks]
        execution={'targetBpm':120,'selections':[{'trackId':tid,'entryBeat':64,'exitBeat':128} for tid in ids]}
        snapshot=s.snapshot();snapshot.update(startedAt=2100,startup={'status':'ready','openingId':ids[0],'entryBeat':0})
        snapshot.update(active=True,phase='playing',index=0,telemetry={'sampledAt':2120,'decks':[
            {'deck':1,'id':ids[0],'loaded':True,'playing':True,'beatPosition':40,'bpm':120,'sampledAt':2120}]})
        offsets=set_plan.execution_offsets(snapshot,plan(),tracks,execution,set(ids),2120)
        self.assertEqual([offsets[tid] for tid in ids],[100,164,196])
        bad={**execution,'tempoSchedule':[{'trackId':tid} for tid in reversed(ids)]}
        self.assertEqual(set_plan.execution_offsets(snapshot,plan(),tracks,bad,set(ids),1000),{})

    def test_resumed_future_is_anchored_to_fresh_exact_clock_or_left_unplaced(self):
        s,a,_,_=self.make();tracks=records()[:3];ids=[t['id'] for t in tracks]
        execution={'targetBpm':120,'selections':[{'trackId':tid,'entryBeat':64,'exitBeat':128} for tid in ids]}
        deck={'deck':1,'id':ids[0],'loaded':True,'playing':True,'beatPosition':100,'bpm':120,'sampledAt':5000}
        snapshot=s.snapshot();snapshot.update(startedAt=2100,index=0,active=True,phase='playing',
            timing={'mixAtBeat':128},startup={'status':'ready','openingId':ids[0],'entryBeat':0},
            telemetry={'sampledAt':5000,'decks':[deck]})
        offsets=set_plan.execution_offsets(snapshot,plan(),tracks,execution,set(ids),5000)
        self.assertEqual([offsets[tid] for tid in ids],[100,3014,3046])
        self.assertGreater(plan()['startAt']+offsets[ids[1]],5000)
        for changed in ({'sampledAt':4996},{'sampledAt':5004},{'id':ids[2]},
                        {'playing':False},{'loaded':False},{'bpm':140},{'beatPosition':129}):
            invalid={**snapshot,'telemetry':{'sampledAt':5000,'decks':[{**deck,**changed}]}}
            self.assertEqual(set_plan.execution_offsets(invalid,plan(),tracks,execution,set(ids),5000),{ids[0]:100})
        invalid={**snapshot,'telemetry':None}
        self.assertEqual(set_plan.execution_offsets(invalid,plan(),tracks,execution,set(ids),5000),{ids[0]:100})

    def test_large_projection_keeps_pins_accessible_and_counts_all_prepared_records(self):
        s,a,_,_=self.make()
        tracks=[{**records()[0],'id':'virtualdj:'+format(i+200,'064x'),'title':f'Projection {i}'} for i in range(71)]
        # Defensive projection stress input; the public queue API remains<=12.
        s.state.update(order=tracks[:70],candidates=tracks,analysis=prepared_report(tracks))
        a.settings.state['pins']=[tracks[-1]['id']]
        with patch.object(a.set_projection.prepared,'valid',return_value=True):result=a.snapshot()['setPlan']
        self.assertEqual(len(result['shortlist']),64)
        self.assertIn(tracks[-1]['id'],[t['id'] for t in result['shortlist']])
        self.assertEqual(result['preparation']['shortlistCount'],71)
        self.assertEqual(result['preparation']['preparedCount'],71)

    def test_large_defensive_preparation_read_cache_does_not_thrash(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);validator=set_plan.PreparedEvidence(root)
            tracks=[{**records()[0],'id':'virtualdj:'+format(i+200,'064x'),'duration':335,
                'cueCandidates':[replayed_cue()],'energyMap':{'coverage':1,'reportDigest':'synthetic-cache-digest'}} for i in range(71)]
            for track in tracks:
                for suffix in ('.json','.verified.json'):(root/(track['id'].removeprefix('virtualdj:')+suffix)).write_text('{}')
            # Receipt validation is independently tested above; isolate cache behavior.
            with patch('afterhours.set_plan.verified_energy',return_value={'reportDigest':'synthetic-cache-digest'}) as read:
                for track in tracks:self.assertTrue(validator.valid(track))
                for track in tracks:self.assertTrue(validator.valid(track))
                self.assertEqual(read.call_count,71)

    def test_projection_energy_and_rows_remain_bounded_for_long_nights(self):
        s,a,_,_=self.make();long={**plan(),'endAt':88400,'peakAt':50000}
        self.change(a,'plan',long);self.change(a,'warmLength',10000,'warm')
        result=a.snapshot()['setPlan'];self.assertLessEqual(len(result['energySamples']),512)
        self.assertEqual(result['energySamples'][0]['offset'],0);self.assertEqual(result['energySamples'][-1]['offset'],86400)
        self.assertTrue(all(0<=r['energy']<=5 for r in result['energySamples']))
        self.assertEqual(result['phases'][-1]['end'],86400)


if __name__=='__main__':unittest.main()
