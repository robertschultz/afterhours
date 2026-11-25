// Real Python server processes on loopback; no VirtualDJ, hardware or production session.
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {AudioBridge} from '../../src/afterhours/web/audio-bridge.js';

// Import-time state is disposable, and the fixture only serves HTTP: it never
// starts server.main's native observer, agent, recovery, or artwork workers.
const fixture = `
from pathlib import Path
import signal, sys
sys.path.insert(0, str(Path.cwd() / 'scripts'))
from test_isolation import test_environment
with test_environment():
    from afterhours import server
    httpd = server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), server.Handler)
    def stop(*_): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    print(f'HTTP fixture ready: http://127.0.0.1:{httpd.server_port}', flush=True)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        httpd.server_close()
        server.ROOM_REQUESTS.close()
        server.RUN_ARCHIVE.close()
        server.DJ_SESSION.crate.close()
        if server.GUEST_LEDGER: server.GUEST_LEDGER.close()
`;

test('real HTTP process restart renews token and accepts sequence one again',{timeout:15000},async()=>{
  const python=process.env.AFTERHOURS_TEST_PYTHON||'python3';
  let child,base;
  async function launch(port){
    child=spawn(python,['-u','-c',fixture,String(port)],{cwd:new URL("../../", import.meta.url),stdio:['ignore','pipe','pipe']});
    base=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(new Error('Isolated server startup timed out')),4000);
      let output='';child.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
      child.once('exit',code=>{clearTimeout(timer);reject(new Error(`Isolated server exited: ${code}`));});
    });
  }
  async function halt(){if(!child||child.exitCode!==null)return;await new Promise(resolve=>{child.once('exit',resolve);child.kill('SIGTERM');});}
  try {
    await launch(0);const port=new URL(base).port;
    const executed=[],audio={position:22.9,paused:true,library:[{id:'local-original'}]};
    const bridge=new AudioBridge({client:'http-restart-test',fetcher:(path,options)=>fetch(base+path,options),snapshot:()=>audio,execute:async c=>executed.push(c.action)});
    const send=async action=>{
      const {token}=await(await fetch(base+'/api/config')).json();
      const response=await fetch(base+'/api/command',{method:'POST',headers:{'Content-Type':'application/json','X-DJ-Token':token},body:JSON.stringify({action,...(action==='auto'?{value:false}:{})})});
      assert.equal(response.status,202);return response.json();
    };
    await bridge.tick();assert.equal(bridge.phase,'ready');
    await send('auto');await bridge.tick();assert.deepEqual(executed,['auto']);
    await halt();await bridge.tick();assert.equal(bridge.phase,'reconnecting');
    await launch(port);await bridge.tick();assert.equal(bridge.phase,'ready');assert.equal(bridge.cursor,0);
    await send('pause');await bridge.tick();assert.deepEqual(executed,['auto','pause']);assert.equal(bridge.cursor,1);
    const restored=await(await fetch(base+'/api/state')).json();
    assert.equal(restored.position,22.9);assert.equal(restored.paused,true);assert.deepEqual(restored.library,audio.library);
  } finally {await halt();}
});
