"""Append compatible recordings within the running set's native worker."""
import re
import time

from afterhours import dj_settings
from afterhours.dj_bridge import BridgeError
from afterhours.dj_discovery import DISCOVERY, credit, identity, normalized, rank_candidates, set_slot, style_for
from afterhours.dj_discovery import fits_faith_set
from afterhours.dj_discovery import heavily_played_credits
from afterhours.queue_planning import finite, transition_cost
from afterhours.rolling_advisor import MODEL, RollingAdvisor
from afterhours.stem_profiles import candidate_fit
from afterhours.set_research import hints as research_hints
from afterhours.track_metadata import clean_text
from afterhours.vdj_session import DEFAULT_QUEUE_WINDOW, song_identity
from afterhours.verified_cues import VerifiedCues


# Adjacent styles give the DJ room to follow a compatible thread without
# taking an unrelated track simply because its BPM happens to fit.
STYLE_GROUPS = (
    frozenset(('house', 'deep house', 'tech house', 'techno', 'disco', 'dance', 'electronic')),
    frozenset(('soul', 'funk', 'r&b', 'gospel', 'hip hop')),
    frozenset(('rock', 'pop', 'funk', 'soul', 'alternative')),
    frozenset(('dance', 'electronic', 'indie dance')),
)
HOUSE_STYLES = frozenset(('house', 'deep house', 'tech house'))


def rolling_style(state, order):
    return (style_for(state.get('agentDirection')) or
            next((style for track in order if (style := style_for(track.get('genre')))), ''))


def fits_style(track, style, opening=None):
    if style == 'gospel':
        return fits_faith_set(track, opening or {})
    catalog_genre = normalized(track.get('genre'))
    actual = style_for(catalog_genre)
    if style in HOUSE_STYLES and not actual and catalog_genre not in ('', 'unknown', 'unclassified'):
        # An unhandled provider label is still genre evidence. A compatible
        # tempo alone cannot turn a Dub or Easy Listening record into house.
        return False
    return (not style or not actual or actual == style
            or any(style in group and actual in group for group in STYLE_GROUPS))

def house_style_priority(track, style):
    """Prefer a tagged house subgenre to broad dance metadata."""
    if style not in HOUSE_STYLES:
        return 0
    actual = style_for(track.get('genre'))
    return 0 if actual == style else 1 if actual in HOUSE_STYLES else 2


def automatic_style_filter(style, order, catalog, discovery=None):
    """Use supporting evidence before treating a broad tag as house."""
    opening = order[0] if order else None
    if style not in HOUSE_STYLES:
        return lambda track: fits_style(track, style, opening)

    discovery = discovery or DISCOVERY
    recordings = discovery.house_recordings()
    tagged = [track for track in (*order, *catalog) if style_for(track.get('genre')) in HOUSE_STYLES]
    established = {artist for track in tagged for artist in credit(track.get('artist'))}
    # Neighbors are a lead backed only by trusted house anchors. A broad-tag
    # recording added later must never make its own catalog an anchor.
    anchors = [track for track in order if style_for(track.get('genre')) in HOUSE_STYLES][-3:]
    for anchor in anchors:
        for artist in credit(anchor.get('artist')):
            for neighbor in discovery.cached_neighbors(artist):
                established.update(credit(neighbor))

    def compatible(track):
        if not fits_style(track, style, opening):
            return False
        actual = style_for(track.get('genre'))
        return (actual not in ('', 'dance', 'electronic')
                or identity(track.get('title'), track.get('artist')) in recordings
                or bool(credit(track.get('artist')) & established)
                or discovery.has_recording_house_tag(track))
    return compatible


def request_missing_house_evidence(style, eligible, unresolved, target_bpm, max_pitch):
    if style not in HOUSE_STYLES or len(eligible) >= 3:
        return
    candidates = [track for track in unresolved
                  if (track.get('catalog') or {}).get('provider')
                  and fits_style(track, style)
                  and style_for(track.get('genre')) in ('', 'dance', 'electronic')
                  and abs(target_bpm / track['bpm'] - 1) * 100 <= max_pitch]
    candidates.sort(key=lambda track: abs(target_bpm - track['bpm']))
    DISCOVERY.request_house_tag_checks(candidates[:12])


