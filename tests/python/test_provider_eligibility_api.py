"""Metadata-only eligibility route with a disposable local HTTP server."""
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock,patch

from afterhours import server
from afterhours.vdj_session import DJSession
from test_recording_availability import records
from test_provider_eligibility import EVIDENCE


class ProviderEligibilityHTTPTests(unittest.TestCase):
    def test_explicit_evidence_is_authenticated_and_never_opens_a_native_client(self):
        native=Mock(side_effect=AssertionError('Native connection'))
        session=DJSession(threading.Lock(),client_factory=native);tracks=records();session.update(candidates=tracks,order=tracks)
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        body={'action':'record-provider-preview','trackId':tracks[3]['id'],'provider':'Fixture provider',
              'previewSeconds':30,'evidence':EVIDENCE}
        def call(payload,token=True,origin=None):
            client=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if token:headers['X-DJ-Token']=server.TOKEN
            if origin:headers['Origin']=origin
            client.request('POST','/api/set-session',body=json.dumps(payload),headers=headers)
            response=client.getresponse();data=json.loads(response.read());status=response.status;client.close()
            return status,data
        try:
            with patch.object(server,'DJ_SESSION',session):
                self.assertEqual(call(body,token=False)[0],403)
                self.assertEqual(call(body,origin='https://example.com')[0],403)
                self.assertEqual(session.availability.snapshot(),[])
                self.assertEqual(call({**body,'evidence':{**EVIDENCE,'source':'timeout'}})[0],409)
                self.assertEqual(session.availability.snapshot(),[])
                status,result=call(body);self.assertEqual(status,200)
                self.assertEqual(result['recordingAvailability'][0]['code'],'provider-preview-only')
                self.assertFalse(result['order'][3]['playback_verified'])
                self.assertEqual([t['id'] for t in result['order']],[t['id'] for t in tracks])
                self.assertEqual(call({**body,'trackId':'virtualdj:'+'f'*64})[0],409)
                native.assert_not_called()
        finally:
            httpd.shutdown();httpd.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
