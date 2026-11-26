"""Bounded DJ transition recipes; native ramps execute timing, not an LLM.

Native stems protect a lead handoff when high-quality separation is ready.
This controls competing parts; it does not identify vocals or musical phrases.
Frequency-specific EQ remains the explicit, less selective fallback.
"""
from afterhours import deck_rotation
import math
import re
import time
from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.vdj_catalog import identity


CONTROLS = ('eq_low_freq', 'eq_mid_freq', 'eq_high_freq', 'level')
LEAD_CONTROLS = ('stem Vocal', 'stem Instru')
LEAD_POLICY = 'single-lead-v1'


class MixerControlChanged(BridgeError):
    """An operator changed the owned mixer before a blend was dispatched."""


def transition_recipe(outgoing, incoming, style='phrase', seconds=None, bpm=None, *, quick_blend_seconds=None):
    if style == 'quick':
        fade=8
        reason='Short listening preview using the native crossfader.'
        if quick_blend_seconds is not None:
            fade=4*round(quick_blend_seconds*bpm/240)
            reason='Native crossfader blend aligned to four-beat bars.'
        return {'technique':'crossfade','label':'Quick crossfade','reason':reason,
                'fadeBeats':fade,'phraseBeats':4}
    a = re.fullmatch(r'(1[0-2]|[1-9])([AB])', outgoing.get('camelot') or '')
    b = re.fullmatch(r'(1[0-2]|[1-9])([AB])', incoming.get('camelot') or '')
    compatible = False
    if a and b:
        gap = abs(int(a[1])-int(b[1]));gap = min(gap,12-gap)
        compatible = gap == 0 or (a[2] == b[2] and gap == 1)
    def family(track):
        genre=re.sub(r'[^a-z0-9]', '', (track.get('genre') or '').lower())
        if 'drumandbass' in genre or 'drumbass' in genre or genre=='dnb':return 'dnb'
        if 'techno' in genre:return 'techno'
        if 'house' in genre or 'progressive' in genre or 'trance' in genre:return 'house'
        if any(g in genre for g in ('hiphop','rnb','pop','openformat')):return 'short'
        return 'unknown'
    first,second=family(outgoing),family(incoming)
    genre=first if first==second else 'unknown'
    profiles={
        'dnb':('D&B bass swap',64,64,.85),
        'house':('House blend',128 if compatible else 64,64,.8),
        'techno':('Percussion blend',64,64,.8),
        'short':('Short bass handoff',16,16,.75),
        'unknown':('Restrained handoff',32,32,.75),
    }
    label,fade,block,entry=profiles[genre]
    # A compatible key never establishes that two voices or lead lines fit.
    # No trusted, time-specific vocal/lead evidence exists in this version.
    reason=f'{label}: hand over one lead at a time; vocal and melody activity is unverified.'
    result = {'technique':'bass-swap' if compatible else 'restrained-bass-swap',
            'label':label,'reason':reason,'fadeBeats':fade,'phraseBeats':block,
            'entryLevel':entry,'genreFamily':genre,'tonalOverlap':False,
            'harmonicCompatible':compatible,'leadPolicy':LEAD_POLICY,
            'basis':'catalog genre and native key estimates; no vocal or section detection'}
    if seconds is not None and seconds <= 30:
        # Leave at least half the trial interval for the next track to load.
        # These are shortened audition mixes, not detected musical phrases.
        cap = max(8, min(32, int(seconds * bpm / 60 / 2 / 8) * 8))
        result.update(fadeBeats=min(fade, cap), phraseBeats=4, trial=True)
        result['reason'] += ' Short trial: overlap is capped to leave preload time.'
    return result


def value(raw):
    if isinstance(raw, bool):
        raise BridgeError('Invalid mixer control reading.')
    try:
        result = float(raw)
    except (ValueError,TypeError) as exc:
        raise BridgeError('Missing mixer control reading.') from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise BridgeError('Mixer control reading is outside 0–1.')
    return result


def loop_cleanup_descriptor(value):
    """Only an exact native recording and supported loop size may be recovered."""
    if (not isinstance(value, dict) or type(value.get('deck')) is not int
            or value['deck'] not in deck_rotation.FOUR or type(value.get('beats')) is not int
            or value['beats'] not in (4, 8) or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('recording')))):
        raise BridgeError('Invalid retained loop ownership.')
    start = value.get('startMs')
    if start is not None and (type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 86_400_000):
        raise BridgeError('Invalid retained loop marker.')
    return {key: value.get(key) for key in ('deck', 'recording', 'beats', 'startMs')}


