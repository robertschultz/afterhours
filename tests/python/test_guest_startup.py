"""Optional request-storage failures cannot disable core music startup.

Imports the actual server only in a disposable data-root subprocess. No listener,
native worker, provider, model allowance or installed service is started.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT=r'''
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock,patch
from afterhours import server

if MODE == 'coordinator':
    server.GUEST_COORDINATOR.start=Mock(side_effect=sqlite3.OperationalError('fixture storage failure'))
server.start_guest_requests()
snapshot=server.ROOM_REQUESTS.snapshot()
with patch.object(server.ROOM_REQUESTS,'resolve_host',side_effect=AssertionError('No venue resolution')) as resolve, \
     patch('afterhours.room_requests.ThreadingHTTPServer',side_effect=AssertionError('No venue listener')) as listener:
    try:server.ROOM_REQUESTS.configure(True)
    except ValueError as exc:enable={'rejected':True,'message':str(exc)}
    else:enable={'rejected':False}
    assert not resolve.called and not listener.called
handler=object.__new__(server.Handler)
handler.headers={'Host':'127.0.0.1','X-DJ-Token':server.TOKEN}
handler.path='/api/guest-requests'
handler.reply=lambda data,status=200:{'status':status,'data':data}
api=handler.do_GET()
session=server.DJ_SESSION.snapshot()
print(json.dumps({'imported':True,'guest':snapshot,'enable':enable,'guestApi':api,
    'coordinatorMissing':server.GUEST_COORDINATOR is None,
    'ledgerMissing':server.GUEST_LEDGER is None,
    'agentGuestDisabled':server.DJ_AGENT.guest_requests is None,
    'coreSessionReady':bool(session.get('id')),'musicActive':session['active']}))
server.ROOM_REQUESTS.close()
server.RUN_ARCHIVE.close()
server.DJ_SESSION.crate.close()
if server.GUEST_LEDGER:server.GUEST_LEDGER.close()
'''


class GuestStartupTests(unittest.TestCase):
    def isolated(self,mode):
        with tempfile.TemporaryDirectory(prefix='guest-startup-regression-') as folder:
            root=Path(folder);runtime=root/'.runtime';runtime.mkdir()
            path=runtime/'guest-requests.sqlite3';saved=b'fixture: invalid SQLite bytes retained exactly'
            if mode=='corrupt':path.write_bytes(saved)
            elif mode=='directory':path.mkdir()
            elif mode=='symlink':
                (root/'saved-guest-data').write_bytes(saved);path.symlink_to(root/'saved-guest-data')
            process=subprocess.run([sys.executable,'-c','MODE='+repr(mode)+'\n'+SCRIPT],
                cwd=Path(__file__).resolve().parent,env={**os.environ,'AFTERHOURS_DATA_ROOT':folder,
                    'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True,timeout=15)
            self.assertEqual(process.returncode,0,process.stderr)
            result=json.loads(process.stdout)
            self.assertTrue(result['imported']);self.assertTrue(result['coreSessionReady'])
            self.assertFalse(result['musicActive']);self.assertTrue(result['agentGuestDisabled'])
            self.assertTrue(result['coordinatorMissing']);self.assertTrue(result['ledgerMissing'])
            self.assertFalse(result['guest']['supported']);self.assertFalse(result['guest']['enabled'])
            self.assertIsNone(result['guest']['url']);self.assertTrue(result['enable']['rejected'])
            self.assertEqual(result['guestApi']['status'],503)
            self.assertEqual(result['guestApi']['data']['code'],'temporarily-unavailable')
            self.assertNotIn(folder,json.dumps(result))
            if mode in ('corrupt','symlink'):self.assertEqual(path.read_bytes(),saved)
            if mode=='symlink':self.assertTrue(path.is_symlink())
            if mode=='directory':self.assertTrue(path.is_dir())
    def test_corrupt_optional_store_preserved_and_no_legacy_listener_fallback(self):self.isolated('corrupt')
    def test_unopenable_optional_store_does_not_stop_music_startup(self):self.isolated('directory')
    def test_symlinked_store_remains_untouched_and_guest_ingress_closed(self):self.isolated('symlink')
    def test_coordinator_initial_read_failure_is_also_optional(self):self.isolated('coordinator')


if __name__=='__main__':unittest.main()
