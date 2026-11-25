"""Cooperative spare-deck preparation; the playback worker is the sole OSC owner.

The analysis thread has NO socket. Each of its bounded calls goes through a
mailbox serviced only with time to spare before a handoff. Provider requests
and audio capture are absent. Cancellation releases blocked calls promptly.
"""
from pathlib import Path
import copy
import math
import queue
import re
import threading
import time
import uuid
from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.vdj_catalog import identity, require_selection, dispatch_exact_load, ExactRestoreCleanupUnconfirmed
from afterhours.audio_analysis import scan_track, restore_effect, PLUGIN
from afterhours.waveform_analysis import scan_waveform, verified_waveform
from afterhours.native_analysis import track_analysis

class AnalysisCancelled(Exception):pass
class CatalogInterrupted(BridgeError):pass

PLUGIN_INFO=Path.home()/'Library/Application Support/VirtualDJ/PluginsMacArm/SoundEffect/Afterhours Analysis.bundle/Contents/Info.plist'


def four_deck_tools(path=None):
    """Whether the installed analysis plugin can prepare on any isolated deck (0.3.0 or later)."""
    import plistlib
    try:
        with open(path or PLUGIN_INFO,'rb') as handle:version=str(plistlib.load(handle).get('CFBundleShortVersionString',''))
        parts=tuple(int(x) for x in version.split('.')[:3])
    except (OSError,ValueError,plistlib.InvalidFileException):return False
    return len(parts)>=2 and parts>=(0,3)


def guard_deck(client, deck=3):
    # Any of the four decks can be a spare in a four-deck rotation; which ones
    # are spare right now is the preparation pool's decision, never this guard's.
    if type(deck) is not int or deck not in (1,2,3,4) or float(client.query('get_decks'))<4 or float(client.query(f'deck {deck} get_deck'))!=deck:
        raise BridgeError('Background analysis needs a four-deck VirtualDJ layout; The analysis deck must not alias A/B.')

def guard_isolation(client, deck=3):
    guard_deck(client,deck)
    if (as_bool(client.query(f'deck {deck} play','bool')) or float(client.query(f'deck {deck} volume'))!=0 or
            as_bool(client.query(f'deck {deck} pfl','bool'))):
        raise BridgeError(f'Analysis deck {deck} is no longer paused and isolated. Background preparation stopped.')

def cleanup_descriptor(value):
    """Validate saved ownership as data, never accept recovered engine verbs."""
    if not isinstance(value,dict) or value.get('version')!=1 or type(value.get('deck')) is not int or value['deck'] not in ((1,2,3,4) if value.get('fourDeck') is True else (3,4)):
        raise BridgeError('Analysis cleanup has an invalid spare-deck descriptor.')
    owner=value.get('ownerId');sid=value.get('selectionId');baseline=value.get('baseline')
    if not isinstance(owner,str) or not re.fullmatch(r'[0-9a-f]{32}',owner):
        raise BridgeError('Analysis cleanup ownership is missing.')
    if sid is not None and (not isinstance(sid,str) or not re.fullmatch(r'[0-9a-f]{64}',sid)):
        raise BridgeError('Analysis cleanup recording identity is invalid.')
    if not isinstance(baseline,dict):raise BridgeError('Analysis cleanup baseline is missing.')
    volume=baseline.get('volume');effect=baseline.get('effect')
    if (type(volume) not in (int,float) or not math.isfinite(volume) or not 0<=volume<=1 or
            type(baseline.get('pfl')) is not bool or not isinstance(effect,str) or
            not re.fullmatch(r'[A-Za-z0-9 _.\-]{0,80}',effect)):
        raise BridgeError('Analysis cleanup baseline is invalid.')
    for field in ('loadPending','unloadRequested','restoreRequested'):
        if type(value.get(field,False)) is not bool:raise BridgeError('Analysis cleanup stage is invalid.')
    # Only a four-deck set can have prepared on A or B, and it says so.
    return {'version':1,'ownerId':owner,'deck':value['deck'],'selectionId':sid,
            'baseline':copy.deepcopy(baseline),'loadPending':value.get('loadPending',False),
            'unloadRequested':value.get('unloadRequested',False),
            'restoreRequested':value.get('restoreRequested',False),'error':str(value.get('error') or '')[:300],
            **({'fourDeck':True} if value['deck'] in (1,2) else {})}

