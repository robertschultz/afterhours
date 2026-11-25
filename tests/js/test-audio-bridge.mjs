import test from 'node:test';
import assert from 'node:assert/strict';
import {AudioBridge} from '../../src/afterhours/web/audio-bridge.js';

function fixture(options={}) {
  const peer={token:'test-session-one',seq:0,commands:[],calls:[],posts:[],offline:false,occupied:false,failState:false};
  const executed=[],outcomes=[],statuses=[];
  const session={current:'imported-original',position:22.9,paused:true,library:['original'],lastCommand:null};
  const reply=(body,status=200)=>({ok:status===200,status,json:async()=>body});
  const fetcher=async(path,init)=>{
    peer.calls.push(path);
    if(peer.offline)throw new TypeError('Network unavailable');
    if(path==='/api/config')return reply({token:peer.token});
    if(init.headers['X-DJ-Token']!==peer.token)return reply({error:'Session changed'},403);
    if(peer.occupied)return reply({error:'Other owner'},409);
    if(path==='/api/claim')return reply({cursor:peer.seq});
    if(path==='/api/state'){
      if(peer.failState){peer.failState=false;throw new TypeError('Acknowledgement lost');}
      peer.posts.push(JSON.parse(init.body));return reply({ok:true});
    }
    if(path.startsWith('/api/commands'))return reply({commands:peer.commands});
    throw new Error('Unexpected test request');
  };
  const bridge=new AudioBridge({client:'test-client-one',fetcher,snapshot:()=>session,
    execute:async c=>executed.push(c.action),onCommand:r=>{outcomes.push(r);session.lastCommand=r;},
    onSession:()=>{session.lastCommand=null;},onStatus:s=>statuses.push(s),...options});
  const enqueue=action=>peer.commands.push({id:++peer.seq,action,created:Date.now()/1000});
  return {peer,bridge,session,executed,outcomes,statuses,enqueue};
}

test('claim publishes current audio snapshot without executing queued commands',async()=>{
  const f=fixture();f.enqueue('stop');await f.bridge.tick();
  assert.equal(f.bridge.phase,'ready');assert.deepEqual(f.executed,[]);
  assert.equal(f.peer.posts[0].position,22.9);assert.equal(f.bridge.cursor,1);
});
test('server restart resets command cursor and old acknowledgement, preserves audio state',async()=>{
  const f=fixture();await f.bridge.tick();f.enqueue('energy');await f.bridge.tick();
  f.peer.token='test-session-two';f.peer.seq=0;f.peer.commands=[];
  await f.bridge.tick();assert.equal(f.bridge.phase,'reconnecting');await f.bridge.tick();
  assert.equal(f.bridge.cursor,0);assert.equal(f.session.lastCommand,null);
  f.enqueue('pause');await f.bridge.tick();
  assert.deepEqual(f.executed,['energy','pause']);
  assert.equal(f.session.position,22.9);assert.deepEqual(f.session.library,['original']);assert.equal(f.session.paused,true);
});
test('offline commands are skipped on recovery, newly submitted command works',async()=>{
  const f=fixture();await f.bridge.tick();f.peer.offline=true;await f.bridge.tick();
  f.peer.offline=false;f.enqueue('resume');await f.bridge.tick();await f.bridge.tick();
  assert.deepEqual(f.executed,[]);f.enqueue('energy');await f.bridge.tick();assert.deepEqual(f.executed,['energy']);
});
test('lost acknowledgement does not replay an executed mutation',async()=>{
  const f=fixture();await f.bridge.tick();f.enqueue('mix');f.peer.failState=true;await f.bridge.tick();
  assert.equal(f.bridge.phase,'reconnecting');await f.bridge.tick();await f.bridge.tick();
  assert.deepEqual(f.executed,['mix']);assert.equal(f.outcomes.length,1);
});
test('another active tab is respected; recovery does not execute its pending commands',async()=>{
  const f=fixture();f.peer.occupied=true;f.enqueue('stop');await f.bridge.tick();await f.bridge.tick();
  assert.equal(f.bridge.phase,'occupied');assert.equal(f.peer.posts.length,0);assert.deepEqual(f.executed,[]);
  assert.equal(f.statuses.length,1);f.peer.occupied=false;await f.bridge.tick();await f.bridge.tick();
  assert.equal(f.bridge.phase,'ready');assert.deepEqual(f.executed,[]);
});
test('parallel tick calls share one exchange and one command execution',async()=>{
  const f=fixture();await f.bridge.tick();f.enqueue('pause');
  const a=f.bridge.tick(),b=f.bridge.tick();assert.equal(a,b);await Promise.all([a,b]);
  assert.deepEqual(f.executed,['pause']);
});
test('failed execution is acknowledged once and later commands still run',async()=>{
  const f=fixture({execute:async c=>{if(c.action==='mix')throw new Error('Start audio first');}});
  await f.bridge.tick();f.enqueue('mix');f.enqueue('energy');await f.bridge.tick();await f.bridge.tick();
  assert.deepEqual(f.outcomes.map(r=>r.result),['failed','executed']);assert.equal(f.outcomes[0].error,'Start audio first');
});
test('malformed or unordered batches dispatch nothing',async()=>{
  for(const commands of [[{id:'1',created:1}],[{id:2,created:1},{id:1,created:1}],[{id:1}]]){
    const f=fixture();await f.bridge.tick();f.peer.commands=commands;await f.bridge.tick();
    assert.deepEqual(f.executed,[]);assert.equal(f.bridge.phase,'reconnecting');
  }
});
test('commands expired during a long preceding operation are skipped',async()=>{
  let now=Date.now();const f=fixture({now:()=>now,execute:async c=>{f.executed.push(c.action);now+=16000;}});
  await f.bridge.tick();f.enqueue('energy');f.enqueue('mix');await f.bridge.tick();
  assert.deepEqual(f.executed,['energy']);assert.equal(f.bridge.cursor,2);
});
test('request timeout releases the poll and later recovery succeeds',async()=>{
  const f=fixture({timeoutMs:10});const working=f.bridge.fetcher;
  f.bridge.fetcher=(_p,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true}));
  await f.bridge.tick();assert.equal(f.bridge.phase,'reconnecting');assert.equal(f.bridge.inflight,null);
  f.bridge.fetcher=working;await f.bridge.tick();assert.equal(f.bridge.phase,'ready');
});
test('failed initial connection recovers without rebooting the library',async()=>{
  const f=fixture();f.peer.offline=true;await f.bridge.tick();f.peer.offline=false;await f.bridge.tick();
  assert.equal(f.bridge.phase,'ready');assert.equal(f.session.position,22.9);
  assert.deepEqual(f.statuses.map(s=>s.phase),['reconnecting','ready']);
});
