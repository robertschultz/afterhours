"""Synthetic crate and queue checks; no native DJ or provider requests."""
import copy
import threading
import unittest
from unittest.mock import Mock, patch
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_discovery import CatalogSearch
from afterhours.vdj_session import DJSession
from test_cue_planner import fixture
from afterhours import ai_mix
from afterhours import cue_planner


class DiscoveryTests(unittest.TestCase):
    def test_paused_search_refreshes_deck_observation_without_transport(self):
        s=DJSession(threading.Lock());s.update(order=[{'id':'keep'}],telemetry={'sampledAt':0})
        job=Mock();job.done=False
        def finish(client): job.done=True;s.update(catalogSearch={'status':'complete','message':'Search complete.'})
        job.step.side_effect=finish
        with patch('afterhours.vdj_session.CatalogSearch',return_value=job),patch.object(s,'_launch') as launch,patch.object(s,'telemetry') as observe,patch.object(s,'send') as send:
            s.discover_more('Requested artist')
            launch.call_args.args[0]('native-client')
            observe.assert_called_once_with('native-client');send.assert_not_called()
        self.assertEqual(s.snapshot()['order'],[{'id':'keep'}])

    def test_only_explicit_catalog_search_adopts_a_new_source(self):
        for explicit in (False,True):
            s=DJSession(threading.Lock());s.folder={'id':'old'};s.update(order=[{'id':'keep'}])
            job=CatalogSearch(s,['Artist'],'','',40,300,recheck_missing=explicit)
            with patch('afterhours.vdj_discovery.folder_state',return_value={'id':'new'}),patch.object(s,'send'):
                if explicit:
                    job.step(None);self.assertEqual(s.folder['id'],'new')
                    self.assertEqual(s.snapshot()['order'],[{'id':'keep'}])
                else:
                    with self.assertRaisesRegex(BridgeError,'folder changed'):job.step(None)
                    self.assertEqual(s.folder['id'],'old')

    def test_public_search_progress_retains_names_tempo_and_actual_read_count(self):
        s=DJSession(threading.Lock());now=[0.0]
        job=CatalogSearch(s,['John Summit','Dom Dolla'],'John Summit; Dom Dolla','Tech House',124,128,clock=lambda:now[0])
        row={'id':'one','artist':'John Summit','metadata':{'id':'one','title':'Test title','artist':'John Summit','genre':'Tech House','bpm':126}}
        with patch('afterhours.vdj_discovery.folder_state',return_value={'id':'folder'}),patch('afterhours.vdj_discovery.selection',return_value=row),patch.object(s,'send'):
            job.step(None);state=s.snapshot()['catalogSearch']
            self.assertEqual(state['term'],'John Summit');self.assertEqual(state['examined'],0)
            self.assertEqual(state['filters'],{'artist':'John Summit; Dom Dolla','genre':'Tech House','bpmMin':124,'bpmMax':128})
            now[0]=2;job.step(None);job.step(None)
            state=s.snapshot()['catalogSearch'];self.assertEqual((state['examined'],state['added']),(1,1))
            self.assertEqual(state['foundIds'],['one'])
            # Reading an existing row is progress, never another found track.
            job.step(None);state=s.snapshot()['catalogSearch'];self.assertEqual((state['examined'],state['added']),(2,1))
            self.assertEqual(state['foundIds'],['one'])
            self.assertEqual(s.snapshot()['order'],[])

    def test_opening_search_keeps_old_finds_and_commits_after_three_new_recordings(self):
        s=DJSession(threading.Lock());s.update(candidates=[{'id':'old','title':'Old','artist':'Original'}])
        now=[0.0];job=CatalogSearch(s,['Reference one','Reference two'],'','House',120,130,clock=lambda:now[0])
        rows=iter({'id':str(i),'artist':'Fixture','metadata':{'id':str(i),'title':f'Track {i}',
            'artist':'Fixture','genre':'House','bpm':125}} for i in range(20))
        with patch('afterhours.vdj_discovery.folder_state',return_value={'id':'folder'}),patch('afterhours.vdj_discovery.selection',side_effect=lambda *a:next(rows)),patch.object(s,'send') as send:
            for _ in range(20):
                job.step(None);now[0]+=.8
                if job.done:break
            self.assertTrue(job.done);self.assertEqual(job.added,3)
            self.assertEqual([t['id'] for t in s.snapshot()['candidates']],['old','0','1','2'])
            self.assertEqual(s.snapshot()['order'],[])
            self.assertTrue(all(v.args[1].startswith(('search ','browser_')) for v in send.call_args_list))
            self.assertIn('Reference two',s.positions['2']['query'])

    def test_empty_search_has_a_short_deadline_and_partial_finds_survive(self):
        s=DJSession(threading.Lock());now=[0.0];job=CatalogSearch(s,['Missing'],'','',100,130,clock=lambda:now[0])
        with patch('afterhours.vdj_discovery.folder_state',return_value={'id':'folder'}),patch.object(s,'send'):
            job.step(None);now[0]=26;job.step(None)
        self.assertTrue(job.done);self.assertIn('Keeping',s.snapshot()['catalogSearch']['message'])

    def test_playback_search_shares_worker_and_yields_before_transition(self):
        s=DJSession(threading.Lock());s.update(active=True,phase='playing',order=[{'id':'a'}])
        with patch.object(s,'_launch') as launch:
            s.discover_more('Some reference');launch.assert_not_called()
        job=s.catalog_job
        with patch.object(job,'step') as step:
            s.service_catalog(None,15.9);step.assert_not_called()
            s.service_catalog(None,20);step.assert_called_once()
            step.side_effect=BridgeError('Synthetic unavailable catalog')
            s.service_catalog(None,20)
        self.assertEqual(s.snapshot()['phase'],'playing')
        self.assertEqual(s.snapshot()['catalogSearch']['status'],'error')
        self.assertTrue(job.done)

    def test_preload_invalidates_search_cursor_without_losing_results(self):
        s=DJSession(threading.Lock());job=CatalogSearch(s,['Reference'],'','',40,300)
        job.phase='read';job.position=7;job.added=2;job.resume_search()
        self.assertEqual((job.phase,job.position,job.added),('begin',0,2))

    def test_timed_search_only_shares_a_spare_scan_after_exact_load(self):
        s = DJSession(threading.Lock())
        s.update(active=True,phase='playing',entryCadence=[60,30])
        s.catalog_job = Mock(done=False,rolling=True)
        s.background_job = Mock()
        s.background_job.navigation_idle.return_value = False

        s.service_catalog(None,40)
        s.catalog_job.step.assert_not_called()
        s.background_job.navigation_idle.return_value = True
        s.service_catalog(None,17)
        s.catalog_job.step.assert_not_called()
        s.service_catalog(None,20)
        s.catalog_job.step.assert_called_once_with(None)
        s.catalog_job.rolling = False
        s.service_catalog(None,40)
        s.catalog_job.step.assert_called_once()


