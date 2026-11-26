"""Verified preview omissions keep the order and never override track locks."""
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock,patch
from afterhours.cue_planner import LimitedAudio
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_session import DJSession,RecordingUnavailable,TrackMetadataChanged


class LimitedPreparationTests(unittest.TestCase):
    def test_timed_preparation_applies_local_measured_plan_without_playing(self):
        from test_cue_planner import fixture
        _, report, _ = fixture(count=3)
        tracks = [{**t, 'engine': {'selection_id': t['id']*64}} for t in report['tracks']]
        session = DJSession(threading.Lock())
        session.update(candidates=tracks)
        session.plan([t['id'] for t in tracks])
        client = Mock()
        client.status.return_value = {'automix': False, 'decks': [{'playing':False}]*2}
        changed = {**tracks[1], 'key':'C minor', 'camelot':'5A'}
        def prepare_track(client, track, deck, bpm, *, cue, checked=None):
            if deck == 2 and checked is None:
                raise TrackMetadataChanged(changed)
        with ExitStack() as stack:
            stack.enter_context(patch('afterhours.vdj_session.track_analysis',
                side_effect=lambda values: {'tracks': list(values)}))
            stack.enter_context(patch('afterhours.vdj_session.verified_waveform', return_value=True))
            stack.enter_context(patch('afterhours.cue_planner.verify_receipts'))
            stack.enter_context(patch.object(session, 'analyze_deck', side_effect=lambda c, t, d: t))
            stack.enter_context(patch.object(session, 'measure_cues', side_effect=lambda c,t,*a,**kw: t['cueCandidates']))
            prepare = stack.enter_context(patch.object(session, 'prepare_deck', side_effect=prepare_track))
            send = stack.enter_context(patch.object(session, 'send'))
            stack.enter_context(patch.object(session, 'telemetry'))
            stack.enter_context(patch.object(session, '_launch', side_effect=lambda fn, phase:fn(client)))
            result = session.prepare_analysis(entry_cadence=[60,30])
        self.assertTrue(result['executionPlan']['applied'])
        self.assertEqual(result['entryCadence'], [60,30])
        self.assertEqual(result['executionPlan']['source'], 'Local measured timed-set preparation')
        self.assertTrue(all('cue' in call.kwargs for call in prepare.call_args_list))
        self.assertEqual(prepare.call_args.kwargs['checked'], changed)
        self.assertEqual(result['order'][1]['camelot'], changed['camelot'])
        send.assert_called_once_with(client, 'crossfader 0%')

    def test_only_unlocked_preview_is_omitted_with_exact_selection_receipt(self):
        for locked,missing in ((False,False),(True,False),(False,True),(True,True)):
            s=DJSession(threading.Lock())
            tracks=[{'id':c,'title':'Track '+c,'bpm':126,'camelot':'8A','engine':{'selection_id':c*64}} for c in 'abc']
            s.update(candidates=tracks);s.plan(['a','b','c'])
            if locked:s.state['setlist']['lockedIds']=['b']
            client=Mock();client.status.return_value={'automix':False,'decks':[{'playing':False}]*2}
            def report(items):
                return {'tracks':[{'id':t['id'],'duration':300,'energyMap':{'reportDigest':'fixture'},'points':[]} for t in items]}
            def measure(client,t,*args):
                if t['id']=='b':raise LimitedAudio('Track b has only 30 seconds of decoded audio available.')
                return [{'id':'cue-'+t['id']}]
            def analyze(client,t,deck):
                if missing and t['id']=='b':raise RecordingUnavailable('Exact recording is unavailable.')
                return {**t,'camelot':'9A'} if t['id']=='c' else t
            with ExitStack() as stack:
                stack.enter_context(patch('afterhours.vdj_session.track_analysis',side_effect=report))
                stack.enter_context(patch('afterhours.vdj_session.verified_waveform',return_value=True))
                stack.enter_context(patch.object(s,'analyze_deck',side_effect=analyze))
                stack.enter_context(patch.object(s,'measure_cues',side_effect=measure))
                stack.enter_context(patch.object(s,'prepare_deck'))
                stack.enter_context(patch.object(s,'send'))
                stack.enter_context(patch.object(s,'telemetry'))
                stack.enter_context(patch.object(s,'_launch',side_effect=lambda fn,phase:fn(client)))
                if locked:
                    with self.assertRaisesRegex(BridgeError,'locked order'):s.prepare_analysis()
                    self.assertEqual([t['id'] for t in s.state['order']],['a','b','c'])
                else:
                    s.prepare_analysis()
                    self.assertEqual([t['id'] for t in s.state['order']],['a','c'])
                    receipt=s.state['preparationSelection']
                    self.assertEqual(receipt['fromIds'],['a','b','c'])
                    self.assertEqual(receipt['orderIds'],['a','c'])
                    self.assertEqual(receipt['skipped'][0]['trackId'],'b')
                    self.assertEqual(s.state['phase'],'ready')
                    self.assertEqual(s.state['order'][-1]['camelot'],'9A')
                    self.assertEqual(set(s.state['setlist']['sourceById']),{'a','c'})


if __name__=='__main__':unittest.main()
