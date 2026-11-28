"""Test stem evidence without native playback, provider calls, or mic input."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from afterhours import agent_loop, stem_probe, stem_profiles
from afterhours.queue_planning import transition_cost
from afterhours.vdj_catalog import identity


OUT = 'virtualdj:' + 'a'*64
IN = 'virtualdj:' + 'b'*64
VOCAL = [.8, .2, .1, .15, .2]
RHYTHM = [.02, .7, .3, .25, .08]


def window(role, at, levels):
    return {'role':role,'startSeconds':at,'durationSeconds':4,
            'levels':[levels.copy() for _ in range(stem_profiles.POINTS)]}


class StemWindowTests(unittest.TestCase):
    def test_pair_evidence_is_bound_to_exact_cues_and_changes_fit(self):
        outgoing = {'replayVerified':True,'exitSeconds':40}
        incoming = {'replayVerified':True,'entrySeconds':12}
        tracks = [{'id':OUT,'bpm':124,'camelot':'8A','genre':'House'},
                  {'id':IN,'bpm':124,'camelot':'8A','genre':'House'}]
        cues = {OUT:outgoing,IN:incoming}
        with tempfile.TemporaryDirectory() as root, patch('afterhours.stem_profiles.data_root',return_value=Path(root)):
            baseline = transition_cost(*tracks,cues)
            stem_profiles.save(OUT,[window('exit',40,VOCAL)])
            stem_profiles.save(IN,[window('entry',12,VOCAL),window('entry',20,RHYTHM)])
            result = stem_profiles.cue_pair(OUT,IN,outgoing,incoming)
            alternate = stem_profiles.cue_pair(OUT,IN,outgoing,{**incoming,'entrySeconds':20})
            self.assertEqual(result['vocalOverlapFraction'],1)
            self.assertEqual(alternate['vocalOverlapFraction'],0)
            self.assertGreater(transition_cost(*tracks,cues),baseline)
            self.assertGreater(result['scoreAdjustment'],alternate['scoreAdjustment'])
            self.assertIsNone(stem_profiles.cue_pair(OUT,IN,outgoing,{**incoming,'entrySeconds':12.5}))
            self.assertIsNone(stem_profiles.cue_pair(OUT,IN,outgoing,{**incoming,'replayVerified':False}))

            snapshot = {'order':tracks,'executionPlan':{'selections':[
                {'trackId':OUT,**outgoing},{'trackId':IN,**incoming}]}}
            evidence = agent_loop.Lookups(snapshot,tracks).transition({'fromId':OUT,'toId':IN})
            self.assertEqual(evidence['stemEvidence'],result)

    def test_rolling_candidate_with_cleaner_entrance_beats_vocal_and_bass_overlap(self):
        vocal_and_bass = [.8, .2, .1, .8, .15]
        clean_drums = [.02, .7, .4, .04, .08]
        alternate = 'virtualdj:' + 'c'*64
        cue = {'replayVerified': True, 'exitSeconds': 40}
        with tempfile.TemporaryDirectory() as root, patch('afterhours.stem_profiles.data_root', return_value=Path(root)):
            stem_profiles.save(OUT, [window('exit', 40, vocal_and_bass)])
            stem_profiles.save(IN, [window('entry', 12, vocal_and_bass)])
            stem_profiles.save(alternate, [window('entry', 20, clean_drums)])
            clashing = stem_profiles.candidate_fit(OUT, IN, cue)
            cleaner = stem_profiles.candidate_fit(OUT, alternate, cue)
            self.assertEqual(clashing['vocalOverlapFraction'], 1)
            self.assertEqual(cleaner['vocalOverlapFraction'], 0)
            self.assertGreater(clashing['lowEndOverlap'], cleaner['lowEndOverlap'])
            self.assertGreater(clashing['scoreAdjustment'], cleaner['scoreAdjustment'])
            self.assertIsNone(stem_profiles.candidate_fit(OUT, alternate, {**cue, 'exitSeconds': 60}))

    def test_sparse_or_unseparated_meter_readings_are_ignored(self):
        self.assertIsNone(stem_profiles.measure_window('entry',12,4,[(12.2,VOCAL)]))
        self.assertFalse(stem_profiles.valid_window(window('entry',12,[.5]*5)))

    def test_preparation_holds_during_active_mix_before_any_native_work(self):
        session = Mock()
        session.snapshot.return_value = {'active':True,'executionPlan':{'applied':True}}
        with self.assertRaisesRegex(ValueError,'pause'):
            stem_probe.prepare(session,[OUT,IN])
        session._launch.assert_not_called()

    def test_probe_samples_only_the_isolated_recording_and_confirms_pause(self):
        recording = 'netsearch://bp123789'
        commands = []
        state = {'time':0.,'position':0.,'playing':False}

        def query(command, kind=None):
            if command=='get_decks':return 4
            self.assertTrue(command.startswith('deck 3 '))
            return {'get_deck':3,'play':state['playing'],'volume':0,'pfl':False,
                    'is_audible':False,'get_filepath':recording,
                    'get_time elapsed absolute':state['position']*1000,
                    'has_stems "2.0"':True,'has_stems "ready"':True,
                    **{f'get_level "{name}"':level for name,level in zip(stem_profiles.STEMS,RHYTHM)}}[command[7:]]

        def send(command):
            commands.append(command)
            if command.startswith('deck 3 goto '):
                state['position'] = float(command.split()[-1].rstrip('%'))
            elif command=='deck 3 play':
                state['playing']=True
            elif command=='deck 3 pause':
                state['playing']=False
            else:
                self.fail(f'Unexpected native mutation {command}')

        def advance(seconds):
            state['time']+=seconds
            if state['playing']:state['position']+=seconds

        session = Mock()
        session.q.side_effect=lambda client,command,kind=None:query(command,kind)
        session.send.side_effect=lambda client,command:send(command)
        session.sleep.side_effect=advance
        client = Mock()
        client.query.side_effect=query
        client.send.side_effect=send
        with patch('afterhours.stem_probe.time.monotonic',side_effect=lambda:state['time']):
            row=stem_probe.probe_window(session,client,3,identity(recording),'entry',12,4,100)
        self.assertEqual(row['levels'],[RHYTHM]*stem_profiles.POINTS)
        self.assertEqual(commands[1:],['deck 3 play','deck 3 pause'])
        self.assertFalse(state['playing'])