class QueueRevisionTests(unittest.TestCase):
    def prepared(self):
        _,report,response=fixture();review=ai_mix.validate_plan(response,report,30)
        plan=cue_planner.compile_plan(review,report,report['tracks'],30);plan['applied']=True
        s=DJSession(threading.Lock());s.update(candidates=copy.deepcopy(report['tracks']),order=copy.deepcopy(report['tracks']),
            analysis=report,aiReview=review,executionPlan=plan,active=True,phase='playing',index=0)
        return s,plan

    def transitions(self,ids):
        return [{'fromId':a,'toId':b,'technique':'short-handoff','blendBeats':8,'reason':'A shorter restrained lead handoff.'}
                for a,b in zip(ids,ids[1:])]

    def test_prepared_future_order_waits_for_handoff_and_keeps_armed_transition(self):
        s,original=self.prepared();ids=['0','1','3','2']
        with patch.object(s,'send') as send,patch.object(s,'factory') as engine:
            result=s.revise_queue(ids,reason='Future musical direction',mix_transitions=self.transitions(ids))
            self.assertEqual([t['id'] for t in result['order']],['0','1','2','3'])
            self.assertEqual(result['queuedUpdate']['status'],'ready');send.assert_not_called();engine.assert_not_called()
            self.assertIsNone(result['lastQueueApplication'])
            queue_revision=result['queuedUpdate']['id']
            pending=result['queuedUpdate']['prepared']['executionPlan']
            self.assertEqual(pending['selections'][:2],original['selections'][:2])
            self.assertEqual(pending['transitions'][0],original['transitions'][0])
            with patch('afterhours.cue_planner.verify_receipts') as verify:
                order,plan=s.adopt_queue(1);verify.assert_called_once()
            self.assertEqual([t['id'] for t in order],ids);self.assertTrue(plan['applied'])
            self.assertEqual(plan['selections'][2]['trackId'],'3')
            self.assertIsNone(s.snapshot()['queuedUpdate'])
            receipt=s.snapshot()['lastQueueApplication']
            self.assertEqual(receipt['queueRevision'],queue_revision)
            self.assertEqual(receipt['trackIds'],ids);self.assertEqual(receipt['planId'],plan['id'])
            self.assertGreater(receipt['appliedAt'],0)

    def test_incoming_current_locked_and_unmeasured_tracks_are_not_swapped_into_audio(self):
        s,_=self.prepared()
        for ids in (['1','0','2','3'],['0','2','1','3']):
            with self.assertRaisesRegex(ValueError,'protected'):s.revise_queue(ids)
        s.state['setlist']['lockedIds']=['2']
        with self.assertRaisesRegex(ValueError,'locked'):s.revise_queue(['0','1','3','2'])
        s.state['setlist']['lockedIds']=[]
        s.state['candidates'].append({'id':'new','title':'New recording','bpm':155})
        result=s.revise_queue(['0','1','new'],reason='New music requested')
        self.assertEqual(result['queuedUpdate']['status'],'needs-analysis')
        with patch('afterhours.cue_planner.verify_receipts') as verify:
            self.assertIsNone(s.adopt_queue(1));verify.assert_not_called()
        self.assertEqual([t['id'] for t in s.snapshot()['order']],['0','1','2','3'])

    def test_stale_cue_evidence_or_late_revision_cannot_be_adopted(self):
        s,_=self.prepared();ids=['0','1','3','2']
        s.revise_queue(ids,mix_transitions=self.transitions(ids))
        with patch('afterhours.cue_planner.verify_receipts',side_effect=ValueError('changed receipt')):
            with self.assertRaisesRegex(ValueError,'changed receipt'):s.adopt_queue(1)
        self.assertEqual([t['id'] for t in s.snapshot()['order']],['0','1','2','3'])
        self.assertIsNone(s.adopt_queue(2))
        self.assertEqual(s.snapshot()['queuedUpdate']['status'],'needs-review')

if __name__=='__main__':unittest.main()
