"""Measure frozen DJ plans and archived native trials without a live engine."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite, sqrt
from pathlib import Path
from statistics import median, quantiles


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
    for target in timing:
        misses = dict.fromkeys(('readyAfterTarget', 'readyByTarget', 'readbackMissing'), 0)
        on_target = 0
        for row, interval in zip(handoffs, intervals):
            if str(row['targetIntervalSeconds']) != target:
                continue
            if abs(interval - int(target)) <= 2:
                on_target += 1
                continue
            ready = row.get('deckReadyRelativeToTargetSeconds')
            if type(ready) not in (int, float) or not isfinite(ready):
                misses['readbackMissing'] += 1
            else:
                misses['readyAfterTarget' if ready > 0 else 'readyByTarget'] += 1
        timing[target].update(within2Seconds=on_target, missesOutside2Seconds=misses)

    master = list(json_lines(Path(f'{prefix}-master.jsonl')))
    starts = [row for row in master if row.get('event') == 'master-monitor-started']
    if len(starts) != 1 or starts[0].get('owner') != owner['id']:
        raise ValueError('The master capture does not match the native playback owner.')
    meter = next((row for row in reversed(master) if 'capturedAudioSeconds' in row), {})
    quiet = [row for row in master if row.get('event') == 'quiet-window']
    quiet_during_blend = sum(any(first <= row['at'] <= last for first, last in blends) for row in quiet)
    last = browser[-1]
    elapsed = number(datetime.fromisoformat(last['at']).timestamp() - start, 'observed seconds')
    terminal = phase.get('phase') in ('completed', 'stopped', 'error', 'takeover')
    if terminal and last['event'] != 'Endurance reached':
        elapsed = max(elapsed, number(phase['seconds'], 'terminal native observation'))
    verified = sum(row.get('verified') is True for row in handoffs)
    return {
        'trialId':prefix.name,
        'scope':'observed-browser-native-and-master',
        'outcome':phase['phase'] if terminal and last['event'] != 'Endurance reached' else last['event'],
        'nativePhase':phase.get('phase'),
        'observedSeconds':round(elapsed, 3),
        'completeHour':last['event'] == 'Endurance reached' and elapsed >= 3600
                       and verified == len(handoffs) == last['tracks']-1,
        'verifiedHandoffs':verified,
        'syncedHandoffs':sum(row.get('synced') is True for row in handoffs),
        'cadenceSeconds':cadence,
        'timingByTarget':timing,
        'masterCapturedSeconds':meter.get('capturedAudioSeconds'),
        'masterStartedAfterPlaybackSeconds':starts[0].get('afterStartSeconds'),
        'captureSequenceDiscontinuities':meter.get('sequenceDiscontinuities'),
        'clippedSamples':meter.get('clippedSamples'),
        'longestBelowMinus65Seconds':meter.get('longestBelowMinus65Seconds'),
        'quietWindowsDuringBlend':quiet_during_blend,
        'quietWindowsOutsideBlend':len(quiet)-quiet_during_blend,
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
