"""Named reference admission with fake upload and signaling, never Fal."""
import copy
import tempfile
import unittest
import threading
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from afterhours.director import DirectorGate,APP,REFERENCE_APP
from afterhours.director_cast import DirectorCast
from afterhours.performance_server import make_server

class CastTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.web=Path(self.tmp.name)
        self.visuals={i:{'id':i,'name':name,'poster':'/'+i+'.jpg','prompt':'Preserve '+name+' identity.'} for i,name in [('nova','Nova'),('rivet','Rivet')]}
        for p in self.visuals.values():(self.web/p['poster'][1:]).write_bytes(b'fake-jpg')
        self.profiles=Mock();self.profiles.visual.side_effect=lambda i:copy.deepcopy(self.visuals[i])
        self.profiles.snapshot.return_value={'profiles':[]}
        self.gate=DirectorGate('local-test-key',now=lambda:100)
        self.uploads=[]
        def upload(key,content):self.uploads.append(key);self.gate.references[key]='https://v3.fal.media/'+key+'.jpg'
        self.gate.prepare_reference=upload;self.cast=DirectorCast(self.profiles,self.gate,self.web)
    def prepare(self):
        self.cast.prepare(['nova','rivet'],'nova',True)
        return self.cast.admission(['nova','rivet'],'nova')
    def test_explicit_approved_references_pinned_to_session_model(self):
        cast=self.prepare();a=self.gate.start('nova',mode='live',consent=True,minutes=2,cast=cast)
        self.assertEqual(a['model'],REFERENCE_APP);self.assertEqual([p['name'] for p in a['referenceImages']],['Nova','Rivet'])
        self.assertEqual(self.gate.snapshot()['castIds'],['nova','rivet']);self.assertIsNone(a['imageUrl'])
        self.gate.authorize(a['lease'],'https://wma.fal.run/ice',{'app_id':REFERENCE_APP})
        with self.assertRaises(ValueError):self.gate.authorize(a['lease'],'https://wma.fal.run/ice',{'app_id':APP})
        with self.assertRaises(ValueError):self.gate.authorize(a['lease'],f'https://fal.run/{APP}/ice',{})
        offer={'app_id':REFERENCE_APP,'type':'offer','sdp':'mock-offer-0123456789'}
        self.gate.authorize(a['lease'],'https://wma.fal.run/session',offer)
        with self.assertRaises(ValueError):self.gate.authorize(a['lease'],'https://wma.fal.run/session',offer)
    def test_changed_identity_or_image_requires_new_preparation(self):
        self.prepare();self.visuals['rivet']['prompt']='Updated identity'
        with self.assertRaises(ValueError):self.cast.admission(['nova','rivet'],'nova')
        old=self.uploads[-1];self.prepare();self.assertNotEqual(old,self.uploads[-1])
        (self.web/'rivet.jpg').write_bytes(b'new-approved-jpg')
        with self.assertRaises(ValueError):self.cast.admission(['nova','rivet'],'nova')
    def test_validate_all_before_any_upload(self):
        for ids,consent in [(['nova'],True),(['rivet','rivet'],True),(['nova','rivet'],False)]:
            with self.assertRaises(ValueError):self.cast.prepare(ids,'nova',consent)
        self.visuals['rivet']['poster']=''
        with self.assertRaises(ValueError):self.cast.prepare(['nova','rivet'],'nova',True)
        self.assertEqual(self.uploads,[])
    def test_original_director_still_uses_single_image_and_bound_model(self):
        self.gate.references['nova']='https://v3.fal.media/nova.jpg'
        a=self.gate.start('nova',mode='live',consent=True,minutes=2)
        self.assertEqual(a['model'],APP);self.assertEqual(a['referenceImages'],[])
        with self.assertRaises(ValueError):self.gate.authorize(a['lease'],'https://wma.fal.run/ice',{'app_id':REFERENCE_APP})
    def test_live_lead_http_validates_session_cast_and_identity_without_restarting(self):
        cast=self.prepare();admission=self.gate.start('nova',mode='live',consent=True,minutes=2,cast=cast)
        self.gate.upstream=Mock(side_effect=AssertionError('No provider calls allowed'))
        server=make_server(0,director=self.gate,captures=self.web/'captures',profiles=self.profiles)
        server.performance.command({'action':'character','character':'nova'})
        before=server.performance.snapshot()
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def post(body,token=None):
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}/api/director/lead',method='POST',
                data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Performance-Token':token or server.token}),timeout=2) as reply:
                return json.load(reply)
        payload={'runId':admission['runId'],'fromId':'nova','toId':'rivet'}
        try:
            for patch in [{'runId':'wrong'},{'fromId':'rivet'},{'toId':'missing'}]:
                with self.assertRaises(HTTPError) as err:post({**payload,**patch})
                self.assertEqual(err.exception.code,400)
            with self.assertRaises(HTTPError) as err:post(payload,'wrong-token')
            self.assertEqual(err.exception.code,403)
            self.visuals['rivet']['prompt']='changed after admission'
            with self.assertRaises(HTTPError):post(payload)
            self.visuals['rivet']['prompt']=cast[1]['prompt']
            result=post(payload)
            self.assertEqual(result['character'],'rivet');self.assertEqual(result['performer'],'Rivet')
            self.assertEqual(result['look'],before['look']);self.assertEqual(result['fit'],before['fit'])
            self.assertEqual(self.gate.run,admission['runId']);self.assertTrue(self.gate.snapshot()['active'])
            self.assertEqual(self.gate.lead,'rivet');self.gate.upstream.assert_not_called()
            with self.assertRaises(HTTPError):post(payload)
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