def merge_cleanup_pending(*sources):
    """Keep known cleanup and any independent unknown latch through recovery."""
    names=[];decks={};conflicts=set();unknown=False
    for source in sources:
        if not isinstance(source,dict):unknown=True;continue
        ramps=source.get('ramps',[])
        if not isinstance(ramps,list) or not all(isinstance(name,str) for name in ramps):unknown=True
        else:names.extend(ramps)
        analysis=source.get('analysis',[])
        if not isinstance(analysis,list):unknown=True;analysis=[]
        for value in analysis:
            try:row=cleanup_descriptor(value)
            except BridgeError:unknown=True;continue
            deck=row['deck'];before=decks.get(deck)
            if before and any(before[k]!=row[k] for k in ('ownerId','selectionId','baseline')):
                conflicts.add(deck);unknown=True
            else:
                if before:
                    for field in ('unloadRequested','restoreRequested'):row[field]=row[field] or before[field]
                    row['loadPending']=row['loadPending'] and before['loadPending']
                decks[deck]=row
        unknown=unknown or bool(source.get('unknown'))
    analysis=[row for deck,row in sorted(decks.items()) if deck not in conflicts]
    result={'ramps':sorted(set(names)),'unknown':unknown or not (names or analysis)}
    if analysis:result['analysis']=analysis
    return result

def cleanup_analysis_deck(client, descriptor):
    """Recover only the retained exact spare-deck ownership; never start audio."""
    row=cleanup_descriptor(descriptor);deck=row['deck'];baseline=row['baseline']
    expected_effect=baseline['effect'] or PLUGIN
    def effect_is_restored():
        return (client.query(f'deck {deck} get_effect_name 1','text')==expected_effect and
                not as_bool(client.query(f'deck {deck} effect_active 1','bool')))
    def isolated_identity():
        guard_isolation(client,deck)
        loaded=as_bool(client.query(f'deck {deck} loaded','bool'))
        if loaded and (not row['selectionId'] or identity(str(client.query(f'deck {deck} get_filepath','text')))!=row['selectionId']):
            raise BridgeError(f'Analysis deck {deck} recording changed; cleanup ownership needs inspection.')
        if loaded:
            row['loadPending']=False;descriptor['loadPending']=False
        elif row['loadPending']:
            raise BridgeError(f'Analysis deck {deck} stream load is still unconfirmed; isolation and ownership were retained.')
        return loaded
    # A lost final readback may follow a successful restoration. Verify that
    # exact final state before requiring the earlier muted-isolation state.
    if row['restoreRequested']:
        guard_deck(client,deck)
        if (not as_bool(client.query(f'deck {deck} play','bool')) and
                not as_bool(client.query(f'deck {deck} loaded','bool')) and effect_is_restored() and
                abs(float(client.query(f'deck {deck} volume'))-baseline['volume'])<.00001 and
                as_bool(client.query(f'deck {deck} pfl','bool'))==baseline['pfl']):return
    loaded=isolated_identity()
    current=client.query(f'deck {deck} get_effect_name 1','text')
    if current not in (PLUGIN,baseline['effect']):
        raise BridgeError(f'Analysis deck {deck} effect changed; cleanup will not overwrite it.')
    if current==PLUGIN:restore_effect(client,deck,expected_effect)
    if not effect_is_restored():
        raise BridgeError(f'Analysis deck {deck} effect cleanup was not confirmed.')
    # Effect restoration can take time. Recheck isolation and exact recording
    # immediately before the guarded, idempotent unload.
    loaded=isolated_identity()
    if loaded:
        descriptor['unloadRequested']=True
        client.send(f'deck {deck} play ? nothing : deck {deck} unload')
        if as_bool(client.query(f'deck {deck} loaded','bool')):
            raise BridgeError(f'Analysis deck {deck} unload was not confirmed; ownership was retained.')
    if isolated_identity():raise BridgeError(f'Analysis deck {deck} changed during cleanup.')
    descriptor['restoreRequested']=True
    client.send(f'deck {deck} play ? nothing : deck {deck} volume {baseline["volume"]*100:.6f}% & deck {deck} pfl '+('on' if baseline['pfl'] else 'off'))
    guard_deck(client,deck)
    if (as_bool(client.query(f'deck {deck} play','bool')) or as_bool(client.query(f'deck {deck} loaded','bool')) or
            not effect_is_restored() or abs(float(client.query(f'deck {deck} volume'))-baseline['volume'])>=.00001 or
            as_bool(client.query(f'deck {deck} pfl','bool'))!=baseline['pfl']):
        raise BridgeError(f'Analysis deck {deck} baseline restoration was not confirmed; ownership was retained.')