class NativeHandoff:
    """One explicitly addressed start and one owned native crossfade.

    The pair is one step of the set's deck rotation: A/B alternating, or
    A, B, C, D in order on a four-deck layout. Odd decks sit on the left of
    the crossfader and even decks on the right; both assignments are read back
    before anything starts, and in a four-deck rotation no other deck may be
    playing, because the fade would bring it up on the incoming side.

    A global mix_now can choose a selected preparation deck in a four-deck
    setup. Keep transport, sync reference and the fade's beat clock explicit;
    a missing acknowledgement must never cause another Play or fade.
    """
    def __init__(self, query, send, sleep):
        self.q, self.send, self.sleep = query, send, sleep
        self.ramps = {}
        self.dispatched = False
        self.loop = None

    def dispatch(self, client, outgoing, incoming, beats, decks=deck_rotation.TWO, *, loop_recording=None):
        if self.dispatched:
            raise BridgeError('This transition was already dispatched. No repeat was sent.')
        if (decks not in (deck_rotation.TWO,deck_rotation.FOUR) or not deck_rotation.pair(outgoing,incoming,decks)
                or type(beats) is not int or not 1 <= beats <= 256):
            raise BridgeError('Invalid addressed transition.')
        start=value(self.q(client,'crossfader'))
        end=deck_rotation.crossfader_end(incoming)
        if abs(start-(1-end))>.02:
            raise MixerControlChanged('Crossfader moved before the transition. No playback was started.')
        if (not as_bool(self.q(client,f'deck {outgoing} play','bool')) or
                as_bool(self.q(client,f'deck {incoming} play','bool'))):
            raise MixerControlChanged('Deck playback changed before the transition. No playback was started.')
        if not all(as_bool(self.q(client,f"deck {deck} cross_assign '{deck_rotation.side(deck)}'",'bool'))
                   for deck in sorted((outgoing,incoming))):
            raise MixerControlChanged('Deck crossfader assignments changed. No playback was started.')
        if any(as_bool(self.q(client,f'deck {deck} play','bool')) for deck in decks if deck not in (outgoing,incoming)):
            raise MixerControlChanged('Another deck is playing. No playback was started.')
        # play_sync uses the selected master as reference on four-deck skins.
        # Setting it does not seek or restart the outgoing recording.
        self.send(client,f'deck {outgoing} masterdeck on')
        if not as_bool(self.q(client,f'deck {outgoing} masterdeck','bool')):
            raise BridgeError('The outgoing sync reference was not confirmed. Playback was not started.')
        if loop_recording is not None:
            self.engage_loop(client, outgoing, loop_recording, 8 if beats >= 16 else 4)
        self.dispatched=True
        self.send(client,f'deck {incoming} play_sync')
        for attempt in range(4):
            if as_bool(self.q(client,f'deck {incoming} play','bool')):
                break
            if attempt<3:self.sleep(.05)
        else:
            raise BridgeError('Incoming playback was not confirmed. No fade or repeat Play was sent.')
        # Do not make a silent or changed pair audible after an uncertain start.
        if (not as_bool(self.q(client,f'deck {outgoing} play','bool')) or
                not as_bool(self.q(client,f'deck {outgoing} masterdeck','bool')) or
                abs(value(self.q(client,'crossfader'))-start)>.02):
            raise MixerControlChanged('The playing pair changed after start. No crossfade was sent.')
        clock = incoming if self.loop else outgoing
        self.ramps['crossfade']=clock
        self.send(client,f"deck {clock} repeat_start_fade 'afterhours_crossfade' {start:.6f} {end:.6f} {beats}bt & crossfader")

    def engage_loop(self, client, deck, recording, beats):
        prefix = f'deck {deck} '
        if identity(str(self.q(client, prefix+'get_filepath', 'text'))) != recording:
            raise MixerControlChanged('The outgoing recording changed before loop entry.')
        if as_bool(self.q(client, prefix+'loop', 'bool')):
            raise MixerControlChanged('The outgoing deck already has a loop. No loop or Play was sent.')
        self.loop = {'deck': deck, 'recording': recording, 'beats': beats, 'startMs': None}
        self.send(client, prefix+f'loop {beats}')
        if (not as_bool(self.q(client, prefix+'loop', 'bool'))
                or float(self.q(client, prefix+'get_active_loop')) != beats):
            raise BridgeError('The outgoing beat loop was not confirmed. Incoming playback was not started.')
        start = float(self.q(client, prefix+'get_loop_in_time'))
        if not math.isfinite(start) or not 0 <= start <= 86_400_000:
            raise BridgeError('The outgoing loop marker was not confirmed.')
        self.loop['startMs'] = start

    def release_loop(self, client):
        if self.loop is None:
            return None
        loop = self.loop
        prefix = f"deck {loop['deck']} "
        if (identity(str(client.query(prefix+'get_filepath', 'text'))) != loop['recording']
                or not as_bool(client.query(prefix+'loop', 'bool'))):
            self.loop = None
            return 'already-inactive'
        if loop['startMs'] is None:
            raise BridgeError('Outgoing loop ownership is unconfirmed. Inspect the deck.')
        if (float(client.query(prefix+'get_active_loop')) != loop['beats']
                or abs(float(client.query(prefix+'get_loop_in_time'))-loop['startMs']) > 3):
            self.loop = None
            return 'ownership-changed'
        client.send(prefix+'loop_exit')
        if as_bool(client.query(prefix+'loop', 'bool')):
            raise BridgeError('Outgoing loop release was not confirmed.')
        self.loop = None
        return 'released'

    def cancel_ramps(self, client):
        if self.ramps:
            # Bypass a cancelled worker's send guard, exactly as owned EQ ramps.
            client.send(f"deck {self.ramps['crossfade']} repeat_stop 'afterhours_crossfade'")
            self.ramps.clear()


