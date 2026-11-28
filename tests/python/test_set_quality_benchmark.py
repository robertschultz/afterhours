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

    def test_observed_run_classifies_timing_and_quiet_without_claiming_an_hour(self):
        with TemporaryDirectory() as folder:
            prefix = Path(folder) / 'run'
            Path(f'{prefix}-owner.json').write_text(json.dumps({'id':'run','at':1000}))
            def log(name, rows):
                Path(f'{prefix}-{name}.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
            log('hour', [
                {'event':'Typed timed opening requested','entryCadence':[30,60]},
                {'event':'Endurance window','startedAt':1000},
                {'event':'Endurance heartbeat','at':'1970-01-01T00:18:30+00:00'},
            ])
            log('handoffs', [
                {'kind':'native-handoff','verified':True,'synced':True,'at':1045,'blendSeconds':4,
                 'entryIntervalSeconds':40,'targetIntervalSeconds':30,'deckReadyRelativeToTargetSeconds':5},
                {'kind':'native-handoff','verified':True,'synced':True,'at':1109,'blendSeconds':8,
                 'entryIntervalSeconds':60,'targetIntervalSeconds':60,'deckReadyRelativeToTargetSeconds':-2},
            ])
            master = [
                {'event':'master-monitor-started','owner':'run','afterStartSeconds':.2},
                {'event':'quiet-window','at':1041}, {'event':'quiet-window','at':1097},
                {'event':'master-heartbeat','capturedAudioSeconds':105,'sequenceDiscontinuities':0,
                 'clippedSamples':0,'longestBelowMinus65Seconds':.2},
            ]
            log('master', master)
            score = SCORER['read_observed_run'](prefix)
            self.assertFalse(score['completeHour'])
            self.assertEqual(score['verifiedHandoffs'], 2)
            self.assertEqual(score['timingByTarget']['30']['missesOutside2Seconds']['readyAfterTarget'], 1)
            self.assertEqual(score['timingByTarget']['60']['within2Seconds'], 1)
            self.assertEqual(score['quietWindowsDuringBlend'], 1)
            self.assertEqual(score['quietWindowsOutsideBlend'], 1)
            self.assertIsNone(score['listeningVerdict'])
            master[0]['owner'] = 'different-run'
            log('master', master)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                SCORER['read_observed_run'](prefix)

    def test_observed_overlay_distinguishes_staged_committed_and_legacy_track_names(self):
        def change(second, visible, label, state=None):
            return {'event':'Up Next overlay changed', 'at':f'1970-01-01T00:00:{second:02}+00:00',
                    'visible':visible, 'label':label, 'state':state}
        rows = [change(4, True, 'Finding a track'), change(7, True, 'One', 'staged'),
                change(10, True, 'One', 'committed'), change(14, True, 'Two'),
                change(18, False, ''), {'event':'Endurance reached', 'at':'1970-01-01T00:00:20+00:00',
                                        'missingNextSamples':1}]
        score = SCORER['observed_up_next'](rows, 1)
        self.assertEqual(score['firstSampleAfterStartSeconds'], 3)
        self.assertEqual(score['missingWhilePlayingSamples'], 1)
        self.assertEqual(score['sampledSecondsByState'], {'searching':3, 'staged':3, 'committed':4,
                                                         'named-unverified':4, 'hidden':2})

    def test_observed_variety_requires_a_contiguous_exact_recording_sequence(self):
        def handoff(outgoing, incoming, artist, incoming_artist):
            return {'verified':True, 'outgoingRecordingId':outgoing, 'incomingRecordingId':incoming,
                    'outgoingArtist':artist, 'incomingArtist':incoming_artist}
        rows = [handoff('one', 'two', 'A', 'A'), handoff('two', 'one', 'A', 'a'),
                handoff('one', 'three', 'A', None)]
        score = SCORER['observed_curatorial_variety'](rows)
        self.assertEqual((score['recordingsPlayed'], score['distinctRecordings']), (4, 3))
        self.assertEqual(score['extraPlaysByRecording'], {'one':1})
        self.assertEqual((score['knownArtistCredits'], score['distinctArtistCredits']), (3, 1))
        self.assertEqual(score['extraPlaysByArtistCredit'], {'a':2})
        self.assertEqual(score['consecutiveRepeatedCreditPairs'], 2)
        rows[1]['outgoingRecordingId'] = 'different'
        self.assertIsNone(SCORER['observed_curatorial_variety'](rows))
        self.assertIsNone(SCORER['observed_curatorial_variety']([{'verified': True}]))

    def test_observed_mix_techniques_use_verified_deck_and_mixer_readbacks(self):
        out, incoming = 'Clearing outgoing bass and lead', 'Bringing in incoming bass'
        rows = [
            {'verified':True, 'maxPhaseGapBeats':.12, 'overlapSamples':8,
             'eqOrder':[out, incoming], 'leadControl':['stems', 'stems'],
             'loopBeats':4, 'loopWraps':2, 'loopRelease':'released'},
            {'verified':True, 'overlapSamples':0, 'eqOrder':[incoming, out], 'leadControl':['frequency-eq'],
             'loopBeats':4, 'loopWraps':1, 'loopRelease':'unconfirmed'},
            {'verified':True},
            {'verified':False, 'maxPhaseGapBeats':.45, 'overlapSamples':99, 'leadControl':['frequency-eq']},
        ]
        result = SCORER['observed_mix_techniques'](rows)
        self.assertEqual((result['withPhaseReadback'], result['worstObservedPhaseGapBeats']), (1, .12))
        self.assertEqual((result['withOverlapReadback'], result['withMeasuredSignalOnBothDecksDuringCrossfade']), (2, 1))
        self.assertEqual((result['withBothBassSwapStages'], result['withOrderedBassSwapPhases']), (2, 1))
        self.assertEqual(result['leadControlHandoffs'], {'frequency-eq':1, 'stems':1})
        self.assertEqual((result['loopHandoffs'], result['loopWrappedAndReleased']), (2, 1))

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
