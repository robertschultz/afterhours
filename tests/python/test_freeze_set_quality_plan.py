"""Prospective benchmark intake and timing, with no provider or deck activity."""

import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FREEZER = runpy.run_path(str(ROOT / 'scripts/freeze-set-quality-plan.py'))
SCORER = runpy.run_path(str(ROOT / 'scripts/evaluate-set-quality.py'))


def cue(label, span, replay=True):
    level = {'rmsDb': -12, 'bassDb': -14, 'highDb': -18, 'levelVariationDb': 1,
             'crestDb': 3, 'silentFraction': 0}
    return {
        'id': label, 'replayVerified': replay, 'entryBeat': 4, 'entrySeconds': 2,
        'exitBeat': 4 + span, 'exitSeconds': (4 + span) / 2,
        'endBeat': 20 + span, 'endSeconds': (20 + span) / 2,
        'introEndSeconds': 10, 'maxBlendBeats': 16, 'entry': level, 'exit': level,
    }


def record(slot, cues):
    sid = f'{slot:064x}'
    track = {'id': f'virtualdj:{sid}', 'engine': {'selection_id': sid},
             'catalog': {'provider': 'beatport'}, 'title': f'Record {slot:04}',
             'artist': f'Artist {slot:04}', 'genre': 'House', 'bpm': 120, 'energy': None}
    evidence = {
        'id': track['id'], 'duration': 240, 'bpm': 120, 'cueCandidates': cues,
        'energyMap': {'coverage': 1, 'reportDigest': 'a'*64}, 'points': [],
    }
    return {'track': track, 'evidence': evidence}


class FreezeSetQualityPlanTests(unittest.TestCase):
    def test_freeze_intake_requires_same_source_exact_identity_and_replay(self):
        valid = record(1, [cue('saved', 60), cue('unverified', 60, False)])
        other = record(2, [cue('saved', 60)])
        other['track']['catalog']['provider'] = 'apple-music'
        mismatch = record(3, [cue('saved', 60)])
        mismatch['track']['engine']['selection_id'] = 'b'*64
        stale = record(4, [cue('saved', 60)])
        invalid = [valid, other, mismatch, stale]
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'saved.json'
            path.write_text(json.dumps({'session': {
                'order': [row['track'] for row in invalid],
                'analysis': {'tracks': [row['evidence'] for row in invalid]},
            }}))

            def scans(evidence):
                if evidence['id'] == stale['track']['id']:
                    raise ValueError('Changed exact scan digest.')
                return []

            with patch('afterhours.cue_planner.measured_rows', side_effect=scans):
                pool = FREEZER['freeze_pool']([path], None, 'beatport', {'house'}, 120, 0)
        self.assertEqual(len(pool['recordings']), 1)
        self.assertEqual(pool['recordings'][0]['track']['id'], valid['track']['id'])
        self.assertEqual([c['id'] for c in pool['recordings'][0]['evidence']['cueCandidates']], ['saved'])
        self.assertEqual(pool['excludedCounts'], {'missing-or-changed-scan': 1})
        plan, result = FREEZER['build_plan'](pool, 'test', [60, 30])
        self.assertIsNone(plan)
        self.assertEqual(result['status'], 'insufficient-measured-coverage')
        self.assertEqual(result['recordingsWithTimedExit'], {'60': 0, '30': 1})

    def test_full_prospective_baseline_is_repeatable_and_passes_scorecard(self):
        records = [record(index, [cue('long', 120), cue('short', 60), cue('finale', 48)])
                   for index in range(1, 83)]
        pool = {'recordings': records, 'targetBpm': 120, 'provider': 'beatport',
                'allowedGenres': ['house'], 'capturedAt': 0, 'excludedCounts': {}}
        with patch('afterhours.cue_planner.measured_rows', return_value=[]):
            plan, result = FREEZER['build_plan'](pool, 'fixture-only', [60, 30])
            repeat, _ = FREEZER['build_plan'](pool, 'fixture-only', [60, 30])
        self.assertEqual(result['status'], 'sealed-offline-draft', result)
        self.assertEqual(FREEZER['encoded'](plan), FREEZER['encoded'](repeat))
        self.assertIs(plan['nativePlaybackVerifiedForPlan'], False)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'plan.json'
            path.write_text(json.dumps(plan))
            score = SCORER['read_plan'](path)
        self.assertEqual(score['timelineGapSeconds'], 0)
        self.assertEqual(score['extraPlaysByRecording'], {})
        self.assertEqual(score['leadSecondsByGenre'], {'house': 3600})
        self.assertEqual(score['energyKnownLeadSeconds'], 0)
        self.assertEqual(score['trackCount'], 80)
        self.assertEqual(plan['tracks'][-1]['finalFadeBeats'], 8)
        self.assertEqual(plan['tracks'][-1]['audibleEndSeconds'], 3600)
