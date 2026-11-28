"""Small, provider-neutral controls and state views for the DJ planner."""

CONTROL_HELP = {
    'hold': 'Keep the current track playing and hold its automatic handoff.',
    'go': 'Bring in the prepared next track on a verified upcoming phrase.',
    'skip': 'Reject the upcoming selection and ask the queue to find another.',
    'shorter-blends': 'Prefer shorter transitions on future, unprepared handoffs.',
    'longer-blends': 'Prefer longer transitions on future, unprepared handoffs when measured windows fit.',
}
BLEND_PREFERENCE_STOPS = {'shorter-blends': 1, 'longer-blends': 3}


def offered_controls(session):
    preferences = list(BLEND_PREFERENCE_STOPS)
    if not session.get('active') or session.get('phase') != 'playing':
        return preferences
    index = session.get('index', -1)
    if not 0 <= index < len(session.get('order', [])) - 1:
        return preferences
    copilot = session.get('copilot') or {}
    mode = copilot.get('mode', 'leading')
    if mode not in ('leading', 'mix') or (copilot.get('command') or {}).get('status') == 'pending':
        return preferences
    if (session.get('executionPlan') or {}).get('applied'):
        return [action for action in CONTROL_HELP if mode != 'mix' or action != 'skip']
    if session.get('mixStyle') == 'quick' and (session.get('timing') or {}).get('basis') == 'native-beat-grid':
        preferences.append('go')
    return preferences


def queue_status(session):
    index = session.get('index', -1)
    order = session.get('order', [])
    proposed = session.get('queuedUpdate') or {}
    future = proposed.get('tracks') or order

    def recording(track):
        return {key: track.get(key) for key in ('id', 'title', 'artist', 'bpm', 'genre')}

    return {
        'phase': session.get('phase'),
        'current': recording(order[index]) if 0 <= index < len(order) else None,
        'upcoming': [recording(track) for track in future[max(0, index + 1):max(0, index + 1) + 4]],
        'preparation': proposed.get('status'),
        'refill': (session.get('rollingQueue') or {}).get('status'),
        'held': bool((session.get('copilot') or {}).get('held')),
    }


def mixer_status(session):
    telemetry = session.get('telemetry') or {}
    timing = session.get('timing') or {}
    plan = session.get('executionPlan') or {}
    index = session.get('index', -1)
    recipes = plan.get('transitions') or []
    recipe = recipes[index] if 0 <= index < len(recipes) and plan.get('applied') else {}
    command = (session.get('copilot') or {}).get('command') or {}
    return {
        'sampledAt': telemetry.get('sampledAt'),
        'mixing': session.get('phase') == 'mixing',
        'decks': [{k: deck.get(k) for k in ('deck', 'title', 'playing', 'bpm', 'beatPosition', 'position', 'sync')}
                  for deck in (telemetry.get('decks') or [])[:4]],
        'crossfader': telemetry.get('crossfader'),
        'nextHandoff': {k: timing.get(k) for k in ('mixAtBeat', 'fadeBeats', 'basis')} if recipe else None,
        'technique': recipe.get('label'),
        'liveEqPhase': (session.get('transitionControl') or {}).get('phase'),
        'leadControl': (session.get('transitionControl') or {}).get('leadControl'),
        'lastCommand': {k: command.get(k) for k in ('action', 'status', 'message')} if command else None,
    }
