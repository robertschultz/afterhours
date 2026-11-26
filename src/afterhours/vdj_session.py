"""Reusable catalog-set execution. The planner chooses; VirtualDJ times audio.

One worker owns OSC while preparing/running. Read-only snapshots feed every
display, including hidden tabs. Cancellation is checked between bounded reads;
no load, start, or transition is automatically replayed after uncertainty.
"""
import copy
import os
import math
import re
import threading
from afterhours.planner_policy import track_seconds
from afterhours.program_timeline import set_timeline
import time
import uuid

from afterhours.dj_bridge import VirtualDJ, BridgeError, as_bool
from afterhours.track_metadata import musical_key
from afterhours.vdj_catalog import (folder_state, selection, identity, load_selected, search_literal, count_value,
                         paused_load_target, load_exact_recording)
from afterhours.native_recording import RECORDINGS
from afterhours.vdj_mixing import NativeHandoff, TransitionMixer, MixerControlChanged, transition_recipe
from afterhours.native_analysis import track_analysis
from afterhours.audio_analysis import scan_track
from afterhours.waveform_analysis import scan_waveform, verified_waveform
from afterhours.opening_playback import OpeningHandoffHeld
from afterhours import cue_planner
from afterhours import ai_mix
from afterhours import tempo_planner
from afterhours import copilot_commands
from afterhours import dj_settings
from afterhours import deck_rotation
from afterhours.preparation_pool import PreparationPool as BackgroundAnalysis
from afterhours.background_analysis import cleanup_analysis_deck, merge_cleanup_pending
from afterhours.record_crate import RecordCrate, WORKING_LIMIT
from afterhours.vdj_discovery import CatalogSearch
from afterhours.recording_availability import RecordingAvailability, ProviderPreviewRestricted, PREVIEW_ONLY
from afterhours.queue_eligibility import preview_tail, preview_failure, QueueEligibilityHeld, skipped_message


class Cancelled(Exception):
    pass


class RecordingUnavailable(BridgeError):
    """Exact recording absent from refreshed results; no load was dispatched."""


class RecordingLookupIncomplete(BridgeError):
    """Bounded provider results did not locate the saved identity; availability is unknown."""


class TrackMetadataChanged(BridgeError):
    def __init__(self, track):
        self.track=track
        super().__init__(f"{track['title']} has updated engine analysis. Rebuild the order from the checked metadata.")


def number(value, low, high):
    if isinstance(value, bool):
        raise BridgeError("Invalid numeric engine reading")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BridgeError("Missing numeric engine reading") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise BridgeError("Engine reading outside its expected range")
    return result


class TempoFitError(ValueError):
    """A valid catalog selection with no safe executable tempo route."""
    def __init__(self, target, tracks, limit):
        self.tracks = tracks
        self.reasons = {t['id']: f"{t['title']} is {t['bpm']:g} BPM; at this set's {target:g} BPM it needs "
                       f"{(target / t['bpm'] - 1) * 100:+.1f}% pitch, beyond the {limit:g}% limit. "
                       "Hold it for a different tempo or a new set." for t in tracks}
        super().__init__(' '.join(self.reasons.values()))


def validate_order(ids, candidates, max_tempo_shift=8, seconds=30, tempo_targets=None, frozen=None, opening=None, fixed=False, history_prefix=(), retained_final_resume=False):
    # Only an engine-owned exact heard prefix may extend the 12-record window.
    if len(history_prefix)>256 or not isinstance(ids,list) or ids[:len(history_prefix)]!=list(history_prefix):
        raise ValueError("Played queue history must stay unchanged.")
    minimum=1 if retained_final_resume is True and history_prefix else 2
    if (not isinstance(ids, list) or len(ids)<2 or not minimum <= len(ids)-len(history_prefix) <= 12
            or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids)):
        raise ValueError("Choose 2–12 distinct tracks from this session's catalog.")
    by_id = {t['id']: t for t in candidates}
    if any(i not in by_id for i in ids):
        raise ValueError("A track is no longer in the analyzed catalog. Discover music again.")
    tracks = [by_id[i] for i in ids]
    if len({song_identity(t)[0] for t in tracks}) != len(tracks):
        raise ValueError('This short set repeats a song title. Choose different songs, including across remasters.')
    if fixed:
        if tempo_targets:raise ValueError('Tempo ramps need a measured mix plan; quick preview holds one tempo.')
        target=opening or tracks[0]['bpm']
        outside=[t for t in tracks if abs(target/t['bpm']-1)>max_tempo_shift/100+1e-6]
        if outside:raise TempoFitError(target,outside,max_tempo_shift)
        return copy.deepcopy(tracks)
    try:tempo_planner.estimate(tracks,seconds,max_tempo_shift,tempo_targets,opening,frozen)
    except tempo_planner.TempoRouteError as exc:
        err=TempoFitError(opening or tracks[0]['bpm'],[exc.track],max_tempo_shift)
        pitch=((opening or tracks[0]['bpm'])/exc.track['bpm']-1)*100
        prefix=(f"{exc.track['title']} at the opening tempo needs {pitch:+.1f}% pitch. " if abs(pitch)>max_tempo_shift else '')
        err.reasons[exc.track['id']]=prefix+str(exc)
        err.args=(err.reasons[exc.track['id']],)
        raise err from exc
    return copy.deepcopy(tracks)


def song_identity(track):
    # Catalog releases can credit the same recording to artists in a different
    # order. Conservatively exclude identical title+mix from one short set.
    normalize = lambda value: re.sub(r'\s+', ' ', str(value or '').strip().casefold())
    return normalize(track.get('title')), normalize(track.get('remix') or track.get('metadata', {}).get('remix'))


def search_terms(query):
    search_literal(query)
    terms = list(dict.fromkeys(term.strip() for term in query.split(';') if term.strip()))
    if not 1 <= len(terms) <= 4:
        raise ValueError('Use one to four searches, separated by semicolons.')
    for term in terms:
        search_literal(term)
    return terms


MIX_STYLES = {
    'phrase': {'phraseBeats': 64, 'fadeBeats': 64},
    'quick': {'phraseBeats': 4, 'fadeBeats': 8},
}


def segment_window(entry, current, seconds, bpm, style='phrase', final=False, recipe=None):
    """A beat-grid schedule, not a claim to detect a song's musical sections.

    Track time includes the outgoing blend. Entry is the actual cued beat,
    preserved across handoff/loading; a slow preload defers to the next block.
    """
    if style not in MIX_STYLES:
        raise ValueError('Choose phrase or quick mixing.')
    policy = recipe or MIX_STYLES[style]
    trial = seconds <= 30
    block = policy['phraseBeats']
    fade = 0 if final else policy['fadeBeats']
    if trial:
        block = 4
        cap = max(8, min(32, int(seconds * bpm / 60 / 2 / 8) * 8))
        fade = min(fade, cap)
    entry = round(number(entry, -10000, 100000))
    current = number(current, -10000, 100000)
    requested = seconds * bpm / 60
    if trial:
        # Trial spacing is between incoming starts, not a minimum phrase+fade.
        span = max(fade + block, round(requested / block) * block)
        begin = entry + span
        end = begin + fade
    else:
        span = max(block + fade, math.ceil(requested / block) * block)
        end = math.ceil((entry + span) / block) * block
        begin = end - fade
    # Allow at least a bar to finish preparation before a scheduled handoff.
    if begin < current + 4:
        end += math.ceil((current + 4 - begin) / block) * block
        begin = end - fade
    return {'entryBeat': entry, 'mixAtBeat': begin, 'endBeat': end,
            'segmentBeats': end-entry, 'phraseBeats': block, 'fadeBeats': fade,
            'basis': 'native-beat-grid'}


