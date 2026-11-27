"""Identity-bound operator steering, without provider calls or transport writes.

The session's single OSC worker executes accepted changes. These pure helpers
only recognize explicit controls and choose from already measured recordings.
Holding does not synthesize a loop or invent another cue: the recorded audio
continues, and Go requires a replay-verified exit still ahead of the playhead.
"""
from afterhours import deck_rotation
import copy
import math
import re
import time

from afterhours import cue_planner


def intent(text):
    if not isinstance(text, str):
        return None
    value = re.sub(r"[.!?]+$", "", text.strip().casefold().replace('’', "'"))
    value = re.sub(r'\b(?:um|uh)\b|[,;]', ' ', value)
    value = ' '.join(value.split())
    # Speech commonly contains a repeated lead-in. Match the entire imperative,
    # never a keyword inside a question, negation, or future instruction.
    value = re.sub(r'\b(\w+)(?:\s+\1)+\b', r'\1', value)
    value = re.sub(r'^(?:you )?(?:can|could|would|will) you (?:please )?', '', value)
    value = re.sub(r'^(?:okay |ok |yes |just |please )+', '', value)
    value = re.sub(r"^please\s+|,?\s+please$", "", value).strip()
    value = re.sub(r'^go ahead and ', '', value)
    phrases = {
        'hold': {'hold', 'hold it', 'hold the mix', 'wait', 'wait a sec', 'not yet'},
        'go': {'go', 'go now', 'go for it', 'bring it in', 'mix now', 'resume the mix',
               'mix it in', 'mix it in. go for it', 'mix into the next track',
               'next', 'next track', 'play the next track', 'skip to the next track',
               'play next', 'go to the next track', 'next song', 'play the next song',
               'start mixing into the next track', 'start mixing the next track',
               'transition to the next track', 'transition into the next track',
               'transition up to the next track', 'mix in the next track',
               'bring in the next track', 'start the next transition',
               'start the next mix', 'mix to the next track', 'transition now',
               'start transitioning to the next track', 'start transitioning into the next track',
               'beat match into the next track', 'beatmatch into the next track',
               'beat match the next track', 'beatmatch the next track'},
        'skip': {'skip', 'skip this pick', 'skip the next track', 'something else', 'another track'},
        'off': {'copilot off', 'turn copilot off', 'take over', 'takeover'},
        'on': {'copilot on', 'turn copilot on'},
        'resume': {'resume copilot', 'take back over', 'take over again', 'you take over',
               'copilot take over', 'take control back', 'resume automatic mixing'},
    }
    return next((name for name, values in phrases.items() if value in values), None)


def context(snapshot):
    index = snapshot.get('index', -1)
    return {'sessionId': snapshot.get('id'), 'startedAt': snapshot.get('startedAt'),
            'planId': (snapshot.get('executionPlan') or {}).get('id'), 'index': index,
            'trackIds': [t['id'] for t in snapshot.get('order', [])[max(0, index):max(0, index)+2]]}


def check_context(snapshot, expected):
    if not isinstance(expected, dict) or expected != context(snapshot):
        raise ValueError('The set changed before this command arrived. No control was sent.')


def held_recording(snapshot):
    """Pause receipt, deck and recording expected by an explicit handback."""
    copilot = snapshot.get('copilot') or {}
    receipt = copilot.get('command') or {}
    tracks = context(snapshot)['trackIds']
    decks = (snapshot.get('telemetry') or {}).get('decks') or []
    if (snapshot.get('active') or snapshot.get('phase') != 'takeover' or copilot.get('mode') != 'off'
            or receipt.get('action') != 'off' or receipt.get('status') != 'confirmed'
            or not isinstance(receipt.get('id'), str) or not tracks
            or any(deck.get('playing') is not False for deck in decks)):
        return None
    lead = [deck for deck in decks if deck.get('id') == tracks[0] and deck.get('loaded') is True]
    return [receipt['id'], lead[0]['deck'], tracks[0]] if len(lead) == 1 else None


def rotation_of(decks):
    """The rotation these readings cover: A/B, or A to D in order. Anything else is not a set."""
    numbers = tuple(d.get('deck') for d in decks) if isinstance(decks, list) else ()
    return numbers if numbers in (deck_rotation.TWO, deck_rotation.FOUR) else None


