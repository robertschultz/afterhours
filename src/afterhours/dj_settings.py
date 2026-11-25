"""Versioned operator settings, without provider calls or transport ownership.

The session remains the authority for the current lead mode. This boundary
retains preferences and exact request receipts; restoring it never starts music.
"""
import copy
import math
from afterhours import learned_preferences
from afterhours import prompt_plan
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


LEADS = ('all', 'mix', 'off')
DEFAULT_STYLE = {'energy': 1, 'adv': 1, 'vocals': 3, 'blend': 3}
STYLE_LABELS = {
    'energy': ('Very patient', 'Patient', 'Steady', 'Quick', 'Driving'),
    'adv': ('Only known', 'Mostly known', 'Some new', 'Plenty new', 'New first'),
    'vocals': ('Almost none', 'Few', 'Spaced out', 'Lots', 'All night'),
    'blend': ('Cuts', 'Short', '16 bars', 'Long', 'Layered'),
}


def validate_style(value):
    if (not isinstance(value, dict) or set(value) != set(DEFAULT_STYLE) or
            any(type(stop) is not int or not 0 <= stop <= 4 for stop in value.values())):
        raise ValueError('Style needs Energy, Adventure, Vocals and Blends at stops 0–4.')
    return copy.deepcopy(value)


def style_context(style):
    """Bounded, honest standing preferences consumed by real planners."""
    value = validate_style(style)
    return {'stops': value, 'energyBuild': STYLE_LABELS['energy'][value['energy']],
            'adventure': STYLE_LABELS['adv'][value['adv']],
            'vocalPreference': STYLE_LABELS['vocals'][value['vocals']],
            'preferredBlendBeats': (8, 16, 64, 128, 128)[value['blend']],
            'meaning': 'Standing preferences for future choices. Current requests and tonight’s plan take priority. '
                'Known means already in this app’s crate, not personal familiarity. Unknown vocals stay unknown. '
                'Blend length is a preference within measured windows; keep protected cues and the one-lead safety guard.'}


def parse_style(value, surface, style):
    # These are whole commands, never a keyword scan of a title or question.
    names = {'energy':'energy','adventure':'adv','vocals':'vocals','blend':'blend','blends':'blend'}
    match = re.fullmatch(r'(?:set|change) (?:my |the |copilot\'s )?(energy|adventure|vocals|blends?)(?: dial)? to ([a-z0-9 ]+?)(?: by default| from now on)?', value)
    if match:
        name, raw = names[match[1]], match[2]
        labels = {label.casefold(): i for i, label in enumerate(STYLE_LABELS[name])}
        labels.update({str(i): i for i in range(5)})
        if name == 'adv': labels.update(familiar=0, discovery=4)
        if name == 'vocals': labels.update(fewer=1, more=3, none=0)
        if name == 'blend': labels.update(quick=0, longer=3, shorter=1)
        if raw in labels: return {'kind':'style','value':{'id':name,'stop':labels[raw]}}
    discovery = {'turn discovery on':True, 'turn discovery off':False,
                 'enable music discovery':True, 'disable music discovery':False,
                 'turn new music discovery on':True, 'turn new music discovery off':False}
    if value in discovery:return {'kind':'discover','value':discovery[value]}
    standing = re.fullmatch(r'(.*?) (?:by default|from now on|on every night)', value)
    if surface != 'settings' and not standing:return None
    phrase = standing[1] if standing else value
    if phrase in ('discover on','discover new music','discover off','stop discovering new music'):
        return {'kind':'discover','value':phrase in ('discover on','discover new music')}
    nudges = {
        'more energy':('energy',1),'faster builds':('energy',1),'make the builds faster':('energy',1),
        'more patient builds':('energy',-1),'slower builds':('energy',-1),'less energy':('energy',-1),
        'more adventure':('adv',1),'more new music':('adv',1),'more familiar music':('adv',-1),
        'less new music':('adv',-1),'more vocals':('vocals',1),'fewer vocals':('vocals',-1),
        'less vocals':('vocals',-1),'longer blends':('blend',1),'longer mixes':('blend',1),
        'shorter blends':('blend',-1),'shorter mixes':('blend',-1),
    }
    if phrase in ('no vocals','instrumentals only'):
        return {'kind':'style','value':{'id':'vocals','stop':0}}
    if phrase not in nudges:return None
    name, delta = nudges[phrase]
    current = validate_style(DEFAULT_STYLE if style is None else style)
    return {'kind':'style','value':{'id':name,'stop':max(0,min(4,current[name]+delta))}}


