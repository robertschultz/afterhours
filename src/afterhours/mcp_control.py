"""Three bounded MCP operations over the existing DJ conversation boundary."""
import base64
from contextlib import nullcontext
import hashlib
import hmac
import json
import math
import re
import secrets
import time

from afterhours import catalog_browser
from afterhours import copilot_commands
from afterhours import dj_settings
from afterhours import native_settings
from afterhours import playback_requests
from afterhours.dj_agent import TEXT_REQUESTS, TEXT_TURN_CEILING, PREPARATION_CEILING
from afterhours.mcp_auth import ControlError
from afterhours.mcp_receipts import ExternalGrant, ACTIVE, TERMINAL, ID, canonical

MESSAGES = {
    'dispatching': 'Direction accepted. Execution is not yet confirmed.',
    'pending': 'The engine is handling this direction. Read the original receipt for its outcome.',
    'confirmed': 'The engine confirmed this direction.',
    'blocked': 'The engine could not complete this direction. Inspect the local DJ conversation for details.',
    'superseded': 'A newer direction owns future work. Already playing audio is unchanged.',
    'uncertain': 'The original outcome is uncertain. No request was replayed.',
}


def short(value, limit=120):
    if not isinstance(value, str):return ''
    return ''.join(c for c in value if c.isprintable())[:limit]


def recording(row):
    if not isinstance(row, dict) or not isinstance(row.get('id'), str):return None
    # No engine locators, media URLs, file paths or provider metadata.
    result={k:short(row.get(k),200 if k=='id' else 120) for k in ('id','title','artist')}
    for source,target,low,high in (('bpm','bpm',40,300),('duration','durationSeconds',0,86400)):
        value=row.get(source)
        if type(value) in (int,float) and math.isfinite(value) and low<value<=high:result[target]=value
    key=row.get('camelot') or row.get('key')
    if isinstance(key,str) and re.fullmatch(r'(?:[1-9]|1[0-2])[AB]|[A-G](?:#|b)?(?:m| minor| major)?',key):result['key']=key
    return result


