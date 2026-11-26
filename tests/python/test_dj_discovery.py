import unittest

from afterhours import dj_discovery


class DiscoveryTests(unittest.TestCase):
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
