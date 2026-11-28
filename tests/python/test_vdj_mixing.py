"""Synthetic recipe and EQ safety tests; no audio or native connections."""
import unittest
from unittest.mock import Mock
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_mixing import CONTROLS, LEAD_CONTROLS, TransitionMixer, measured_bridge_loop, measured_recipe, transition_recipe


def track(genre='Drum & Bass',key='2A'):
    return {'genre':genre,'camelot':key}


class MixerTests(unittest.TestCase):
    def test_requested_quick_blends_round_to_full_bars_within_one_second(self):
        for seconds in (15,20,30):
            recipe=transition_recipe({}, {},'quick',60,124,quick_blend_seconds=seconds)
            self.assertEqual(recipe['fadeBeats']%4,0)
            self.assertLessEqual(abs(recipe['fadeBeats']*60/124-seconds),1)

    def test_short_trial_caps_overlap_but_preserves_genre_technique(self):
        for genre in ('Drum & Bass','House','Techno','Hip-Hop',''):
            p=transition_recipe(track(genre),track(genre),seconds=30,bpm=155)
            self.assertLessEqual(p['fadeBeats'],32)
            self.assertEqual(p['phraseBeats'],4)
            self.assertTrue(p['trial'])
        self.assertEqual(transition_recipe(track('House'),track('House'),seconds=60,bpm=120)['fadeBeats'],128)

    def test_genres_choose_different_lengths_and_overlap(self):
        for genre,length,label in [('Drum & Bass',64,'D&B bass swap'),('Progressive House',128,'House blend'),
                                   ('Techno',64,'Percussion blend'),('Hip-Hop',16,'Short bass handoff'),('',32,'Restrained handoff')]:
            with self.subTest(genre=genre):
                result=transition_recipe(track(genre),track(genre))
                self.assertEqual(result['fadeBeats'],length)
                self.assertEqual(result['label'],label)
                self.assertLessEqual(result['entryLevel'],.85)
        unknown=transition_recipe(track('Techno'),track('Hip-Hop'))
        self.assertEqual(unknown['genreFamily'],'unknown')
        self.assertFalse(unknown['tonalOverlap'])

    def test_key_changes_restrict_overlap_and_house_blend_length(self):
        for key in ('7B',None,'99A'):
            p=transition_recipe(track('House'),track('House',key))
            self.assertFalse(p['tonalOverlap']);self.assertEqual(p['fadeBeats'],64)
        for key in ('2A','2B','1A','3A'):
            p=transition_recipe(track(),track(key=key))
            self.assertTrue(p['harmonicCompatible']);self.assertFalse(p['tonalOverlap'])
        self.assertFalse(transition_recipe(track(),track(key='3B'))['tonalOverlap'])
        self.assertEqual(transition_recipe(track(),track(),'quick')['technique'],'crossfade')

    def mixer(self, stems=True):
        values={deck:{**dict.fromkeys(CONTROLS[:3]+LEAD_CONTROLS,.5),'level':.8,'play':False,'stems':stems} for deck in (1,2)}
        sent=[]
        def query(client,script,kind=None):
            if script=='mixermode':return True
            if script=='crossfader':return 0
            if script in ('fake_eq','fake_mixer'):return False
            _,deck,key=script.split(maxsplit=2)
            return values[int(deck)]['stems'] if key.startswith('has_stems') else values[int(deck)][key]
        def send(client,script):
            sent.append(script)
            if script.startswith('deck') and 'repeat_start_fade' not in script:
                for part in script.split(' & '):
                    head,amount=part.rsplit(' ',1);_,deck,key=head.split(maxsplit=2)
                    values[int(deck)][key]=float(amount.rstrip('%'))/100
        m=TransitionMixer(query,send);m.preflight(None)
        return m,values,sent

    def test_explicit_opening_restores_paused_channels_and_preserves_master_and_usable_level(self):
        m,v,sent=self.mixer();v[1].update(level=.5,eq_low_freq=.9,filter=.8)
        v[2].update(level=0,eq_high_freq=0,eq_mid_freq=0,filter=.1)
        m.initialize_opening(None)
        for deck in (1,2):
            self.assertEqual(v[deck]['level'],.5);self.assertEqual(v[deck]['filter'],.5)
            self.assertTrue(all(v[deck][key]==.5 for key in CONTROLS[:3]+LEAD_CONTROLS))
        self.assertFalse(any(' play' in s or 'master' in s for s in sent))

    def test_opening_never_normalizes_a_playing_channel(self):
        m,v,sent=self.mixer();v[2]['play']=True
        with self.assertRaises(BridgeError):m.initialize_opening(None)
        self.assertEqual(sent,[])

    def test_arm_is_silent_and_starts_with_no_incoming_bass(self):
        m,v,sent=self.mixer();m.arm(None,1,2,transition_recipe(track(),track(key='7B')))
        self.assertEqual(v[2]['eq_low_freq'],0)
        self.assertEqual(v[2]['eq_mid_freq'],.5)
        self.assertEqual([v[2][key] for key in LEAD_CONTROLS],[0,0])
        self.assertLess(v[2]['level'],v[1]['level'])
        self.assertFalse(any(' play' in script for script in sent))
        self.assertEqual(m.targets(),{'2':{'fader':.8,'low':.5},'1':{'low':0}})
        # The engine crossfades out; it does not promise an outgoing fader move.
        self.assertNotIn('fader',m.targets()['1'])
        v[2]['play']=True;before=len(sent)
        with self.assertRaises(BridgeError):m.arm(None,1,2,transition_recipe(track(),track()))
        self.assertEqual(len(sent),before)

    def test_bass_is_removed_before_other_bass_is_raised(self):
        m,v,sent=self.mixer();m.arm(None,1,2,transition_recipe(track(),track()));m.begin(None)
        m.tick(None,16)
        self.assertTrue(any('afterhours_out_low' in s for s in sent))
        m.tick(None,24)
        self.assertFalse(any('afterhours_in_low' in s for s in sent))
        v[1]['eq_low_freq']=0;m.tick(None,24.5)
        self.assertTrue(any('afterhours_in_low' in s for s in sent))
        count=len(sent);m.tick(None,25)
        self.assertEqual(len(sent),count)

    def test_measured_bright_pair_arms_silent_treble_cut_and_restores_it(self):
        mixer, values, sent=self.mixer()
        bright={'rmsDb':-12,'highDb':-25}
        plan={'transitions':[transition_recipe(track(),track())],
              'selections':[{'replayVerified':True,'exit':bright},{'replayVerified':True,'entry':bright}]}
        recipe=measured_recipe(plan,0)
        self.assertNotIn('incomingHighEq',plan['transitions'][0])
        mixer.arm(None,1,2,recipe)
        self.assertEqual(values[2]['eq_high_freq'],.32)
        self.assertEqual(values[1]['eq_high_freq'],.5)
        mixer.begin(None)
        self.assertTrue(any("afterhours_in_high' 0.320000 0.500000 48bt" in script for script in sent))

    def test_measured_bridge_is_bound_to_the_verified_exit_and_room_for_a_wrap(self):
        outgoing={'exitBeat':128,'loopBridgeBeats':8,'replayVerified':True}
        plan={'selections':[outgoing]}
        recipe={'genreFamily':'house'}
        self.assertEqual(measured_bridge_loop(plan,0,recipe,{'mixAtBeat':128,'fadeBeats':16}),8)
        self.assertIsNone(measured_bridge_loop(plan,0,recipe,{'mixAtBeat':132,'fadeBeats':16}))
        self.assertIsNone(measured_bridge_loop(plan,0,recipe,{'mixAtBeat':128,'fadeBeats':8}))
        self.assertIsNone(measured_bridge_loop(plan,0,{'genreFamily':'short'},{'mixAtBeat':128,'fadeBeats':16}))
        outgoing['replayVerified']=False
        self.assertIsNone(measured_bridge_loop(plan,0,recipe,{'mixAtBeat':128,'fadeBeats':16}))

    def test_late_step_stops_without_bursting_missed_mutations(self):
        m,v,sent=self.mixer();m.arm(None,1,2,transition_recipe(track(),track()));m.begin(None)
        count=len(sent)
        with self.assertRaises(BridgeError):m.tick(None,30)
        self.assertEqual(len(sent),count)

    def test_two_bar_handoff_waits_for_native_bass_completion(self):
        m,v,sent=self.mixer()
        recipe={**transition_recipe(track(),track(key='7B')),'fadeBeats':8,'technique':'short-handoff'}
        m.arm(None,2,1,recipe);m.begin(None)
        m.tick(None,2.2)
        # Measured native regression: bass still above threshold at 3.64 beats.
        v[2]['eq_low_freq']=.076923;m.tick(None,3.64)
        self.assertFalse(any('afterhours_in_low' in script for script in sent))
        v[2]['eq_low_freq']=0;m.tick(None,4.77)
        self.assertEqual(sum('afterhours_in_low' in script for script in sent),1)
        m.tick(None,4.9)
        self.assertEqual(sum('afterhours_in_low' in script for script in sent),1)

    def test_short_handoff_dependency_deadline_remains_bounded(self):
        m,v,sent=self.mixer()
        recipe={**transition_recipe(track(),track(key='7B')),'fadeBeats':8}
        m.arm(None,2,1,recipe);m.begin(None);m.tick(None,2.2)
        v[2]['eq_low_freq']=0;count=len(sent)
        # Even with bass clear, an overdue stage must stop before any catch-up.
        with self.assertRaises(BridgeError):m.tick(None,5.1)
        self.assertEqual(len(sent),count)

    def test_takeover_cancels_owned_ramps_without_resetting_live_eq(self):
        m,v,sent=self.mixer();m.arm(None,1,2,transition_recipe(track(),track()));m.begin(None)
        client=Mock();m.cancel_ramps(client)
        script=client.send.call_args.args[0]
        self.assertIn("deck 2 repeat_stop 'afterhours_in_level'",script)
        self.assertNotIn('eq_',script);self.assertNotIn(' play',script)
        m.cancel_ramps(client);client.send.assert_called_once()

    def test_reset_requires_paused_deck_and_preflight_rejects_nonflat_eq(self):
        m,v,sent=self.mixer();v[1]['play']=True
        with self.assertRaises(BridgeError):m.reset_paused(None,1)
        self.assertFalse(sent)
        v[1]['play']=False;v[1]['eq_mid_freq']=.1
        with self.assertRaises(BridgeError):m.preflight(None)

    def test_resume_restores_only_the_confirmed_silent_channel(self):
        m,v,sent=self.mixer();baseline={k:dict(row) for k,row in m.baselines.items()}
        m.arm(None,1,2,transition_recipe(track(),track()))
        v[1]['play']=True;sent.clear();query=m.q
        m.q=lambda c,script,kind=None:0 if script=='crossfader' else query(c,script,kind)
        m.resume_preflight(None,1,baseline)
        self.assertEqual(len(sent),1);self.assertNotIn('deck 1',sent[0])
        self.assertEqual(m.read(None,2),baseline[2]);self.assertTrue(v[1]['play'])

    def test_resume_rejects_changed_audible_controls_before_any_write(self):
        m,v,sent=self.mixer();v[1]['play']=True;v[1]['eq_mid_freq']=.2
        with self.assertRaisesRegex(BridgeError,'playing mixer'):m.resume_preflight(None,1,m.baselines)
        self.assertFalse(sent)

    def test_explicit_handback_preserves_live_controls_and_fades_from_their_actual_values(self):
        for stems in (False,True):
            m,v,sent=self.mixer(stems=stems);baseline={k:dict(row) for k,row in m.baselines.items()}
            v[1].update(play=True,eq_low_freq=.77,eq_mid_freq=.86,eq_high_freq=1,level=.48)
            v[1].update({'stem Vocal':.4,'stem Instru':.3})
            actual=m.read(None,1)
            m.resume_preflight(None,1,baseline,adopt_playing=True)
            self.assertEqual(m.read(None,1),actual);self.assertEqual(m.baselines,baseline)
            self.assertTrue(all('deck 1' not in command for command in sent))
            recipe=transition_recipe(track(),track())
            m.arm(None,1,2,recipe);m.validate_armed(None)
            self.assertEqual(m.read(None,1),actual)
            m.tick(None,recipe['fadeBeats']/4)
            self.assertTrue(any("'afterhours_out_low' 0.770000 0.000000" in command for command in sent))
            if stems:
                self.assertTrue(any("'afterhours_out_vocal' 0.400000 0.000000" in command for command in sent))
                self.assertTrue(any("'afterhours_out_instru' 0.300000 0.000000" in command for command in sent))
            else:
                self.assertTrue(any("'afterhours_out_mid' 0.860000 0.000000" in command for command in sent))

    def test_explicit_handback_rechecks_new_movements_and_never_raises_a_muted_fader(self):
        m,v,sent=self.mixer();v[1].update(play=True,level=0)
        with self.assertRaisesRegex(BridgeError,'fader is down'):
            m.resume_preflight(None,1,m.baselines,adopt_playing=True)
        self.assertFalse(sent)
        v[1].update(level=.48,eq_mid_freq=.86)
        m.resume_preflight(None,1,m.baselines,adopt_playing=True)
        v[1]['eq_mid_freq']=.5;count=len(sent)
        with self.assertRaisesRegex(BridgeError,'controls changed'):
            m.arm(None,1,2,transition_recipe(track(),track()))
        self.assertEqual(len(sent),count)

    def test_resume_rejects_overlap_or_missing_original_baselines(self):
        m,v,sent=self.mixer();v[1]['play']=True;query=m.q
        m.q=lambda c,script,kind=None:.5 if script=='crossfader' else query(c,script,kind)
        with self.assertRaisesRegex(BridgeError,'silent'):m.resume_preflight(None,1,m.baselines)
        with self.assertRaises(BridgeError):m.resume_preflight(None,1,None)
        self.assertFalse(sent)

    def test_endpoint_requires_neutral_incoming_eq_and_baseline_level(self):
        m,v,sent=self.mixer();m.arm(None,1,2,transition_recipe(track(),track()))
        with self.assertRaises(BridgeError):m.verify_handoff(None)
        v[2].update(eq_low_freq=.5,eq_mid_freq=.5,eq_high_freq=.5,level=.8)
        v[2].update(dict.fromkeys(LEAD_CONTROLS,.5));m.done.update(('out_lead','in_lead','in_low'))
        m.verify_handoff(None)

    def test_stem_handoff_waits_for_both_outgoing_lead_parts(self):
        m,v,sent=self.mixer();recipe={**transition_recipe(track('House'),track('House'),seconds=30,bpm=125),'fadeBeats':32}
        m.arm(None,1,2,recipe);m.validate_armed(None);m.begin(None)
        m.tick(None,8);v[1]['eq_low_freq']=0;m.tick(None,12)
        v[1]['stem Vocal']=0;v[1]['stem Instru']=.1;m.tick(None,16)
        self.assertFalse(any('afterhours_in_vocal' in script for script in sent))
        v[1]['stem Instru']=0;m.tick(None,16.4)
        self.assertEqual(sum('afterhours_in_vocal' in script for script in sent),1)
        self.assertEqual(sum('afterhours_in_instru' in script for script in sent),1)

    def test_unavailable_stems_use_explicit_eq_guard_without_stem_mutation(self):
        m,v,sent=self.mixer(stems=False);recipe=transition_recipe(track(),track());m.arm(None,1,2,recipe);m.begin(None)
        self.assertEqual(recipe['leadControl'],'frequency-eq');self.assertEqual(v[2]['eq_mid_freq'],0)
        m.tick(None,16);v[1]['eq_low_freq']=0;m.tick(None,24);m.tick(None,32)
        self.assertFalse(any('afterhours_in_mid' in script for script in sent))
        v[1]['eq_mid_freq']=0;m.tick(None,32.5)
        self.assertTrue(any('afterhours_in_mid' in script for script in sent))
        self.assertFalse(any('stem ' in script for script in sent))

    def test_stem_loss_before_play_rearms_only_the_silent_deck_for_eq(self):
        mixer,values,sent=self.mixer()
        recipe=transition_recipe(track(),track())
        mixer.arm(None,1,2,recipe)
        before=values[1].copy()
        values[2]['stems']=False
        sent.clear()
        self.assertTrue(mixer.fallback_from_unready_stems(None))
        mixer.validate_armed(None)
        self.assertEqual(values[1],before)
        self.assertEqual(values[2]['eq_mid_freq'],0)
        self.assertEqual([values[2][name] for name in LEAD_CONTROLS],[.5,.5])
        self.assertEqual(recipe['leadControl'],'frequency-eq')
        self.assertEqual(len(sent),1)
        self.assertTrue(all(part.startswith('deck 2 ') for part in sent[0].split(' & ')))
        self.assertFalse(mixer.fallback_from_unready_stems(None))

    def test_stem_fallback_preserves_manual_mixer_changes(self):
        for field,value in (('play',True),('eq_high_freq',.2)):
            with self.subTest(field=field):
                mixer,values,sent=self.mixer()
                mixer.arm(None,1,2,transition_recipe(track(),track()))
                values[2].update(stems=False,**{field:value})
                sent.clear()
                with self.assertRaises(BridgeError):mixer.fallback_from_unready_stems(None)
                self.assertEqual(sent,[])

    def test_stem_loss_or_manual_edit_prevents_mix_and_late_lead_cannot_catch_up(self):
        m,v,sent=self.mixer();m.arm(None,1,2,transition_recipe(track(),track()));before=len(sent)
        v[2]['stems']=False
        with self.assertRaisesRegex(BridgeError,'no longer ready'):m.validate_armed(None)
        self.assertEqual(len(sent),before);v[2]['stems']=True;v[2]['stem Vocal']=.5
        with self.assertRaisesRegex(BridgeError,'controls changed'):m.validate_armed(None)
        v[2]['stem Vocal']=0;m.begin(None);m.tick(None,16);v[1]['eq_low_freq']=0;m.tick(None,24);m.tick(None,32)
        count=len(sent)
        with self.assertRaisesRegex(BridgeError,'timing window'):m.tick(None,36.1)
        self.assertEqual(len(sent),count)


if __name__=='__main__':unittest.main()
