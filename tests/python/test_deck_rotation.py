"""Which deck a record plays on, and which side of the crossfader that is. Pure rules; no engine."""
import unittest

from afterhours import deck_rotation as rotation
from afterhours.dj_bridge import BridgeError
from test_native_handoff import Peer
from afterhours.vdj_mixing import MixerControlChanged


class RotationRuleTests(unittest.TestCase):
    def test_four_decks_are_used_in_order_and_two_decks_alternate(self):
        self.assertEqual([rotation.deck_for(i, rotation.FOUR) for i in range(6)], [1, 2, 3, 4, 1, 2])
        self.assertEqual([rotation.deck_for(i, rotation.TWO) for i in range(5)], [1, 2, 1, 2, 1])
        self.assertEqual([rotation.following(d, rotation.FOUR) for d in (1, 2, 3, 4)], [2, 3, 4, 1])
        self.assertEqual([rotation.following(d, rotation.TWO) for d in (1, 2)], [2, 1])
        self.assertEqual([rotation.previous(d, rotation.FOUR) for d in (1, 2, 3, 4)], [4, 1, 2, 3])

    def test_odd_decks_are_on_the_left_and_even_decks_on_the_right(self):
        self.assertEqual([rotation.side(d) for d in (1, 2, 3, 4)], ['left', 'right', 'left', 'right'])
        self.assertEqual([rotation.crossfader_end(d) for d in (1, 2, 3, 4)], [0.0, 1.0, 0.0, 1.0])
        # Every handoff in either rotation crosses the fader.
        for decks in (rotation.TWO, rotation.FOUR):
            for d in decks:
                self.assertNotEqual(rotation.side(d), rotation.side(rotation.following(d, decks)))

    def test_decks_have_letters_and_a_pair_leaves_the_others_spare(self):
        self.assertEqual(''.join(rotation.letter(d) for d in (1, 2, 3, 4)), 'ABCD')
        self.assertEqual(rotation.spares(1, 2, rotation.FOUR), (3, 4))
        self.assertEqual(rotation.spares(2, 3, rotation.FOUR), (4, 1))    # the next to play comes first
        self.assertEqual(rotation.spares(4, 1, rotation.FOUR), (2, 3))
        self.assertEqual(rotation.spares(1, 2, rotation.TWO), ())

    def test_only_a_real_deck_and_a_real_pair_are_accepted(self):
        for bad in (0, 5, '1', None, True, 1.0):
            with self.assertRaises(ValueError):
                rotation.side(bad)
        self.assertTrue(rotation.pair(2, 3, rotation.FOUR)); self.assertTrue(rotation.pair(4, 1, rotation.FOUR))
        self.assertFalse(rotation.pair(1, 3, rotation.FOUR))               # same side: not a crossfade
        self.assertFalse(rotation.pair(2, 1, rotation.FOUR))               # backwards in a four-deck rotation
        self.assertFalse(rotation.pair(2, 3, rotation.TWO))
        self.assertTrue(rotation.pair(2, 1, rotation.TWO))

    def test_the_rotation_follows_the_layout(self):
        self.assertEqual(rotation.for_layout(4), rotation.FOUR); self.assertEqual(rotation.for_layout(6), rotation.FOUR)
        self.assertEqual(rotation.for_layout(2), rotation.TWO); self.assertEqual(rotation.for_layout(None), rotation.TWO)


class FourDeckPeer(Peer):
    """The handoff peer with any outgoing/incoming pair."""
    def __init__(self, outgoing, incoming):
        super().__init__(1, 3)
        self.outgoing = outgoing; self.incoming = incoming; self.master = incoming
        self.cross = rotation.crossfader_end(outgoing)
        self.playing = {deck: deck == outgoing for deck in (1, 2, 3, 4)}
        self.bpm = {1: 127, 2: 127, 3: 127, 4: 127}
        self.assigned = {1: 'left', 2: 'right', 3: 'left', 4: 'right'}
        self.queried = []

    def query(self, client, script, kind=None):
        self.queried.append(script)
        if script == 'crossfader': return self.cross
        _, number, key = script.split(maxsplit=2); deck = int(number)
        if key.startswith('cross_assign'): return self.assignments and key == f"cross_assign '{self.assigned[deck]}'"
        if key == 'masterdeck': return self.master_ack and self.master == deck
        if key == 'play': return self.playing[deck]
        raise AssertionError(script)


