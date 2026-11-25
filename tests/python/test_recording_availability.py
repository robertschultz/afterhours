"""Unavailable recordings never turn a retry loop into a playable queue.

Local fixtures only: no catalog, decoding, network, or playback access.
"""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch,Mock
from afterhours.recording_availability import RecordingAvailability,LEGACY_BOUNDED_SEARCH_REASON
from afterhours.dj_agent import DJAgent,music_context,validate_decision,context_key
from afterhours.vdj_session import DJSession,RecordingUnavailable
from afterhours.vdj_discovery import CatalogSearch
from afterhours.background_analysis import BackgroundAnalysis
from afterhours.dj_bridge import BridgeError
from test_dj_agent import decision,settled


def records():
    return [{'id':'virtualdj:'+c*64,'title':'Track '+c,'artist':'Fixture artist','bpm':126,
             'camelot':'8A','engine':{'selection_id':c*64,'folder_id':'f'*64}} for c in 'abcd']


class AvailabilityTests(unittest.TestCase):
    def test_legacy_capped_search_absence_is_not_restored_as_unplayable_audio(self):
        a,b=records()[:2];cache=RecordingAvailability(clock=lambda:100)
        base={'checkedAt':50,'retryAfter':950}
        cache.restore([{**base,'trackId':a['id'],'code':'recording-unavailable','reason':LEGACY_BOUNDED_SEARCH_REASON},
                       {**base,'trackId':b['id'],'code':'limited-decoded-audio','reason':'Only a short preview was decoded.'}])
        self.assertIsNone(cache.blocked(a['id']))
        self.assertEqual(cache.blocked(b['id'])['code'],'limited-decoded-audio')

    def test_explicit_search_can_clear_absence_but_not_audio_limits(self):
        cache=RecordingAvailability();a,b=records()[:2]
        cache.record(a['id'],'recording-unavailable','Not in search.')
        cache.record(b['id'],'limited-decoded-audio','Short preview.')
        cache.found_again(a['id']);cache.found_again(b['id'])
        self.assertIsNone(cache.blocked(a['id']))
        self.assertEqual(cache.blocked(b['id'])['code'],'limited-decoded-audio')

    def test_cache_is_exact_bounded_private_and_expires_after_restart(self):
        now=[100.0];tid=records()[0]['id']
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'availability.json';cache=RecordingAvailability(path,lambda:now[0])
            cache.record(tid,'recording-unavailable','Exact recording absent.')
            restored=RecordingAvailability(path,lambda:now[0])
            self.assertEqual(restored.blocked(tid)['retryAfter'],1000)
            self.assertIsNone(restored.blocked(records()[1]['id']))
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            now[0]=1001;self.assertEqual(restored.snapshot(),[])
            cache.record(tid,'limited-decoded-audio','Only a short decoded preview.')
            cache.verified(tid);self.assertEqual(RecordingAvailability(path,lambda:now[0]).snapshot(),[])

    def test_invalid_or_future_evidence_and_uncertain_errors_do_not_block_recordings(self):
        cache=RecordingAvailability(clock=lambda:100)
        with self.assertRaises(ValueError):cache.record(records()[0]['id'],'load-timeout','Uncertain load.')
        base={'trackId':records()[0]['id'],'code':'recording-unavailable','reason':'Missing','checkedAt':50,'retryAfter':950}
        for bad in ({'trackId':'not-a-recording'},{'checkedAt':101},{'retryAfter':99},{'retryAfter':999999},{'code':'timeout'}):
            cache.restore([{**base,**bad}]);self.assertEqual(cache.snapshot(),[])

    def test_known_failure_survives_new_set_and_is_excluded_from_planner_not_operator_retry(self):
        s=DJSession(threading.Lock());tracks=records();ids=[t['id'] for t in tracks]
        s.update(candidates=tracks);before=context_key(s.snapshot())
        s.availability.record(ids[1],'limited-decoded-audio','Only 30 seconds decoded.')
        self.assertNotEqual(before,context_key(s.snapshot()))
        packet=music_context(s.snapshot())
        self.assertNotIn(ids[1],[t['id'] for t in packet['catalog']])
        self.assertEqual(packet['unavailableRecordings'][0]['trackId'],ids[1])
        with self.assertRaisesRegex(ValueError,'unavailable'):validate_decision(decision(ids[:3]),s.snapshot())
        with self.assertRaisesRegex(ValueError,'excluded'):s.plan(ids[:3])
        s.plan(ids[:3],source='operator')
        s.new_set();self.assertIsNotNone(s.availability.blocked(ids[1]))

    def test_discovery_does_not_count_known_unplayable_rows_as_a_useful_opening(self):
        s=DJSession(threading.Lock());tracks=records();now=[0.0]
        s.availability.record(tracks[0]['id'],'recording-unavailable','Exact recording absent.')
        job=CatalogSearch(s,['Reference'],'','',40,300,clock=lambda:now[0])
        rows=iter({'id':t['id'],'artist':t['artist'],'metadata':t} for t in tracks)
        with patch('afterhours.vdj_discovery.folder_state',return_value={'id':'f'*64}),patch.object(s,'send'),patch('afterhours.vdj_discovery.selection',side_effect=lambda *a:next(rows)):
            for _ in range(12):
                job.step(None);now[0]+=1
                if job.done:break
        self.assertTrue(job.done);self.assertEqual(job.added,3)
        self.assertEqual([t['id'] for t in s.snapshot()['candidates']],[t['id'] for t in tracks[1:]])

    def test_background_conclusive_failure_is_remembered_but_load_uncertainty_is_not(self):
        for error,remembered in [(RecordingUnavailable('Absent after exact search.'),True),(BridgeError('Load acknowledgement uncertain.'),False)]:
            s=DJSession(threading.Lock());track=records()[0];s.update(candidates=[track])
            job=BackgroundAnalysis(s,'revision',[track])
            with patch.object(DJSession,'analyze_deck',side_effect=error):job.run()
            self.assertEqual(job.status,'blocked');self.assertEqual(bool(s.availability.blocked(track['id'])),remembered)

    def test_blocked_unavailable_tail_can_trigger_one_bounded_reselection(self):
        s=DJSession(threading.Lock());tracks=records();ids=[t['id'] for t in tracks]
        s.update(candidates=tracks,order=copy.deepcopy(tracks[:3]),active=True,phase='playing',index=0,
                 executionPlan={'id':'fixture','applied':True,'targetBpm':126},
                 telemetry={'decks':[{'playing':True}]},
                 queuedUpdate={'id':'tail','tracks':copy.deepcopy(tracks),'status':'blocked'})
        s.availability.record(ids[3],'limited-decoded-audio','Only 30 seconds decoded.')
        e=Mock(return_value=(decision(ids[:3]),{'responseId':'local-fixture'}));a=DJAgent(s,evaluator=e)
        a.enable(True,120,3);a.state['direction']='House'
        a.last_refill=(s.state['id'],0,tuple(ids))
        a.tick(force=True);settled(a)
        self.assertEqual(e.call_count,1)
        self.assertEqual(e.call_args.args[0]['reason'],'replace unavailable upcoming recordings')
        self.assertEqual([t['id'] for t in s.snapshot()['queuedUpdate']['tracks']],ids[:3])
        a.tick(force=True);settled(a);self.assertEqual(e.call_count,1)
        self.assertEqual(a.remaining,2)


if __name__=='__main__':unittest.main()
