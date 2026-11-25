"""Explicit provider evidence, isolated metadata and fake decks only."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from afterhours.audio_analysis import validate_scan,verified_energy
from afterhours.background_analysis import BackgroundAnalysis
from afterhours.preparation_pool import PreparationPool
from afterhours.recording_availability import RecordingAvailability,ProviderPreviewRestricted,PREVIEW_ONLY
from afterhours.vdj_session import DJSession
from afterhours.dj_agent import DJAgent,music_context
from test_dj_agent import decision,settled
from test_recording_availability import records

EVIDENCE={'source':'native-dialog','reference':'exact-record-provider-dialog.png','sha256':'e'*64}


def restrict(availability,tid,at=None):
    availability.record_preview_only(tid,'Fixture provider',30,EVIDENCE,at)


def decoded_proof(root,tid,*,tail=True,at=950):
    sid=tid.removeprefix('virtualdj:');duration=335
    data={'version':2,'recordingId':sid,'scanStartedAtMs':at*1000,'sampleRate':44100,
          'duration':duration,'totalFrames':duration*44100,'coveredFrames':duration*44100,
          'bufferReadComplete':True,'audioSaved':False,
          'features':[{'seconds':n*.5,'duration':.5,'rmsDb':-20 if tail or n<60 else -120,
                       'lowDb':-25,'highDb':-30,'crestDb':10} for n in range(duration*2)]}
    raw=json.dumps(data).encode();(root/(sid+'.json')).write_bytes(raw)
    sound_end=334.8 if tail else 30
    receipt={'version':1,'recordingId':sid,'reportDigest':hashlib.sha256(raw).hexdigest(),
        'verifiedAt':at,'audioSaved':False,'method':'two-stable-scans-and-native-sound-end',
        'passes':2,'stableForSeconds':1.2,'soundEndSeconds':sound_end,**validate_scan(data,sound_end)}
    (root/(sid+'.verified.json')).write_text(json.dumps(receipt))
    return receipt


class ProviderEligibilityTests(unittest.TestCase):
    def test_durable_exact_restriction_survives_restart_expiry_search_and_metadata(self):
        now=[100.];tid=records()[0]['id']
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'availability.json';a=RecordingAvailability(path,lambda:now[0]);restrict(a,tid)
            a.verified(tid);a.found_again(tid);a.record(tid,'recording-unavailable','Search absence')
            now[0]+=365*86400;a=RecordingAvailability(path,lambda:now[0])
            self.assertEqual(a.blocked(tid)['code'],PREVIEW_ONLY)
            self.assertIsNone(a.blocked(records()[1]['id']))
            self.assertIsNone(a.blocked(tid)['retryAfter'])
            self.assertFalse(a.blocked(tid)['playback_verified'])
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            s=DJSession(threading.Lock(),availability=a);track={**records()[0],'duration':335,'engineChecked':True,'playback_verified':True}
            s.update(candidates=[track],order=[track]);s.remember_record(track,source='analysis',analysis='analyzed')
            self.assertFalse(s.crate.get(tid)['playback_verified'])
            self.assertFalse(s.snapshot()['candidates'][0]['playback_verified'])
            self.assertFalse(s.snapshot()['order'][0]['playback_verified'])
            s.new_set();self.assertEqual(s.crate.summary()['count'],1);self.assertIsNotNone(a.blocked(tid))

    def test_old_checkpoint_cannot_downgrade_more_recent_provider_evidence(self):
        tid=records()[0]['id'];a=RecordingAvailability(clock=lambda:1000)
        restrict(a,tid,950);old=a.snapshot();old[0]['checkedAt']=900;old[0]['provider']='Old name'
        a.restore(old);a.restore([{'trackId':tid,'code':'recording-unavailable','reason':'Absent','checkedAt':950,'retryAfter':1100}])
        self.assertEqual(a.blocked(tid)['checkedAt'],950);self.assertEqual(a.blocked(tid)['provider'],'Fixture provider')

    def test_only_explicit_evidence_is_accepted_no_timeout_or_locator_namespace_inference(self):
        tid=records()[0]['id'];a=RecordingAvailability(clock=lambda:100)
        for evidence in ({**EVIDENCE,'source':'load-timeout'},{**EVIDENCE,'reference':'netsearch://va123'},
                         {**EVIDENCE,'sha256':'bad'},None):
            with self.assertRaises(ValueError):a.record_preview_only(tid,'Provider',30,evidence)
        with self.assertRaises(ValueError):a.record(tid,PREVIEW_ONLY,'Timeout')
        self.assertEqual(a.snapshot(),[])
        for bad in (float('inf'),float('nan'),True,101,-1):
            with self.assertRaises(ValueError):restrict(a,tid,bad)

    def test_bounded_cache_never_evicts_durable_restrictions_for_transient_failures(self):
        a=RecordingAvailability(clock=lambda:100)
        for n in range(128):restrict(a,'virtualdj:'+format(n,'064x'))
        with self.assertRaises(ValueError):restrict(a,'virtualdj:'+format(128,'064x'))
        a.record('virtualdj:'+format(129,'064x'),'recording-unavailable','Absent')
        self.assertEqual(len(a.snapshot()),128)
        self.assertTrue(all(r['code']==PREVIEW_ONLY for r in a.snapshot()))

    def test_all_session_load_analysis_and_checked_cue_entries_reject_before_any_io(self):
        s=DJSession(threading.Lock());track=records()[0];restrict(s.availability,track['id'])
        peer=Mock();peer.query.side_effect=AssertionError('Native query');peer.send.side_effect=AssertionError('Native send')
        with patch.object(s,'select',side_effect=AssertionError('Catalog selection')):
            calls=[lambda:s.locate_for_load(peer,track,1),lambda:s.load(peer,track,2),
                   lambda:s.analyze_deck(peer,track,3),lambda:s.prepare_deck(peer,track,2,126,checked=track)]
            for call in calls:
                with self.assertRaises(ProviderPreviewRestricted):call()
        peer.query.assert_not_called();peer.send.assert_not_called()

    def test_background_helper_shares_restriction_and_cannot_reuse_old_cues(self):
        s=DJSession(threading.Lock());track=records()[0];s.update(candidates=[track]);restrict(s.availability,track['id'])
        cached={track['id']:{'cueCandidates':[{}],'preparationSeconds':s.snapshot()['secondsPerTrack']}}
        with patch('afterhours.cue_planner.measured_rows',side_effect=AssertionError('Cached analysis reused')):
            job=BackgroundAnalysis(s,'rev',[track],cached);job.run()
            pool=PreparationPool(s,'rev',[track],cached)
        self.assertTrue(job.done);self.assertIn('preview',job.error);self.assertEqual(job.result,[])
        self.assertTrue(pool.done);self.assertIn('preview',pool.error);self.assertEqual(pool.result,[])
        self.assertIs(s.crate.availability,s.availability)
        peer=Mock();job=BackgroundAnalysis(s,'rev',[track]);job.start(peer)
        self.assertFalse(job.owned);peer.assert_not_called();self.assertEqual(peer.mock_calls,[])

    def test_new_evidence_stops_pending_pool_before_acquiring_a_deck(self):
        s=DJSession(threading.Lock());track=records()[0];pool=PreparationPool(s,'rev',[track]);restrict(s.availability,track['id'])
        peer=Mock();pool.step(peer,40)
        self.assertTrue(pool.done);self.assertEqual(pool.jobs,[]);self.assertEqual(peer.mock_calls,[])

    def test_fast_start_rechecks_new_provider_evidence_immediately_before_play(self):
        from test_opening_playback import OpeningTests
        from afterhours.opening_playback import start_and_prepare
        s,report,trace,playing,beat=OpeningTests().setup_session()
        original=s.telemetry
        def telemetry(client):
            value=original(client)
            restrict(s.availability,'virtualdj:'+s.snapshot()['order'][0]['engine']['selection_id'])
            trace.append('restriction-recorded-before-Play')
            return value
        s.telemetry=telemetry
        with patch('afterhours.opening_playback.TransitionMixer') as mixer:
            mixer.return_value.baselines={}
            with self.assertRaises(ProviderPreviewRestricted):start_and_prepare(s,None,s.snapshot()['order'])
        self.assertIn('restriction-recorded-before-Play',trace)
        self.assertNotIn('deck 1 play',trace);self.assertFalse(playing[0])

    def test_fast_start_checks_existing_loaded_recording_before_native_io(self):
        from test_opening_playback import OpeningTests
        from afterhours.opening_playback import start_and_prepare
        s,report,trace,playing,beat=OpeningTests().setup_session()
        restrict(s.availability,'virtualdj:'+s.snapshot()['order'][0]['engine']['selection_id'])
        s.q=Mock(side_effect=AssertionError('Native read before eligibility check'))
        with self.assertRaises(ProviderPreviewRestricted):start_and_prepare(s,None,s.snapshot()['order'])
        s.q.assert_not_called();self.assertEqual(trace,[])

    def test_existing_session_analysis_cannot_bypass_tail_eligibility(self):
        s=DJSession(threading.Lock());tracks=records();s.update(candidates=tracks,order=tracks[:2],
            analysis={'tracks':tracks},queuedUpdate={'id':'rev','tracks':tracks,'status':'needs-analysis','protectedIds':[t['id'] for t in tracks[:2]]})
        restrict(s.availability,tracks[3]['id']);peer=Mock()
        s.service_background(peer,40)
        self.assertEqual(s.snapshot()['queuedUpdate']['status'],'blocked')
        self.assertIsNone(s.background_job);self.assertEqual(peer.mock_calls,[])

    def test_ready_queue_cannot_adopt_a_newly_restricted_future_record(self):
        s=DJSession(threading.Lock());tracks=records();s.update(candidates=tracks,order=tracks[:3],
            queuedUpdate={'id':'rev','tracks':tracks,'status':'ready','protectedIds':[t['id'] for t in tracks[:2]],'prepared':{}})
        restrict(s.availability,tracks[3]['id'])
        self.assertIsNone(s.adopt_queue(1));self.assertEqual(s.snapshot()['queuedUpdate']['status'],'blocked')
        self.assertEqual(s.snapshot()['order'],tracks[:3])

    def test_preview_evidence_triggers_one_bounded_reselection_before_background_failure(self):
        s=DJSession(threading.Lock());tracks=records();ids=[t['id'] for t in tracks]
        s.update(candidates=tracks,order=copy.deepcopy(tracks[:3]),active=True,phase='playing',index=0,
            executionPlan={'id':'fixture','applied':True,'targetBpm':126},telemetry={'decks':[{'playing':True}]},
            queuedUpdate={'id':'tail','tracks':copy.deepcopy(tracks),'status':'needs-analysis'})
        restrict(s.availability,ids[3]);e=Mock(return_value=(decision(ids[:3]),{'responseId':'local-fixture'}));a=DJAgent(s,evaluator=e)
        a.enable(True,120,3);a.state['direction']='House';a.last_refill=(s.state['id'],0,tuple(ids))
        a.tick(force=True);settled(a);a.tick(force=True);settled(a)
        self.assertEqual(e.call_count,1);self.assertEqual(a.remaining,2)
        self.assertEqual(e.call_args.args[0]['reason'],'replace unavailable upcoming recordings')
        self.assertEqual([t['id'] for t in s.snapshot()['queuedUpdate']['tracks']],ids[:3])
        self.assertEqual([t['id'] for t in s.snapshot()['order'][:2]],ids[:2])
        self.assertEqual(s.crate.summary()['count'],4)
        self.assertNotIn(ids[3],[t['id'] for t in music_context(s.snapshot())['catalog']])

    def test_locked_or_protected_preview_is_never_automatically_replaced(self):
        for blocked_index,locked,reorder in ((3,True,True),(3,False,False),(1,False,True)):
            s=DJSession(threading.Lock());tracks=records();ids=[t['id'] for t in tracks]
            s.update(candidates=tracks,order=tracks[:3],active=True,phase='playing',index=0,
                queuedUpdate={'id':'tail','tracks':tracks,'status':'ready'},
                setlist={'allowReorder':reorder,'lockedIds':[ids[3]] if locked else []},telemetry={'decks':[{'playing':True}]})
            restrict(s.availability,ids[blocked_index]);e=Mock();a=DJAgent(s,evaluator=e);a.enable(True,120,3);a.state['direction']='House'
            a.tick(force=True);settled(a);e.assert_not_called()
            self.assertEqual([t['id'] for t in s.snapshot()['queuedUpdate']['tracks']],ids)

    def test_explicit_clearance_needs_real_fresh_proof_beyond_the_preview(self):
        tid=records()[0]['id']
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);a=RecordingAvailability(root/'availability.json',lambda:1000);restrict(a,tid,900)
            stale_snapshot=a.snapshot();receipt=decoded_proof(root,tid,tail=False)
            self.assertEqual(verified_energy(tid.removeprefix('virtualdj:'),335,root)['coverage'],1)
            with self.assertRaisesRegex(ValueError,'beyond the preview'):
                a.confirm_full_audio_recheck(tid,335,receipt,operator_requested=True,report_root=root)
            receipt=decoded_proof(root,tid)
            with self.assertRaises(ValueError):a.confirm_full_audio_recheck(tid,335,receipt,report_root=root)
            with self.assertRaises(ValueError):
                a.confirm_full_audio_recheck(tid,335,{**receipt,'verifiedAt':999},operator_requested=True,report_root=root)
            a.confirm_full_audio_recheck(tid,335,receipt,operator_requested=True,report_root=root)
            self.assertIsNone(a.blocked(tid))
            restored=RecordingAvailability(root/'availability.json',lambda:1000);restored.restore(stale_snapshot)
            self.assertIsNone(restored.blocked(tid))
            restrict(restored,tid,990);self.assertIsNotNone(restored.blocked(tid))

    def test_metadata_only_recording_action_keeps_queue_and_no_native_connection(self):
        native=Mock(side_effect=AssertionError('Native connection'))
        s=DJSession(threading.Lock(),client_factory=native);tracks=records();s.update(candidates=tracks,order=tracks)
        before=copy.deepcopy(s.state['order']);result=s.record_provider_preview(tracks[3]['id'],'Fixture provider',30,EVIDENCE)
        self.assertEqual(s.state['order'],before);self.assertEqual(s.crate.summary()['count'],4)
        self.assertFalse(result['order'][3]['playback_verified']);native.assert_not_called()
        with self.assertRaises(ValueError):s.record_provider_preview('virtualdj:'+'f'*64,'Provider',30,EVIDENCE)


if __name__=='__main__':unittest.main()
