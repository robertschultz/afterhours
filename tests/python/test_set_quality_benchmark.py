"""Regression tests for the offline mix-quality benchmark."""

import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest


SCORER = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts' / 'evaluate-set-quality.py'))


class SetQualityBenchmarkTests(unittest.TestCase):
    def test_lead_time_does_not_double_count_overlaps_or_missing_energy(self):
        plan = {
            'schemaVersion': 1, 'targetSeconds': 3600, 'setId': 'a', 'briefId': 'house',
            'catalogSnapshotId': 'fixed', 'cadenceSeconds': [60, 30],
            'allowedGenres': ['house'],
            'energyCurve': [{'startSeconds': 0, 'target': .5}, {'startSeconds': 60, 'target': .75}],
            'tracks': [
                {'recordingId': 'one', 'artist': 'Artist', 'genre': 'HOUSE', 'energy': .25,
                 'entrySeconds': 0, 'audibleEndSeconds': 64, 'cueSeconds': 30},
                {'recordingId': 'two', 'artist': 'Artist', 'genre': 'dance',
                 'entrySeconds': 60, 'audibleEndSeconds': 90, 'cueSeconds': 0},
                {'recordingId': 'one', 'artist': 'Artist', 'genre': None, 'energy': .75,
                 'entrySeconds': 100, 'audibleEndSeconds': 3600, 'cueSeconds': 30},
            ],
        }
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'plan.json'
            path.write_text(json.dumps(plan))
            score = SCORER['read_plan'](path)
        self.assertEqual(score['timelineGapSeconds'], 10)
        self.assertEqual(score['leadSecondsByGenre'], {'house': 60, 'dance': 30})
        self.assertEqual(score['outOfBriefGenreLeadSeconds'], 30)
        self.assertEqual(score['unclassifiedLeadSeconds'], 3500)
        self.assertEqual(score['energyKnownLeadSeconds'], 3560)
        self.assertEqual(score['energyAndGenreByPhase'][0]['energyMeanAbsoluteError'], .25)
        self.assertEqual(score['energyAndGenreByPhase'][1]['energyMeanAbsoluteError'], 0)
        self.assertEqual(score['extraPlaysByRecording'], {'one': 1})
        self.assertEqual(score['extraPlaysByArtistCredit'], {'artist': 2})
        self.assertEqual(score['trackDwell'][0]['audibleSeconds'], 64)
        self.assertEqual(score['plannedOverlapsSeconds'], [4, 0])
        self.assertEqual(score['timingByTarget']['30']['within5Seconds'], 0)
        self.assertEqual(score['timingByTarget']['60']['within5Seconds'], 1)
        self.assertIsNone(score['listeningVerdict'])

    def test_truncated_receipts_cannot_claim_complete_cadence(self):
        with TemporaryDirectory() as folder:
            prefix = Path(folder) / 'interrupted'
            summary = {
                'test': {'event': 'Interrupted', 'seconds': 100}, 'confirmedHandoffs': 2,
                'syncObserved': 2, 'handoffsWithMixerSamples': 2,
                'loopWrappedAndReleased': 1, 'stemHandoffs': 0, 'cadenceSeconds': [60, 30],
            }
            Path(f'{prefix}-owner.json').write_text(json.dumps({'at': 1000}))
            Path(f'{prefix}-summary.json').write_text(json.dumps(summary))
            Path(f'{prefix}-receipts.json').write_text(json.dumps([{'verified': True, 'at': 1060}]))
            score = SCORER['read_trial'](prefix)
        self.assertFalse(score['completeHour'])
        self.assertIsNone(score['timingByTarget'])
        self.assertIsNone(score['clippedSamplesThroughCaptureStop'])
        self.assertIsNone(score['listeningVerdict'])

    def test_judge_reports_abstention_and_order_swap_on_held_out_pairs(self):
        human = [{'reviewerId': 'one', 'choice': 'a'}, {'reviewerId': 'two', 'choice': 'a'}]
        base = {'briefId': 'house', 'dimension': 'transition-sound', 'humanLabels': human,
                'split': 'held-out', 'judgeChoice': 'a', 'swappedJudgeChoice': 'b'}
        packet = {'schemaVersion': 1, 'judgeVersion': 'v1', 'comparisons': [
            {**base, 'comparisonId': 'match'},
            {**base, 'comparisonId': 'swap-biased', 'swappedJudgeChoice': 'a'},
            {**base, 'comparisonId': 'abstained', 'judgeChoice': None, 'swappedJudgeChoice': None},
            {**base, 'comparisonId': 'during-training', 'split': 'development'},
            {**base, 'comparisonId': 'human-disagreement',
             'humanLabels': [human[0], {'reviewerId': 'two', 'choice': 'b'}]},
        ]}
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'judgments.json'
            path.write_text(json.dumps(packet))
            score = SCORER['read_judgments'](path)['overall']
        self.assertEqual(score['comparisonCount'], 4)
        self.assertEqual(score['nonTiedHumanConsensus'], 3)
        self.assertEqual(score['judgeAbstentionsOnConsensus'], 1)
        self.assertEqual(score['agreementOverAllConsensus'], .667)
        self.assertEqual(score['reviewedOrderSwaps'], 3)
        self.assertEqual(score['inconsistentOrderSwaps'], 1)
