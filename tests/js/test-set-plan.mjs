import test from 'node:test';
import assert from 'node:assert/strict';
import {cleanTracks,readCrate,energyTargets,buildDraft,draftRows,editDraft,parseDraft} from '../../src/afterhours/web/set-plan.js';
const tracks=[
  {id:'a',title:'Opening',bpm:120,camelot:'8A',energy:.3,duration:180},
  {id:'b',title:'Lift',bpm:121,camelot:'9A',energy:.5,duration:180},
  {id:'c',title:'Peak',bpm:122,camelot:'9B',energy:.8,duration:180},
  {id:'d',title:'Settle',bpm:120,camelot:'8B',energy:.6,duration:180}
];
const settings={curve:'gradual',target:.8,length:3};
const crate={tracks,current:tracks[0]};

test('plans unique tracks after current deck without mutating source',()=>{
  const before=JSON.stringify(crate),draft=buildDraft(crate,settings,1000);
  assert.equal(draft.tracks.length,3);assert.equal(new Set(draft.tracks.map(t=>t.id)).size,3);
  assert.ok(draft.tracks.every(t=>t.id!=='a'));assert.equal(JSON.stringify(crate),before);
});
test('gradual target rises and finishes at the selected energy',()=>{
  const t=energyTargets(.3,.8,4,'gradual');assert.ok(t.every((v,i)=>i===0||v>t[i-1]));assert.equal(t.at(-1),.8);
  assert.deepEqual(energyTargets(.3,.6,3,'steady'),[.6,.6,.6]);
});
test('rise and settle peaks above its final target, including single-track edge',()=>{
  const t=energyTargets(.4,.6,5,'rise-settle');assert.ok(Math.max(...t)>.6);assert.equal(t.at(-1),.6);
  assert.deepEqual(energyTargets(.4,.6,1,'rise-settle'),[.6]);
});
test('scoring chooses a musical energy build for a compatible crate',()=>{
  const draft=buildDraft({tracks:tracks.slice(0,3),current:tracks[0]},{...settings,length:2});
  assert.deepEqual(draft.tracks.map(t=>t.id),['b','c']);
});
test('same inputs always produce the same order, regardless of source order',()=>{
  assert.deepEqual(buildDraft(crate,settings).tracks,buildDraft({...crate,tracks:[...tracks].reverse()},settings).tracks);
});
test('limited crates yield fewer tracks, never duplicates or fabricated tracks',()=>{
  const draft=buildDraft(crate,{...settings,length:12});assert.equal(draft.requestedLength,12);assert.equal(draft.tracks.length,3);
  assert.throws(()=>buildDraft({tracks:[tracks[0]],current:tracks[0]},settings),/Add another/);
});
test('wide tempo changes are visibly flagged, matching browser engine limits',()=>{
  const draft=buildDraft({tracks:[tracks[0],{...tracks[1],bpm:160}],current:tracks[0]},{...settings,length:1});
  assert.match(draftRows(draft)[0].warning,/Wide tempo gap/);
});
test('unknown or malformed analysis stays unknown instead of becoming fabricated metadata',()=>{
  const clean=cleanTracks([{id:'u',bpm:'128',energy:Infinity,camelot:'0A',duration:-1,title:''}])[0];
  assert.equal(clean.bpm,null);assert.equal(clean.energy,null);assert.equal(clean.camelot,null);assert.equal(clean.duration,null);
  const row=draftRows(buildDraft({tracks:[clean],current:null},settings))[0];
  assert.match(row.reason,/Tempo comparison unavailable/);assert.match(row.reason,/energy unavailable/);
  assert.equal(row.warning,null);
});
test('only crate metadata is retained: paths, tokens and unrelated server fields are discarded',()=>{
  const c=readCrate({app:'Afterhours',backend:'Web Audio',library:tracks.map(t=>({...t,url:'/private/path',token:'never-keep'})),current:'a',connected:true,updated:10},10000);
  assert.equal(c.live,true);assert.doesNotMatch(JSON.stringify(c),/private|never-keep|token/);
  assert.equal(readCrate({app:'Afterhours',backend:'Web Audio',library:tracks,connected:true,updated:1},20000).live,false);
});
test('read refuses missing audio sessions and malformed or oversized libraries',()=>{
  assert.throws(()=>readCrate({connected:false}),/Open the local audio console/);
  assert.throws(()=>cleanTracks([tracks[0],tracks[0]]),/duplicate/);
  assert.throws(()=>cleanTracks(Array.from({length:15},(_,i)=>({id:String(i)}))),/14/);
});
test('reorder and removal preserve identity and recompute neighboring explanations',()=>{
  const original=buildDraft(crate,settings),second=original.tracks[1].id;
  const moved=editDraft(original,1,'earlier');assert.equal(moved.tracks[0].id,second);
  assert.notEqual(draftRows(original)[0].reason,draftRows(moved)[0].reason);
  assert.equal(original.tracks.length,3);assert.equal(editDraft(moved,0,'remove').tracks.length,2);
  assert.equal(editDraft(original,0,'earlier'),original);
});
test('saved plans round-trip safely and edits can end in an empty draft',()=>{
  let draft=buildDraft(crate,settings,1000);
  assert.deepEqual(parseDraft(JSON.stringify(draft)),draft);
  while(draft.tracks.length)draft=editDraft(draft,0,'remove',2000);
  assert.equal(parseDraft(JSON.stringify(draft)).tracks.length,0);
  assert.equal(parseDraft(null),null);
});
test('invalid saved state or settings never becomes an executable or plausible plan',()=>{
  assert.throws(()=>parseDraft('{'));assert.throws(()=>parseDraft('x'.repeat(64001)),/large/);
  assert.throws(()=>parseDraft(JSON.stringify({version:99})),/format/);
  for(const change of [{curve:'secret'},{target:NaN},{target:2},{length:0},{length:100}])assert.throws(()=>buildDraft(crate,{...settings,...change}));
});
test('maximum-size crate stays bounded and produces exactly 12 unique choices',()=>{
  const full=Array.from({length:14},(_,i)=>({...tracks[i%4],id:`track-${i}`}));
  const draft=buildDraft({tracks:full,current:full[0]},{...settings,length:12});
  assert.equal(draft.tracks.length,12);assert.equal(new Set(draft.tracks.map(t=>t.id)).size,12);
});

