"""Cue crate browsing and exact queue edits. No transport or provider calls.

The existing session owns discovery, validation, preparation and playback.
This boundary keeps search presentation and idempotent operator receipts apart
from autonomous selection. Its state is ephemeral and is not replayed on boot.
"""
import copy
import re
from afterhours.planner_journal import binding

STOP = set('the and for something with find play any some that get track tracks record records from more have got can you please me by'.split())


def query_words(text):
    return [v for v in re.findall(r'[^\W_]+', text.casefold()) if len(v) >= 3 and v not in STOP]


def intent(text):
    value = text.strip().casefold().rstrip('.!?')
    if value in ('skip', 'skip this pick', 'something else', 'another track', 'not that', 'instead'):
        return 'skip'
    if value in ('darker', 'play darker', 'something darker', 'play something darker', 'go deeper'):
        return 'darker'
    if value in ('brighter', 'play brighter', 'something brighter', 'play something brighter', 'lift it'):
        return 'brighter'
    return 'search'


def browse_request(text):
    """Only explicit browsing bypasses the DJ's action planner.

    A screen is not an intent: 'add', 'play' and compound find-and-queue
    directions must have the same effect in Catalog, Control and Talk.
    """
    value = re.sub(r'^(?:(?:can|could|would) you\s+|please\s+)+', '', text.strip().casefold())
    if re.search(r'\b(?:and|then)\s+(?:please\s+)?(?:add|queue|play|mix|put|start)\b', value):
        return False
    return bool(re.match(r'^(?:find\b|show\b|search\b|look for\b|do (?:you|we) have\b|have (?:you|we) got\b)', value))


def import_query(text):
    """An explicit library addition discovers records without changing a set."""
    value = re.sub(r'^(?:(?:can|could|would) you\s+|please\s+)+', '', text.strip(), flags=re.I)
    match = re.fullmatch(r'(?:add|import|bring)\s+(?:some\s+)?(.+?)\s+(?:to|into)\s+(?:(?:my|the|our)\s+)?(?:catalog|library|crate)[.!?]?', value, re.I)
    return match.group(1).strip() if match else None


def context(session, current_id=None):
    state = binding(session)
    order = state['orderIds']
    index = order.index(current_id) if current_id in order else session.get('index', -1)
    if not isinstance(index, int) or not -1 <= index < len(order):
        index = -1
    return {**state, 'index': index, 'currentId': current_id}


def current(session):
    try:
        program = session.program() if hasattr(session, 'program') else None
    except (KeyError, TypeError, IndexError, ValueError):
        # Optional program facts can be absent during connection/startup.
        # They must not make the shared conversation snapshot unavailable.
        return None
    return program.get('current') if isinstance(program, dict) and program.get('connected') is True else None


def search(text, records):
    terms = query_words(text)
    ranked = []
    for position, record in enumerate(records):
        hard = (str(record.get('title') or '') + ' ' + str(record.get('artist') or '')).casefold()
        soft = ' '.join(str(record.get(k) or '') for k in ('genre', 'tags', 'reason')).casefold()
        score = sum(3 if term in hard else 1 if re.sub(r'(er|y|s)$', '', term) in soft else 0 for term in terms)
        if score:
            ranked.append((-score, position, record['id']))
    return [row[2] for row in sorted(ranked)[:3]]


def play_next_ids(track_id, state, expected):
    """Preserve history/current plus the first upcoming recording, even paused."""
    order = (state.get('queuedUpdate') or {}).get('tracks') or state.get('order', [])
    ids = [r['id'] for r in order]
    start = expected['index'] + 1
    prefix, future = ids[:start], ids[start:]
    if track_id in ids:
        return ids, True, min(len(ids), start + 1)
    future.insert(min(1, len(future)), track_id)
    return prefix + future[:5], False, min(len(ids), start + 1)


