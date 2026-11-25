"""Process-local run permission and bounded planner views; never transport."""
import copy
import math
import uuid

DECISIONS = 60
SECONDS = 30 * 60
MAX_SECONDS = 6 * 60 * 60
# 60 decisions, each at most five bounded planner requests since the planner
# may look things up before it decides (issue #73, approved 11 November 2026).
COST_CEILING_USD = 2.90


def history_ids(session):
    """Only observed progress in an executing run can enlarge a queue."""
    audible = session.get('active') or any(d.get('playing') for d in (session.get('telemetry') or {}).get('decks', []))
    count = max(0, session.get('index', -1)) if audible else 0
    return [t['id'] for t in session.get('order', [])[:count]]


def planning_view(session):
    """The host retains heard recordings; model slots start at the current one."""
    prefix = history_ids(session)
    queued = (session.get('queuedUpdate') or {}).get('tracks') or session.get('order', [])
    # Keep the established full-order/absolute-slot contract while it fits.
    # At 12 records, a rolling view makes room for the next extension.
    if not prefix or len(queued) < 12:
        return session, []
    offset = len(prefix)
    view = copy.deepcopy(session)
    view['order'] = view.get('order', [])[offset:]
    view['index'] = 0
    if view.get('queuedUpdate'):
        view['queuedUpdate']['tracks'] = view['queuedUpdate'].get('tracks', [])[offset:]
    heard = set(prefix)
    view['candidates'] = [t for t in view.get('candidates', []) if t['id'] not in heard]
    view['setPins'] = [tid for tid in view.get('setPins', []) if tid not in heard]
    view['setPlayedPins'] = [tid for tid in view.get('setPlayedPins', []) if tid not in heard]
    view.setdefault('setlist', {})['lockedIds'] = [tid for tid in view.get('setlist', {}).get('lockedIds', []) if tid not in heard]
    plan = view.get('executionPlan') or {}
    route = plan.get('tempoSchedule', [])[offset:]
    if route:
        plan['targetBpm'] = route[0]['fromBpm']
    plan['tempoSchedule'] = route
    # The engine retains exact armed transitions and fits future measured cues.
    # A partial-window model cannot author absolute transition indices.
    plan['selections'] = []
    plan['transitions'] = plan.get('transitions', [])[offset:]
    return view, prefix


def expand_decision(raw, prefix):
    if not prefix or not isinstance(raw, dict) or not raw.get('trackIds'):
        return raw
    if not isinstance(raw['trackIds'], list) or not 2 <= len(raw['trackIds']) <= 12:
        raise ValueError('Choose 2–12 current and future recordings.')
    if raw.get('mixTransitions') is not None:
        raise ValueError('Rolling queue changes use the existing measured preparation path.')
    result = copy.deepcopy(raw)
    result['trackIds'] = prefix + result['trackIds']
    review = result.get('selectionReview')
    if isinstance(review, dict) and type(review.get('slot')) is int:
        review['slot'] += len(prefix)
    # Crowd ballots currently bind absolute slots below 12. Do not reinterpret
    # a relative slot as an absolute room vote.
    result['crowdChoice'] = None
    return result


class RunCuration:
    def __init__(self, clock):
        self.clock = clock
        self.grant = None
        self.requests = set()

    def admit(self, request, session, until=None):
        if (not request or request.get('id') in self.requests or
                request.get('status') not in ('choosing', 'preparing', 'starting') or
                session.get('active') or any(d.get('playing') for d in (session.get('telemetry') or {}).get('decks', []))):
            return False
        if self.grant and self.grant['status'] in ('preparing', 'active'):
            return False
        # A finite process-local receipt set prevents uncertain repeated starts
        # from buying another allowance after conversation history is trimmed.
        if len(self.requests) >= 128:
            return False
        self.requests.add(request['id'])
        self.grant = {'id': uuid.uuid4().hex, 'requestId': request['id'],
            'sessionId': session['id'], 'startedAt': None, 'remaining': DECISIONS,
            'expiresAt': self.expiry(until), 'status': 'preparing',
            'message': 'Curation will begin after playback is confirmed.',
            'costCeilingUSD': COST_CEILING_USD}
        return True

    def expiry(self, until=None):
        """Use the approved night's end without increasing decisions or cost."""
        now = self.clock()
        if type(until) in (int, float) and math.isfinite(until):
            return max(now + SECONDS, min(until, now + MAX_SECONDS))
        return now + SECONDS

    def end(self, status, message):
        if self.grant and self.grant['status'] in ('preparing', 'active', 'exhausted'):
            self.grant.update(status=status, message=message)

    def reconcile(self, session, request, lead):
        grant = self.grant
        if not grant or grant['status'] not in ('preparing', 'active', 'exhausted'):
            return
        if self.clock() >= grant['expiresAt']:
            self.end('expired', 'The curation allowance ended. Prepared music can continue.'); return
        if session.get('id') != grant['sessionId'] or lead != 'all' or (session.get('copilot') or {}).get('mode') == 'off':
            self.end('revoked', 'Automatic curation ended when session control changed.'); return
        phase = session.get('phase')
        if phase in ('takeover', 'error'):
            self.end('blocked' if phase == 'error' else 'revoked',
                     session.get('message') or 'Automatic curation stopped with the playing session.'); return
        if grant['status'] == 'preparing':
            if not request or request.get('id') != grant['requestId'] or request.get('status') in ('cancelled', 'failed', 'ended', 'stopped', 'completed'):
                self.end('revoked', 'The approved start ended before curation began.'); return
            if request.get('status') == 'playing' and session.get('active') and any(d.get('playing') for d in (session.get('telemetry') or {}).get('decks', [])):
                grant.update(status='active', startedAt=session.get('startedAt'),
                    message='Keeping the upcoming queue supplied within this run’s allowance.')
        elif (not session.get('active') or phase in ('stopped', 'complete', 'completed') or
              session.get('startedAt') != grant['startedAt']):
            self.end('revoked', 'This run ended. Automatic curation is off.')

    def owns(self, session):
        """Ending this grant cannot transfer its background work to Talk."""
        grant = self.grant
        return bool(grant and grant['sessionId'] == session.get('id') and
                    (grant['status'] == 'preparing' or grant['startedAt'] == session.get('startedAt')))

    def available(self):
        grant = self.grant
        return bool(grant and grant['status'] == 'active' and grant['remaining'] > 0 and self.clock() < grant['expiresAt'])

    def reserve(self):
        if not self.available():
            return None
        self.grant['remaining'] -= 1
        if not self.grant['remaining']:
            self.grant.update(status='exhausted', message='All 60 curation decisions were used. Prepared music can continue.')
        return self.grant['id']

    def accepts(self, token):
        return bool(self.grant and token == self.grant['id'] and self.grant['status'] in ('active', 'exhausted') and self.clock() < self.grant['expiresAt'])

    def snapshot(self, session):
        if not self.grant:
            return None
        plan = session.get('executionPlan') or {}
        prepared = {r['trackId'] for r in plan.get('selections', [])} if plan.get('applied') else set()
        count = 0
        for row in session.get('order', [])[max(0, session.get('index', -1)) + 1:]:
            if row['id'] not in prepared:
                break
            count += 1
        result = {k: v for k, v in self.grant.items() if k not in ('id', 'requestId')}
        result['preparedUpcoming'] = min(100, count)
        return result
