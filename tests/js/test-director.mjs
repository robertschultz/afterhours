import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {DirectorController} from '../../src/afterhours/web/director-controller.js';
import {connectionFailure,connectionDiagnostic} from '../../src/afterhours/web/director-diagnostics.js';
function fixture(){
  const sent=[],changes=[],media=[];let callbacks,closed=0,ended=0,throws=false;
  const controller=new DirectorController({open:o=>{callbacks=o;return{send:x=>{sent.push(x);if(throws)throw Error('uncertain');},close:async()=>{closed++;}};},onChange:s=>changes.push(s),onMedia:s=>media.push(s),onEnd:()=>ended++});
  return {controller,sent,changes,media,get callbacks(){return callbacks;},get closed(){return closed;},get ended(){return ended;},uncertain(){throws=true;},start(){controller.start({prompt:'Rivet at the decks',imageUrl:'https://example.invalid/approved-reference',deadline:Date.now()/1000+60});}};
}
test('only matching configured acknowledgement allows updates, not transport live or applied',async()=>{
  const f=fixture();f.start();f.callbacks.onState('live');assert.throws(()=>f.controller.direct('wave'));
  f.callbacks.onData(JSON.stringify({type:'prompt_applied',prompt_version:1}));assert.throws(()=>f.controller.direct('wave'));
  f.callbacks.onData(JSON.stringify({type:'configured',prompt_version:2}));assert.equal(f.controller.state.configured,false);
  f.callbacks.onData(JSON.stringify({type:'configured',prompt_version:1}));assert.equal(f.controller.direct('wave'),2);
  assert.deepEqual(f.sent[1],{type:'prompt',prompt_version:2,prompt:'wave',replan:true});await f.controller.stop();
});
test('offer matches the published Director receive contract without sending local media',async()=>{
  const contract=JSON.parse(readFileSync(new URL('../fixtures/director-media-contract.json',import.meta.url)));
  const f=fixture();f.start();assert.deepEqual(f.callbacks.receive,contract['x-fal-media'].receive.map(track=>track.kind));assert.equal(f.callbacks.localStream,undefined);
  assert.deepEqual(contract['x-fal-media'].send,[]);
  assert.equal(f.sent[0].image_url,'https://example.invalid/approved-reference');assert.equal(f.sent[0].resolution,'768p');assert.equal(f.sent[0].audio_url,undefined);await f.controller.stop();
});
test('named-cast configure uses named references and keeps the model in its receipt',async()=>{
  const f=fixture(), model='minimax/h3-max/reference-director';
  const referenceImages=[{name:'Nova',url:'https://example.invalid/nova.jpg'},{name:'Rivet',url:'https://example.invalid/rivet.jpg'}];
  f.controller.start({prompt:'Nova at the decks; Rivet waits off camera.',model,referenceImages,deadline:Date.now()/1000+60});
  assert.deepEqual(f.sent[0],{type:'configure',protocol_version:1,prompt_version:1,prompt:'Nova at the decks; Rivet waits off camera.',resolution:'768p',aspect_ratio:'16:9',memory:12,reference_images:referenceImages});
  assert.equal(f.controller.receipt().model,model);
  f.controller.message({type:'configured',prompt_version:1});f.controller.direct('Rivet takes over from Nova.');
  assert.deepEqual(f.sent[1],{type:'prompt',prompt_version:2,prompt:'Rivet takes over from Nova.',replan:true});
  await f.controller.stop();
});
test('invalid named references fail before opening a session',()=>{
  const f=fixture();
  for(const referenceImages of [[],[{name:'Rivet',url:'https://example.invalid'}],[{name:'Nova',url:'http://private.invalid'},{name:'Rivet',url:'https://example.invalid'}]]){
    assert.throws(()=>f.controller.start({model:'minimax/h3-max/reference-director',referenceImages,deadline:Date.now()/1000+60}));
  }
  assert.equal(f.callbacks,undefined);assert.equal(f.sent.length,0);
});
test('uncertain send consumes its version, no replay and no acknowledgement for older direction',async()=>{
  const f=fixture();f.start();f.controller.message({type:'configured',prompt_version:1});f.uncertain();assert.throws(()=>f.controller.direct('nod'));assert.equal(f.controller.state.version,2);
  assert.throws(()=>f.controller.direct('look'));assert.equal(f.controller.state.version,3);
  f.controller.message({type:'prompt_applied',prompt_version:2});assert.equal(f.controller.state.version,3);assert.match(f.controller.state.ack,/waiting for acknowledgement/);assert.equal(f.sent.length,3);await f.controller.stop();
});
test('chunk telemetry is distinct from prompt acceptance and malformed values ignored',async()=>{
  const f=fixture();f.start();f.controller.message('bad json');f.controller.message({type:'configured',prompt_version:1});f.controller.direct('nod');f.controller.message({type:'prompt_applied',prompt_version:2});assert.match(f.controller.state.ack,/accepted for generation/);
  f.controller.message({type:'chunk',chunk_index:3,prompt_version:1,buffer_depth_seconds:5,generation_seconds:3.4});assert.equal(f.controller.state.chunk.version,1);assert.match(f.controller.state.ack,/New video generated/);
  f.controller.message({type:'chunk',chunk_index:'bad',prompt_version:999});assert.equal(f.controller.state.chunk.index,3);await f.controller.stop();
});
test('stop closes once, invalidates late media/configuration, never reconnects',async()=>{
  const f=fixture();f.start();const cb=f.callbacks;await f.controller.stop();cb.onData('{"type":"configured","prompt_version":1}');cb.onMedia({});cb.onState('live');assert.equal(f.controller.state.configured,false);assert.equal(f.closed,1);assert.deepEqual(f.media,[null]);assert.equal(f.sent.filter(s=>s.type==='configure').length,1);
});
test('provider terminal errors and stream exhaustion close the session',async()=>{
  for(const event of [{type:'error',code:'generation_failed'},{type:'stream_exhausted'}]){const f=fixture();f.start();f.controller.message(event);await Promise.resolve();assert.equal(f.closed,1);assert.equal(f.ended,1);assert.equal(f.controller.state.phase,'stopped');}
});
test('session timer closes even if provider never configures',async()=>{
  const f=fixture();f.controller.start({prompt:'Rivet',imageUrl:'https://example.invalid',deadline:Date.now()/1000+.02});await new Promise(r=>setTimeout(r,40));assert.equal(f.closed,1);assert.match(f.controller.state.ack,/time limit/);
});
test('duplicate start cannot open a second paid connection',async()=>{const f=fixture();f.start();assert.throws(()=>f.start());assert.equal(f.sent.length,1);await f.controller.stop();});
test('only received frames complete the sample, not configuration or elapsed time',async()=>{
  let clock=1000,callbacks;const events=[];
  const c=new DirectorController({now:()=>clock,open:o=>{callbacks=o;return {send(){},close:async()=>{}};},onEnd:r=>events.push(r)});
  c.start({prompt:'Nova',imageUrl:'https://example.invalid/reference',deadline:100000});c.message({type:'configured',prompt_version:1});
  clock=11000;assert.equal(c.state.frames,0);c.frame(20);assert.equal(c.state.startupMs,10000);assert.equal(c.state.phase,'live');
  clock=40959;c.frame(738);assert.equal(c.state.phase,'live');c.frame(739);assert.equal(c.state.phase,'stopped');assert.equal(events[0].presentedSeconds,30);assert.equal(events[0].reason,'30-second sample complete');
});
test('duplicate chunks do not inflate budget, in-flight reserve stops at 45 generated seconds',async()=>{
  const f=fixture();f.start();f.controller.message({type:'configured',prompt_version:1});
  const chunk={type:'chunk',chunk_index:0,prompt_version:1,generated_frame_count:360,playback_seconds:12};
  f.controller.message(chunk);f.controller.message(chunk);assert.equal(f.controller.state.generatedSeconds,15);
  f.controller.message({...chunk,chunk_index:1});assert.equal(f.closed,0);f.controller.message({...chunk,chunk_index:2});assert.equal(f.controller.state.generatedSeconds,45);assert.equal(f.closed,1);
});
test('stalled video stops, nonterminal provider diagnostics do not terminate the stream',async()=>{
  let clock=1000;const c=new DirectorController({now:()=>clock,open:()=>({send(){},close:async()=>{}})});
  c.start({prompt:'Nova',imageUrl:'https://example.invalid/reference',deadline:100000});c.message({type:'configured',prompt_version:1});c.message({type:'error',code:'stale_prompt_version'});assert.equal(c.state.phase,'live');
  c.frame(1);clock=4000;c.checkStall();assert.equal(c.state.stallCount,1);c.checkStall();assert.equal(c.state.stallCount,1);clock=9000;c.checkStall();assert.equal(c.state.phase,'stopped');
});
test('SDK failed-state callback does not discard its following error and diagnostics',async()=>{
  const f=fixture();f.start();const cb=f.callbacks;
  cb.onDiagnostic({kind:'progress',phase:'signaling',detail:{step:'session',status:502}});
  cb.onState('failed');assert.equal(f.closed,0);
  cb.onDiagnostic({kind:'failure',message:'Fal signaling returned HTTP 402; no retry was submitted.'});
  cb.onError(new Error('Fal signaling returned HTTP 402; no retry was submitted.'));
  await new Promise(r=>setTimeout(r,5));
  assert.equal(f.closed,1);assert.equal(f.ended,1);assert.match(f.controller.state.ack,/HTTP 402/);
  const receipt=f.controller.receipt();assert.equal(receipt.frames,0);
  assert.equal(receipt.events.filter(x=>x.type==='connection_diagnostic').length,2);
  assert.equal(f.sent.filter(x=>x.type==='configure').length,1);
});
test('missing SDK error still ends once and late callbacks cannot replace the receipt',async()=>{
  const f=fixture();f.start();const cb=f.callbacks;cb.onState('failed');
  await new Promise(r=>setTimeout(r,5));const reason=f.controller.state.ack;
  cb.onError(new Error('later HTTP 500'));assert.equal(f.controller.state.ack,reason);assert.equal(f.closed,1);
});
test('connection evidence retains only approved counts and classifications',()=>{
  const secret='Bearer secret-key https://private.invalid?token=secret-key 192.168.1.42';
  assert.ok(!connectionFailure(new Error(secret)).includes('secret-key'));
  assert.equal(connectionDiagnostic({phase:'network-path',detail:{address:secret}}),null);
  assert.equal(connectionDiagnostic({phase:'ice-servers',detail:{source:secret}}).label,'ICE discovery unavailable');
  assert.deepEqual(connectionDiagnostic({phase:'ice-gathering',detail:{host:2,srflx:1,relay:0,address:secret}}),{source:'ice-gathering',label:'Candidates: host 2, server-reflexive 1, relay 0'});
  assert.equal(connectionDiagnostic({phase:'signaling',detail:{step:secret,status:402}}),null);
  assert.deepEqual(connectionDiagnostic({phase:'validation',detail:{field:'body.sdp',issue:'media_contract'}}),{source:'validation',label:'body.sdp: incompatible media offer'});
  assert.deepEqual(connectionDiagnostic({phase:'validation',detail:{field:secret,issue:secret}}),{source:'validation',label:'request: validation failed'});
  assert.match(connectionFailure('Fal signaling returned HTTP 422'),/video\/audio connection offer/);
});
test('credit verification failure stays specific, private and single-attempt',async()=>{
  const f=fixture();f.start();const cb=f.callbacks;
  cb.onDiagnostic({kind:'progress',phase:'billing',detail:{issue:'credit_verification_unavailable',input:'private-key'}});
  cb.onState('failed');
  cb.onError(new Error('Fal could not verify session credit (HTTP 503).'));
  await new Promise(r=>setTimeout(r,5));
  assert.equal(f.closed,1);assert.equal(f.ended,1);assert.equal(f.controller.receipt().frames,0);
  assert.match(f.controller.state.ack,/key belongs to the account/);
  assert.doesNotMatch(f.controller.state.ack,/service is unavailable|insufficient/);
  assert.doesNotMatch(JSON.stringify(f.controller.receipt()),/private-key/);
  assert.match(f.controller.receipt().events.find(x=>x.source==='billing').label,/does not confirm an empty balance/);
  assert.equal(connectionDiagnostic({phase:'billing',detail:{issue:'private-key'}}),null);
  assert.match(connectionFailure('HTTP 503'),/could not open the video session/);
});

