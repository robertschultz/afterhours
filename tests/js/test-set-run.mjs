import test from 'node:test';
import assert from 'node:assert/strict';
import {draftOrder,validateRun,createRun,nextInRun,advanceRun,finishRun,releaseRun} from '../../src/afterhours/web/set-run.js';
import {SetRunClient,localPlaybackReady} from '../../src/afterhours/web/set-run-client.js';
const tracks=[{id:'a',title:'A',bpm:120,duration:60},{id:'b',title:'B',bpm:122,duration:60},{id:'c',title:'C',bpm:124,duration:60}];
const draft={anchor:tracks[0],tracks:tracks.slice(1)},sessionId='audio-session';
const context={sessionId,tracks,decodedIds:new Set(tracks.map(t=>t.id))};
const command={sessionId,requestId:'request-1234',trackIds:['a','c','b']};
const now=100000;
const state={connected:true,app:'Afterhours',backend:'Web Audio',audioEnabled:true,capabilities:['set-run-v1'],sessionId,updated:100,library:tracks};
const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
test('a draft includes its anchor once and preserves the edited order',()=>{assert.deepEqual(draftOrder(draft),['a','b','c']);assert.deepEqual(validateRun(command,context),['a','c','b']);});
test('catalog snapshots cannot be converted into a local playback order',()=>{assert.throws(()=>draftOrder({...draft,tracks:[{...tracks[1],metadata:{source:'virtualdj.browser'}}]}),/Catalog/);});
test('missing files and a reloaded audio owner reject before playback',()=>{
 assert.throws(()=>validateRun(command,{...context,decodedIds:new Set(['a','b'])}),/missing/);
 assert.throws(()=>validateRun(command,{...context,sessionId:'replacement'}),/session changed/);
 assert.throws(()=>validateRun({...command,trackIds:['a','a']},context),/distinct/);
 assert.throws(()=>validateRun(command,{...context,tracks:[{...tracks[0],bpm:NaN},...tracks.slice(1)]}),/tempo/);
});
test('manual order survives transitions, ends without wrapping, and ignores duplicate completion',()=>{
 let run=createRun('planned',['a','c','b']);assert.equal(nextInRun(run),'c');
 run=advanceRun(run,'c');run=advanceRun(run,'c');assert.equal(run.position,1);assert.equal(nextInRun(run),'b');
 run=advanceRun(run,'b');assert.equal(nextInRun(run),null);run=finishRun(run);assert.equal(run.status,'completed');assert.equal(nextInRun(run),null);
});
test('manual takeover and unexpected deck changes prevent automatic continuation',()=>{
 const run=createRun('planned',['a','b','c']);
 assert.equal(nextInRun(releaseRun(run)),null);assert.equal(advanceRun(run,'c').status,'released');
 assert.equal(finishRun(run).status,'interrupted');assert.equal(nextInRun(finishRun(run)),null);
});
test('readiness requires a fresh enabled console with the current implementation',()=>{
 assert.ok(localPlaybackReady(state,now));
 for(const change of [{updated:94},{updated:103},{connected:false},{audioEnabled:false},{capabilities:[]},{backend:'VirtualDJ'}])assert.equal(localPlaybackReady({...state,...change},now),false);
});
test('queued is not acknowledged until the execution result arrives, even if later commands overwrite lastCommand',async()=>{
 let reads=0,sends=0,queued=0;
 const client=new SetRunClient({now:()=>now,sleep:async()=>{},fetcher:async(path,options)=>{
  if(path==='/api/config')return response({token:'test'});
  if(options.method==='POST'){sends++;return response({accepted:true,id:7},202);}
  reads++;return response({...state,...(reads>2?{lastCommand:{id:8,result:'executed'},commandResults:[{id:7,result:'executed'}]}:{})});
 }});
 await client.send('play-draft',draft,()=>queued++);assert.equal(sends,1);assert.equal(queued,1);assert.equal(reads,3);
});
test('unknown delivery blocks replay until an explicit read; no automatic retry',async()=>{
 let sends=0;
 const client=new SetRunClient({now:()=>now,sleep:async()=>{},fetcher:async(path,options)=>{
  if(path==='/api/config')return response({token:'test'});
  if(options.method==='POST'){sends++;throw new Error('Lost connection');}
  return response(state);
 }});
 await assert.rejects(client.send('play-draft',draft),/may have applied/);assert.equal(client.uncertain,true);
 await assert.rejects(client.send('play-draft',draft),/unconfirmed/);assert.equal(sends,1);
 await client.inspect();assert.equal(client.uncertain,false);
});
test('confirmed console rejection is distinct from an uncertain result',async()=>{
 const client=new SetRunClient({now:()=>now,sleep:async()=>{},fetcher:async(path,options)=>{
  if(path==='/api/config')return response({token:'test'});
  if(options.method==='POST')return response({accepted:true,id:9},202);
  return response({...state,commandResults:[{id:9,result:'failed',error:'Missing decoded track'}]});
 }});
 await assert.rejects(client.send('play-draft',draft),/Missing decoded/);assert.equal(client.uncertain,false);
});
test('stale or missing local tracks send no command',async()=>{
 for(const local of [{...state,updated:90},{...state,library:tracks.slice(1)}]){
  let sends=0;const client=new SetRunClient({now:()=>now,fetcher:async(path,options)=>{if(options.method==='POST')sends++;return response(local);}});
  await assert.rejects(client.send('play-draft',draft));assert.equal(sends,0);
 }
});
test('expired acknowledgement is uncertain and an overlapping click is rejected',async()=>{
 let release;const gate=new Promise(resolve=>release=resolve);let posts=0;
 const client=new SetRunClient({now:()=>now,sleep:async()=>{},fetcher:async(path,options)=>{
  if(path==='/api/config')return response({token:'test'});
  if(options.method==='POST'){posts++;await gate;return response({accepted:true,id:1},202);}
  return response(state);
 }});
 const pending=client.send('play-draft',draft);await assert.rejects(client.send('play-draft',draft),/Wait/);release();
 await assert.rejects(pending,/may have applied/);assert.equal(posts,1);
});