class CatalogBrowser:
    def __init__(self, clock):
        self.clock = clock
        self.browse = None
        self.receipts = []
        self.search_cache = None

    def active(self):
        return bool(self.browse and self.clock() < self.browse['expiresAt'])

    def open(self, text, request_id, state):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1600:
            raise ValueError('Use 1–1,600 characters to find records.')
        if self.browse and self.browse['id'] == request_id:
            return
        self.browse = {'id': request_id, 'query': text.strip(), 'openedAt': self.clock(),
                       'expiresAt': self.clock() + 16, 'sessionId': state['id']}

    def close(self, search_id):
        if self.browse and self.browse['id'] == search_id:
            self.browse = None

    def snapshot(self, session, journal):
        s = session.snapshot()
        expected = context(s, current(session))
        records = {r['id']: r for r in s.get('order', []) + s.get('candidates', [])}
        archive = getattr(session, 'crate', None)
        result = {'version': 1, 'scope': 'available-crate', 'availableCount': archive.summary()['count'] if archive else len(records),
                  'context': expected, 'search': None, 'receipt': copy.deepcopy(self.receipts[-1]) if self.receipts else None}
        if self.active() and self.browse['sessionId'] == s['id']:
            if archive:
                key = (self.browse['id'], archive.summary()['revision'])
                if not self.search_cache or self.search_cache['key'] != key:
                    self.search_cache = {'key': key, 'ids': [r['id'] for r in archive.search(self.browse['query'])]}
                ids = self.search_cache['ids']
            else:
                ids = search(self.browse['query'], list(records.values()))
            result['search'] = {**self.browse, 'ids': ids, 'best': None, 'status': 'found' if ids else 'empty'}
        return result

    def play_next(self, session, track_id, request_id, expected):
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,100}', request_id):
            raise ValueError('A Catalog request identity is required.')
        prior = next((r for r in self.receipts if r['id'] == request_id), None)
        if prior:
            if prior['trackId'] != track_id or prior['before'] != expected:
                raise ValueError('This request identity already belongs to a different queue edit.')
            return copy.deepcopy(prior)
        with session.lock:
            s = session.snapshot()
            if expected != context(s, current(session)):
                raise ValueError('The queue changed before this pick arrived. Pick the record again.')
            track = next((r for r in s['candidates'] if r['id'] == track_id), None)
            if track is None and hasattr(session, 'hydrate_records'):
                recalled = session.hydrate_records([track_id])
                track = recalled[0] if recalled else None
            if track is None:
                raise ValueError('This recording is no longer available. Search it again.')
            wanted, already, protected = play_next_ids(track_id, s, expected)
            if not already:
                # A single-record crate remains a selection, never an implicit Play.
                if len(wanted) == 1:
                    if s['active'] or any(d.get('playing') for d in (s.get('telemetry') or {}).get('decks', [])):
                        raise ValueError('The loaded decks are not part of this queue. Take over before replacing the set.')
                    session.state.update(order=[copy.deepcopy(track)], phase='catalog-ready', analysis=None,
                                         aiReview=None, executionPlan=None, queuedUpdate=None)
                else:
                    session.revise_queue(wanted, reason=f"Operator picked {track['title']} from Catalog.", preserve_prefix=protected)
                session.state['setlist']['sourceById'][track_id] = 'operator'
            after = session.snapshot()
            staged = (after.get('queuedUpdate') or {}).get('tracks') or []
            accepted = staged if staged else after.get('order', [])
            if [r['id'] for r in accepted] != wanted:
                raise ValueError('The queue edit was not confirmed.')
            receipt = {'id': request_id, 'trackId': track_id, 'at': self.clock(), 'before': expected,
                       'after': context(after, current(session)), 'orderIds': wanted,
                       'status': 'already' if already else 'staged' if staged else 'selected',
                       'message': f"{track['title']} is already in the queue." if already else
                           f"{track['title']} is queued after the prepared next record." if protected > expected['index'] + 1 else
                           f"{track['title']} is queued first.",
                       'note': 'Already queued' if already else 'You asked'}
            if staged and not already:
                receipt['message'] += ' Its handoff is being prepared.' if after['active'] else ' Its handoff will be prepared when the set starts.'
            self.receipts = (self.receipts + [receipt])[-32:]
            session.event(receipt['message'], 'catalog-pick')
            return copy.deepcopy(receipt)