class TransitionMixer:
    def __init__(self, query, send):
        self.q, self.send = query, send
        self.baselines = {}
        self.ramps = {}
        self.done = set()
        self.recipe = None
        self.incoming = None
        self.outgoing = None
        self.lead_mode = None
        self.armed = None
        self.resume_controls = {}

    def read(self, client, deck):
        return {name:value(self.q(client,f'deck {deck} {name}')) for name in CONTROLS+LEAD_CONTROLS}

    def stems_ready(self, client, deck):
        # Readiness is for the current playhead, not the whole recording.
        return (as_bool(self.q(client,f'deck {deck} has_stems "2.0"','bool')) and
                as_bool(self.q(client,f'deck {deck} has_stems "ready"','bool')))

    def preflight(self, client):
        if not as_bool(self.q(client,'mixermode','bool')):
            raise BridgeError('EQ mixing needs VirtualDJ internal mixer mode.')
        if as_bool(self.q(client,'fake_eq','bool')) or as_bool(self.q(client,'fake_mixer','bool')):
            raise BridgeError('VirtualDJ EQ/mixer processing is bypassed. Restore internal processing before EQ mixing.')
        for deck in (1,2):
            if as_bool(self.q(client,f'deck {deck} play','bool')):
                raise BridgeError('Pause both decks before initializing EQ mixing.')
            controls = self.read(client,deck)
            if any(abs(controls[k]-.5)>.025 for k in CONTROLS[:3]+LEAD_CONTROLS) or controls['level']<.1:
                raise BridgeError('Restore both decks’ frequency EQ and vocal/instrument stems to neutral, and raise their channel faders before mixing.')
            self.baselines[deck] = controls

    def initialize_opening(self,client):
        """An explicit Start music hands paused channels to the automatic mixer.

        Keep the outgoing channel's usable level and the master output setting.
        Reset frequency EQ/stems/filter, and give the cued channel the same level
        behind the already-confirmed closed crossfader. Never normalize live audio.
        """
        if not as_bool(self.q(client,'mixermode','bool')) or as_bool(self.q(client,'fake_eq','bool')) or as_bool(self.q(client,'fake_mixer','bool')):
            raise BridgeError('Internal mixer processing is required before starting music.')
        if value(self.q(client,'crossfader'))>.02:
            raise BridgeError('The opening crossfader was not confirmed. Playback remains stopped.')
        level=self.read(client,1)['level'];level=level if level>=.1 else .5
        for deck in (1,2):
            if any(as_bool(self.q(client,f'deck {d} play','bool')) for d in (1,2)):
                raise BridgeError('A deck started manually. Opening mixer setup stopped.')
            self.set_controls(client,deck,{**{k:.5 for k in CONTROLS[:3]+LEAD_CONTROLS},'level':level})
            self.send(client,f'deck {deck} filter 50%')
            if abs(value(self.q(client,f'deck {deck} filter'))-.5)>.025:
                raise BridgeError('The opening filter was not confirmed. Playback remains stopped.')
        self.preflight(client)

    def adopt(self, client, deck, level, audible):
        """Bring a deck the set has not used yet under the automatic mixer.

        On a four-deck layout C and D start outside the opening preflight, and
        may have been left with the fader down by track preparation. The deck
        must be paused and closed off by the crossfader, so nothing here can be
        heard: it gets neutral EQ, stems and filter, and the playing level.
        """
        cross=value(self.q(client,'crossfader'))
        if as_bool(self.q(client,f'deck {deck} play','bool')) or abs(cross-deck_rotation.crossfader_end(audible))>.02:
            raise BridgeError(f'Deck {deck_rotation.letter(deck)} is not confirmed silent; its mixer controls were not changed.')
        controls=self.set_controls(client,deck,{**{k:.5 for k in CONTROLS[:3]+LEAD_CONTROLS},'level':level if level>=.1 else .5})
        self.send(client,f'deck {deck} filter 50%')
        if abs(value(self.q(client,f'deck {deck} filter'))-.5)>.025:
            raise BridgeError(f'Deck {deck_rotation.letter(deck)} filter was not confirmed.')
        self.baselines[deck]=controls

    def resume_preflight(self, client, playing, baselines, adopt_playing=False, paused_current=False, decks=deck_rotation.TWO):
        """Resume an owned set without changing the audible channel.

        Keep the original neutral baselines for future silent-deck resets.
        An explicit handback may accept the playing channel's current controls
        as the start of this fade, never as the next track's neutral baseline.
        Only the confirmed silent, paused deck is restored here.
        """
        if playing not in decks or not isinstance(baselines,dict):
            raise BridgeError('The original mixer baselines are unavailable. Prepare a new stopped set.')
        if not as_bool(self.q(client,'mixermode','bool')) or as_bool(self.q(client,'fake_eq','bool')) or as_bool(self.q(client,'fake_mixer','bool')):
            raise BridgeError('Internal mixer processing is required to resume.')
        checked={}
        paused=deck_rotation.following(playing,decks)
        for deck in decks:
            saved=baselines.get(deck,baselines.get(str(deck)))
            # The opening pair always has baselines; C and D only once used.
            if saved is None and deck not in (1,2,playing):continue
            saved=saved or {}
            checked[deck]={name:value(saved.get(name)) for name in CONTROLS+LEAD_CONTROLS}
            if any(abs(checked[deck][k]-.5)>.025 for k in CONTROLS[:3]+LEAD_CONTROLS) or checked[deck]['level']<.1:
                raise BridgeError('The original neutral mixer baseline is invalid.')
        actual=self.read(client,playing)
        if adopt_playing and actual['level'] < .1:
            raise BridgeError('The playing channel fader is down. Raise it before handing the mix back.')
        if not adopt_playing and any(abs(actual[k]-v)>.03 for k,v in checked[playing].items()):
            raise BridgeError('The playing mixer controls changed. Restore them before resuming Copilot.')
        cross=value(self.q(client,'crossfader'))
        if abs(cross-deck_rotation.crossfader_end(playing))>.02:
            raise BridgeError('The incoming channel is not confirmed silent.')
        if any(as_bool(self.q(client,f'deck {deck} play','bool')) for deck in decks if deck not in (playing,paused)):
            raise BridgeError('Another deck is playing. Resume is held.')
        if as_bool(self.q(client,f'deck {playing} play','bool')) == paused_current or as_bool(self.q(client,f'deck {paused} play','bool')):
            raise BridgeError('The playing pair changed during resume checks.')
        self.baselines=checked
        self.resume_controls={playing:actual} if adopt_playing else {}
        if paused in checked:self.reset_paused(client,paused)
        else:self.adopt(client,paused,checked[playing]['level'],playing)

    def set_controls(self, client, deck, controls):
        script=' & '.join(f'deck {deck} {name} {amount*100:.4f}%' for name,amount in controls.items())
        self.send(client,script)
        actual=self.read(client,deck)
        if any(abs(actual[k]-v)>.025 for k,v in controls.items()):
            raise BridgeError('Mixer setting was not confirmed; it was not replayed.')
        return actual

    def arm(self, client, outgoing, incoming, recipe):
        if as_bool(self.q(client,f'deck {incoming} play','bool')):
            raise BridgeError('Incoming deck started manually; EQ was not changed.')
        self.outgoing,self.incoming,self.recipe = outgoing,incoming,recipe
        self.done = set()
        if outgoing not in self.baselines:
            raise BridgeError('The playing deck has no mixer baseline. Prepare a new stopped set.')
        if incoming not in self.baselines:self.adopt(client,incoming,self.baselines[outgoing]['level'],outgoing)
        controls = self.read(client,outgoing)
        expected=self.resume_controls.get(outgoing,self.baselines[outgoing])
        if any(abs(controls[k]-expected[k])>.03 for k in CONTROLS+LEAD_CONTROLS):
            raise MixerControlChanged('Playing deck mixer controls changed.')
        incoming_controls=self.read(client,incoming)
        if any(abs(incoming_controls[k]-.5)>.025 for k in LEAD_CONTROLS):
            raise BridgeError('Incoming vocal/instrument controls changed; restore them before preparing a mix.')
        ready={d:self.stems_ready(client,d) for d in (outgoing,incoming)}
        self.lead_mode='stems' if all(ready.values()) else 'frequency-eq'
        # Record the actual control path in the reusable session evidence.
        recipe.update(leadPolicy=LEAD_POLICY,tonalOverlap=False,leadControl=self.lead_mode,
                      leadControlNote=('Native vocal and instrument handoff; phrase boundaries remain unverified.'
                                       if self.lead_mode=='stems' else
                                       'High-quality stems not ready; midrange EQ reduces overlap but cannot isolate vocals.'))
        settings={'eq_low_freq':0, 'eq_mid_freq':.5 if self.lead_mode=='stems' else 0,
                  'eq_high_freq':.5, 'level':self.baselines[incoming]['level']*recipe['entryLevel']}
        if self.lead_mode=='stems':settings.update(dict.fromkeys(LEAD_CONTROLS,0))
        actual=self.set_controls(client,incoming,settings)
        self.armed={outgoing:controls,incoming:actual}
        self.resume_controls.pop(outgoing,None)
        return actual

    def fallback_from_unready_stems(self, client):
        if self.lead_mode!='stems' or all(self.stems_ready(client,d) for d in (self.outgoing,self.incoming)):
            return False
        if (as_bool(self.q(client,f'deck {self.incoming} play','bool'))
                or abs(value(self.q(client,'crossfader'))-deck_rotation.crossfader_end(self.outgoing))>.02):
            raise MixerControlChanged('The incoming channel is no longer silent. No transition was started.')
        self.validate_armed(client,check_stems=False)
        self.armed[self.incoming]=self.set_controls(client,self.incoming,
            {'eq_mid_freq':0,**dict.fromkeys(LEAD_CONTROLS,.5)})
        self.lead_mode='frequency-eq'
        self.recipe.update(leadControl=self.lead_mode,
            leadControlNote='Stems lost readiness before the mix; midrange EQ reduces overlap but cannot isolate vocals.')
        return True

    def validate_armed(self, client, *, check_stems=True):
        # Called before Mix, not after an incoming deck has become audible.
        if check_stems and self.lead_mode=='stems' and not all(self.stems_ready(client,d) for d in (self.outgoing,self.incoming)):
            raise BridgeError('High-quality stems are no longer ready. No transition was started.')
        for deck,target in self.armed.items():
            actual=self.read(client,deck)
            if any(abs(actual[k]-v)>.03 for k,v in target.items()):
                raise MixerControlChanged('Armed mixer controls changed. No transition was started.')

    def targets(self):
        """Declared controls this mixer will move, for display only."""
        if self.incoming is None or self.outgoing is None:return None
        return {str(self.incoming): {'fader':self.baselines[self.incoming]['level'], 'low':.5},
                str(self.outgoing): {'low':0}}

    def ramp(self, client, name, deck, control, start, end, beats):
        # Each bounded native ramp has a unique owned name and is cancellable.
        self.ramps[name]=deck
        self.send(client,f"deck {deck} repeat_start_fade 'afterhours_{name}' {start:.6f} {end:.6f} {beats}bt & deck {deck} {control}")

    def begin(self, client):
        level=self.baselines[self.incoming]['level']
        beats=self.recipe['fadeBeats']
        self.ramp(client,'in_level',self.incoming,'level',level*self.recipe['entryLevel'],level,beats//2)

    def tick(self, client, beats):
        # Never compress missed steps into an uncontrolled burst.
        length=self.recipe['fadeBeats']
        steps=[(length/4,'out_low'),(length/4,'out_lead'),(length*3/8,'in_low'),
               (length/2,'in_lead'),(length*3/4,'out_high')]
        # Incoming bass depends on the *observed completion* of outgoing bass.
        # Native short ramps can settle after their nominal one-beat duration.
        # Allow a bounded extra window for that dependency, still leaving time
        # to finish the incoming ramp before the crossfade endpoint. Check all
        # missed deadlines before dispatching any new control in this tick.
        for at,name in steps:
            slack=min(4,length/4) if name in ('in_low','in_lead') else min(4,length/8)
            if beats>=at and name not in self.done and beats-at>slack:
                raise BridgeError('EQ timing window was missed. Future automation stopped; take over the mix.')
        for at,name in steps:
            if beats < at or name in self.done:
                continue
            if name=='out_low':
                self.ramp(client,name,self.outgoing,'eq_low_freq',self.armed[self.outgoing]['eq_low_freq'],0,length//8)
            elif name=='out_lead':
                if self.lead_mode=='stems':
                    for stem in ('Vocal','Instru'):
                        self.ramp(client,'out_'+stem.lower(),self.outgoing,'stem '+stem,self.armed[self.outgoing]['stem '+stem],0,length//4)
                else:
                    self.ramp(client,'out_mid',self.outgoing,'eq_mid_freq',self.armed[self.outgoing]['eq_mid_freq'],0,length//4)
            elif name=='in_low':
                # Do not bring a second bassline in until the first is down.
                if value(self.q(client,f'deck {self.outgoing} eq_low_freq'))>.03:
                    continue
                self.ramp(client,name,self.incoming,'eq_low_freq',0,.5,length//8)
            elif name=='in_lead':
                leads=LEAD_CONTROLS if self.lead_mode=='stems' else ('eq_mid_freq',)
                # Never restore the new lead just because the clock says so.
                if any(value(self.q(client,f'deck {self.outgoing} {control}'))>.03 for control in leads):
                    continue
                if any(value(self.q(client,f'deck {self.incoming} {control}'))>.03 for control in leads):
                    raise BridgeError('Incoming lead controls changed during the handoff. Automation stopped.')
                if self.lead_mode=='stems':
                    if not all(self.stems_ready(client,d) for d in (self.outgoing,self.incoming)):
                        raise BridgeError('High-quality stems lost readiness during the handoff. Take over the mix.')
                    for stem in ('Vocal','Instru'):
                        self.ramp(client,'in_'+stem.lower(),self.incoming,'stem '+stem,0,.5,length//8)
                else:
                    self.ramp(client,'in_mid',self.incoming,'eq_mid_freq',0,.5,length//8)
            elif name=='out_high':
                self.ramp(client,name,self.outgoing,'eq_high_freq',self.armed[self.outgoing]['eq_high_freq'],.2,length//4)
            self.done.add(name)
        phase = ('Introducing rhythm' if beats<length/4 else 'Clearing outgoing bass and lead' if beats<length*3/8 else
                 'Bringing in incoming bass' if beats<length/2 else 'Handing over the lead' if beats<length*3/4 else 'Clearing the outgoing track')
        return {'technique':self.recipe['label'], 'phase':phase, 'beat':beats,
                'sampledAt':time.time(),
                'leadControl':self.lead_mode,'leadControlNote':self.recipe['leadControlNote'],
                'outgoingDeck':self.outgoing, 'incomingDeck':self.incoming,
                'outgoing':self.read(client,self.outgoing), 'incoming':self.read(client,self.incoming)}

    def verify_handoff(self, client):
        if not {'out_lead','in_lead','in_low'}.issubset(self.done):
            raise BridgeError('Bass/lead handoff was not confirmed. Inspect the mixer before continuing.')
        actual=self.read(client,self.incoming)
        target={**self.baselines[self.incoming], 'eq_low_freq':.5,'eq_mid_freq':.5,'eq_high_freq':.5}
        if any(abs(actual[k]-v)>.03 for k,v in target.items()):
            raise BridgeError('Incoming EQ/level endpoint was not confirmed. Inspect the mixer before continuing.')

    def reset_paused(self, client, deck):
        if as_bool(self.q(client,f'deck {deck} play','bool')):
            raise BridgeError('Deck is still playing; its EQ was not reset.')
        self.set_controls(client,deck,self.baselines[deck])

    def cancel_ramps(self, client):
        # Direct sends intentionally ignore the session cancel flag so takeover
        # can stop already-dispatched ramps. Leave live controls at their values.
        if self.ramps:
            client.send(' & '.join(f"deck {deck} repeat_stop 'afterhours_{name}'" for name,deck in sorted(self.ramps.items())))
            self.ramps.clear()
