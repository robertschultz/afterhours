"""Actual opt-in disposable HTTP assets; no provider/native/production service."""
import json
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request,urlopen

from afterhours import guest_web
import test_guest_http as fixtures


class GuestWebTests(unittest.TestCase):
    setUp=fixtures.GuestHTTPTests.setUp
    call=fixtures.GuestHTTPTests.call
    bootstrap=fixtures.GuestHTTPTests.bootstrap

    def raw(self,url,**headers):
        try:
            with urlopen(Request(url,headers=headers),timeout=3) as response:
                return response.status,response.headers,response.read()
        except HTTPError as error:return error.code,error.headers,error.read()

    def test_invitation_serves_real_local_assets_and_no_operator_credentials(self):
        status,headers,body=self.raw(self.url);text=body.decode()
        self.assertEqual(status,200);self.assertEqual(headers['Referrer-Policy'],'no-referrer')
        self.assertEqual(headers['Cache-Control'],'no-store');self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
        self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
        self.assertIn("connect-src 'self'",headers['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertIn(urlsplit(self.url).path+'/assets/app.mjs',text)
        self.assertNotIn('__CROWD_ROOT__',text);self.assertNotIn('/api/',text)
        self.assertNotIn('guestToken',text);self.assertNotIn('operator',text)
        self.assertNotIn('<script>',text)
        for name,content_type in guest_web.ASSETS.items():
            code,h,content=self.raw(self.url+'/assets/'+name)
            self.assertEqual(code,200);self.assertEqual(h['Content-Type'],content_type)
            self.assertEqual(content,guest_web.asset(name)[0]);self.assertGreater(len(content),100)
        self.receive.assert_not_called();self.wake.assert_not_called()

    def test_assets_reject_wrong_host_path_query_and_private_files(self):
        for suffix in ['../server.py','%2e%2e/server.py','/core.mjs','core.mjs?token=guess',
                       'index.html','server.py','core.mjs/extra','app.js']:
            self.assertEqual(self.raw(self.url+'/assets/'+suffix)[0],404,suffix)
        self.assertEqual(self.raw(self.url+'/assets/core.mjs',Host='foreign.invalid')[0],404)
        self.assertEqual(self.raw(self.url.replace('/r/','/r/wrong')+'/assets/core.mjs')[0],404)
        self.assertEqual(self.raw(urlsplit(self.url).scheme+'://'+urlsplit(self.url).netloc+'/assets/core.mjs')[0],404)
        self.receive.assert_not_called()

    def test_new_night_revokes_page_and_assets_before_serving(self):
        old=self.url;self.night[0]='new-night';self.facts={'nightId':self.night[0]}
        self.assertEqual(self.raw(old+'/assets/core.mjs')[0],404)
        self.assertEqual(self.raw(old)[0],404)
        self.url=self.room.snapshot()['url'];self.assertNotEqual(old,self.url)
        self.assertEqual(self.raw(self.url)[0],200)
        self.assertEqual(self.raw(self.url+'/assets/app.mjs')[0],200)

    def test_free_text_form_cannot_bypass_exact_recording_admission(self):
        request=Request(self.url,data=b'request=stop+music',headers={'Content-Type':'application/x-www-form-urlencoded'})
        with self.assertRaises(HTTPError) as caught:urlopen(request,timeout=3)
        self.assertEqual(caught.exception.code,405)
        self.assertEqual(json.load(caught.exception)['code'],'exact-recording-required')
        self.receive.assert_not_called();self.wake.assert_not_called()
        self.assertEqual(self.ledger.own(self.night[0],self.bootstrap()['guest']['id'])[0],[])

    def test_browser_post_follows_existing_durable_exact_protocol(self):
        self.raw(self.url);boot=self.bootstrap();token=boot['guestToken']
        code,search=self.call('/search?q=Fixture',token=token);self.assertEqual(code,200)
        body={'requestId':'web-protocol-01','searchId':search['searchId'],'trackId':search['records'][0]['id'],'follow':False}
        code,accepted=self.call('/requests',body,token,headers={'Origin':boot['scope']['origin'],'Sec-Fetch-Site':'same-origin'})
        self.assertEqual(code,200);self.assertEqual(accepted['receipt']['change'],{'trackId':body['trackId'],'follow':False})
        code,lookup=self.call('/receipts/'+body['requestId'],token=token)
        self.assertEqual(code,200);self.assertEqual(lookup['receipt'],accepted['receipt'])
        self.receive.assert_not_called();self.assertEqual(self.wake.call_count,1)


class GuestAssetTests(unittest.TestCase):
    def test_only_fixed_assets_and_valid_invitation_paths_can_be_rendered(self):
        for path in ['/r/short','/r/'+('x'*129),'/r/<script>','//evil.test/x','/r/'+('x'*20)+'?q=x']:
            with self.assertRaises(ValueError):guest_web.page(path)
        for name in ['../server.py','/etc/passwd','index.html','core.mjs?q=x']:
            with self.assertRaises(ValueError):guest_web.asset(name)


if __name__=='__main__':unittest.main()