def lead(session):
    return {'leading': 'all', 'mix': 'mix', 'off': 'off'}.get(
        (session.get('copilot') or {}).get('mode'), 'all')


def selection_epoch(session):
    return (session.get('copilot') or {}).get('selectionEpoch', 0)


def local_zone():
    try:
        with open('/etc/localtime','rb') as file:
            return ZoneInfo.from_file(file)
    except (OSError,ValueError):
        return datetime.now().astimezone().tzinfo


def clock_label(timestamp):
    return datetime.fromtimestamp(timestamp,local_zone()).strftime('%I:%M %p').lstrip('0')


def validate_plan(value):
    if value is None:return None
    required={'startAt','endAt','peakAt','warm','onePeak','darkLate'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'warmLen'}:
        raise ValueError('A night plan needs its start, end, peak and energy preferences.')
    if any(type(value[k]) not in (int,float) or not math.isfinite(value[k]) or not 0<value[k]<32_503_680_000 for k in ('startAt','endAt','peakAt')):
        raise ValueError('The night plan needs valid clock times.')
    if not 1800<=value['endAt']-value['startAt']<=86400 or not value['startAt']<value['peakAt']<value['endAt']:
        raise ValueError('The peak must fall inside a set lasting 30 minutes to 24 hours.')
    if any(type(value[k]) is not bool for k in ('warm','onePeak','darkLate')):
        raise ValueError('Energy preferences must be on or off.')
    if 'warmLen' in value:
        from afterhours.set_plan import validate_warm
        validate_warm(value,value['warmLen'])
    return copy.deepcopy(value)


def parse(text,now=None,zone=None,*,surface=None,style=None):
    """Complete operator imperatives only; no keyword-based preference changes."""
    value = ' '.join(str(text).strip().casefold().replace('’', "'").split())
    value = re.sub(r'[.!]+$', '', value)
    value = re.sub(r'^(?:please |just )+', '', value)
    change = parse_style(value, surface, style)
    if change:return change
    commands = {
        'mixing only': 'mix', 'copilot mixing only': 'mix',
        'switch to mixing only': 'mix', 'set copilot to mixing only': 'mix',
        'i pick the tracks, you mix': 'mix', 'tracks and mixing': 'all',
        'copilot tracks and mixing': 'all', 'switch to tracks and mixing': 'all',
        'copilot off': 'off', 'turn copilot off': 'off',
        'copilot on': 'all', 'turn copilot on': 'all', 'resume copilot': 'all',
    }
    if value in commands:return {'kind':'lead','value':commands[value]}
    rules={'keep my queue order':('reorder',False),'allow queue reordering':('reorder',True),
           'start warm and patient':('warm',True),'lift the energy sooner':('warm',False),
           'one peak tonight':('peak',True),'no single peak tonight':('peak',False),
           'darker after the peak':('dark',True),'keep the energy up after the peak':('dark',False)}
    if value in rules:
        rule,on=rules[value];return {'kind':'rule','value':{'id':rule,'on':on}}
    if value in ('push the peak later','push the peak a bit later','move the peak later'):
        return {'kind':'peakShift','value':1800}
    if value in ('bring the peak earlier','move the peak earlier'):
        return {'kind':'peakShift','value':-1800}
    if value=='clear the night plan':return {'kind':'plan','value':None}
    token=r'(midnight|noon|\d{1,2}(?::\d{2})?\s*(?:am|pm))'
    match=re.fullmatch(r'(?:plan tonight|plan the night) from '+token+r' to '+token+r',? (?:with a )?peak (?:at )?'+token,value)
    if not match:return None
    zone=zone or local_zone();current=datetime.fromtimestamp(time.time() if now is None else now,zone)
    def hour(raw):
        if raw in ('midnight','noon'):return (0 if raw=='midnight' else 12),0
        h,m,part=re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)',raw).groups()
        if not 1<=int(h)<=12 or not 0<=int(m or 0)<=59:raise ValueError('Use a clock time such as 10 PM or 12:40 AM.')
        return int(h)%12+(12 if part=='pm' else 0),int(m or 0)
    sh,sm=hour(match[1]);eh,em=hour(match[2]);ph,pm=hour(match[3])
    start=current.replace(hour=sh,minute=sm,second=0,microsecond=0)
    end=start.replace(hour=eh,minute=em)
    if end<=start:end+=timedelta(days=1)
    # If the requested overnight set is still in progress, use that night.
    if start-timedelta(days=1)<=current<end-timedelta(days=1):
        start-=timedelta(days=1);end-=timedelta(days=1)
    elif current>=end:
        start+=timedelta(days=1);end+=timedelta(days=1)
    peak=start.replace(hour=ph,minute=pm)
    if peak<=start:peak+=timedelta(days=1)
    plan=validate_plan({'startAt':start.timestamp(),'endAt':end.timestamp(),'peakAt':peak.timestamp(),
                        'warm':True,'onePeak':True,'darkLate':False})
    return {'kind':'plan','value':plan}


