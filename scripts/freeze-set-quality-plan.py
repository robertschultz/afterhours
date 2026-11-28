"""Freeze a one-hour benchmark draft using only saved exact cue evidence."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path

TARGET_SECONDS = 3600
PITCH_LIMIT = 8
ENTRY_TOLERANCE = 5


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode() + b'\n'


def read_json(path):
    return json.loads(path.read_bytes())


def cue_seconds(cue, end, bpm):
    return (cue[end] - cue['entryBeat']) * 60 / bpm


def valid_window(cue, evidence):
    if (not isinstance(cue, dict) or cue.get('replayVerified') is not True
            or not isinstance(cue.get('id'), str)):
        return False
    try:
        if cue['maxBlendBeats'] < 8:
            return False
        beats = [cue[k] for k in ('entryBeat', 'exitBeat', 'endBeat')]
        positions = [cue[k] for k in ('entrySeconds', 'introEndSeconds', 'exitSeconds', 'endSeconds')]
        return (all(type(v) in (int, float) and isfinite(v) for v in beats + positions)
                and beats[0] + 8 <= beats[1] < beats[2]
                and 0 <= positions[0] < positions[1] <= positions[2] < positions[3] <= evidence['duration']
                and beats[2] - beats[1] >= cue['maxBlendBeats'] - .01)
    except (KeyError, TypeError, ValueError):
        return False


def freeze_pool(checkpoints, availability, provider, genres, bpm, captured_at):
    from afterhours import cue_planner
    from afterhours.recording_availability import RecordingAvailability, exact_id

    flags = RecordingAvailability(clock=lambda: captured_at)
    if availability:
        flags.restore(read_json(availability))
    restricted = {row['trackId'] for row in flags.snapshot()}
    found = {}
    rejects = {}
    sources = []
    for path in checkpoints:
        raw = path.read_bytes()
        sources.append(sha256(raw).hexdigest())
        payload = json.loads(raw)
        session = payload.get('session', payload)
        tracks = {row['id']: row for row in session.get('candidates', []) + session.get('order', [])}
        for incoming in (session.get('analysis') or {}).get('tracks', []):
            key = incoming['id']
            track = tracks.get(key)
            if (not track or not exact_id(key) or (track.get('engine') or {}).get('selection_id') != key[10:]
                    or (track.get('catalog') or {}).get('provider') != provider):
                continue
            if any(not isinstance(track.get(field), str) or not track[field].strip() for field in ('title','artist')):
                rejects[key] = 'missing-recording-credit'
                continue
            cue_pool = [c for c in incoming.get('cueCandidates', []) if valid_window(c, incoming)]
            if not cue_pool:
                rejects[key] = 'no-replay-verified-cues'
                continue
            if key in restricted:
                rejects[key] = 'local-provider-restriction'
                continue
            genre = (track.get('genre') or '').casefold()
            if genres and genre not in genres:
                rejects[key] = 'outside-brief-genre'
                continue
            if track.get('title', '').strip().casefold().endswith((' [mixed]', ' (mixed)')):
                rejects[key] = 'continuous-mix-edition'
                continue
            source_bpm = incoming.get('bpm')
            if (type(source_bpm) not in (int, float) or not isfinite(source_bpm) or source_bpm < 40
                    or abs(bpm / source_bpm - 1) > PITCH_LIMIT / 100):
                rejects[key] = 'outside-fixed-tempo-range'
                continue
            try:
                rows = cue_planner.measured_rows(incoming)
                if cue_planner.limited_audio(incoming, rows):
                    rejects[key] = 'limited-decoded-audio'
                    continue
            except (KeyError, TypeError, ValueError):
                rejects[key] = 'missing-or-changed-scan'
                continue
            scan = incoming['energyMap']['reportDigest']
            if incoming['energyMap'].get('coverage') != 1:
                rejects[key] = 'incomplete-decoded-coverage'
                continue
            if key not in found:
                found[key] = {
                    'track': {**{field: track.get(field) for field in
                                 ('id', 'title', 'artist', 'genre', 'label', 'year', 'key', 'energy')},
                              'camelot': incoming.get('camelot'), 'bpm': source_bpm},
                    'evidence': {**incoming, 'cueCandidates': []},
                }
            frozen = found[key]['evidence']
            if frozen['energyMap']['reportDigest'] != scan or frozen.get('bpm') != source_bpm:
                continue
            frozen['cueCandidates'] = sorted({row['id']: row for row in (
                frozen['cueCandidates'] + cue_pool)}.values(), key=lambda row: row['id'])
    return {
        'schemaVersion': 1, 'scope': 'saved-measurements', 'provider': provider,
        'allowedGenres': sorted(genres), 'targetBpm': bpm, 'capturedAt': captured_at,
        'checkpointDigests': sources,
        'recordings': [found[key] for key in sorted(found)],
        'excludedCounts': dict(Counter(value for key, value in rejects.items() if key not in found)),
    }


def select_tracks(pool, cadence, discovery, target=TARGET_SECONDS):
    from afterhours.dj_discovery import credit, rank_candidates
    from afterhours.queue_planning import transition_cost
    from afterhours.rolling_queue import base_song_title

    selection = []
    remaining = list(pool['recordings'])
    start = 0.0
    bpm = pool['targetBpm']
    while remaining:
        previous = selection[-1][0]['track'] if selection else None
        recent = [row[0]['track'] for row in selection[-5:]]
        compatible = {}
        requested = cadence[len(selection) % len(cadence)]
        for record in remaining:
            cues = record['evidence']['cueCandidates']
            # End the set only within an already measured exit window, leaving
            # at least eight beats for the proposed final fade.
            finale = [cue for cue in cues if
                      cue_seconds(cue, 'exitBeat', bpm) + 480 / bpm <= target - start + 1e-6
                      <= cue_seconds(cue, 'endBeat', bpm) + 1e-6]
            if finale:
                compatible[record['track']['id']] = (record, finale, True)
                continue
            timed = [cue for cue in cues if
                     abs(cue_seconds(cue, 'exitBeat', bpm) - requested) <= ENTRY_TOLERANCE
                     and start + cue_seconds(cue, 'exitBeat', bpm) < target]
            if timed:
                compatible[record['track']['id']] = (record, timed, False)
        if not compatible:
            break
        ranked, _ = rank_candidates([row[0]['track'] for row in compatible.values()], None, recent, recent,
            lambda track: (0 if compatible[track['id']][2] else 1,
                           transition_cost(previous, track, {}) if previous else abs(track['bpm'] - bpm)),
            discovery=discovery)
        track = ranked[0]
        record, cues, finale = compatible[track['id']]
        # Choose the closest measured interval before applying the engine's
        # signal-based cue and transition fit within that interval.
        distance = min(abs(cue_seconds(cue, 'exitBeat', bpm) - requested) for cue in cues)
        if not finale:
            cues = [cue for cue in cues if abs(cue_seconds(cue, 'exitBeat', bpm) - requested) <= distance + .01]
        selection.append((record, cues, start))
        if finale:
            return selection
        start += cue_seconds(cues[0], 'exitBeat', bpm)
        remaining = [r for r in remaining if r is not record and not (
            base_song_title(r['track']) == base_song_title(track)
            and credit(r['track'].get('artist')) & credit(track.get('artist')))]
    raise ValueError('The ranked, replay-verified pool did not reach a complete timed hour.')


def build_plan(pool, brief, cadence):
    from afterhours import cue_planner
    from afterhours.dj_discovery import Discovery

    bpm = pool['targetBpm']
    records = pool['recordings']
    # This generous coverage bound includes all potential fades and overlaps.
    # Failing it means no selection from this exact pool can fill an hour.
    possible = sum(max(cue_seconds(cue, 'endBeat', bpm) for cue in row['evidence']['cueCandidates'])
                   for row in records)
    status = {'recordings': len(records), 'maximumMeasuredSecondsBeforeOverlap': round(possible, 3),
              'targetSeconds': TARGET_SECONDS, 'excludedCounts': pool['excludedCounts'],
              'recordingsWithTimedExit': {str(seconds): sum(any(
                  abs(cue_seconds(cue, 'exitBeat', bpm) - seconds) <= ENTRY_TOLERANCE
                  for cue in row['evidence']['cueCandidates']) for row in records)
                  for seconds in sorted(set(cadence))}}
    if possible < TARGET_SECONDS:
        return None, {**status, 'status': 'insufficient-measured-coverage'}
    try:
        for record in records:
            cue_planner.measured_rows(record['evidence'])
        selection = select_tracks(pool, cadence, Discovery(clock=lambda: pool['capturedAt']))
        order = [record['track'] for record, _, _ in selection]
        evidence = [{**row['evidence'], 'cueCandidates': cues} for row, cues, _ in selection]
        seconds = cadence[0]
        data = {'secondsPerTrack': seconds, 'entryCadence': cadence, 'mixStyle': 'phrase',
                'maxTempoShift': PITCH_LIMIT, 'executionPlan': {'orderIds': [], 'targetBpm': bpm},
                'analysis': {'tracks': [], 'cueVersion': cue_planner.VERSION, 'secondsBetweenEntries':seconds,
                             'targetBpm': bpm}, 'aiReview': {'decision':'propose', 'selections':[],
                                                            'secondsBetweenEntries':seconds}}
        result = cue_planner.complete_background_queue(
            data, order, evidence, 'Offline fixed-tempo benchmark draft.', 0,
            tempo_targets=[{'trackId': track['id'], 'bpm': bpm} for track in order],
        )
        compiled = result['executionPlan']
    except (KeyError, TypeError, ValueError) as exc:
        return None, {**status, 'status': 'plan-not-feasible', 'reason': str(exc)}
    manifest = {
        'schemaVersion': 1, 'briefId': brief, 'scope': 'prospective-offline-draft',
        'catalogSnapshotId': sha256(encoded(pool)).hexdigest(), 'targetSeconds': TARGET_SECONDS,
        'cadenceSeconds': cadence, 'allowedGenres': pool['allowedGenres'], 'targetBpm': bpm,
        'provider': pool['provider'], 'nativePlaybackVerifiedForPlan': False, 'tracks': [],
    }
    start = 0.0
    for index, (track, cue, measured) in enumerate(zip(order, compiled['selections'], evidence)):
        final = index == len(order) - 1
        outgoing = start + cue_seconds(cue, 'exitBeat', bpm)
        recipe = None if final else compiled['transitions'][index]
        end = TARGET_SECONDS if final else outgoing + recipe['fadeBeats'] * 60 / bpm
        if (end > TARGET_SECONDS or end < outgoing
                or end - start > cue_seconds(cue, 'endBeat', bpm) + 1e-5
                or final and end - outgoing + 1e-5 < 480 / bpm):
            return None, {**status, 'status': 'final-boundary-not-verified'}
        manifest['tracks'].append({
            'recordingId': track['id'], 'artist': track['artist'], 'title': track['title'],
            'genre': track['genre'], 'energy': track['energy'], 'sourceCueId': cue['id'],
            'reportDigest': measured['energyMap']['reportDigest'],
            'entrySeconds': round(start, 6), 'audibleEndSeconds': round(end, 6),
            'cueSeconds': cue['entrySeconds'], 'exitCueSeconds': cue['exitSeconds'],
            'transition': recipe, 'finalFadeBeats': 8 if final else None,
            'finalFadeStartsAtSeconds': round(TARGET_SECONDS - 480 / bpm, 6) if final else None,
        })
        start = outgoing
    manifest['setId'] = sha256(encoded(manifest)).hexdigest()
    return manifest, {**status, 'status': 'sealed-offline-draft', 'tracks': len(order)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, action='append', required=True)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--availability', type=Path)
    parser.add_argument('--captured-at', type=float, required=True, help='Fixed Unix timestamp for local eligibility and rank inputs')
    parser.add_argument('--provider', required=True)
    parser.add_argument('--genre', action='append', default=[])
    parser.add_argument('--bpm', type=float, required=True)
    parser.add_argument('--cadence', type=int, nargs='+', default=[60, 30])
    parser.add_argument('--brief', required=True)
    parser.add_argument('--out', type=Path, required=True, help='New directory for private evidence and output')
    args = parser.parse_args()
    if not args.reports.is_dir() or not 40 <= args.bpm <= 300 or any(not 15 <= t <= 600 for t in args.cadence):
        parser.error('Supply saved reports, a supported tempo, and cadence entries between 15 and 600 seconds.')
    if not isfinite(args.captured_at) or not 0 <= args.captured_at <= 4_102_444_800:
        parser.error('Supply a valid fixed capture timestamp.')
    os.environ['AFTERHOURS_ANALYSIS_REPORTS_DIR'] = str(args.reports.resolve())
    try:
        pool = freeze_pool(args.checkpoint, args.availability, args.provider,
                           {g.casefold() for g in args.genre}, args.bpm, args.captured_at)
        args.out.mkdir(mode=0o700)
        # The offline baseline does not use mutable live stem profile caches.
        os.environ['AFTERHOURS_DATA_ROOT'] = str(args.out.resolve())
        manifest, status = build_plan(pool, args.brief, args.cadence)
        (args.out / 'source-pool.json').write_bytes(encoded(pool))
        (args.out / 'coverage.json').write_bytes(encoded(status))
        if manifest:
            (args.out / 'plan.json').write_bytes(encoded(manifest))
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(encoded(status).decode(), end='')
    return 0 if manifest else 2


if __name__ == '__main__':
    raise SystemExit(main())