class FourDeckHandoffTests(unittest.TestCase):
    def test_every_step_of_the_rotation_fades_to_the_incoming_decks_side(self):
        for outgoing, incoming in ((1, 2), (2, 3), (3, 4), (4, 1)):
            with self.subTest(outgoing=outgoing, incoming=incoming):
                p = FourDeckPeer(outgoing, incoming); h = p.handoff(); h.dispatch(p, outgoing, incoming, 16, decks=rotation.FOUR)
                self.assertEqual(p.master, outgoing)
                self.assertEqual({d for d in (1, 2, 3, 4) if p.playing[d]}, {outgoing, incoming})
                end = '1.000000' if incoming in (2, 4) else '0.000000'
                self.assertTrue(p.sent[-1].startswith(f'deck {outgoing} repeat_start_fade')); self.assertIn(f' {end} 16bt & crossfader', p.sent[-1])
                others = {1, 2, 3, 4} - {outgoing, incoming}
                self.assertFalse(any(f'deck {d} ' in s for d in others for s in p.sent))     # no command leaves the pair
                # The side of both decks is confirmed before anything is started.
                self.assertIn(f"deck {outgoing} cross_assign '{rotation.side(outgoing)}'", p.queried)
                self.assertIn(f"deck {incoming} cross_assign '{rotation.side(incoming)}'", p.queried)

    def test_a_deck_assigned_to_the_wrong_side_stops_the_handoff_before_any_play(self):
        p = FourDeckPeer(2, 3); p.assigned[3] = 'right'
        with self.assertRaisesRegex(MixerControlChanged, 'assignments'):
            p.handoff().dispatch(p, 2, 3, 16, decks=rotation.FOUR)
        self.assertEqual(p.sent, [])

    def test_another_deck_playing_stops_the_handoff_before_any_play(self):
        p = FourDeckPeer(2, 3); p.playing[1] = True      # A shares the left side with the incoming C
        with self.assertRaisesRegex(MixerControlChanged, 'Another deck'):
            p.handoff().dispatch(p, 2, 3, 16, decks=rotation.FOUR)
        self.assertEqual(p.sent, [])

    def test_pairs_outside_the_rotation_are_refused(self):
        for outgoing, incoming, decks in ((1, 3, rotation.FOUR), (2, 1, rotation.FOUR), (3, 3, rotation.FOUR), (2, 3, rotation.TWO), (1, 5, rotation.FOUR)):
            p = FourDeckPeer(1, 2)
            with self.assertRaisesRegex(BridgeError, 'Invalid addressed transition'):
                p.handoff().dispatch(p, outgoing, incoming, 16, decks=decks)
            self.assertEqual(p.sent, [])

    def test_two_deck_dispatch_is_unchanged(self):
        for outgoing in (1, 2):
            p = Peer(outgoing, 3); p.handoff().dispatch(p, outgoing, 3 - outgoing, 16)
            self.assertTrue(p.playing[1] and p.playing[2])