test('continuous live mode crosses 30 seconds and stops only on Off or its limits',async()=>{
  const f=fixture();f.controller.start({prompt:'Nova',imageUrl:'https://example.invalid/reference',mode:'live',sampleSeconds:null,generatedLimitSeconds:285,deadline:Date.now()/1000+420});
  f.controller.message({type:'configured',prompt_version:1});f.controller.frame(1);f.controller.frame(2401);
  assert.equal(f.controller.state.phase,'live');assert.ok(f.controller.state.presentedSeconds>100);assert.equal(f.controller.receipt().targetSeconds,null);
  await f.controller.stop('Live video switched off');assert.equal(f.closed,1);assert.equal(f.controller.receipt().mode,'live');
});
test('live mode preserves its footage guard and rejects unbounded session input',async()=>{
  const f=fixture();assert.throws(()=>f.controller.start({mode:'live',sampleSeconds:null,generatedLimitSeconds:Infinity,deadline:Date.now()/1000+60}));
  f.controller.start({prompt:'Nova',imageUrl:'https://example.invalid/reference',mode:'live',sampleSeconds:null,generatedLimitSeconds:105,deadline:Date.now()/1000+240});f.controller.message({type:'configured',prompt_version:1});
  for(let i=0;i<7;i++)f.controller.message({type:'chunk',chunk_index:i,prompt_version:1,generated_frame_count:360,playback_seconds:12});
  assert.equal(f.closed,1);assert.equal(f.controller.state.generatedSeconds,105);assert.match(f.controller.state.ack,/footage limit/);
});

