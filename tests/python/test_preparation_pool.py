"""C/D isolation, queue priority and catalog ownership without live audio."""
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from afterhours import cue_planner
from afterhours.preparation_pool import PreparationPool, service_timed_analysis
from afterhours.background_analysis import BackgroundAnalysis,allowed
from afterhours.vdj_session import DJSession
from afterhours.verified_cues import MAX_AGE, VerifiedCues
from test_background_analysis import Peer


class PoolPeer(Peer):
    def __init__(self):
        super().__init__()
        self.values.update({k.replace('deck 3','deck 4'):v for k,v in list(self.values.items()) if k.startswith('deck 3')})
        self.values['deck 4 get_deck']=4
    def send(self,script):
        if 'deck 4' not in script:return super().send(script)
        self.commands.append(script)
        if 'deck 4 volume 0%' in script:self.values['deck 4 volume']=0
        if 'deck 4 unload' in script:self.values['deck 4 loaded']=False
        if 'deck 4 volume 100.' in script:self.values['deck 4 volume']=1


class PoolTests(unittest.TestCase):
    def test_verified_cues_survive_a_new_reader_and_fail_closed_on_changed_evidence(self):
        track = {'id':'virtualdj:'+'a'*64,'bpm':122,'camelot':'8A','engineChecked':True}
        measured = {**track,'preparationSeconds':60,'timedPreparation':True,'cueCandidates':[{'id':'cue'}]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            VerifiedCues(root).write(measured)
            reader = VerifiedCues(root)
            reader.evidence.valid = Mock(return_value=True)
            self.assertEqual(reader.read(track,60,True),measured)
            self.assertIsNone(reader.read(track,30,True))
            self.assertIsNone(reader.read(track,60,False))
            self.assertIsNone(reader.read({**track,'bpm':124},60,True))
            self.assertIsNone(reader.read({**track,'camelot':'7A'},60,True))
            reader.evidence.valid.return_value = False
            self.assertIsNone(reader.read(track,60,True))
            reader.evidence.valid.return_value = True
            path = reader.path(track['id'], timed_seconds=60)
            reader.clock = lambda: path.stat().st_mtime + MAX_AGE + 1
            self.assertIsNone(reader.read(track,60,True))

    def test_exact_cues_keep_both_timed_intervals_and_the_ordinary_variant(self):
        track = {'id':'virtualdj:'+'b'*64, 'bpm':122, 'camelot':'8A', 'engineChecked':True}
        with TemporaryDirectory() as temporary:
            cache = VerifiedCues(Path(temporary))
            cache.evidence.valid = Mock(return_value=True)
            def measured(seconds, timed):
                return {**track, 'preparationSeconds':seconds, 'timedPreparation':timed,
                        'cueCandidates':[{'id':f'{seconds}-{timed}'}]}

            old = measured(60, True)
            cache.path(track['id']).write_text(json.dumps({'cueVersion':cue_planner.VERSION, 'measurement':old}))
            cache.write(measured(30, True))
            self.assertEqual(cache.read(track, 60, True), old)
            self.assertEqual(cache.read(track, 30, True), measured(30, True))

            cache.write(old)
            cache.write(measured(60, False))
            self.assertEqual(cache.read(track, 60, True), old)
            self.assertEqual(cache.read(track, 30, True), measured(30, True))
            self.assertEqual(cache.read(track, 60, False), measured(60, False))
            self.assertIsNone(cache.read(track, 45, True))

    def test_rolling_pool_can_prepare_a_current_cached_cue_without_a_spare_load(self):
        session = DJSession(threading.Lock())
        candidate = {'id':'virtualdj:'+'a'*64,'title':'Fixture'}
        session.update(entryCadence=[30,60], secondsPerTrack=60, order=[{'id':'other'},candidate],
                       queuedUpdate={'id':'revision','tracks':[candidate,{'id':'other'}]})
        measured = {**candidate, 'cueCandidates':[{'id':'cue'}]}
        with patch('afterhours.preparation_pool.VerifiedCues') as storage:
            storage.return_value.read.return_value = measured
            pool = PreparationPool(session,'revision',[candidate])
        client = Mock()
        pool.step(client, 35)
        self.assertTrue(pool.done)
        self.assertEqual(pool.result,[measured])
        storage.return_value.read.assert_called_once_with(candidate,30,True)
        self.assertEqual(client.mock_calls, [])

    def test_timed_analysis_advances_several_mailbox_requests_between_transport_reads(self):
        timestamp = 0.0
        def wait(seconds):
            nonlocal timestamp
            timestamp += seconds
        job = Mock(done=False)
        client = Mock()
        service_timed_analysis(job, client, 35, wait, clock=lambda: timestamp)
        self.assertAlmostEqual(timestamp, .12)
        self.assertGreater(job.step.call_count, 1)
        self.assertLessEqual(job.step.call_count, 11)
        job.step.assert_called_with(client, 35)

        timestamp = 0.0
        job.step.reset_mock()
        def finished(*_):
            job.done = True
        job.step.side_effect = finished
        service_timed_analysis(job, client, 35, wait, clock=lambda: timestamp)
        self.assertEqual(timestamp, 0.0)
        job.step.assert_called_once()

    def test_mix_reserve_pauses_both_native_analysis_clocks_without_osc(self):
        client = Mock()
        session = DJSession(threading.Lock())
        with patch('afterhours.background_analysis.time.monotonic', return_value=0) as clock:
            pool = PreparationPool(session, 'revision', [])
            pool.jobs = [BackgroundAnalysis(session, 'revision', [], deck=deck) for deck in (3, 4)]
            pool.set_suspended(False)
            clock.return_value = 3
            pool.step(client, 11)
            clock.return_value = 35  # longer than the 15-second metadata deadline
            self.assertEqual([job.clock() for job in pool.jobs], [3, 3])
            pool.set_suspended(False)
            clock.return_value = 38
            self.assertEqual([job.clock() for job in pool.jobs], [6, 6])
        self.assertEqual(client.mock_calls, [])

    def test_deck_d_has_the_same_isolation_guards_and_cannot_address_another_deck(self):
        peer=PoolPeer();s=DJSession(threading.Lock());job=BackgroundAnalysis(s,'rev',[],deck=4)
        job.acquire(peer);job.close(peer)
        self.assertTrue(all('deck 4' in cmd and 'deck 3' not in cmd for cmd in peer.commands))
        self.assertTrue(allowed('deck 4 goto 30%',True,4))
        for command in ('deck 3 goto 30%','deck 1 play','deck 4 play','crossfader 50%'):
            self.assertFalse(allowed(command,True,4))

    def test_loading_serializes_catalog_then_both_scans_share_one_owner(self):
        s=DJSession(threading.Lock());peer=PoolPeer();calls=[]
        tracks=[{'id':str(i),'title':str(i)} for i in range(3)]
        class Job:
            def __init__(j,s,rev,tracks,cache,deck):
                j.tracks=tracks;j.deck=deck;j.loaded=None;j.done=False;j.error=None;j.cache={};j.result=[]
                j.owned=True;j.serviced=0;j.status='analyzing';j.message='checking';j.cancelled=threading.Event()
            def step(j,c,seconds):calls.append((j.tracks[0]['id'],j.deck));j.serviced+=1
            def close(j,c):j.owned=False
            def invalidate_catalog(j):pass
            def set_suspended(j, suspended):pass
        with patch('afterhours.preparation_pool.BackgroundAnalysis',Job):
            pool=PreparationPool(s,'rev',tracks)
            pool.step(peer,40);pool.step(peer,40)
            self.assertEqual(calls,[('0',3),('0',3)]);self.assertEqual(len(pool.jobs),1)
            pool.jobs[0].loaded='0';pool.step(peer,40)
            self.assertEqual(calls[-1],('1',4));pool.jobs[1].loaded='1'
            for _ in range(6):pool.step(peer,40)
            self.assertEqual(calls[-6:].count(('0',3)),4);self.assertEqual(calls[-6:].count(('1',4)),2)
            count=len(calls);pool.step(peer,11);self.assertEqual(len(calls),count)
            self.assertEqual([r['deck'] for r in pool.snapshot()['decks']],[3,4])
            pool.jobs[0].done=True;pool.jobs[0].result=[tracks[0]];pool.step(peer,40)
            self.assertEqual(calls[-1],('2',3))
            pool.close(peer);self.assertTrue(all(j.cancelled.is_set() for j in pool.jobs))

    def test_timed_catalog_yields_to_load_and_restarts_search_afterward(self):
        session = DJSession(threading.Lock())
        session.update(entryCadence=[60, 30])
        pool = PreparationPool(session, 'revision', [{'id':'b', 'title':'B'}])
        session.preparation_decks = Mock(return_value=(4,))
        session.background_job = pool
        job = Mock(done=False, rolling=True)
        session.catalog_job = job
        client = Mock()
        pool.jobs = [Mock(loaded=None, done=False, deck=4)]
        session.service_catalog(client, 35)
        job.resume_search.assert_called_once()
        job.step.assert_not_called()
        pool.jobs[0].loaded = 'verified-exact-recording'
        session.service_catalog(client, 17)
        job.step.assert_not_called()
        session.service_catalog(client, 20)
        job.step.assert_called_once_with(client)
        session.preparation_decks.return_value = (3,4)
        session.service_catalog(client, 40)
        self.assertEqual(job.resume_search.call_count, 2)
        job.rolling = False
        session.service_catalog(client, 40)
        job.step.assert_called_once_with(client)

    def test_owned_or_loaded_spares_are_never_replaced(self):
        s=DJSession(threading.Lock());peer=PoolPeer()
        peer.values.update({'deck 3 loaded':True,'deck 4 play':True})
        pool=PreparationPool(s,'rev',[{'id':'x','title':'X'}]);pool.step(peer,40)
        self.assertTrue(pool.done);self.assertIn('in use',pool.error);self.assertEqual(peer.commands,[])

    def test_close_harvests_a_completed_sibling_after_another_job_failed(self):
        session = DJSession(threading.Lock())
        pool = PreparationPool(session, 'revision', [])
        failed = BackgroundAnalysis(session, 'revision', [], deck=3)
        survivor = BackgroundAnalysis(session, 'revision', [], deck=4)
        failed.done = survivor.done = True
        failed.error = 'Exact lookup failed.'
        survivor.result = [{'id':'verified'}]
        pool.jobs = [failed, survivor]
        peer = PoolPeer()
        pool.step(peer, 35)
        self.assertTrue(pool.done)
        self.assertEqual(pool.result, [])
        pool.close(peer)
        pool.close(peer)
        self.assertEqual(pool.result, survivor.result)
        self.assertEqual(peer.commands, [])

    def test_failure_stops_other_work_without_retrying_a_load(self):
        s=DJSession(threading.Lock());pool=PreparationPool(s,'rev',[]);peer=PoolPeer()
        job=BackgroundAnalysis(s,'rev',[],deck=4);job.done=True;job.error='Load unconfirmed'
        pool.jobs=[job];pool.step(peer,40);pool.step(peer,40)
        self.assertEqual(pool.error,'Load unconfirmed');self.assertTrue(pool.done);self.assertEqual(peer.commands,[])

if __name__=='__main__':unittest.main()