class FourDeckMixerTests(unittest.TestCase):
    """Decks C and D join the automatic mixer when they first become the incoming deck."""
    def mixer(self):
        from afterhours.vdj_mixing import CONTROLS, LEAD_CONTROLS, TransitionMixer
        neutral = dict.fromkeys(CONTROLS[:3] + LEAD_CONTROLS, .5)
        values = {deck: {**neutral, 'level': .8, 'play': False, 'stems': False, 'filter': .5} for deck in (1, 2, 3, 4)}
        state = {'crossfader': 0.0}; sent = []
        def query(client, script, kind=None):
            if script == 'mixermode': return True
            if script == 'crossfader': return state['crossfader']
            if script in ('fake_eq', 'fake_mixer'): return False
            _, deck, key = script.split(maxsplit=2)
            return values[int(deck)]['stems'] if key.startswith('has_stems') else values[int(deck)][key]
        def send(client, script):
            sent.append(script)
            if script.startswith('deck') and 'repeat_start_fade' not in script:
                for part in script.split(' & '):
                    head, amount = part.rsplit(' ', 1); _, deck, key = head.split(maxsplit=2)
                    values[int(deck)][key] = float(amount.rstrip('%')) / 100
        m = TransitionMixer(query, send); m.preflight(None)
        return m, values, sent, state

    def recipe(self):
        from afterhours.vdj_mixing import transition_recipe
        return transition_recipe({'genre': 'House', 'camelot': '8A'}, {'genre': 'House', 'camelot': '8A'})

    def test_the_opening_pair_is_all_the_preflight_touches(self):
        m, values, sent, _ = self.mixer()
        self.assertEqual(sorted(m.baselines), [1, 2]); self.assertEqual(sent, [])

    def test_a_new_incoming_deck_is_made_neutral_at_the_playing_level_while_silent(self):
        m, values, sent, state = self.mixer()
        values[3].update(level=0.0, eq_low_freq=.9, filter=.2)       # left as an analysis deck: fader down
        values[2]['play'] = True; state['crossfader'] = 1.0          # B is playing; C is closed off on the left
        m.arm(None, 2, 3, self.recipe())
        self.assertEqual(m.baselines[3]['level'], .8); self.assertEqual(values[3]['filter'], .5)
        self.assertEqual(values[3]['eq_low_freq'], 0)                # armed for the blend: no incoming bass
        self.assertFalse(any('deck 1 ' in s or 'deck 4 ' in s for s in sent))
        self.assertFalse(any(' play' in s for s in sent))

    def test_a_new_deck_that_could_be_heard_is_not_touched(self):
        m, values, sent, state = self.mixer()
        values[2]['play'] = True
        for crossfader, playing in ((0.4, False), (1.0, True)):      # the fader is open to C, or C is already playing
            state['crossfader'] = crossfader; values[3]['play'] = playing; sent.clear()
            with self.assertRaises(BridgeError):
                m.arm(None, 2, 3, self.recipe())
            self.assertEqual(sent, []); self.assertNotIn(3, m.baselines)

    def test_resume_knows_the_next_deck_in_a_four_deck_rotation(self):
        m, values, sent, state = self.mixer()
        saved = {deck: dict(m.baselines[1]) for deck in (1, 2, 3)}
        values[3]['play'] = True; state['crossfader'] = 0.0          # C is playing on the left; D is next and never used
        values[4].update(level=0.0)
        fresh = type(m)(m.q, m.send)
        fresh.resume_preflight(None, 3, saved, decks=rotation.FOUR)
        self.assertEqual(fresh.baselines[4]['level'], .8)            # D joined at the playing level, silently
        self.assertFalse(any('deck 3 ' in s for s in sent))          # the audible channel was not changed
        state['crossfader'] = 1.0
        with self.assertRaisesRegex(BridgeError, 'not confirmed silent'):
            type(m)(m.q, m.send).resume_preflight(None, 3, saved, decks=rotation.FOUR)



class TelemetryPeer:
    """Four decks answering the read-only queries telemetry makes."""
    def __init__(self, playing=1, crossfader=0.0):
        self.calls = []; self.crossfader = crossfader; self.broken = set()
        self.decks = {n: {'loaded': 1, 'get_beatpos': 32, 'get_beat_bar': .25, 'get_filepath': f'track-{n}', 'get_title': f'Track {n}',
                          'get_artist': 'Artist', 'play': int(n == playing), 'get_bpm': 125, 'get_time elapsed absolute': 15000,
                          'get_vu_meter': 0, 'is_sync': 0} for n in (1, 2, 3, 4)}

    def query(self, script, kind=None):
        self.calls.append(script)
        if script == 'crossfader': return self.crossfader
        _, number, key = script.split(' ', 2); deck = int(number)
        if deck in self.broken: raise BridgeError('No answer')
        if 'eq_' in key: return .5
        if key not in self.decks[deck]: raise BridgeError('Unsupported simulated reading')
        return self.decks[deck][key]


