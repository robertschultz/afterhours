"""Allowlisted manual controls and native console readbacks. No arbitrary script API."""
import math
import re
import time
from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.vdj_catalog import identity


def numeric(value, low, high):
    if isinstance(value, bool):
        raise BridgeError('Expected a numeric engine reading.')
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise BridgeError('Engine reading unavailable.') from None
    if not math.isfinite(value) or not low <= value <= high:
        raise BridgeError('Engine reading outside its expected range.')
    return value


def track_id(client, deck):
    if not as_bool(client.query(f'deck {deck} loaded', 'bool')):
        return None
    return 'virtualdj:' + identity(str(client.query(f'deck {deck} get_filepath', 'text')))


def read_controls(client, deck, loaded):
    """Optional fields fail independently. Never invent a neutral control."""
    values, missing = {}, []
    def read(name, script, kind=None, bounds=None):
        try:
            v = client.query(f'deck {deck} {script}', kind)
            if kind == 'bool': v = as_bool(v)
            elif bounds is not None: v = numeric(v, *bounds)
            elif kind == 'text': v = str(v)[:120]
            values[name] = v
        except BridgeError:
            missing.append(name)
    for name, script in [('high','eq_high'),('mid','eq_mid'),('low','eq_low'),
                         ('filter','filter'),('volume','volume')]:
        read(name, script, bounds=(0,1))
    for name, script in [('highLabel','high_label'),('midLabel','mid_label'),
                         ('lowLabel','low_label'),('filterLabel','filter_label "name"')]:
        read(name, script, 'text')
    read('pitchRange','pitch_range',bounds=(.001,1))
    read('keyLock','key_lock','bool')
    read('master','masterdeck','bool')
    read('headphones','pfl','bool')
    if loaded:
        # OSC get_pitch reports percentage points (2 means +2%), unlike
        # pitch_range and slider positions, which are normalized fractions.
        read('pitch','get_pitch',bounds=(-100,100))
        if 'pitch' in values: values['pitch']/=100
        read('originalBpm','get_bpm absolute',bounds=(1,500))
        read('durationMs','get_time total absolute',bounds=(0,86_400_000))
        read('key','get_key "harmonic"','text')
        read('loop','loop','bool')
        read('loopBeats','get_loop',bounds=(.03125,1024))
        read('activeCue','get_cue',bounds=(0,99))
        cues=[]
        for slot in range(1,9):
            try:
                exists=as_bool(client.query(f'deck {deck} has_cue {slot}','bool'))
                cue={'slot':slot,'set':exists}
                if exists:
                    cue['seconds']=numeric(client.query(f'deck {deck} cue_pos {slot} mseconly'),0,86_400_000)/1000
                    cue['name']=str(client.query(f'deck {deck} cue_name {slot}','text'))[:80]
                cues.append(cue)
            except BridgeError:
                cues.append({'slot':slot,'unavailable':True})
        values['cues']=cues
    values['sampledAt']=time.time()
    values['unavailable']=missing
    return values


def bounded_controls(client,deck,loaded):
    """Optional UI reads may use at most 50 ms of a live engine timing loop."""
    if not hasattr(client,'deadline'):return read_controls(client,deck,loaded)
    original=client.deadline
    client.deadline=min(original or math.inf,time.monotonic()+.05)
    try:return read_controls(client,deck,loaded)
    finally:client.deadline=original


def live_controls(client, deck, cached):
    """Refresh physical faders/EQ without rereading cue names and metadata.

    Failed values become unavailable, rather than painting old knob positions
    with a fresh timestamp. This is a read-only, 15 ms bounded observation.
    """
    values = dict(cached)
    missing = set(values.get('unavailable', []))
    original = getattr(client, 'deadline', None)
    if hasattr(client, 'deadline'):
        client.deadline = min(original or math.inf, time.monotonic() + .015)
    try:
        for name, script in [('high','eq_high'),('mid','eq_mid'),('low','eq_low'),('filter','filter'),('volume','volume')]:
            try:
                values[name] = numeric(client.query(f'deck {deck} {script}'), 0, 1)
                missing.discard(name)
            except BridgeError:
                values.pop(name, None)
                missing.add(name)
        values['liveSampledAt'] = time.time()
        values['unavailable'] = sorted(missing)
        return values
    finally:
        if hasattr(client, 'deadline'):client.deadline = original


SLIDERS={'high':'eq_high','mid':'eq_mid','low':'eq_low','filter':'filter','volume':'volume','crossfader':'crossfader'}
TOGGLES={'key-lock':'key_lock','headphones':'pfl','master':'masterdeck'}
CONTROL_NAMES=set(SLIDERS)|set(TOGGLES)|{'play','pause','cue','hotcue','sync','pitch','loop-exit'}