def playing_pair(snapshot, now=None):
    telemetry = snapshot.get('telemetry') or {}
    sampled = telemetry.get('sampledAt')
    now = time.time() if now is None else now
    if (type(sampled) not in (int, float) or not math.isfinite(sampled)
            or not 0 <= now-sampled <= 3):
        raise ValueError('Fresh deck readings are required. No control was sent.')
    decks = telemetry.get('decks', [])
    playing = [d for d in decks if d.get('playing') is True]
    index = snapshot.get('index', -1)
    order = snapshot.get('order', [])
    rotation = rotation_of(decks)
    if rotation is None or len(playing) != 1 or not 0 <= index < len(order):
        raise ValueError('This control needs one playing deck outside a blend.')
    current = playing[0]
    if current.get('id') != order[index]['id'] or current.get('deck') not in rotation:
        raise ValueError('The playing recording changed. No control was sent.')
    incoming = next((d for d in decks if d.get('deck') == deck_rotation.following(current['deck'], rotation)), None)
    if not incoming or incoming.get('playing') is not False:
        raise ValueError('The incoming deck is not confirmed paused.')
    if index+1 < len(order) and incoming.get('id') != order[index+1]['id']:
        raise ValueError('The cued recording changed. No control was sent.')
    cross = telemetry.get('crossfader')
    if (type(cross) not in (int, float) or not math.isfinite(cross)
            or not 0 <= cross <= 1 or abs(cross-deck_rotation.crossfader_end(current['deck'])) > .02):
        raise ValueError('The incoming deck is not confirmed silent at the crossfader.')
    return current, incoming


def resumable(snapshot):
    """Only an owned manual set or a known pre-dispatch alignment stop.

    Older builds recorded alignment as a generic error. Catalog searches may
    clear lastError, so retain the latest error event as its recovery evidence.
    Unknown failures and unconfirmed cleanup never authorize transport.
    """
    if snapshot.get('resumeBlocked'):
        return False
    if snapshot.get('phase') == 'takeover':
        return True
    if snapshot.get('phase') != 'error':
        return False
    error = snapshot.get('lastError') or next((e.get('text') for e in reversed(snapshot.get('events', []))
                                              if e.get('kind') == 'error'), None)
    return error == 'Deck tempos moved after alignment. No transition was sent.'


def resume_pair(snapshot, allow_paused=False):
    """Explicit handback can close a silent channel and resume paused audio.

    Inspect the real pair first. A synthetic playing flag is used only to reuse
    identity/freshness validation; it is never stored or published as telemetry.
    """
    telemetry = snapshot.get('telemetry') or {}
    decks = telemetry.get('decks', [])
    playing = [d for d in decks if d.get('playing') is True]
    if len(playing) == 1:
        current = playing[0]
    elif allow_paused and rotation_of(decks) and all(d.get('playing') is False for d in decks):
        index = snapshot.get('index', -1)
        order = snapshot.get('order', [])
        current = next((d for d in decks if 0 <= index < len(order) and d.get('id') == order[index]['id']), None)
        if current is None:
            raise ValueError('The paused recording changed. No playback was started.')
    else:
        raise ValueError('Handback needs one playing deck or the paused prepared pair, outside a blend.')
    rotation = rotation_of(decks)
    if rotation is None or current.get('deck') not in rotation:
        raise ValueError('The current deck is unavailable.')
    check = copy.deepcopy(snapshot)
    check['telemetry']['crossfader'] = deck_rotation.crossfader_end(current['deck'])
    for deck in check['telemetry']['decks']:
        if deck['deck'] == current['deck']:
            deck['playing'] = True
    playing_pair(check)
    # Require a real, finite crossfader reading even when it needs closing.
    cross = telemetry.get('crossfader')
    if type(cross) not in (int, float) or not math.isfinite(cross) or not 0 <= cross <= 1:
        raise ValueError('The crossfader reading is unavailable.')
    return current, next(d for d in decks if d['deck'] == deck_rotation.following(current['deck'], rotation))


