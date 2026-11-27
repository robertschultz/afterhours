"""Paused VirtualDJ buffer scans and conservative, local feature verification.

GetSongBuffer can return zeros while decoding is pending. Pointer coverage alone
is not complete musical evidence. Require two matching full scans, separated in
time, signal through the native end marker, and no unexplained interior zero run.
No PCM, provider locator, transport command or network request leaves this module.
"""
import hashlib
import json
import math
import re
import time

from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.vdj_catalog import identity
from afterhours.paths import analysis_reports

# The installed native bridge has a compiled report location. An isolated
# backend candidate must read that same store without reinstalling the bridge
# or treating copied receipts as new analysis. Default installs are unchanged.
REPORTS = analysis_reports()
PLUGIN = 'Afterhours Analysis'
MAX_SCAN_SECONDS = 1200
PAUSED_SCAN_TIMEOUT = 40
SPARE_SCAN_TIMEOUT = 120


class AudioAnalysisTimeout(BridgeError):
    pass


def bounded(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def stable_scan_interval(receipt):
    stable = receipt.get('stableForSeconds')
    if 'scanWallSeconds' not in receipt:
        return bounded(stable, 1, PAUSED_SCAN_TIMEOUT)
    return (bounded(stable, 1, SPARE_SCAN_TIMEOUT)
            and bounded(receipt['scanWallSeconds'], stable, SPARE_SCAN_TIMEOUT))


def exceeds_scan_duration(track):
    duration=track.get('duration')
    return type(duration) in (int,float) and math.isfinite(duration) and duration>MAX_SCAN_SECONDS


def read_scan(recording_id, root=REPORTS):
    if not isinstance(recording_id, str) or not re.fullmatch('[a-f0-9]{64}', recording_id):
        raise ValueError('Invalid recording identity for audio analysis.')
    path = root / (recording_id + '.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        return None
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('version') != 2 or
                data.get('recordingId') != recording_id or data.get('audioSaved') is not False):
            return None
        return data, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        return None


def validate_scan(data, sound_end):
    """Reject partial, malformed, zero-filled and inconsistent feature reports."""
    if data.get('bufferReadComplete') is not True:
        raise ValueError('The decoded buffer scan is incomplete.')
    rate, duration = data.get('sampleRate'), data.get('duration')
    frames = data.get('totalFrames')
    if (type(rate) is not int or not 8000 <= rate <= 192000 or
            not bounded(duration, .001, MAX_SCAN_SECONDS) or type(frames) is not int or
            abs(frames-duration*rate) > 1.01 or data.get('coveredFrames') != frames or
            type(data.get('coveredFrames')) is not int or
            not bounded(data.get('scanStartedAtMs'), 1, 1e16)):
        raise ValueError('The audio scan has inconsistent coverage or timing.')
    if not bounded(sound_end, .001, duration+.01):
        raise ValueError('A native end-of-sound marker is required to check decoded coverage.')
    rows = data.get('features')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2401:
        raise ValueError('The audio scan has an invalid feature count.')
    cursor = 0.0
    last_signal = 0.0
    zero_run = 0.0
    longest_interior_zero = 0.0
    for row in rows:
        if (not isinstance(row, dict) or not bounded(row.get('seconds'), 0, duration) or
                not bounded(row.get('duration'), .000001, .500001) or
                abs(row['seconds']-cursor) > 2/rate or
                any(not bounded(row.get(k), -120, hi) for k, hi in
                    [('rmsDb', 0), ('lowDb', 0), ('highDb', 6)]) or
                not bounded(row.get('crestDb'), 0, 126)):
            raise ValueError('The audio scan contains a gap or invalid measurement.')
        cursor = row['seconds'] + row['duration']
        if row['rmsDb'] > -100:
            if last_signal:
                longest_interior_zero = max(longest_interior_zero, zero_run)
            last_signal = cursor
            zero_run = 0.0
        elif row['rmsDb'] <= -119.99:
            zero_run += row['duration']
        else:
            zero_run = 0.0
    if abs(cursor-frames/rate) > 2/rate:
        raise ValueError('The audio scan does not cover the whole track.')
    if last_signal < sound_end-2:
        raise ValueError('The buffer is still silent before the native end-of-sound marker.')
    if longest_interior_zero > 4:
        raise ValueError('An interior zero-filled region needs verification before analysis can be used.')
    return {'lastSignalSeconds': round(last_signal, 6),
            'longestInteriorZeroSeconds': round(longest_interior_zero, 6),
            'featureDigest': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}


def restore_effect(client, deck, previous, wait=time.sleep, clock=time.monotonic):
    """Confirm removal before another scan can acquire the asynchronous slot."""
    name=f'deck {deck} get_effect_name 1';active=f'deck {deck} effect_active 1'
    if client.query(name,'text')!=PLUGIN:return
    client.send(active+' off');deadline=clock()+3
    while as_bool(client.query(active,'bool')):
        if client.query(name,'text')!=PLUGIN:return
        if clock()>=deadline:raise BridgeError('Analysis effect deactivation was not confirmed. No replacement effect was selected.')
        wait(.05)
    if previous==PLUGIN or client.query(name,'text')!=PLUGIN:return
    client.send(f'deck {deck} effect_select 1 "{previous}"')
    while True:
        observed=client.query(name,'text')
        if observed==previous:return
        if observed!=PLUGIN:return  # Leave an operator's different selection alone.
        if clock()>=deadline:raise BridgeError('Analysis effect restoration was not confirmed. Check the deck effect slot.')
        wait(.05)


def scan_track(client, track, sound_end, check=lambda: None, wait=time.sleep,
               root=REPORTS, clock=time.monotonic, timeout=PAUSED_SCAN_TIMEOUT, deck=1, isolated=None):
    """One bounded scan operation. The caller owns the OSC connection lock.

    An isolated deck is a spare one held paused, at zero volume and off the
    cue while other decks play: C or D, or any deck of a four-deck rotation.
    """
    isolated=deck in (3,4) if isolated is None else isolated is True
    if deck not in ((1,2,3,4) if isolated else (1,)):raise ValueError('Analysis uses paused deck A or an isolated spare deck.')
    sid = track['engine']['selection_id']
    if not bounded(sound_end, .001, 1200):
        raise BridgeError('A native end-of-sound marker is required before scanning this recording.')
    deadline = clock()+timeout
    def guard():
        check()
        if isolated:
            from afterhours.background_analysis import guard_isolation
            guard_isolation(client,deck)
        if clock() >= deadline:
            raise AudioAnalysisTimeout('Audio analysis did not settle in time. Leave the track loaded and prepare again.')
        if any(as_bool(client.query(f'deck {d} play', 'bool')) for d in ((deck,) if isolated else (1,2))):
            raise BridgeError('A deck is playing. Audio analysis stopped without changing transport.')
        if identity(str(client.query(f'deck {deck} get_filepath', 'text'))) != sid:
            raise BridgeError('The recording changed during audio analysis.')
    guard()
    previous = client.query(f'deck {deck} get_effect_name 1', 'text')
    if not isinstance(previous, str) or not re.fullmatch(r'[A-Za-z0-9 _.\-]{1,80}', previous):
        raise BridgeError('The first effect slot cannot be safely restored. Choose an inactive native effect before analysis.')
    if as_bool(client.query(f'deck {deck} effect_active 1', 'bool')):
        raise BridgeError('Turn off the first effect slot on deck A before preparing audio analysis.')
    selected = False
    began = clock()
    passes = 0
    last = None
    last_time = None
    last_reason = 'Waiting for a fresh buffer scan.'
    try:
        client.send(f'deck {deck} effect_select 1 "{PLUGIN}"')
        until = clock()+3
        while client.query(f'deck {deck} get_effect_name 1', 'text') != PLUGIN:
            guard()
            if clock() >= until:
                raise BridgeError('The Afterhours Analysis bridge is not available. Install it and restart VirtualDJ.')
            wait(.1)
        selected = True
        client.send(f'deck {deck} effect_active 1 on')
        wait(.1)
        if not as_bool(client.query(f'deck {deck} effect_active 1', 'bool')):
            raise BridgeError('The analysis bridge did not activate.')
        while clock() < deadline:
            guard()
            old = read_scan(sid, root)
            old_id = old[0].get('scanStartedAtMs') if old else None
            # A completed scan starts a new pass; partial scans continue without
            # reloading or seeking the track. Readbacks serialize native work.
            fresh = False
            while clock() < deadline:
                guard()
                if client.query(f'deck {deck} get_effect_name 1', 'text') != PLUGIN:
                    raise BridgeError('The effect slot changed. Audio analysis stopped.')
                client.send(f'deck {deck} effect_button 1 1')
                wait(.05)
                label = client.query(f'deck {deck} get_effect_string 1 1', 'text')
                if isolated and 'pause both decks' in str(label).lower():
                    raise BridgeError('Update Afterhours Analysis and restart VirtualDJ to analyze on a spare deck while others play.')
                current = read_scan(sid, root)
                if current and (fresh or current[0]['scanStartedAtMs'] != old_id):
                    fresh = True
                    if current[0].get('bufferReadComplete') is True:
                        break
                if not fresh and str(label or '').startswith('Buffer read finished'):
                    raise AudioAnalysisTimeout(
                        'VirtualDJ finished scanning, but Afterhours could not read a new report. '
                        'Check that Afterhours and the installed analysis bridge use the same report folder.'
                    )
                if label and not any(s in label for s in ('scanned', 'incomplete', 'Buffer read finished')):
                    raise BridgeError('The analysis bridge could not scan this recording. ' + str(label)[:192])
                wait(.15)
            else:
                break
            passes += 1
            data, report_digest = current
            try:
                details = validate_scan(data, sound_end)
            except ValueError as exc:
                last, last_time, last_reason = None, None, str(exc)
            else:
                digest = details['featureDigest']
                if digest == last and clock()-last_time >= 1:
                    guard()
                    receipt = {'version': 1, 'recordingId': sid, 'reportDigest': report_digest,
                               'verifiedAt': time.time(), 'method': 'two-stable-scans-and-native-sound-end',
                               'passes': passes, 'stableForSeconds': round(clock()-last_time, 3),
                               'soundEndSeconds': sound_end, 'scanWallSeconds': round(clock()-began, 3),
                               'audioSaved': False, **details}
                    path = root / (sid+'.verified.json')
                    tmp = root / (sid+'.verified.partial')
                    if path.is_symlink() or tmp.is_symlink():
                        raise BridgeError('Audio analysis report path is not a regular local file.')
                    tmp.write_text(json.dumps(receipt, indent=2))
                    tmp.replace(path)
                    return receipt
                last, last_time = digest, clock()
                last_reason = 'Waiting for two matching whole-track scans.'
            wait(1.05)
        raise AudioAnalysisTimeout('Audio analysis remains unverified: '+last_reason)
    finally:
        # Do not overwrite an operator's newly selected effect. Cleanup never
        # sends Play, Pause, Load, gain, EQ or seek; it only removes our plugin.
        if selected:restore_effect(client,deck,previous,wait=wait,clock=clock)


def verified_energy(recording_id, duration, root=REPORTS):
    """Only a matching verification receipt can publish an energy map."""
    observed = read_scan(recording_id, root)
    receipt_path = root/(recording_id+'.verified.json')
    if not observed or receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size > 16000:
        return None
    try:
        data, digest = observed
        receipt = json.loads(receipt_path.read_text())
        if (receipt.get('version') != 1 or receipt.get('recordingId') != recording_id or
                receipt.get('reportDigest') != digest or receipt.get('audioSaved') is not False or
                receipt.get('method') != 'two-stable-scans-and-native-sound-end' or
                type(receipt.get('passes')) is not int or receipt['passes'] < 2 or
                not stable_scan_interval(receipt) or
                not bounded(duration, .001, MAX_SCAN_SECONDS) or abs(duration-data['duration']) > .05):
            return None
        details = validate_scan(data, receipt['soundEndSeconds'])
        if details['featureDigest'] != receipt['featureDigest']:
            return None
        # Compact whole-track contour for the bounded model request. Retain the
        # original half-second measurements locally for future cue-level work.
        features = data['features']
        stride = max(1, math.ceil(len(features)/96))
        contour = []
        for start in range(0, len(features), stride):
            group = features[start:start+stride]
            seconds = sum(f['duration'] for f in group)
            def power_average(field):
                return round(10*math.log10(max(1e-12, sum(10**(f[field]/10)*f['duration'] for f in group)/seconds)), 2)
            contour.append([round(group[0]['seconds'], 3), round(seconds, 3),
                            power_average('rmsDb'), power_average('lowDb'), power_average('highDb')])
        return {'source': 'VirtualDJ decoded PCM via local analysis bridge', 'duration': data['duration'],
                'sampleRate': data['sampleRate'], 'coverage': 1, 'windowCount': len(features),
                'resolutionSeconds': .5, 'verification': receipt['method'],
                'columns': ['seconds', 'duration', 'rmsDbFS', 'lowBandDbFS', 'highBandDbFS'],
                'contour': contour, 'semanticSections': False, 'vocalsDetected': False,
                'reportDigest': digest}
    except (OSError, ValueError, KeyError, TypeError):
        return None