class FourDeckTelemetryTests(unittest.TestCase):
    def session(self, decks=rotation.FOUR):
        import threading
        from afterhours.vdj_session import DJSession
        s = DJSession(threading.Lock()); s.decks = decks
        return s

    def read(self, s, peer, **kw):
        from unittest.mock import patch
        with patch('afterhours.vdj_controls.bounded_controls', return_value={'sampledAt': 0}), patch('afterhours.vdj_controls.live_controls', side_effect=lambda c, d, v: v), \
             patch('afterhours.vdj_controls.track_id', side_effect=lambda c, d: 'virtualdj:' + __import__('afterhours.vdj_catalog', fromlist=['identity']).identity(f'track-{d}')):
            return s.telemetry(peer, observing=True, **kw)

    def test_a_two_deck_session_still_reads_only_a_and_b(self):
        s = self.session(rotation.TWO); p = TelemetryPeer(); t = self.read(s, p)
        self.assertEqual([d['deck'] for d in t['decks']], [1, 2])
        self.assertFalse(any(c.startswith(('deck 3 ', 'deck 4 ')) for c in p.calls))

    def test_a_four_deck_session_reports_every_deck_in_order(self):
        s = self.session(); p = TelemetryPeer(playing=3); t = self.read(s, p)
        self.assertEqual([d['deck'] for d in t['decks']], [1, 2, 3, 4])
        self.assertEqual([d['playing'] for d in t['decks']], [False, False, True, False])
        self.assertTrue(all(d['id'].startswith('virtualdj:') for d in t['decks']))
        self.assertEqual(rotation.row(t, 3)['deck'], 3)

    def test_decks_outside_the_playing_pair_are_read_lightly(self):
        s = self.session(); s.pair_decks = (2, 3); p = TelemetryPeer(playing=2, crossfader=1.0); t = self.read(s, p)
        self.assertNotIn('standby', t['decks'][1]); self.assertNotIn('standby', t['decks'][2])
        for row in (t['decks'][0], t['decks'][3]):
            self.assertTrue(row['standby']); self.assertIn('playing', row); self.assertIn('id', row); self.assertNotIn('beatPosition', row)
        outside = [c for c in p.calls if c.startswith(('deck 1 ', 'deck 4 '))]
        self.assertLessEqual(len(outside), 2 * 5)                       # a handful of reads, never the full clock, EQ and console set
        self.assertFalse(any('eq_' in c or 'get_beatpos' in c for c in outside))

    def test_the_audible_decks_clock_must_be_available(self):
        s = self.session(); s.pair_decks = (2, 3); s.active_deck = 2
        p = TelemetryPeer(playing=2, crossfader=1.0); p.broken = {2}
        with self.assertRaisesRegex(BridgeError, 'audible deck clock'):
            self.read(s, p, preloading=True)
        p.broken = {4}                                                  # D is on the audible side but is not the playing deck
        t = self.read(s, p, preloading=True)
        self.assertTrue(t['decks'][3]['unavailable']); self.assertEqual(t['decks'][3]['deck'], 4)



class FourDeckPairTests(unittest.TestCase):
    """Go, Skip and Resume find the playing and cued decks anywhere in the rotation."""
    def snapshot(self, playing=3, index=2, crossfader=None, paused=False):
        import time
        order = [{'id': f'r{i}'} for i in range(6)]
        on = {rotation.deck_for(i, rotation.FOUR): order[i]['id'] for i in range(index - 1, index + 3) if 0 <= i < len(order)}
        decks = [{'deck': n, 'loaded': n in on, 'id': on.get(n), 'playing': n == playing and not paused, 'bpm': 125.0, 'beatPosition': 64.0}
                 for n in (1, 2, 3, 4)]
        return {'order': order, 'index': index, 'telemetry': {'decks': decks, 'sampledAt': time.time(),
                'crossfader': rotation.crossfader_end(playing) if crossfader is None else crossfader}}

    def test_the_cued_deck_is_the_next_in_the_rotation(self):
        from afterhours import copilot_commands
        for index in range(5):
            playing = rotation.deck_for(index, rotation.FOUR)
            current, incoming = copilot_commands.playing_pair(self.snapshot(playing, index))
            self.assertEqual((current['deck'], incoming['deck']), (playing, rotation.following(playing, rotation.FOUR)))

    def test_the_crossfader_must_rest_on_the_playing_decks_side(self):
        from afterhours import copilot_commands
        with self.assertRaisesRegex(ValueError, 'not confirmed silent'):
            copilot_commands.playing_pair(self.snapshot(3, 2, crossfader=1.0))     # C is on the left
        copilot_commands.playing_pair(self.snapshot(4, 3, crossfader=1.0))

    def test_a_second_playing_deck_or_a_wrong_cued_record_is_refused(self):
        from afterhours import copilot_commands
        s = self.snapshot(3, 2); s['telemetry']['decks'][0]['playing'] = True
        with self.assertRaises(ValueError): copilot_commands.playing_pair(s)
        s = self.snapshot(3, 2); s['telemetry']['decks'][3]['id'] = 'something-else'
        with self.assertRaisesRegex(ValueError, 'cued recording changed'): copilot_commands.playing_pair(s)
        s = self.snapshot(3, 2); s['telemetry']['decks'] = s['telemetry']['decks'][:3]
        with self.assertRaises(ValueError): copilot_commands.playing_pair(s)

    def test_a_paused_prepared_pair_can_be_handed_back_on_any_deck(self):
        from afterhours import copilot_commands
        current, incoming = copilot_commands.resume_pair(self.snapshot(4, 3, paused=True), allow_paused=True)
        self.assertEqual((current['deck'], incoming['deck']), (4, 1))



