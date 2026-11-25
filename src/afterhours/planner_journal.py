"""Bounded public decision facts, never private model reasoning or mix control.

An optional model-authored comparison is a fit estimate, not a measured score.
Queue delivery comes only from exact session/queue receipts. The journal is
process-local so a restart cannot replay an old decision as current activity.
"""
import copy
import time
import uuid


def ids(tracks):
    return [t['id'] for t in tracks]


def line(value, limit=400):
    return ' '.join(str(value or '').split())[:limit]


def binding(session):
    queued=session.get('queuedUpdate') or {}
    return {'sessionId':session.get('id'), 'startedAt':session.get('startedAt'),
            'planId':(session.get('executionPlan') or {}).get('id'),
            'orderIds':ids(session.get('order',[])),
            'queueRevision':queued.get('id'), 'queuedIds':ids(queued.get('tracks',[]))}


def comparison(result, session, supplied_ids=None):
    """Discard a bad display annotation without discarding a valid music choice.

Only a specific unprotected slot and the actual supplied catalog may be rated.
The chosen recording must be included. A re-check includes its incumbent too.
"""
    review=result.get('selectionReview')
    chosen=result.get('trackIds') or []
    if not isinstance(review,dict) or set(review)!={'slot','options'} or not chosen or result.get('search'):
        return None
    slot=review['slot'];options=review['options']
    audible=session.get('active') or any(d.get('playing') for d in (session.get('telemetry') or {}).get('decks',[]))
    protected=min(len(session.get('order',[])),max(0,session.get('index',-1))+2) if audible else 0
    before=ids((session.get('queuedUpdate') or {}).get('tracks') or session.get('order',[]))
    first_change=next((i for i,tid in enumerate(chosen) if i>=len(before) or before[i]!=tid),None)
    if type(slot) is not int or not protected<=slot<len(chosen) or (first_change is not None and slot!=first_change):
        return None
    if not isinstance(options,list) or not 1<=len(options)<=4:return None
    catalog={t['id']:t for t in session.get('candidates',[])}
    supplied=set(catalog if supplied_ids is None else supplied_ids)
    seen=set();clean=[]
    for option in options:
        if not isinstance(option,dict) or set(option)!={'trackId','fit','reason'}:return None
        tid=option['trackId'];fit=option['fit'];reason=option['reason']
        if (not isinstance(tid,str) or tid not in catalog or tid not in supplied or tid in seen
                or type(fit) is not int or not 0<=fit<=100
                or not isinstance(reason,str) or not 1<=len(reason.strip())<=320):return None
        # Already played/current/cued records are not alternative futures.
        if tid in before[:protected]:return None
        seen.add(tid)
        clean.append({'trackId':tid,'title':line(catalog[tid].get('title'),160),
                      'artist':line(catalog[tid].get('artist'),100),'fit':fit,'reason':line(reason,320)})
    incumbent=before[slot] if slot<len(before) else None
    if chosen[slot] not in seen or incumbent is not None and incumbent not in seen:return None
    scores={o['trackId']:o['fit'] for o in clean}
    winner=chosen[slot]
    # A published comparison must support its actual outcome. Omit a
    # contradictory annotation; never let display metadata choose a different
    # recording or interfere with an otherwise validated music operation.
    if scores[winner] < max(scores.values()):return None
    if incumbent is not None and winner!=incumbent and scores[winner]<=scores[incumbent]:return None
    return {'slot':slot,'incumbentId':incumbent,'winnerId':chosen[slot],
            'options':clean,'scoreBasis':'model fit estimate; not a measured probability'}