class MCPControl:
    def __init__(self, agent, store, clock=time.time):
        self.agent, self.session, self.store, self.clock = agent, agent.session, store, clock
        self.context_key = secrets.token_bytes(32)
        store.acquire_dispatch_owner()
        agent.external_receipt=self.native_receipt

    def native_receipt(self):
        latest=next((m for m in reversed(self.agent.state['messages']) if m.get('role')=='user'),{})
        messages=[m for m in self.agent.state['messages'] if m.get('role')=='user' and m.get('source')=='mcp']
        # Refresh historical rows as well as the latest receipt. Persisted
        # display labels are never an alternative to the paired-client ledger.
        for message in messages:message.pop('sourceIdentity',None)
        result=None
        for message in messages:
            try:
                row,identity=self.store.native_lookup(message.get('id'))
                if not row:continue
                message['sourceIdentity']=identity
                if message is latest:result={**self._receipt(row),'sourceIdentity':identity}
            except Exception:
                if message is latest:
                    result={'commandId':latest.get('id'),'status':'uncertain','message':MESSAGES['uncertain']}
        return result

    def _context(self):
        s=self.session.state
        return {**copilot_commands.context(s),'revision':self.agent.state['revision'],
                'selectionEpoch':(s.get('copilot') or {}).get('selectionEpoch',0),
                'mode':(s.get('copilot') or {}).get('mode','leading'),'phase':s.get('phase'),
                'active':s.get('active') is True,'held':(s.get('copilot') or {}).get('held') is True,
                'resumeBlocked':bool(s.get('resumeBlocked')),
                'controlCommand':{k:((s.get('copilot') or {}).get('command') or {}).get(k) for k in ('id','action','status')},
                'queueHash':hashlib.sha256(canonical([t.get('id') for t in s.get('order',[])]).encode()).hexdigest(),
                'pendingQueueHash':hashlib.sha256(canonical([t.get('id') for t in (s.get('queuedUpdate') or {}).get('tracks',[])]).encode()).hexdigest(),
                'decks':[{k:d.get(k) for k in ('deck','id','playing')} for d in (s.get('telemetry') or {}).get('decks',[])[:2]]}

    def _token(self, client_id, context):
        raw=canonical({'clientId':client_id,'expires':self.clock()+60,'context':context}).encode()
        return base64.urlsafe_b64encode(raw).decode()+'.'+hmac.new(self.context_key,raw,hashlib.sha256).hexdigest()

    def _check_context(self, token, client_id):
        try:
            if not isinstance(token,str) or len(token)>4096:raise ValueError()
            encoded,signature=token.split('.')
            raw=base64.b64decode(encoded,altchars=b'-_',validate=True)
            if not hmac.compare_digest(signature,hmac.new(self.context_key,raw,hashlib.sha256).hexdigest()):raise ValueError()
            item=json.loads(raw)
            if (item['clientId']!=client_id or self.clock()>=item['expires'] or item['context']!=self._context()):raise ValueError()
            return item['context']
        except (ValueError,KeyError,TypeError):
            raise ControlError('stale-context','The set changed or this context expired. Read state before a new direction.') from None

    def state(self, credential, *, offset=0, limit=12):
        client=self.store.authenticate(credential)
        if type(offset) is not int or not 0<=offset<=10000 or type(limit) is not int or not 1<=limit<=24:
            raise ControlError('invalid-page','Use a queue offset of 0–10000 and a limit of 1–24.')
        with self.agent.lock,getattr(self.session,'lock',nullcontext()):
            s=self.session.state
            context=self._context()
            order=s.get('order',[])
            index=s.get('index',-1)
            if type(index) is not int or not -1<=index<len(order):index=-1
            telemetry=s.get('telemetry') or {}
            sampled=telemetry.get('sampledAt')
            age=self.clock()-sampled if type(sampled) in (int,float) and math.isfinite(sampled) else None
            queued=s.get('queuedUpdate') or {}
            pending=queued.get('tracks') or []
            pending_status=queued.get('status')
            if pending_status not in ('needs-analysis','needs-review','analyzing','preparing','ready','blocked'):pending_status='staged'
            pipeline=self.agent.pipeline or {}
            current=recording(order[index]) if 0<=index<len(order) else None
            cued=recording(order[index+1]) if 0<=index+1<len(order) and index>=0 else None
            stage=pipeline.get('stage')
            if stage not in ('analyze','wait-analysis','wait-review','wait-apply'):stage=None
            return {'version':1,'client':{'id':client['id'],'name':client['name'],'scopes':client['scopes']},
                    'session':{'id':short(s.get('id'),80),'phase':s.get('phase') if s.get('phase') in
                               ('idle','stopped','ready','playing','takeover','error','complete','completed','preparing','analyzing','reviewing','loading','mixing','stopping','discovering','catalog-ready') else 'unknown',
                               'active':s.get('active') is True,'control':context['mode'] if context['mode'] in ('leading','on','mix','off','manual') else 'unknown',
                               'held':(s.get('copilot') or {}).get('held') is True,
                               'telemetryAgeSeconds':max(0,round(age,3)) if age is not None else None,
                               'freshTelemetry':age is not None and 0<=age<=3},
                    'current':current,'cued':cued,
                    'queue':{'offset':offset,'total':len(order),'currentIndex':index,'records':[recording(t) for t in order[offset:offset+limit]],
                             'nextOffset':offset+limit if offset+limit<len(order) else None,
                             'pendingUpdate':bool(queued.get('tracks'))},
                    'pendingQueue':({'applied':False,'status':pending_status,'offset':offset,'total':len(pending),
                                     'records':[recording(t) for t in pending[offset:offset+limit]],
                                     'nextOffset':offset+limit if offset+limit<len(pending) else None} if pending else None),
                    'preparation':{'stage':stage,'evaluating':bool(self.agent.worker and self.agent.worker.is_alive()),
                                   'prepared':(s.get('executionPlan') or {}).get('applied') is True},
                    'allowance':{'configured':client['allowance'],'committedCeilingUSD':client['committedCeilingUSD'],
                                 'committedProviderCalls':client['committedProviderCalls'],'billingVerified':False},
                    'context':self._token(client['id'],context),'pollAfterSeconds':2}

    def _kind(self,text,surface):
        """Admission uses the existing parsers; execution still calls message()."""
        if self.agent.roster_command(text) or native_settings.intent(text):
            raise ControlError('unsupported-operation','This connection controls music, not characters or native devices.')
        control=copilot_commands.intent(text)
        setting,_=self.agent.settings_direction(text,surface)
        if control or setting and setting['kind']=='lead':return 'control'
        if setting:return 'local'
        if surface=='set':return 'local'
        if catalog_browser.import_query(text):return 'discovery'
        transport=playback_requests.intent(text)
        if transport in ('stop','status'):return transport
        s=self.session.state
        if transport=='continue' and (s.get('phase') in ('takeover','error')
                or s.get('active') and (s.get('copilot') or {}).get('held')):return 'control'
        if transport=='continue' and (s.get('phase') in ('takeover','error','complete','completed')
                or s.get('active') or s.get('startedAt') is not None):return 'local'
        if transport in ('start','continue'):
            return 'start' if (s.get('executionPlan') or {}).get('applied') else 'music'
        if surface=='settings':return 'local'
        if surface=='catalog' and catalog_browser.browse_request(text) and transport is None:return 'local'
        return 'music'

    def direct(self,credential,payload):
        client=self.store.authenticate(credential)
        if (not isinstance(payload,dict) or set(payload)-{'requestId','text','context','surface'}
                or not {'requestId','text','context'}<=set(payload)
                or not isinstance(payload['requestId'],str) or not ID.fullmatch(payload['requestId'])
                or not isinstance(payload['text'],str) or not 1<=len(payload['text'].strip())<=1600
                or payload.get('surface') not in (None,'catalog','settings','set')):
            raise ControlError('invalid-request','Supply a request ID, current context and 1–1600 characters of direction.')
        previous=self.store.lookup(client['id'],payload['requestId'],payload)
        if previous:return self._receipt(previous)
        with self.agent.lock,getattr(self.session,'lock',nullcontext()):
            # Recheck under the dispatch lock: two simultaneous retries claim once.
            previous=self.store.lookup(client['id'],payload['requestId'],payload)
            if previous:return self._receipt(previous)
            context=self._check_context(payload['context'],client['id'])
            text=payload['text'].strip();surface=payload.get('surface')
            kind=self._kind(text,surface)
            emergency=playback_requests.intent(text)=='stop' or copilot_commands.intent(text)=='off'
            pending_control=((self.session.state.get('copilot') or {}).get('command') or {}).get('status')=='pending'
            pending_discovery=(self.session.state.get('catalogSearch') or {}).get('status') in ('queued','searching')
            busy=bool(self.agent.worker and self.agent.worker.is_alive() or self.agent.pipeline or self.agent.awaiting_catalog or pending_control or pending_discovery)
            if busy and not emergency:
                raise ControlError('busy','The DJ is handling an earlier direction. Read its outcome before a new direction.')
            row,claimed=self.store.claim(client,payload['requestId'],payload,context,music=kind in ('music','discovery'),
                selection_cost=TEXT_TURN_CEILING,preparation_cost=PREPARATION_CEILING,selection_calls=TEXT_REQUESTS,operation=kind)
            if not claimed:return self._receipt(row)
            grant=ExternalGrant(self.store,row,client)
            try:
                self.agent.message(text,'mcp',grant.id,approved=kind=='music',approve_preparation=grant.prepare,
                                   surface=surface,external=grant)
                status='confirmed' if kind=='status' else 'pending'
                self.store.update(grant.id,status,MESSAGES[status])
            except ValueError as exc:
                self.store.update(grant.id,'blocked',MESSAGES['blocked'],getattr(exc,'code',None))
            except Exception:
                # Do not expose raw exceptions, paths, request bodies or tokens.
                self.store.update(grant.id,'uncertain',MESSAGES['uncertain'])
            return self._receipt(self.store.lookup(client['id'],payload['requestId']))

    def receipt(self,credential,request_id):
        client=self.store.authenticate(credential)
        if not isinstance(request_id,str) or not ID.fullmatch(request_id):
            raise ControlError('invalid-request','Use the original request ID.')
        row=self.store.lookup(client['id'],request_id)
        if not row:raise ControlError('unknown-request','No receipt belongs to this client and request ID.')
        return self._receipt(row)

    def _receipt(self,row):
        with self.agent.lock,getattr(self.session,'lock',nullcontext()):
            status=row['status']
            reason=None
            identifier=row['engine_id']
            if status not in TERMINAL and status!='uncertain':
                s=self.session.state;a=self.agent.state
                control=(getattr(self.session,'copilot_requests',{}).get(identifier)
                         or (s.get('copilot') or {}).get('command') or a.get('copilotControl') or {})
                playback=self.agent.playback_request or {}
                grant=self.agent._external_grants.get(identifier)
                if control.get('id')==identifier:
                    # Text acknowledges admission, not OSC execution. A known
                    # control never falls through to the generic reply path.
                    status={'confirmed':'confirmed','pending':'pending','blocked':'blocked','cancelled':'blocked',
                            'failed':'blocked','unconfirmed':'uncertain'}.get(control.get('status'),'uncertain')
                    reason={'failed':'control-failed','blocked':'control-blocked','cancelled':'control-cancelled'}.get(control.get('status'))
                elif json.loads(row['allocation']).get('operation')=='control':
                    status='uncertain'
                elif json.loads(row['allocation']).get('operation')=='discovery':
                    catalog=s.get('catalogSearch') or {}
                    if catalog.get('externalRequestId')==identifier:
                        status={'queued':'pending','searching':'pending','complete':'confirmed','error':'blocked'}.get(catalog.get('status'),'uncertain')
                        if status=='blocked':reason='discovery-failed'
                    else:status='uncertain'
                    if status=='pending' and grant:
                        try:grant.check()
                        except ValueError as exc:status='blocked';reason=getattr(exc,'code',None)
                elif playback.get('id')==identifier:
                    if playback.get('status') in ('playing','completed'):status='confirmed'
                    elif playback.get('status') in ('failed','cancelled','manual'):status='blocked'
                elif grant:
                    try:grant.check()
                    except ValueError as exc:status='blocked';reason=getattr(exc,'code',None)
                    else:
                        active=self.agent._external_allowance is grant
                        bound=any((value or {}).get('externalRequestId')==identifier for value in
                                  (self.agent.pipeline,self.agent.suspended_preparation))
                        busy=bool(bound or active and (self.agent.worker and self.agent.worker.is_alive() or self.agent.awaiting_catalog))
                        if active and not busy and a['status'] in ('error','held','needs-preparation'):status='blocked'
                        elif not busy and a['status'] not in ('pending','evaluating','preparing','starting'):
                            reply=next((m for m in a['messages'] if m['id']==identifier+'-reply'),None)
                            # Stop needs a settled engine, not merely its text reply.
                            request=next((m for m in a['messages'] if m['id']==identifier),None)
                            if request and playback_requests.intent(request['text'])=='stop':
                                if s.get('phase')=='stopped':status='confirmed'
                            elif reply or ((a.get('proposal') or {}).get('externalRequestId')==identifier
                                    and (not a['proposal'].get('trackIds') or a['proposal'].get('status')=='applied')
                                    and not a['proposal'].get('search')):status='confirmed'
                            elif not active:status='superseded'
                if status!=row['status']:
                    self.store.update(identifier,status,MESSAGES[status],reason)
                    row=self.store.lookup(row['client_id'],row['id'])
            result=self.store.public(row)
            # Only fixed contract copy crosses MCP, never model prose or errors.
            result['message']=MESSAGES[row['status']]
            return result
