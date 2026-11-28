"""Measure frozen DJ plans and archived native trials without a live engine."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite, sqrt
from pathlib import Path
from statistics import median, quantiles
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from afterhours.dj_discovery import credit


def number(value, field, minimum=0, maximum=None):
    if (type(value) not in (int, float) or not isfinite(value) or value < minimum
            or (maximum is not None and value > maximum)):
        raise ValueError(f'Invalid {field}: {value!r}')
    return value


def label(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'Missing {field}')
    return value.strip()


def cadence_metrics(intervals, targets):
    errors = defaultdict(list)
    for index, interval in enumerate(intervals):
        if targets:
            target = targets[index % len(targets)]
            errors[target].append(abs(interval - target))
    return {
        str(target): {
            'entries': len(values),
            'within5Seconds': sum(error <= 5 for error in values),
            'within10Seconds': sum(error <= 10 for error in values),
            'medianAbsoluteErrorSeconds': round(median(values), 3),
            'worstAbsoluteErrorSeconds': round(max(values), 3),
            'p95AbsoluteErrorSeconds': round(quantiles(values, n=20, method='inclusive')[18], 3) if len(values) > 1 else None,
        }
        for target, values in sorted(errors.items())
    }


def energy_phases(plan, target):
    phases = []
    for row in plan.get('energyCurve', []):
        start = number(row['startSeconds'], 'energy phase start', maximum=target)
        energy = number(row['target'], 'energy phase target', maximum=1)
        if start >= target or (not phases and start != 0) or (phases and start <= phases[-1][0]):
            raise ValueError('Energy phases must start at zero and increase within the set')
        phases.append((start, energy))
    return [
        (start, phases[index + 1][0] if index + 1 < len(phases) else target, energy)
        for index, (start, energy) in enumerate(phases)
    ]


def artist_concentration(credited_names):
    appearances = Counter()
    streaks = {}
    longest_streak = adjacent_pairs = known = 0
    for names in credited_names:
        names = credit(names) if isinstance(names, str) else ()
        appearances.update(names)
        known += bool(names)
        adjacent_pairs += any(name in streaks for name in names)
        streaks = {name:streaks.get(name, 0)+1 for name in names}
        longest_streak = max(longest_streak, max(streaks.values(), default=0))
    maximum = max(appearances.values(), default=0)
    return {
        'tracksWithKnownArtists':known,
        'distinctCreditedArtists':len(appearances),
        'mostAppearancesByOneArtist':maximum,
        'mostPlayedArtists':sorted(name for name, count in appearances.items() if count == maximum),
        'longestConsecutiveAppearances':longest_streak,
        'adjacentPairsSharingAnArtist':adjacent_pairs,
    }


def read_plan(path):
    raw = path.read_bytes()
    plan = json.loads(raw)
    if plan.get('schemaVersion') != 1 or plan.get('targetSeconds') != 3600:
        raise ValueError(f'{path}: expected one-hour plan schemaVersion 1')
    target = plan['targetSeconds']
    tracks = plan.get('tracks', [])
    if not isinstance(tracks, list) or not tracks:
        raise ValueError(f'{path}: supply a nonempty track timeline')
    cadence = [number(value, 'cadence', minimum=1, maximum=target) for value in plan.get('cadenceSeconds', [])]
    allowed = {label(value, 'allowed genre').casefold() for value in plan.get('allowedGenres', [])}
    phases = energy_phases(plan, target)
    previous_entry = -1
    audible_through = 0
    recorded_decisions = Counter()
    artists = Counter()
    genres = defaultdict(float)
    entries = []
    dwell = []
    overlaps = []
    phase_results = [
        {'startSeconds': start, 'endSeconds': end, 'targetEnergy': energy,
         'genreLeadSeconds': defaultdict(float), 'energyKnownLeadSeconds': 0, 'absoluteError': 0}
        for start, end, energy in phases
    ]
    missing_genre = gap_seconds = energy_seconds = 0

    for index, track in enumerate(tracks):
        recording = label(track['recordingId'], 'recording ID')
        artist = label(track['artist'], 'credited artist').casefold()
        genre = label(track['genre'], 'genre').casefold() if track.get('genre') is not None else None
        entry = number(track['entrySeconds'], 'entry', maximum=target)
        end = number(track['audibleEndSeconds'], 'audible end', maximum=target)
        number(track['cueSeconds'], 'source cue')
        if entry <= previous_entry or end <= entry or end < audible_through:
            raise ValueError(f'{path}: track {index + 1} needs increasing entries and ordered audible ends')
        energy = track.get('energy')
        if energy is not None:
            energy = number(energy, 'energy', maximum=1)
        next_entry = number(tracks[index + 1]['entrySeconds'], 'next entry', maximum=target) if index + 1 < len(tracks) else target
        if next_entry <= entry:
            raise ValueError(f'{path}: entries must strictly increase')
        lead_end = min(end, next_entry)
        lead_seconds = lead_end - entry
        if genre is None:
            missing_genre += lead_seconds
        else:
            genres[genre] += lead_seconds
        if energy is not None:
            energy_seconds += lead_seconds
        for phase in phase_results:
            shared_seconds = min(lead_end, phase['endSeconds']) - max(entry, phase['startSeconds'])
            if shared_seconds <= 0:
                continue
            if genre is not None:
                phase['genreLeadSeconds'][genre] += shared_seconds
            if energy is not None:
                phase['energyKnownLeadSeconds'] += shared_seconds
                phase['absoluteError'] += shared_seconds * abs(energy - phase['targetEnergy'])
        gap_seconds += max(0, entry - audible_through)
        audible_through = max(audible_through, end)
        if entries:
            overlaps.append(max(0, tracks[index - 1]['audibleEndSeconds'] - entry))
        entries.append(entry)
        dwell.append({'recordingId': recording, 'audibleSeconds': round(end - entry, 3)})
        recorded_decisions[recording] += 1
        artists[artist] += 1
        previous_entry = entry

    gap_seconds += target - audible_through
    intervals = [after - before for before, after in zip(entries, entries[1:])]
    scored_energy_seconds = sum(phase['energyKnownLeadSeconds'] for phase in phase_results)
    weighted_energy_error = sum(phase['absoluteError'] for phase in phase_results)
    for phase in phase_results:
        observed = phase['energyKnownLeadSeconds']
        error = phase.pop('absoluteError')
        phase['energyMeanAbsoluteError'] = round(error / observed, 3) if observed else None
    return {
        'setId': label(plan['setId'], 'set ID'),
        'briefId': label(plan['briefId'], 'brief ID'),
        'catalogSnapshotId': label(plan['catalogSnapshotId'], 'catalog snapshot ID'),
        'sealedManifestSha256': sha256(raw).hexdigest(),
        'targetSeconds': target,
        'trackCount': len(tracks),
        'timelineGapSeconds': round(gap_seconds, 3),
        'leadSecondsByGenre': {genre: round(seconds, 3) for genre, seconds in sorted(genres.items())},
        'unclassifiedLeadSeconds': round(missing_genre, 3),
        'outOfBriefGenreLeadSeconds': round(sum(seconds for genre, seconds in genres.items() if genre not in allowed), 3) if allowed else None,
        'energyKnownLeadSeconds': round(energy_seconds, 3),
        'energyScoredLeadSeconds': round(scored_energy_seconds, 3),
        'energyMeanAbsoluteError': round(weighted_energy_error / scored_energy_seconds, 3) if scored_energy_seconds else None,
        'energyAndGenreByPhase': phase_results,
        'extraPlaysByRecording': {key: count - 1 for key, count in sorted(recorded_decisions.items()) if count > 1},
        'extraPlaysByArtistCredit': {key: count - 1 for key, count in sorted(artists.items()) if count > 1},
        'artistConcentration': artist_concentration(track['artist'] for track in tracks),
        'trackDwell': dwell,
        'plannedOverlapsSeconds': [round(seconds, 3) for seconds in overlaps],
        'cadenceSeconds': cadence,
        'timingByTarget': cadence_metrics(intervals, cadence),
        'listeningVerdict': None,
    }


def json_lines(path):
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def read_trial(prefix):
    summary = json.loads(Path(f'{prefix}-summary.json').read_text())
    ending = summary['test']
    receipts_path = Path(f'{prefix}-receipts.json')
    queue_path = Path(f'{prefix}-queue.jsonl')
    owner_path = Path(f'{prefix}-owner.json')
    receipts = []
    if receipts_path.is_file():
        receipts = json.loads(receipts_path.read_text())
    elif queue_path.is_file():
        for row in json_lines(queue_path):
            if row.get('transitions'):
                receipts = row['transitions']
    verified_count = summary['confirmedHandoffs']
    timing = None
    if receipts and len(receipts) == verified_count and all(row.get('verified') is True for row in receipts) and owner_path.is_file():
        previous = number(json.loads(owner_path.read_text())['at'], 'playback start')
        intervals = []
        for row in receipts:
            at = number(row['at'], 'transition time', minimum=previous)
            intervals.append(at - previous)
            previous = at
        timing = cadence_metrics(intervals, summary['cadenceSeconds'])

    master_path = Path(f'{prefix}-master.jsonl')
    meter = {}
    if master_path.is_file():
        for row in json_lines(master_path):
            if 'clippedSamples' in row:
                meter = row

    return {
        'trialId': prefix.name,
        'scope': 'native-execution-only',
        'outcome': ending['event'],
        'reason': ending.get('reason'),
        'observedSeconds': ending['seconds'],
        'completeHour': ending['event'] == 'Endurance reached' and ending['seconds'] >= 3600,
        'verifiedHandoffs': verified_count,
        'syncObserved': summary['syncObserved'],
        'handoffsWithMixerSamples': summary['handoffsWithMixerSamples'],
        'loopWrappedAndReleased': summary['loopWrappedAndReleased'],
        'stemControlHandoffs': summary['stemHandoffs'],
        'cadenceSeconds': summary['cadenceSeconds'],
        'timingByTarget': timing,
        'captureStartedAfterPlaybackSeconds': summary.get('masterCaptureStartedSecondsAfterPlayback'),
        'captureSequenceDiscontinuities': summary.get('sequenceDiscontinuities'),
        'quietWindowsDuringHandoff': summary.get('quietWindowsDuringHandoff'),
        'clippedSamplesThroughCaptureStop': meter.get('clippedSamples'),
        'listeningVerdict': None,
    }


def observed_up_next(browser, start):
    """Summarize sampled display states, keeping legacy named tracks unverified."""
    legacy = {'Preparing the next track':'preparing', 'Finding a track':'searching',
              'Awaiting next track':'awaiting', 'Track preparation stalled':'stalled',
              'No compatible track found':'no-match', 'End of queue':'ended'}
    valid = {*legacy.values(), 'committed', 'staged'}
    changes = []
    last_label = None
    handoffs = Counter()
    for row in browser:
        if row.get('event') == 'Rolling handoff':
            expected = row.get('expectedNextLabel')
            title = row.get('next')
            if not changes or changes[-1][1] != 'committed' or not last_label or not title:
                handoffs['unobserved'] += 1
            elif expected:
                handoffs['fullLabelMatches' if last_label == expected else 'fullLabelMismatches'] += 1
            elif last_label == title or last_label.startswith(f'{title} · '):
                handoffs['titleMatches'] += 1
            else:
                handoffs['titleMismatches'] += 1
        if row.get('event') != 'Up Next overlay changed':
            continue
        last_label = row.get('label')
        at = datetime.fromisoformat(row['at']).timestamp()
        if not row.get('visible'):
            state = 'hidden'
        elif row.get('state') in valid:
            state = row['state']
        else:
            state = legacy.get(row.get('label'), 'named-unverified')
        previous = changes[-1][0] if changes else start
        changes.append((number(at, 'Up Next observation', minimum=previous), state))
    if not changes:
        return None
    end = datetime.fromisoformat(browser[-1]['at']).timestamp()
    seconds = Counter()
    for (at, state), (following, _) in zip(changes, [*changes[1:], (end, None)]):
        seconds[state] += number(following-at, 'Up Next observed duration')
    result = {
        'firstSampleAfterStartSeconds':round(changes[0][0]-start, 3),
        'sampledSecondsByState':{state:round(duration, 3) for state, duration in sorted(seconds.items())},
    }
    if browser[-1].get('event') == 'Endurance reached':
        result['missingWhilePlayingSamples'] = browser[-1].get('missingNextSamples')
    if handoffs:
        result['handoffSamples'] = dict(sorted(handoffs.items()))
    return result


def observed_curatorial_variety(handoffs):
    """Measure the exact verified recording sequence when identity evidence exists."""
    if not handoffs or any(
            row.get('verified') is not True or
            not all(isinstance(row.get(key), str) and row[key] for key in ('outgoingRecordingId', 'incomingRecordingId'))
            for row in handoffs):
        return None
    if any(previous['incomingRecordingId'] != current['outgoingRecordingId']
           for previous, current in zip(handoffs, handoffs[1:])):
        return None
    first = handoffs[0]
    track_ids = [first['outgoingRecordingId'], *(row['incomingRecordingId'] for row in handoffs)]
    artist_credits = [first.get('outgoingArtist'), *(row.get('incomingArtist') for row in handoffs)]
    normalized = [credit.strip().casefold() if isinstance(credit, str) and credit.strip() else None
                  for credit in artist_credits]
    recordings = Counter(track_ids)
    artists = Counter(credit for credit in normalized if credit)
    return {
        'recordingsPlayed':len(track_ids),
        'distinctRecordings':len(recordings),
        'extraPlaysByRecording':{key: count-1 for key, count in sorted(recordings.items()) if count > 1},
        'knownArtistCredits':sum(artists.values()),
        'distinctArtistCredits':len(artists),
        'extraPlaysByArtistCredit':{key: count-1 for key, count in sorted(artists.items()) if count > 1},
        'artistConcentration':artist_concentration(artist_credits),
        'consecutiveRepeatedCreditPairs':sum(bool(previous and previous == current)
                                             for previous, current in zip(normalized, normalized[1:])),
    }


def observed_control_exchange(handoffs, control, *, mode=None):
    # Control readback measures mixer automation, not source separation quality.
    result = dict(withReadback=0, verifiedExchange=0, withSampledOverlap=0)
    for handoff in handoffs:
        values = []
        for row in handoff.get('controlReadback') or ():
            if not isinstance(row, dict) or (mode and row.get('leadControl') != mode):
                continue
            outgoing, incoming = row.get('outgoing'), row.get('incoming')
            if not isinstance(outgoing, dict) or not isinstance(incoming, dict):
                continue
            pair = outgoing.get(control), incoming.get(control)
            if all(type(value) in (int, float) and isfinite(value) and 0 <= value <= 1 for value in pair):
                values.append(pair)
        if not values:
            continue
        result['withReadback'] += 1
        overlap = any(outgoing > .03 and incoming > .03 for outgoing, incoming in values)
        result['withSampledOverlap'] += overlap
        if overlap:
            continue
        opening = next((index for index, (outgoing, incoming) in enumerate(values)
                        if outgoing >= .45 and incoming <= .03), None)
        if opening is not None and any(outgoing <= .03 and incoming >= .45
                                       for outgoing, incoming in values[opening+1:]):
            result['verifiedExchange'] += 1
    return result


def observed_mix_techniques(handoffs):
    rows = [row for row in handoffs if row.get('verified') is True]
    if not rows:
        return None
    phase = [row['maxPhaseGapBeats'] for row in rows
             if type(row.get('maxPhaseGapBeats')) in (int, float) and isfinite(row['maxPhaseGapBeats'])]
    overlap = [row['overlapSamples'] for row in rows if type(row.get('overlapSamples')) is int]
    eq = [row['eqOrder'] for row in rows if isinstance(row.get('eqOrder'), list) and row['eqOrder']]
    modes = Counter(mode for row in rows for mode in set(row.get('leadControl') or ()) if mode)
    loops = [row for row in rows if type(row.get('loopBeats')) is int and row['loopBeats'] > 0]
    outgoing, incoming = 'Clearing outgoing bass and lead', 'Bringing in incoming bass'
    bass_swaps = [controls for controls in eq if outgoing in controls and incoming in controls]
    return {
        'withPhaseReadback':len(phase),
        'worstObservedPhaseGapBeats':max(phase, default=None),
        'withOverlapReadback':len(overlap),
        'withMeasuredSignalOnBothDecksDuringCrossfade':sum(samples > 0 for samples in overlap),
        'withMixerPhaseReadback':len(eq),
        'withBothBassSwapStages':len(bass_swaps),
        'withOrderedBassSwapPhases':sum(
            controls.index(outgoing) < controls.index(incoming) for controls in bass_swaps),
        'leadControlHandoffs':dict(sorted(modes.items())),
        'bassEqReadback':observed_control_exchange(rows, 'eq_low_freq'),
        'vocalStemReadback':observed_control_exchange(rows, 'stem Vocal', mode='stems'),
        'instrumentStemReadback':observed_control_exchange(rows, 'stem Instru', mode='stems'),
        'loopHandoffs':len(loops),
        'loopWrappedAndReleased':sum(
            (row.get('loopWraps') or 0) > 0 and row.get('loopRelease') == 'released' for row in loops),
    }


def observed_genre_timing(handoffs, intervals):
    seconds = defaultdict(float)
    transitions = Counter()
    classified_pairs = 0
    total = 0
    for row, interval in zip(handoffs, intervals):
        if row.get('verified') is not True:
            continue
        total += 1
        outgoing, incoming = (value.strip().casefold() if isinstance(value, str) else ''
                              for value in (row.get('outgoingGenre'), row.get('incomingGenre')))
        seconds[outgoing] += interval
        if outgoing and incoming:
            transitions[outgoing, incoming] += 1
            classified_pairs += 1
    if not total:
        return None
    unclassified = seconds.pop('', 0)
    return {
        'verifiedIntervals':total,
        'entryIntervalSecondsByOutgoingGenre':{genre:round(value, 3) for genre, value in sorted(seconds.items())},
        'unclassifiedEntryIntervalSeconds':round(unclassified, 3),
        'handoffsWithBothGenres':classified_pairs,
        'genreHandoffs':[{'fromGenre':outgoing, 'toGenre':incoming, 'count':count}
                         for (outgoing, incoming), count in sorted(transitions.items())],
    }


def observed_queue_runway(path, owner, end):
    if not path.is_file():
        return None
    rows = []
    previous_at = owner['at']
    for row in json_lines(path):
        if (row.get('owner'), row.get('startedAt')) != (owner['id'], owner['at']):
            raise ValueError('Queue runway sampling changed playback owner.')
        at = number(row['at'], 'queue runway observation', minimum=previous_at)
        if at > end:
            break
        previous_at = at
        for field in ('index', 'committedSuccessors', 'stagedSuccessors'):
            if type(row.get(field)) is not int or row[field] < 0:
                raise ValueError('Invalid queue runway reading.')
        rows.append(row)
    playing = [row for row in rows if row['phase'] == 'playing']
    if not playing:
        return None

    sampled = no_committed = no_pending_successor = longest_empty = empty_run = 0
    for before, after in zip(rows, rows[1:]):
        span = after['at'] - before['at']
        # Never attribute a skipped poll, transition, or new recording to the
        # earlier reading. A separate browser log covers the blend itself.
        if (span > 5 or before['phase'] != 'playing' or after['phase'] != 'playing'
                or before['index'] != after['index']):
            empty_run = 0
            continue
        sampled += span
        if before['committedSuccessors']:
            empty_run = 0
            continue
        no_committed += span
        if before['stagedSuccessors']:
            empty_run = 0
            continue
        no_pending_successor += span
        empty_run += span
        longest_empty = max(longest_empty, empty_run)

    return {
        'firstPlayingSampleSeconds':round(playing[0]['at'] - owner['at'], 3),
        'lastPlayingSampleSeconds':round(playing[-1]['at'] - owner['at'], 3),
        'playingSamples':len(playing),
        'measuredPlayingSeconds':round(sampled, 3),
        'minimumCommittedSuccessors':min(row['committedSuccessors'] for row in playing),
        'medianCommittedSuccessors':median(row['committedSuccessors'] for row in playing),
        'secondsWithoutCommittedSuccessor':round(no_committed, 3),
        'secondsWithoutCommittedOrStagedSuccessor':round(no_pending_successor, 3),
        'longestWithoutCommittedOrStagedSuccessorSeconds':round(longest_empty, 3),
    }


def read_observed_run(prefix):
    """Compare native handoff timing with independent master capture logs."""
    owner = json.loads(Path(f'{prefix}-owner.json').read_text())
    start = number(owner['at'], 'playback start')
    browser = list(json_lines(Path(f'{prefix}-hour.jsonl')))
    windows = [row for row in browser if row.get('event') == 'Endurance window']
    if len(windows) != 1 or windows[0].get('startedAt') != start:
        raise ValueError('The browser log does not match the native playback owner.')
    cadence = next((row.get('entryCadence') for row in browser
                    if row.get('entryCadence')), None)
    if not cadence:
        raise ValueError('The browser log has no requested cadence.')
    native = list(json_lines(Path(f'{prefix}-handoffs.jsonl')))
    handoffs = [row for row in native if row.get('kind') == 'native-handoff']
    phase = next((row for row in reversed(native) if row.get('kind') == 'queue'), {})
    previous_observation = entry = start
    intervals = []
    blends = []
    for index, row in enumerate(handoffs):
        observed = number(row['at'], 'native handoff observation', minimum=previous_observation)
        interval = number(row['entryIntervalSeconds'], 'native entry interval')
        entry += interval
        if entry > observed + .1 or row['targetIntervalSeconds'] != cadence[index % len(cadence)]:
            raise ValueError('Native handoff timing does not match the owned playback clock and cadence.')
        previous_observation = observed
        intervals.append(interval)
        if row.get('blendSeconds') is not None:
            # The audit timestamp is emitted after completion. Reconstruct the
            # blend start from measured entry intervals on the playback clock.
            blends.append((entry, entry + number(row['blendSeconds'], 'blend length')))
    timing = cadence_metrics(intervals, cadence)
    late_entries = []
    for target in timing:
        misses = dict.fromkeys(('readyAfterTarget', 'readyByTarget', 'readbackMissing'), 0)
        on_target = 0
        for row, interval in zip(handoffs, intervals):
            if str(row['targetIntervalSeconds']) != target:
                continue
            error = interval - int(target)
            if abs(error) <= 2:
                on_target += 1
                continue
            ready = row.get('deckReadyRelativeToTargetSeconds')
            if type(ready) not in (int, float) or not isfinite(ready) or ready > error + .01:
                ready = None
                misses['readbackMissing'] += 1
            else:
                misses['readyAfterTarget' if ready > 0 else 'readyByTarget'] += 1
            if error > 5 and row.get('verified') is True:
                deck_delay = max(0, ready) if ready is not None else None
                trace = row.get('schedulerReadback') or {}
                scheduled, guard, target_beat, chosen, bpm = (trace.get(key) for key in
                    ('scheduledFromBeat','guardedFromBeat','cadenceTargetBeat','mixAtBeat','scheduledBpm'))
                scheduler = {}
                if (all(type(value) in (int,float) and isfinite(value) for value in
                        (scheduled,guard,target_beat,chosen,bpm)) and scheduled <= guard <= chosen and 40 <= bpm <= 300):
                    scheduler = {
                        'secondsTargetAlreadyPassed':round(max(0, scheduled-target_beat)*60/bpm, 3),
                        'secondsPastTargetAfterGuards':round(max(0, guard-target_beat)*60/bpm, 3),
                        'secondsBeyondGuardAndTargetToChosenBar':round(max(0, chosen-max(guard,target_beat))*60/bpm, 3),
                    }
                late_entries.append({
                    'outgoing':row.get('outgoing'), 'incoming':row.get('incoming'),
                    'targetSeconds':int(target), 'secondsLate':round(error, 3),
                    'secondsLateBeforeDeckReady':round(deck_delay, 3) if deck_delay is not None else None,
                    'secondsLateAfterDeckReady':round(max(0, error - deck_delay), 3) if deck_delay is not None else None,
                    'timingBasis':row.get('timingBasis'),
                    **({'schedulerReadback':scheduler} if scheduler else {}),
                })
        timing[target].update(within2Seconds=on_target, missesOutside2Seconds=misses)

    master = list(json_lines(Path(f'{prefix}-master.jsonl')))
    starts = [row for row in master if row.get('event') == 'master-monitor-started']
    if len(starts) != 1 or starts[0].get('owner') != owner['id']:
        raise ValueError('The master capture does not match the native playback owner.')
    meter = next((row for row in reversed(master) if 'capturedAudioSeconds' in row), {})
    quiet = [row for row in master if row.get('event') == 'quiet-window']
    quiet_during_blend = measured_windows = 0
    measured_blend_quiet = 0
    for row in quiet:
        end = number(row['at'], 'quiet window end')
        duration = row.get('windowSeconds')
        if duration is None:
            quiet_during_blend += any(first <= end <= last for first, last in blends)
            continue
        duration = number(duration, 'quiet window duration', minimum=.001, maximum=end)
        measured_windows += 1
        start_of_window = end - duration
        covered_until = start_of_window
        for first, last in blends:
            overlap_start = max(covered_until, first)
            overlap_end = min(last, end)
            if overlap_end > overlap_start:
                measured_blend_quiet += overlap_end - overlap_start
                covered_until = overlap_end
        quiet_during_blend += covered_until > start_of_window
    trial_end = next((idx for idx, row in enumerate(browser)
                      if row.get('event') in ('Endurance reached', 'Interrupted')), len(browser)-1)
    last = browser[trial_end]
    elapsed = number(datetime.fromisoformat(last['at']).timestamp() - start, 'observed seconds')
    terminal = phase.get('phase') in ('completed', 'stopped', 'error', 'takeover')
    if terminal and last['event'] != 'Endurance reached':
        elapsed = max(elapsed, number(phase['seconds'], 'terminal native observation'))
    waiting = None
    if (not terminal and last.get('phase') == 'playing' and last.get('index') == len(handoffs)):
        current_elapsed = max(0, start + elapsed - entry)
        requested = cadence[len(handoffs) % len(cadence)]
        waiting = {'trackIndex':last['index'], 'elapsedSeconds':round(current_elapsed, 3),
                   'targetSeconds':requested, 'pastTargetSeconds':round(max(0, current_elapsed-requested), 3)}
    verified = sum(row.get('verified') is True for row in handoffs)
    return {
        'trialId':prefix.name,
        'scope':'observed-browser-native-and-master',
        'outcome':phase['phase'] if terminal and last['event'] != 'Endurance reached' else last['event'],
        'nativePhase':phase.get('phase'),
        'observedSeconds':round(elapsed, 3),
        'completeHour':last['event'] == 'Endurance reached' and elapsed >= 3600
                       and verified == len(handoffs) == last['tracks']-1,
        'stopVerified':any(row.get('event') == 'Stop verified' for row in browser),
        'verifiedHandoffs':verified,
        'syncedHandoffs':sum(row.get('synced') is True for row in handoffs),
        'cadenceSeconds':cadence,
        'timingByTarget':timing,
        'lateEntriesOutside5Seconds':sorted(late_entries, key=lambda row: -row['secondsLate']),
        'openEntryInterval':waiting,
        'upNextOverlay':observed_up_next(browser[:trial_end+1], start),
        'queueRunway':observed_queue_runway(Path(f'{prefix}-runway.jsonl'), owner, start + elapsed),
        'curatorialVariety':observed_curatorial_variety(handoffs),
        'genreTiming':observed_genre_timing(handoffs, intervals),
        'mixTechniques':observed_mix_techniques(handoffs),
        'masterCapturedSeconds':meter.get('capturedAudioSeconds'),
        'masterStartedAfterPlaybackSeconds':starts[0].get('afterStartSeconds'),
        'captureSequenceDiscontinuities':meter.get('sequenceDiscontinuities'),
        'clippedSamples':meter.get('clippedSamples'),
        'longestBelowMinus65Seconds':meter.get('longestBelowMinus65Seconds'),
        'quietWindowsDuringBlend':quiet_during_blend,
        'quietWindowsOutsideBlend':len(quiet)-quiet_during_blend,
        'quietWindowsWithDuration':measured_windows,
        'measuredQuietSecondsDuringBlend':round(measured_blend_quiet, 3)
                                         if measured_windows else None,
        'listeningVerdict':None,
    }


def review_choice(value):
    if value not in ('a', 'b', 'tie', None):
        raise ValueError(f'Invalid blind review choice: {value!r}')
    return value


def agreement_interval(matches, total):
    if not total:
        return None
    ratio = matches / total
    squared_z = 1.96 ** 2
    denominator = 1 + squared_z / total
    center = (ratio + squared_z / (2 * total)) / denominator
    margin = 1.96 * sqrt(ratio * (1 - ratio) / total + squared_z / (4 * total ** 2)) / denominator
    return [round(max(0, center - margin), 3), round(min(1, center + margin), 3)]


def judge_metrics(rows):
    decisions = [(human, judge) for human, judge, _ in rows if human in ('a', 'b')]
    matches = sum(human == judge for human, judge in decisions)
    abstentions = sum(judge is None for _, judge in decisions)
    swaps = [(judge, swapped) for _, judge, swapped in rows if judge is not None and swapped is not None]
    order_conflicts = sum(judge != {'a': 'b', 'b': 'a', 'tie': 'tie'}[swapped] for judge, swapped in swaps)
    return {
        'comparisonCount': len(rows),
        'nonTiedHumanConsensus': len(decisions),
        'judgeAbstentionsOnConsensus': abstentions,
        'agreementOverAllConsensus': round(matches / len(decisions), 3) if decisions else None,
        'agreement95PercentInterval': agreement_interval(matches, len(decisions)),
        'reviewedOrderSwaps': len(swaps),
        'inconsistentOrderSwaps': order_conflicts,
    }


def read_judgments(path):
    packet = json.loads(path.read_text())
    if packet.get('schemaVersion') != 1:
        raise ValueError(f'{path}: expected blind judgment schemaVersion 1')
    model = label(packet['judgeVersion'], 'judge version')
    held_out = []
    groups = defaultdict(list)
    seen = set()
    for row in packet['comparisons']:
        brief = label(row['briefId'], 'brief ID')
        dimension = label(row['dimension'], 'review dimension')
        key = (label(row['comparisonId'], 'comparison ID'), dimension)
        if key in seen:
            raise ValueError(f'Duplicate comparison and dimension: {key}')
        seen.add(key)
        split = row['split']
        if split not in ('development', 'calibration', 'held-out'):
            raise ValueError(f'Invalid comparison split: {split!r}')
        reviewers = {}
        for review in row['humanLabels']:
            reviewer = label(review['reviewerId'], 'reviewer ID')
            if reviewer in reviewers:
                raise ValueError(f'Duplicate reviewer in comparison: {key}')
            reviewers[reviewer] = review_choice(review['choice'])
        votes = Counter(choice for choice in reviewers.values() if choice is not None)
        choice, count = votes.most_common(1)[0] if votes else (None, 0)
        consensus = choice if count >= 2 and count * 2 > sum(votes.values()) else None
        judge = review_choice(row.get('judgeChoice'))
        swapped = review_choice(row.get('swappedJudgeChoice'))
        if split == 'held-out':
            result = (consensus, judge, swapped)
            groups[(brief, dimension)].append(result)
            held_out.append(result)

    return {
        'judgeVersion': model,
        'evidenceScope': 'held-out-blind-labels',
        'overall': judge_metrics(held_out),
        'byBriefAndDimension': [
            {'briefId': brief, 'dimension': dimension, **judge_metrics(rows)}
            for (brief, dimension), rows in sorted(groups.items())
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, action='append', default=[], help='Sealed one-hour plan JSON')
    parser.add_argument('--trial', type=Path, action='append', default=[], help='Prefix of archived native trial files')
    parser.add_argument('--observed', type=Path, action='append', default=[],
                        help='Prefix of owned browser, native handoff, and master capture logs')
    parser.add_argument('--judgments', type=Path, action='append', default=[], help='Blinded human and judge comparison labels')
    args = parser.parse_args()
    if not args.plan and not args.trial and not args.observed and not args.judgments:
        parser.error('supply at least one --plan, --trial, --observed, or --judgments')
    try:
        plans = [read_plan(path) for path in args.plan]
        trials = [read_trial(path) for path in args.trial] + [read_observed_run(path) for path in args.observed]
        judgments = [read_judgments(path) for path in args.judgments]
    except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
        parser.error(str(exc))
    plan_groups = {(row['briefId'], row['catalogSnapshotId'], tuple(row['cadenceSeconds'])) for row in plans}
    print(json.dumps({
        'plans': plans,
        'sameBriefCatalogAndCadence': len(plan_groups) == 1 if len(plans) > 1 else None,
        'trials': trials,
        'judgeCalibration': judgments,
    }, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
