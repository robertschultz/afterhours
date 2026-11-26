"""Append compatible recordings within the running set's native worker."""
from afterhours import dj_settings
from afterhours.dj_bridge import BridgeError
from afterhours.dj_discovery import normalized
from afterhours.queue_planning import finite, transition_cost
from afterhours.vdj_session import song_identity


class RollingQueue:
    def __init__(self):
        self.searched = set()
        self.missed = set()

    def tick(self, session, slack):
        with session.lock:
            state = session.snapshot()
            if (not state.get('rollingQueue') or not state['active'] or state['phase'] != 'playing' or dj_settings.lead(state) != 'all'
                    or not state['setlist']['allowReorder'] or state['copilot'].get('held')):
                return
            if slack < 4 or session.cancel.is_set():
                return
            pending = state.get('queuedUpdate') or {}
            miss=pending.get('catalogMissId')
            if (miss and pending.get('status')=='blocked' and pending.get('rollingTrackId')==miss
                    and not state.get('resumeBlocked') and not session.background_job):
                self.missed.add(miss)
                session.update(queuedUpdate=None,rollingQueue={'status':'searching'})
                session.event('The next candidate was not found. Searching for another recording.', 'queue')
                pending={}
            if pending.get('status') == 'blocked' and state['rollingQueue'].get('status') != 'held':
                session.update(rollingQueue={'status': 'held'})
            if pending or session.background_job:
                return

            order = state['order']
            if not order or len(order) - state['index'] > 3:
                return
            quick = state['mixStyle'] == 'quick'
            if not quick and not (state.get('executionPlan') or {}).get('applied'):
                return
            ids = [track['id'] for track in order]
            titles = {song_identity(track)[0] for track in order}
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

            candidates = [track for track in state['candidates']
                          if track['id'] not in unavailable and track['id'] not in ids and track['id'] not in self.missed
                          and song_identity(track)[0] not in titles
                          and finite(track.get('bpm')) and track['bpm'] > 0]
            for track in sorted(candidates, key=score):
                if session.cancel.is_set():
                    return
                try:
                    session.revise_queue(ids + [track['id']], reason=f"Continuing the set with {track['title']}.",
                                         source='planner')
                except (ValueError, BridgeError):
                    continue
                session.state['queuedUpdate']['rollingTrackId']=track['id']
                session.update(rollingQueue={'status': 'preparing', 'next': track['title']})
                return

            if session.catalog_job and not session.catalog_job.done:
                return
            if slack < (9 if quick else 18):
                return
            for track in reversed(order[-3:]):
                term = str(track.get('artist') or '').strip()
                key = normalized(term)
                if not key or key in self.searched or len(term) > 95 or ';' in term:
                    continue
                self.searched.add(key)
                target = state.get('quickTargetBpm') or tail['bpm']
                try:
                    session.discover_more(term, bpm_min=max(40, target * .90), bpm_max=min(300, target * 1.11))
                except (ValueError, BridgeError):
                    continue
                session.catalog_job.rolling = True
                session.update(rollingQueue={'status': 'searching', 'term': term})
                return
            if state['rollingQueue'].get('status') != 'held':
                session.update(rollingQueue={'status': 'held'})