test('low-confidence analysis cannot steer harmonic or energy scoring',()=>{
  const unknown={id:'u',title:'Uncertain',bpm:120,camelot:'2A',energy:.95,
    metadata:{source:'local.analysis',confidence:{bpm:.1,camelot:.2,energy:.1}}};
  const nulls={id:'u',title:'Uncertain',bpm:null,camelot:null,energy:null};
  const settings={curve:'steady',target:.5,length:2};
  const uncertain=buildDraft({tracks:[tracks[0],tracks[1],unknown],current:tracks[0]},settings);
  const missing=buildDraft({tracks:[tracks[0],tracks[1],nulls],current:tracks[0]},settings);
  assert.deepEqual(uncertain.tracks.map(t=>t.id),missing.tracks.map(t=>t.id));
  const row=draftRows(uncertain).find(row=>row.track.id==='u');
  assert.match(row.reason,/Tempo comparison unavailable/);
  assert.match(row.reason,/energy unavailable/);
  assert.match(row.reason,/Low-confidence values were excluded/);
});

test('planning retains bounded metadata provenance through save and restore',()=>{
  const source={...tracks[1],metadata:{source:'virtualdj.browser',observedAt:'2026-10-25T12:00:00Z',
    genre:'House',remix:'Extended Mix',label:'Fixture label',confidence:{bpm:null},token:'never-keep',url:'/private/path'}};
  const draft=buildDraft({tracks:[tracks[0],source],current:tracks[0]},{...settings,length:1},1000);
  const restored=parseDraft(JSON.stringify(draft));
  assert.deepEqual(restored,draft);
  assert.equal(restored.tracks[0].metadata.remix,'Extended Mix');
  assert.equal(restored.tracks[0].metadata.confidence.bpm,null);
  assert.doesNotMatch(JSON.stringify(restored),/never-keep|private|token/);
  assert.match(draftRows(restored)[0].reason,/Values reported by VirtualDJ/);
});
test('constant-tempo session planner constrains every track to the opening tempo',()=>{
 const pool=[150,155,156,160,163,165].map((bpm,i)=>({id:String(i),title:`Song ${i}`,bpm,camelot:'8A'}));
 const draft=buildDraft({tracks:pool,current:null},{curve:'steady',target:.6,length:5,constantTempo:true},1000,{maxTempoShift:8});
 assert.equal(draft.tracks.length,5);assert.ok(draft.tracks.every(t=>Math.abs(draft.tracks[0].bpm/t.bpm-1)<=.08));
});