def energy(plan,at):
    """Operator-authored intention, not measured audio or a promised outcome."""
    length=plan['endAt']-plan['startAt'];t=max(0,min(14400,(at-plan['startAt'])/length*14400))
    peak=(plan['peakAt']-plan['startAt'])/length*14400
    e=1.6+2.8*(t/peak)**(1.8 if plan['warm'] else .8) if t<=peak else 4.4-(1.3 if plan['onePeak'] else .3)*min(1,(t-peak)/(14400-peak))
    if plan['onePeak']:e+=.6*math.exp(-((t-peak)/1200)**2)
    if plan['darkLate'] and t>10800:e-=.5*min(1,(t-10800)/900)
    return max(.6,min(5,e))


def validate_change(change):
    if not isinstance(change, dict) or set(change) != {'kind', 'value'}:
        raise ValueError('A settings change needs a kind and value.')
    kind, value = change['kind'], change['value']
    if kind == 'lead':
        if not isinstance(value, str) or value not in LEADS:
            raise ValueError('Choose Tracks & mixing, Mixing only, or Off.')
    elif kind == 'brief':
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1600:
            raise ValueError('Use 1–1600 characters for tonight’s brief.')
        value = value.strip()
    elif kind=='plan':value=validate_plan(value)
    elif kind=='rule':
        if not isinstance(value,dict) or set(value)!={'id','on'} or value['id'] not in ('reorder','warm','peak','dark') or type(value['on']) is not bool:
            raise ValueError('Choose an available rule and an on or off state.')
        value=copy.deepcopy(value)
    elif kind=='peakShift':
        if type(value) is not int or not -7200<=value<=7200 or value==0:
            raise ValueError('Move the peak by up to two hours inside the planned night.')
    elif kind=='warmLength':
        if type(value) is not int or not 60<=value<86400:
            raise ValueError('Use a whole warm-up duration from one minute to less than 24 hours.')
    elif kind=='pin':
        from afterhours.set_plan import exact_id
        if (not isinstance(value,dict) or set(value)!={'trackId','on'}
                or not exact_id(value.get('trackId')) or type(value.get('on')) is not bool):
            raise ValueError('Pin one exact known recording on or off.')
        value=copy.deepcopy(value)
    elif kind=='lesson':
        if (not isinstance(value,dict) or set(value)!={'archiveId','archiveRevision','lessonId','keep'}
                or not isinstance(value.get('archiveId'),str) or not re.fullmatch('[a-f0-9]{32}',value['archiveId'])
                or type(value.get('archiveRevision')) is not int or value['archiveRevision']<1
                or not isinstance(value.get('lessonId'),str) or not re.fullmatch('[a-f0-9]{24}',value['lessonId'])
                or type(value.get('keep')) is not bool):
            raise ValueError('Choose one exact archived lesson and its current revision.')
        value=copy.deepcopy(value)
    elif kind=='style':
        if (not isinstance(value,dict) or set(value)!={'id','stop'} or not isinstance(value.get('id'),str) or value.get('id') not in DEFAULT_STYLE
                or type(value.get('stop')) is not int or not 0<=value['stop']<=4):
            raise ValueError('Choose an available style dial and a stop from 0 to 4.')
        value=copy.deepcopy(value)
    elif kind=='discover':
        if type(value) is not bool:raise ValueError('Discover new music must be on or off.')
    else:
        raise ValueError('This settings change is not supported.')
    return {'kind': kind, 'value': value}


