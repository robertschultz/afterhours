"""Pure queue/telemetry guards for optional exact crowd choices.

No HTTP, SQL, provider, native command or planner allowance is touched here.
The engine still validates each complete order with its existing validator.
"""
import hashlib
import json

from afterhours import dj_settings
from afterhours.guest_coordinator import finite,public_facts
from afterhours.guest_requests import exact_id,track_ref


def parse(value):
    if value is None:return None
    if (not isinstance(value,dict) or set(value)!={'slot','trackIds'} or type(value['slot']) is not int
            or not 0<=value['slot']<12 or not isinstance(value['trackIds'],list)
            or not 2<=len(value['trackIds'])<=3 or any(not exact_id(tid) for tid in value['trackIds'])
            or len(set(value['trackIds']))!=len(value['trackIds'])):
        raise ValueError('A crowd choice needs one exact future slot and two or three distinct recordings.')
    return {'slot':value['slot'],'trackIds':list(value['trackIds'])}


def order_ids(snapshot):
    return [t['id'] for t in (snapshot.get('queuedUpdate') or {}).get('tracks') or snapshot.get('order',[])]


def protected(snapshot):
    return [t['id'] for t in snapshot.get('order',[])[:max(0,snapshot.get('index',-1))+2]]


def ab_pair(snapshot,now=None):
    """Exact observed playing/cued pair; timestamps are guards, not identity.

    C/D preparation can change independently. A/B must still contain the
    applied playing and next records on their bound physical decks before a
    future slot may change. A scheduled prefix alone cannot establish that.
    """
    index=snapshot.get('index');order=snapshot.get('order',[])
    if type(index) is not int or not 0<=index<len(order)-1:return None
    expected={order[index].get('id'):True,order[index+1].get('id'):False}
    if len(expected)!=2 or any(not exact_id(tid) for tid in expected):return None
    telemetry=snapshot.get('telemetry') or {};decks=telemetry.get('decks',[])
    pair=[d for d in decks if type(d.get('deck')) is int and d['deck'] in (1,2)]
    if len(pair)!=2 or {d['deck'] for d in pair}!={1,2}:return None
    if {d.get('id') for d in pair}!=set(expected):return None
    for deck in pair:
        if (deck.get('loaded') is not True or deck.get('unavailable')
                or deck.get('playing') is not expected[deck['id']]):return None
        if now is not None:
            stamp=deck.get('sampledAt',telemetry.get('sampledAt'));controls=deck.get('controls') or {}
            control_stamp=controls.get('sampledAt');volume=controls.get('volume')
            if (not finite(stamp) or not -2<=now-stamp<3
                    or not finite(control_stamp) or not -2<=now-control_stamp<3
                    or not finite(volume) or not 0<=volume<=1):return None
    return [{'deck':d['deck'],'id':d['id'],'loaded':True,'playing':d['playing']}
            for d in sorted(pair,key=lambda d:d['deck'])]


def eligible(snapshot,now,require_future=True):
    pending=snapshot.get('queuedUpdate') or {}
    if (snapshot.get('active') is not True or snapshot.get('phase')!='playing'
            or dj_settings.lead(snapshot)!='all' or (snapshot.get('copilot') or {}).get('held')
            or snapshot.get('resumeBlocked') or not (snapshot.get('executionPlan') or {}).get('applied')
            or pending.get('status')=='blocked'
            # A slot vote does not carry authority to replace explicit per-
            # recording tempo targets. Keep that authored future plan intact.
            or pending.get('tempoTargets') or snapshot.get('tempoTargets')
            or not snapshot.get('setlist',{}).get('allowReorder',True)):
        return False
    index=snapshot.get('index');order=snapshot.get('order',[])
    if type(index) is not int or not 0<=index<len(order):return False
    if ab_pair(snapshot,now) is None:return False
    _,playing=public_facts(snapshot,{},now)
    return playing==order[index]['id'] and (not require_future or len(order_ids(snapshot))>len(protected(snapshot)))


def context_hash(snapshot):
    # Preparation can enrich metadata without invalidating a still-identical
    # choice. Operator/mode/policy/queue/active-prefix changes cannot do so.
    data={'sessionId':snapshot.get('id'),'startedAt':snapshot.get('startedAt'),'index':snapshot.get('index'),
          'active':snapshot.get('active'),'mode':dj_settings.lead(snapshot),
          'selectionEpoch':dj_settings.selection_epoch(snapshot),
          'held':bool((snapshot.get('copilot') or {}).get('held')),
          'order':order_ids(snapshot),'protected':protected(snapshot),'abPair':ab_pair(snapshot),
          'setlist':snapshot.get('setlist',{}),'pins':snapshot.get('setPins',[]),
          'playedPins':snapshot.get('setPlayedPins',[]),'maxTempoShift':snapshot.get('maxTempoShift',8),
          'secondsPerTrack':snapshot.get('secondsPerTrack',30)}
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def binding(snapshot,revision,generation,scope_generation=None):
    fingerprint=context_hash(snapshot)
    if scope_generation is not None:
        fingerprint=hashlib.sha256(json.dumps([fingerprint,scope_generation],separators=(',',':')).encode()).hexdigest()
    return {'sessionId':snapshot['id'],'agentRevision':revision,'agentGeneration':generation,'contextHash':fingerprint}


def validate_variants(snapshot,base_order,slot,track_ids,validator,now):
    choice=parse({'slot':slot,'trackIds':track_ids})
    if not eligible(snapshot,now) or base_order!=order_ids(snapshot):
        raise ValueError('The bound future crowd slot is no longer available.')
    if not len(protected(snapshot))<=slot<len(base_order) or base_order[slot] not in track_ids:
        raise ValueError('The playing, cued and original future recording must stay protected.')
    policy=snapshot.get('setlist') or {}
    reserved=set(policy.get('lockedIds',[]))|set(snapshot.get('setPins',[]))
    if reserved.intersection(track_ids):raise ValueError('An operator pin or locked record cannot become a crowd choice.')
    if any(tid in base_order[:slot]+base_order[slot+1:] for tid in track_ids):
        raise ValueError('A crowd option cannot duplicate another queue position.')
    catalog={t['id']:t for t in snapshot.get('candidates',[])}
    if any(tid not in catalog for tid in track_ids):raise ValueError('A crowd option left the working catalog.')
    tracks=[track_ref(catalog[tid]) for tid in choice['trackIds']]
    for tid in track_ids:
        order=list(base_order);order[slot]=tid
        validator({'reply':'Checking a crowd choice.','direction':'','search':None,'trackIds':order,
            'reason':'Exact future crowd choice.','mixTransitions':None,'tempoTargets':None,
            'deferredTracks':None,'selectionReview':None,'requestVerdicts':None,'performance':None,
            'handoff':None,'crowdChoice':None},snapshot)
    return tracks
