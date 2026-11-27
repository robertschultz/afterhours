import unittest

from afterhours import dj_discovery, queue_planning


class DiscoveryTests(unittest.TestCase):
    def test_non_house_genres_keep_their_own_ranking(self):
        self.assertEqual(dj_discovery.style_for('Play Christian pop'), 'gospel')
        self.assertEqual(dj_discovery.style_for('Classic Rock'), 'rock')
        self.assertEqual(dj_discovery.style_for('R&B'), 'r&b')
        self.assertEqual(dj_discovery.style_for('Hip-Hop/Rap'), 'hip hop')
        pick = lambda row: (0, 0)
        rock = {'id':'rock', 'title':'Rock track', 'artist':'Band A', 'genre':'Classic Rock'}
        house = {'id':'house', 'title':'House track', 'artist':'Artist B', 'genre':'House'}
        ranked, _ = dj_discovery.rank_candidates([house, rock], 'rock', [], [], pick)
        self.assertEqual([row['id'] for row in ranked], ['rock', 'house'])

    def test_rolling_hints_reach_records_beyond_the_first_six(self):
        discovery = dj_discovery.Discovery(clock=lambda: 1)
        discovery.feeds['house'] = {'observedAt': 1, 'source': 'Test house chart',
                                    'rows': [{'title': f'Track {index}', 'artist': f'Artist {index}',
                                              'percentile': 100 - index} for index in range(15)]}
        first = discovery.hints('house', [])
        searched = {dj_discovery.normalized(f"{row['artist']} {row['title']}") for row in first}
        second = discovery.hints('house', [], searched=searched)
        self.assertEqual(len(first), 6)
        self.assertEqual(len(second), 6)
        self.assertFalse({row['title'] for row in first} & {row['title'] for row in second})

    def test_style_feed_uses_recording_evidence_and_real_artist_credit(self):
        recording = '5ad1a2e9-60a7-4c07-ad68-f6cb7995f75e'
        feeds = iter([
            [{'recording_mbid': recording, 'source': 'release-group', 'percent': 84.53},
             {'recording_mbid': 'a' * 36, 'source': 'artist', 'percent': 100}],
            {recording: {'recording': {'name': 'An actual recording'},
                         'artist': {'name': 'A credited artist'},
                         'release': {'year': 2026}}},
        ])
        result = dj_discovery.listenbrainz_style('deep house', 42, lambda _: next(feeds))
        self.assertEqual(result['rows'], [{'title': 'An actual recording', 'artist': 'A credited artist',
                                          'releaseYear': 2026, 'percentile': 84.5}])

    def test_chart_match_is_exact_and_expires(self):
        now = [1]
        discovery = dj_discovery.Discovery(clock=lambda: now[0])
        discovery.feeds['dance'] = {'observedAt': 1, 'source': 'Test chart',
                                    'rows': [{'title': 'Home', 'artist': 'Alex & Steph', 'rank': 3}]}
        self.assertEqual(discovery.evidence({'title': 'Home', 'artist': 'Steph, Alex'})['danceChart']['rank'], 3)
        self.assertEqual(discovery.evidence({'title': 'Home (Remix)', 'artist': 'Steph, Alex'}), {})
        now[0] = dj_discovery.MAX_AGE + 2
        self.assertEqual(discovery.evidence({'title': 'Home', 'artist': 'Steph, Alex'}), {})

    def test_ranking_keeps_request_priority_and_avoids_artist_repeats(self):
        played = {'id': 'playing', 'artist': 'Alex', 'label': 'A Label', 'genre': 'House'}
        same_artist = {'id': 'same', 'artist': 'Alex', 'label': 'A Label', 'genre': 'House'}
        variety = {'id': 'fresh', 'artist': 'Other DJ', 'label': 'A Label', 'genre': 'House'}
        requested = {'id': 'requested', 'artist': 'Alex & Quinn', 'genre': 'House'}
        ranked, facts = dj_discovery.rank_candidates([same_artist, variety, requested], 'house',
            [played], [played], lambda track: (-int(track is requested), 0))
        self.assertEqual([row['id'] for row in ranked], ['requested', 'fresh', 'same'])
        self.assertEqual(facts['fresh']['sharedLabel'], ['playing'])
        self.assertEqual(facts['same']['recentArtistAppearances'], 1)

    def test_exact_live_match_can_start_with_a_feasible_followup(self):
        first={'id':'a','title':'Opening','artist':'Daft Punk','bpm':125}
        nearby={'id':'b','title':'Follower','artist':'Other','bpm':126}
        unavailable={'id':'c','title':'Also fine','artist':'Other','bpm':125}
        room={'candidates':[unavailable,first,nearby],
              'recordingAvailability':[{'trackId':'c'}]}
        self.assertEqual(queue_planning.quick_opening(room,'Daft Punk',{'a','b','c'}),['a','b'])
        self.assertIsNone(queue_planning.quick_opening(room,'Daft Punk',{'a','c'}))
        self.assertIsNone(queue_planning.quick_opening(room,'A mood',{'a','b','c'}))

    def test_peak_slot_prefers_energy_and_credited_collaborators(self):
        anchor={'id':'anchor','artist':'Alex','genre':'House'}
        collab={'id':'collab','artist':'Alex, Jules','energy':.4,'genre':'House'}
        related={'id':'related','artist':'Jules','energy':.9,'genre':'House'}
        filler={'id':'filler','artist':'Other','energy':.3,'genre':'House'}
        slot=dj_discovery.set_slot('a peak-hour House set')
        ranked,facts=dj_discovery.rank_candidates([collab,related,filler],'house',[anchor],[anchor],
                                                   lambda _: (0,0),set_slot=slot)
        self.assertEqual(ranked[0]['id'],'related')
        self.assertEqual(facts['related']['collaborationLinks'],['jules'])