def validate_control(data):
    if not isinstance(data,dict) or set(data)-{'action','control','deck','value','slot','expectedTrackId','observedAt'}:
        raise ValueError('Unsupported console control fields.')
    name=data.get('control');deck=data.get('deck')
    if not isinstance(name,str) or name not in CONTROL_NAMES:
        raise ValueError('Unknown console control.')
    if type(deck) is not int or deck not in (1,2):
        raise ValueError('Select deck A or B.')
    expected=data.get('expectedTrackId')
    if 'expectedTrackId' not in data or expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'virtualdj:[a-f0-9]{64}',expected)):
        raise ValueError('The console must identify the currently observed recording.')
    observed=data.get('observedAt')
    if type(observed) not in (float,int) or not math.isfinite(observed) or not -2<=time.time()-observed<3:
        raise ValueError('Console readings expired. Wait for live readings before using this control.')
    value=data.get('value')
    if name in SLIDERS or name=='pitch':
        low=-1 if name=='pitch' else 0
        if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=1:
            raise ValueError('Control value is outside its range.')
    elif name in TOGGLES and type(value) is not bool:
        raise ValueError('Choose an explicit on or off state.')
    if name=='hotcue' and (type(data.get('slot')) is not int or not 1<=data['slot']<=8):
        raise ValueError('Select hot cue 1–8.')
    return name,deck,value


def perform_control(client,data):
    """Dispatch once, then query. Lost acknowledgement never replays a command."""
    name,deck,value=validate_control(data)
    if as_bool(client.query('automix','bool')):
        raise ValueError('Turn off VirtualDJ Automix before using console controls.')
    expected=data['expectedTrackId']
    if track_id(client,deck)!=expected:
        raise ValueError('The loaded track changed. Wait for the console to refresh.')
    p=f'deck {deck} '
    needs_track=name not in set(SLIDERS)|set(TOGGLES)
    if expected is None and needs_track:
        raise ValueError('Load a track on this deck first.')
    other_id=None
    if name=='sync':
        other_id=track_id(client,3-deck)
        if other_id is None:raise ValueError('Load the other deck before synchronizing.')
    if name in SLIDERS:
        getter=SLIDERS[name];prefix='' if name=='crossfader' else p
        script=f'{prefix}{getter} {value*100:.5f}%'
        check=lambda:abs(numeric(client.query(prefix+getter),0,1)-value)<.005
    elif name in TOGGLES:
        script=p+TOGGLES[name]+(' on' if value else ' off')
        check=lambda:as_bool(client.query(p+TOGGLES[name],'bool'))==value
    elif name=='pitch':
        limit=numeric(client.query(p+'pitch_range'),.001,1)
        if abs(value)>limit+.00001:raise ValueError('Requested pitch exceeds the current VirtualDJ pitch range.')
        script=p+f'pitch {(1+value)*100:.5f}%'
        check=lambda:abs(numeric(client.query(p+'get_pitch'),-100,100)/100-value)<.001
    elif name in {'play','pause'}:
        script=p+name;check=lambda:as_bool(client.query(p+'play','bool'))==(name=='play')
    elif name=='sync':
        script=p+'sync';check=lambda:as_bool(client.query(p+'is_sync','bool'))
    elif name=='loop-exit':
        script=p+'loop_exit';check=lambda:not as_bool(client.query(p+'loop','bool'))
    elif name=='cue':
        playing=as_bool(client.query(p+'play','bool'))
        cue=int(numeric(client.query(p+'get_cue'),0,99)) if playing else 0
        target=numeric(client.query(p+(f'cue_pos {cue} mseconly' if playing else 'get_time elapsed absolute')),0,86_400_000)
        script=p+'pause & '+p+'goto_cue' if playing else p+'set_cue 0'
        check=lambda:not as_bool(client.query(p+'play','bool')) and abs(numeric(client.query(p+('get_time elapsed absolute' if playing else 'cue_pos 0 mseconly')),0,86_400_000)-target)<150
    else:
        slot=data['slot'];exists=as_bool(client.query(p+f'has_cue {slot}','bool'))
        target=numeric(client.query(p+(f'cue_pos {slot} mseconly' if exists else 'get_time elapsed absolute')),0,86_400_000)
        script=p+(f'hot_cue {slot}' if exists else f'set_cue {slot}')
        check=lambda:as_bool(client.query(p+f'has_cue {slot}','bool')) and (not exists or as_bool(client.query(p+'play','bool'))) and abs(numeric(client.query(p+('get_time elapsed absolute' if exists else f'cue_pos {slot} mseconly')),0,86_400_000)-target)<1200
    # Recheck immediately before dispatch; serial HTTP work cannot replace the
    # deck meanwhile. A physical load can still race, which postflight detects.
    if track_id(client,deck)!=expected or other_id is not None and track_id(client,3-deck)!=other_id:
        raise ValueError('A deck changed while this control was prepared. No command sent.')
    client.send(script)
    verified=False
    try:
        for attempt in range(3):
            if check():verified=True;break
            if attempt<2:time.sleep(.08)
        verified=verified and track_id(client,deck)==expected
        if other_id is not None:verified=verified and track_id(client,3-deck)==other_id
    except (BridgeError,OSError):
        verified=False
    label=f'Deck {"A" if deck==1 else "B"} {name.replace("-"," ")}' if name!='crossfader' else 'Crossfader'
    return {'control':name,'deck':deck,'sent':True,'verified':verified,
            'message':label+(' confirmed by VirtualDJ.' if verified else ' sent; readback not confirmed. Inspect VirtualDJ before retrying.')}