class DJSession:
    def __init__(self, control_lock, port=7474, client_factory=VirtualDJ, availability=None, crate=None, recordings=None, archive=None):
        self.control_lock = control_lock
        self.port = port
        self.factory = client_factory
        self.archive = archive
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.stop_audio = False
        self.worker = None
        self.mixer = None
        self.handoff = None
        self.positions = {}
        self.folder = None
        self.observing = False
        self.eq_readings = {}
        self.catalog_job = None
        self.dig_job = None
        self._dig_resume = False
        # The decks this set plays through, in order; the pair in use right now.
        self.decks = deck_rotation.TWO
        # Four-deck rotation is off unless explicitly switched on. Its first
        # independent review found it cannot yet run against real deck
        # readings (issue #96); until that is fixed and checked live, every
        # set stays on A and B.
        self.four_deck_enabled = os.environ.get('AFTERHOURS_FOUR_DECK') == '1'
        self.active_deck = None
        self.pair_decks = None
        self.dig_allowed = False      # the planner says when discovery is on; until then nothing browses on its own
        self._dig_ended = None
        self.dig_clock = time.monotonic
        self.background_job = None
        self.analysis_cache = {}
        self.tempo_ramp = None
        self.copilot_requests = {}
        self.availability = availability if availability is not None else RecordingAvailability()
        self.crate = crate if crate is not None else RecordCrate()
        self.crate.availability = self.availability
        self.recordings = recordings if recordings is not None else RECORDINGS
        self.state = {'id': uuid.uuid4().hex, 'phase': 'idle', 'active': False,
                      'message': 'Discover music to build a live set.', 'candidates': [],
                      'order': [], 'events': [], 'transitions': [], 'telemetry': None,
                      'index': -1, 'secondsPerTrack': track_seconds(), 'excerptBars': 16,
                      'mixStyle': 'phrase', 'phraseBeats': 64, 'fadeBeats': 64,
                      'analysis': None, 'aiReview': None, 'executionPlan': None, 'preparationSelection': None,
                      'queuedUpdate': None, 'lastQueueApplication': None, 'lastQueueRepair': None, 'catalogSearch': None,
                      'backgroundAnalysis': None, 'tempoControl': None, 'tempoTargets': None, 'rollingQueue': None,
                      'mixerBaselines': None, 'startup': None, 'lastError': None,
                      'copilotTargets': None,
                      'copilot': {'mode': 'leading', 'held': False, 'command': None, 'selectionEpoch': 0},
                      'setlist': {'allowReorder': True, 'lockedIds': [], 'sourceById': {}},
                      'updated': time.time()}

    @property
    def active(self):
        with self.lock:
            return self.state['active']

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            result['catalogFolder'] = copy.deepcopy(self.folder)
        result['copilotContext'] = copilot_commands.context(result)
        result['copilot']['supportedCommands'] = ['hold', 'go', 'skip', 'off', 'on', 'mix', 'resume']
        result['copilot']['leadMode'] = {'leading':'all','mix':'mix','off':'off'}[result['copilot']['mode']]
        result['recordingAvailability'] = self.availability.snapshot()
        restricted={r['trackId'] for r in result['recordingAvailability'] if r['code']==PREVIEW_ONLY}
        for row in result['candidates']+result['order']+(result.get('queuedUpdate') or {}).get('tracks',[]):
            if row['id'] in restricted:row['playback_verified']=False
        result['crateSummary'] = self.crate.summary()
        return result

    def record_provider_preview(self, track_id, provider, seconds, evidence, observed_at=None):
        """Record explicit operator/native evidence. No transport or queue edit."""
        track=self.crate.get(track_id)
        if not track:raise ValueError('Choose the exact recording already retained in the crate.')
        self.availability.record_preview_only(track_id,provider,seconds,evidence,observed_at)
        self.crate.upsert({**track,'playback_verified':False},source='provider-evidence')
        self.event(f'{track.get("title","Recording")}: {provider} preview restriction recorded; automatic loading is held.',
                   'unavailable',trackId=track_id)
        return self.snapshot()

    def _trim_candidates(self, preferred=()):
        """Bound planning memory while retaining all selected/loaded records."""
        protected = {t['id'] for t in self.state.get('order', [])}
        protected.update(t['id'] for t in (self.state.get('queuedUpdate') or {}).get('tracks', []))
        protected.update((self.state.get('setlist') or {}).get('lockedIds', []))
        protected.update(self.state.get('setPins',[]))
        protected.update(t.get('id') for t in (self.state.get('telemetry') or {}).get('decks', []) if t.get('loaded'))
        records = self.state['candidates']
        kept = {t['id'] for t in records if t['id'] in protected}
        room = max(0, WORKING_LIMIT - len(kept))
        available = {t['id'] for t in records}
        preferred = [tid for tid in dict.fromkeys(preferred) if tid in available and tid not in kept]
        kept.update(preferred[:room])
        room = max(0, WORKING_LIMIT - len(kept))
        extras = [t['id'] for t in records if t['id'] not in kept]
        kept.update(extras[-room:] if room else [])
        self.state['candidates'] = [t for t in records if t['id'] in kept]
        self.positions = {tid: value for tid, value in self.positions.items() if tid in kept}

    def remember_record(self, track, *, source='discovery', query=None, position=None, analysis=None):
        """Persist first, then keep a bounded selection in the working set."""
        archived, inserted = self.crate.upsert(track, source=source, query=query, position=position, analysis=analysis)
        record = {k: v for k, v in archived.items() if k not in ('crate', 'crateSlot')}
        with self.lock:
            known = {t['id'] for t in self.state['candidates']}
            if record['id'] in known:
                self.state['candidates'] = [copy.deepcopy(record) if t['id'] == record['id'] else t for t in self.state['candidates']]
            else:
                self.state['candidates'].append(copy.deepcopy(record))
            if query:
                self.positions[record['id']] = {'position': position or 0, 'query': query}
            self._trim_candidates()
        return record, inserted

    def hydrate_records(self, ids, keep=()):
        """Recall exact archived metadata; this never loads or plays audio.

        `keep` names working recordings the caller still relies on; making
        room for the recalled ones never evicts them.
        """
        if not isinstance(ids, (list, tuple)) or len(ids) > 32:
            raise ValueError('Recall at most 32 crate recordings at a time.')
        found, locators = [], {}
        for track_id in dict.fromkeys(ids):
            record = self.crate.get(track_id)
            if not record:
                continue
            record = {k: v for k, v in record.items() if k not in ('crate', 'crateSlot')}
            locator = self.crate.locator(track_id)
            if not locator:
                # Archived metadata may predate saved browser cursors. Use a
                # safe title/artist search, then revalidate the exact engine ID.
                raw = str(record.get('artist') or record.get('title') or '')
                query = re.sub(r'[\x00-\x1f\x7f"`/\\*?\[\]{}#]', ' ', raw)
                query = ' '.join(query.split())[:160]
                locator = {'query': query, 'position': 0} if query else None
            found.append(record)
            if locator:
                locators[track_id] = locator
        with self.lock:
            # Recall is one operation: adding a later result must not evict a
            # more relevant earlier result that happened to be an old row.
            by_id = {t['id']: t for t in self.state['candidates']}
            by_id.update({t['id']: copy.deepcopy(t) for t in found})
            self.state['candidates'] = list(by_id.values())
            self.positions.update(locators)
            if self.folder is None:
                folder_id = next(((t.get('engine') or {}).get('folder_id') for t in found
                                  if (t.get('engine') or {}).get('folder_id')), None)
                if folder_id:
                    self.folder = {'id': folder_id}
            self._trim_candidates(preferred=[t['id'] for t in found] + [tid for tid in keep if isinstance(tid, str)])
            retained = {t['id'] for t in self.state['candidates']}
        return [t for t in found if t['id'] in retained]

    def set_pins(self, ids, *, restoring=False):
        """Retain exact future-inclusion requests, without changing playback."""
        from afterhours.set_plan import MAX_PINS,exact_id
        if (not isinstance(ids,list) or len(ids)>MAX_PINS or any(not exact_id(tid) for tid in ids)
                or len(set(ids))!=len(ids)):
            raise ValueError(f'Pin at most {MAX_PINS} distinct exact recordings for this set.')
        rows=[self.crate.get(tid) for tid in ids]
        if any(row is None for row in rows):raise ValueError('A pinned recording is missing from the saved crate.')
        old=set(self.snapshot().get('setPins',[]))
        if not restoring:self.require_available([tid for tid in ids if tid not in old])
        with self.lock:
            self.state['setPins']=list(ids)
            self.hydrate_records(ids)
            self._trim_candidates()
        return self.snapshot()

    def recall_records(self, text, limit=12):
        rows = self.crate.search(text, limit=limit)
        return self.hydrate_records([t['id'] for t in rows if not self.availability.blocked(t['id'])])

    def control_state(self):
        """Copy only cached physical readings for the high-frequency UI lane.

        The program/library/analysis has its own slower endpoint. Never rebuild
        that payload or acquire the OSC owner merely to read a fader.
        """
        with self.lock:
            return {'sessionId': self.state['id'],
                    'telemetry': copy.deepcopy(self.state.get('telemetry'))}

    def require_available(self, ids):
        for track_id in ids:
            unavailable=self.availability.blocked(track_id)
            if unavailable:
                raise ValueError(unavailable['reason']+' This recording is excluded from automatic selection until it can be checked again.')

    def restore_lead_mode(self, mode):
        """Restore a preference on an idle session, never a transport command."""
        if mode not in ('all', 'mix', 'off'):
            raise ValueError('Unknown lead mode.')
        with self.lock:
            if self.state['active'] or any(d.get('playing') for d in (self.state.get('telemetry') or {}).get('decks', [])):
                raise ValueError('A saved lead mode cannot replace a running session.')
            state = self.state['copilot']
            if (state.get('command') or {}).get('status') == 'pending':
                raise ValueError('A saved lead mode cannot replace a pending control.')
            requested = {'all': 'leading', 'mix': 'mix', 'off': 'off'}[mode]
            if state['mode'] != requested:
                state.update(mode=requested, held=False, selectionEpoch=state.get('selectionEpoch', 0)+1)

    def copilot_command(self, action, command_id, expected, *, held_recording=None):
        """Accept once against an exact run/plan/pair. Only the OSC owner loads.

        Holding gates future handoffs, not the playing transport. Off uses the
        existing takeover path. On can resume only a coherent, measured pair.
        """
        if action not in ('hold', 'go', 'skip', 'off', 'on', 'mix', 'resume'):
            raise ValueError('Unknown Copilot control.')
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 100:
            raise ValueError('A command identity is required.')
        with self.lock:
            previous = self.copilot_requests.get(command_id)
            if previous:
                if (previous['action'] != action or previous['context'] != expected
                        or previous.get('heldRecording') != held_recording):
                    raise ValueError('This command identity belongs to a different request.')
                return self.snapshot()
            s = self.snapshot()
            copilot_commands.check_context(s, expected)
            if held_recording is not None and (action != 'resume' or held_recording != copilot_commands.held_recording(s)):
                raise ValueError('The held recording changed before Resume arrived. No playback was started.')
            state = self.state['copilot']
            audible = any(d.get('playing') for d in (s.get('telemetry') or {}).get('decks', []))
            resuming = (action in ('go','on','mix','resume') and not s['active']
                        and copilot_commands.resumable(s) and (audible or action == 'resume'))
            if (state.get('command') or {}).get('status') == 'pending' and action != 'off':
                raise ValueError('Wait for the pending Copilot control to finish.')
            if action in ('on','mix','resume') and not s['active'] and s['phase'] == 'error' and not resuming:
                raise ValueError(s.get('lastError') or 'The set stopped after an engine error. Playback has not resumed.')
            if action == 'resume' and not s['active'] and not resuming:
                raise ValueError('There is no interrupted prepared set to resume. Say start the queue to begin.')
            if resuming:
                current, _ = (copilot_commands.resume_pair(s, allow_paused=True) if action == 'resume'
                              else copilot_commands.playing_pair(s))
                final = s['index'] == len(s['order'])-1
                plan = s.get('executionPlan') or {}
                if not plan.get('applied'):
                    raise ValueError('Handback needs the prepared mix plan.')
                copilot_commands.retime(s,current,0 if final else plan['transitions'][s['index']]['fadeBeats'],final)
            if action in ('hold', 'go', 'skip') or action=='resume' and s['active'] and state['held']:
                if not resuming and (not s['active'] or s['phase'] != 'playing' or state['mode'] not in ('leading', 'mix')):
                    raise ValueError('This control needs a running set outside a blend.')
                if action == 'skip' and state['mode'] == 'mix':
                    raise ValueError('Mixing only keeps your chosen order. Pick the replacement in Catalog.')
                current, _ = copilot_commands.playing_pair(s)
                if s['index']+1 >= len(s['order']):
                    raise ValueError('There is no next recording to steer.')
                quick_go=action=='go' and s.get('mixStyle')=='quick' and (s.get('timing') or {}).get('basis')=='native-beat-grid'
                if not (s.get('executionPlan') or {}).get('applied') and not quick_go:
                    raise ValueError('This control needs an applied measured mix plan.')
                if action == 'hold' and not state['held']:
                    copilot_commands.retime(s, current, s.get('fadeBeats', 0))
                if action == 'skip':
                    copilot_commands.skip_plan(s)  # reject before any transport
                if resuming:
                    copilot_commands.retime(s,current,s['executionPlan']['transitions'][s['index']]['fadeBeats'])
            if action in ('on', 'mix') and not s['active'] and audible:
                if not resuming:
                    raise ValueError('Take over and inspect the playing pair before resuming Copilot.')
                copilot_commands.playing_pair(s)
            if action == 'off' and (state.get('command') or {}).get('status') == 'pending':
                self._copilot_result('cancelled', 'The pending control was cancelled by takeover.')
            receipt = {'id': command_id, 'action': action, 'context': copy.deepcopy(expected),
                       'status': 'pending', 'at': time.time(), 'message': 'Control accepted; the engine is checking it.'}
            if held_recording is not None:
                receipt['heldRecording'] = copy.deepcopy(held_recording)
            self.copilot_requests[command_id] = receipt
            state['command'] = receipt
            if action in ('on', 'mix', 'off', 'resume') or resuming:
                state['selectionEpoch'] = state.get('selectionEpoch', 0) + 1
            if action == 'hold':
                timing = s.get('timing') or {}
                state.update(held=True, heldCountdownBeats=max(0, timing.get('mixAtBeat', current['beatPosition'])-current['beatPosition']))
                self._copilot_result('confirmed', 'The next handoff is paused. The current recording keeps playing.')
            elif action == 'off':
                state.update(mode='off', held=False)
                self.stop(pause=False)
                if s['active']:
                    receipt['message']='Handing the decks back. Owned automation is stopping.'
                else:
                    self._copilot_result('confirmed', 'Copilot is off. The decks are yours.')
            elif action=='resume' and s['active'] and state['held']:
                receipt['message']='Resuming the next handoff at a verified cue.'
            elif action in ('on', 'mix', 'resume') or resuming:
                requested_mode = 'mix' if action == 'mix' else 'leading'
                if resuming:
                    try:
                        state['mode'] = requested_mode
                        if action == 'resume':
                            guard = {'held_recording': held_recording} if held_recording is not None else {}
                            self.start(resume=True,adopt_mixer=True,resume_paused=True,**guard)
                        else:
                            self.start(resume=True,adopt_mixer=True)
                    except (ValueError, BridgeError):
                        state['mode'] = s['copilot']['mode']
                        self._copilot_result('failed', 'Copilot could not resume the measured set.')
                        raise
                else:
                    state['mode']=requested_mode
                    if not s['active']:state['held']=False
                    self._copilot_result('confirmed', 'Mixing only. Your chosen tracks stay in order; Copilot times the blends.' if action == 'mix' else
                                         'Copilot is leading this set.' if s['active'] else 'Copilot is on. Music remains paused.')
            self.state['updated'] = time.time()
            return self.snapshot()

    def _copilot_result(self, status, message, command_id=None):
        with self.lock:
            command = self.state['copilot'].get('command')
            if command_id is not None and (not command or command['id'] != command_id):
                return
            if command:
                command.update(status=status, message=message, completedAt=time.time())
            self.state['updated'] = time.time()
            self.event(message, 'copilot-control', status=status)

    def service_copilot(self, client, current, incoming, ready, target, window):
        """Called only by the existing playback worker between timing reads."""
        s = self.snapshot()
        command = s['copilot'].get('command') or {}
        if command.get('status') != 'pending' or command.get('action') not in ('go', 'skip', 'resume'):
            return None
        try:
            copilot_commands.check_context(s, command['context'])
            active, cued = copilot_commands.playing_pair(s)
            if active['deck'] != current['deck'] or cued['deck'] != incoming:
                raise ValueError('The playing pair changed before the control could run.')
            if command['action'] in ('go','resume'):
                if s.get('mixStyle')=='quick' and window.get('basis')=='native-beat-grid':
                    block=window['phraseBeats']
                    next_bar=window['entryBeat']+math.ceil((active['beatPosition']+2-window['entryBeat'])/block)*block
                    begin=min(window['mixAtBeat'],next_bar)
                    end=begin+window['fadeBeats']
                    changed={**window,'mixAtBeat':begin,'endBeat':end,'segmentBeats':end-window['entryBeat']}
                    message='The next recording enters at the next available grid bar.'
                else:
                    cue_planner.verify_receipts(s['analysis'])
                    changed=copilot_commands.retime(s, active, window['fadeBeats'])
                    message='The next recording enters at the next verified cue.'
                with self.lock:
                    self.state['copilot']['held'] = False
                    self.check()
                    self._copilot_result('confirmed',message,command['id'])
                return {'window': changed}
            order, prepared = copilot_commands.skip_plan(s)
            plan = prepared['executionPlan']
            plan['applied'] = True
            revised = {**s, **prepared, 'order': order}
            changed = copilot_commands.retime(revised, active, plan['transitions'][s['index']]['fadeBeats'])
            cue_planner.verify_receipts(prepared['analysis'])
        except ValueError as exc:
            self._copilot_result('failed', str(exc), command['id'])
            return None
        # Preparation touches only the observed silent incoming deck. A failed
        # load is not undone, retried, or published as a successful queue change.
        try:
            if self.mixer:
                self.mixer.cancel_ramps(client)
                self.mixer.reset_paused(client, incoming)
            ready(client, order[s['index']+1], incoming, target, s['index']+1, plan)
            reading = self.telemetry(client)
            if reading['decks'][incoming-1]['id'] != order[s['index']+1]['id'] or reading['decks'][incoming-1]['playing']:
                raise BridgeError('The replacement pick was not confirmed. No handoff was started.')
            observed, _ = copilot_commands.playing_pair({**revised, 'telemetry': reading})
            changed = copilot_commands.retime(revised, observed, changed['fadeBeats'])
            with self.lock:
                self.check()
                self.state.update(order=order, **prepared)
                self._copilot_result('confirmed', f"{order[s['index']+1]['title']} is the next prepared pick.", command['id'])
            return {'window': changed, 'order': order, 'plan': plan, 'telemetry': reading}
        except (ValueError, BridgeError, OSError):
            self._copilot_result('unconfirmed', 'The replacement could not be confirmed. Scheduling stopped; no retry was sent.', command['id'])
            raise

    def new_set(self):
        with self.lock:
            if (self.state['active'] or self.worker and self.worker.is_alive()
                    or any(d.get('playing') for d in (self.state.get('telemetry') or {}).get('decks',[]))):
                raise ValueError('Pause playback and finish preparation before starting a fresh set.')
            catalog=copy.deepcopy(self.state['candidates'])
            self.archive_call('boundary')
            cleanup={key:copy.deepcopy(self.state.get(key)) for key in ('resumeBlocked','cleanupPending')}
            self.state=DJSession(self.control_lock,self.port,self.factory,crate=self.crate).state
            # Fresh set clears its order, not the operator's music collection.
            # Exact identities and saved searches still revalidate at load.
            self.state['candidates']=catalog
            self.state.update(cleanup)
            self.state['operatorFresh']=True
            self.eq_readings={}
            self.catalog_job=None
            self.analysis_cache={}
            self.copilot_requests={}
            self.state['message']='Fresh set. Tell the DJ what you want to hear.'
            return self.snapshot()

    def waveform(self,track_id):
        """Read prepared display data for this session's exact recording only."""
        snapshot=self.snapshot()
        evidence=next((t for t in (snapshot.get('analysis') or {}).get('tracks',[]) if t.get('id')==track_id),None)
        if not evidence or not isinstance(track_id,str) or not re.fullmatch(r'virtualdj:[a-f0-9]{64}',track_id):return None
        return verified_waveform(track_id.removeprefix('virtualdj:'),evidence.get('duration'),
                                 (evidence.get('energyMap') or {}).get('reportDigest'))

    def restore(self, saved):
        """Explicit local recovery of a crate. Never restore active transport."""
        if not isinstance(saved, dict):
            raise ValueError('Saved set must be a session object.')
        saved = saved.get('final', saved)
        candidates = saved.get('candidates') if isinstance(saved, dict) else None
        if not isinstance(candidates, list) or not 2 <= len(candidates) <= 100:
            raise ValueError('Saved set needs 2–100 catalog records.')
        positions, folders = {}, set()
        for track in candidates:
            if not isinstance(track, dict) or not isinstance(track.get('engine'), dict):
                raise ValueError('Saved track metadata is incomplete.')
            engine = track['engine']
            for field in ('selection_id', 'folder_id'):
                if not isinstance(engine.get(field), str) or not re.fullmatch(r'[a-f0-9]{64}', engine[field]):
                    raise ValueError('Saved track identity is invalid.')
            if track.get('id') != 'virtualdj:' + engine['selection_id'] or track['id'] in positions:
                raise ValueError('Saved track identity is duplicated or inconsistent.')
            number(track.get('bpm'), 40, 300)
            if not isinstance(track.get('title'), str) or not track['title'].strip():
                raise ValueError('Saved track has no title.')
            search_literal(track.get('artist'))
            positions[track['id']] = {'position': 0, 'query': track['artist']}
            folders.add(engine['folder_id'])
        # A crate can retain recordings found in earlier folders. Their origin
        # is provenance, not the current search scope. Recover that scope
        # explicitly; all later search/load identity and folder guards remain.
        catalog_folder = saved.get('catalogFolder')
        if catalog_folder is not None:
            if (not isinstance(catalog_folder,dict) or
                    not isinstance(catalog_folder.get('id'),str) or
                    not re.fullmatch(r'[a-f0-9]{64}',catalog_folder['id'])):
                raise ValueError('Saved catalog folder binding is invalid.')
            catalog_folder = {'id':catalog_folder['id']}
        elif len(folders) == 1:
            catalog_folder = {'id':next(iter(folders))}
        else:
            raise ValueError('A saved multi-catalog crate needs a current catalog folder binding. Inspect the catalog before restoring.')
        order = saved.get('order')
        if not isinstance(order, list) or any(not isinstance(t, dict) or not isinstance(t.get('id'),str) for t in order):
            raise ValueError('Saved set has no valid order.')
        recovered=saved.get('setlist',{'allowReorder':True,'lockedIds':[],'sourceById':{}})
        order_ids={t.get('id') for t in order}
        if (not isinstance(recovered,dict) or type(recovered.get('allowReorder')) is not bool
                or not isinstance(recovered.get('lockedIds'),list)
                or any(not isinstance(i,str) or i not in order_ids for i in recovered['lockedIds'])
                or len(set(recovered['lockedIds']))!=len(recovered['lockedIds'])
                or not isinstance(recovered.get('sourceById'),dict)
                or any(i not in order_ids or source not in ('planner','operator') for i,source in recovered['sourceById'].items())):
            raise ValueError('Saved setlist preferences are invalid.')
        with self.lock:
            if self.state['active']:
                raise ValueError('Cannot restore an active set.')
            before = copy.deepcopy(self.state)
            try:
                self.state['candidates'] = copy.deepcopy(candidates)
                self.state['setlist']={'allowReorder':True,'lockedIds':[],'sourceById':{}}
                if order:
                    recovered_route=None
                    if isinstance(saved.get('analysis'),dict) and isinstance(saved.get('aiReview'),dict):
                        try:
                            recovered_plan=cue_planner.compile_plan(saved['aiReview'],saved['analysis'],order,
                                saved.get('secondsPerTrack',30),saved.get('mixStyle','quick'))
                            recovered_route=recovered_plan.get('tempoSchedule')
                        except (KeyError,TypeError,IndexError,AttributeError) as exc:
                            raise ValueError('Saved tempo plan is malformed.') from exc
                    self.plan([t.get('id') for t in order], saved.get('secondsPerTrack',30),
                              saved.get('excerptBars',16), saved.get('maxTempoShift',8),
                              saved.get('mixStyle','quick'),source='operator',tempo_targets=saved.get('tempoTargets'),_tempo_route=recovered_route,
                              quick_blend_seconds=saved.get('quickBlendSeconds'),
                              _archive_boundary=False)
                else:
                    # A conversation can finish a catalog search before choosing
                    # an order. Recover those results without inventing a set.
                    if saved.get('analysis') or saved.get('aiReview'):
                        raise ValueError('An empty order cannot carry a prepared mix plan.')
                    self.state.update(order=[],analysis=None,aiReview=None,executionPlan=None)
                self.state['setlist']=copy.deepcopy(recovered)
                # Preserve a paid, validated plan across local server repairs.
                # Restore evidence only after checking its checksum and local
                # receipts. Native cue readbacks must still pass Apply again.
                report=copy.deepcopy(saved.get('analysis'))
                review=copy.deepcopy(saved.get('aiReview'))
                if report is not None:
                    if (not isinstance(report,dict) or report.get('cueVersion')!=cue_planner.VERSION
                            or not isinstance(report.get('tracks'),list)
                            or any(not isinstance(t,dict) for t in report['tracks'])
                            or [t.get('id') for t in report['tracks']]!=[t['id'] for t in order]
                            or report.get('secondsBetweenEntries')!=self.state['secondsPerTrack']
                            or report.get('fingerprint')!=cue_planner.fingerprint(report['tracks'])):
                        raise ValueError('Saved analysis is stale or invalid. Prepare this set again.')
                    cue_planner.verify_receipts(report)
                    self.state['analysis']=report
                if review is not None:
                    if (not report or not isinstance(review,dict)
                            or review.get('analysisFingerprint')!=report['fingerprint']
                            or review.get('secondsBetweenEntries')!=self.state['secondsPerTrack']):
                        raise ValueError('Saved AI plan is stale. Prepare and plan this set again.')
                    checked=ai_mix.validate_plan({k:review.get(k) for k in ai_mix.PLAN_SCHEMA['properties']},report,self.state['secondsPerTrack'])
                    self.state['aiReview']={**review,**checked,'appliedToPlayback':False}
                self.state['executionPlan']=None
            except (ValueError, BridgeError):
                self.state = before
                raise
            self.archive_call('boundary')
            self.positions, self.folder = positions, catalog_folder
            if before.get('resumeBlocked') or saved.get('resumeBlocked'):
                # Cleanup uncertainty survives a restart or fresh-set restore;
                # it is evidence of native ownership, not replayable transport.
                pending=[source.get('cleanupPending') for source in (before,saved) if source.get('resumeBlocked')]
                self.state.update(resumeBlocked=True,cleanupPending=merge_cleanup_pending(*pending))
            self.crate.remember(candidates, source='restored-set')
            self._trim_candidates()
            self.availability.restore(saved.get('recordingAvailability',[]))
            for field in ('query','artistFilter','genreFilter','bpmMin','bpmMax'):
                if field in saved:
                    self.state[field] = copy.deepcopy(saved[field])
            self.update(phase='stopped' if order else 'catalog-ready', active=False, telemetry=None, index=-1,
                        message=('Saved catalog restored. The DJ can choose from these recordings.' if not order else
                                 'Saved AI plan restored. Apply it to verify native cues before starting.' if self.state.get('aiReview') else
                                 'Saved set restored. Prepare decks before the next run; audio was not started.'))
            self.event('Saved catalog and order restored. No engine commands were sent.', 'restored')
        return self.snapshot()

    def update(self, **values):
        # Metadata changes are infrequent. Telemetry updates and snapshots do
        # not touch SQLite or copy the growing archive.
        for field in ('candidates', 'order'):
            if field in values:
                self.crate.remember(values[field], source='session')
        if values.get('queuedUpdate'):
            self.crate.remember(values['queuedUpdate'].get('tracks', []), source='queue')
        with self.lock:
            self.state.update(values, updated=time.time())
            if 'candidates' in values:
                self._trim_candidates()
            observed=None
            if self.archive and set(values)&{'telemetry','phase','active','startedAt','analysis','queuedUpdate'}:
                try:
                    from afterhours.run_archive import observation
                    observed=observation(self.state,time.time())
                except Exception:
                    self.archive.error='Playback observation could not be archived. Music control is unchanged.'
        if observed is not None:self.archive_call('observe',observed)

    def archive_call(self,method,*args):
        """Archival is observational; an archive failure cannot interrupt audio."""
        if self.archive:
            try:return getattr(self.archive,method)(*args)
            except Exception:
                self.archive.error='Playback observation could not be archived. Music control is unchanged.'

    def event(self, text, kind='info', **facts):
        with self.lock:
            self.state['events'].append({'at': time.time(), 'kind': kind, 'text': text, **facts})
            self.state['events'] = self.state['events'][-100:]

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def sleep(self, seconds):
        if self.cancel.wait(seconds):
            raise Cancelled()

    def service_telemetry(self, client):
        """The active worker refreshes the display while holding the OSC lock."""
        s = self.snapshot()
        if s['phase'] != 'playing' or s['index'] < 0:
            return
        t = s.get('telemetry')
        if t and time.time() - t['sampledAt'] < .4:
            return
        try:
            reading=self.telemetry(client, preloading=True)
        except (BridgeError, OSError):
            return  # Real read failures still expire; never invent a live clock.
        if self.tempo_ramp and self.tempo_ramp.row:
            active=self.tempo_ramp.deck
            self.update(tempoControl=self.tempo_ramp.tick(client,reading['decks'][active-1],
                                                          reading['decks'][deck_rotation.following(active,self.decks)-1]))

    def wait_observed(self, client, seconds):
        if self.snapshot()['phase'] != 'playing':
            return self.sleep(seconds)
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            self.service_telemetry(client)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.sleep(min(.4, remaining))

    def q(self, client, script, kind=None):
        self.check()
        return client.query(script, kind)

    def send(self, client, script):
        self.check()
        return client.send(script)

    def _launch(self, fn, phase, wait_for_control=False):
        with self.lock:
            if self.state['active']:
                raise ValueError("A session operation is already running.")
            acquired=self.control_lock.acquire(blocking=False)
            if not acquired and not (wait_for_control or self.observing):
                raise ValueError("VirtualDJ is busy. Wait for its current command to finish.")
            self.cancel.clear()
            self.stop_audio = False
            self.state.update(active=True, phase=phase, lastError=None)
            self.worker = threading.Thread(target=self._work if acquired else self._wait_for_control, args=(fn,), daemon=True)
            self.worker.start()

    def _wait_for_control(self, fn):
        # Reserve the single worker before waiting for a read-only observation.
        # Wait outside the state lock so its telemetry update can finish. This
        # is one dispatch, never a retry of an uncertain engine command.
        if not self.control_lock.acquire(timeout=3):
            message=('Stop could not acquire VirtualDJ. Pause the decks in VirtualDJ.' if self.stop_audio else
                     'VirtualDJ readings did not finish. No command was sent.')
            with self.lock:
                self.update(active=False,phase='error',message=message,lastError=message)
                if (self.state['copilot'].get('command') or {}).get('status')=='pending':
                    self._copilot_result('failed',message)
            return
        self._work(fn)

    def remember_cleanup_failure(self, kind, owner):
        """Retain bounded owned ramps and exact analysis-deck cleanup evidence."""
        names=[];analysis=[]
        if kind in ('mixer','handoff'):
            ramps=getattr(owner,'ramps',None)
            if isinstance(ramps,dict):
                names=[f'{deck}:afterhours_{name}' for name,deck in ramps.items()]
        elif kind=='tempo' and isinstance(getattr(owner,'NAME',None),str):
            names=[f'{owner.deck}:{owner.NAME}']
        elif kind=='background':
            describe=getattr(owner,'cleanup_descriptors',None)
            if callable(describe):analysis=describe()
        with self.lock:
            sources=[{'ramps':names,'analysis':analysis,'unknown':not (names or analysis)}]
            if self.state.get('resumeBlocked'):sources.insert(0,self.state.get('cleanupPending'))
            self.state.update(resumeBlocked=True,cleanupPending=merge_cleanup_pending(*sources))

    def retry_paused_cleanup(self, client):
        """Explicit Stop retries retained cleanup, never Load, Play or Mix."""
        snapshot=self.snapshot()
        if not snapshot.get('resumeBlocked'):return
        pending=merge_cleanup_pending(snapshot.get('cleanupPending'))
        names=pending.get('ramps') or []
        allowed={'afterhours_'+name for name in ('in_level','out_low','out_mid','out_vocal','out_instru',
                 'in_low','in_mid','in_vocal','in_instru','out_high','crossfade')}|{tempo_planner.TempoRamp.NAME}
        stops=[]
        for descriptor in names:
            deck,separator,name=descriptor.partition(':')
            if not separator:  # Old checkpoints only retained the ramp name.
                name=deck
            if name not in allowed or (separator and deck not in ('1','2','3','4')):
                return  # Recovered data can never supply an arbitrary engine verb.
            prefix=f'deck {deck} ' if separator else ''
            stops.append(f"{prefix}repeat_stop '{name}'")
        if stops:client.send(' & '.join(stops))
        remaining=[];errors=[]
        for descriptor in pending.get('analysis',[]):
            try:cleanup_analysis_deck(client,descriptor)
            except (BridgeError,OSError,ValueError) as exc:
                descriptor['error']=str(exc)[:300];remaining.append(descriptor);errors.append(str(exc))
        unknown=bool(pending.get('unknown'))
        result={'ramps':[],'unknown':unknown}
        if remaining:result['analysis']=remaining
        # A final read timeout must retain the updated restoration stage, so a
        # later explicit Stop can verify success without overwriting controls.
        self.update(resumeBlocked=True,cleanupPending=pending)
        if any(as_bool(client.query(f'deck {deck} play','bool')) for deck in self.decks):
            result['unknown']=True
            self.update(resumeBlocked=True,cleanupPending=result)
            raise BridgeError('A deck started during automation cleanup; recovery remains blocked.')
        blocked=unknown or bool(remaining)
        self.update(resumeBlocked=blocked,cleanupPending=result if blocked else None)
        if errors:raise BridgeError('Analysis cleanup remains unconfirmed: '+'; '.join(errors))

    def _work(self, fn):
        client = None
        cleanup_failed = False
        try:
            with self.factory(self.port, timeout=.45) as client:
                try:
                    fn(client)
                finally:
                    if self.tempo_ramp:
                        try:self.tempo_ramp.cancel(client)
                        except (BridgeError,OSError):
                            cleanup_failed=True
                            self.remember_cleanup_failure('tempo',self.tempo_ramp)
                            self.event('Tempo cancellation unconfirmed; inspect VirtualDJ.','error')
                    if self.handoff:
                        try:self.handoff.cancel_ramps(client)
                        except (BridgeError,OSError):
                            cleanup_failed=True
                            self.remember_cleanup_failure('handoff',self.handoff)
                            self.event('Native crossfade cancellation unconfirmed; inspect the mixer.','error')
                    if self.mixer:
                        try:
                            self.mixer.cancel_ramps(client)
                        except (BridgeError, OSError):
                            cleanup_failed=True
                            self.remember_cleanup_failure('mixer',self.mixer)
                            self.event('Native EQ ramp cancellation unconfirmed; inspect the mixer.', 'error')
                    # Stop audible automation before potentially slow decoder
                    # effect restoration on the isolated preparation decks.
                    if self.background_job:
                        self.analysis_cache.update(self.background_job.cache)
                        try:self.background_job.close(client)
                        except (BridgeError,OSError,ValueError) as exc:
                            cleanup_failed=True
                            self.remember_cleanup_failure('background',self.background_job)
                            self.event('Analysis deck cleanup unconfirmed: '+str(exc),'analysis')
                        self.update(backgroundAnalysis={**self.background_job.snapshot(),'status':'paused'})
                        self.background_job=None
        except Cancelled:
            self.update(phase='stopped' if self.stop_audio else 'takeover',
                        message='Stopping the set.' if self.stop_audio else
                        'You have control. EQ automation stopped at its current values; an active crossfade may finish.')
            self.event('Operator stopped the set.' if self.stop_audio else 'Operator took control.', 'takeover')
        except MixerControlChanged as exc:
            if cleanup_failed:
                message='Mixer controls changed and automation cancellation is unconfirmed. Inspect the mixer before resuming.'
                self.update(phase='error',message=message,lastError=message)
                self.event(message,'error')
            else:
                message=('Deck tempos changed. Automatic mixing is paused; the current track keeps playing.'
                         if str(exc).startswith('Deck tempos moved') else
                         'Mixer controls changed. You have control; the current track keeps playing.')
                with self.lock:
                    state=self.state['copilot']
                    state.update(mode='off',held=False,selectionEpoch=state.get('selectionEpoch',0)+1)
                    self.state.update(phase='takeover',message=message,lastError=None)
                self.event(message,'takeover',reason=str(exc))
        except OpeningHandoffHeld as exc:
            message=('The opening is playing. The next handoff is on hold. '+str(exc)
                     if not cleanup_failed else 'The opening was confirmed playing. Check the preparation deck before mixing.')
            with self.lock:
                self.state['copilot'].update(mode='off',held=False)
                self.state.update(phase='takeover',message=message,lastError=None)
            self.event(message,'analysis',reason=str(exc))
        except (BridgeError, OSError, ValueError) as exc:
            self.update(phase='error', message=str(exc), lastError=str(exc))
            with self.lock:
                if (self.state.get('startup') or {}).get('status') in ('loading','starting','analyzing'):
                    self.state['startup']['status']='blocked'
            self.event(str(exc), 'error')
        except Exception:
            self.update(phase='error', message='Session stopped after an internal error. Inspect VirtualDJ before restarting.',
                        lastError='Session stopped after an internal error. Inspect VirtualDJ before restarting.')
            self.event('Internal session error; commands were not replayed.', 'error')
        finally:
            # A lost connection never justifies replaying Start/Load/Mix. An
            # explicit Stop sends the idempotent pause command once, separately.
            if self.stop_audio:
                try:
                    stopped_crossfader=None
                    with self.factory(self.port, timeout=.5, budget_seconds=3) as stop:
                        # Every deck the set plays through, wherever the rotation stood.
                        stop.send(' & '.join(f'deck {deck} pause' for deck in self.decks))
                        paused = not any(as_bool(stop.query(f'deck {deck} play', 'bool')) for deck in self.decks)
                        if paused:
                            self.retry_paused_cleanup(stop)
                            if self.handoff:
                                try:stopped_crossfader=number(stop.query('crossfader'),0,1)
                                except (BridgeError,OSError):pass
                        if paused and self.mixer:
                            # Restore only while confirmed paused. This cleanup
                            # deliberately bypasses the canceled worker checks.
                            for deck,controls in self.mixer.baselines.items():
                                stop.send(' & '.join(f'deck {deck} {name} {v*100:.4f}%' for name,v in controls.items()))
                    self.update(message=('Both decks are paused. Native cleanup remains unconfirmed; inspect VirtualDJ.'
                                         if self.snapshot().get('resumeBlocked') else 'Set stopped. Both decks are paused.') if paused else
                                'Stop sent; paused state is unconfirmed. Check VirtualDJ.')
                    with self.lock:
                        telemetry = self.state.get('telemetry')
                        if telemetry and paused:
                            for deck in telemetry['decks']:
                                deck['playing'] = False
                            if stopped_crossfader is not None:
                                telemetry['crossfader']=stopped_crossfader
                            if self.handoff is None or stopped_crossfader is not None:
                                telemetry['sampledAt']=time.time()
                        if paused:self.state.update(activeHandoff=None,transitionControl=None)
                except (BridgeError, OSError):
                    self.update(message='Stop could not be confirmed. Pause the decks in VirtualDJ.')
            terminal=self.snapshot()
            if terminal['phase'] in ('stopped','error') and terminal.get('startedAt'):
                # Stop may be repeated after completion to verify paused decks
                # or retry owned cleanup. That does not extend a finished set.
                if not (self.stop_audio and terminal.get('endedAt') is not None):
                    self.update(endedAt=time.time())
            with self.lock:
                self.state['copilot']['held'] = False
                command=self.state['copilot'].get('command') or {}
                if command.get('status') == 'pending':
                    if command.get('action') == 'off' and self.state['phase'] == 'takeover' and not cleanup_failed:
                        self._copilot_result('confirmed', 'Copilot is off. The decks are yours.')
                    else:
                        self._copilot_result('unconfirmed', 'Scheduling ended before the control was confirmed. Inspect the mixer; no retry was sent.')
            self.update(active=False,copilotTargets=None)
            if self.catalog_job and not self.catalog_job.done:
                self.catalog_job.finish('Crate search paused with the session. Recordings already found were kept.')
            self.mixer = None
            self.handoff = None
            self.tempo_ramp = None
            self.control_lock.release()

    def stop(self, pause=True):
        with self.lock:
            self.state['copilot']['held'] = False
            if not pause:
                if self.state['copilot']['mode']!='off':
                    self.state['copilot']['selectionEpoch']=self.state['copilot'].get('selectionEpoch',0)+1
                self.state['copilot']['mode'] = 'off'
            command = self.state['copilot'].get('command') or {}
            if command.get('status') == 'pending' and command.get('action') not in ('off', 'on', 'mix'):
                self._copilot_result('cancelled', 'The pending control was cancelled by the operator.')
            if not self.state['active']:
                if not pause:
                    self.state.update(phase='takeover', message='You have control. Individual deck transport is available; audio is unchanged.', updated=time.time())
                    return self.snapshot()
                def stop_idle(client):
                    self.stop_audio = True
                    raise Cancelled()
                self._launch(stop_idle, 'stopping', wait_for_control=True)
            self.stop_audio = pause
            self.cancel.set()
            self.state['message'] = 'Stopping…' if pause else 'Handing control back…'
        return self.snapshot()

    def discover(self, query, artist='', genre='', bpm_min=40, bpm_max=300):
        terms = search_terms(query)
        if not isinstance(artist, str) or len(artist) > 120:
            raise ValueError('Artist filter must be under 120 characters.')
        if not isinstance(genre,str) or len(genre)>80:
            raise ValueError('Genre filter must be under 80 characters.')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (bpm_min,bpm_max)) or not 40<=bpm_min<=bpm_max<=300:
            raise ValueError('Choose a catalog tempo range from 40 to 300 BPM.')
        normalized_genre = re.sub(r'[^a-z0-9]', '', genre.casefold())
        def work(client):
            before = client.status()
            if before['automix'] or any(d['playing'] for d in before['decks']):
                raise BridgeError('Pause both decks and turn Automix off before catalog preparation.')
            folder = folder_state(client)
            self.folder = folder
            self.positions = {}
            self.archive_call('boundary')
            self.update(candidates=[], order=[], index=-1, transitions=[], telemetry=None,
                        setlist={'allowReorder':True,'lockedIds':[],'sourceById':{}},
                        query=query, artistFilter=artist, genreFilter=genre, bpmMin=bpm_min, bpmMax=bpm_max,
                        message='Searching the selected VirtualDJ catalog…')
            records, seen = [], set()
            filters = [item.strip().casefold() for item in artist.split(';') if item.strip()]
            quota = 12 // len(terms)
            for term in terms:
                self.update(message=f'Searching {term}…')
                self.send(client, 'search ' + search_literal(term))
                self.sleep(1.5)
                self.send(client, "browser_window 'songs' & browser_scroll 'top'")
                self.sleep(.15)
                added, last_id, repeated = 0, None, 0
                # Fair per-search allocation keeps one artist from filling the
                # crate. No streams are opened to read this metadata.
                for position in range(101):
                    self.check()
                    if folder_state(client)['id'] != folder['id']:
                        raise BridgeError('The catalog folder changed during discovery. Nothing was started.')
                    try:
                        selected = selection(client, True)
                    except BridgeError:
                        selected = None
                    if selected:
                        repeated = repeated + 1 if selected['id'] == last_id else 0
                        last_id = selected['id']
                        if repeated >= 2:
                            break
                        signature = song_identity(selected['metadata'])[0]
                        matches = not filters or any(f in selected['artist'].casefold() for f in filters)
                        metadata = selected['metadata']
                        matches = matches and (not normalized_genre or normalized_genre in re.sub(r'[^a-z0-9]', '', metadata.get('genre','').casefold()))
                        matches = matches and isinstance(metadata.get('bpm'),(int,float)) and bpm_min<=metadata['bpm']<=bpm_max
                        if matches:
                            # Exact recordings, including alternate releases
                            # omitted by this short set, remain in the crate.
                            self.crate.upsert(metadata,source='discovery',query=term,position=position)
                        if matches and signature not in seen and not self.availability.blocked(selected['metadata']['id']):
                            seen.add(signature)
                            records.append(selected['metadata'])
                            self.positions[selected['metadata']['id']] = {'position': position, 'query': term}
                            added += 1
                            self.update(candidates=copy.deepcopy(records), message=f"{term} · {len(records)} songs found across the catalog.")
                            if added >= quota:
                                break
                    self.send(client, 'browser_scroll +1')
                    self.sleep(.04)
                self.event(f'{term}: {added} distinct songs added.', 'catalog')
            if len(records) < 2:
                raise BridgeError('Fewer than two matching songs were found. Widen the search or artist filter.')
            self.event(f"Discovered {len(records)} distinct songs in {folder['name']}.", 'catalog')
            self.update(phase='catalog-ready', candidates=records,
                        message=f'{len(records)} catalog songs read. Build an order to compare tempo and key.')
        self._launch(work, 'discovering')
        return self.snapshot()

    def plan(self, ids, seconds=30, excerpt_bars=16, max_tempo_shift=8, mix_style='phrase',
             source='planner', tempo_targets=None, _tempo_route=None, _archive_boundary=True, quick_blend_seconds=None):
        with self.lock:
            if self.state['active']:
                raise ValueError('Wait for the current session operation to finish.')
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 15 <= seconds <= 300:
                raise ValueError('Choose 15–300 seconds per track.')
            if type(excerpt_bars) is not int or not 0 <= excerpt_bars <= 32:
                raise ValueError('The excerpt can begin 0–32 bars after the first beat.')
            if type(max_tempo_shift) not in (int,float) or not math.isfinite(max_tempo_shift) or not 0 <= max_tempo_shift <= 8:
                raise ValueError('Choose a tempo adjustment limit from 0 to 8%.')
            if not isinstance(mix_style, str) or mix_style not in MIX_STYLES:
                raise ValueError('Choose phrase or quick mixing.')
            if quick_blend_seconds is not None:
                if (mix_style!='quick' or type(quick_blend_seconds) is not int or quick_blend_seconds not in (15,20,30)
                        or seconds<quick_blend_seconds+16):
                    raise ValueError('Choose a 15, 20 or 30 second quick blend with time to prepare the next deck.')
            if mix_style == 'phrase' and excerpt_bars % 16:
                raise ValueError('Phrase mixing needs an excerpt at 0, 16 or 32 bars.')
            order = validate_order(ids, self.state['candidates'], max_tempo_shift, seconds, tempo_targets,
                                   frozen=_tempo_route,opening=_tempo_route[0]['fromBpm'] if _tempo_route else None,
                                   fixed=mix_style=='quick')
            if source=='planner':self.require_available(ids)
            policy=self.state.get('setlist',{'allowReorder':True,'lockedIds':[],'sourceById':{}})
            previous=[t['id'] for t in self.state['order']]
            if source not in ('planner','operator'):
                raise ValueError('Unknown setlist source.')
            if source=='planner' and not policy['allowReorder'] and previous and ids!=previous:
                raise ValueError('Planner reordering is off. Enable it in Setlist or move tracks manually.')
            if any(i>=len(ids) or ids[i]!=track for i,track in enumerate(previous) if track in policy['lockedIds']):
                raise ValueError('A locked track must keep its setlist position. Unlock it before changing the order.')
            sources={t['id']:policy['sourceById'].get(t['id'],'planner') for t in order}
            if source=='operator':
                sources.update({track:'operator' for i,track in enumerate(ids) if i>=len(previous) or previous[i]!=track})
            if _archive_boundary:self.archive_call('boundary')
            self.state.update(order=order, recoveryRemainder=[], secondsPerTrack=seconds, excerptBars=excerpt_bars,
                              setlist={**policy,'sourceById':sources},
                              maxTempoShift=max_tempo_shift, mixStyle=mix_style, quickBlendSeconds=quick_blend_seconds, quickTargetBpm=None,
                              **MIX_STYLES[mix_style],
                              phase='ready', index=-1, segmentProgress=0,
                              message=('Order ready. Analyze the set and apply a measured mix plan before starting.' if mix_style=='phrase' else 'Quick preview ready. Start when you want to listen.'), transitions=[], transitionControl=None,
                              analysis=None, aiReview=None, executionPlan=None, preparationSelection=None, queuedUpdate=None, lastQueueApplication=None, lastQueueRepair=None, startedAt=None, endedAt=None, startup=None,lastError=None,
                              tempoTargets=copy.deepcopy(tempo_targets),tempoControl=None,backgroundAnalysis=None,rollingQueue=None,
                              mixPlans=[transition_recipe(a,b,mix_style,seconds,order[0]['bpm']) for a,b in zip(order,order[1:])])
            self.event(f'{"Operator reordered" if source=="operator" else "Planner ordered"} {len(order)} tracks. Prepare audio to choose measured cues.', 'plan')
            return self.snapshot()

    def discover_more(self, query, artist='', genre='', bpm_min=40, bpm_max=300, recheck_missing=False,
                      external_request_id=None,authorize=None,finish=None):
        terms=search_terms(query)
        if not isinstance(artist,str) or len(artist)>120 or not isinstance(genre,str) or len(genre)>80:
            raise ValueError('Invalid catalog filters.')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (bpm_min,bpm_max)) or not 40<=bpm_min<=bpm_max<=300:
            raise ValueError('Choose a tempo range from 40 to 300 BPM.')
        with self.lock:
            if self.catalog_job and not self.catalog_job.done:raise ValueError('A crate search is already in progress.')
            job=CatalogSearch(self,terms,artist,genre,bpm_min,bpm_max,recheck_missing=recheck_missing,
                              external_request_id=external_request_id,authorize=authorize,finish=finish)
            if self.state['active']:
                if self.state['phase'] not in ('playing','mixing','loading'):
                    raise ValueError('Wait for current preparation to finish.')
                self.catalog_job=job;job.publish('queued','Crate search queued for a safe gap between transitions.')
                return self.snapshot()
            previous_phase=self.state['phase']
            if external_request_id:job.publish('queued','Crate search is awaiting the native worker.')
            def work(client):
                self.catalog_job=job
                try:
                    while not job.done:
                        job.step(client)
                        # This worker owns the bridge during a paused search.
                        # Keep real deck readings fresh so Catalog never moves
                        # the current record back into the queue mid-search.
                        reading=self.snapshot().get('telemetry')
                        if not reading or time.time()-reading.get('sampledAt',0) >= .1:
                            self.telemetry(client)
                        if not job.done:self.wait_observed(client,.1)
                except (BridgeError,ValueError,OSError) as exc:
                    job.done=True;job.publish('error',str(exc));self.event(str(exc),'catalog')
                self.update(phase=previous_phase if self.snapshot()['order'] else 'catalog-ready',
                            message=self.snapshot()['catalogSearch']['message'])
            self._launch(work,'discovering')
            return self.snapshot()

    def dig_interrupted(self):
        """Another job changed the browser's search; the dig must search again."""
        self._dig_resume=True

    def service_dig(self,client,seconds_available):
        """Autonomous crate digging, one browser step, only when nothing else needs the browser.

        The planner's own search, spare-deck analysis and the approach to a
        handoff all come first. Digging is metadata only and never changes the
        working set, the queue or playback.
        """
        job=self.dig_job
        try:
            # The operator's switch: discovery off, Mixing only or Copilot off
            # all mean the engine does not browse the catalog on its own.
            if not self.dig_allowed or dj_settings.lead(self.state)!='all':
                if job and not job.done:job.stop('off','Digging is off.')
                return
        except Exception:return
        if seconds_available<16 or self.background_job or (self.catalog_job and not self.catalog_job.done):
            if self.catalog_job and not self.catalog_job.done:self._dig_resume=True
            return
        if job is None or job.done:
            # A finished dig starts again once the crate has new names to follow.
            if self._dig_ended is not None and self.dig_clock()-self._dig_ended<60:return
            from afterhours.vdj_discovery import CrateDig
            job=self.dig_job=CrateDig(self,clock=self.dig_clock)
        try:
            if self._dig_resume:
                job.resume_search();self._dig_resume=False
            job.step(client)
        except Exception:
            # step() contains its own faults; this is the last line of defence.
            job.done=True
        if job.done:self._dig_ended=self.dig_clock()

    def preparation_decks(self):
        """Decks track preparation may use now: never the playing or cued one.

        A two-deck set keeps C and D. A four-deck set uses the decks outside
        the playing pair, the one needed last first.
        """
        if len(self.decks)==2 or not self.pair_decks:return (3,4)
        return tuple(reversed(deck_rotation.spares(*self.pair_decks,self.decks)))

    def release_preparation(self, client, deck):
        """The set needs this deck: end any preparation on it and put its record back in line."""
        job=self.background_job
        if job is not None and hasattr(job,'release'):job.release(client,deck)

    def choose_rotation(self, client):
        """Four decks in rotation when the layout and the tools allow it; otherwise A and B.

        Needs a real four-deck layout, decks C and D on their usual sides of
        the crossfader, and the analysis plugin that can prepare on any
        isolated deck. Any doubt keeps the two-deck set that always worked.
        """
        if not self.four_deck_enabled:return deck_rotation.TWO
        try:
            from afterhours.background_analysis import four_deck_tools
            if not four_deck_tools():return deck_rotation.TWO
            if number(self.q(client,'get_decks'),0,99)<4:return deck_rotation.TWO
            for deck in deck_rotation.FOUR:
                if number(self.q(client,f'deck {deck} get_deck'),0,99)!=deck:return deck_rotation.TWO
                if not as_bool(self.q(client,f"deck {deck} cross_assign '{deck_rotation.side(deck)}'",'bool')):return deck_rotation.TWO
            return deck_rotation.FOUR
        except (BridgeError,OSError,ValueError):
            return deck_rotation.TWO

    def dig_restart(self):
        """A new playing loop: the browser may show anything, so the dig searches its name again."""
        self._dig_resume=True

    def service_catalog(self,client,seconds_available):
        job=self.catalog_job
        # Audio timing has priority. Metadata work never loads a deck and stops
        # well before the beat-critical handoff. It resumes after that handoff.
        if not job or job.done or seconds_available<(9 if getattr(job,'rolling',False) else 16) or self.background_job:return
        try:job.step(client)
        except (BridgeError,ValueError,OSError) as exc:
            job.done=True;job.publish('error',str(exc));self.event(str(exc),'catalog')

    def revise_queue(self,ids,reason='',mix_transitions=None,tempo_targets=None,preserve_prefix=0,cue_choices=None, *, source=None):
        """Stage a future revision without touching the playing/cued prefix."""
        with self.lock:
            s=self.snapshot()
            if source not in (None,'operator','planner'):
                raise ValueError('Unknown setlist source.')
            if type(preserve_prefix) is not int or not 0<=preserve_prefix<=len(s['order']):
                raise ValueError('Invalid protected queue prefix.')
            audible=s['active'] or any(d.get('playing') for d in (s.get('telemetry') or {}).get('decks',[]))
            protected=max(preserve_prefix,min(len(s['order']),max(0,s['index'])+2) if audible else 0)
            if isinstance(ids,list) and ids[:protected]!=[t['id'] for t in s['order'][:protected]]:
                raise ValueError('The current and cued tracks are protected.')
            if isinstance(ids,list):self.require_available(ids[protected:])
            old_plan=s.get('executionPlan') or {}
            frozen=(old_plan.get('tempoSchedule') or [])[:protected]
            quick=s['mixStyle']=='quick'
            opening=old_plan.get('targetBpm')
            if quick and s['order']:opening=s.get('quickTargetBpm') or s['order'][0]['bpm']
            order=validate_order(ids,s['candidates'],s.get('maxTempoShift',8),s['secondsPerTrack'],tempo_targets,
                                 frozen or None,opening if audible else None,fixed=quick,
                                 history_prefix=[t['id'] for t in s['order'][:max(0,s['index'])]] if audible else [])
            keep_prepared=preserve_prefix>0 and old_plan.get('applied')
            if not audible and not keep_prepared:
                return self.plan(ids,s['secondsPerTrack'],s['excerptBars'],s.get('maxTempoShift',8),s['mixStyle'],source=source or 'planner',tempo_targets=tempo_targets,quick_blend_seconds=s.get('quickBlendSeconds'))
            if audible and s['phase'] not in ('playing','mixing','loading'):
                raise ValueError('Manual playback keeps control. Stop or return to the scheduled set before applying a new queue.')
            if not quick and not old_plan.get('applied'):
                raise ValueError('Live queue expansion needs a measured mix plan. Prepare the set before starting an autonomous run.')
            old=[t['id'] for t in s['order']]
            policy=s.get('setlist',{})
            if not policy.get('allowReorder',True) and ids!=old:raise ValueError('Planner reordering is off.')
            if any(i>=len(ids) or ids[i]!=tid for i,tid in enumerate(old) if tid in policy.get('lockedIds',[])):
                raise ValueError('A locked track must keep its position.')
            prepared=cue_planner.revise_prepared_queue(s,order,mix_transitions,reason,protected,tempo_targets,cue_choices=cue_choices) if mix_transitions and not quick else None
            self.state['queuedUpdate']={'id':uuid.uuid4().hex,'tracks':order,'protectedIds':old[:protected],
                'status':'ready' if prepared or quick else 'needs-analysis','reason':reason,'prepared':prepared,
                **({'quick':True} if quick else {}),
                **({'sourceById':{tid:source for tid in ids if tid not in old}} if source else {}),
                'mixTransitions':copy.deepcopy(mix_transitions),'tempoTargets':copy.deepcopy(tempo_targets),
                'cueChoices':copy.deepcopy(cue_choices) if cue_choices else None,
                'standingStyle':copy.deepcopy(getattr(self,'standing_style',None))}
            entries=len(((prepared or {}).get('cueChoices') or {}).get('applied',[]))
            self.state['message']=('Upcoming queue updated. The prepared change will join after the current handoff.'+
                (f" The DJ chose where {entries} record{'s' if entries!=1 else ''} come{'s' if entries==1 else ''} in." if entries else '') if prepared else
                'Upcoming tracks queued for background analysis. The playing and cued tracks are unchanged.')
            if quick:
                self.state['message']='Upcoming tracks queued at the set tempo. The playing and cued tracks are unchanged.'
            self.event(self.state['message'],'queue')
            return self.snapshot()

    def service_background(self,client,seconds_available):
        """One bounded analysis slice between clock reads, never during a mix."""
        q=self.snapshot().get('queuedUpdate');job=self.background_job
        if job and (not q or job.revision!=q['id']):
            if seconds_available<12:return
            self.analysis_cache.update(job.cache)
            try:job.close(client)
            except (BridgeError,OSError,ValueError):
                self.remember_cleanup_failure('background',job)
                raise
            self.background_job=None;job=None
        if not q or q.get('quick') or q['status'] not in ('needs-analysis','analyzing','ready'):return
        restriction=preview_failure(self.availability,q['tracks'])
        if (restriction and job and job.error
                and preview_failure(self.availability,q['tracks'],job.error) is None):
            # A separately known preview cannot turn an uncertain Load/decode
            # failure into permission to replay the other pending operations.
            restriction=None
        if restriction:
            # Only the sole worker can retire an existing analysis owner. A
            # failed cleanup still latches and stops scheduling before repair.
            if job:
                if seconds_available<12:return
                self.analysis_cache.update(job.cache)
                try:job.close(client)
                except (BridgeError,OSError,ValueError):
                    self.remember_cleanup_failure('background',job);raise
                self.background_job=None;job=None
            failure=restriction
            try:
                with self.lock:
                    self.check()
                    s=self.snapshot();latest=s.get('queuedUpdate')
                    if not latest or latest['id']!=q['id']:return
                    # A continuation belongs to the explicit running set.
                    # Stopped/manual/off states cannot acquire this authority.
                    if (not s.get('active') or s.get('phase')!='playing' or s.get('resumeBlocked')
                            or s.get('copilot',{}).get('mode')=='off'):
                        raise QueueEligibilityHeld(failure)
                    protected=min(len(s['order']),max(len(q.get('protectedIds',[])),max(0,s['index'])+2))
                    if [t['id'] for t in q['tracks'][:protected]]!=[t['id'] for t in s['order'][:protected]]:
                        raise QueueEligibilityHeld({**failure,'reason':failure['reason']+' The prepared queue prefix changed; no repair was applied.'})
                    omission=preview_tail(self.availability,{**s,'tempoTargets':q.get('tempoTargets')},q['tracks'],protected)
                    if omission is None:return
                    self.revise_queue(omission['retainedIds'],reason=skipped_message(omission['skipped']),
                                      tempo_targets=omission['tempoTargets'],preserve_prefix=protected)
                    revised=self.state['queuedUpdate']
                    revised.update(sourceById={tid:source for tid,source in q.get('sourceById',{}).items() if tid in omission['retainedIds']},
                        skipped=copy.deepcopy(q.get('skipped',[]))+copy.deepcopy(omission['skipped']),
                        standingStyle=copy.deepcopy(q.get('standingStyle',getattr(self,'standing_style',None))))
                    if q.get('startId'):revised['startId']=q['startId']
                    receipt={'id':uuid.uuid4().hex,'queueRevision':revised['id'],'previousRevision':q['id'],
                        'requestedIds':[t['id'] for t in q['tracks']],'retainedIds':omission['retainedIds'],
                        'skipped':copy.deepcopy(omission['skipped']),'at':time.time(),'basis':'known-provider-preview-only'}
                    self.state['lastQueueRepair']=receipt
                    self.update(backgroundAnalysis={'revision':revised['id'],'status':'queued',
                        'message':skipped_message(omission['skipped']),'completed':0,'total':len(revised['tracks'])-protected,
                        'decks':[],'operations':0,'audioSaved':False})
                    self.event(skipped_message(omission['skipped']),'queue-skipped',skipped=omission['skipped'])
                    return  # Preparation resumes in the next bounded slice.
            except QueueEligibilityHeld as exc:failure=exc.failure
            except (BridgeError,ValueError,OSError) as exc:
                failure={**restriction,'reason':restriction['reason']+' The remaining queue could not be fitted: '+str(exc)}
            with self.lock:
                if (self.state.get('queuedUpdate') or {}).get('id')==q['id']:
                    self.state['queuedUpdate'].update(status='blocked',error=failure['reason'],failure=copy.deepcopy(failure))
            self.update(backgroundAnalysis={'revision':q['id'],'status':'blocked','message':failure['reason'],
                        'failure':copy.deepcopy(failure),'completed':0,'total':0,'decks':[],'operations':0,'audioSaved':False})
            return
        if q['status']=='ready':return
        if not job:
            if seconds_available<12:return
            s=self.snapshot();existing={t['id'] for t in (s.get('analysis') or {}).get('tracks',[])}
            todo=[t for t in q['tracks'] if t['id'] not in existing]
            self.background_job=job=BackgroundAnalysis(self,q['id'],todo,self.analysis_cache)
            with self.lock:self.state['queuedUpdate']['status']='analyzing'
            self.event(f'Preparing {len(todo)} upcoming recordings on isolated decks C/D.','analysis')
        previous=(self.snapshot().get('backgroundAnalysis') or {}).get('message')
        try:job.step(client,seconds_available)
        except (BridgeError,ValueError,OSError) as exc:job.error=str(exc);job.done=True
        progress=job.snapshot();self.update(backgroundAnalysis=progress)
        if progress['message']!=previous:self.event(progress['message'],'analysis')
        if not job.done or seconds_available<12:return
        self.analysis_cache.update(job.cache)
        cleanup_failed=False
        try:
            try:job.close(client)
            except (BridgeError,OSError,ValueError):
                cleanup_failed=True
                self.remember_cleanup_failure('background',job)
                raise
            finally:
                # The deck ownership displayed before cleanup is stale after
                # a successful unload, including when the queue itself failed.
                progress=job.snapshot()
            if job.error:raise BridgeError(job.error)
            with self.lock:
                s=self.snapshot();q=s.get('queuedUpdate')
                if not q or q['id']!=job.revision:return
                protected=min(len(s['order']),max(len(q['protectedIds']),max(0,s['index'])+2))
                observed={t['id']:t for t in job.result}
                for i,t in enumerate(q['tracks']):
                    if i>=protected and t['id'] in observed:
                        reading=observed[t['id']]
                        key,camelot=musical_key(reading.get('camelot'))
                        t.update(bpm=reading['bpm'],key=key,camelot=camelot,engineChecked=True)
                        if reading.get('duration') is not None:t['duration']=reading['duration']
                prepared=cue_planner.complete_background_queue(s,q['tracks'],job.result,q['reason'],protected,
                    q.get('mixTransitions'),q.get('tempoTargets'),standing_style=q.get('standingStyle'),cue_choices=q.get('cueChoices'))
                cue_planner.verify_receipts(prepared['analysis'])
                # snapshot() owns a deep copy. Publish its checked recording
                # facts with the verified plan, or adoption keeps stale catalog
                # tempo/key and the incoming-deck guard correctly stops playback.
                self.state['queuedUpdate'].update(status='ready',prepared=prepared,tracks=copy.deepcopy(q['tracks']))
                self.state['queuedUpdate'].pop('error',None);self.state['queuedUpdate'].pop('failure',None)
                checked={t['id']:t for t in q['tracks']}
                self.state['candidates']=[copy.deepcopy(checked.get(t['id'],t)) for t in self.state['candidates']]
                self.event('Upcoming tracks verified. Local measured cue fitting is ready for the next safe handoff; no extra AI request.','queue-ready')
                self.update(backgroundAnalysis={**progress,'status':'ready','message':'Upcoming tracks ready for a safe handoff.'})
        except (BridgeError,ValueError,OSError) as exc:
            with self.lock:
                if self.state.get('queuedUpdate') and self.state['queuedUpdate']['id']==job.revision:
                    failure=preview_failure(self.availability,self.state['queuedUpdate']['tracks'],str(exc))
                    self.state['queuedUpdate'].update(status='blocked',error=str(exc))
                    self.state['queuedUpdate'].pop('failure',None)
                    if failure:self.state['queuedUpdate']['failure']=failure
            self.update(backgroundAnalysis={**progress,'status':'blocked','message':str(exc)})
            self.event('Background preparation held: '+str(exc),'analysis')
            if cleanup_failed:raise  # Stop scheduling; keep the already playing track untouched.
        finally:self.background_job=None

    def retry_background(self):
        with self.lock:
            q=self.state.get('queuedUpdate')
            if not q or q['status']!='blocked' or not self.state['active']:
                raise ValueError('There is no blocked background job in the running set. Use Prepare set while paused.')
            q.update(id=uuid.uuid4().hex,status='needs-analysis');q.pop('error',None);q.pop('failure',None)
            self.event('Background analysis retry requested. Playback keeps priority.','analysis')
            return self.snapshot()

    def adopt_queue(self,index):
        """Called only by the engine worker after a handoff and before preload."""
        with self.lock:
            q=self.state.get('queuedUpdate')
            if not q or q['status']!='ready':return None
            old=self.state['order'];order=q['tracks'];protected=max(index+1,len(q['protectedIds']))
            try:
                for track in order[protected:]:self.availability.require_track(track)
            except ProviderPreviewRestricted as exc:
                q.update(status='blocked',error=str(exc));q.pop('failure',None)
                failure=preview_failure(self.availability,order[protected:],str(exc))
                if failure:q['failure']=failure
                return None
            if [t['id'] for t in order[:protected]]!=[t['id'] for t in old[:protected]]:
                q['status']='needs-review';return None
            if q.get('quick'):
                if self.state['mixStyle']!='quick':
                    q['status']='needs-review';return None
                track_ids=[t['id'] for t in order]
                sources={**self.state['setlist'].get('sourceById',{}),**q.get('sourceById',{})}
                self.state['setlist']['sourceById']={tid:sources.get(tid,'operator') for tid in track_ids}
                self.update(order=copy.deepcopy(order),queuedUpdate=None,
                            lastQueueApplication={'queueRevision':q['id'],'planId':None,
                                                  'trackIds':track_ids,'appliedAt':time.time()})
                self.event('Quick queue revision adopted. Playing cue preserved.','queue-applied')
                return copy.deepcopy(order),None
            prepared=copy.deepcopy(q['prepared']);cue_planner.verify_receipts(prepared['analysis'])
            # Which of the planner's window choices were used is a receipt, not set state.
            windows=prepared.pop('cueChoices',None)
            plan=prepared['executionPlan'];plan['applied']=True
            sources={**self.state['setlist'].get('sourceById',{}),**q.get('sourceById',{})}
            self.state['setlist']['sourceById']={t['id']:sources.get(t['id'],'planner') for t in order}
            self.state.update(order=copy.deepcopy(order),**prepared,mixPlans=plan['transitions'],queuedUpdate=None,
                              lastQueueApplication={'queueRevision':q['id'],'planId':plan.get('id'),
                                                    'trackIds':[t['id'] for t in order],'appliedAt':time.time(),
                                                    **({'cueChoices':windows} if windows else {})},
                              tempoTargets=copy.deepcopy(prepared['analysis'].get('tempoTargets')))
            self.event('Prepared queue revision adopted at the handoff. Playing cue preserved.','queue-applied')
            return copy.deepcopy(order),copy.deepcopy(plan)

    def edit_setlist(self, operation, track_id=None, value=None, ids=None):
        """Edit preparation state only; never issue audio commands."""
        with self.lock:
            if self.state['active'] or any(d.get('playing') for d in (self.state.get('telemetry') or {}).get('decks',[])):
                raise ValueError('Stop the set before editing its order or locks.')
            policy=copy.deepcopy(self.state.get('setlist',{'allowReorder':True,'lockedIds':[],'sourceById':{}}))
            current=[t['id'] for t in self.state['order']]
            if operation=='allow-reorder':
                if type(value) is not bool:raise ValueError('Reorder preference must be on or off.')
                policy['allowReorder']=value
                message='Planner reordering is on.' if value else 'Planner reordering is off. Manual moves are still available.'
            elif operation=='lock':
                if track_id not in current or type(value) is not bool:raise ValueError('Choose an existing track and a lock state.')
                locked=set(policy['lockedIds']);locked.add(track_id) if value else locked.discard(track_id)
                policy['lockedIds']=[i for i in current if i in locked]
                message='Track position locked.' if value else 'Track position unlocked.'
            elif operation=='reorder':
                if not isinstance(ids,list) or len(ids)!=len(current) or any(not isinstance(i,str) for i in ids) or set(ids)!=set(current):
                    raise ValueError('Manual reordering must preserve every track exactly once.')
                result=self.plan(ids,self.state['secondsPerTrack'],self.state['excerptBars'],self.state.get('maxTempoShift',8),self.state['mixStyle'],source='operator',quick_blend_seconds=self.state.get('quickBlendSeconds'))
                self.update(message='Setlist reordered. Analyze this order again before planning the mix.')
                return self.snapshot()
            else:raise ValueError('Unknown setlist edit.')
            self.update(setlist=policy,message=message)
            self.event(message,'setlist')
            return self.snapshot()

    def set_reorder_preference(self, value):
        """Future planner policy only. Never rewrite an accepted audio plan."""
        if type(value) is not bool:raise ValueError('Reorder preference must be on or off.')
        with self.lock:
            policy=copy.deepcopy(self.state.get('setlist',{'allowReorder':True,'lockedIds':[],'sourceById':{}}))
            policy['allowReorder']=value
            self.update(setlist=policy)
            return self.snapshot()

    def prepare_analysis(self):
        """Preload and verify full-track numerical features while paused."""
        snapshot=self.snapshot()
        if snapshot['mixStyle']!='phrase':
            raise ValueError('Choose genre-aware EQ mixing to prepare measured cue candidates.')
        order=validate_order([t['id'] for t in snapshot['order']],snapshot['candidates'],snapshot.get('maxTempoShift',8),snapshot['secondsPerTrack'],snapshot.get('tempoTargets'))
        def work(client):
            before=client.status()
            if before['automix'] or any(d['playing'] for d in before['decks']):
                raise BridgeError('Pause both decks and turn Automix off before preparing analysis.')
            self.update(analysis=None,aiReview=None,executionPlan=None,preparationSelection=None)
            checked=[];candidates={};skipped=[]
            def omit(track,exc,code):
                if code!=PREVIEW_ONLY:self.availability.record(track['id'],code,str(exc))
                if not snapshot.get('setlist',{}).get('allowReorder',True) or snapshot.get('setlist',{}).get('lockedIds'):
                    raise BridgeError(str(exc)+' The locked order is unchanged.')
                skipped.append({'trackId':track['id'],'reason':str(exc),'code':code})
                self.event(f'Skipped {track["title"]}: '+str(exc),'unavailable',trackId=track['id'])
            for i,track in enumerate(order):
                self.update(message=f'Analyzing {i+1} of {len(order)} · {track["title"]}. Audio stays paused.')
                try:checked_track = self.analyze_deck(client,track,1)
                except ProviderPreviewRestricted as exc:
                    omit(track,exc,PREVIEW_ONLY);continue
                except RecordingUnavailable as exc:
                    omit(track,exc,'recording-unavailable');continue
                evidence = track_analysis([checked_track])['tracks'][0]
                waveform_ready = bool(evidence.get('energyMap')) and verified_waveform(
                    track['engine']['selection_id'],evidence['duration'],evidence['energyMap']['reportDigest'])
                if not waveform_ready:
                    # A new decode can differ from a previously verified buffer.
                    # Refresh both maps together rather than comparing a new
                    # waveform against stale features for the entire timeout.
                    self.update(message=f'Scanning audio {i+1} of {len(order)} · {track["title"]}. Checking complete decoding.')
                    sound_end = next((p['seconds'] for p in evidence['points'] if p['label']=='realEnd'), None)
                    scan_track(client, checked_track, sound_end, check=self.check, wait=self.sleep)
                    evidence=track_analysis([checked_track])['tracks'][0]
                    self.update(message=f'Preparing detailed waveform · {track["title"]}. Audio stays paused.')
                    scan_waveform(client,checked_track,evidence,check=self.check,wait=self.sleep)
                self.update(message=f'Mapping musical entry and exit candidates · {track["title"]}. Audio stays paused.')
                try:
                    candidates[track['id']]=self.measure_cues(client,checked_track,evidence,
                        checked[0]['bpm'] if checked else checked_track['bpm'],snapshot['secondsPerTrack'])
                except cue_planner.LimitedAudio as exc:
                    omit(track,exc,'limited-decoded-audio');continue
                checked.append(checked_track)
                self.availability.verified(track['id'])
            if len(checked)<2:
                raise BridgeError('Fewer than two full recordings are available for this mix. '+ ' '.join(t['reason'] for t in skipped))
            targets=snapshot.get('tempoTargets')
            if skipped and targets:
                targets=[r for r in targets if r['trackId'] in {t['id'] for t in checked}]
            validate_order([t['id'] for t in checked],checked,snapshot.get('maxTempoShift',8),snapshot['secondsPerTrack'],targets)
            report=track_analysis(checked)
            original={t['id']:t for t in order}
            if any(abs(a['bpm']/original[a['id']]['bpm']-1)>.02 or a.get('camelot')!=original[a['id']].get('camelot') for a in checked):
                # Every cue above already uses this measured metadata. The
                # filtered order has just passed the tempo-route validator, so
                # restarting the whole analysis would repeat identical work.
                self.event('Native tempo or key refined the selection. The measured order remains within its mixing limits.','analysis-metadata')
            estimate=tempo_planner.estimate(checked,snapshot['secondsPerTrack'],snapshot.get('maxTempoShift',8),targets)
            for i in range(2):self.prepare_deck(client,checked[i],i+1,estimate[i]['fromBpm'])
            self.send(client,'crossfader 0%')
            self.telemetry(client)
            report=cue_planner.attach_candidates(report,candidates,snapshot['secondsPerTrack'],checked[0]['bpm'],targets,snapshot.get('maxTempoShift',8))
            count=sum(len(t['cueCandidates']) for t in report['tracks'])
            selection={'fromIds':[t['id'] for t in order],'orderIds':[t['id'] for t in checked],'skipped':skipped} if skipped else None
            policy=snapshot.get('setlist',{'allowReorder':True,'lockedIds':[],'sourceById':{}})
            policy={**policy,'sourceById':{tid:source for tid,source in policy.get('sourceById',{}).items() if tid in {t['id'] for t in checked}}}
            self.update(order=checked,setlist=policy,tempoTargets=targets,preparationSelection=selection,analysis=report,phase='ready',
                        message=f'{count} measured cue candidates prepared. Plan the transitions before starting.')
            self.event(f'Prepared {count} audio-derived cue candidates with native position readbacks. Musical phrases remain estimates.','analysis')
        self._launch(work,'analyzing')
        return self.snapshot()

    def prepare_waveforms(self):
        """Refresh display envelopes without replacing paid analysis or its cues."""
        snapshot=self.snapshot();report=snapshot.get('analysis')
        if not report or [t['id'] for t in report['tracks']]!=[t['id'] for t in snapshot['order']]:
            raise ValueError('Analyze this exact set before preparing detailed waveforms.')
        cue_planner.verify_receipts(report)
        plan=(cue_planner.compile_plan(snapshot['aiReview'],report,snapshot['order'],snapshot['secondsPerTrack'],snapshot['mixStyle'])
              if snapshot.get('aiReview') else None)
        def work(client):
            before=client.status()
            if before['automix'] or any(d['playing'] for d in before['decks']):
                raise BridgeError('Pause both decks and turn Automix off before preparing waveforms.')
            self.update(executionPlan=None)
            for i,(track,evidence) in enumerate(zip(snapshot['order'],report['tracks'])):
                self.check()
                if self.waveform(track['id']):continue
                self.update(message=f'Preparing waveform {i+1} of {len(snapshot["order"])} · {track["title"]}. Audio stays paused.')
                checked=self.analyze_deck(client,track,1)
                scan_waveform(client,checked,evidence,check=self.check,wait=self.sleep)
            cue_planner.verify_receipts(report)
            target=plan['targetBpm'] if plan else snapshot['order'][0]['bpm']
            for i in range(2):
                bpm=plan['tempoSchedule'][i]['fromBpm'] if plan and plan.get('tempoSchedule') else target
                self.prepare_deck(client,snapshot['order'][i],i+1,bpm,cue=plan['selections'][i] if plan else None)
            self.send(client,'crossfader 0%');self.telemetry(client)
            if plan:plan['applied']=True
            self.update(executionPlan=plan,phase='ready',index=-1,startedAt=None,endedAt=None,transitions=[],
                        activeHandoff=None,transitionControl=None,segmentProgress=0,
                        message='Detailed waveforms prepared. Opening decks are paused; the existing mix plan is preserved.')
            if plan:self.update(mixPlans=plan['transitions'],aiReview={**snapshot['aiReview'],'appliedToPlayback':True})
            self.event('Prepared verified 20 ms waveforms for the set. No new AI request or audio playback.','waveforms')
        self._launch(work,'analyzing')
        return self.snapshot()

    def review_mix(self,approved=False,authorize=None,confirm_authorization=None):
        if approved is not True:raise ValueError('Approve one bounded AI request before reviewing this set.')
        with self.lock:
            snapshot=self.snapshot();report=snapshot.get('analysis')
            standing_style=copy.deepcopy(getattr(self,'standing_style',None))
        if not report or [t['id'] for t in report['tracks']] != [t['id'] for t in snapshot['order']]:
            raise ValueError('Prepare native analysis for this exact set before requesting AI review.')
        def work(client):
            before=client.status()
            if before['automix'] or any(d['playing'] for d in before['decks']):
                raise BridgeError('Pause both decks before AI review; no request was sent.')
            self.update(aiReview=None,message='AI is reviewing the observed mix points and musical risks. Audio stays paused.')
            if report.get('cueVersion'):cue_planner.verify_receipts(report)
            if authorize:authorize()
            result=(ai_mix.plan(report,snapshot['secondsPerTrack'],approved=True,direction=snapshot.get('agentDirection'),standing_style=standing_style)
                    if report.get('cueVersion') else ai_mix.review(report,snapshot['secondsPerTrack'],approved=True))
            self.check()
            if confirm_authorization:confirm_authorization()
            self.update(aiReview=result,executionPlan=None,phase='ready',message='AI plan ready to inspect and apply.' if result.get('decision')=='propose' else 'AI review complete. Check its findings before another trial.')
            self.event('AI plan saved. Apply it to prepare the selected cues; audio remains paused.','ai-review')
        self._launch(work,'reviewing')
        return self.snapshot()

    def measure_cues(self,client,track,evidence,target_bpm,seconds,deck=1,isolated=None):
        isolated=deck in (3,4) if isolated is None else isolated is True
        if deck not in ((1,2,3,4) if isolated else (1,)):raise ValueError('Cue analysis uses paused deck A or an isolated spare deck.')
        def guard():
            self.check()
            if isolated:
                from afterhours.background_analysis import guard_isolation
                guard_isolation(client,deck)
            elif any(as_bool(self.q(client,f'deck {d} play','bool')) for d in (1,2)):
                raise BridgeError('A deck started manually. Cue analysis stopped without changing playback.')
            if identity(str(self.q(client,f'deck {deck} get_filepath','text')))!=track['engine']['selection_id']:
                raise BridgeError('The recording changed during cue analysis.')
        rows=cue_planner.measured_rows(evidence)
        unavailable=cue_planner.limited_audio(evidence,rows)
        if unavailable:raise cue_planner.LimitedAudio(unavailable)
        def at(offset, *, grid_anchor=False):
            guard()
            self.send(client,f'deck {deck} goto_first_beat')
            self.sleep(.12)
            if offset:
                self.send(client,f'deck {deck} goto +{offset}')
                self.sleep(.12)
            # VirtualDJ may place its grid origin just before the first audio
            # sample (Free Your Mind reads -2 ms). Preserve that measured
            # origin for offset selection, using the same bounded allowance
            # as native first-beat analysis. Every playable cue and its replay
            # must still read within the recording; do not clamp the grid.
            return (number(self.q(client,f'deck {deck} get_beatpos'),-10000,100000),
                    number(self.q(client,f'deck {deck} get_time elapsed absolute'),
                           -6000 if grid_anchor else 0,1200000)/1000)
        anchor,anchor_time=at(0,grid_anchor=True)
        policy=cue_planner.propose_offsets(evidence,rows,anchor_time,track['bpm'],target_bpm,seconds)
        result=[]
        for offset in policy['offsets']:
            entry,entry_time=at(offset)
            intro,intro_time=at(offset+policy['blendBeats'])
            exit_beat,exit_time=at(offset+policy['spanBeats'])
            end,end_time=at(offset+policy['spanBeats']+policy['blendBeats'])
            # A fluid grid's absolute beat numbering can shift across tempo
            # regions. Validate the measured span, not a global offset guess.
            if (abs((intro-entry)-policy['blendBeats'])>.2 or
                    abs((exit_beat-entry)-policy['spanBeats'])>.2 or
                    abs((end-exit_beat)-policy['blendBeats'])>.2):
                self.event('Candidate crosses a changing native beat span; skipped.', 'analysis',
                           trackId=track['id'], observedBeats=[entry,intro,exit_beat,end])
                continue
            native={'anchorBeat':anchor,'offsetBeats':offset,'entryBeat':entry,'entrySeconds':entry_time,
                    'introEndSeconds':intro_time,'exitBeat':exit_beat,'exitSeconds':exit_time,
                    'endBeat':end,'endSeconds':end_time}
            try:cue=cue_planner.candidate(evidence,rows,native,policy)
            except ValueError:continue  # Fluid-grid readback may invalidate an estimated window.
            # Prove that execution can reproduce the absolute cue before the
            # candidate is offered to the planner. Both decks remain paused.
            guard()
            self.send(client,f"deck {deck} goto {cue['entrySeconds']/cue['recordingDuration']*100:.9f}%")
            self.sleep(.12)
            replay_beat=number(self.q(client,f'deck {deck} get_beatpos'),-10000,100000)
            replay_time=number(self.q(client,f'deck {deck} get_time elapsed absolute'),0,1200000)/1000
            if abs(replay_beat-entry)>.2 or abs(replay_time-entry_time)>.2:
                self.event('Absolute cue could not be reproduced; skipped.','analysis',trackId=track['id'])
                continue
            cue['replayVerified']=True
            result.append(cue)
        if not result:
            raise BridgeError(f'No usable measured excerpt was found for {track["title"]}. Choose another track or a longer interval.')
        return result

    def apply_plan(self):
        snapshot=self.snapshot();report=snapshot.get('analysis');review=snapshot.get('aiReview')
        if not report or not review:raise ValueError('Prepare audio and request an AI plan first.')
        plan=cue_planner.compile_plan(review,report,snapshot['order'],snapshot['secondsPerTrack'],snapshot['mixStyle'])
        def work(client):
            before=client.status()
            if before['automix'] or any(d['playing'] for d in before['decks']):
                raise BridgeError('Pause both decks before applying the mix plan.')
            cue_planner.verify_receipts(report)
            self.update(executionPlan=None,message='Preparing the AI-selected opening cues. Audio stays paused.')
            for i in range(2):
                target=plan['tempoSchedule'][i]['fromBpm'] if plan.get('tempoSchedule') else plan['targetBpm']
                self.prepare_deck(client,snapshot['order'][i],i+1,target,cue=plan['selections'][i])
            self.send(client,'crossfader 0%');self.telemetry(client)
            plan['applied']=True
            self.update(executionPlan=plan,mixPlans=plan['transitions'],
                        aiReview={**review,'appliedToPlayback':True},phase='ready',
                        message='Analyzed mix plan applied. Selected opening cues are paused and ready.')
            self.event('AI-selected cues and transition recipes applied to the reusable mixer. Playback has not started.','plan-applied')
        self._launch(work,'preparing')
        return self.snapshot()

    def select(self, client, track):
        if self.background_job:self.background_job.invalidate_catalog()
        if folder_state(client)['id'] != self.folder['id']:
            raise BridgeError('Catalog folder changed. Discover music again before starting.')
        context = self.positions[track['id']]
        if self.catalog_job:self.catalog_job.resume_search()
        self._dig_resume=True
        # Re-run the saved search; another view/operator may have changed it.
        # Exact identity is still required before the guarded load command.
        self.send(client, 'search ' + search_literal(context['query']))
        self.wait_observed(client, 1.5)
        position = context['position']
        self.send(client, "browser_window 'songs' & browser_scroll 'top'")
        if position:
            self.send(client, f'browser_scroll +{position}')
        self.sleep(.1)
        try:
            selected = selection(client)
        except BridgeError:
            selected = None
        if selected and selected['id'] == track['engine']['selection_id']:
            return

        def scan(query, limit):
            # file_count is the currently displayed result count, not a proof
            # that the provider returned its complete catalog. Beatport can
            # cap a broad artist search at 100 recordings.
            if folder_state(client)['id']!=self.folder['id']:
                raise BridgeError('Catalog folder changed during recording lookup. No replacement was loaded.')
            try:visible=count_value(self.q(client,'file_count'))
            except BridgeError:visible=None
            self.send(client,"browser_window 'songs' & browser_scroll 'top'")
            self.sleep(.04)
            for position in range(min(limit,visible) if visible is not None else limit):
                self.check()
                self.service_telemetry(client)
                if folder_state(client)['id'] != self.folder['id']:
                    raise BridgeError('Catalog folder changed during recording lookup. No replacement was loaded.')
                try:
                    selected = selection(client)
                except BridgeError:
                    selected = None
                if selected and selected['id'] == track['engine']['selection_id']:
                    context.update(query=query,position=position)
                    return True
                self.send(client, 'browser_scroll +1')
                # Browser scrolling is dispatched asynchronously. Give the
                # native result selection a render tick before reading its ID.
                self.sleep(.04)
            return False

        original=context['query']
        title=re.sub(r'[\x00-\x1f\x7f"`/\\*?\[\]{}#]',' ',str(track.get('title') or ''))
        title=' '.join(title.split())[:160]
        # Keep the saved cursor's fast path, then narrow by title before a
        # costly artist walk. Versions with the same title are never substitutes:
        # only the original opaque recording identity can authorize Load.
        if title and title.casefold()!=original.casefold():
            if folder_state(client)['id']!=self.folder['id']:
                raise BridgeError('Catalog folder changed during recording lookup. No replacement was loaded.')
            self.send(client,'search '+search_literal(title))
            self.wait_observed(client,1.5)
            if scan(title,32):return
            if folder_state(client)['id']!=self.folder['id']:
                raise BridgeError('Catalog folder changed during recording lookup. No replacement was loaded.')
            self.send(client,'search '+search_literal(original))
            self.wait_observed(client,1.5)
        if scan(original,101):return
        label=' — '.join(str(track.get(key) or '').strip() for key in ('title','artist') if track.get(key)) or 'The saved recording'
        raise RecordingLookupIncomplete(f'{label} was not found in the current catalog search. Its availability is unconfirmed; no replacement was loaded.')

    def locate_for_load(self, client, track, deck):
        """Return a private exact locator, or leave the exact browser row selected.

        An origin folder is provenance, not an assertion about provider identity.
        A retained recording can have a different opaque namespace from search
        results even when both were observed under the same provider folder.
        """
        self.availability.require_track(track)
        self.check()
        if folder_state(client)['id'] != self.folder['id']:
            raise BridgeError('Catalog folder changed. Discover music again before starting.')
        sid = track['engine']['selection_id']
        attempted = False
        origin = track['engine'].get('folder_id')
        if isinstance(origin, str) and re.fullmatch(r'[a-f0-9]{64}', origin) and origin != self.folder['id']:
            target = paused_load_target(client, deck)
            recording = self.recordings.find(sid)
            attempted = True
            self.check()
            if recording:
                return recording, target
        try:
            self.select(client, track)
        except RecordingLookupIncomplete:
            if not attempted:
                target = paused_load_target(client, deck)
                recording = self.recordings.find(sid)
                self.check()
                if recording:
                    return recording, target
            raise
        return None

    def load(self, client, track, deck):
        self.availability.require_track(track)
        restored = self.locate_for_load(client, track, deck)
        # Both paths keep their one-dispatch confirmation contract. A private
        # locator never falls back to a second Load after a lost/wrong readback.
        if restored:
            recording, target = restored
            result = load_exact_recording(client, deck, recording, track['engine']['selection_id'],
                                          self.folder['id'], target, before_dispatch=lambda:(self.check(),self.availability.require_track(track)),
                                          wait=lambda seconds: self.wait_observed(client, seconds))
        else:
            self.availability.require_track(track)
            result = load_selected(client, deck, track['engine']['selection_id'], self.folder['id'], True,
                                   wait=lambda seconds: self.wait_observed(client, seconds))
        if result.get('verified'):
            return
        # Downloads may outlast the immediate command budget. Read the same
        # deck for up to 12 additional seconds; never re-send Load.
        until = time.monotonic() + 12
        while time.monotonic() < until:
            self.wait_observed(client, .25)
            try:
                if as_bool(self.q(client, f'deck {deck} deck_has_error', 'bool')):
                    raise ValueError('VirtualDJ reports a streamed-track error.')
                if (as_bool(self.q(client, f'deck {deck} loaded', 'bool')) and
                        identity(str(self.q(client, f'deck {deck} get_filepath', 'text'))) == track['engine']['selection_id']):
                    return
            except BridgeError:
                continue
        raise BridgeError('Track load remains unconfirmed. No repeat load or Play was sent.')

    def telemetry(self, client, observing=False, preloading=False):
        q = (lambda client,script,kind=None: client.query(script,kind)) if observing else self.q
        def read_deck(deck):
            loaded = as_bool(q(client, f'deck {deck} loaded', 'bool'))
            if not loaded:
                return {'deck': deck, 'loaded': False, 'playing': False}
            beat_position = number(q(client, f'deck {deck} get_beatpos'), -10000, 100000)
            phase = number(q(client, f'deck {deck} get_beat_bar'), 0, 1)
            reading = {'deck': deck, 'loaded': True,
                          'id': 'virtualdj:' + identity(str(q(client, f'deck {deck} get_filepath', 'text'))),
                          'title': q(client, f'deck {deck} get_title', 'text'),
                          'artist': q(client, f'deck {deck} get_artist', 'text'),
                          'playing': as_bool(q(client, f'deck {deck} play', 'bool')),
                          'bpm': number(q(client, f'deck {deck} get_bpm'), 40, 300),
                          # Native cue/pre-roll can precede zero (e.g. -35 ms).
                          # Preserve that signed reading within the same one-hour
                          # bound in either direction; do not invent zero or drop
                          # the entire observer snapshot for a valid cue position.
                          'position': number(q(client, f'deck {deck} get_time elapsed absolute'), -3_600_000, 3_600_000) / 1000,
                          'beatPosition': beat_position,
                          'phase': (phase * 4) % 1,
                          'level': number(q(client, f'deck {deck} get_vu_meter'), 0, 1),
                          'sync': as_bool(q(client, f'deck {deck} is_sync', 'bool')),
                          'sampledAt': time.time()}
            return reading
        def read_standby(deck):
            # A deck outside the playing pair: what is on it and whether it
            # moves. Its clock, EQ and console are not on the timing path.
            loaded = as_bool(q(client, f'deck {deck} loaded', 'bool'))
            if not loaded:
                return {'deck': deck, 'loaded': False, 'playing': False, 'standby': True}
            return {'deck': deck, 'loaded': True, 'standby': True,
                    'id': 'virtualdj:' + identity(str(q(client, f'deck {deck} get_filepath', 'text'))),
                    'title': q(client, f'deck {deck} get_title', 'text'),
                    'playing': as_bool(q(client, f'deck {deck} play', 'bool')),
                    'bpm': number(q(client, f'deck {deck} get_bpm'), 40, 300), 'sampledAt': time.time()}
        pair = self.pair_decks if self.pair_decks and len(self.decks) > 2 else None
        decks = []
        for deck in self.decks:
            try:
                decks.append(read_standby(deck) if pair and deck not in pair else read_deck(deck))
            except BridgeError:
                if not preloading:
                    raise
                decks.append({'deck': deck, 'loaded': None, 'playing': None, 'unavailable': True})
        crossfader = number(q(client, 'crossfader'), 0, 1)
        heard = [d for d in decks if deck_rotation.crossfader_end(d['deck']) == (0.0 if crossfader < .5 else 1.0)]
        known = [d for d in heard if d['deck'] == self.active_deck]
        if all(d.get('unavailable') for d in (known or heard)):
            raise BridgeError('The audible deck clock is unavailable.')
        # Keep controls visible outside transitions. The mixer already reads EQ
        # on its timing loop; avoid adding six queries to that critical path.
        mixing = self.snapshot().get('phase') == 'mixing'
        for d in decks:
            deck = d['deck']
            cached = self.eq_readings.get(deck)
            if cached and cached['trackId'] != d.get('id'):
                cached = None
            if not mixing and not preloading and not d.get('unavailable') and not d.get('standby') and (
                    not cached or time.time() - cached['sampledAt'] >= .75):
                try:
                    values = {name: number(q(client, f'deck {deck} {name}'), 0, 1)
                              for name in ('eq_high_freq', 'eq_mid_freq', 'eq_low_freq')}
                    cached = {'values': values, 'sampledAt': time.time(), 'trackId': d.get('id')}
                    self.eq_readings[deck] = cached
                except BridgeError:
                    cached = None
                    self.eq_readings.pop(deck, None)
            if cached and time.time() - cached['sampledAt'] < 3:
                d.update(eq=cached['values'], eqSampledAt=cached['sampledAt'])
        # The additional console controls are observed outside the beat-critical
        # transition loop. Each recording owns its cache, including empty decks.
        from afterhours.vdj_controls import bounded_controls, live_controls, track_id
        if not hasattr(self, 'console_readings'): self.console_readings = {}
        for d in decks:
            cached = self.console_readings.get(d['deck'])
            if cached and cached['trackId'] != d.get('id'): cached = None
            if d.get('standby'): continue
            if not preloading and not d.get('unavailable') and (
                    not cached or time.time()-cached['values']['sampledAt'] >= .5):
                values = bounded_controls(client, d['deck'], d.get('loaded') is True)
                # A load from hardware while reading cannot attach old metadata
                # or cue positions to the new recording.
                if track_id(client,d['deck']) != d.get('id'):
                    self.console_readings.pop(d['deck'],None)
                    number_ = d['deck']
                    d.clear();d.update(deck=number_,unavailable=True,loaded=None,playing=None)
                    continue
                cached={'trackId':d.get('id'),'values':values}
                self.console_readings[d['deck']]=cached
            if cached:
                d['controls']=live_controls(client,d['deck'],cached['values']) if not preloading else cached['values']
        result = {'decks': decks, 'crossfader': crossfader, 'sampledAt': time.time()}
        self.update(telemetry=result)
        return result

    def analyze_deck(self, client, track, deck, clock=None):
        self.availability.require_track(track)
        self.crate.upsert(track,source='analysis',analysis='attempted')
        clock=clock or time.monotonic
        already_loaded = as_bool(self.q(client,f'deck {deck} loaded','bool'))
        same = already_loaded and identity(str(self.q(client,f'deck {deck} get_filepath','text'))) == track['engine']['selection_id']
        if not same:
            self.load(client, track, deck)
        self.send(client,f'deck {deck} master_tempo on')
        # New streaming loads can initially expose catalog values before native
        # analysis replaces them. Require a bounded stable window, not one read.
        self.wait_observed(client, .2 if track.get('engineChecked') else 5)
        if as_bool(self.q(client,f'deck {deck} play','bool')):
            raise BridgeError('A deck started manually. Analysis stopped without seeking.')
        # Compare recordings at the same grid anchor on every preparation, not
        # at a previous playback position elsewhere in a variable-tempo song.
        self.send(client,f'deck {deck} goto_first_beat')
        self.sleep(.15)
        deadline=clock()+15
        previous=None;stable=0
        while clock()<deadline:
            bpm=number(self.q(client,f'deck {deck} get_bpm absolute'),40,300)
            key,camelot=musical_key(self.q(client,f'deck {deck} get_key','text'))
            # This is a grid anchor, not an audio seek. Native analysis may
            # place it just before sample zero (Free Your Mind: -2.494 ms).
            # Keep it bounded to one bar before zero at the minimum 40 BPM.
            first=number(self.q(client,f'deck {deck} get_firstbeat'),-6000,3_600_000)
            reading=(round(bpm,3),camelot,round(first))
            stable=stable+1 if reading==previous else 0;previous=reading
            if stable>=3:break
            self.wait_observed(client, .6)
        else:raise BridgeError('Track analysis did not settle. No playback was started.')
        if 150<=track['bpm']<=190 and 75<=bpm<=95 and abs(bpm*2/track['bpm']-1)<=.08:
            # Correct a half-time grid, not the audio playback speed. This is a
            # reversible native analysis edit; the original media is untouched.
            original=bpm
            self.send(client,f'deck {deck} set_bpm {bpm*2:.6f}')
            self.sleep(.2)
            bpm=number(self.q(client,f'deck {deck} get_bpm absolute'),40,300)
            if abs(bpm-original*2)>.05:raise BridgeError('Half-time grid correction was not confirmed.')
            self.event(f'{track["title"]}: half-time grid normalized from {original:.2f} to {bpm:.2f} BPM.','analysis')
        updated={**track,'bpm':bpm,'key':key,'camelot':camelot,'engineChecked':True}
        self.remember_record(updated,source='analysis',analysis='analyzed')
        return updated

    def prepare_deck(self, client, track, deck, target_bpm, cue=None, checked=None):
        self.availability.require_track(track)
        if checked is not None:
            if as_bool(self.q(client,f'deck {deck} play','bool')) or identity(str(self.q(client,f'deck {deck} get_filepath','text')))!=track['engine']['selection_id']:
                raise BridgeError('The checked incoming recording changed. No playback was started.')
        actual=checked if checked is not None else self.analyze_deck(client,track,deck)
        # A variable beat grid can refine the reported tempo as its playhead
        # moves. Small refinements do not invalidate the musical selection;
        # changed key, large revisions and the pitch bound still require a plan.
        if actual['camelot']!=track.get('camelot') or abs(actual['bpm']/track['bpm']-1)>.02:
            raise TrackMetadataChanged(actual)
        if abs(target_bpm/actual['bpm']-1)>self.snapshot().get('maxTempoShift',8)/100:
            raise BridgeError('The analyzed tempo exceeds the pitch limit. Rebuild the order.')
        if abs(actual['bpm']-track['bpm'])>.15:
            self.event(f'{track["title"]}: native tempo refined to {actual["bpm"]:.2f} BPM; key and pitch limits still fit.', 'analysis')
            with self.lock:
                self.state['order']=[actual if t['id']==track['id'] else t for t in self.state['order']]
        self.send(client, f'deck {deck} master_tempo on & deck {deck} goto_first_beat')
        if cue:
            # Replay the measured absolute position, not a relative seek whose
            # result can depend on the local region of a fluid grid.
            self.send(client,f"deck {deck} goto {cue['entrySeconds']/cue['recordingDuration']*100:.9f}%")
        else:
            offset=self.snapshot()['excerptBars']*4
            if offset:self.send(client,f'deck {deck} goto +{offset}')
        # Set pitch at the excerpt, not at the track's previous playhead: a
        # variable grid can have a different local BPM after seeking.
        self.sleep(.15)
        if cue:
            beat=number(self.q(client,f'deck {deck} get_beatpos'),-10000,100000)
            position=number(self.q(client,f'deck {deck} get_time elapsed absolute'),0,1200000)/1000
            if abs(beat-cue['entryBeat'])>.2 or abs(position-cue['entrySeconds'])>.2:
                raise BridgeError('The selected cue moved after analysis. Prepare and plan again; playback was not started.')
        self.match_tempo(client, track, deck, target_bpm)
        self.event(f"Deck {deck_rotation.letter(deck)} ready · {track['title']} at {target_bpm:.1f} BPM", 'loaded')

    def match_tempo(self, client, track, deck, target_bpm):
        if as_bool(self.q(client,f'deck {deck} play','bool')):
            raise BridgeError('Incoming deck started manually. Tempo was not changed.')
        if identity(str(self.q(client,f'deck {deck} get_filepath','text'))) != track['engine']['selection_id']:
            raise BridgeError('Incoming deck changed. Tempo was not changed.')
        original=number(self.q(client,f'deck {deck} get_bpm absolute'),40,300)
        if abs(target_bpm/original-1)>self.snapshot().get('maxTempoShift',8)/100:
            raise BridgeError('The local beat-grid tempo exceeds the pitch limit. No transition was started.')
        # VirtualDJ fluid grids need its native stabilizer to hold a set tempo.
        # Setting a pitch percentage alone follows the recording's tempo drift.
        if as_bool(self.q(client,f'deck {deck} is_fluid','bool')):
            if not as_bool(self.q(client,f'deck {deck} bpm_stabilizer','bool')):
                self.send(client,f'deck {deck} bpm_stabilizer on')
                self.sleep(.15)
                if not as_bool(self.q(client,f'deck {deck} bpm_stabilizer','bool')):
                    raise BridgeError('Native tempo stabilization was not confirmed. No transition was started.')
                self.event(f'Deck {deck_rotation.letter(deck)}: native variable-tempo stabilization enabled.','analysis')
        # The "pitch N bpm" verb uses the catalog/global tempo on fluid grids.
        # Its result can differ from the measured local grid. Set the absolute
        # speed ratio from that measured tempo, then verify the actual result.
        original=number(self.q(client,f'deck {deck} get_bpm absolute'),40,300)
        ratio=target_bpm/original
        if abs(ratio-1)>self.snapshot().get('maxTempoShift',8)/100:
            raise BridgeError('The stabilized grid exceeds the pitch limit. No transition was started.')
        self.send(client,f'deck {deck} pitch {ratio*100:.6f}%')
        # A load can briefly report the previous native BPM even after the
        # playhead/identity settles. One matching sample is insufficient. A
        # confirmed small grid refinement permits one newly calculated pitch
        # on this same paused recording; never retry Play/Load/Mix or fight a
        # changed pitch with an unchanged source grid.
        deadline=time.monotonic()+3
        stable=0;refined=False
        while time.monotonic()<deadline:
            self.sleep(.15)
            if as_bool(self.q(client,f'deck {deck} play','bool')) or identity(str(self.q(client,f'deck {deck} get_filepath','text'))) != track['engine']['selection_id']:
                raise BridgeError('Incoming deck changed during tempo confirmation. No transition was started.')
            bpm=number(self.q(client,f'deck {deck} get_bpm'),40,300)
            native=number(self.q(client,f'deck {deck} get_bpm absolute'),40,300)
            if abs(native-original)>.03:
                if refined or abs(native/original-1)>.02 or abs(target_bpm/native-1)>self.snapshot().get('maxTempoShift',8)/100:
                    raise BridgeError('The native tempo did not settle within the prepared range. No transition was started.')
                original=native;refined=True;stable=0
                self.send(client,f'deck {deck} pitch {target_bpm/native*100:.6f}%')
                continue
            stable=stable+1 if abs(bpm-target_bpm)<=.03 else 0
            if stable>=4:
                self.event(f'Deck {deck_rotation.letter(deck)} tempo confirmed at {bpm:.2f} BPM.', 'tempo-aligned',
                           deck=deck,targetBpm=target_bpm,observedBpm=bpm,nativeBpm=native,gridRefined=refined)
                return
        raise BridgeError('Requested tempo was not confirmed. No transition was started.')

    def prepare(self):
        snapshot=self.snapshot()
        if snapshot.get('executionPlan'):
            return self.apply_plan()
        order=validate_order([t['id'] for t in snapshot['order']],snapshot['candidates'],snapshot.get('maxTempoShift',8),
                             snapshot['secondsPerTrack'],snapshot.get('tempoTargets'),fixed=snapshot['mixStyle']=='quick')
        def work(client):
            before=client.status()
            if before['automix'] or any(d['playing'] for d in before['decks']):
                raise BridgeError('Pause both decks and turn Automix off before preparing the set.')
            changed=False
            for i,track in enumerate(order):
                if track.get('engineChecked'):continue
                self.update(message=f'Checking {i+1} of {len(order)} · {track["title"]}. Audio remains paused.')
                actual=self.analyze_deck(client,track,1)
                changed=changed or actual['camelot']!=track.get('camelot') or abs(actual['bpm']-track['bpm'])>.15
                if as_bool(self.q(client,'deck 1 play','bool')):raise BridgeError('A deck started manually. Preparation stopped.')
                self.send(client,'deck 1 unload')
                self.sleep(.2)
            if changed:
                self.update(phase='catalog-ready',message='Selected tracks checked. Tempo or key changed after analysis; rebuild the order using the checked recordings.')
                self.event('Analysis updated. Rebuild required; no playback was started.','analysis')
                return
            checked={t['id']:t for t in self.snapshot()['candidates']}
            for i in range(2):
                self.prepare_deck(client,checked[order[i]['id']],i+1,order[0]['bpm'])
            self.send(client,'crossfader 0%')
            self.telemetry(client)
            self.update(phase='ready',message='Opening decks prepared and paused. Start set when you are ready.')
            self.event('Opening decks prepared. No playback was started.','prepared')
        self._launch(work,'preparing')
        return self.snapshot()

    def refresh_key_plan(self, snapshot, track, index):
        """Re-fit only a native key revision; retain measured cues and tempo."""
        old=snapshot['order'][index]
        if (old['id']!=track['id'] or abs(track['bpm']-old['bpm'])>.15
                or track.get('camelot')==old.get('camelot') or not track.get('camelot')
                or not (snapshot.get('executionPlan') or {}).get('applied')):
            raise TrackMetadataChanged(track)
        order=copy.deepcopy(snapshot['order']);order[index]=copy.deepcopy(track)
        source=next(copy.deepcopy(r) for r in snapshot['analysis']['tracks'] if r['id']==track['id'])
        source.update(key=track.get('key'),camelot=track['camelot'],bpm=track['bpm'])
        prepared=cue_planner.complete_background_queue(snapshot,order,[source],
            'Native key refined on the paused incoming deck; measured cues and the playing route are preserved.',index,
            tempo_targets=snapshot.get('tempoTargets'))
        cue_planner.verify_receipts(prepared['analysis'])
        return order,prepared

    def start(self, resume=False, initialize_mixer=False, immediate=False, adopt_mixer=False, resume_paused=False,
              held_recording=None, rolling_queue=False):
        from afterhours.rolling_queue import RollingQueue

        self.dig_restart()
        snapshot = self.snapshot()
        rolling = RollingQueue() if rolling_queue else None
        if snapshot.get('resumeBlocked'):
            raise ValueError('Native automation cleanup is unconfirmed. Stop playback and verify cleanup before starting another set.')
        planned=snapshot.get('executionPlan')
        immediate=bool(immediate and not resume and not (planned or {}).get('applied'))
        if ((snapshot['phase'] not in (('ready', 'completed', 'stopped', 'takeover', 'error') if immediate else ('ready', 'completed', 'stopped', 'takeover'))
             and not (resume and copilot_commands.resumable(snapshot))) or len(snapshot['order']) < 2):
            raise ValueError('Build a set before starting playback.')
        if immediate:planned=None
        order = validate_order([t['id'] for t in snapshot['order']], snapshot['candidates'], snapshot.get('maxTempoShift',8),
                               snapshot['secondsPerTrack'],snapshot.get('tempoTargets'),
                               frozen=(planned or {}).get('tempoSchedule'),opening=(planned or {}).get('targetBpm'),
                               fixed=snapshot['mixStyle']=='quick',
                               history_prefix=[t['id'] for t in snapshot['order'][:max(0,snapshot['index'])]] if resume else [],
                               retained_final_resume=bool(resume and snapshot['index']==len(snapshot['order'])-1 and
                                   (planned or {}).get('applied') and (planned or {}).get('orderIds')==[t['id'] for t in snapshot['order']]))
        if not immediate and snapshot['mixStyle']=='phrase' and not (snapshot.get('analysis') or {}).get('cueVersion'):
            raise ValueError('Analyze this set and apply a measured mix plan before starting.')
        if not immediate and (planned or (snapshot.get('analysis') and snapshot['analysis'].get('cueVersion'))):
            if not snapshot.get('analysis') or not snapshot.get('aiReview'):
                raise ValueError('The analyzed mix plan lost its source evidence. Prepare and plan again.')
            if not planned or not planned.get('applied'):
                raise ValueError('Apply the analyzed mix plan before starting this set.')
            checked_plan=cue_planner.compile_plan(snapshot['aiReview'],snapshot['analysis'],order,
                                                  snapshot['secondsPerTrack'],snapshot['mixStyle'])
            if checked_plan['id']!=planned['id']:
                raise ValueError('The analyzed mix plan changed. Apply it again before starting.')
            cue_planner.verify_receipts(snapshot['analysis'])
        def ready(client,track,deck,target,index,override=None):
            nonlocal order,planned
            if self.catalog_job and getattr(self.catalog_job,'rolling',False):
                self.catalog_job.resume_search()
            selected = override or planned
            if len(self.decks)>2:self.release_preparation(client,deck)
            if selected and selected.get('tempoSchedule'):target=selected['tempoSchedule'][index]['fromBpm']
            try:
                if selected:self.prepare_deck(client,track,deck,target,cue=selected['selections'][index])
                else:self.prepare_deck(client,track,deck,target)
            except TrackMetadataChanged as exc:
                if not override and not selected and snapshot['mixStyle']=='quick':
                    checked=exc.track
                    if checked['id']!=track['id']:raise
                    self.prepare_deck(client,checked,deck,target,checked=checked)
                    with self.lock:
                        order[index]=checked
                        candidates=[checked if t['id']==checked['id'] else t for t in self.state['candidates']]
                        self.update(order=copy.deepcopy(order),candidates=candidates)
                        queued=self.state.get('queuedUpdate')
                        if queued:
                            queued['tracks']=[copy.deepcopy(checked) if t['id']==checked['id'] else t for t in queued['tracks']]
                    self.event(f"{track['title']}: native metadata refreshed at the set tempo.",'analysis')
                    return
                if override or not selected:raise
                revised,prepared=self.refresh_key_plan({**self.snapshot(),'order':order,'executionPlan':planned},exc.track,index)
                newplan=prepared['executionPlan']
                target=newplan['tempoSchedule'][index]['fromBpm'] if newplan.get('tempoSchedule') else target
                self.prepare_deck(client,revised[index],deck,target,cue=newplan['selections'][index],checked=exc.track)
                newplan['applied']=True
                self.update(order=revised,**prepared,mixPlans=newplan['transitions'])
                with self.lock:
                    queued=self.state.get('queuedUpdate')
                    if queued:
                        checked_by_id={t['id']:t for t in revised}
                        queued['tracks']=[copy.deepcopy(checked_by_id.get(t['id'],t)) for t in queued['tracks']]
                        queued.update(id=uuid.uuid4().hex,status='needs-analysis',prepared=None)
                        queued.pop('error',None);queued.pop('failure',None)
                order,planned=revised,newplan
                self.event(f"{track['title']}: native key refined; upcoming handoff recalculated locally.",'analysis')
        def work(client):
            nonlocal order,planned
            self.update(rollingQueue={'status': 'ready'} if rolling else None)
            if not immediate and not resume:self.update(startup=None)
            before = client.status()
            if before['automix'] or (not resume and any(d['playing'] for d in before['decks'])):
                raise BridgeError('Pause both decks and turn Automix off before starting this set.')
            if planned and any(r['durationBeats'] for r in planned.get('tempoSchedule',[])) and as_bool(self.q(client,'pitch_lock','bool')):
                raise BridgeError('Turn off linked pitch before starting a set with tempo ramps. Playback was not started.')
            # The decks this set plays through. A resumed set keeps the
            # rotation it started with; a new one takes what the layout allows.
            saved=tuple(snapshot.get('deckRotation') or deck_rotation.TWO)
            self.decks=(saved if saved in (deck_rotation.TWO,deck_rotation.FOUR) else deck_rotation.TWO) if resume else self.choose_rotation(client)
            self.active_deck=None;self.pair_decks=None
            self.update(deckRotation=list(self.decks))
            nxt=lambda deck:deck_rotation.following(deck,self.decks)
            if not resume and any(as_bool(self.q(client,f'deck {deck} play','bool')) for deck in self.decks[2:]):
                raise BridgeError('Pause every deck before starting this set.')
            target = planned['targetBpm'] if planned else order[0]['bpm']
            if snapshot['mixStyle']=='quick':self.update(quickTargetBpm=target)
            resume_window = None
            paused_current = False
            if immediate:
                from afterhours.opening_playback import start_and_prepare
                order,planned,t,resume_window=start_and_prepare(self,client,order)
                target=planned['targetBpm'];active,index=1,0
            elif resume:
                if not planned or not copilot_commands.resumable(snapshot):
                    raise BridgeError('Resume needs a measured set in manual takeover.')
                t = self.telemetry(client)
                current, _ = (copilot_commands.resume_pair({**snapshot, 'telemetry': t},allow_paused=True) if resume_paused
                              else copilot_commands.playing_pair({**snapshot, 'telemetry': t}))
                paused_current = current['playing'] is False
                if held_recording is not None and (not paused_current or [current['deck'], current['id']] != held_recording[1:]):
                    raise BridgeError('The held recording changed before handback. No playback was started.')
                active, index = current['deck'], snapshot['index']
                final = index == len(order)-1
                resume_window = copilot_commands.retime(snapshot, current, 0 if final else planned['transitions'][index]['fadeBeats'], final)
                # A changing tempo route was cancelled on takeover. Resuming it
                # halfway would replay a ramp: require its settled target.
                rows = planned.get('tempoSchedule') or []
                target = rows[index]['toBpm'] if rows else target
                if abs(current['bpm']-target) > .15:
                    raise BridgeError('The playing tempo has not reached the prepared handoff target. Resume is held.')
                if resume_paused:
                    # An explicit handback owns the silent channel. Close it
                    # before restoring its measured cue; never seek the current
                    # recording or start an overlapping manually playing deck.
                    if abs(t['crossfader']-deck_rotation.crossfader_end(active)) > .02:
                        self.send(client,f'crossfader {int(deck_rotation.crossfader_end(active)*100)}%')
                    t=self.telemetry(client)
                    checked,_=copilot_commands.resume_pair({**snapshot,'telemetry':t},allow_paused=True)
                    if checked['deck'] != active or checked['playing'] != current['playing']:
                        raise BridgeError('The deck transport changed during handback. No playback was started.')
                    if not final:
                        ready(client,order[index+1],nxt(active),target,index+1)
                        t=self.telemetry(client)
            else:
                self.update(index=0, transitions=[], activeHandoff=None, phase='loading', message='Loading the first two tracks…')
                for i in range(2):
                    ready(client, order[i], i+1, target,i)
                self.send(client, 'crossfader 0%')
                t = self.telemetry(client)
                if t['crossfader'] > .02 or [d['id'] for d in t['decks'][:2]] != [order[0]['id'], order[1]['id']]:
                    raise BridgeError('Opening deck and mixer state were not confirmed; playback was not started.')
                active, index = 1, 0
            entries = {d['deck']: round(d['beatPosition']) for d in t['decks']}
            if snapshot['mixStyle']=='phrase' and not immediate:
                self.mixer = TransitionMixer(self.q,self.send)
                if resume:
                    self.mixer.resume_preflight(client,active,snapshot.get('mixerBaselines'),adopt_playing=adopt_mixer,
                                                **({'paused_current':True} if paused_current else {}),
                                                **({'decks':self.decks} if len(self.decks)>2 else {}))
                elif initialize_mixer:self.mixer.initialize_opening(client)
                else:self.mixer.preflight(client)
                self.update(mixerBaselines=copy.deepcopy(self.mixer.baselines))
            if planned and planned.get('tempoSchedule'):
                self.tempo_ramp=tempo_planner.TempoRamp(self.q,self.send,self.event)
            if paused_current:
                fresh=self.telemetry(client)
                checked,_=copilot_commands.resume_pair({**snapshot,'telemetry':fresh},allow_paused=True)
                if checked['playing'] or checked['deck'] != active:
                    raise BridgeError('A deck started during handback. No second Play was sent.')
                # Preparing the incoming recording can refine the plan or take
                # time. Confirm the current paused position still has a real
                # exit before granting this explicit resume its one Play.
                copilot_commands.retime({**self.snapshot(),'order':order,'executionPlan':planned,'index':index},checked,
                                        0 if final else planned['transitions'][index]['fadeBeats'],final)
                self.availability.require_track(order[index])
                self.send(client,f'deck {active} play')
                self.sleep(.2)
                t=self.telemetry(client)
                copilot_commands.playing_pair({**snapshot,'telemetry':t})
            if not resume and not immediate:
                self.availability.require_track(order[0])
                self.send(client, 'deck 1 play')
                self.sleep(.2)
                t = self.telemetry(client)
                if not t['decks'][0]['playing']:
                    raise BridgeError('Start was not confirmed. No retry was sent.')
            self.update(phase='playing', startedAt=self.snapshot().get('startedAt') if immediate or resume else time.time(), endedAt=None, message=f"Playing {order[index]['title']}")
            with self.lock:
                self.state['copilot'].update(mode='mix' if self.state['copilot']['mode']=='mix' else 'leading', held=False)
            self.event(f"Set {'resumed' if resume else 'started'} · {order[index]['title']}", 'playing')
            resumed_index = index if resume else None
            while index<len(order):
                track=order[index]
                self.update(index=index)
                final = index == len(order)-1
                incoming = nxt(active)
                # Telemetry reads these two in full and the others lightly.
                self.active_deck=active;self.pair_decks=(active,incoming)
                recipe = None
                tempo_row=planned['tempoSchedule'][index] if planned and planned.get('tempoSchedule') and index != resumed_index else None
                if tempo_row:
                    target=tempo_row['toBpm']
                    if not self.tempo_ramp:self.tempo_ramp=tempo_planner.TempoRamp(self.q,self.send,self.event)
                    if not self.tempo_ramp.row or self.tempo_ramp.track['id']!=track['id']:
                        self.tempo_ramp.arm(track,active,tempo_row,planned['selections'][index]['entryBeat'])
                if not final:
                    expected = order[index+1]
                    recipe = copy.deepcopy(planned['transitions'][index]) if planned else transition_recipe(track,expected,snapshot['mixStyle'],snapshot['secondsPerTrack'],target,
                        quick_blend_seconds=snapshot.get('quickBlendSeconds'))
                    if t['decks'][incoming-1].get('id') != expected['id'] or t['decks'][incoming-1]['playing']:
                        raise BridgeError('Incoming deck changed. No transition was sent.')
                    incoming_target=target if tempo_row else t['decks'][active-1]['bpm']
                    if abs(t['decks'][incoming-1]['bpm']-incoming_target) > .15:
                        if not tempo_row and abs(incoming_target/target-1)>.02:
                            raise BridgeError('Playing deck tempo moved outside the set range. No transition was sent.')
                        self.match_tempo(client,expected,incoming,incoming_target)
                        t=self.telemetry(client)
                    if self.mixer:
                        self.mixer.arm(client,active,incoming,recipe)
                        self.update(copilotTargets=self.mixer.targets())
                        self.event(f"{recipe['label']} prepared · {recipe['reason']}", 'technique')
                        self.event(recipe['leadControlNote'], 'technique')
                if resume and index == resumed_index:
                    # The outgoing audio keeps advancing during silent-deck
                    # preparation and mixer arming. Choose a verified future
                    # exit from a new read, not the pre-preparation position.
                    t=self.telemetry(client)
                    current,following=copilot_commands.playing_pair({**self.snapshot(),'order':order,'index':index,'telemetry':t})
                    if abs(current['bpm']-target)>.15 or (not final and abs(following['bpm']-target)>.15):
                        raise MixerControlChanged('Deck tempos moved after alignment. No transition was sent.')
                    resume_window=copilot_commands.retime({**self.snapshot(),'order':order,'executionPlan':planned,'index':index},current,
                                                         0 if final else recipe['fadeBeats'],final)
                window = resume_window or (cue_planner.execution_window(planned['selections'][index],t['decks'][active-1]['beatPosition'],0 if final else recipe['fadeBeats'],final)
                          if planned else segment_window(entries[active], t['decks'][active-1]['beatPosition'],
                                        snapshot['secondsPerTrack'], target, snapshot['mixStyle'], final, recipe))
                resume_window = None
                start_beat, goal = window['entryBeat'], window['mixAtBeat']
                self.update(segmentBeats=window['segmentBeats'], targetBeat=goal, fadeBeats=window['fadeBeats'],
                            timing=dict(window), message=f"Playing {track['title']}")
                if resume and index == resumed_index:
                    self._copilot_result('confirmed', 'Copilot resumed from the playing recording at a verified future cue.')
                revised_final=False
                while True:
                    t = self.telemetry(client)
                    current = t['decks'][active-1]
                    if current['id'] != track['id'] or not current['playing']:
                        raise BridgeError('A deck changed or was paused. Scheduling stopped for manual control.')
                    if tempo_row:
                        control=self.tempo_ramp.tick(client,current,t['decks'][incoming-1])
                        self.update(tempoControl=control)
                    change = self.service_copilot(client, current, incoming, ready, target, window)
                    if change:
                        window = change['window']
                        start_beat, goal = window['entryBeat'], window['mixAtBeat']
                        if 'plan' in change:
                            order, planned, t = change['order'], change['plan'], change['telemetry']
                            expected = order[index+1]
                            recipe = copy.deepcopy(planned['transitions'][index])
                            entries[incoming] = round(t['decks'][incoming-1]['beatPosition'])
                            if self.mixer:
                                self.mixer.arm(client,active,incoming,recipe)
                                self.update(copilotTargets=self.mixer.targets())
                        self.update(targetBeat=goal, timing=dict(window), segmentBeats=window['segmentBeats'], fadeBeats=window['fadeBeats'])
                        continue  # fresh observation after preparation or retiming
                    if final and snapshot['mixStyle']=='quick':
                        revised=self.adopt_queue(index)
                        if revised:
                            order,planned=revised
                            if index+1<len(order):
                                ready(client,order[index+1],incoming,target,index+1)
                                t=self.telemetry(client);entries[incoming]=round(t['decks'][incoming-1]['beatPosition'])
                                revised_final=True;break
                    if self.snapshot()['copilot']['held']:
                        # Current music continues. Do not invent a loop, seek,
                        # substitute cue, or implicit handoff when Hold expires.
                        self.sleep(.12)
                        continue
                    self.update(segmentProgress=max(0, min(1, (current['beatPosition'] - start_beat) / (window['endBeat']-start_beat))))
                    if current['beatPosition'] >= goal:
                        if current['beatPosition'] - goal > 1:
                            if planned:
                                raise BridgeError('The analyzed transition window was missed. Scheduling stopped; no substitute cue was used.')
                            # A delayed native read must not turn a missed block
                            # into an immediate mid-phrase cut.
                            extra = math.ceil((current['beatPosition']+4-goal)/window['phraseBeats']) * window['phraseBeats']
                            goal += extra
                            window.update(mixAtBeat=goal, endBeat=window['endBeat']+extra,
                                          segmentBeats=window['segmentBeats']+extra)
                            self.update(targetBeat=goal, timing=dict(window), segmentBeats=window['segmentBeats'])
                            self.event('Timing read arrived late; waiting for the next grid block.', 'timing')
                            continue
                        with self.lock:
                            control = self.state['copilot']
                            if control['held'] or (control.get('command') or {}).get('status') == 'pending':
                                continue
                            if not final:self.state['phase'] = 'mixing'
                            elif snapshot['mixStyle']=='quick':
                                if (self.state.get('queuedUpdate') or {}).get('status')=='ready':
                                    continue  # adopt an insertion received as the last window ended
                                self.state['phase']='stopping'
                        break
                    slack=(goal-current['beatPosition'])*60/max(40,current['bpm'])
                    self.service_background(client,slack)
                    if slack>=16 and (self.snapshot().get('queuedUpdate') or {}).get('status')=='ready':
                        revised=self.adopt_queue(index)
                        if revised:
                            order,planned=revised
                            if final and index+1<len(order):
                                ready(client,order[index+1],incoming,target,index+1)
                                t=self.telemetry(client);entries[incoming]=round(t['decks'][incoming-1]['beatPosition'])
                                revised_final=True;break
                    if rolling:rolling.tick(self,slack)
                    self.service_catalog(client,slack)
                    self.service_dig(client,slack)
                    self.sleep(.12)
                if revised_final:continue
                if final:
                    self.send(client, f'deck {active} pause')
                    self.sleep(.15)
                    t = self.telemetry(client)
                    if any(d.get('playing') for d in t['decks']):
                        raise BridgeError('Set finished; paused state is not confirmed. Inspect VirtualDJ.')
                    self.update(phase='completed', endedAt=time.time(), message=f"Set complete · {len(order)} tracks, {len(order)-1} transitions", segmentProgress=1)
                    self.event('Set complete. Both decks are paused.', 'completed')
                    return
                if t['decks'][incoming-1]['id'] != expected['id'] or t['decks'][incoming-1]['playing']:
                    raise BridgeError('Incoming deck changed. No transition was sent.')
                if abs(t['decks'][active-1]['bpm']-t['decks'][incoming-1]['bpm']) > .15:
                    raise MixerControlChanged('Deck tempos moved after alignment. No transition was sent.')
                if tempo_row and not self.tempo_ramp.done:
                    raise BridgeError('Tempo ramp was not confirmed before the handoff. No transition was started.')
                fade_beats = window['fadeBeats']
                if self.mixer:
                    self.mixer.validate_armed(client)
                    observed=number(self.q(client,f'deck {active} get_beatpos'),-10000,100000)
                    if observed-goal>1:
                        raise BridgeError('Mixer checks missed the transition window. No late transition was started.')
                    current={**current,'beatPosition':observed}
                self.availability.require_track(expected)
                self.update(phase='mixing', message=f"Mixing {track['title']} → {expected['title']}")
                self.event(f"{fade_beats//4}-bar blend · {track['title']} → {expected['title']}", 'mix')
                self.handoff=NativeHandoff(self.q,self.send,self.sleep)
                self.handoff.dispatch(client,active,incoming,fade_beats,**({'decks':self.decks} if len(self.decks)>2 else {}))
                if self.mixer:
                    self.mixer.begin(client)
                deadline = time.monotonic() + fade_beats*60/target + 8
                evidence = {'from': track['id'], 'to': expected['id'], 'at': time.time(), 'samples': [],
                            'timing': copy.deepcopy(window), 'observedStartBeat':current['beatPosition'],
                            'incomingCueBeat':entries[incoming], 'recipe':recipe, 'mixerSamples':[]}
                if tempo_row:evidence.update(tempoPlan=copy.deepcopy(tempo_row),tempoSamples=copy.deepcopy(self.tempo_ramp.samples))
                self.update(activeHandoff={k:evidence[k] for k in ('from','to','observedStartBeat','incomingCueBeat','timing')})
                while time.monotonic() < deadline:
                    self.sleep(.16)
                    t = self.telemetry(client)
                    if self.mixer:
                        controls = self.mixer.tick(client,t['decks'][incoming-1]['beatPosition']-entries[incoming])
                        self.update(transitionControl=controls)
                        evidence['mixerSamples'].append(controls)
                    # The pair in deck order, so A/B samples read as before.
                    a,b = (t['decks'][deck-1] for deck in sorted((active,incoming)))
                    if a.get('playing') and b.get('playing'):
                        delta = abs(a['phase']-b['phase'])
                        evidence['samples'].append({'bpmA': a['bpm'], 'bpmB': b['bpm'],
                            'phaseDifference': min(delta,1-delta), 'syncA':a['sync'],'syncB':b['sync'],
                            'crossfader':t['crossfader'], 'levelA':a['level'],'levelB':b['level']})
                    self.update(segmentProgress=max(0, min(1, (t['decks'][active-1]['beatPosition']-start_beat)/(window['endBeat']-start_beat))))
                    endpoint = abs(t['crossfader']-deck_rotation.crossfader_end(incoming)) <= .001
                    if endpoint and t['decks'][incoming-1]['playing']:
                        break
                else:
                    raise BridgeError('Transition completion was not confirmed. Scheduling stopped; no repeat mix was sent.')
                evidence['verified'] = True
                if self.mixer:
                    self.mixer.verify_handoff(client)
                evidence['completedAt'] = time.time()
                evidence['syncObserved'] = any(s['syncA'] or s['syncB'] for s in evidence['samples'])
                with self.lock:
                    self.state['transitions'].append(evidence)
                self.archive_call('transition',evidence)
                self.event(f"Handoff confirmed · {expected['title']}", 'transition', syncObserved=evidence['syncObserved'])
                self.send(client, f'deck {active} pause')
                self.sleep(.15)
                if as_bool(self.q(client, f'deck {active} play', 'bool')):
                    raise BridgeError('Outgoing deck did not pause. Scheduling stopped.')
                self.handoff.cancel_ramps(client)
                if self.mixer:
                    self.mixer.cancel_ramps(client)
                    self.mixer.reset_paused(client,active)
                if len(self.decks)>2:
                    # Its record is finished. Empty, the deck can prepare an
                    # upcoming one until its own turn comes round again.
                    self.send(client,f'deck {active} play ? nothing : deck {active} unload')
                active = incoming
                self.active_deck=active;self.pair_decks=(active,nxt(active))
                self.update(phase='playing', index=index+1, transitionControl=None, activeHandoff=None, copilotTargets=None, message=f"Playing {expected['title']}")
                revised=self.adopt_queue(index+1)
                if revised:order,planned=revised
                if planned and planned.get('tempoSchedule'):
                    if not self.tempo_ramp:self.tempo_ramp=tempo_planner.TempoRamp(self.q,self.send,self.event)
                    self.tempo_ramp.arm(order[index+1],active,planned['tempoSchedule'][index+1],planned['selections'][index+1]['entryBeat'])
                if index+2 < len(order):
                    ready(client, order[index+2], nxt(active), target,index+2)
                t = self.telemetry(client)
                if index+2 < len(order):
                    entries[nxt(active)] = round(t['decks'][nxt(active)-1]['beatPosition'])
                index+=1
        self._launch(work, 'loading')
        return self.snapshot()

    def console_control(self, data):
        from afterhours.vdj_controls import perform_control, validate_control
        validate_control(data)
        if not self.control_lock.acquire(timeout=.35):
            raise ValueError('VirtualDJ is busy. No control was queued; try again after readings refresh.')
        try:
            s=self.snapshot()
            if s['active'] or s['phase']!='takeover':
                raise ValueError('Take over the set before using manual console controls.')
            with self.factory(self.port,timeout=.3,budget_seconds=4) as client:
                result=perform_control(client,data)
                self.console_readings={};self.eq_readings={}
                try:self.telemetry(client,observing=True)
                except (BridgeError,OSError):pass
            self.event(result['message'],kind='control',control=result['control'],verified=result['verified'])
            return dict(result,session=self.snapshot())
        finally:
            self.control_lock.release()

    def observe_once(self):
        """Native loads are visible even before the agent has built a queue."""
        with self.lock:
            if self.state['active'] or not self.control_lock.acquire(blocking=False):return False
            self.observing=True
        try:
            with self.factory(self.port,timeout=.2,budget_seconds=2) as client:
                self.telemetry(client,observing=True)
            return True
        except (BridgeError,OSError):
            return False
        finally:
            with self.lock:
                self.observing=False
                self.control_lock.release()

    def observe(self):
        """Keep visuals truthful after takeover without issuing transport."""
        while True:
            began=time.monotonic()
            self.observe_once()
            # Fast physical readback is independent of slower catalog/AI polls.
            # The nonblocking ownership guard still yields to every mix/load.
            time.sleep(max(.01,.05-(time.monotonic()-began)))

    def program(self):
        s = self.snapshot()
        t = s.get('telemetry')
        fresh = bool(t and -2 <= time.time()-t['sampledAt'] < 3)
        if not t:
            if s.get('operatorFresh'):
                now=time.time()
                return {'app':'Afterhours','backend':'Afterhours','connected':True,'playing':False,'paused':True,
                        'current':None,'library':[],'session':s['id'],'sessionPhase':s['phase'],
                        'clock':{'bpm':None,'playing':False,'sampledAtUnixMs':now*1000},'updated':now}
            return None
        deck = t['decks'][0 if t['crossfader'] < .5 else 1]
        available=[d for d in t['decks'] if d.get('loaded') is not False and not d.get('unavailable')]
        # A paused, completed set keeps its last recording as the program.
        # Moving the hardware crossfader alone cannot resurrect an older track
        # or a next-track countdown. New manual loads still use observed decks.
        index=s.get('index',-1)
        if (s['phase']=='completed' and available and all(d.get('playing') is False for d in available)
                and 0<=index<len(s['order']) and all(d.get('id') in {v['id'] for v in s['order']} for d in available)):
            deck=next((d for d in available if d.get('id')==s['order'][index]['id']),deck)
        if not deck.get('playing'):
            deck=next((d for d in available if d.get('playing')),deck)
        if deck.get('loaded') is False:
            deck=next(iter(available),deck)
        playing = fresh and deck.get('playing', False)
        controls=deck.get('controls') or {}
        native_fresh=fresh and -2<=time.time()-controls.get('sampledAt',0)<3
        track=next((v for v in s['order'] if v['id']==deck.get('id')),None)
        library=copy.deepcopy(s['order'])
        if deck.get('id') and track is None:
            track={'id':deck['id'],'title':deck.get('title',''),'artist':deck.get('artist',''),
                   'bpm':controls.get('originalBpm') or deck.get('bpm'),
                   'camelot':controls.get('key') if native_fresh else None,
                   'duration':controls.get('durationMs',0)/1000 or None,'source':'VirtualDJ'}
            library.append(track)
        evidence=next((v for v in (s.get('analysis') or {}).get('tracks',[]) if v['id']==deck.get('id')),None)
        current_index=next((i for i,v in enumerate(s['order']) if v['id']==deck.get('id')),-1)
        next_track=s['order'][current_index+1] if 0<=current_index<len(s['order'])-1 else None
        cue=next((v for v in (s.get('executionPlan') or {}).get('selections',[]) if v['trackId']==deck.get('id')),None)
        exit_beat=cue['exitBeat'] if cue else None
        scheduled=(s.get('timing') or {}).get('mixAtBeat')
        if (s.get('active') and s['phase'] in ('playing','mixing') and current_index==index and
                ((s.get('executionPlan') or {}).get('applied') or
                 (s.get('mixStyle')=='quick' and (s.get('timing') or {}).get('basis')=='native-beat-grid')) and
                type(scheduled) in (int,float) and math.isfinite(scheduled)):
            exit_beat=scheduled  # Go can advance the verified exit independently of the saved plan.
        remaining=(exit_beat-deck['beatPosition'])*60/deck['bpm'] if exit_beat is not None and deck.get('bpm') and deck.get('beatPosition') is not None else None
        elapsed=max(0,(s.get('endedAt') or (time.time() if fresh else t['sampledAt']))-s['startedAt']) if s.get('startedAt') else 0
        return {'app': 'Afterhours', 'backend': 'VirtualDJ', 'connected': fresh,
                'playing': playing, 'paused': not playing, 'current': deck.get('id'),
                'library': library, 'session': s['id'], 'sessionPhase': s['phase'],
                'positionSeconds':deck.get('position'), 'durationSeconds':controls.get('durationMs',0)/1000 or (evidence or {}).get('duration'),
                'key':controls.get('key') if native_fresh else (track or {}).get('camelot'), 'energyMap':(evidence or {}).get('energyMap'),
                'nextTrack':next_track, 'transitionInSeconds':max(0,remaining) if remaining is not None and next_track else None,
                'setElapsedSeconds':elapsed, 'room':None, 'setTimeline':set_timeline(s,deck),
                'clock': {'bpm':deck.get('bpm'), 'beatPosition':deck.get('beatPosition'),
                          'playing':playing, 'sampledAtUnixMs':t['sampledAt']*1000,
                          'trackId':deck.get('id'), 'estimatedGrid':True,
                          'outputLatencySeconds':0}, 'updated':t['sampledAt']}