def base_song_title(track):
    # Providers may include featured artists and release versions in the title.
    suffix = r'[\[(](?:(?:feat\.?|ft\.?|featuring|with)\s+[^\])]+|[^\])]*(?:mix|version|edit|dub|remaster|rework)[^\])]*)[\])]'
    bare_version = r'[\[(](?:extended|original|radio|vip|instrumental|a cappella|acapella|live)[\])]'
    base = re.sub(r'(?:\s*(?:'+suffix+'|'+bare_version+r'))+\s*$', '', clean_text(track.get('title')) or '', flags=re.I)
    base = re.sub(r'\s+(?:feat\.|ft\.|featuring)\s+\S.*$', '', base, flags=re.I)
    words = normalized(base).split()
    without_article = [word for word in words if word != 'the']
    # Keep short names intact. Longer titles sometimes lose an article in
    # catalog listings, even under the same artist credit.
    return ' '.join(without_article if len(without_article) >= 3 else words)

def song_credit_keys(track):
    base = base_song_title(track)
    return {(base, artist) for artist in credit(track.get('artist'))} if base else set()


def repeated_artists(order):
    if len(order) < 2:
        return set()
    seen, repeated = set(), set()
    for track in order[-3:]:
        artists = credit(track.get('artist'))
        repeated.update(seen & artists)
        seen.update(artists)
    return repeated


def brings_new_artist(track, repeated):
    artists = credit(track.get('artist'))
    return bool(artists) and not artists.intersection(repeated)


def repeats_artist_query(term, repeated):
    query = f' {normalized(term)} '
    return any(f' {artist} ' in query for artist in repeated)


def failed_rolling_recording(pending, *, pinned=()):
    if pending.get('status') != 'blocked':
        return None
    attempted = pending.get('rollingTrackIds') or [pending.get('rollingTrackId')]
    return next((pending[key] for key in ('catalogMissId','analysisMissId','measuredFitMissId')
                 if pending.get(key) and pending[key] in attempted
                 and (key != 'measuredFitMissId' or not set(attempted).intersection(pinned))), None)


