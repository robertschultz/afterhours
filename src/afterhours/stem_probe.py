"""Sample supported native per-stem meters on an owned, isolated spare deck."""
import time

from afterhours.background_analysis import BackgroundAnalysis, guard_isolation
from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.stem_profiles import MATCH_BEATS, STEMS, measure_window, save, valid_number
from afterhours.vdj_catalog import identity


def probe_window(session, client, deck, selection_id, role, start, duration, track_duration):
    prefix = f'deck {deck} '
    q = lambda verb, kind=None: session.q(client, prefix+verb, kind)

    def exact():
        return identity(str(client.query(prefix+'get_filepath','text'))) == selection_id

    def isolated():
        if (not exact() or float(q('volume')) != 0 or as_bool(q('pfl','bool'))
                or as_bool(q('is_audible','bool'))):
            raise BridgeError('Stem measurement stopped because the spare deck changed or became audible.')

    guard_isolation(client,deck)
    isolated()
    seek = max(0, start-.45)
    session.send(client, f'{prefix}goto {seek/track_duration*100:.9f}%')
    session.sleep(.12)
    if abs(float(q('get_time elapsed absolute'))/1000-seek)>.18:
        return None
    if not as_bool(q('has_stems "2.0"','bool')):
        return None

    rows = []
    dispatched = False
    try:
        guard_isolation(client,deck)
        isolated()
        dispatched = True
        session.send(client, prefix+'play')
        deadline = time.monotonic() + duration + 3
        while time.monotonic() < deadline:
            session.check()
            isolated()
            before = float(q('get_time elapsed absolute'))/1000
            if before >= start+duration:
                break
            if before >= start-.12 and as_bool(q('has_stems "ready"','bool')):
                levels = [float(q(f'get_level "{stem}"')) for stem in STEMS]
                after = float(q('get_time elapsed absolute'))/1000
                if (0 <= after-before <= .32 and all(valid_number(level,0,1) for level in levels)
                        and exact() and as_bool(q('play','bool'))):
                    rows.append(((before+after)/2,levels))
            session.sleep(.09)
    finally:
        if dispatched:
            # Cleanup runs even after cancellation. Never pause another record.
            if not exact():
                raise BridgeError('The spare recording changed; playback cleanup needs inspection.')
            client.send(prefix+'pause')
            if as_bool(client.query(prefix+'play','bool')):
                raise BridgeError('The isolated stem probe did not confirm Pause.')
    return measure_window(role,start,duration,rows)


def prepare(session, ids):
    """Precompute comparison windows, leaving the existing program untouched."""
    s = session.snapshot()
    plan = s.get('executionPlan') or {}
    if (s['active'] or not plan.get('applied')
            or (s.get('resumeBlocked') and not session.quarantined_preparation_decks())):
        raise ValueError('Prepare and pause a measured set before inspecting stem windows.')
    if (not isinstance(ids,list) or not 1 <= len(ids) <= 4
            or any(not isinstance(key,str) for key in ids) or len(set(ids)) != len(ids)):
        raise ValueError('Select one to four exact measured recordings for stem analysis.')
    by_id = {track['id']:track for track in s['order']}
    chosen = {cue['trackId']:cue for cue in plan['selections']}
    if any(key not in by_id or key not in chosen for key in ids):
        raise ValueError('Stem analysis needs recordings in the current measured plan.')
    from afterhours.cue_planner import verify_receipts
    verify_receipts(s['analysis'])
    available = {row['id']:row['cueCandidates'] for row in s['analysis']['tracks']}

    def work(client):
        current = session.snapshot()
        if current['id'] != s['id'] or (current.get('executionPlan') or {}).get('id') != plan['id']:
            raise BridgeError('The measured set changed. No stem playback was started.')
        native = client.status()
        if native.get('automix') or any(row.get('playing') for row in native['decks']):
            raise BridgeError('Pause the program decks and turn off Automix before stem measurement.')
        # Native status reports A/B only. C and D can also hold external playback.
        spare_decks = tuple(deck for deck in session.preparation_decks() if deck in (3,4))
        if not spare_decks or float(client.query('get_decks')) < 4:
            raise BridgeError('No empty isolated spare deck is available for stem measurement.')
        if any(as_bool(client.query(f'deck {deck} play','bool')) for deck in (3,4)):
            raise BridgeError('Pause decks C and D before stem measurement.')
        session.verify_preparation_quarantine(client)
        spare = next((deck for deck in spare_decks
                      if not as_bool(client.query(f'deck {deck} loaded','bool'))),None)
        if spare is None:
            raise BridgeError('No empty isolated spare deck is available for stem measurement.')
        results = []
        session.update(stemAnalysis={'status':'measuring','tracks':[]})
        for track_id in ids:
            session.check()
            track = by_id[track_id]
            owner = BackgroundAnalysis(session,'stem-probe',[track],deck=spare)
            rows = []
            try:
                owner.acquire(client)
                selection_id = track['engine']['selection_id']
                owner.requested_load = selection_id
                session.load(client,track,owner.deck)
                owner.loaded = selection_id
                # Native auto-PFL can enable itself when a deck loads. Close it
                # before authorizing an isolated Play.
                session.send(client,f'deck {owner.deck} play ? nothing : deck {owner.deck} pfl off')
                guard_isolation(client,owner.deck)
                selected = chosen[track_id]
                alternatives = [cue for cue in available[track_id]
                                if cue.get('replayVerified') is True and cue['id'] != selected['id']]
                for cue in [selected, *alternatives[:1]]:
                    bars = cue['maxBlendBeats']
                    if bars < MATCH_BEATS:
                        continue
                    for role, start_key, end_key in (
                        ('entry','entrySeconds','introEndSeconds'),('exit','exitSeconds','endSeconds')
                    ):
                        start = cue[start_key]
                        duration = (cue[end_key]-start)*MATCH_BEATS/bars
                        if not valid_number(duration,2,16):
                            continue
                        session.update(message=f'Measuring {role} stem levels for {track["title"]} on the muted spare deck.')
                        row = probe_window(session,client,owner.deck,selection_id,role,start,duration,cue['recordingDuration'])
                        if row:
                            rows.append(row)
            finally:
                try:
                    owner.close(client)
                except (OSError, BridgeError, ValueError):
                    session.remember_cleanup_failure('background',owner)
                    raise
            if rows:
                save(track_id,rows)
            measured = [{key: row[key] for key in ('role', 'startSeconds', 'durationSeconds')} for row in rows]
            results.append({'trackId':track_id,'windowCount':len(measured),'measuredWindows':measured})
        session.update(phase=s['phase'],stemAnalysis={'status':'complete','tracks':results},
                       message=f'Inspected {len(results)} recordings for timed stem evidence. Program decks remain paused.')

    session._launch(work,'analyzing-stems',wait_for_control=True)
    return session.snapshot()
