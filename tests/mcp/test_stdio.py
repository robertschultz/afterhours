"""Real official SDK client/server over OS pipes plus a disposable fake engine."""
import asyncio
from contextlib import asynccontextmanager
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from afterhours.dj_agent import DJAgent
from afterhours.mcp_auth import write_credentials
from afterhours.mcp_control import MCPControl
from afterhours.mcp_receipts import ReceiptStore
from test_dj_agent import decision,settled
from test_mcp_control import FakeSession
from afterhours import server

ROOT=Path(__file__).resolve().parents[2]
SENTINELS=['PRIVATE_PROVIDER_BODY','/private/SECRET_MEDIA_PATH','PRIVATE_OPERATOR_CHAT','PRIVATE_RUNTIME_TOKEN']


class StdioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=ReceiptStore(self.root/'mcp'/'receipts.sqlite3');self.addCleanup(self.store.close)
        self.session=FakeSession()
        self.provider=Mock(return_value=(decision(),{'responseId':'synthetic-provider'}))
        self.agent=DJAgent(self.session,evaluator=self.provider)
        self.control=MCPControl(self.agent,self.store)
        self.httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.url=f'http://127.0.0.1:{self.httpd.server_port}'
        self.operator='synthetic-operator-token-for-disposable-fixture'
        self.patch=patch.multiple(server,MCP_CONTROL=self.control,TOKEN=self.operator)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_http)
        self.client=self.store.pair('Pipe fixture')
        self.config=self.root/'client.json'
        write_credentials(self.config,{'version':1,'url':self.url,'clientId':self.client['clientId'],'credential':self.client['credential']})
        self.env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','HOME':str(self.root),
                  'TMPDIR':str(self.root),'PYTHONDONTWRITEBYTECODE':'1',
                  'AFTERHOURS_TEST_PEER_PORT':str(self.httpd.server_port)}
        self.params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'tests'/'mcp'/'stdio_fixture.py'),'--config',str(self.config)],env=self.env,cwd=self.root)

    def stop_http(self):
        self.httpd.shutdown();self.thread.join(2);self.httpd.server_close()

    @asynccontextmanager
    async def connect(self):
        with (self.root/'stderr.log').open('a+') as errors:
            async with Client(stdio_client(self.params,errlog=errors)) as client:
                yield client

    async def test_real_pipe_catalog_state_direction_duplicate_conflict_and_receipt(self):
        async with self.connect() as client:
            tools=await client.list_tools()
            self.assertEqual({t.name for t in tools.tools},{'afterhours_state','afterhours_direct','afterhours_receipt'})
            state=await client.call_tool('afterhours_state',{})
            self.assertFalse(state.is_error);data=state.structured_content
            payload={'requestId':'pipe-control','text':'copilot on','context':data['context']}
            first=await client.call_tool('afterhours_direct',payload)
            self.assertFalse(first.is_error);self.assertEqual(first.structured_content['status'],'confirmed')
            duplicate=await client.call_tool('afterhours_direct',payload)
            self.assertEqual(first.structured_content,duplicate.structured_content)
            conflict=await client.call_tool('afterhours_direct',{**payload,'text':'copilot off'})
            self.assertTrue(conflict.is_error);self.assertIn('request-conflict',conflict.content[0].text)
            receipt=await client.call_tool('afterhours_receipt',{'requestId':'pipe-control'})
            self.assertEqual(receipt.structured_content['commandId'],first.structured_content['commandId'])
        self.assertEqual(self.session.calls,[('control','on')]);self.provider.assert_not_called()

    async def test_invalid_tool_payloads_and_malformed_frames_never_reach_engine(self):
        with patch.object(self.control,'direct',wraps=self.control.direct) as direct:
            async with self.connect() as client:
                for arguments in ({'requestId':[], 'text':'x','context':'x'},
                                  {'requestId':'large','text':'x'*1601,'context':'x'},
                                  {'requestId':'spoof','text':'x','context':'x','sourceIdentity':{'name':'You'}}):
                    result=await client.call_tool('afterhours_direct',arguments)
                    self.assertTrue(result.is_error)
                direct.assert_not_called()
        for raw in (b'{invalid PRIVATE_OPERATOR_CHAT\n',b'x'*65537+b'\n'):
            process=await asyncio.create_subprocess_exec(self.params.command,*self.params.args,
                env=self.env,cwd=self.root,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await asyncio.wait_for(process.communicate(raw),10)
            self.assertEqual(out,b'');self.assertIn(b'connection closed',err)
            self.assertNotIn(b'PRIVATE_OPERATOR_CHAT',out+err)
        self.assertEqual(self.session.calls,[]);self.provider.assert_not_called()

    async def test_nested_private_data_and_errors_absent_from_wire_and_stderr(self):
        self.session.state.update(provider={'body':SENTINELS[0]},token=SENTINELS[3])
        self.session.state['order'][0].update(path=SENTINELS[1],provider={'body':SENTINELS[0]})
        self.agent.state['messages']=[{'id':'old','role':'user','source':'typed','text':SENTINELS[2],'at':100}]
        wire=[]
        async with self.connect() as client:
            wire.append((await client.call_tool('afterhours_state',{})).model_dump_json())
            with patch.object(self.control,'state',side_effect=ValueError(' '.join(SENTINELS))):
                error=await client.call_tool('afterhours_state',{})
                self.assertTrue(error.is_error);wire.append(error.model_dump_json())
        encoded='\n'.join(wire)+(self.root/'stderr.log').read_text()
        for sentinel in SENTINELS+[self.client['credential'],self.operator]:self.assertNotIn(sentinel,encoded)

    async def test_disconnect_after_dispatch_retains_receipt_without_replay(self):
        music=self.store.pair('Bounded pipe',allowance={'maxCostUSD':.10,'maxProviderCalls':8,'expiresAt':self.store.clock()+300,'prepare':False})
        self.config.unlink()
        write_credentials(self.config,{'version':1,'url':self.url,'clientId':music['clientId'],'credential':music['credential']})
        entered=threading.Event();release=threading.Event()
        def evaluate(_):entered.set();release.wait(5);return decision(),{}
        provider=Mock(side_effect=evaluate);self.agent.evaluator=provider
        try:
            async with self.connect() as client:
                state=await client.call_tool('afterhours_state',{})
                result=await client.call_tool('afterhours_direct',{'requestId':'disconnect','text':'Find a house groove','context':state.structured_content['context']})
                self.assertEqual(result.structured_content['status'],'pending')
                # Admission can reply before the background worker is scheduled.
                # Observe dispatch before disconnecting, without blocking the
                # SDK event loop or assuming a particular runner's timing.
                self.assertTrue(await asyncio.to_thread(entered.wait,5), 'The fake provider must dispatch before disconnect')
            release.set();settled(self.agent)
            async with self.connect() as client:
                result=await client.call_tool('afterhours_receipt',{'requestId':'disconnect'})
                self.assertEqual(result.structured_content['status'],'confirmed')
            self.assertEqual(provider.call_count,1)
        finally:release.set();settled(self.agent)

    async def test_management_uses_operator_token_and_client_routes_reject_browser_origin(self):
        import urllib.error
        from urllib.request import Request,urlopen
        def post(path,body,headers):
            req=Request(self.url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**headers})
            try:
                with urlopen(req,timeout=3) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as error:return error.code,json.load(error)
        status,_=await asyncio.to_thread(post,'/api/mcp/clients',{'action':'list'},{'Authorization':'Bearer '+self.client['credential']})
        self.assertEqual(status,403)
        status,result=await asyncio.to_thread(post,'/api/mcp/clients',{'action':'list'},{'X-DJ-Token':self.operator})
        self.assertEqual(status,200);self.assertNotIn('credential',json.dumps(result))
        status,_=await asyncio.to_thread(post,'/api/mcp/state',{},
            {'Authorization':'Bearer '+self.client['credential'],'Origin':self.url})
        self.assertEqual(status,403)
        status,_=await asyncio.to_thread(post,'/api/dj-agent',{'action':'stop'},{'Authorization':'Bearer '+self.client['credential']})
        self.assertEqual(status,403)

    async def test_actual_pair_list_revoke_cli_does_not_interrupt_another_pending_request(self):
        payload={'requestId':'pending-management','text':'copilot on','context':self.control.state(self.client['credential'])['context']}
        row,_=self.store.claim(self.store.authenticate(self.client['credential']),payload['requestId'],payload,
            self.control._context(),music=False,selection_cost=.02,preparation_cost=.02,selection_calls=2)
        session_file=self.root/'session.json'
        write_credentials(session_file,{'url':self.url,'token':self.operator})
        output=self.root/'paired.json'
        async def cli(*arguments):
            process=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'tests'/'mcp'/'stdio_fixture.py'),
                '--management','--session-file',str(session_file),*arguments,env=self.env,cwd=self.root,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            out,err=await asyncio.wait_for(process.communicate(),10)
            self.assertEqual(process.returncode,0,err.decode())
            self.assertNotIn(self.operator,(out+err).decode())
            return json.loads(out)
        paired=await cli('pair','--name','CLI client','--output',str(output),'--music-usd','0.10',
                         '--music-calls','6','--expires-minutes','10','--prepare')
        self.assertNotIn('credential',paired);self.assertIn('dj:music',paired['scopes'])
        self.assertEqual(output.stat().st_mode & 0o777,0o600)
        saved=json.loads(output.read_text())
        listing=await cli('list')
        self.assertNotIn(saved['credential'],json.dumps(listing))
        self.assertEqual(await cli('revoke',paired['clientId']),{'revoked':True})
        self.assertEqual(self.store.lookup(self.client['clientId'],row['id'])['status'],'dispatching')
        self.store.check(row['engine_id']);self.provider.assert_not_called();self.assertEqual(self.session.calls,[])


if __name__=='__main__':unittest.main()
