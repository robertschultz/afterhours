"""Track preparation on the spare decks of a four-deck rotation; simulated replies only."""
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours import deck_rotation as rotation
from afterhours.background_analysis import BackgroundAnalysis, allowed, cleanup_descriptor, guard_deck, guard_isolation
from afterhours.dj_bridge import BridgeError
from afterhours.preparation_pool import PreparationPool
from afterhours.vdj_session import DJSession


class Peer:
    """Four real decks, all empty, paused and at full volume."""
    def __init__(self, decks=4):
        self.commands = []; self.values = {'get_decks': decks}
        for n in (1, 2, 3, 4):
            self.values.update({f'deck {n} get_deck': n if decks >= 4 else (n - 1) % 2 + 1, f'deck {n} loaded': False, f'deck {n} play': False,
                                f'deck {n} volume': 1, f'deck {n} pfl': False, f'deck {n} get_effect_name 1': 'Flanger',
                                f'deck {n} effect_active 1': False, f'deck {n} get_filepath': 'test'})

    def query(self, script, kind=None):
        return self.values[script]

    def send(self, script):
        self.commands.append(script)
        for n in (1, 2, 3, 4):
            if f'deck {n} volume 0%' in script: self.values[f'deck {n} volume'] = 0
            if f'deck {n} pfl off' in script: self.values[f'deck {n} pfl'] = False
            if f'deck {n} unload' in script: self.values[f'deck {n} loaded'] = False
            if f'deck {n} volume 100.' in script: self.values[f'deck {n} volume'] = 1


class Job:
    """Stands in for one deck's analysis."""
    def __init__(self, session, revision, tracks, cache, deck):
        self.tracks = tracks; self.deck = deck; self.loaded = None; self.done = False; self.error = None; self.cache = {}; self.result = []
        self.owned = True; self.serviced = 0; self.status = 'analyzing'; self.message = 'checking'; self.cancelled = threading.Event(); self.closed = False
    def step(self, client, seconds): self.serviced += 1
    def close(self, client): self.owned = False; self.closed = True
    def invalidate_catalog(self): pass


def session(pair=None, decks=rotation.FOUR):
    s = DJSession(threading.Lock()); s.decks = decks; s.pair_decks = pair
    return s


class SpareDeckTests(unittest.TestCase):
    def test_a_two_deck_set_prepares_on_c_and_d_as_before(self):
        self.assertEqual(session(None, rotation.TWO).preparation_decks(), (3, 4))
        self.assertEqual(session((1, 2), rotation.TWO).preparation_decks(), (3, 4))
        self.assertEqual(session(None).preparation_decks(), (3, 4))          # before a four-deck set has a playing pair

    def test_a_four_deck_set_prepares_outside_the_playing_pair_the_last_needed_deck_first(self):
        self.assertEqual(session((1, 2)).preparation_decks(), (4, 3))
        self.assertEqual(session((2, 3)).preparation_decks(), (1, 4))
        self.assertEqual(session((3, 4)).preparation_decks(), (2, 1))
        self.assertEqual(session((4, 1)).preparation_decks(), (3, 2))

    def tracks(self, count=3):
        return [{'id': str(i), 'title': str(i)} for i in range(count)]

    def test_the_pool_never_starts_on_the_playing_or_cued_deck(self):
        s = session((2, 3)); peer = Peer()
        with patch('afterhours.preparation_pool.BackgroundAnalysis', Job):
            pool = PreparationPool(s, 'rev', self.tracks())
            pool.step(peer, 40); pool.jobs[0].loaded = '0'; pool.step(peer, 40)
        self.assertEqual([job.deck for job in pool.jobs], [1, 4])
        self.assertEqual(pool.snapshot()['message'].split(':')[0], 'Deck A')

    def test_a_deck_the_set_needs_is_released_and_its_record_goes_back_first_in_line(self):
        s = session((1, 2)); peer = Peer()
        with patch('afterhours.preparation_pool.BackgroundAnalysis', Job):
            pool = PreparationPool(s, 'rev', self.tracks())
            pool.step(peer, 40); pool.jobs[0].loaded = '0'; pool.step(peer, 40)
            self.assertEqual([(job.deck, job.tracks[0]['id']) for job in pool.jobs], [(4, '0'), (3, '1')])
            pool.release(peer, 3)                                         # C is about to be loaded for the set
            self.assertTrue(pool.jobs[1].closed and pool.jobs[1].done and pool.jobs[1].cancelled.is_set())
            self.assertFalse(pool.jobs[0].closed)
            self.assertEqual([t['id'] for t in pool.pending], ['1', '2'])
            pool.release(peer, 2)                                         # nothing is being prepared there
            self.assertEqual([t['id'] for t in pool.pending], ['1', '2'])

    def test_a_job_whose_deck_joined_the_playing_pair_is_released_on_the_next_pass(self):
        s = session((1, 2)); peer = Peer()
        with patch('afterhours.preparation_pool.BackgroundAnalysis', Job):
            pool = PreparationPool(s, 'rev', self.tracks(1)); pool.step(peer, 40)
            self.assertEqual(pool.jobs[0].deck, 4)
            s.pair_decks = (3, 4); pool.step(peer, 40)
            self.assertTrue(pool.jobs[0].closed)
            self.assertNotIn(4, [job.deck for job in pool.jobs if not job.done])

    def test_the_session_releases_a_deck_through_its_running_pool(self):
        s = session((1, 2)); s.background_job = Mock()
        s.release_preparation('client', 3)
        s.background_job.release.assert_called_once_with('client', 3)
        s.background_job = None; s.release_preparation('client', 3)       # nothing running: nothing to do


