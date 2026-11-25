import copy
import unittest
from afterhours import music_facts


class MusicFactsTests(unittest.TestCase):
    def setUp(self):
        self.camel = {'id':'camel','title':'Camel Toe','bpm':123.001228}
        self.dawgs = {'id':'dawgs','title':'House Dawgs','bpm':126}
        self.session = {'order':[self.camel], 'candidates':[self.camel,self.dawgs],
                        'telemetry':{'sampledAt':100,'decks':[{'deck':2,'id':'camel','bpm':127.999176,'playing':False}]}}

    def test_original_and_current_deck_tempo_are_distinct(self):
        self.assertEqual(music_facts.answer('What is the tempo of Camel Toe?',self.session,101),
                         'Camel Toe is 123 BPM originally; deck B is at 128 BPM.')

    def test_model_context_uses_observed_paused_deck_not_planned_tempo(self):
        self.session['executionPlan']={'targetBpm':126,'tempoSchedule':[{'trackId':'camel','fromBpm':123,'toBpm':123}]}
        before=copy.deepcopy(self.session)
        facts=music_facts.deck_facts(self.session,101)
        self.assertEqual(facts['status'],'observed')
        self.assertEqual(facts['decks'],[{'deck':'B','trackId':'camel','title':'Camel Toe','playing':False,
                                       'currentBpm':128,'originalBpm':123,'bpmDifference':5,'pitchPercent':4.07}])
        self.assertEqual(self.session,before)

    def test_model_context_drops_stale_unavailable_and_invalid_readings(self):
        for sampled in (90,104,None,True,float('nan')):
            self.session['telemetry']['sampledAt']=sampled
            self.assertEqual(music_facts.deck_facts(self.session,101)['decks'],[])
        self.session['telemetry']['sampledAt']=100
        for values in ({'deck':True},{'deck':5},{'unavailable':True}):
            self.session['telemetry']['decks']=[{'deck':2,'id':'camel','bpm':128,**values}]
            self.assertEqual(music_facts.deck_facts(self.session,101)['decks'],[])
        self.session['telemetry']['decks']=[{'deck':2,'id':'other','bpm':float('nan'),'playing':'true','path':'private-file'}]
        row=music_facts.deck_facts(self.session,101)['decks'][0]
        self.assertIsNone(row['originalBpm']);self.assertIsNone(row['currentBpm']);self.assertIsNone(row['playing'])
        self.assertNotIn('path',row)

    def test_unqueued_candidate_is_never_the_next_track_and_does_not_mutate(self):
        before=copy.deepcopy(self.session)
        reply=music_facts.answer('What is the tempo of Camel Toe, and what is queued after it? Keep playback paused.',self.session,101)
        self.assertEqual(reply,'Camel Toe is 123 BPM originally; deck B is at 128 BPM. Nothing follows it in the current queue.')
        self.assertEqual(self.session,before)

    def test_actual_next_and_staged_update_stay_distinct(self):
        self.session['queuedUpdate']={'tracks':[self.camel,self.dawgs],'status':'preparing'}
        reply=music_facts.answer('What is after Camel Toe?',self.session,101)
        self.assertEqual(reply,'Nothing follows it in the current queue. The staged update puts House Dawgs after it; that change has not been applied.')
        self.session['order'].append(self.dawgs)
        self.assertEqual(music_facts.answer('What is after Camel Toe?',self.session,101),
                         'House Dawgs follows it in the current queue.')

    def test_held_recording_is_not_described_as_queued(self):
        self.assertEqual(music_facts.answer('What follows House Dawgs?',self.session,101),
                         'House Dawgs is not in the current queue.')

    def test_queue_question_uses_exact_queued_id_among_catalog_variants(self):
        self.session['candidates'].append({'id':'remix','title':'Camel Toe','bpm':130})
        self.assertEqual(music_facts.answer('What is the tempo of Camel Toe and what follows it?',self.session,101),
                         'Camel Toe is 123 BPM originally; deck B is at 128 BPM. Nothing follows it in the current queue.')
        self.assertIsNone(music_facts.answer('What BPM is Camel Toe?',self.session,101))

    def test_stale_or_missing_tempo_is_not_invented(self):
        self.assertEqual(music_facts.answer('What BPM is Camel Toe?',self.session,110),'Camel Toe is 123 BPM.')
        self.camel['bpm']=None
        self.assertEqual(music_facts.answer('What BPM is Camel Toe?',self.session,110),
                         'The original tempo for Camel Toe is unavailable.')

    def test_ambiguous_subject_and_mixed_commands_stay_with_agent(self):
        for text in ['What is the tempo?', 'What should follow Camel Toe?',
                     'What is the tempo of Camel Toe, then play it?', 'Play Camel Toe at 126 BPM',
                     'What is after Camel Toe and House Dawgs?']:
            self.assertIsNone(music_facts.answer(text,self.session,101),text)
        self.session['candidates'].append({'id':'remix','title':'Camel Toe','bpm':130})
        self.assertIsNone(music_facts.answer('What BPM is Camel Toe?',self.session,101))


if __name__=='__main__':unittest.main()
