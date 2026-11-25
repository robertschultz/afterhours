"""Verified preview omissions keep the order and never override track locks."""
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock,patch
from afterhours.cue_planner import LimitedAudio
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_session import DJSession,RecordingUnavailable


class LimitedPreparationTests(unittest.TestCase):
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