def retime(snapshot, current, fade_beats, final=False, *, entry_interval=None, actual_entry=None, earliest_beat=None):
    """Earliest verified exit with lead time, preserving the actual entry.

    A different candidate supplies only its measured exit region. The track is
    never sought or restarted, and a pending tempo ramp keeps its settle margin.
    """
    index = snapshot['index']
    plan = snapshot.get('executionPlan') or {}
    if not plan.get('applied'):
        raise ValueError('Steering the handoff needs an applied measured mix plan.')
    cue = plan['selections'][index]
    if current.get('id') != cue['trackId']:
        raise ValueError('The playing recording changed before retiming.')
    position = current.get('beatPosition')
    if type(position) not in (int, float) or not math.isfinite(position):
        raise ValueError('The native beat position is unavailable.')
    earliest = position+8  # two bars for bounded readbacks; never an instant cut
    if earliest_beat is not None:
        earliest = max(earliest, earliest_beat)
    rows = plan.get('tempoSchedule') or []
    if index < len(rows):
        row = rows[index]
        earliest = max(earliest, cue['entryBeat']+row['startOffsetBeats']+row['durationBeats']+row['settleBeats'])
    source = next((t for t in (snapshot.get('analysis') or {}).get('tracks', []) if t['id'] == current['id']), {})
    entry = actual_entry or cue
    target = earliest if entry_interval is None else entry['entryBeat'] + entry_interval * current['bpm'] / 60
    if entry_interval is not None and not final:
        # A timed run can flex to a nearby verified bar. A window several
        # minutes later belongs to cue-led pacing, not the requested cadence.
        tolerance = 10 * current['bpm'] / 60
        earliest = max(earliest, target - tolerance)
        cadence_end = target + tolerance
    blend = 0 if final else fade_beats
    valid = []
    for candidate in source.get('cueCandidates', []):
        if candidate.get('replayVerified') is not True or candidate['maxBlendBeats'] < blend:
            continue
        exit_beat = candidate['exitBeat']
        if entry_interval is None or final:
            if exit_beat >= earliest:
                valid.append(candidate)
            continue
        # Both endpoints of this audible outro were read from the native
        # grid. Bound the bar adjustment to the measured audio span, leaving
        # all the beats required for the proposed fade inside it.
        span = candidate['endBeat']-exit_beat
        first_bar = max(0, math.ceil((earliest-exit_beat)/4))
        last_bar = math.floor((min(span, candidate['maxBlendBeats'])-blend+1e-4)/4)
        last_bar = min(last_bar, math.floor((cadence_end-exit_beat)/4))
        if first_bar > last_bar:
            continue
        bars = min(last_bar, max(first_bar, round((target-exit_beat)/4)))
        if bars:
            seconds = (candidate['endSeconds']-candidate['exitSeconds']) * bars*4/span
            valid.append({**candidate,'exitBeat':exit_beat+bars*4,'exitSeconds':candidate['exitSeconds']+seconds})
        else:
            valid.append(candidate)
    if not valid:
        timing = ' within the requested cadence' if entry_interval is not None and not final else ' ahead'
        raise ValueError(f'No verified handoff remains{timing}. The current track keeps playing; take over or prepare another exit.')
    selected = min(valid, key=lambda c: (abs(c['exitBeat']-target), c['exitBeat']))
    window = cue_planner.execution_window(selected, position, fade_beats, final)
    window.update(entryBeat=entry['entryBeat'], entrySeconds=entry['entrySeconds'],
                  segmentBeats=window['endBeat']-entry['entryBeat'],
                  basis='operator-steered-measured-exit', originalCandidateId=cue['id'])
    if entry_interval is not None:
        window.update(basis='cadence-measured-exit', requestedEntryInterval=entry_interval,
                      plannedEntryInterval=(selected['exitBeat']-entry['entryBeat'])*60/current['bpm'])
    return window


def skip_plan(snapshot):
    """Choose another prepared future record, preserving heard/current tracks."""
    index = snapshot['index']
    order = snapshot['order']
    policy = snapshot.get('setlist') or {}
    if not policy.get('allowReorder', True):
        raise ValueError('Queue reordering is off. The pick is unchanged.')
    if snapshot.get('queuedUpdate'):
        raise ValueError('Finish the pending queue revision before replacing this pick.')
    if index+2 >= len(order):
        raise ValueError('There is no other prepared recording after this pick yet.')
    protected = index+1
    current = order[index]
    def rank(i):
        track = order[i]
        same_key = bool(current.get('camelot')) and track.get('camelot') == current['camelot']
        return (not same_key, abs(track['bpm']-current['bpm']), i)
    errors = []
    for replacement in sorted(range(index+2, len(order)), key=rank):
        revised = copy.deepcopy(order)
        revised[index+1], revised[replacement] = revised[replacement], revised[index+1]
        if any(t['id'] in policy.get('lockedIds', []) and t['id'] != revised[i]['id'] for i, t in enumerate(order)):
            continue
        try:
            prepared = cue_planner.complete_background_queue(snapshot, revised, [],
                'Operator requested another pick. Selected a prepared recording with a feasible measured tempo route.', protected)
            # Completion publishes this plan only after the silent-deck load is
            # confirmed. The old queue remains authoritative until then.
            return revised, prepared
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError(errors[0] if errors else 'The remaining prepared picks are locked. The queue is unchanged.')
