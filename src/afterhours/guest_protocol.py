"""Scoped foreground guest API, with bounded exact search bindings.

This object does not bind a socket. RoomRequests explicitly activates it on an
already authorized local venue listener. It never calls an audio/provider API.
"""
from collections import OrderedDict,deque
import copy
import math
import secrets
import threading
import time

from afterhours.guest_requests import GuestError,CAPABILITIES,FOLLOW,command_id,exact_id,plain,track_ref


class GuestLimits:
    def __init__(self,clock):self.clock=clock;self.lock=threading.Lock();self.rows=OrderedDict()
    def take(self,operation,guest,peer):
        now=self.clock()
        limits=[(('global','all'),6000),(('peer',peer),1500)] if operation=='ingress' else []
        if operation=='bootstrap':limits.extend([(('bootstrap-peer',peer),120),(('bootstrap','all'),256)])
        elif operation!='ingress':limits.append(((operation,guest),{'state':60,'search':30,'ask':12,'receipt':30,'resume':30,'vote':12,'vote-receipt':30}[operation]))
        with self.lock:
            for key in list(self.rows):
                if not self.rows[key] or self.rows[key][-1]<=now-60:self.rows.pop(key,None)
            for key,cap in limits:
                row=self.rows.get(key,deque())
                while row and row[0]<=now-60:row.popleft()
                if len(row)>=cap:raise GuestError('rate-limited','Give the DJ a moment, then try again.',429,max(1,int(row[0]+61-now)))
            if len(self.rows)+len(limits)>4096:raise GuestError('capacity','Requests are busy. Try again shortly.',503)
            for key,_ in limits:
                self.rows.setdefault(key,deque()).append(now);self.rows.move_to_end(key)