class PlannerJournal:
    MAX_ROUNDS=24
    MAX_EVENTS=96
    MAX_LOOKUPS=12  # four rounds of three, the most one evaluation can make
    MAX_STEPS=24

    def __init__(self,clock=time.time):
        self.clock=clock;self.epoch=uuid.uuid4().hex;self.sequence=0
        self.rounds=[];self.events=[]

    def _event(self,row,status,**values):
        if row['status']==status and all(row.get(k)==v for k,v in values.items()):return
        self.sequence+=1
        row.update(status=status,updatedAt=self.clock(),sequence=self.sequence,**copy.deepcopy(values))
        self.events.append({'sequence':self.sequence,'roundId':row['id'],'status':status,'at':self.clock()})
        self.events=self.events[-self.MAX_EVENTS:]
        if status in ('selected','staged','applied','kept','failed','held','superseded'):
            wording={'selected':'Preparing the selected recordings', 'staged':'Staged for a verified handoff',
                     'applied':'Confirmed the prepared queue', 'kept':'Confirmed the current order',
                     'failed':'The operation stopped', 'held':'The selection is held',
                     'superseded':'A newer context replaced this work'}[status]
            self.step(row,'delivery',wording)

    def step(self,row,key,text,result=''):
        value=dict(id=key,text=line(text,180),result=line(result,240),at=self.clock())
        old=next((s for s in row.setdefault('steps',[]) if s['id']==key),None)
        if old:
            if (old['text'],old['result'])==(value['text'],value['result']):return
            old.update(text=value['text'],result=value['result'])
        elif len(row['steps'])<self.MAX_STEPS:row['steps'].append(value)
        row['updatedAt']=self.clock()

    def lookup(self,round_id,text,result=''):
        """Record one lookup the planner made, while its round is still open."""
        row=self.get(round_id)
        if not row or row['status']!='evaluating':return
        made=sum(1 for s in row.get('steps',[]) if s['id'].startswith('lookup:'))
        if made>=self.MAX_LOOKUPS:return
        self.step(row,f'lookup:{made+1}',text,result)
        self.sequence+=1

    def get(self,round_id):
        return next((r for r in self.rounds if r['id']==round_id),None)

    def begin(self,session,revision,reason):
        row={'id':uuid.uuid4().hex,'status':None,'revision':revision,'openedAt':self.clock(),
             'before':binding(session),'reason':line(reason),'comparison':None,'selectedIds':[],
             'titles':{t['id']:line(t.get('title'),160) for t in session.get('order',[])[:64]}}
        self.rounds.append(row);self.rounds=self.rounds[-self.MAX_ROUNDS:]
        self._event(row,'evaluating')
        self.step(row,'context','Read the current set',f" · {len(session.get('order',[]))} queued · {len(session.get('candidates',[]))} available recordings")
        self.step(row,'work',{'operator message':'Checking the requested direction',
                             'choose from catalog results':'Comparing the search results',
                             'recheck upcoming queue after music changed':'Re-checking the upcoming queue',
                             'refill upcoming queue':'Choosing what follows the current queue'}.get(reason,'Checking the upcoming queue'))
        return row['id']

    def finish(self,round_id,result,session,receipt,supplied_ids):
        row=self.get(round_id)
        if not row or row['status']!='evaluating':return
        review=comparison(result,session,receipt.get('catalogIds',supplied_ids))
        wanted=set(result['trackIds']) | {o['trackId'] for o in (review or {}).get('options',[])}
        row['titles'].update({t['id']:line(t.get('title'),160) for t in session.get('candidates',[]) if t['id'] in wanted})
        status='decided' if result['trackIds'] else 'search' if result['search'] else 'unchanged'
        self._event(row,status,decidedAt=self.clock(),comparison=review,
                    selectedIds=result['trackIds'],reason=line(result['reason']),
                    responseId=line(receipt.get('responseId'),160))
        if review:
            for option in review['options']:
                self.step(row,'fit:'+option['trackId'],option['title'],f" · {option['fit']} fit estimate · {option['reason']}")
        if result.get('search'):
            row['searchQuery']=result['search']['query']
            self.step(row,'search','Searching for '+result['search']['query'])
        elif not result['trackIds']:
            self.step(row,'answer','Checked the current recording and queue context')

    def reply(self,round_id,message_id,text,request_id=None):
        row=self.get(round_id)
        if row:row.update(messageId=message_id,reply=line(text,1600),requestId=request_id)

    def close(self,round_id,status,reason):
        row=self.get(round_id)
        if row and row['status'] not in ('applied','kept','superseded','failed'):
            self._event(row,status,reason=line(reason))

    def cancel_evaluating(self,reason):
        for row in self.rounds:
            if row['status']=='evaluating':self._event(row,'superseded',reason=line(reason))

    def accepted(self,round_id,session):
        row=self.get(round_id)
        if not row or not row['selectedIds']:return
        now=binding(session);wanted=row['selectedIds'];queue=session.get('queuedUpdate') or {}
        if now['queuedIds']==wanted and now['queueRevision']:
            self._event(row,'staged',acceptedAt=self.clock(),after=now,
                        queueRevision=now['queueRevision'],preparation=queue.get('status'))
        elif now['orderIds']==wanted:
            applied=(session.get('executionPlan') or {}).get('applied') is True
            kept=wanted==row['before']['orderIds']
            self._event(row,'kept' if applied and kept else 'applied' if applied else 'selected',
                        acceptedAt=self.clock(),after=now,
                        appliedAt=self.clock() if applied else None)

    def reconcile(self,session):
        current=binding(session)
        for row in self.rounds:
            search=session.get('catalogSearch') or {}
            if row['status']=='search' and row.get('searchQuery') and search.get('query')==row['searchQuery']:
                count=search.get('examined',0);found=search.get('added',0)
                if isinstance(count,int) and isinstance(found,int):
                    self.step(row,'search','Searched for '+row['searchQuery'],f' · {count} checked · {found} found')
            if row['status'] not in ('selected','staged'):continue
            after=row.get('after') or row['before']
            if (current['sessionId'],current['startedAt'])!=(after['sessionId'],after['startedAt']):
                self._event(row,'superseded',reason='The session changed before this decision was applied.');continue
            if row['status']=='staged':
                queue=session.get('queuedUpdate') or {}
                receipt=session.get('lastQueueApplication') or {}
                if queue.get('id')==row['queueRevision']:
                    self._event(row,'staged',preparation=queue.get('status'),deliveryNote=line(queue.get('error')))
                elif (receipt.get('queueRevision')==row['queueRevision']
                      and receipt.get('trackIds')==row['selectedIds']==current['orderIds']
                      and receipt.get('planId')==current['planId']
                      and (session.get('executionPlan') or {}).get('applied') is True):
                    self._event(row,'applied',after=current,appliedAt=receipt['appliedAt'])
                else:self._event(row,'superseded',reason='The staged queue changed before its application was confirmed.')
            elif current['orderIds']!=row['selectedIds'] or current['queueRevision'] is not None:
                self._event(row,'superseded',reason='A different queue replaced this selection.')
            elif (session.get('executionPlan') or {}).get('applied') is True:
                self._event(row,'applied',after=current,appliedAt=self.clock())

    def snapshot(self,session):
        self.reconcile(session)
        return {'version':1,'epoch':self.epoch,'sequence':self.sequence,
                'updatedAt':self.events[-1]['at'] if self.events else None,
                'context':binding(session),'rounds':copy.deepcopy(self.rounds),
                'events':copy.deepcopy(self.events)}
