"""Reuse exact, locally measured cue receipts. Playback still replays the cue."""
import json
import math
import os
import tempfile
import time

from afterhours import cue_planner
from afterhours.paths import data_root
from afterhours.set_plan import PreparedEvidence, exact_id


MAX_BYTES = 128 * 1024
MAX_AGE = 7 * 24 * 60 * 60


class VerifiedCues:
    def __init__(self, root=None, clock=time.time):
        self.root = root if root is not None else data_root() / '.runtime/verified-cues'
        self.clock = clock
        self.evidence = PreparedEvidence()

    def path(self, track_id, *, timed_seconds=None):
        selection_id = track_id.removeprefix('virtualdj:')
        suffix = f'-timed-{timed_seconds}' if timed_seconds is not None else ''
        return self.root / f'{selection_id}{suffix}.json'

    def read(self, track, seconds, timed):
        track_id = track.get('id')
        if not exact_id(track_id):
            return None
        if timed:
            if type(seconds) is not int or not 30 <= seconds <= 180:
                return None
            item = self.read_file(self.path(track_id, timed_seconds=seconds), track, seconds, timed)
            if item is not None:
                return item
        # Older timed receipts may still be valid in the recording-only slot.
        return self.read_file(self.path(track_id), track, seconds, timed)

    def read_file(self, path, track, seconds, timed):
        try:
            if path.is_symlink() or self.root.is_symlink():
                return None
            stat = path.stat()
            if not 0 <= self.clock() - stat.st_mtime <= MAX_AGE or stat.st_size > MAX_BYTES:
                return None
            with path.open('rb') as file:
                data = file.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return None
            saved = json.loads(data)
            item = saved['measurement']
            bpm = item.get('bpm')
            known_bpm = track.get('bpm')
            metadata_changed = track.get('engineChecked') and (
                (track.get('camelot') and track['camelot'] != item.get('camelot'))
                or (type(known_bpm) in (int, float)
                    and (type(bpm) not in (int, float) or not math.isfinite(bpm) or abs(known_bpm - bpm) > .15)))
            if (saved.get('cueVersion') != cue_planner.VERSION or item['id'] != track['id'] or metadata_changed
                    or item.get('preparationSeconds') != seconds
                    or bool(item.get('timedPreparation')) != timed
                    or not self.evidence.valid(item)):
                return None
            if timed:
                stable = [cue for cue in item['cueCandidates'] if not cue_planner.incompatible_timed_grid(cue)]
                if not stable:
                    return None
                item['cueCandidates'] = stable
            return item
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def compatible_timed_bpm(self, track, seconds, target_bpm, max_pitch):
        measurement = self.read(track, seconds, True)
        bpm = measurement.get('bpm') if measurement else None
        if (type(bpm) in (int, float) and math.isfinite(bpm) and 40 <= bpm <= 300
                and abs(target_bpm / bpm - 1) * 100 <= max_pitch + .0001):
            return bpm
        return None

    def write(self, measurement):
        track_id = measurement.get('id')
        if not exact_id(track_id):
            return
        seconds = None
        if measurement.get('timedPreparation'):
            seconds = measurement.get('preparationSeconds')
            if type(seconds) is not int or not 30 <= seconds <= 180:
                return
        self._write_slot(self.path(track_id, timed_seconds=seconds), measurement)
        if seconds is None:
            return
        # A batch may have actually replayed shorter fallback windows while
        # the same recording was paused on a spare. Keep that proof for the
        # shorter slot, even if a queue repair moves this recording later.
        alternates = {}
        for cue in measurement.get('cueCandidates') or []:
            alternate = cue.get('preparedForSeconds')
            if type(alternate) is int and 30 <= alternate < seconds and cue.get('replayVerified') is True:
                alternates.setdefault(alternate, []).append(cue)
        for alternate, cues in alternates.items():
            variant = {**measurement, 'preparationSeconds': alternate, 'cueCandidates': cues}
            if not self.evidence.valid(variant):
                continue
            existing = self.read({**measurement, 'engineChecked': True}, alternate, True)
            if existing and len(existing['cueCandidates']) >= len(cues):
                continue
            self._write_slot(self.path(track_id, timed_seconds=alternate), variant)

    def _write_slot(self, path, measurement):
        try:
            if self.root.is_symlink() or path.is_symlink():
                return
            data = json.dumps({'cueVersion':cue_planner.VERSION,'measurement':measurement},
                              allow_nan=False, separators=(',', ':')).encode()
            if len(data) > MAX_BYTES:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as file:
                temporary = file.name
                try:
                    file.write(data)
                    file.close()
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        except (OSError, ValueError, TypeError):
            return
