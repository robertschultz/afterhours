"""Startup integration: real loopback sockets and disposable child processes."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from afterhours import native_runtime as runtime

SERVICE='''import argparse,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--port',type=int);args,_=p.parse_known_args()
kind='music' if Path(__file__).name=='server.py' else 'performance'
class H(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        values={'/api/config':{'token':'x'*32,'service':'afterhours-music'},
            '/api/set-session':{'active':False,'phase':'stopped','order':[]},
            '/api/performance/config':{'token':'y'*32,'service':'afterhours-performance','director':{}},
            '/api/performance':{'director':{},'mode':'paused','segments':[]}}
        body=json.dumps(values.get(self.path,{})).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
with ThreadingHTTPServer(('127.0.0.1',args.port),H) as server:
    print(f'{kind} fixture listening on {server.server_address}',flush=True)
    server.serve_forever()
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.config={'version':1,'python':sys.executable,**{key:str(self.root) for key in ('musicRoot','performanceRoot','dataRoot','runtimeRoot')}}
        self.sockets=[];ports=[]
        for _ in range(4):
            sock=socket.socket();sock.bind(('127.0.0.1',0));self.sockets.append(sock);ports.append(sock.getsockname()[1])
        self.config['ports']=dict(zip(('music','performance','vdj','reply'),ports))
        for sock in self.sockets:sock.close()
        for name in ('server.py','performance_server.py'):(self.root/name).write_text(SERVICE)
        self.children=[]
        real_launch=runtime.launch
        def launch(config,name):
            child=real_launch(config,name);self.children.append(child);return child
        launcher = patch.object(runtime, 'launch', side_effect=launch)
        launcher.start()
        self.addCleanup(launcher.stop)
        self.addCleanup(self.stop_children)

    def start_services(self):
        result = runtime.ensure(self.config)
        if not result['ready']:
            logs = {name: log.read_text() for name in ('music', 'performance')
                    if (log := self.root / f'{name}.log').is_file()}
            self.fail(f'{result["message"]}\nFixture logs: {logs}')
        return result

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:child.terminate()
            child.wait(timeout=3)

    def test_cold_launch_then_reuse_never_restarts_a_healthy_process(self):
        result=self.start_services()
        self.assertEqual(result['services'],{'music':'started','performance':'started'})
        self.assertTrue(result['ready']);pids=[c.pid for c in self.children]
        result=runtime.ensure(self.config)
        self.assertEqual(result['services'],{'music':'reused','performance':'reused'})
        self.assertEqual([c.pid for c in self.children],pids)
        for child in self.children:
            self.assertIsNone(child.poll())
            self.assertFalse(any('approved' in arg or 'restore-set' in arg for arg in child.args))
        self.assertEqual((self.root/'music.log').stat().st_mode&0o777,0o600)

    def test_starts_only_the_missing_service(self):
        self.start_services()
        music,audience=self.children;music.terminate();music.wait(timeout=3)
        result=runtime.ensure(self.config)
        self.assertEqual(result['services'],{'music':'started','performance':'reused'})
        self.assertIsNone(audience.poll());self.assertEqual(len(self.children),3)

    def test_simultaneous_app_launches_create_one_pair(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:runtime.ensure(self.config),range(2)))
        self.assertTrue(all(r['ready'] for r in results))
        self.assertEqual(len(self.children),2)
        self.assertEqual({r['services']['music'] for r in results},{'started','reused'})

    def test_port_conflict_is_reported_without_replacing_its_owner(self):
        self.start_services()
        music,audience=self.children;music.terminate();music.wait(timeout=3)
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            occupied.bind(('127.0.0.1',self.config['ports']['music']));occupied.listen(5)
            result=runtime.ensure(self.config)
        self.assertFalse(result['ready']);self.assertEqual(result['services']['performance'],'reused')
        self.assertIn('occupied',result['message']);self.assertEqual(len(self.children),2)
        self.assertIsNone(audience.poll())

    def test_failed_child_is_not_retried_and_does_not_stop_existing_service(self):
        self.start_services()
        music,audience=self.children;music.terminate();music.wait(timeout=3)
        (self.root/'server.py').write_text('raise SystemExit(9)')
        result=runtime.ensure(self.config)
        self.assertFalse(result['ready']);self.assertIn('exited',result['message'])
        self.assertEqual(len(self.children),3);self.assertEqual(self.children[-1].returncode,9)
        self.assertIsNone(audience.poll())

    def test_unready_new_child_is_stopped_on_timeout(self):
        self.start_services()
        music,audience=self.children;music.terminate();music.wait(timeout=3)
        (self.root/'server.py').write_text('import time;time.sleep(30)')
        result=runtime.ensure(self.config,startup_timeout=.15)
        self.assertFalse(result['ready']);self.assertIn('did not become ready',result['message'])
        self.assertIsNotNone(self.children[-1].poll());self.assertIsNone(audience.poll())

    def test_private_config_is_read_without_shell_execution_or_budget_flags(self):
        (self.root/'.env.local').write_text('OPENAI_API_KEY="fixture-key"\nFAL_KEY=fixture-fal\nNOT_ALLOWED=secret\n')
        with patch.dict(os.environ,{},clear=True):env=runtime.child_environment(self.config)
        self.assertEqual(env['OPENAI_API_KEY'],'fixture-key');self.assertEqual(env['FAL_KEY'],'fixture-fal')
        self.assertNotIn('NOT_ALLOWED',env)
        self.assertEqual(env['AFTERHOURS_ANALYSIS_REPORTS_DIR'],str(self.root/'.runtime/audio-analysis'))
        self.assertNotIn('fixture-key',json.dumps(self.config))

    def test_manifest_rejects_relative_paths_and_colliding_ports(self):
        path=self.root/'runtime.json';path.write_text(json.dumps(self.config))
        self.assertEqual(runtime.read_manifest(path),self.config)
        self.config['ports']['music']=self.config['ports']['performance'];path.write_text(json.dumps(self.config))
        with self.assertRaises(runtime.StartupError):runtime.read_manifest(path)
        self.config['musicRoot']='relative';path.write_text(json.dumps(self.config))
        with self.assertRaises(runtime.StartupError):runtime.read_manifest(path)


if __name__=='__main__':unittest.main()
