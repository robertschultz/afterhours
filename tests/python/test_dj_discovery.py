import unittest

from afterhours import dj_discovery, queue_planning


class DiscoveryTests(unittest.TestCase):
    def test_christian_set_rejects_tempo_only_and_partial_name_matches(self):
        opening = {'id': 'opening', 'artist': 'Forrest Frank & The Figs', 'genre': 'Christian'}
        alternatives = (
            {'id': 'am', 'artist': 'Flowers and Sea Creatures', 'genre': 'Alternative'},
            {'id': 'memories', 'artist': 'Frank Sinatra', 'genre': 'Easy Listening'},
            {'id': 'house', 'artist': 'Forrest Frank', 'genre': 'Deep House'},
        )
        for candidate in alternatives:
            with self.subTest(artist=candidate['artist']):
                with self.assertRaises(dj_discovery.SetStyleMismatch):
                    dj_discovery.require_faith_set([opening, candidate])
        for candidate in (
            {'id': 'same-artist', 'artist': 'Forrest Frank', 'genre': ''},
            {'id': 'adjacent', 'artist': 'Another artist', 'genre': 'Christian Hip Hop'},
        ):
            dj_discovery.require_faith_set([opening, candidate])

    def test_public_artist_relationships_require_an_unambiguous_seed(self):
        seed = '11111111-1111-1111-1111-111111111111'
        artists = {'artists': [{'id':seed, 'name':'Fleetwood Mac'}]}
        radio = {seed:[{'similar_artist_name':'Fleetwood Mac'}],
                 'another':[{'similar_artist_name':'Stevie Nicks'}, {'similar_artist_name':'Stevie Nicks'}]}
        calls = []
        replies = iter((artists, radio))
        def fetch(url):
            calls.append(url)
            return next(replies)
        self.assertEqual(dj_discovery.similar_artists('Fleetwood Mac', fetch), ('Stevie Nicks',))
        self.assertIn(seed, calls[-1])
        self.assertIn('pop_begin=55', calls[-1])
        ambiguous = {'artists': artists['artists'] + [{'id':'another', 'name':'Fleetwood Mac'}]}
        self.assertEqual(dj_discovery.similar_artists('Fleetwood Mac', lambda _:ambiguous), ())

    def test_non_house_genres_keep_their_own_ranking(self):
        self.assertEqual(dj_discovery.style_for('Play Christian pop'), 'gospel')
        self.assertEqual(dj_discovery.style_for('Classic Rock'), 'rock')
        self.assertEqual(dj_discovery.style_for('Alternative'), 'alternative')
        self.assertEqual(dj_discovery.style_for('Alternative Rock'), 'rock')
        self.assertEqual(dj_discovery.style_for('Indie Dance'), 'indie dance')
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

    def test_dance_chart_matches_do_not_require_genre_percentiles(self):
        discovery = dj_discovery.Discovery(clock=lambda: 1)
        discovery.feeds['dance'] = {'observedAt': 1, 'source': 'iTunes Dance',
                                    'rows': [{'title': 'Track', 'artist': 'Artist', 'rank': 2}]}
        track = {'id': 'exact', 'title': 'Track', 'artist': 'Artist', 'genre': 'Dance'}
        ranked, facts = dj_discovery.rank_candidates([track], 'dance', [], [], lambda _: (0, 0),
                                                      discovery=discovery)
        self.assertEqual(ranked, [track])
        self.assertEqual(facts['exact']['danceChart']['rank'], 2)
        self.assertNotIn('genrePopularity', facts['exact'])
        self.assertEqual(discovery.hints('dance', [])[0]['rank'], 2)

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

    def test_sparse_live_results_only_reuse_a_cached_followup_with_same_artist_and_source(self):
        opener = {'id':'a', 'title':'First', 'artist':'Forrest Frank', 'bpm':118,
                  'catalog':{'provider':'online-a'}}
        matching = {**opener, 'id':'b', 'title':'Second', 'bpm':120}
        wrong_artist = {**opener, 'id':'c', 'title':'Unrelated', 'artist':'Other'}
        another_provider = {**opener, 'id':'d', 'title':'Third', 'catalog':{'provider':'online-b'}}
        state = {'candidates':[opener, wrong_artist, another_provider, matching]}
        self.assertEqual(queue_planning.quick_opening(state,'Forrest Frank',{'a'}),['a','b'])
        self.assertIsNone(queue_planning.quick_opening(state,'Forrest Frank',{'c'}))
        self.assertIsNone(queue_planning.quick_opening({'candidates':state['candidates'][:-1]},'Forrest Frank',{'a'}))

    def test_settled_spoken_search_accepts_one_unambiguous_doubled_letter(self):
        first={'id':'a','title':'First','artist':'Forrest Frank','bpm':118}
        second={**first,'id':'b','title':'Second'}
        state={'candidates':[first,second]}
        self.assertIsNone(queue_planning.quick_opening(state,'Forest Frank',{'a','b'}))
        self.assertEqual(queue_planning.quick_opening(state,'Forest Frank',{'a','b'},allow_spoken_spelling=True),['a','b'])
        state['candidates'].append({**first,'id':'c','artist':'Forest Frrank'})
        self.assertIsNone(queue_planning.quick_opening(state,'Forest Frank',{'a','b','c'},allow_spoken_spelling=True))

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

    def test_saved_night_plan_changes_the_target_without_inventing_missing_energy(self):
        plan={'startAt':0,'peakAt':3600,'endAt':7200,'warm':True,'onePeak':True,'darkLate':False}
        rows=[{'id':'warm','artist':'Act A','genre':'House','energy':.35},
              {'id':'peak','artist':'Act B','genre':'House','energy':.9},
              {'id':'unknown','artist':'Act C','genre':'House'}]
        for at, expected in ((120,'warm'),(3600,'peak')):
            slot=dj_discovery.set_slot('house',plan=plan,at=at)
            ranked,facts=dj_discovery.rank_candidates(rows,'house',[],[],lambda _:(0,0),set_slot=slot)
            self.assertEqual(ranked[0]['id'],expected)
            self.assertIn('setEnergyFit',facts[expected])
            self.assertNotIn('setEnergyFit',facts['unknown'])
        self.assertIsNone(dj_discovery.set_slot('house',plan=plan,at=7201))
        self.assertEqual(dj_discovery.set_slot('warm up',plan=plan,at=3600)['name'],'warm-up')
