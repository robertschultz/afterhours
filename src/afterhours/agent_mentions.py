"""Resolve explicit names against the local roster before spending a model turn."""
import re
import unicodedata

def handle(name):
    value=unicodedata.normalize('NFKC',name).strip()
    return re.sub(r'[^\w-]+','-',value,flags=re.UNICODE).strip('-')

def resolve_mentions(text,profiles):
    matches=[]
    # A quoted name supports spaces. An at-sign within an email is not a mention.
    pattern=r'(?<![\w@])@(?:"([^"\n]{1,60})"|([\w-]+))'
    for found in re.finditer(pattern,text):
        name=found.group(1) or found.group(2)
        key=unicodedata.normalize('NFKC',name).casefold()
        selected=[p for p in profiles if key in {p['id'].casefold(),p['name'].casefold(),handle(p['name']).casefold()}]
        if not selected:raise ValueError(f'No agent named @{name}. Choose a name from Agents or the mention menu.')
        if len(selected)>1:raise ValueError(f'@{name} matches more than one agent. Give them distinct names in Agents.')
        p=selected[0]
        if any(item['id']==p['id'] for item in matches):continue
        matches.append({k:p.get(k) for k in ('id','name','genres','bpmMin','bpmMax','hype','humor','talkative','callouts') }|
                       {'appearance':p.get('appearance','')[:400],'wardrobe':p.get('wardrobe','')[:200],
                        'visualReady':bool(p.get('approvedFrame'))})
    if len(matches)>4:raise ValueError('Mention up to four agents in one direction.')
    return matches

def validate_handoff(handoff,mentions,current):
    if handoff is None:return None
    if not isinstance(handoff,dict) or set(handoff)!={'fromAgentId','toAgentId','reason'}:
        raise ValueError('The agent handoff has an invalid format.')
    if not isinstance(handoff['reason'],str) or not 1<=len(handoff['reason'])<=600:
        raise ValueError('The agent handoff needs a short reason.')
    if any(not isinstance(handoff[k],str) for k in ('fromAgentId','toAgentId')):
        raise ValueError('The handoff must use existing agent IDs.')
    by_id={p['id']:p for p in mentions}
    target=by_id.get(handoff['toAgentId'])
    if not target or handoff['fromAgentId']!=current.get('id') or target['id']==current.get('id'):
        raise ValueError('A handoff must go from the current DJ to another explicitly mentioned agent.')
    if not target['visualReady']:raise ValueError(f"Approve a booth frame for {target['name']} in Agents before their handoff.")
    return {**handoff,'fromName':current.get('name','Current DJ'),'toName':target['name']}


def roster_command(text, profiles, assigned):
    """Only complete, immediate operator imperatives enter the local path.

    The same names work with @ in chat and without it in a voice transcript.
    Questions, negations, future directions and track requests remain ordinary
    conversation. A matching imperative with an unknown name fails explicitly.
    """
    value=' '.join(text.strip().rstrip('.!?').split())
    value=re.sub(r'^(?:please\s+|(?:can|could|would|will) you (?:please )?)','',value,flags=re.I)
    name=r'@?(?:"[^"\n]{1,60}"|[\w][\w -]{0,59}?)'
    def named(value):
        value=value.lstrip('@').strip('"').strip()
        return resolve_mentions('@"'+value+'"',profiles)[0]
    default=re.fullmatch(rf'(?:make|set|use) (?P<to>{name}) (?:as )?(?:my |the )?default (?:dj|character)',value,re.I)
    if default:
        target=named(default['to'])
        if not target['visualReady']:raise ValueError(f"Choose a performance image for {target['name']} before making them the default DJ.")
        return {'kind':'default','id':target['id'],'name':target['name']}
    handoff=re.fullmatch(rf'(?:(?:have|let) )?(?P<to>{name}) (?:now )?(?:come in and )?take over(?: the decks)?(?: from (?P<from>{name}))?(?: now)?',value,re.I)
    if not handoff:
        handoff=re.fullmatch(rf'(?:switch|change) (?:the )?(?:dj|performer) to (?P<to>{name})(?: now)?',value,re.I)
    if not handoff:return None
    current=next((p for p in profiles if p['id']==assigned),None)
    if not current:raise ValueError('The current DJ is unavailable. Refresh the roster.')
    if handoff.groupdict().get('from') and named(handoff['from'])['id']!=assigned:
        raise ValueError(f"{current['name']} is currently at the decks. The handoff source changed.")
    target=named(handoff['to'])
    if target['id']==assigned:return {'kind':'current','id':assigned,'name':current['name']}
    change=validate_handoff({'fromAgentId':assigned,'toAgentId':target['id'],'reason':'Explicit operator character handoff.'},[target],current)
    return {'kind':'handoff',**change}
