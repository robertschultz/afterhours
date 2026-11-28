"""Append compatible recordings within the running set's native worker."""
from afterhours import dj_settings
from afterhours.algorhythm_refill import AlgorhythmRefill, linked_apple_recording
from afterhours.dj_bridge import BridgeError
from afterhours.dj_discovery import DISCOVERY, normalized, rank_candidates, set_slot, style_for
from afterhours.queue_planning import finite, transition_cost
from afterhours.vdj_session import song_identity


def discovery_style(state, order):
    requested = style_for(state.get('agentDirection'))
    opening = normalized(order[0].get('genre'))
    latest = order[-1].get('genre')
    if requested:
        return requested
    if opening and opening not in ('dance', 'electronic', 'music', 'unknown'):
        return opening
    return style_for(latest) or normalized(latest)


class RollingQueue:
    def __init__(self):
        self.algorhythm = AlgorhythmRefill()
        self.searched = set()
        self.missed = set()
        self.live_matches = set()
        self.missed_titles = set()

    def tick(self, session, slack):
        with session.lock:
            state = session.snapshot()
            if (not state.get('rollingQueue') or not state['active'] or state['phase'] != 'playing' or dj_settings.lead(state) != 'all'
                    or not state['setlist']['allowReorder'] or state['copilot'].get('held')):
                return
            if slack < 4 or session.cancel.is_set():
                return
            pending = state.get('queuedUpdate') or {}
            timed_set = bool(state.get('entryCadence'))
            miss = pending.get('catalogMissId')
            if timed_set:
                miss = miss or pending.get('analysisMissId') or pending.get('measuredFitMissId')
            attempted = pending.get('rollingTrackIds') or [pending.get('rollingTrackId')]
            if (miss and pending.get('status')=='blocked' and miss in attempted
                    and not state.get('resumeBlocked') and not session.background_job):
                self.missed.add(miss)
                if timed_set:
                    rejected = next((t for t in pending.get('tracks', []) if t['id'] == miss), None)
                    if rejected:
                        self.missed_titles.add(song_identity(rejected)[0])
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
                    used = {row['id'] for row in staged} | self.missed
                    used.update(row['trackId'] for row in state.get('recordingAvailability', []))
                    titles = {song_identity(row)[0] for row in staged} | self.missed_titles
                    supply = sum(row['id'] in self.live_matches and row['id'] not in used
                                 and song_identity(row)[0] not in titles for row in state['candidates'])
                    if supply < 2:
                        direction = str(state.get('agentDirection') or '')
                        style = style_for(direction) or style_for(staged[-1].get('genre')) or 'house'
                        self.search(session, state, staged, style, timed_set, lookahead=True)
                return

            order = state['order']
            # Two spare decks still prepare at most two recordings at once.
            # Start replenishing timed sets earlier so a slow decode or search
            # does not consume all of the upcoming 30-second slots.
            if not order or len(order) - state['index'] > (8 if timed_set else 3):
                return
            quick = state['mixStyle'] == 'quick'
            if not quick and not (state.get('executionPlan') or {}).get('applied'):
                return
            ids = [track['id'] for track in order]
            titles = {song_identity(track)[0] for track in order} | self.missed_titles
            unavailable = {row['trackId'] for row in state.get('recordingAvailability', [])}
            tail = order[-1]
            artists = [normalized(track.get('artist')) for track in order[-2:]]
            genre = normalized(tail.get('genre'))

            def score(candidate):
                candidate_artist = normalized(candidate.get('artist'))
                same_genre = bool(genre and genre == normalized(candidate.get('genre')))
                relationship = 0 if candidate_artist in artists else 1.5
                repetition = 2 if len(set(artists)) == 1 and candidate_artist in artists else 0
                return (transition_cost(tail, candidate, {}) + relationship + repetition
                        + (0 if same_genre else 2.5), candidate['id'])

            folder = (state.get('catalogFolder') or {}).get('id')
            candidates = [track for track in state['candidates']
                          if (not timed_set or not folder or not track.get('engine', {}).get('folder_id')
                              or track['engine']['folder_id'] == folder)
                          and track['id'] not in unavailable and track['id'] not in ids and track['id'] not in self.missed
                          and song_identity(track)[0] not in titles
                          and finite(track.get('bpm')) and track['bpm'] > 0]
            style = style_for(state.get('agentDirection')) or style_for(tail.get('genre')) or 'house'
            if self.algorhythm.enabled and len(order) - state['index'] - 1 <= 2:
                self.algorhythm.hints(state, order, discovery_style(state, order), (),
                                     activity=session.event, searched=self.searched)
            if timed_set and not any(t['id'] in self.live_matches for t in candidates):
                if (slack >= 26 and len(order)-state['index'] >= 2
                        and (not catalog or catalog.done) and self.search(session, state, order, style, timed_set)):
                    return
                if catalog and not catalog.done and slack >= 22:
                    return
            if timed_set:
                candidates, facts = rank_candidates(candidates, style, order[-5:], order[-5:], lambda track: (
                    0 if track['id'] in self.live_matches else 1,
                    transition_cost(tail, track, {}) + (0 if track.get('engineChecked') else 3)),
                    set_slot=set_slot(str(state.get('agentDirection') or '')))
            else:
                candidates = sorted(candidates, key=score)
            deferred = state.get('timedContinuation') or []
            if deferred:
                ranked = {t['id']: t for t in candidates}
                requested = [ranked[t['id']] for t in deferred if t['id'] in ranked]
                candidates = requested + [t for t in candidates if t['id'] not in {row['id'] for row in requested}]
            picked = []
            # A short gap after preloading is enough to start a batch when
            # verified tracks cover the next two handoffs. The pool pauses
            # native analysis before the mix and resumes it in the next gap.
            upcoming = len(order) - state['index'] - 1
            batch_slack = 16 if upcoming >= 2 else 23
            batch = 2 if timed_set and upcoming > 0 and slack >= batch_slack else 1
            for track in candidates:
                if session.cancel.is_set():
                    return
                try:
                    session.revise_queue(ids + [t['id'] for t in picked] + [track['id']],
                                         reason=f"Continuing the set with {', '.join(t['title'] for t in picked+[track])}.",
                                         source='planner')
                except (ValueError, BridgeError):
                    continue
                picked.append(track)
                if len(picked) >= batch:
                    break
            if picked:
                selected = [t['id'] for t in picked]
                queued = session.state['queuedUpdate']
                queued['rollingTrackId'] = selected[0]
                if len(selected) > 1:
                    queued['rollingTrackIds'] = selected
                if deferred:
                    session.update(timedContinuation=[t for t in deferred if t['id'] not in selected])
                if timed_set:
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
        terms = [str(track.get('artist') or '').strip() for track in reversed(order[-3:])]
        leads = self.algorhythm.hints(state, order, discovery_style(state, order), (),
                                     activity=session.event, searched=self.searched)
        identified = {f"{lead['artist']} {lead['title']}": lead for lead in leads}
        if timed_set:
            suggestions = DISCOVERY.hints(style, order, searched=self.searched)
            terms = [f"{row['artist']} {row['title']}" for row in suggestions] + terms
            terms += [str(track.get('label') or '').strip() for track in reversed(order[-5:])]
            if style == 'house' and not style_for(state.get('agentDirection')):
                for related in ('deep house', 'tech house'):
                    terms += [f"{row['artist']} {row['title']}"
                              for row in DISCOVERY.hints(related, order, searched=self.searched)]
            terms += [str(tail.get('genre') or style).strip(), style]
        for term in (*identified, *terms):
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
            if lead := identified.get(term):
                linked = linked_apple_recording(lead.get('public_reference'), lead.get('source_url'))
                job.set_rolling_seed(lead['artist'], lead['title'], version=lead.get('version'),
                                     exact_credits=True, linked_provider_reference=linked)
            if timed_set:
                job.reuse_matches = True
                job.target = job.quota = 4
                job.max_rows = 12
                job.seconds = 18
            if not lookahead:
                session.update(rollingQueue={'status': 'searching', 'term': term})
            return True
        return False