def allowed(script, mutation, deck=3):
    # The analysis facade must never address another deck, a master, or an
    # unscoped audio command. Catalog navigation is shared but carries no audio action.
    if deck not in (1,2,3,4) or re.search(rf'deck\s+(?!{deck}\b)\S+',script):return False
    if script.startswith(f'deck {deck} '):
        verbs=('get_', 'loaded','play','deck_has_error','effect_active','volume','pfl','pitch') if not mutation else (
            'master_tempo on','goto_first_beat','goto ','set_bpm ','effect_select 1 ',
            'effect_active 1 ','effect_button 1 ')
        return script[7:].startswith(verbs) and '&' not in script
    if not mutation:return script.startswith(('get_browsed_','file_count','get_decks','automix'))
    return script.startswith(('search "','browser_scroll ')) or script=="browser_window 'songs' & browser_scroll 'top'"

class MailboxClient:
    def __init__(self,job):self.job=job
    def query(self,script,kind=None):return self.job.request('query',script,kind)
    def send(self,script):return self.job.request('send',script,None)

class BackgroundAnalysis:
    def __init__(self,session,revision,tracks,cache=None,deck=3):
        if type(deck) is not int or deck not in (1,2,3,4):raise ValueError("Preparation needs a spare deck.")
        self.deck=deck
        self.session=session;self.revision=revision;self.tracks=copy.deepcopy(tracks)
        self.cache=copy.deepcopy(cache or {});self.pending=queue.Queue(maxsize=1)
        self.cancelled=threading.Event();self.done=False;self.error=None;self.result=[]
        self.status='queued';self.message='Upcoming recordings waiting for background analysis.'
        self.thread=None;self.owned=False;self.loaded=None;self.baseline=None
        self.owner_id=uuid.uuid4().hex;self.requested_load=None;self.cleanup_state={}
        self.load_dispatches=0
        self.started=time.monotonic();self.serviced=0;self.suspended=True;self.last_clock=time.monotonic();self.active_time=0
        self.clock_lock=threading.Lock()
        self.browser_epoch=0
    def invalidate_catalog(self):
        # Foreground deck preparation owns catalog navigation during a handoff.
        # Re-select afterwards; never load whichever row it left selected.
        self.browser_epoch+=1
    def clock(self):
        with self.clock_lock:
            now=time.monotonic()
            if not self.suspended:self.active_time+=now-self.last_clock
            self.last_clock=now;return self.active_time
    def check(self):
        if self.cancelled.is_set():raise AnalysisCancelled()
        if time.monotonic()-self.started>900:raise BridgeError('Background preparation exceeded its time limit; the active set was kept.')
    def wait(self,seconds):
        self.check()
        if self.cancelled.wait(seconds):raise AnalysisCancelled()
    def request(self,kind,script,argument):
        self.check()
        if kind not in ('load','isolate-loaded') and not allowed(script,kind=='send',self.deck):
            raise BridgeError('Analysis attempted an unsupported engine operation.')
        response=queue.Queue(maxsize=1);self.pending.put((kind,script,argument,response))
        while True:
            self.check()
            try:value=response.get(timeout=.1)
            except queue.Empty:continue
            if isinstance(value,Exception):raise value
            return value
    def acquire(self,client):
        guard_deck(client,self.deck)
        if as_bool(client.query(f'deck {self.deck} loaded','bool')) or as_bool(client.query(f'deck {self.deck} play','bool')):
            raise BridgeError(f'Deck {self.deck} is in use. Empty it to allow background analysis; A/B playback is unchanged.')
        previous=client.query(f'deck {self.deck} get_effect_name 1','text')
        if (not isinstance(previous,str) or not re.fullmatch(r'[A-Za-z0-9 _.\-]{0,80}',previous) or
                as_bool(client.query(f'deck {self.deck} effect_active 1','bool'))):
            raise BridgeError(f'Deck {self.deck} needs an inactive effect slot for background analysis.')
        self.baseline={'volume':float(client.query(f'deck {self.deck} volume')),'pfl':as_bool(client.query(f'deck {self.deck} pfl','bool')),'effect':previous}
        self.owned=True
        client.send(f'deck {self.deck} play ? nothing : deck {self.deck} volume 0% & deck {self.deck} pfl off')
        guard_isolation(client,self.deck)
        if previous=='':
            # A fresh VirtualDJ launch can leave spare slots empty. The host
            # does not restore an empty selection, so initialize only this
            # empty, owned slot with our inactive pass-through analyzer.
            client.send(f'deck {self.deck} effect_select 1 "{PLUGIN}"')
            deadline=time.monotonic()+3
            while True:
                guard_isolation(client,self.deck)
                current=client.query(f'deck {self.deck} get_effect_name 1','text')
                if current==PLUGIN:break
                if current!='':raise BridgeError('The preparation effect slot changed during initialization.')
                if time.monotonic()>=deadline:raise BridgeError('The Afterhours Analysis bridge is not available on the spare deck.')
                self.wait(.05)
            if as_bool(client.query(f'deck {self.deck} effect_active 1','bool')):
                raise BridgeError('The preparation effect activated unexpectedly.')
    def start(self,client):
        try:
            for track in self.tracks:self.session.availability.require_track(track)
            if not self.owned:self.acquire(client)
        except (BridgeError,ValueError,OSError) as exc:
            self.error=str(exc);self.done=True;return
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def step(self,client,seconds_available):
        self.clock();self.suspended=seconds_available<12
        if self.suspended or self.done:return
        if not self.tracks:
            self.done=True;self.status='complete';self.message='Reusing already verified recordings.';return
        if not self.thread:self.start(client)
        if self.done:return
        # At most six serialized operations/60 ms per service call. A single
        # timeout may take the client's bounded .45s; twelve seconds is reserved.
        until=time.monotonic()+.06
        for _ in range(6):
            if time.monotonic()>until:break
            try:kind,script,arg,response=self.pending.get(timeout=.001)
            except queue.Empty:break
            try:
                self.check()
                if kind in ('send','load'):
                    guard_isolation(client,self.deck)
                    if self.loaded and identity(str(client.query(f'deck {self.deck} get_filepath','text')))!=self.loaded:
                        raise BridgeError('Analysis recording changed outside the background worker.')
                if kind=='isolate-loaded':
                    # VirtualDJ's automatic headphone cue can enable PFL when
                    # our streamed Load finishes. Clear it only at this exact
                    # owned-load boundary, never after later operator changes.
                    guard_deck(client,self.deck)
                    if (not self.owned or self.loaded!=arg or
                            identity(str(client.query(f'deck {self.deck} get_filepath','text')))!=arg or
                            as_bool(client.query(f'deck {self.deck} play','bool')) or
                            float(client.query(f'deck {self.deck} volume'))!=0):
                        raise BridgeError('The preparation deck changed while its recording loaded.')
                    if as_bool(client.query(f'deck {self.deck} pfl','bool')):
                        client.send(f'deck {self.deck} play ? nothing : deck {self.deck} pfl off')
                    guard_isolation(client,self.deck)
                    value=None
                elif kind=='load':
                    if arg['browserEpoch']!=self.browser_epoch:raise CatalogInterrupted('Catalog navigation yielded to the playing set.')
                    track=arg['track']
                    self.session.availability.require_track(track)
                    def latch():
                        self.check()
                        self.session.availability.require_track(track)
                        # Retain ownership even when the native send/readback
                        # is uncertain. The counter prohibits catalog retries
                        # from replaying a dispatched Load.
                        self.requested_load=track['engine']['selection_id']
                        self.loaded=None
                        self.load_dispatches+=1
                    if arg.get('restored'):
                        recording,target=arg['restored']
                        dispatch_exact_load(client,self.deck,recording,track['engine']['selection_id'],
                                            arg['folderId'],target,before_dispatch=latch)
                    else:
                        require_selection(client,track['engine']['selection_id'],arg['folderId'])
                        latch()
                        client.send(f'deck {self.deck} play ? nothing : deck {self.deck} load')
                    value=None
                else:value=client.query(script,arg) if kind=='query' else client.send(script)
                self.serviced+=1
            except Exception as exc:value=exc
            response.put(value)
    def load(self,helper,client,track,deck):
        self.session.availability.require_track(track)
        for attempt in range(3):
            epoch=self.browser_epoch
            dispatches=self.load_dispatches
            try:
                restored=helper.locate_for_load(client,track,deck)
                self.request('load','',{'track':track,'browserEpoch':epoch,'folderId':helper.folder['id'],
                                        'restored':restored})
                break
            except BridgeError as exc:
                if (isinstance(exc,ExactRestoreCleanupUnconfirmed) or self.load_dispatches!=dispatches
                        or epoch==self.browser_epoch or attempt==2):raise
                self.check()
        deadline=self.clock()+20
        while self.clock()<deadline:
            self.wait(.15)
            try:
                failed=as_bool(client.query(f'deck {self.deck} deck_has_error','bool'))
                loaded=as_bool(client.query(f'deck {self.deck} loaded','bool'))
                sid=identity(str(client.query(f'deck {self.deck} get_filepath','text'))) if loaded else None
            except (BridgeError,OSError):
                # A streamed load can briefly occupy the host decoder. Retry
                # only its bounded readbacks, never the uncertain Load action.
                self.check();continue
            if failed:raise BridgeError('Stream could not be prepared on the analysis deck.')
            if sid==track['engine']['selection_id']:
                self.loaded=sid
                self.request('isolate-loaded','',sid)
                return
        raise BridgeError('Background stream load was not confirmed; no repeat load was sent.')
    def run(self):
        from afterhours.vdj_session import DJSession, RecordingUnavailable
        from afterhours.cue_planner import LimitedAudio
        helper=DJSession(threading.Lock(),crate=getattr(self.session,'crate',None),
                         recordings=getattr(self.session,'recordings',None),
                         availability=self.session.availability);s=self.session.snapshot()
        helper.state.update(candidates=copy.deepcopy(s['candidates']),maxTempoShift=s.get('maxTempoShift',8))
        helper.positions=copy.deepcopy(self.session.positions);helper.folder=copy.deepcopy(self.session.folder)
        helper.check=self.check;helper.sleep=self.wait
        helper.load=lambda client,track,deck:self.load(helper,client,track,deck)
        client=MailboxClient(self)
        try:
            for track in self.tracks:
                self.session.availability.require_track(track)
                self.check();self.status='analyzing';self.message='Analyzing upcoming track · '+track['title']
                cached=self.cache.get(track['id'])
                if cached and cached.get('cueCandidates') and cached.get('preparationSeconds')==s['secondsPerTrack']:
                    try:
                        from afterhours import cue_planner
                        cue_planner.measured_rows(cached);self.result.append(cached);continue
                    except (ValueError,OSError):pass
                checked=helper.analyze_deck(client,track,self.deck,clock=self.clock)
                evidence=track_analysis([checked])['tracks'][0]
                waveform_ready = bool(evidence.get('energyMap')) and verified_waveform(
                    track['engine']['selection_id'],evidence['duration'],evidence['energyMap']['reportDigest'])
                if not waveform_ready:
                    # Keep the waveform and features bound to the same decode.
                    end=next((p['seconds'] for p in evidence['points'] if p['label']=='realEnd'),None)
                    scan_track(client,checked,end,check=self.check,wait=self.wait,clock=self.clock,timeout=120,deck=self.deck,isolated=True)
                    evidence=track_analysis([checked])['tracks'][0]
                    self.message='Preparing waveform · '+track['title']
                    scan_waveform(client,checked,evidence,check=self.check,wait=self.wait,clock=self.clock,timeout=120,deck=self.deck,isolated=True)
                self.message='Verifying mix cues · '+track['title']
                evidence['cueCandidates']=helper.measure_cues(client,checked,evidence,checked['bpm'],s['secondsPerTrack'],deck=self.deck,isolated=True)
                evidence['preparationSeconds']=s['secondsPerTrack']
                self.result.append(evidence);self.cache[track['id']]=copy.deepcopy(evidence)
                self.session.availability.verified(track['id'])
            self.status='complete';self.message='Upcoming recordings analyzed; validating their handoffs.'
        except AnalysisCancelled:
            self.status='cancelled';self.message='Background preparation paused; verified measurements were kept.'
        except (RecordingUnavailable,LimitedAudio) as exc:
            code='recording-unavailable' if isinstance(exc,RecordingUnavailable) else 'limited-decoded-audio'
            self.session.availability.record(track['id'],code,str(exc))
            self.status='blocked';self.error=str(exc);self.message=str(exc)
        except Exception as exc:
            self.status='blocked';self.error=str(exc);self.message=str(exc)
        finally:self.done=True
    def close(self,client):
        self.cancelled.set()
        if self.thread:self.thread.join(.3)
        if not self.owned:return
        if self.cleanup_state.get('error'):
            raise BridgeError(self.cleanup_state['error'])  # Only explicit Stop retries retained cleanup.
        descriptor=self.cleanup_descriptors()[0]
        try:
            cleanup_analysis_deck(client,descriptor)
        except (BridgeError,OSError,ValueError) as exc:
            descriptor['error']=str(exc)[:300]
            raise
        else:self.owned=False
        finally:self.cleanup_state=descriptor
    def cleanup_descriptors(self):
        if not self.owned:return []
        return [{'version':1,'ownerId':self.owner_id,'deck':self.deck,
                 'selectionId':self.loaded or self.requested_load,'baseline':copy.deepcopy(self.baseline),
                 'loadPending':self.cleanup_state.get('loadPending',self.loaded is None and self.requested_load is not None),
                 'unloadRequested':self.cleanup_state.get('unloadRequested',False),
                 'restoreRequested':self.cleanup_state.get('restoreRequested',False),
                 'error':self.cleanup_state.get('error',''),
                 **({'fourDeck':True} if self.deck in (1,2) else {})}]
    def snapshot(self):
        return {'revision':self.revision,'status':self.status,'message':self.message,
                'completed':len(self.result),'total':len(self.tracks),'deck':self.deck,'operations':self.serviced,
                'suspendedForMix':self.suspended,'audioSaved':False}
