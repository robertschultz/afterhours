"""Exercise the native rehearsal evidence checks using a simulated booth."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'verify-timed-set.py'
SPEC = importlib.util.spec_from_file_location('verify_timed_set', SCRIPT)
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)
PROVIDERS = ['beatport', 'apple-music', 'beatport', 'beatport', 'apple-music']
ENTRIES = [0, 60, 90, 150, 180]
START = 1000


class TimedSetEvidenceTests(unittest.TestCase):
    def run_trial(self, *, replace_at=None, stale_index=None, discontinuity=False):
        ids = ['recording-' + str(index) for index in range(len(ENTRIES))]
        order = [{'id':tid, 'title':tid, 'catalog':{'provider':provider}}
                 for tid, provider in zip(ids, PROVIDERS, strict=True)]
        mix = {'verified':True, 'samples':[{'phaseDifference':.01,'bpmA':125,'bpmB':125}] * 4,
               'mixerSamples':[{'leadControl':'frequency-eq',
                                'outgoing':{'eq_low_freq':0,'eq_mid_freq':0,'eq_high_freq':.2},
                                'incoming':{'eq_low_freq':.5,'eq_mid_freq':.5}}],
               'loop':{'beats':4,'wraps':1,'released':True}, 'timing':{'fadeBeats':8}}
        transitions = [{**mix, 'at':START+entry, 'from':ids[index], 'to':ids[index+1]}
                       for index, entry in enumerate(ENTRIES[1:])]
        if discontinuity:
            transitions[-1]['from'] = ids[0]
        elapsed = 0

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        def request(*args):
            index = max(i for i, entry in enumerate(ENTRIES) if entry <= elapsed)
            return {'id':'same-session', 'startedAt':START+(replace_at is not None and elapsed >= replace_at),
                    'index':index, 'order':order, 'active':True, 'phase':'playing',
                    'entryCadence':[60,30], 'transitions':transitions[:index],
                    'telemetry':{'sampledAt':START+elapsed-(4 if index == stale_index else 0),
                                 'decks':[{'id':ids[index],'playing':True,'level':.4}]}}

        fake_time = SimpleNamespace(time=lambda:START+elapsed, monotonic=lambda:elapsed, sleep=sleep)
        args = ['verify-timed-set.py','--seconds','190','--provider-sequence',','.join(PROVIDERS)]
        with patch.object(sys, 'argv', args), patch.object(VERIFIER, 'time', fake_time), \
             patch.object(VERIFIER, 'request', side_effect=request), patch.object(VERIFIER, 'emit') as emit:
            result = VERIFIER.main()
            return result, emit.call_args.kwargs

    def test_mixed_provider_result_requires_ordered_handoffs_and_live_meters(self):
        result, summary = self.run_trial()
        self.assertEqual(result, 0)
        self.assertEqual(summary['providerSequence'], PROVIDERS)
        self.assertEqual(summary['passedHandoffs'], 4)
        self.assertEqual(summary['nativeMeterTracks'], 5)
        self.assertEqual(summary['maxObservationGapSeconds'], 1)

        result, summary = self.run_trial(stale_index=2)
        self.assertEqual(result, 1)
        self.assertEqual(summary['nativeMeterTracks'], 4)
        self.assertGreater(summary['maxObservationGapSeconds'], 4)

    def test_same_session_restart_and_noncontiguous_receipts_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'replaced the observed session'):
            self.run_trial(replace_at=61)
        with self.assertRaisesRegex(ValueError, 'contiguous playback sequence'):
            self.run_trial(discontinuity=True)
