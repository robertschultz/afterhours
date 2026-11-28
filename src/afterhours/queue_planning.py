"""Deterministic placement and runway for the engine's exact queue."""

import math
import re

from afterhours import session_curation
from afterhours.dj_discovery import credit, normalized, style_for
from afterhours.playback_requests import named_recording
from afterhours.program_timeline import BeatTime
from afterhours.stem_profiles import cue_pair
from afterhours.track_metadata import clean_text
from afterhours.vdj_mixing import transition_recipe
from afterhours.vdj_session import DEFAULT_QUEUE_WINDOW, validate_order


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def transition_cost(outgoing, incoming, cues):
    recipe = transition_recipe(outgoing, incoming)
    keys_known = outgoing.get('camelot') and incoming.get('camelot')
    cost = (0 if recipe['harmonicCompatible'] else 3 if keys_known else 1)
    cost += abs(math.log2(outgoing['bpm'] / incoming['bpm'])) * 24
    exit_window = cues.get(outgoing['id'], {}).get('exit') or {}
    entry_window = cues.get(incoming['id'], {}).get('entry') or {}
    for field, weight in (('rmsDb', .12), ('bassDb', .08)):
        if finite(exit_window.get(field)) and finite(entry_window.get(field)):
            cost += abs(exit_window[field] - entry_window[field]) * weight
    stem_fit = cue_pair(outgoing['id'],incoming['id'],cues.get(outgoing['id'],{}),cues.get(incoming['id'],{}))
    if stem_fit:
        cost += stem_fit['scoreAdjustment']
    return cost


def quick_opening(session, query, found, *, allow_spoken_spelling=False):
    """Use a live opener and a feasible next slot before audio analysis."""
    wanted=normalized(query)
    title,artist=named_recording(query)
    title,artist=normalized(title),normalized(artist)
    blocked={item['trackId'] for item in session.get('recordingAvailability',[])}
    candidates=[row for row in session.get('candidates',[])
                if row['id'] not in blocked and clean_text(row.get('title'))
                and finite(row.get('bpm')) and row['bpm']>0]
    live=[row for row in candidates if row['id'] in found]
    if allow_spoken_spelling and not title and len(wanted.split()) >= 2:
        # Accept a repeated-letter spelling from speech only after the native
        # search settles and supplies one unambiguous artist credit.
        names=set().union(*(credit(row.get('artist')) for row in live))
        folded=re.sub(r'([a-z])\1+', r'\1', wanted)
        variants={name for name in names if re.sub(r'([a-z])\1+', r'\1', name)==folded}
        if len(variants)==1 and not any(normalized(row.get('title'))==wanted for row in live):
            possible=next(iter(variants))
            if abs(len(possible)-len(wanted))==1 and set(possible.split()) & set(wanted.split()):
                wanted=possible
    for first in live:
        first_title=normalized(first.get('title'))
        first_artists=credit(first.get('artist'))
        qualified=bool(title and first_title==title and artist in first_artists)
        if wanted!=first_title and wanted not in first_artists and not qualified:
            continue
        first_style = style_for(first.get('genre'))
        provider = (first.get('catalog') or {}).get('provider')
        def same_artist(row):
            return bool(first_artists & credit(row.get('artist')))
        # A cached followup is only a preparation candidate. It must be
        # looked up again by exact identity before it can become audible.
        followups = sorted((row for row in candidates if row['id'] != first['id']
                            and (row['id'] in found or (provider and same_artist(row)
                                and (row.get('catalog') or {}).get('provider') == provider))
                            and (not qualified or normalized(row.get('title')) != first_title)
                            and not row['title'].strip().casefold().endswith((' [mixed]', ' (mixed)'))),
                           key=lambda row: (row['id'] not in found, not same_artist(row),
                               bool(first_style and first_style != style_for(row.get('genre'))),
                               abs(first['bpm'] - row['bpm'])))
        for second in followups:
            pair=[first['id'],second['id']]
            try:
                validate_order(pair,candidates,session.get('maxTempoShift',8),session.get('secondsPerTrack',30))
            except ValueError:
                continue
            return pair
    return None


