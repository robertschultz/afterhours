"""Observed-run archive behavior. No native, network or provider operations."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from afterhours.audio_analysis import read_scan,validate_scan
from afterhours.run_archive import RunArchive,MAX_INTERVALS
from afterhours.vdj_session import DJSession
from test_recording_availability import records
from test_audio_analysis import report,SID


class Clock:
    def __init__(self):self.now=1000.0;self.mono=0.0
    def wall(self):return self.now
    def monotonic(self):return self.mono
    def advance(self,seconds):self.now+=seconds;self.mono+=seconds


def setup(clock,archive):
    blocked=Mock(side_effect=AssertionError('No native access'))
    session=DJSession(threading.Lock(),client_factory=blocked,archive=archive)
    rows=records();session.update(candidates=rows,order=rows[:3],phase='ready')
    return session,blocked


def emit(session,clock,a=True,b=False,phase='playing',active=True,a_id=None,b_id=None,cross=0,position=None):
    rows=records();decks=[]
    for number,playing,identifier in ((1,a,a_id or rows[0]['id']),(2,b,b_id or rows[1]['id'])):
        decks.append({'deck':number,'id':identifier,'loaded':True,'playing':playing,
                      'bpm':126,'position':clock.mono if position is None else position,'sampledAt':clock.now})
    session.update(phase=phase,active=active,startedAt=1000,
                   telemetry={'sampledAt':clock.now,'crossfader':cross,'decks':decks})


class ArchiveTests(unittest.TestCase):
    def make(self,path=None,**kwargs):
        clock=Clock();archive=RunArchive(path,clock=clock.wall,monotonic=clock.monotonic,**kwargs)
        self.addCleanup(archive.close);session,blocked=setup(clock,archive)
        return clock,archive,session,blocked

    def finish(self,archive):
        self.assertTrue(archive.flush());summary=archive.latest();self.assertIsNotNone(summary)
        return archive.get(summary['id'])

    def test_no_retroactive_recap_from_plan_preparation_or_unowned_manual_playback(self):
        c,a,s,n=self.make();self.assertIsNone(a.latest())
        for phase,active,playing in [('ready',False,False),('preparing',True,False),('takeover',False,True)]:
            emit(s,c,a=playing,phase=phase,active=active);c.advance(1)
        self.assertTrue(a.flush());self.assertIsNone(a.latest());self.assertIsNone(a.active);n.assert_not_called()

    def test_stop_records_only_exact_played_tracks_and_their_observed_intervals(self):
        c,a,s,n=self.make();emit(s,c);c.advance(2);emit(s,c)
        c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
        recap=self.finish(a)
        self.assertEqual(recap['status'],'stopped');self.assertEqual(recap['durationSeconds'],3)
        self.assertEqual(recap['endedAt'],1003);self.assertEqual(recap['trackCount'],1)
        row=recap['played'][0]
        self.assertEqual((row['id'],row['title'],row['start'],row['end'],row['endConfirmed']),
                         (records()[0]['id'],'Track a',0,3,True))
        self.assertNotIn('Track b',recap['tracklist']['text']);self.assertIsNone(recap['music']);self.assertIsNone(recap['room'])
        self.assertFalse(recap['capabilities']['exportRecording']);n.assert_not_called()

    def test_partial_handoff_retains_both_observed_tracks_without_claiming_verified_success(self):
        c,a,s,n=self.make();emit(s,c);c.advance(1);emit(s,c,b=True,phase='mixing',cross=.4)
        c.advance(1);emit(s,c,b=True,phase='error',active=False,cross=.4)
        recap=self.finish(a)
        self.assertEqual(recap['status'],'interrupted');self.assertIsNone(recap['endedAt'])
        self.assertEqual(recap['trackCount'],2);self.assertEqual(recap['landed'],[])
        self.assertEqual([(r['start'],r['end']) for r in recap['played']],[(0,2),(1,2)])
        self.assertTrue(all(not r['endConfirmed'] for r in recap['played']));n.assert_not_called()

    def test_verified_handoff_and_confirmed_completion_are_factual_milestones(self):
        c,a,s,n=self.make();emit(s,c);c.advance(1);emit(s,c,b=True,phase='mixing',cross=.5)
        c.advance(1);emit(s,c,b=True,phase='mixing',cross=1)
        evidence={'verified':True,'from':records()[0]['id'],'to':records()[1]['id'],'completedAt':c.now}
        a.transition(evidence);a.transition(evidence)
        c.advance(1);emit(s,c,a=False,b=True,cross=1)
        c.advance(1);emit(s,c,a=False,b=False,phase='completed',active=True,cross=1)
        recap=self.finish(a)
        self.assertEqual(recap['status'],'completed');self.assertEqual(recap['endedAt'],1004)
        self.assertEqual(len(recap['landed']),1);self.assertEqual(recap['landed'][0]['track']['id'],records()[1]['id'])
        self.assertEqual(recap['played'][0]['end'],3);self.assertTrue(all(r['endConfirmed'] for r in recap['played']))

    def test_fast_opening_that_later_fails_analysis_is_still_archived(self):
        c,a,s,n=self.make();emit(s,c,phase='preparing');self.assertIsNone(a.active)
        s.update(startup={'playingAt':c.now,'status':'analyzing'},phase='playing',startedAt=c.now)
        self.assertIsNotNone(a.active);c.advance(1);emit(s,c,phase='error',active=False)
        recap=self.finish(a);self.assertEqual(recap['trackCount'],1);self.assertEqual(recap['status'],'interrupted')

    def test_paused_identity_replacement_and_observation_gaps_are_not_filled_with_invented_playback(self):
        c,a,s,n=self.make();emit(s,c);c.advance(2);emit(s,c)
        c.advance(20);emit(s,c);c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
        recap=self.finish(a)
        self.assertEqual([(r['start'],r['end'],r['endConfirmed']) for r in recap['played']],[(0,2,False),(22,23,True)])
        self.assertEqual(recap['coverage']['unconfirmedGaps'],1);self.assertEqual(recap['trackCount'],1)

    def test_repeated_stop_does_not_extend_or_rewrite_an_immutable_run_and_resume_gets_new_identity(self):
        with tempfile.TemporaryDirectory() as d:
            c,a,s,n=self.make(Path(d));emit(s,c);c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
            first=self.finish(a);body=(Path(d)/(first['id']+'.json')).read_bytes()
            c.advance(20);emit(s,c,a=False,phase='stopped',active=False);self.assertTrue(a.flush())
            self.assertEqual(a.latest()['id'],first['id']);self.assertEqual((Path(d)/(first['id']+'.json')).read_bytes(),body)
            emit(s,c);c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
            second=self.finish(a);self.assertNotEqual(second['id'],first['id']);self.assertEqual(second['startAt'],1021)
            self.assertEqual((Path(d)/(first['id']+'.json')).read_bytes(),body);self.assertEqual(len(list(Path(d).glob('????????????????????????????????.json'))),2)

    def test_clock_correction_does_not_expand_monotonic_played_duration(self):
        c,a,s,n=self.make();emit(s,c);c.now+=3600;c.advance(2);emit(s,c)
        c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
        recap=self.finish(a);self.assertEqual(recap['durationSeconds'],3);self.assertEqual(recap['endedAt'],1003)

    def test_old_live_checkpoint_is_archived_as_interrupted_without_replaying_transport(self):
        with tempfile.TemporaryDirectory() as d:
            c,a,s,n=self.make(Path(d));emit(s,c);c.advance(2);emit(s,c);self.assertTrue(a.flush())
            # Simulate process loss after an on-disk checkpoint, not graceful close.
            with a.condition:a.closed=True;a.condition.notify_all()
            a.worker.join(2);a.active=None
            recovered=RunArchive(Path(d));self.addCleanup(recovered.close)
            recap=self.finish(recovered)
            self.assertEqual(recap['status'],'interrupted');self.assertIsNone(recap['endedAt'])
            self.assertEqual(recap['durationSeconds'],2);self.assertEqual(recap['trackCount'],1)
            self.assertFalse((Path(d)/'active.json').exists());n.assert_not_called()

    def test_new_set_and_plan_boundaries_preserve_observed_run_before_replacing_metadata(self):
        for operation in ('new_set','plan','restore'):
            with self.subTest(operation=operation):
                c,a,s,n=self.make();emit(s,c);c.advance(1)
                # Idle/paused metadata reset without a terminal run receipt.
                s.state['active']=False;s.state['telemetry']['decks'][0]['playing']=False
                saved=s.snapshot()
                if operation=='new_set':s.new_set()
                elif operation=='plan':s.plan([t['id'] for t in records()[:3]],mix_style='quick')
                else:s.restore(saved)
                recap=self.finish(a);self.assertEqual(recap['status'],'interrupted');self.assertIsNone(recap['endedAt'])
                self.assertEqual(recap['played'][0]['id'],records()[0]['id']);n.assert_not_called()

    def test_private_arc_and_interval_bounds_do_not_expand_counts_from_queue(self):
        c,a,s,n=self.make()
        for i in range(MAX_INTERVALS+8):
            emit(s,c,a_id=records()[i%2]['id']);c.advance(.1)
        emit(s,c,a=False,phase='stopped',active=False)
        recap=self.finish(a);self.assertEqual(len(recap['played']),MAX_INTERVALS)
        self.assertEqual(recap['trackCount'],2);self.assertTrue(recap['coverage']['truncated'])
        self.assertLessEqual(len(recap['moments']),16);self.assertLessEqual(len(recap['tracklist']['text'].encode()),65536)
        with self.assertRaises(ValueError):a.get('../active')

    def test_failed_restore_leaves_current_observed_run_and_its_metadata_intact(self):
        c,a,s,n=self.make();emit(s,c);s.state['active']=False
        saved=s.snapshot();saved['analysis']={'tracks':[]}
        before=copy.deepcopy(a.active)
        with self.assertRaisesRegex(ValueError,'Saved analysis'):s.restore(saved)
        self.assertEqual(a.active,before);self.assertIsNone(a.latest());n.assert_not_called()

    def test_corrupt_private_archive_is_rejected_without_a_partial_recap(self):
        with tempfile.TemporaryDirectory() as d:
            c,a,s,n=self.make(Path(d));emit(s,c);c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
            recap=self.finish(a);identifier=recap['id'];target=Path(d)/(identifier+'.json')
            self.assertEqual(target.stat().st_mode&0o777,0o600)
            recap['played'][0]['end']=500;target.write_text(json.dumps(recap));a.cache.clear()
            with self.assertRaisesRegex(ValueError,'malformed'):a.get(identifier)
            n.assert_not_called()

    def test_storage_failure_is_visible_and_recoverable_without_stopping_audio(self):
        with tempfile.TemporaryDirectory() as d:
            c,a,s,n=self.make(Path(d));failed=threading.Event()
            def broken(*args):failed.set();raise OSError('fixture disk failure')
            with patch.object(a,'_write',side_effect=broken):
                emit(s,c);self.assertTrue(failed.wait(1));self.assertFalse(a.flush(.02))
                c.advance(1);start=time.monotonic();emit(s,c)
                self.assertLess(time.monotonic()-start,.2);self.assertTrue(s.snapshot()['active'])
                self.assertIn('Music control is unchanged',a.error)
            self.assertTrue(a.flush(2));c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
            recap=self.finish(a);self.assertEqual(recap['status'],'stopped');self.assertIsNone(a.error);n.assert_not_called()

    def test_failed_old_checkpoint_cannot_resurrect_after_two_runs_finalize_or_regress_restart(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c,a,s,n=self.make(root)
            entered=threading.Event();release=threading.Event();write=a._write;failed=[False]
            def fail_delayed_checkpoint(path,value):
                if path.name=='active.json' and not failed[0]:
                    failed[0]=True;entered.set();self.assertTrue(release.wait(2));raise OSError('old checkpoint failed')
                return write(path,value)
            try:
                with patch.object(a,'_write',side_effect=fail_delayed_checkpoint):
                    emit(s,c);self.assertTrue(entered.wait(1));first=a.active['id']
                    c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
                    c.advance(1);emit(s,c);second=a.active['id']
                    c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
                    release.set();self.assertTrue(a.flush(3))
                self.assertFalse((root/'active.json').exists());self.assertEqual(a.latest()['id'],second)
                first_bytes=(root/(first+'.json')).read_bytes();second_bytes=(root/(second+'.json')).read_bytes()
                a.close();recovered=RunArchive(root);self.addCleanup(recovered.close)
                self.assertTrue(recovered.flush());self.assertEqual(recovered.latest()['id'],second)
                self.assertEqual((root/(first+'.json')).read_bytes(),first_bytes)
                self.assertEqual((root/(second+'.json')).read_bytes(),second_bytes);n.assert_not_called()
            finally:release.set()

    def test_recent_openings_survive_restart_and_exclude_unplayed_queue_entries(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clock, archive, session, peer = self.make(root)
            first, second, unplayed = [row['id'] for row in records()[:3]]
            for opener in (first, second):
                emit(session, clock, a_id=opener)
                clock.advance(2)
                emit(session, clock, a_id=opener, a=False, phase='stopped', active=False)
                self.finish(archive)
                clock.advance(1)
            self.assertEqual(archive.recent_opening_ids(), {first, second})
            archive.close()
            recovered = RunArchive(root)
            self.addCleanup(recovered.close)
            self.assertEqual(recovered.recent_opening_ids(), {first, second})
            self.assertNotIn(unplayed, recovered.recent_opening_ids())
            peer.assert_not_called()

    def test_archive_keeps_pair_evidence_for_synchronized_handoffs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock, archive, session, _ = self.make(root)
            outgoing, incoming, unplayed = [row['id'] for row in records()[:3]]
            saved = []
            for synced in (True, False):
                emit(session, clock)
                clock.advance(1)
                emit(session, clock, b=True, phase='mixing', cross=.5)
                receipt = {'verified':True, 'syncObserved':synced, 'from':outgoing, 'to':incoming,
                           'completedAt':clock.now}
                archive.transition(receipt)
                archive.transition({**receipt, 'to':unplayed})
                clock.advance(1)
                emit(session, clock, a=False, phase='stopped', active=False)
                saved.append(self.finish(archive)['id'])
                clock.advance(1)
            archive.close()
            recovered = RunArchive(root)
            self.addCleanup(recovered.close)
            first, second = [recovered.get(run_id)['landed'] for run_id in saved]
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]['fromRecordingId'], outgoing)
            self.assertEqual(first[0]['track']['id'], incoming)
            self.assertNotIn('fromRecordingId', second[0])

    def test_stale_finalized_journal_cannot_regress_latest_but_incomplete_pointer_commit_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c,a,s,n=self.make(root)
            emit(s,c);c.advance(1);emit(s,c,a=False,phase='stopped',active=False);first=self.finish(a)
            # Run order must remain correct even if the wall clock moves back.
            c.now-=300;emit(s,c);c.advance(1);emit(s,c,a=False,phase='stopped',active=False);second=self.finish(a)
            self.assertLess(second['startAt'],first['startAt']);a.close()
            (root/'active.json').write_text(json.dumps(first))
            recovered=RunArchive(root);self.assertTrue(recovered.flush())
            self.assertEqual(recovered.latest()['id'],second['id']);self.assertFalse((root/'active.json').exists());recovered.close()
            # The immutable second file was committed, but its latest pointer
            # may not have committed before power loss. Its journal repairs it.
            (root/'latest.json').write_text(json.dumps({'id':first['id']}))
            (root/'active.json').write_text(json.dumps(second))
            recovered=RunArchive(root);self.addCleanup(recovered.close);self.assertTrue(recovered.flush())
            self.assertEqual(recovered.latest()['id'],second['id']);n.assert_not_called()

    def test_a_four_deck_set_records_what_played_on_c(self):
        c,a,s,n=self.make();rows=records()
        s.update(deckRotation=[1,2,3,4]);emit(s,c)
        reading=s.snapshot()['telemetry'];reading['decks'][0]['playing']=False
        reading['decks']+= [{'deck':3,'id':rows[2]['id'],'title':rows[2].get('title','Third'),'loaded':True,'playing':True,'bpm':126,
                             'sampledAt':c.now,'position':1},{'deck':4,'loaded':False,'playing':False,'standby':True}]
        c.advance(1);reading['sampledAt']=c.now
        for row in reading['decks']:
            if 'sampledAt' in row:row['sampledAt']=c.now
        s.update(telemetry=reading,phase='playing',active=True)
        c.advance(1);reading=s.snapshot()['telemetry'];reading['sampledAt']=c.now
        for row in reading['decks']:row['playing']=False;row['sampledAt']=c.now
        s.update(telemetry=reading,phase='stopped',active=False)
        recap=self.finish(a)
        self.assertEqual([row['deck'] for row in recap['played']],[1,3])

    def test_spare_deck_activity_and_unverified_scan_do_not_create_play_or_music_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);data=report();(root/(SID+'.json')).write_text(json.dumps(data));digest=read_scan(SID,root)[1]
            c,a,s,n=self.make(report_root=root);tid='virtualdj:'+SID
            s.update(analysis={'tracks':[{'id':tid,'duration':10,'energyMap':{'reportDigest':digest}}]})
            emit(s,c,a_id=tid)
            reading=s.snapshot()['telemetry'];reading['decks'].append({'deck':3,'id':records()[2]['id'],'title':'Spare analysis',
                'loaded':True,'playing':True,'sampledAt':c.now,'position':1})
            s.update(telemetry=reading);c.advance(1);emit(s,c,a=False,a_id=tid,phase='stopped',active=False)
            recap=self.finish(a);self.assertEqual(recap['trackCount'],1);self.assertEqual(recap['played'][0]['deck'],1)
            self.assertIsNone(recap['music']);self.assertNotIn('Spare analysis',recap['tracklist']['text'])

    def test_source_loudness_requires_matching_verified_scan_and_program_position(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);data=report();(root/(SID+'.json')).write_text(json.dumps(data));digest=read_scan(SID,root)[1]
            receipt={'version':1,'recordingId':SID,'reportDigest':digest,'audioSaved':False,
                     'method':'two-stable-scans-and-native-sound-end','passes':2,'stableForSeconds':1,
                     'soundEndSeconds':9.8,'featureDigest':validate_scan(data,9.8)['featureDigest']}
            (root/(SID+'.verified.json')).write_text(json.dumps(receipt))
            c,a,s,n=self.make(report_root=root);track={**records()[0],'id':'virtualdj:'+SID,'title':'Measured recording'}
            s.update(candidates=[track,*records()[1:]],analysis={'tracks':[{'id':track['id'],'duration':10,'energyMap':{'reportDigest':digest}}]})
            emit(s,c,a_id=track['id'],position=1);c.advance(1);emit(s,c,a_id=track['id'],position=2,cross=.5)
            c.advance(1);emit(s,c,a_id=track['id'],position=3);c.advance(1);emit(s,c,a=False,phase='stopped',active=False,a_id=track['id'])
            recap=self.finish(a);self.assertEqual([r['t'] for r in recap['music']['samples']],[0,2])
            self.assertAlmostEqual(recap['music']['samples'][0]['e'],3.3333)
            self.assertIn('not master-output or room',recap['music']['basis']);self.assertIsNone(recap['room'])

    def test_slow_writer_and_broken_observer_cannot_block_or_fail_audio_state_updates(self):
        with tempfile.TemporaryDirectory() as d:
            c,a,s,n=self.make(Path(d));entered=threading.Event();release=threading.Event();write=a._write
            def slow(*args):entered.set();self.assertTrue(release.wait(2));return write(*args)
            with patch.object(a,'_write',side_effect=slow):
                with s.control_lock:
                    emit(s,c);self.assertTrue(entered.wait(1))
                    start=time.monotonic();c.advance(.1);emit(s,c)
                    self.assertLess(time.monotonic()-start,.2)
                release.set();self.assertTrue(a.flush())
            with patch.object(a,'observe',side_effect=RuntimeError('fixture bug')):
                s.update(phase='playing',active=True)
            self.assertEqual(s.snapshot()['phase'],'playing');self.assertIn('Music control is unchanged',a.error)
            n.assert_not_called()


if __name__=='__main__':unittest.main()