class GuestProtocol:
    def __init__(self,ledger,crate,availability,context,current_night,clock=time.time,on_request=None,polls=None,poll_supported=None):
        self.ledger=ledger;self.crate=crate;self.availability=availability
        self.context=context;self.current_night=current_night;self.clock=clock;self.on_request=on_request or (lambda:None)
        self.lock=threading.RLock();self.scope=None;self.bindings=OrderedDict();self.bootstraps=OrderedDict()
        self.limits=GuestLimits(clock)
        self.polls=polls;self.poll_supported=poll_supported or (lambda:False)

    def activate(self,origin,generation,night):
        with self.lock:
            self.ledger.ensure_night(night)
            self.scope={'origin':origin,'roomId':self.ledger.room_id,'nightId':night,'generation':generation}
            self.bindings.clear();self.bootstraps.clear()

    def deactivate(self):
        with self.lock:self.scope=None;self.bindings.clear();self.bootstraps.clear()

    def common(self):
        if self.scope is None:raise GuestError('invitation-closed','This request page is closed.',404)
        return {'version':1,'scope':copy.deepcopy(self.scope),'revision':self.ledger.revision(self.scope['nightId']),'serverAt':self.clock()}

    def guest(self,bearer):
        return self.ledger.authenticate(self.scope['nightId'],bearer)

    def public_facts(self):
        facts=copy.deepcopy(self.context());now=self.clock()
        # Only the coordinator's public facts cross this boundary. A mismatched
        # night has no reusable program/performer promise.
        if facts.get('nightId')!=self.scope['nightId']:facts={}
        program=facts.get('program');stamp=(program or {}).get('sampledAt')
        if (not program or type(stamp) not in (int,float) or not math.isfinite(stamp)
                or not -2<=now-stamp<3):
            facts={**facts,'program':None};facts.pop('next',None)
        return facts,now

    def public_request(self,request,facts,now):
        # Admission receipts are immutable; their accompanying request is a
        # current projection, using the same freshness rules on every route.
        request=copy.deepcopy(request);program=facts.get('program') or {}
        request['playingNow']=bool(request['playingNow'] and request['stage']=='playing'
            and program.get('playing') is True and request['track']['id']==program.get('track',{}).get('id'))
        promised=request.get('at');upcoming=facts.get('next') or {}
        if (request['stage']!='coming' or type(promised) not in (int,float) or not math.isfinite(promised)
                or promised<=now or program.get('playing') is not True
                or upcoming.get('track',{}).get('id')!=request['track']['id']
                or upcoming.get('estimatedAt')!=promised):request.pop('at',None)
        return request

    def request_result(self,result):
        facts,now=self.public_facts()
        return {**self.common(),**result,'request':self.public_request(result['request'],facts,now)}

    def state(self,guest):
        requests,open_count=self.ledger.own(self.scope['nightId'],guest['id'])
        facts,now=self.public_facts()
        requests=[self.public_request(request,facts,now) for request in requests]
        supported=bool(self.polls and self.poll_supported())
        poll=self.polls.public(self.scope['nightId'],guest['id']) if supported else None
        return {**self.common(),'guest':{**guest,'openCount':open_count,'openLimit':3},
            'capabilities':{**copy.deepcopy(CAPABILITIES),'poll':supported},'follow':copy.deepcopy(FOLLOW),
            'night':copy.deepcopy(facts.get('night',{})),
            'performer':copy.deepcopy(facts.get('performer',{'id':'dj','name':'The DJ'})),
            'program':copy.deepcopy(facts.get('program')),
            **({'next':copy.deepcopy(facts['next'])} if facts.get('next') else {}),'requests':requests,
            **({'poll':poll} if poll is not None else {})}

    def vote(self,guest,payload,peer):
        if (not isinstance(payload,dict) or set(payload)!={'requestId','pollId','trackId'}
                or not isinstance(payload['pollId'],str) or not exact_id(payload['trackId'])):
            raise GuestError('invalid-request','Choose one exact option from this vote.')
        command_id(payload['requestId'])
        prior=self.polls.receipt(self.scope['nightId'],guest['id'],payload['requestId'],self.scope,payload)
        if prior:return {**self.common(),**prior}
        self.limits.take('vote',guest['id'],peer)
        if self.availability.blocked(payload['trackId']) or self.crate.get(payload['trackId']) is None:
            raise GuestError('option-unavailable','This recording is no longer available for the vote.',409)
        result=self.polls.vote(self.scope['nightId'],guest['id'],payload,self.scope)
        return {**self.common(),**result}

    def session(self,payload,bearer,peer):
        if not isinstance(payload,dict) or set(payload)!={'requestId'}:raise GuestError('invalid-request','Use one session request identity.')
        command_id(payload['requestId'])
        if bearer is not None:
            guest=self.guest(bearer);self.limits.take('resume',guest['id'],peer)
            return self.state(guest)
        now=self.clock()
        for key,row in list(self.bootstraps.items()):
            if row['expires']<=now:self.bootstraps.pop(key,None)
        # Bind a bootstrap retry to its peer plus unpredictable command ID;
        # these transient responses are the only memory containing raw tokens.
        key=(peer,payload['requestId'])
        if key in self.bootstraps:
            token=self.bootstraps[key]['response']['guestToken']
            return {**self.state(self.guest(token)),'guestToken':token}
        self.limits.take('bootstrap','',peer)
        guest,token=self.ledger.create_guest(self.scope['nightId'])
        response={**self.state(guest),'guestToken':token}
        self.bootstraps[key]={'expires':now+60,'response':copy.deepcopy(response)}
        while len(self.bootstraps)>256:self.bootstraps.popitem(last=False)
        return response

    def search(self,guest,query,peer):
        if not isinstance(query,str) or not 2<=len(query.strip())<=160 or any(ord(c)<32 for c in query):
            raise GuestError('invalid-query','Search using 2–160 characters.')
        query=query.strip();self.limits.take('search',guest['id'],peer);records=[]
        for track in self.crate.search(query,limit=16):
            if not exact_id(track.get('id')) or self.availability.blocked(track['id']):continue
            try:records.append(track_ref(track))
            except GuestError:continue
            if len(records)==5:break
        identifier='search-'+secrets.token_hex(16);expires=self.clock()+120
        self.bindings[identifier]={'guest':guest['id'],'scope':copy.deepcopy(self.scope),'expires':expires,'records':copy.deepcopy(records)}
        for key,row in list(self.bindings.items()):
            if row['expires']<=self.clock():self.bindings.pop(key,None)
        while len(self.bindings)>512:self.bindings.popitem(last=False)
        return {**self.common(),'query':query,'searchId':identifier,'expiresAt':expires,'scopeLabel':'known-crate','records':records}

    def ask(self,guest,payload,peer):
        if (not isinstance(payload,dict) or set(payload)!={'requestId','searchId','trackId','follow'}
                or not isinstance(payload['searchId'],str) or len(payload['searchId'])>80
                or not exact_id(payload['trackId']) or type(payload['follow']) is not bool):
            raise GuestError('invalid-request','Choose one exact recording from search.')
        command_id(payload['requestId'])
        prior=self.ledger.receipt(self.scope['nightId'],guest['id'],payload['requestId'],self.scope,payload)
        if prior:return self.request_result(prior)
        self.limits.take('ask',guest['id'],peer)
        selection=self.bindings.get(payload['searchId'])
        if (not selection or selection['guest']!=guest['id'] or selection['scope']!=self.scope
                or selection['expires']<=self.clock()):
            raise GuestError('selection-expired','This selection expired. Refresh the same recording before sending.',410)
        track=next((r for r in selection['records'] if r['id']==payload['trackId']),None)
        if not track:raise GuestError('invalid-request','That recording was not in this search.')
        if self.availability.blocked(track['id']) or self.crate.get(track['id']) is None:
            raise GuestError('recording-unavailable','This recording is not available for requests.',409)
        result=self.ledger.ask(self.scope['nightId'],guest['id'],payload,track,self.scope)
        self.on_request()
        return self.request_result(result)

    def handle(self,method,route,query,payload,bearer,peer):
        # Serialize admission with invitation rotation, not with the audio lock.
        with self.lock:
            self.common()
            self.limits.take('ingress','',peer)
            if self.current_night()!=self.scope['nightId']:raise GuestError('invitation-closed','Open the new room invitation.',404)
            if route=='/session' and method=='POST' and not query:return self.session(payload,bearer,peer)
            guest=self.guest(bearer)
            if route=='/state' and method=='GET' and not query:
                self.limits.take('state',guest['id'],peer);return self.state(guest)
            if route=='/search' and method=='GET' and set(query)=={'q'} and len(query['q'])==1:
                return self.search(guest,query['q'][0],peer)
            if route=='/requests' and method=='POST' and not query:return self.ask(guest,payload,peer)
            if self.polls and self.poll_supported():
                if route=='/votes' and method=='POST' and not query:return self.vote(guest,payload,peer)
                if route.startswith('/vote-receipts/') and method=='GET' and not query:
                    self.limits.take('vote-receipt',guest['id'],peer)
                    result=self.polls.receipt(self.scope['nightId'],guest['id'],route.removeprefix('/vote-receipts/'),self.scope)
                    if result:return {**self.common(),**result}
                    raise GuestError('receipt-not-found','No accepted vote was found with this identity.',404)
            if route.startswith('/receipts/') and method=='GET' and not query:
                self.limits.take('receipt',guest['id'],peer)
                result=self.ledger.receipt(self.scope['nightId'],guest['id'],route.removeprefix('/receipts/'),self.scope)
                if result:return self.request_result(result)
                raise GuestError('receipt-not-found','No accepted request was found with this identity.',404)
            raise GuestError('not-found','This guest action is not available.',404)