class RollingQueue:
    def __init__(self):
        self.advisor = RollingAdvisor()
        self.cues = VerifiedCues()
        self.searched = set()
        self.missed = set()
        self.live_matches = set()
        self.missed_titles = set()
        self.failed_scan_songs = set()
        self.scan_failure_index = None
        self.artist_expansion = None
        self.artist_search_index = None

    def timed_ready_ids(self, state, candidates):
        cadence, order = state.get('entryCadence'), state['order']
        if not cadence or len(order) > state['index'] + 2 or not candidates:
            return set()
        route = (state.get('executionPlan') or {}).get('tempoSchedule') or []
        last = route[-1] if route else {}
        target, limit = last.get('toBpm'), state.get('maxTempoShift', 8)
        if (last.get('trackId') != order[-1]['id'] or not finite(target) or not 40 <= target <= 300
                or not finite(limit) or not 0 <= limit <= 8):
            return set()
        seconds = cadence[len(order) % len(cadence)]
        # Keep verification work bounded on the native playback worker.
        return {row['id'] for row in candidates[:8]
                if self.cues.compatible_timed_bpm(row, seconds, target, limit) is not None}

    def tick(self, session, slack):
        with session.lock:
            state = session.snapshot()
            if (not state.get('rollingQueue') or not state['active'] or state['phase'] != 'playing' or dj_settings.lead(state) != 'all'
                    or not state['setlist']['allowReorder'] or state['copilot'].get('held')):
                return
            if slack < 4 or session.cancel.is_set():
                return
            if self.scan_failure_index != state['index']:
                self.failed_scan_songs.clear()
                self.scan_failure_index = state['index']
            pending = state.get('queuedUpdate') or {}
            omission = (state.get('lastQueueApplication') or {}).get('refillOmission') or {}
            if omission.get('trackId'):
                self.missed.add(omission['trackId'])
            timed_set = bool(state.get('entryCadence'))
            pinned = set(state.get('setPins', [])) | set(state['setlist'].get('lockedIds', []))
            miss = failed_rolling_recording(pending, pinned=pinned)
            if (miss and (not state.get('resumeBlocked') or session.quarantined_preparation_decks())
                    and not session.background_job):
                self.missed.add(miss)
                if miss != pending.get('catalogMissId'):
                    rejected = next((t for t in pending.get('tracks', []) if t['id'] == miss), None)
                    if rejected:
                        self.missed_titles.add(song_identity(rejected)[0])
                        if miss == pending.get('analysisMissId'):
                            self.failed_scan_songs.update(song_credit_keys(rejected))
                session.update(queuedUpdate=None,rollingQueue={'status':'searching'})
                session.event('The next candidate did not prepare. Looking for another recording.', 'queue')
                pending={}
            if pending.get('status') == 'blocked' and state['rollingQueue'].get('status') != 'held':
                session.update(rollingQueue={'status': 'held'})
            catalog = session.catalog_job
            if timed_set and catalog and getattr(catalog, 'rolling', False):
                self.live_matches.update(catalog.seen)
            if pending or session.background_job:
                if (timed_set and pending.get('status') == 'analyzing' and pending.get('rollingTrackId')
                        and slack >= 20 and session.background_job
                        and session.background_job.revision == pending['id']
                        and session.background_job.navigation_idle()
                        and (not catalog or catalog.done)):
                    staged = pending['tracks']
                    style = rolling_style(state, staged)
                    matches_style = automatic_style_filter(style, staged, state['candidates'])
                    used = {row['id'] for row in staged} | self.missed
                    used.update(row['trackId'] for row in state.get('recordingAvailability', []))
                    titles = {song_identity(row)[0] for row in staged} | self.missed_titles
                    repeated = repeated_artists(staged) | heavily_played_credits(staged)
                    supply = {title for row in state['candidates']
                              if row['id'] in self.live_matches and row['id'] not in used
                              and (title := song_identity(row)[0]) and title not in titles
                              and matches_style(row) and (not repeated or brings_new_artist(row, repeated))}
                    if len(supply) < 2:
                        self.search(session, state, staged, style, timed_set, lookahead=True)
                return

            order = state['order']
            # Two spare decks still prepare at most two recordings at once.
            # Start replenishing timed sets earlier so a slow decode or search
            # does not consume all of the upcoming 30-second slots.
            quick = state['mixStyle'] == 'quick'
            refill_at = state.get('queueWindow',DEFAULT_QUEUE_WINDOW) - 2 if timed_set else (3 if quick else 5)
            if not order or len(order) - state['index'] > refill_at:
                return
            if not quick and not (state.get('executionPlan') or {}).get('applied'):
                return
            ids = [track['id'] for track in order]
            titles = {song_identity(track)[0] for track in order} | self.missed_titles
            song_versions = set().union(*(song_credit_keys(track) for track in order))
            unavailable = {row['trackId'] for row in state.get('recordingAvailability', [])}
            if state.get('resumeBlocked'):
                # Leave an unverified native Load alone until its deck is inspected.
                unavailable.update('virtualdj:'+row['selectionId']
                    for row in (state.get('cleanupPending') or {}).get('analysis', []) if row.get('selectionId'))
            tail = order[-1]
            DISCOVERY.neighbors(tail.get('artist'))
            artists = [normalized(track.get('artist')) for track in order[-2:]]
            genre = style_for(tail.get('genre')) or normalized(tail.get('genre'))

            def score(candidate):
                candidate_artist = normalized(candidate.get('artist'))
                candidate_genre = style_for(candidate.get('genre')) or normalized(candidate.get('genre'))
                same_genre = bool(genre and genre == candidate_genre)
                relationship = 0 if candidate_artist in artists else 1.5
                repetition = 2 if len(set(artists)) == 1 and candidate_artist in artists else 0
                return (transition_cost(tail, candidate, {}) + relationship + repetition
                        + (0 if same_genre else 2.5), candidate['id'])

            folder = (state.get('catalogFolder') or {}).get('id')
            style = rolling_style(state, order)
            matches_style = automatic_style_filter(style, order, state['candidates'])
            candidates = [track for track in state['candidates']
                          if (not timed_set or not folder or not track.get('engine', {}).get('folder_id')
                              or track['engine']['folder_id'] == folder)
                          and track['id'] not in unavailable and track['id'] not in ids and track['id'] not in self.missed
                          # Continuous-mix cuts often duplicate the full
                          # recording and have a baked-in DJ transition.
                          and clean_text(track.get('title'))
                          and not track['title'].strip().casefold().endswith((' [mixed]', ' (mixed)'))
                          and song_identity(track)[0] not in titles
                          and not song_credit_keys(track).intersection(song_versions)
                          and fits_style(track, style, order[0])
                          and finite(track.get('bpm')) and track['bpm'] > 0]
            eligible, unresolved = [], []
            for track in candidates:
                (eligible if matches_style(track) else unresolved).append(track)
            candidates = eligible
            upcoming = len(order) - state['index'] - 1
            one_spare_pressure = timed_set and upcoming <= 2 and len(session.preparation_decks()) == 1
            if upcoming <= 4:
                request_missing_house_evidence(style, candidates, unresolved, tail['bpm'], state.get('maxTempoShift', 8))
            # With two successors already committed, yield one catalog
            # search to broaden an operator-started queue that has become
            # dominated by one artist. The shorter buffer can still refill.
            if (timed_set and not one_spare_pressure and upcoming >= 2 and slack >= 22
                    and not state.get('agentDirection') and candidates):
                avoid = repeated_artists(order) | heavily_played_credits(order)
                if avoid and not any(brings_new_artist(row, avoid) for row in candidates):
                    if self.artist_search_index == state['index']:
                        if catalog and not catalog.done:
                            return
                    elif slack >= 26 and (not catalog or catalog.done):
                        self.artist_search_index = state['index']
                        if self.search(session, state, order, style, timed_set, lookahead=True):
                            return
            # With a short buffer, prepare an eligible previously searched
            # recording now. Live discovery can resume once its deck is loaded.
            urgent_refill = upcoming <= 2 and bool(candidates)
            if timed_set and not urgent_refill and not any(t['id'] in self.live_matches for t in candidates):
                if (slack >= 26 and len(order)-state['index'] >= 2
                        and (not catalog or catalog.done) and self.search(session, state, order, style, timed_set)):
                    return
                if catalog and not catalog.done and slack >= 22:
                    return
            if timed_set or not quick:
                tail_cue=next((cue for cue in (state.get('executionPlan') or {}).get('selections',[])
                               if cue.get('trackId')==tail['id']),{})
                stem_fit={row['id']:fit for row in candidates
                          if (fit:=candidate_fit(tail['id'],row['id'],tail_cue))}
                candidates, facts = rank_candidates(candidates, style, order[-5:], order[-5:], lambda track: (
                    (house_style_priority(track, style) if upcoming and slack >= 20 else 0,
                     0 if not timed_set or track['id'] in self.live_matches else 1),
                    transition_cost(tail, track, {}) + (0 if track.get('engineChecked') else 3)
                    + 8*stem_fit.get(track['id'],{}).get('scoreAdjustment',0)),
                    set_slot=set_slot(str(state.get('agentDirection') or ''),
                                      plan=getattr(session,'standing_night_plan',None),at=time.time()))
                for track_id, fit in stem_fit.items():
                    facts[track_id]['stemFit']=fit
            else:
                candidates = sorted(candidates, key=score)
            deferred = state.get('timedContinuation') or []
            if deferred:
                ranked = {t['id']: t for t in candidates}
                requested = [ranked[t['id']] for t in deferred if t['id'] in ranked]
                candidates = requested + [t for t in candidates if t['id'] not in {row['id'] for row in requested}]
            failed_songs = self.failed_scan_songs if not deferred else set()
            timed_ready = self.timed_ready_ids(state, candidates) if not deferred else set()
            advised = receipt = None
            if not one_spare_pressure and not deferred and (timed_set or not quick):
                repeat = repeated_artists(order)
                fresh = [row for row in candidates if not song_credit_keys(row) & failed_songs] or candidates
                ready = [row for row in fresh if row['id'] in timed_ready]
                fresh = ready or fresh
                avoid = repeat | heavily_played_credits(order)
                diverse = [row for row in fresh if brings_new_artist(row, avoid)] if avoid else []
                advised, waiting = self.advisor.advise(state,tail,diverse or fresh,facts,slack)
                if waiting:
                    return
                if advised:
                    candidates.sort(key=lambda row: row['id'] != advised)
                receipt = self.advisor.receipt
                self.advisor.receipt = None
            picked = []
            # With one spare and little runway, stage its next job now.
            # The first result can publish while the same deck verifies the
            # second, with no additional ranking or catalog round trip.
            batch_slack = 16 if one_spare_pressure or upcoming >= 2 else 23 if upcoming else 40
            batch = 2 if timed_set and slack >= batch_slack else 1
            remaining = list(candidates)
            while remaining and len(picked) < batch:
                if session.cancel.is_set():
                    return
                avoid = set()
                if not deferred:
                    avoid = repeated_artists(order + picked) | heavily_played_credits(order + picked)
                    avoid.update(artist for track in picked for artist in credit(track.get('artist')))
                # Favor artists with room in the set. Keep repeats in reserve if every
                # alternative fails the native queue and tempo checks.
                slot = min(range(len(remaining)), key=lambda index: (
                    bool(failed_songs & song_credit_keys(remaining[index])),
                    bool(timed_ready and not picked and remaining[index]['id'] not in timed_ready),
                    bool(avoid and not brings_new_artist(remaining[index], avoid)),
                )) if failed_songs or timed_ready or avoid else 0
                track = remaining.pop(slot)
                keys = song_credit_keys(track)
                if keys & song_versions:
                    continue
                try:
                    session.revise_queue(ids + [t['id'] for t in picked] + [track['id']],
                                         reason=f"Continuing the set with {', '.join(t['title'] for t in picked+[track])}.",
                                         source='planner')
                except (ValueError, BridgeError):
                    continue
                picked.append(track)
                song_versions.update(keys)
            if receipt:
                description = {
                    'selected': 'GPT-6.2 Sol ranked a shortlisted candidate first.',
                    'no-pick': 'GPT-6.2 Sol left the local track ranking in place.',
                    'error': 'GPT-6.2 Sol advice was unavailable. Using the local track ranking.',
                    'deadline': 'GPT-6.2 Sol did not return before the preparation deadline. Using the local track ranking.',
                    'stale': 'The shortlisted recording changed. Using the current local track ranking.',
                }[receipt['outcome']]
                accepted = advised is not None and any(track['id'] == advised for track in picked)
                session.event(description, 'queue', trackId=advised, model=MODEL, outcome=receipt['outcome'])
                self.advisor.log_benchmark_receipt({
                    **receipt,
                    'at': time.time(),
                    'sessionId': state['id'],
                    'model': MODEL,
                    'rankedTrackId': advised,
                    'acceptedForPreparation': accepted,
                    'queueRevisionId': session.state['queuedUpdate']['id'] if accepted else None,
                })
            if picked:
                selected = [t['id'] for t in picked]
                queued = session.state['queuedUpdate']
                queued['rollingTrackId'] = selected[0]
                if len(selected) > 1:
                    queued['rollingTrackIds'] = selected
                if deferred:
                    session.update(timedContinuation=[t for t in deferred if t['id'] not in selected])
                if timed_set or not quick:
                    for track in picked:
                        session.event(f"Selected {track['title']} for a measured queue slot.", 'queue',
                                      trackId=track['id'], currentCatalogMatch=track['id'] in self.live_matches,
                                      musicEvidence=facts[track['id']])
                session.update(rollingQueue={'status': 'preparing', 'next': picked[0]['title']})
                return

            if session.catalog_job and not session.catalog_job.done:
                return
            if slack < (9 if quick else 18):
                return
            if not self.search(session, state, order, style, timed_set) and state['rollingQueue'].get('status') != 'held':
                session.update(rollingQueue={'status': 'held'})

    def search(self, session, state, order, style, timed_set, *, lookahead=False):
        tail = order[-1]
        recent = list(reversed(order[-8:]))
        artists = [str(track.get('artist') or '').strip() for track in recent[:3]]
        seeds = [(artist, artist if timed_set else '', '') for artist in artists]
        if timed_set or state.get('mixStyle') == 'phrase':
            suggestions = research_hints(style, order, self.searched)
            suggestions += DISCOVERY.hints(style, order, searched=self.searched)
            seeds = [(f"{row['artist']} {row['title']}", row['artist'], row['title'])
                     for row in suggestions] + seeds
            seeds += [(name, name, '') for name in DISCOVERY.neighbors(tail.get('artist'))]
            seeds += [(str(track.get('label') or '').strip(), '', '') for track in reversed(order[-5:])]
            if style == 'house' and not style_for(state.get('agentDirection')):
                for related in ('deep house', 'tech house'):
                    seeds += [(f"{row['artist']} {row['title']}", row['artist'], row['title'])
                              for row in DISCOVERY.hints(related, order, searched=self.searched)]
            if style not in HOUSE_STYLES:
                seeds.append((str(tail.get('genre') or style).strip(), '', ''))
            seeds.append((style, '', ''))
            seeds += [(name, name, '') for track in recent for name in sorted(credit(track.get('artist')))]
            if style == 'gospel':
                seeds += [(name, '', '') for name in ('Christian pop', 'Contemporary Christian', 'Christian hip hop')]
            elif not style_for(state.get('agentDirection')):
                neighbors = {name for group in STYLE_GROUPS if style in group for name in group if name!=style}
                seeds += [(name, '', '') for name in sorted(neighbors)]
        direction = state.get('agentDirection')
        expansion = self.artist_expansion
        if expansion:
            job, artist, previous_style, previous_direction = expansion
            if (session.catalog_job is job and job.done and not job.seen
                    and (style, direction) == (previous_style, previous_direction)):
                # A chart title may be missing under this provider's release
                # spelling. Give its credited artist one broader native search.
                seeds.insert(0, (artist, artist, ''))
        self.artist_expansion = None
        if avoid := repeated_artists(order) | heavily_played_credits(order):
            seeds.sort(key=lambda seed: repeats_artist_query(seed[0], avoid))
        for term, artist, title in seeds:
            key = normalized(term)
            if not key or key in self.searched or len(term) > 95 or ';' in term:
                continue
            self.searched.add(key)
            target = state.get('quickTargetBpm') or tail['bpm']
            try:
                session.discover_more(term, bpm_min=max(40, target * .90), bpm_max=min(300, target * 1.11))
            except (ValueError, BridgeError):
                continue
            job = session.catalog_job
            job.rolling = True
            if timed_set:
                job.set_rolling_seed(artist, title)
                if title and artist:
                    self.artist_expansion = (job, artist, style, direction)
                job.reuse_matches = True
                job.target = job.quota = 4
                job.max_rows = 12
                job.seconds = 18
            if not lookahead:
                session.update(rollingQueue={'status': 'searching', 'term': term})
            return True
        return False
