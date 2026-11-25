"""Disposable loopback listener only; no installed services or provider calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode,urlsplit
from urllib.request import Request,urlopen
from unittest.mock import Mock

from afterhours.guest_requests import GuestLedger
from afterhours.guest_protocol import GuestProtocol
from afterhours.record_crate import RecordCrate
from afterhours.recording_availability import RecordingAvailability
from afterhours.room_requests import RoomRequests
from test_guest_requests import records


class GuestHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=[1000.];self.night=['night-fixture'];self.receive=Mock()
        self.ledger=GuestLedger(Path(self.tmp.name)/'guests.sqlite',clock=lambda:self.now[0]);self.addCleanup(self.ledger.close)
        self.crate=RecordCrate();self.addCleanup(self.crate.close);self.crate.remember(records())
        self.availability=RecordingAvailability(clock=lambda:self.now[0]);self.wake=Mock()
        self.facts={'nightId':self.night[0],'night':{'title':'Fixture night'},'performer':{'id':'nova','name':'Nova'},'program':None}
        self.api=GuestProtocol(self.ledger,self.crate,self.availability,lambda:copy.deepcopy(self.facts),lambda:self.night[0],
            clock=lambda:self.now[0],on_request=self.wake)
        self.room=RoomRequests(self.receive,resolve_host=lambda:'127.0.0.1',protocol=self.api);self.addCleanup(self.room.close)
        self.assertFalse(self.room.snapshot()['enabled'])
        self.url=self.room.configure(True,port=0)['url']
    def call(self,route,body=None,token=None,headers=None,url=None):
        request=Request((url or self.url)+'/v1'+route,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if token is not None else {}),**(headers or {})})
        try:
            with urlopen(request,timeout=3) as reply:return reply.status,json.load(reply)
        except HTTPError as exc:return exc.code,json.load(exc)
    def bootstrap(self,command='bootstrap-fixture'):
        status,state=self.call('/session',{'requestId':command});self.assertEqual(status,200);return state
    def test_actual_http_contract_and_safe_headers(self):
        fixture=self.contract_fixture()
        self.assertEqual(fixture['accepted']['request']['stage'],'sent')
        self.assertEqual(fixture['lookup']['receipt'],fixture['accepted']['receipt'])
        self.assertEqual(fixture['quotaError']['code'],'open-limit')
        self.assertEqual(fixture['unknownReceipt']['code'],'receipt-not-found')
        self.receive.assert_not_called()
    def contract_fixture(self):
        boot=self.bootstrap();token=boot['guestToken'];scope=boot['scope']
        _,search=self.call('/search?q=Fixture',token=token)
        command={'requestId':'send-fixture-01','searchId':search['searchId'],'trackId':search['records'][0]['id'],'follow':True}
        status,accepted=self.call('/requests',command,token);self.assertEqual(status,200)
        _,lookup=self.call('/receipts/'+command['requestId'],token=token)
        _,duplicate=self.call('/requests',command,token)
        _,resumed=self.call('/session',{'requestId':'resume-fixture'},token)
        for i in (1,2):
            self.assertEqual(self.call('/requests',{**command,'requestId':f'send-fixture-0{i+1}','trackId':search['records'][i]['id']},token)[0],200)
        status,quota=self.call('/requests',{**command,'requestId':'send-fixture-04','trackId':search['records'][3]['id']},token)
        self.assertEqual(status,409)
        _,sent=self.call('/state',token=token)
        request_id=accepted['request']['id']
        self.ledger.stage(self.night[0],request_id,'listening',round_id='authorized-fixture-round')
        _,listening=self.call('/state',token=token)
        self.ledger.stage(self.night[0],request_id,'coming',at=self.now[0]+120)
        _,coming=self.call('/state',token=token)
        self.ledger.stage(self.night[0],request_id,'playing',playing=True)
        self.facts['program']={'track':search['records'][0],'playing':True,'sampledAt':self.now[0],'positionSeconds':20,'durationSeconds':300}
        _,playing=self.call('/state',token=token)
        # Tied firstAt values and random request IDs do not define which exact
        # recording this fixture declines. Bind the mutation deliberately.
        declined_track_id=search['records'][1]['id']
        other=next(row for row in sent['requests'] if row['track']['id']==declined_track_id)
        self.ledger.stage(self.night[0],other['id'],'declined',reason='not-tonight')
        _,declined=self.call('/state',token=token)
        status,unknown=self.call('/receipts/never-submitted',token=token);self.assertEqual(status,404)
        self.now[0]+=121
        status,expired=self.call('/requests',{**command,'requestId':'expired-send','trackId':search['records'][4]['id']},token)
        self.assertEqual(status,410)
        _,after_expiry=self.call('/requests',command,token)
        return {'basis':'Actual disposable loopback HTTP; fake crate/clock and explicitly injected planner/deck stages; no native/provider/installed-service actions.',
            'version':1,'invitationURL':self.url,'scope':scope,
            'actions':{'acceptedTrackId':command['trackId'],'declinedTrackId':declined_track_id},
            'bootstrap':boot,'search':search,'sendBody':command,
            'accepted':accepted,'lookup':lookup,'duplicate':duplicate,'resumed':resumed,
            'sent':sent,'listening':listening,'coming':coming,'playing':playing,'declined':declined,
            'quotaError':quota,'unknownReceipt':unknown,'expiredSelection':expired,'replayedAfterExpiry':after_expiry}
    def test_auth_origin_host_and_private_paths(self):
        boot=self.bootstrap();token=boot['guestToken']
        self.assertEqual(self.call('/state')[0],401)
        for headers in ({'Origin':'null'},{'Origin':'https://other.test'},{'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.call('/state',token=token,headers=headers)[0],403)
        status,value=self.call('/state',token=token,headers={'Host':'other.test'})
        self.assertEqual(status,404);self.assertNotIn('scope',value)
        self.assertEqual(self.call('/operator',token=token)[0],404)
        origin=boot['scope']['origin']
        for path in ('/api/config','/api/dj-agent','/api/vdj/control'):
            with self.assertRaises(HTTPError) as error:urlopen(origin+path,timeout=2)
            self.assertEqual(error.exception.code,404)
        self.assertEqual(self.call('/state',token='not-a-guest')[0],401)
        self.receive.assert_not_called()
    def test_invitation_generation_rotates_on_disable_and_new_night(self):
        boot=self.bootstrap();old=self.url;token=boot['guestToken']
        self.room.close();self.url=self.room.configure(True,port=0)['url']
        self.assertNotEqual(self.api.scope['generation'],boot['scope']['generation'])
        self.assertEqual(self.call('/session',{'requestId':'resume-same-night'},token)[1]['guest']['id'],boot['guest']['id'])
        old=self.url;self.night[0]='night-new';self.facts={'nightId':self.night[0]}
        status,value=self.call('/state',token=token,url=old);self.assertEqual(status,404)
        self.url=self.room.snapshot()['url'];self.assertNotEqual(old,self.url)
        self.assertEqual(self.call('/session',{'requestId':'old-night-guest'},token)[0],401)
    def test_body_bounds_duplicates_unknown_keys_and_wrong_content_type(self):
        self.assertEqual(self.call('/session',{'requestId':'a'*9000})[0],413)
        self.assertEqual(self.call('/session',{'requestId':'bootstrap-a','approve':True})[0],400)
        self.assertEqual(self.call('/session',{'requestId':'bootstrap-a'},headers={'Content-Type':'text/plain'})[0],415)
        request=Request(self.url+'/v1/session',data=b'{"requestId":"bootstrap-a","requestId":"bootstrap-b"}',headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as error:urlopen(request,timeout=2)
        self.assertEqual(error.exception.code,400)


if __name__=='__main__':unittest.main()