def placement(session, track_id):
    """Choose the least disruptive feasible insertion, preserving every existing ID."""
    staged = session.get('queuedUpdate') or {}
    order = staged.get('tracks') or session.get('order', [])
    ids = [track['id'] for track in order]
    if track_id in ids:
        raise ValueError('This recording is already in the current or staged queue.')
    if not ids:
        raise ValueError('Build an opening queue before inserting an extra recording.')
    incoming = next((track for track in session.get('candidates', []) if track['id'] == track_id), None)
    if incoming is None:
        raise ValueError('The requested recording is missing from the current catalog.')
    policy = session.get('setlist') or {}
    if not policy.get('allowReorder', True):
        raise ValueError('Queue changes are disabled in Setlist.')
    audible = session.get('active') or any(deck.get('playing') for deck in (session.get('telemetry') or {}).get('decks', []))
    protected = min(len(ids), max(0, session.get('index', -1)) + 2) if audible else 0
    locked = set(policy.get('lockedIds', []))
    first_slot = max(1, protected, max((i + 1 for i, tid in enumerate(ids) if tid in locked), default=0))
    plan = session.get('executionPlan') or {}
    cues = {row['trackId']: row for row in plan.get('selections', []) if row.get('trackId')}
    targets = staged.get('tempoTargets') if staged else session.get('tempoTargets')
    frozen = (plan.get('tempoSchedule') or [])[:protected]
    quick = session.get('mixStyle') == 'quick'
    opening = plan.get('targetBpm')
    if quick and session.get('order'):opening=session.get('quickTargetBpm') or session['order'][0]['bpm']
    history = session_curation.history_ids(session)
    queue_window = session.get('queueWindow',DEFAULT_QUEUE_WINDOW)
    if len(ids) - len(history) >= queue_window:
        raise ValueError('The current queue window is full. Wait for it to advance or remove an upcoming track.')
    choices = []
    for slot in range(first_slot, len(ids) + 1):
        revised = ids[:slot] + [track_id] + ids[slot:]
        try:
            validate_order(revised, session.get('candidates', []), session.get('maxTempoShift', 8),
                           session.get('secondsPerTrack', 30), targets, frozen or None,
                           opening if protected else None, fixed=quick, history_prefix=history, window_limit=queue_window)
        except ValueError:
            continue
        before = order[slot - 1]
        after = order[slot] if slot < len(order) else None
        cost = transition_cost(before, incoming, cues)
        if after:
            cost += transition_cost(incoming, after, cues) - .5 * transition_cost(before, after, cues)
        choices.append((round(cost, 6), slot, revised))
    if not choices:
        raise ValueError('No safe slot fits this recording and the current tempo route.')
    ranked = sorted(choices)
    _, slot, revised = ranked[0]
    options = [{'slot': at, 'afterId': ids[at - 1], 'beforeId': ids[at] if at < len(ids) else None,
                'transitionCost': cost} for cost, at, _ in ranked[:3]]
    return {**options[0], 'ids': revised, 'tempoTargets': targets, 'options': options}


def runway(session, now):
    """Estimate contiguous prepared music using the committed cues and deck clock."""
    order = session.get('order', [])
    plan = session.get('executionPlan') or {}
    cues = plan.get('selections', [])
    index = session.get('index', -1)
    if not plan.get('applied') or [row.get('trackId') for row in cues] != [row['id'] for row in order]:
        return None
    if not order or not -1 <= index < len(order) or session.get('phase') in ('completed', 'error', 'takeover') or (session.get('copilot') or {}).get('held'):
        return None
    if index >= 0 and session.get('phase') != 'playing':
        return None
    route = {row.get('trackId'): row for row in plan.get('tempoSchedule', [])}
    deck = None
    sampled = None
    if index >= 0:
        telemetry = session.get('telemetry') or {}
        matching = [row for row in telemetry.get('decks', []) if row.get('id') == order[index]['id'] and row.get('playing') is True]
        if len(matching) != 1:
            return None
        deck = matching[0]
        sampled = deck.get('sampledAt', telemetry.get('sampledAt'))
        if not finite(sampled) or not -2 <= now - sampled < 3:
            return None
    total = 0
    for i in range(max(0, index), len(order)):
        cue = cues[i]
        if cue.get('replayVerified') is not True:
            return None
        start = deck.get('beatPosition') if deck and i == index else cue.get('entryBeat')
        end = (session.get('timing') or {}).get('mixAtBeat', cue.get('exitBeat')) if i == index else cue.get('exitBeat')
        if not finite(start) or not finite(end):
            return None
        timing = session.get('timing') or {}
        if deck and i == index and timing.get('basis') == 'operator-steered-measured-exit':
            bpm = deck.get('bpm')
            if not finite(bpm) or bpm <= 0:
                return None
            elapsed = (end - start) * 60 / bpm
        else:
            try:
                clock = BeatTime(cue, route.get(order[i]['id'], {}), plan.get('targetBpm'))
                elapsed = clock.seconds(end) - clock.seconds(start)
            except (KeyError, TypeError, ValueError):
                return None
        total += max(0, elapsed)
    return round(max(0, total - (max(0, now - sampled) if sampled is not None else 0)), 1)


def rotate_opening(session, ids, recent):
    """Prefer a different opening pair within a proposed order when its route fits."""
    if len(ids) < 3 or not recent.intersection(ids[:2]):
        return ids
    original_count = sum(tid in recent for tid in ids[:2])
    combinations = []
    for first, first_id in enumerate(ids):
        for second, second_id in enumerate(ids):
            if first == second:
                continue
            repeats = (first_id in recent) + (second_id in recent)
            if repeats < original_count:
                combinations.append((repeats, first + second, first, second))
    for _, _, first, second in sorted(combinations):
        trial = [ids[first], ids[second]] + [tid for i, tid in enumerate(ids) if i not in (first, second)]
        try:
            validate_order(trial, session.get('candidates', []), session.get('maxTempoShift', 8),
                           session.get('secondsPerTrack', 30), fixed=session.get('mixStyle') == 'quick')
        except ValueError:
            continue
        return trial
    return ids