class RotationChoiceTests(unittest.TestCase):
    """Four decks only when the layout, the crossfader sides and the analysis plugin all allow it."""
    def choose(self, tools=True, decks=4, alias=None, wrong_side=None, fail=False, enabled=True):
        import threading
        from unittest.mock import patch
        from afterhours.vdj_session import DJSession
        s = DJSession(threading.Lock()); s.four_deck_enabled = enabled
        def query(client, script, kind=None):
            if fail: raise BridgeError('No answer')
            if script == 'get_decks': return decks
            _, number, key = script.split(maxsplit=2); deck = int(number)
            if key == 'get_deck': return alias if deck == 3 and alias else deck
            if key.startswith('cross_assign'):
                side = rotation.side(deck) if deck != wrong_side else ('right' if rotation.side(deck) == 'left' else 'left')
                return key == f"cross_assign '{side}'"
            raise AssertionError(script)
        s.q = query
        with patch('afterhours.background_analysis.four_deck_tools', return_value=tools):
            return s.choose_rotation(None)

    def test_four_decks_need_everything_in_place(self):
        self.assertEqual(self.choose(), rotation.FOUR)

    def test_four_decks_are_off_unless_switched_on(self):
        import os, threading
        from unittest.mock import patch
        from afterhours.vdj_session import DJSession
        self.assertEqual(self.choose(enabled=False), rotation.TWO)        # everything in place, but not switched on
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AFTERHOURS_FOUR_DECK', None)
            self.assertFalse(DJSession(threading.Lock()).four_deck_enabled)
        with patch.dict(os.environ, {'AFTERHOURS_FOUR_DECK': '1'}):
            self.assertTrue(DJSession(threading.Lock()).four_deck_enabled)

    def test_anything_missing_keeps_the_two_deck_set(self):
        self.assertEqual(self.choose(tools=False), rotation.TWO)          # the installed plugin cannot prepare on A/B while others play
        self.assertEqual(self.choose(decks=2), rotation.TWO)              # a two-deck layout
        self.assertEqual(self.choose(alias=1), rotation.TWO)              # deck 3 is an alias of deck 1
        self.assertEqual(self.choose(wrong_side=3), rotation.TWO)         # C is not on the left of the crossfader
        self.assertEqual(self.choose(fail=True), rotation.TWO)            # VirtualDJ did not answer

    def test_the_plugin_version_is_read_from_the_installed_bundle(self):
        import plistlib, tempfile
        from pathlib import Path
        from afterhours.background_analysis import four_deck_tools
        with tempfile.TemporaryDirectory() as folder:
            info = Path(folder) / 'Info.plist'
            for version, expected in (('0.2.1', False), ('0.3.0', True), ('1.0', True), ('nonsense', False)):
                info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': version}))
                self.assertEqual(four_deck_tools(info), expected, version)
            self.assertFalse(four_deck_tools(Path(folder) / 'missing.plist'))


if __name__ == '__main__':
    unittest.main()
