import test from 'node:test';
import assert from 'node:assert/strict';
import {MusicConnection} from '../../src/afterhours/web/music-client.js';
const response = (value, status=200) => ({ok:status===200,status,json:async()=>value});
const state = {connected:true,decks:[{deck:1,title:'Original test track',playing:false}],automix:false};
const preview = {connected:true,read_only:true,automix:false,availability:'automix-off',queue_count:8,upcoming:[],complete:false,snapshot_atomic:false};
function harness({command, catalog, status, queue}={}) {
  const calls=[];
  const client=new MusicConnection(async(path, options)=>{
    calls.push({path,body:options?.body && JSON.parse(options.body)});
    if(path==='/api/config')return response({token:'test-only-token'});
    if(path.endsWith('/status'))return status ? status() : response(state);
    if(path.endsWith('/catalog'))return catalog ? catalog() : response({folder:{id:'f'.repeat(64)},selected:{id:'a'.repeat(64)}});
    if(path.endsWith('/queue'))return queue ? queue() : response(preview);
    return command ? command() : response({sent:true,verified:true});
  },()=>100000);
  return {client,calls};
}
test('disconnected controls never dispatch',async()=>{
  const {client,calls}=harness(); await assert.rejects(client.command('play'),/out of date/);assert.equal(calls.length,0);
});
test('read-only connection uses no playback endpoints',async()=>{
  const {client,calls}=harness();await client.refresh();assert.equal(client.ready,true);assert.ok(calls.every(c=>!c.body));
});
test('a mutation is dispatched exactly once and needs a new read',async()=>{
  const {client,calls}=harness();await client.refresh();await client.command('play',{deck:1});
  await assert.rejects(client.command('play',{deck:1}),/out of date/);
  assert.equal(calls.filter(c=>c.body).length,1);assert.deepEqual(calls.at(-1).body,{deck:1,action:'play',execute:true});
});
test('lost command response is not automatically retried',async()=>{
  const {client,calls}=harness({command:()=>{throw new Error('lost reply');}});await client.refresh();
  await assert.rejects(client.command('queue-selected'),/lost reply/);assert.equal(client.ready,false);assert.equal(client.busy,false);assert.equal(calls.filter(c=>c.body).length,1);
});
test('busy connection rejects overlapping actions',async()=>{
  let release;const {client,calls}=harness({command:()=>new Promise(r=>release=r)});await client.refresh();
  const pending=client.command('play');await assert.rejects(client.command('pause'),/progress/);release(response({sent:true,verified:true}));await pending;assert.equal(calls.filter(c=>c.body).length,1);
});
test('missing folder preserves transport but clears old selection',async()=>{
  const {client}=harness({catalog:()=>response({error:'Select a folder'},400)});client.catalog={selected:{id:'stale'}};
  const result=await client.refresh();assert.equal(result.catalogError,'Select a folder');assert.equal(client.ready,true);assert.equal(client.catalog,null);
});
test('failed connection invalidates stale track and deck state',async()=>{
  const {client}=harness({status:()=>response({error:'No OSC response'},503)});client.status=state;client.catalog={selected:{id:'stale'}};
  await assert.rejects(client.refresh(),/No OSC/);assert.equal(client.ready,false);assert.equal(client.status,null);assert.equal(client.catalog,null);
});
test('expired state cannot command the engine',async()=>{
  const {client,calls}=harness();await client.refresh();client.clock=()=>120000;
  await assert.rejects(client.command('play'),/out of date/);assert.equal(calls.filter(c=>c.body).length,0);
});
test('session expiry discards token for the next explicit check',async()=>{
  let expired=false;const {client}=harness({status:()=>expired?response({error:'Session expired'},403):response(state)});
  await client.refresh();expired=true;await assert.rejects(client.refresh(),/Session expired/);assert.equal(client.token,null);assert.equal(client.ready,false);
});
test('background transport poll preserves catalog and never starts a catalog request',async()=>{
  let catalogReads=0;const {client,calls}=harness({catalog:()=>{catalogReads++;if(catalogReads>1)throw new Error('Catalog would stall');return response({selected:{id:'kept'}});}});
  await client.refresh();const selection=client.catalog;
  await client.refresh({includeCatalog:false});assert.equal(client.catalog,selection);assert.equal(client.ready,true);assert.equal(catalogReads,1);
  assert.equal(calls.at(-1).path,'/api/vdj/status');
});
test('a click during polling waits for fresh state and dispatches once',async()=>{
  let release;const {client,calls}=harness();await client.refresh();
  const original=client.fetcher;client.fetcher=(path,options)=>path.endsWith('/status')?new Promise(r=>release=r):original(path,options);
  const poll=client.poll();assert.equal(client.ready,true);assert.equal(client.polling,true);
  const command=client.command('takeover');assert.equal(client.waitingCommand,true);
  assert.equal(calls.filter(c=>c.body).length,0);
  release(response({...state,automix:true}));await poll;await command;
  assert.equal(calls.filter(c=>c.body).length,1);assert.equal(calls.at(-1).body.action,'takeover');
  assert.equal(client.waitingCommand,false);assert.equal(client.polling,false);assert.equal(client.ready,false);
});
test('rapid second click cannot create a second waiting command',async()=>{
  let release;const {client,calls}=harness();await client.refresh();
  const original=client.fetcher;client.fetcher=(path,options)=>path.endsWith('/status')?new Promise(r=>release=r):original(path,options);
  const poll=client.poll();const first=client.command('takeover');
  await assert.rejects(client.command('next'),/progress/);
  release(response(state));await poll;await first;assert.equal(calls.filter(c=>c.body).length,1);
});
test('failed background read cancels the waiting command without dispatch',async()=>{
  let fail;const {client,calls}=harness();await client.refresh();
  client.fetcher=()=>new Promise((_,reject)=>fail=reject);
  const poll=client.poll();const command=client.command('takeover');
  const checked=Promise.all([assert.rejects(poll,/timeout/),assert.rejects(command,/state check failed; no command was sent/)]);
  fail(new Error('status timeout'));await checked;
  assert.equal(calls.filter(c=>c.body).length,0);assert.equal(client.needsRefresh,true);assert.equal(client.busy,false);assert.equal(client.waitingCommand,false);
});
test('uncertain command cannot be cleared by automatic polling',async()=>{
  const {client,calls}=harness({command:()=>response({sent:true,verified:false})});await client.refresh();await client.command('next');
  const count=calls.length;await assert.rejects(client.poll(),/Check connection/);assert.equal(calls.length,count);
});
test('explicit catalog reads do not accept deferred mutations',async()=>{
  let release;const {client,calls}=harness({catalog:()=>new Promise(r=>release=r)});
  const refresh=client.refresh();await new Promise(r=>setImmediate(r));
  await assert.rejects(client.command('play'),/progress/);release(response({selected:null}));await refresh;
  assert.equal(calls.filter(c=>c.body).length,0);
});
test('waiting for a poll does not bypass the state expiry guard',async()=>{
  let release;const {client,calls}=harness();await client.refresh();
  client.fetcher=()=>new Promise(r=>release=r);
  const poll=client.poll();const command=client.command('takeover');
  // Reading a clock once to stamp status, then observing expiry models a tab
  // suspended between completion of the read and continuation of the action.
  let ticks=0;client.clock=()=>++ticks===1?100000:120000;
  release(response(state));await poll;await assert.rejects(command,/out of date/);
  assert.equal(calls.filter(c=>c.body).length,0);
});
test('queue is read explicitly without mutations or refreshing transport freshness',async()=>{
  const {client,calls}=harness();await client.refresh();const checked=client.checkedAt;
  client.clock=()=>101000;await client.readQueue();assert.equal(client.checkedAt,checked);
  assert.equal(client.queueFresh,true);assert.equal(client.queue.availability,'automix-off');
  assert.ok(calls.every(c=>!c.body));assert.equal(calls.filter(c=>c.path.endsWith('/queue')).length,1);
});
test('queue age expires even while deck polling stays fresh',async()=>{
  const {client,calls}=harness();await client.refresh();await client.readQueue();
  client.clock=()=>114000;await client.poll();client.clock=()=>115001;
  assert.equal(client.ready,true);assert.equal(client.queueFresh,false);
  assert.equal(calls.filter(c=>c.path.endsWith('/queue')).length,1);
});
test('changed deck or Automix state invalidates upcoming metadata',async()=>{
  for(const changed of [{...state,automix:true},{...state,decks:[{...state.decks[0],title:'Another track'}]}]) {
    let current=state;const {client}=harness({status:()=>response(current)});await client.refresh();await client.readQueue();
    current=changed;await client.poll();assert.equal(client.queueFresh,false);
  }
});
test('preview does not become current again after a command and status refresh',async()=>{
  const {client}=harness();await client.refresh();await client.readQueue();await client.command('play');
  await client.refresh({includeCatalog:false});assert.equal(client.ready,true);assert.equal(client.queueFresh,false);
});
test('queue reads cannot clear uncertainty or dispatch during another read',async()=>{
  const {client,calls}=harness();await client.refresh();await client.command('next');
  await assert.rejects(client.readQueue(),/Check connection/);assert.equal(calls.filter(c=>c.path.endsWith('/queue')).length,0);
  let release;const h=harness({queue:()=>new Promise(r=>release=r)});await h.client.refresh();const reading=h.client.readQueue();
  await assert.rejects(h.client.command('takeover'),/progress/);await assert.rejects(h.client.readQueue(),/progress/);
  release(response(preview));await reading;assert.equal(h.calls.filter(c=>c.body).length,0);
});
test('old backend preview failure leaves fresh transport usable',async()=>{
  const {client}=harness({queue:()=>response({error:'Unknown API path'},404)});await client.refresh();
  await assert.rejects(client.readQueue(),/Unknown API/);assert.equal(client.queueUnsupported,true);assert.match(client.queueError,/Restart Afterhours at a break/);
  assert.equal(client.ready,true);assert.equal(client.queue,null);await client.command('play');
});
test('failed preview clears old rows and recovers only on another explicit read',async()=>{
  let failed=false;const {client,calls}=harness({queue:()=>failed?response({error:'Changed queue'},503):response(preview)});
  await client.refresh();await client.readQueue();failed=true;await assert.rejects(client.readQueue(),/Changed queue/);
  assert.equal(client.queue,null);assert.equal(client.ready,true);assert.equal(client.busy,false);
  await client.poll();assert.equal(calls.filter(c=>c.path.endsWith('/queue')).length,2);
  failed=false;await client.readQueue();assert.equal(client.queueError,'');assert.equal(client.queueFresh,true);
});
test('expired session during a queue read requires a new connection check',async()=>{
  const {client}=harness({queue:()=>response({error:'Expired token'},403)});await client.refresh();await assert.rejects(client.readQueue(),/Expired/);
  assert.equal(client.token,null);assert.equal(client.ready,false);assert.match(client.queueError,/Check connection/);
});
test('malformed preview never becomes visible data',async()=>{
  for(const value of [{}, {...preview,queue_count:-1}, {...preview,availability:'available'}, {...preview,upcoming:[{offset:1,title:'bad',artist:12}]}]) {
    const {client}=harness({queue:()=>response(value)});await client.refresh();await assert.rejects(client.readQueue(),/Invalid upcoming/);
    assert.equal(client.queue,null);assert.equal(client.queueFresh,false);
  }
});
test('queue request uses a bounded five second client timeout',async()=>{
  const {client}=harness();await client.refresh();const old=client.fetcher;let timeout;
  const original=AbortSignal.timeout;AbortSignal.timeout=ms=>{timeout=ms;return original(ms);};
  try {await client.readQueue();assert.equal(timeout,5000);} finally {AbortSignal.timeout=original;client.fetcher=old;}
});

test('fresh starts and fitted insertions use the authenticated engine contracts', async () => {
  const calls = [];
  const client = new MusicConnection(async (path, options) => {
    calls.push([path, options.body ? JSON.parse(options.body) : null, options.headers]);
    return {ok: true, json: async () => path === '/api/config' ? {token:'fixture-token'} : {status:'ok'}};
  });
  await client.startNewQueue({messageId:'fresh-fixture', approveRequest:true});
  await client.previewInsertion('virtualdj:fixture');
  await client.insertTrack('virtualdj:fixture');
  assert.deepEqual(calls.slice(1).map(([path, data]) => [path, data.action]), [
    ['/api/dj-agent', 'start-new-queue'],
    ['/api/set-session', 'preview-insertion'],
    ['/api/set-session', 'insert-track'],
  ]);
  assert.ok(calls.slice(1).every(([, , headers]) => headers['X-DJ-Token'] === 'fixture-token'));
});
