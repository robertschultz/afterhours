"""Optional background discovery for the native rolling queue."""

from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
import re
from threading import Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from afterhours.dj_discovery import credited_names, normalized

LOG = logging.getLogger(__name__)
RETRY_INTERVAL = 20
CATALOG_CACHE_LIMIT = 512


@dataclass
class SearchMemory:
    catalog: dict = field(default_factory=dict)
    searched_artists: set = field(default_factory=set)
    genre_pages: dict = field(default_factory=dict)
    genre_refresh_after: dict = field(default_factory=dict)
    lookup_retry_after: dict = field(default_factory=dict)
    search: object = None

    def position(self):
        return frozenset(self.searched_artists), self.genre_pages.copy(), self.genre_refresh_after.copy()

    def scheduled_lookup_due(self, now):
        return (
            any(now >= scheduled for scheduled in self.lookup_retry_after.values())
            or any(self.genre_pages.get(genre) is None and now >= scheduled
                   and now >= self.lookup_retry_after.get(f'genre:{genre}', now)
                   for genre, scheduled in self.genre_refresh_after.items())
        )


def linked_apple_recording(public_reference, source_url):
    """Accept Apple track IDs only from direct links that corroborate the public result."""
    if (not isinstance(public_reference, (tuple, list)) or len(public_reference) != 2
            or public_reference[0] != 'itunes-store' or not isinstance(source_url, str)):
        return None
    track_id = public_reference[1]
    if not isinstance(track_id, str) or not re.fullmatch(r'[1-9][0-9]{0,17}', track_id):
        return None
    try:
        url = urlsplit(source_url)
    except ValueError:
        return None
    if url.scheme != 'https' or url.netloc != 'music.apple.com':
        return None
    if re.fullmatch(r'/[a-z]{2}/album/[^/]+/[1-9][0-9]{0,17}', url.path):
        if parse_qs(url.query).get('i') == [track_id]:
            return 'apple-music', track_id
    if re.fullmatch(r'/[a-z]{2}/song/(?:[^/]+/)?' + track_id, url.path):
        return 'apple-music', track_id
    return None


def propose(order, style, neighbors, *, memory, excluded):
    """Translate the full existing queue into an Algorhythm continuation brief."""
    from algorhythm.catalog import ArtistRadio, Recording
    from algorhythm.curation import GENRE_FAMILIES, Selection, SetBrief, SetDirection, SetProposal
    from algorhythm.rolling import RollingCurator
    from algorhythm.sources import public_catalog_search

    now = datetime.now(timezone.utc)
    seed_names = credited_names(order[0].get('artist'))
    if not seed_names or not style:
        return ()
    seed = next(iter(seed_names.values()))
    family = 'christian' if style in GENRE_FAMILIES['christian'] else style
    genres = GENRE_FAMILIES.get(family, (style,))
    brief = SetBrief(seed, (len(order) + 4) * 150, genres=genres)
    selections = []
    for index, row in enumerate(order):
        catalog = row.get('catalog') or {}
        track = Recording(catalog.get('provider') or 'afterhours', catalog.get('track_id') or row['id'],
                          row['title'], tuple(credited_names(row.get('artist')).values()), '',
                          (row['genre'],) if row.get('genre') else (), row.get('bpm'),
                          version=row.get('remix'), camelot=row.get('camelot'))
        selections.append(Selection(track, index * 150, 150, ()))
    direction = SetDirection('requested', genres, brief.target_bpm)
    prefix = SetProposal(brief, tuple(selections), 4 * 150, direction)
    radio = ArtistRadio('', seed, now, '', (), tuple((name, name) for name in neighbors))
    if memory.search is None:
        memory.search = public_catalog_search()
    plan, attempts = RollingCurator(brief, radio, memory.search, catalog=memory.catalog,
                                    searched_artists=memory.searched_artists, genre_pages=memory.genre_pages,
                                    genre_refresh_after=memory.genre_refresh_after,
                                    lookup_retry_after=memory.lookup_retry_after).refill(
        now, prefix=prefix, max_new_tracks=4, search_budget=2, excluded_queries=excluded,
    )
    overflow = max(0, len(memory.catalog) - CATALOG_CACHE_LIMIT)
    for key in tuple(memory.catalog)[:overflow]:
        del memory.catalog[key]
    if len(plan.selections) == len(order) and attempts and all(row['status'] == 'provider-error' for row in attempts):
        raise RuntimeError('Catalog provider unavailable')
    return tuple({'artist':', '.join(row.recording.artists), 'title':row.recording.title,
                  'version':row.recording.version, 'public_reference':row.recording.ref,
                  'source_url':row.recording.source_url}
                 for row in plan.selections[len(order):])


class AlgorhythmRefill:
    def __init__(self, lookup=propose, *, enabled=None, clock=monotonic, wall_clock=None):
        self.enabled = os.getenv('AFTERHOURS_ALGORHYTHM') == '1' if enabled is None else enabled
        self.lookup = lookup
        self.clock = clock
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.key = None
        self.future = None
        self.reported = False
        self.scope = None
        self.memory = None
        self.requested_at = -float('inf')
        self.search_position = None

    def hints(self, state, order, style, neighbors, *, activity=None, searched=()):
        if not self.enabled or not order or not style:
            return ()
        scope = (state.get('id'), state.get('startedAt'), state.get('agentDirection'), style)
        key = scope + (tuple(row['id'] for row in order),)
        excluded = frozenset(searched)
        now = self.clock()
        if self.future is not None:
            if not self.future.done():
                return ()
            if self.key == key:
                lookup_due = self.memory.scheduled_lookup_due(self.wall_clock())
                try:
                    rows = self.future.result()
                except Exception:
                    if activity and not self.reported:
                        activity('Background catalog lookup is unavailable. Afterhours can continue searching its crate.',
                                 'algorhythm-discovery', stage='unavailable')
                        self.reported = True
                    if (now - self.requested_at < RETRY_INTERVAL
                            or (self.memory.lookup_retry_after and not lookup_due)):
                        return ()
                else:
                    fresh = tuple(row for row in rows if normalized(f"{row['artist']} {row['title']}") not in excluded)
                    if activity and not self.reported:
                        count = len(fresh)
                        message = (
                            f'Found {count} catalog {"lead" if count == 1 else "leads"} for Afterhours to verify.'
                            if count else 'No new matches from the background lookup. Afterhours can check other sources.'
                        )
                        activity(message, 'algorhythm-discovery', stage='leads' if count else 'no-match')
                        self.reported = True
                    searched_more = self.memory.position() != self.search_position
                    if fresh or (not rows and not searched_more and not lookup_due) or now - self.requested_at < RETRY_INTERVAL:
                        return fresh
        if self.scope != scope:
            self.scope, self.memory = scope, SearchMemory()
        self.key = key
        self.requested_at = now
        self.search_position = self.memory.position()
        self.reported = False
        self.future = future = Future()
        snapshot = tuple({**row, 'catalog':dict(row.get('catalog') or {})} for row in order)
        neighbor_snapshot = tuple(neighbors)
        memory = self.memory

        def work():
            try:
                future.set_result(self.lookup(snapshot, style, neighbor_snapshot, memory=memory, excluded=excluded))
            except Exception as exc:
                LOG.debug('Algorhythm lookup unavailable: %s', type(exc).__name__)
                future.set_exception(exc)

        Thread(target=work, name='afterhours-algorhythm-refill', daemon=True).start()
        if activity:
            activity('Checking related tracks to refill the queue.', 'algorhythm-discovery', stage='searching')
        return ()
