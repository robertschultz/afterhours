"""Exact-recording, time-local VirtualDJ stem meter evidence for queue fitting."""
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
from statistics import mean
import tempfile

from afterhours.paths import data_root

SOURCE = 'virtualdj-stem-meters-v1'
STEMS = ('vocal', 'kick', 'hihat', 'bass', 'instru')
POINTS = 8
MATCH_BEATS = 8


def report_path(track_id):
    match = re.fullmatch(r'virtualdj:([0-9a-f]{64})', track_id or '')
    return data_root() / '.runtime' / 'stem-profiles' / (match[1]+'.json') if match else None


def valid_number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def valid_window(row):
    if not isinstance(row, dict) or not valid_number(row.get('startSeconds'), 0, 1200):
        return False
    duration = row.get('durationSeconds')
    values = row.get('levels')
    return (row.get('role') in ('entry','exit') and valid_number(duration, 2, 16)
            and isinstance(values, list) and len(values) == POINTS
            and all(isinstance(point,list) and len(point)==len(STEMS)
                    and all(valid_number(level,0,1) for level in point) for point in values)
            and any(max(point) >= .015 for point in values)
            and any(max(point)-min(point) >= .01 for point in values))


@lru_cache(maxsize=128)
def read_report(path, signature, track_id):
    try:
        with path.open('rb') as handle:
            body = handle.read(32769)
        if len(body)>32768:
            return ()
        report = json.loads(body)
        rows = report['windows']
        if (report['source'] == SOURCE and report['trackId'] == track_id
                and isinstance(rows,list) and len(rows)<=12 and all(map(valid_window,rows))):
            return tuple(rows)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return ()


def windows(track_id):
    path = report_path(track_id)
    if path is None:
        return ()
    try:
        if path.is_symlink():
            return ()
        return read_report(path, path.stat().st_mtime_ns, track_id)
    except OSError:
        return ()


def save(track_id, rows):
    path = report_path(track_id)
    if path is None or not rows or not all(map(valid_window,rows)):
        raise ValueError('Stem measurements lack independently verified window coverage.')
    existing = [old for old in windows(track_id) if not any(
        new['role']==old['role'] and abs(new['startSeconds']-old['startSeconds'])<.12 for new in rows)]
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('Stem profile storage must be local.')
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
        try:
            json.dump({'source':SOURCE,'trackId':track_id,'windows':(existing + rows)[-12:]},output)
            output.close()
            os.replace(output.name,path)
        finally:
            Path(output.name).unlink(missing_ok=True)


def measure_window(role, start, duration, samples):
    """Accept a complete envelope only if every bin has a metered native frame."""
    levels = []
    for i in range(POINTS):
        center = start + duration * (i+.5)/POINTS
        if not samples:
            return None
        at, reading = min(samples,key=lambda pair: abs(pair[0]-center))
        if abs(at-center)>duration/POINTS*.75:
            return None
        levels.append(reading)
    row = {'role':role,'startSeconds':start,'durationSeconds':duration,'levels':levels}
    return row if valid_window(row) else None


def compare(outgoing, incoming):
    """Compare audible occupancy and balance. Stem meters do not identify notes or lyrics."""
    def components(levels):
        vocal, kick, hihat, bass, instru = levels
        scale = max(.08, math.sqrt(sum(value*value for value in levels)))
        return vocal/scale, math.hypot(kick,hihat)/scale, bass/scale

    a = list(map(components,outgoing['levels']))
    b = list(map(components,incoming['levels']))
    overlap = mean(x[0] >= .35 and y[0] >= .35 for x,y in zip(a,b))
    low_overlap = mean(min(x[2], y[2]) for x,y in zip(a,b))
    rhythm_bass_gap = mean(abs(x[1]-y[2]) for x,y in zip(a,b))
    cost = 4*overlap + 1.5*low_overlap + rhythm_bass_gap - 1.25
    return {'source':SOURCE, 'vocalOverlapFraction':round(overlap,3),
            'lowEndOverlap':round(low_overlap,3),'drumsToIncomingBassGap':round(rhythm_bass_gap,3),
            'scoreAdjustment':round(cost,3)}


def cue_pair(outgoing_id, incoming_id, outgoing_cue, incoming_cue):
    """Return evidence only for the exact replay-verified entry and exit."""
    if not (outgoing_cue.get('replayVerified') is True and incoming_cue.get('replayVerified') is True):
        return None
    exit_seconds = outgoing_cue.get('exitSeconds')
    entry_seconds = incoming_cue.get('entrySeconds')
    if not (valid_number(exit_seconds,0,1200) and valid_number(entry_seconds,0,1200)):
        return None
    def matching(recording, role, at):
        return [row for row in windows(recording) if row['role']==role and abs(row['startSeconds']-at)<=.12]

    outgoing = matching(outgoing_id,'exit',exit_seconds)
    incoming = matching(incoming_id,'entry',entry_seconds)
    return min((compare(a,b) for a in outgoing for b in incoming),
               key=lambda result: result['scoreAdjustment'], default=None)
