#!/usr/bin/env python3
"""Observe a real Afterhours set. Optionally start an approved native rehearsal."""
import argparse
import json
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = 'http://127.0.0.1:8765'
SESSION = '/api/set-session'


def request(path, body=None, token=None):
    headers = {'X-DJ-Token': token} if token else {}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    req = Request(BASE + path, headers=headers, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(req, timeout=8) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(json.load(exc).get('error', exc.reason)) from None


def handoff_checks(row, previous, requested_interval):
    samples = row.get('samples', [])
    controls = row.get('mixerSamples', [])
    loop = row.get('loop', {})
    phase = max((s['phaseDifference'] for s in samples), default=None)
    bpm = max((abs(s['bpmA'] - s['bpmB']) for s in samples), default=None)
    interval = row['at'] - previous
    band_readback = bool(controls and
                         min(s['outgoing']['eq_low_freq'] for s in controls) <= .03 and
                         max(s['incoming']['eq_low_freq'] for s in controls) >= .45 and
                         min(s['outgoing']['eq_high_freq'] for s in controls) <= .30)
    lead_readback = bool(controls and (
        any(s['leadControl'] == 'stems' and s['outgoing']['stem Vocal'] <= .03
            and s['incoming']['stem Vocal'] >= .45 for s in controls)
        or any(s['leadControl'] == 'frequency-eq' and s['outgoing']['eq_mid_freq'] <= .03
               and s['incoming']['eq_mid_freq'] >= .45 for s in controls)))
    checks = {
        'transport': row.get('verified') is True,
        'timing': abs(interval - requested_interval) <= 10,
        'beatmatch': len(samples) >= 4 and phase <= .125 and bpm <= .15,
        'loop': loop.get('wraps', 0) >= 1 and loop.get('released') is True,
        'eq': band_readback and lead_readback,
    }
    return {'interval': round(interval, 3), 'targetInterval': requested_interval,
            'phaseDifferenceBeats': phase, 'tempoDifferenceBpm': bpm,
            'loopBeats': loop.get('beats'), 'loopWraps': loop.get('wraps'),
            'fadeBeats': row['timing']['fadeBeats'], 'checks': checks}


def emit(kind, **payload):
    print(json.dumps({'type': kind, **payload}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=3600)
    parser.add_argument('--start', action='store_true', help='Start the already selected queue with alternating 60/30 seconds.')
    parser.add_argument('--prepare', action='store_true', help='Verify the opening buffer while paused before starting.')
    parser.add_argument('--provider', help='Require every observed recording to have this exact native provider.')
    parser.add_argument('--provider-sequence', help='Require this comma-separated sequence at the start of the set.')
    args = parser.parse_args()
    if args.prepare and not args.start:
        parser.error('--prepare requires --start.')
    if args.seconds < 30:
        parser.error('Observe at least 30 seconds.')
    expected_sources = tuple(source.strip() for source in args.provider_sequence.split(',')) if args.provider_sequence else ()
    if len(expected_sources) > 12 or not all(expected_sources):
        parser.error('Supply up to twelve nonempty provider IDs in order.')
    state = request(SESSION)
    if args.start:
        if state['active'] or any(d.get('playing') for d in (state.get('telemetry') or {}).get('decks', [])):
            raise ValueError('The decks are already in use.')
        token = request('/api/config')['token']
        if args.prepare:
            state = request(SESSION, {'action': 'prepare-analysis', 'entryCadence': [60, 30]}, token)
            preparation_deadline = time.monotonic() + 15*60
            previous = None
            while state['active']:
                if time.monotonic() >= preparation_deadline:
                    raise RuntimeError('The paused opening buffer did not finish.')
                if previous != state['message']:
                    emit('preparing', detail=state['message'])
                    previous = state['message']
                time.sleep(1)
                state = request(SESSION)
            if not (state.get('executionPlan') or {}).get('applied'):
                raise RuntimeError(state['message'])
        state = request(SESSION, {'action': 'start', 'prepareWhilePlaying': True, 'rollingQueue': True,
                                  'entryCadence': [60, 30], 'loopHandoffs': True}, token)
    session = state['id']
    cadence = state.get('entryCadence') or [60, 30]
    counts = {'handoffs': 0, 'passedHandoffs': 0, 'queueChanges': 0, 'catalogMatches': 0}
    last_order = ()
    last_event = 0
    last_entry = None
    max_observation_gap = 0
    last_music = None
    deck_meter_seen = set()
    heard = set()
    provider_sequence = []
    deadline = time.monotonic() + args.seconds + 45
    while time.monotonic() < deadline:
        state = request(SESSION)
        if state['id'] != session:
            raise RuntimeError('A different set replaced the observed session.')
        order = state['order']
        ordered_ids = tuple(t['id'] for t in order)
        titles = {t['id']: t.get('title') for t in order}
        providers = {t['id']: (t.get('catalog') or {}).get('provider') for t in order}
        if ordered_ids != last_order:
            counts['queueChanges'] += 1
            emit('queue', index=state['index'], order=[titles[k] for k in ordered_ids], at=time.time())
            last_order = ordered_ids
        for event in state.get('events', []):
            if event['at'] <= last_event:
                continue
            if event.get('kind') in ('catalog', 'queue-ready', 'queue-applied', 'loaded'):
                emit(event['kind'], detail=event['text'], at=event['at'])
            if 'currentCatalogMatch' in event:
                counts['catalogMatches'] += int(event['currentCatalogMatch'])
                emit('selection', foundInCurrentSearch=event['currentCatalogMatch'], detail=event['text'],
                     musicEvidence=event['musicEvidence'], at=event['at'])
            last_event = event['at']
        for row in state['transitions'][counts['handoffs']:]:
            if last_entry is None:
                last_entry = state['startedAt']
                provider_sequence.append(providers.get(row['from']))
            provider_sequence.append(providers.get(row['to']))
            if args.provider and any(providers.get(row[key]) != args.provider for key in ('from','to')):
                raise ValueError('A verified handoff used a different or unclassified provider.')
            if tuple(provider_sequence[:len(expected_sources)]) != expected_sources[:len(provider_sequence)]:
                raise ValueError('The heard provider sequence diverged from the expected opening.')
            checks = handoff_checks(row, last_entry, cadence[counts['handoffs'] % len(cadence)])
            heard.update((row['from'], row['to']))
            counts['handoffs'] += 1
            counts['passedHandoffs'] += int(all(checks['checks'].values()))
            emit('handoff', fromTrack=titles.get(row['from']), toTrack=titles.get(row['to']),
                 fromProvider=providers.get(row['from']), toProvider=providers.get(row['to']), at=row['at'], **checks)
            last_entry = row['at']
        started = state.get('startedAt')
        now = time.time()
        if started:
            telemetry = state.get('telemetry') or {}
            playing = [d for d in telemetry.get('decks', []) if d.get('playing')]
            deck_meter_seen.update(d['id'] for d in playing if d.get('level', 0) > .025)
            if playing and now - telemetry.get('sampledAt', 0) < 3:
                if last_music is not None:
                    max_observation_gap = max(max_observation_gap, now-last_music)
                last_music = now
            elapsed = now - started
            if elapsed >= args.seconds and state['active']:
                # Do not disturb the running set when observation finishes.
                required = 0
                latest = 0
                while latest + cadence[required % len(cadence)] + 10 < args.seconds:
                    latest += cadence[required % len(cadence)] + 10
                    required += 1
                passed = (counts['handoffs'] == counts['passedHandoffs'] and counts['handoffs'] >= max(1, required)
                          and heard <= deck_meter_seen and max_observation_gap <= 4
                          and len(provider_sequence) >= len(expected_sources))
                emit('complete', seconds=round(elapsed, 1), nativeMeterTracks=len(deck_meter_seen),
                     minimumHandoffs=required, maxObservationGapSeconds=round(max_observation_gap, 2),
                     passed=passed, **counts)
                return 0 if passed else 1
        if not state['active'] and state['phase'] in ('error', 'takeover', 'completed', 'stopped'):
            emit('ended', phase=state['phase'], message=state['message'], at=now, **counts)
            return 1
        time.sleep(1)
    raise RuntimeError('The engine did not reach the observation duration.')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
