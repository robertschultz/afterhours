import io
import json
import unittest
from urllib.error import HTTPError
from afterhours.director import DirectorGate
from afterhours.director_account import check_account


class FakeAccount:
    def __init__(self, body=None, code=200):
        self.body=body;self.code=code;self.calls=[]
    def open(self, request, timeout):
        self.calls.append(request)
        if self.code!=200:raise HTTPError(request.full_url,self.code,'private provider detail',{},io.BytesIO(b'private-secret'))
        return io.BytesIO(json.dumps(self.body).encode())


class AccountTests(unittest.TestCase):
    def test_reads_same_key_account_without_admission_or_other_data(self):
        gate=DirectorGate('fake-key-id:fake-secret',1)
        gate.opener=FakeAccount({'username':'funded-team','credits':{'current_balance':45,'currency':'USD'},'secret':'private-secret'})
        result=gate.check_account()
        self.assertEqual(result['status'],'verified');self.assertEqual(result['balance'],45)
        self.assertEqual(result['account'],'funded-team');self.assertEqual(gate.used,0);self.assertIsNone(gate.lease)
        request=gate.opener.calls[0]
        self.assertEqual(request.method,'GET')
        self.assertEqual(request.full_url,'https://api.fal.ai/v1/account/billing?expand=credits')
        self.assertEqual(request.get_header('Authorization'),'Key fake-key-id:fake-secret')
        self.assertNotIn('secret',json.dumps(result));self.assertNotIn('funded-team',json.dumps(gate.snapshot()))
    def test_missing_key_and_active_session_do_not_send(self):
        gate=DirectorGate();gate.opener=FakeAccount()
        self.assertEqual(gate.check_account()['status'],'missing_key');self.assertEqual(gate.opener.calls,[])
        gate.set_key('test-only-key');gate.references['rivet']='https://fixture.fal.media/rivet.jpg';gate.start(mode='live',consent=True,minutes=2)
        with self.assertRaises(ValueError):gate.check_account()
        self.assertEqual(gate.opener.calls,[])
    def test_permission_missing_is_distinct_from_invalid_key_and_no_retry(self):
        for code,status in ((401,'key_rejected'),(403,'billing_not_accessible'),(429,'unavailable'),(503,'unavailable')):
            fake=FakeAccount(code=code);result=check_account('fake-secret',fake)
            self.assertEqual(result['status'],status);self.assertEqual(len(fake.calls),1)
            self.assertNotIn('balance',result);self.assertNotIn('private',json.dumps(result))
    def test_missing_malformed_and_nonfinite_balance_never_become_zero(self):
        for credits in (None,{}, {'current_balance':float('nan'),'currency':'USD'}, {'current_balance':True,'currency':'USD'}, {'current_balance':45,'currency':'secret'}):
            result=check_account('fake',FakeAccount({'username':'team','credits':credits}))
            self.assertEqual(result['status'],'unavailable');self.assertNotIn('balance',result)
        result=check_account('fake',FakeAccount({'username':'team','credits':{'current_balance':0,'currency':'USD'}}))
        self.assertEqual(result['balance'],0);self.assertEqual(result['status'],'verified')


if __name__=='__main__':unittest.main()