test('configured streams without a picture stop once after 60 seconds, with no retry',async()=>{
  for(const mode of ['sample','live']){
    let clock=1000,closed=0,ended=0,callbacks;const sent=[];
    const c=new DirectorController({now:()=>clock,open:o=>{callbacks=o;return {send:x=>sent.push(x),close:async()=>{closed++;}};},onEnd:()=>ended++});
    c.start({prompt:'Nova',imageUrl:'https://example.invalid/reference',mode,sampleSeconds:mode==='live'?null:30,generatedLimitSeconds:mode==='live'?1785:45,deadline:1921});
    clock=10000;callbacks.onState('live');callbacks.onData({type:'configured',prompt_version:1});
    clock=60999;c.checkStall();assert.equal(c.state.phase,'live');
    // Repeated successful configuration does not buy more waiting time.
    callbacks.onData({type:'configured',prompt_version:1});clock=61000;c.checkStall();
    assert.equal(c.state.phase,'stopped');assert.match(c.state.ack,/No video received within 60 seconds; no automatic retry/);
    callbacks.onMedia({});callbacks.onData({type:'configured',prompt_version:1});c.frame(1);c.checkStall();await c.stop();
    assert.equal(c.state.frames,0);assert.equal(c.firstFrameDeadline,null);assert.equal(closed,1);assert.equal(ended,1);
    assert.equal(sent.filter(x=>x.type==='configure').length,1);assert.equal(sent.filter(x=>x.type==='stop').length,1);
  }
});

test('a decoded picture cancels the startup deadline; subsequent stalls use the frame guard',async()=>{
  let clock=1000;const c=new DirectorController({now:()=>clock,open:()=>({send(){},close:async()=>{}})});
  c.start({prompt:'Nova',imageUrl:'https://example.invalid/reference',mode:'live',sampleSeconds:null,generatedLimitSeconds:285,deadline:421});c.message({type:'configured',prompt_version:1});
  clock=60999;c.frame(1);assert.equal(c.firstFrameDeadline,null);
  clock=61000;c.checkStall();assert.equal(c.state.phase,'live');
  clock=68998;c.frame(2);c.checkStall();assert.equal(c.state.phase,'live');
  clock=76998;c.checkStall();assert.equal(c.state.phase,'stopped');assert.match(c.state.ack,/stalled for eight seconds/);
});