class Settings:
    def __init__(self, saved=None):
        self.state = {'version': 1, 'revision': 0, 'lead': 'all', 'receipts': [],'plan':None,'allowReorder':True,'ruleChanged':{},
                      'style':copy.deepcopy(DEFAULT_STYLE),'discover':True,'pins':[],'playedPins':[]}
        learned_preferences.restore(self.state,None)
        if not isinstance(saved, dict) or saved.get('version') != 1:
            return
        if type(saved.get('revision')) is int and 0 <= saved['revision'] < 1_000_000_000:
            self.state['revision'] = saved['revision']
        if saved.get('lead') in LEADS:
            self.state['lead'] = saved['lead']
        if type(saved.get('allowReorder')) is bool:self.state['allowReorder']=saved['allowReorder']
        if type(saved.get('discover')) is bool:self.state['discover']=saved['discover']
        try:self.state['style']=validate_style(saved.get('style'))
        except ValueError:pass
        try:self.state['plan']=validate_plan(saved.get('plan'))
        except ValueError:pass
        from afterhours.set_plan import exact_id,MAX_PINS
        pins=saved.get('pins')
        if isinstance(pins,list) and len(pins)<=MAX_PINS and all(exact_id(tid) for tid in pins) and len(set(pins))==len(pins):
            self.state['pins']=list(pins)
        played=saved.get('playedPins')
        if isinstance(played,list) and len(played)<=MAX_PINS:
            self.state['playedPins']=[tid for tid in self.state['pins'] if tid in played]
        if isinstance(saved.get('ruleChanged'),dict):
            self.state['ruleChanged']={k:v for k,v in saved['ruleChanged'].items() if k in ('reorder','warm','peak','dark') and type(v) in (int,float) and math.isfinite(v) and v>=0}
        learned_preferences.restore(self.state,saved)
        # Restored receipts are inert. Never replay an engine command on load.
        receipts = saved.get('receipts')
        for receipt in receipts[-32:] if isinstance(receipts, list) else []:
            try:
                if not isinstance(receipt, dict) or not isinstance(receipt.get('id'), str) or not 1 <= len(receipt['id']) <= 100:
                    continue
                validate_change(receipt.get('change'))
                if receipt.get('status') not in ('confirmed', 'pending', 'failed', 'cancelled', 'unconfirmed', 'expired') or not isinstance(receipt.get('expected'), dict):
                    continue
                restored = copy.deepcopy(receipt)
                if restored.get('interpretation') is not None:restored['restored']=True
                if restored['status'] == 'pending':
                    restored.update(status='expired', message='Previous control was not replayed after restart.')
                self.state['receipts'].append(restored)
            except (ValueError, TypeError):
                continue

    def expected(self, session, direction_revision):
        return {'sessionId': session.get('id'), 'revision': self.state['revision'],
                'lead': lead(session), 'selectionEpoch': selection_epoch(session),
                'allowReorder':(session.get('setlist') or {}).get('allowReorder',True),
                'directionRevision': direction_revision}

    def preference(self,change,session,now):
        """Compute the whole change before publishing it to the coordinator."""
        kind,value=change['kind'],change['value'];plan=copy.deepcopy(self.state['plan'])
        changed=None;reorder=None
        if kind=='plan':
            plan=value
            message=(f"The night is planned from {clock_label(plan['startAt'])} to {clock_label(plan['endAt'])}, with a peak at {clock_label(plan['peakAt'])}. Prepared tracks stay in place."
                     if plan else 'The scheduled energy plan is cleared. The brief still guides future choices.')
        elif kind=='peakShift':
            if not plan:raise ValueError('No timed night is planned yet. Give a start, end and peak time first.')
            plan.update(peakAt=plan['peakAt']+value,onePeak=True);validate_plan(plan);changed='peak'
            message=f"The peak moves to {clock_label(plan['peakAt'])}. Future choices follow the revised build; prepared tracks stay in place."
        elif kind=='warmLength':
            from afterhours.set_plan import validate_warm
            seconds=validate_warm(plan,value);plan['warmLen']=seconds;changed='warm'
            message=f"The warm-up lasts {seconds//60} minutes, until {clock_label(plan['startAt']+seconds)}. Future choices follow that build; prepared tracks stay in place."
        elif kind=='rule':
            changed=value['id'];on=value['on']
            if changed=='reorder':
                reorder=on
                message=('Copilot can reorder future picks. Playing and prepared recordings stay protected.' if on else
                         'Your chosen queue order stays fixed. Prepared music keeps its order and timing.')
            else:
                if not plan:raise ValueError('This energy rule needs a timed night plan first.')
                plan[{'warm':'warm','peak':'onePeak','dark':'darkLate'}[changed]]=on
                message={'warm':('The build stays patient.' if on else 'The build lifts sooner.'),
                         'peak':('The night builds toward one peak.' if on else 'Energy stays higher after the peak.'),
                         'dark':('The late part of the night eases into deeper energy.' if on else 'The late energy taper is removed.')}[changed]
                message+=' Future choices follow the shape; prepared tracks stay in place.'
        else:raise ValueError('Unknown preference change.')
        return plan,reorder,changed,message

    def rules(self,session):
        rows=[{'id':'reorder','group':'always','title':'Copilot can reorder future picks',
               'detail':'Playing and prepared recordings stay protected.',
               'on':(session.get('setlist') or {}).get('allowReorder',True)}]
        plan=self.state['plan']
        if plan:
            rows=[{'id':'warm','group':'words','title':'Warm, patient start','detail':'An energy target for future track choices.','on':plan['warm']},
                  {'id':'peak','group':'words','title':'One peak, around '+clock_label(plan['peakAt']),'detail':'The build eases after the planned peak.','on':plan['onePeak']},
                  {'id':'dark','group':'words','title':'Deeper energy late','detail':'Ease the target in the last quarter of the night.','on':plan['darkLate']}]+rows
        return [{**row,'changedAt':self.state['ruleChanged'].get(row['id'])} for row in rows]

    def planner_context(self,session,now):
        from afterhours.set_plan import planned_energy
        plan=self.state['plan']
        return {'lead':lead(session),'allowReorder':(session.get('setlist') or {}).get('allowReorder',True),
                'nightPlan':copy.deepcopy(plan),'observedAt':now,
                'style':style_context(self.state['style']),'discover':self.state['discover'],
                'pins':list(self.state['pins']),
                'fulfilledPins':list(self.state['playedPins']),
                'energyTarget':round(planned_energy(plan,now),2) if plan and plan['startAt']<=now<=plan['endAt'] else None,
                'meaning':'Energy is the operator’s planned intention, not a measured track or room score. Use available track evidence; never invent vocal, explicitness or energy metadata.'}

    def check(self, request_id, change, expected, session, direction_revision):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError('A settings request identity is required.')
        change = validate_change(change)
        for receipt in self.state['receipts']:
            if receipt['id'] == request_id:
                if receipt['change'] != change or receipt['expected'] != expected:
                    raise ValueError('This identity belongs to a different settings change.')
                return receipt
        if not isinstance(expected, dict) or expected != self.expected(session, direction_revision):
            raise ValueError('Settings changed before this edit arrived. Read the current settings and try again.')
        return None

    def confirmed(self, request_id, change, expected, session, direction_revision, now, message, status='confirmed', interpretation=None):
        self.state['revision'] += 1
        self.state['lead'] = lead(session)
        receipt = {'id': request_id, 'change': validate_change(change), 'expected': copy.deepcopy(expected),
                   'at': now, 'status': status, 'message': message,
                   'current': self.expected(session, direction_revision)}
        if interpretation is not None:receipt['interpretation']=copy.deepcopy(interpretation)
        self.state['receipts'] = (self.state['receipts'] + [receipt])[-32:]
        # Keep large brief edits from crowding out the conversation checkpoint.
        import json
        while len(self.state['receipts'])>1 and len(json.dumps(self.state['receipts']))>24000:
            self.state['receipts'].pop(0)
        return receipt

    def snapshot(self, session, direction, direction_revision, supported, library=None, set_supported=False):
        return {'version': 1, 'revision': self.state['revision'], 'lead': lead(session),
                'brief': direction, 'expected': self.expected(session, direction_revision),
                'capabilities': ['brief','plan','prompt-plan','style','discover','peakShift','warmLength'] + (['pin'] if set_supported else []) + (['lead','rules'] if supported else []) + (['librarySummary'] if library is not None else []),
                'interpretedPlan':prompt_plan.readback(self.state),
                'style':copy.deepcopy(self.state['style']),'discover':self.state['discover'],
                'houseRules':([r for r in self.rules(session) if r['id']=='reorder']+learned_preferences.rules(self.state)) if supported else [],
                **({'library':copy.deepcopy(library)} if library is not None else {}),
                'rules':self.rules(session) if supported else [],'plan':copy.deepcopy(self.state['plan']),
                'receipt': copy.deepcopy(self.state['receipts'][-1]) if self.state['receipts'] else None}