class IsolationTests(unittest.TestCase):
    def test_any_real_deck_can_be_a_spare_but_never_an_alias(self):
        for deck in (1, 2, 3, 4):
            guard_deck(Peer(), deck)
            with self.assertRaises(BridgeError): guard_deck(Peer(decks=2), deck)
        for bad in (0, 5, '1', None):
            with self.assertRaises(BridgeError): guard_deck(Peer(), bad)

    def test_preparation_on_deck_a_touches_only_deck_a_and_restores_it(self):
        peer = Peer(); job = BackgroundAnalysis(session((3, 4)), 'rev', [{'id': 'test', 'title': 'Test'}], deck=1)
        job.acquire(peer); self.assertEqual(peer.values['deck 1 volume'], 0); guard_isolation(peer, 1)
        job.close(peer)
        self.assertEqual(peer.values['deck 1 volume'], 1)
        self.assertTrue(all('deck 1' in c and not any(f'deck {n}' in c for n in (2, 3, 4)) for c in peer.commands))

    def test_a_job_can_only_address_its_own_deck(self):
        self.assertTrue(allowed('deck 1 goto 30%', True, 1)); self.assertTrue(allowed('deck 2 get_bpm', False, 2))
        for command in ('deck 2 goto 30%', 'deck 3 play', 'deck 1 play', 'crossfader 50%', 'deck 1 volume 100%'):
            self.assertFalse(allowed(command, True, 1), command)
        self.assertFalse(allowed('deck 5 goto 30%', True, 5))

    def test_a_deck_that_stopped_being_silent_is_no_longer_isolated(self):
        for change in ({'deck 1 play': True}, {'deck 1 volume': .4}, {'deck 1 pfl': True}):
            peer = Peer(); peer.values['deck 1 volume'] = 0; guard_isolation(peer, 1)
            peer.values.update(change)
            with self.assertRaises(BridgeError): guard_isolation(peer, 1)

    def test_scans_accept_deck_b_only_as_an_isolated_spare(self):
        from afterhours.audio_analysis import scan_track
        from afterhours.waveform_analysis import scan_waveform
        track = {'engine': {'selection_id': 'a' * 64}}
        for scan, arguments in ((scan_track, (Peer(), track, None)),):
            with self.assertRaisesRegex(ValueError, 'isolated spare deck'): scan(*arguments, deck=2)
            with self.assertRaisesRegex(BridgeError, 'end-of-sound'): scan(*arguments, deck=2, isolated=True)   # past the deck check
            with self.assertRaisesRegex(ValueError, 'isolated spare deck'): scan(*arguments, deck=5, isolated=True)
        with self.assertRaisesRegex(ValueError, 'isolated spare deck'):
            scan_waveform(Peer(), track, {'duration': 1, 'points': []}, deck=2)
        s = session()
        with self.assertRaisesRegex(ValueError, 'isolated spare deck'): s.measure_cues(Peer(), track, {}, 125, 30, deck=2)

    def test_a_recovered_cleanup_for_a_or_b_must_come_from_a_four_deck_set(self):
        row = {'version': 1, 'ownerId': '8c72823f81c54543bcac3dcdd57dc844', 'deck': 1, 'selectionId': None,
               'baseline': {'volume': 1.0, 'pfl': False, 'effect': 'Flanger'}}
        with self.assertRaises(BridgeError): cleanup_descriptor(row)
        kept = cleanup_descriptor({**row, 'fourDeck': True})
        self.assertEqual((kept['deck'], kept['fourDeck']), (1, True))
        self.assertNotIn('fourDeck', cleanup_descriptor({**row, 'deck': 3}))
        job = BackgroundAnalysis(session((3, 4)), 'rev', [{'id': 'test', 'title': 'Test'}], deck=2); job.acquire(Peer())
        self.assertTrue(job.cleanup_descriptors()[0]['fourDeck'])


class LoaderTests(unittest.TestCase):
    def test_the_set_may_load_any_of_its_four_decks_and_nothing_else(self):
        from afterhours.vdj_catalog import load_exact_recording, load_selected
        for loader, arguments in ((load_selected, ()), (load_exact_recording, ({}, 'x', 'y', {}))):
            for bad in (0, 5, '3', None):
                with self.assertRaisesRegex(ValueError, '1 to 4'): loader(Mock(), bad, *arguments)
            try: loader(Mock(), 3, *arguments)
            except ValueError as exc: self.assertNotIn('1 to 4', str(exc))
            except Exception: pass                                        # past the deck check; the fake client answers nothing useful


if __name__ == '__main__':
    unittest.main()
