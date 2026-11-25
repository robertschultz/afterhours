// Cross-language fixture: Python adapter output -> actual JavaScript planner.
// This test does not contact VirtualDJ, Beatport or any audio output.
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {readCatalogCrate,planCatalogSet,collectCatalogTrack,catalogSnapshotFresh} from '../../src/afterhours/web/catalog-intelligence.js';
import {parseDraft} from '../../src/afterhours/web/set-plan.js';

const python=process.env.DJ_PYTHON||'python3';
const now=Date.parse('2026-10-25T12:00:00Z');
const records=JSON.parse(execFileSync(python,['-c',`
import json
from afterhours.track_metadata import vdj_track
rows = [
  ('a', 'Fixture opening', 120, 'A minor', 'Original Mix'),
  ('b', 'Fixture lift', 122, 'E minor', 'Extended Mix'),
  ('c', 'Fixture lift', 126, 'B minor', 'Radio Edit'),
  ('d', 'Fixture unknown analysis', None, '', '')
]
print(json.dumps([vdj_track({'id': char*64, 'folder': {'id':'f'*64},
  'title': title, 'artist': 'Synthetic artist', 'bpm': bpm, 'key': key,
  'remix': remix, 'genre': 'House', 'label': 'Fixture label',
  'duration_ms': 372125 if char != 'd' else None},
  observed_at='2026-10-25T11:59:00Z') for char,title,bpm,key,remix in rows]))
`],{cwd:fileURLToPath(new URL('.',import.meta.url)),encoding:'utf8'}));
const settings={curve:'gradual',target:.7,length:3};
const options={currentId:records[0].id,now};

test('actual Python normalized metadata feeds the actual set planner',()=>{
  const plan=planCatalogSet(records,settings,options);
  assert.equal(plan.draft.tracks.length,3);
  assert.equal(plan.draft.anchor.camelot,'8A');
  assert.equal(plan.draft.tracks.find(t=>t.id===records[1].id).camelot,'9A');
  assert.equal(plan.draft.tracks.find(t=>t.id===records[1].id).duration,372.125);
  assert.equal(plan.playbackVerified,false);
  assert.equal(plan.completeCatalog,false);
  assert.match(plan.purpose,/no queue applied/);
});
test('same title with distinct recording IDs preserves both versions',()=>{
  const crate=readCatalogCrate(records,{now});
  const variants=crate.tracks.filter(t=>t.title==='Fixture lift');
  assert.equal(variants.length,2);
  assert.notEqual(variants[0].id,variants[1].id);
  assert.deepEqual(variants.map(t=>t.metadata.remix),['Extended Mix','Radio Edit']);
});
test('metadata does not imply energy, live connectivity or complete catalog access',()=>{
  const crate=readCatalogCrate(records,{now});
  assert.equal(crate.live,false);
  assert.equal(crate.metadataOnly,true);
  assert.ok(crate.tracks.every(t=>t.energy===null));
  const plan=planCatalogSet(records,settings,options);
  assert.ok(plan.comparisons.every(row=>row.reason.includes('energy unavailable')));
  assert.ok(plan.comparisons.every(row=>row.reason.includes('provider origin and accuracy are unverified')));
});
test('saved catalog draft retains identities, provenance and unknowns',()=>{
  const {draft}=planCatalogSet(records,settings,options);
  assert.deepEqual(parseDraft(JSON.stringify(draft)),draft);
  assert.equal(draft.tracks[0].metadata.source,'virtualdj.browser');
  assert.equal(draft.tracks[0].metadata.confidence.bpm,null);
});
test('duplicate, incompatible, future and mismatched identity snapshots are rejected',()=>{
  assert.throws(()=>readCatalogCrate([records[0],records[0]],{now}),/twice/);
  for(const change of [{schema:'wrong'},{engine:{name:'other'}},{id:'wrong'},
    {observed_at:'invalid'},{observed_at:'2026-10-26T12:00:00Z'}]){
    assert.throws(()=>readCatalogCrate([{...records[0],...change}],{now}),/snapshot/);
  }
  assert.throws(()=>readCatalogCrate(records,{now,currentId:'missing'}),/starting track/);
  assert.throws(()=>readCatalogCrate(Array(15).fill(records[0]),{now}),/14/);
});
test('private extra fields are removed and input records are never changed',()=>{
  const source=structuredClone(records);
  source[0].token='SECRET_FIXTURE';source[0].filepath='/private/fixture';
  source[0].provenance.bpm.token='SECRET_FIXTURE';
  const before=JSON.stringify(source),plan=planCatalogSet(source,settings,options);
  assert.equal(JSON.stringify(source),before);
  assert.doesNotMatch(JSON.stringify(plan),/SECRET_FIXTURE|private|filepath|token/);
});
test('collection requires a recent snapshot; deck polling cannot renew it',()=>{
  assert.equal(catalogSnapshotFresh(records[0],now),false);
  assert.throws(()=>collectCatalogTrack(null,records[0],{now}),/Read selection/);
  const fresh={...records[0],observed_at:new Date(now-14999).toISOString()};
  assert.equal(collectCatalogTrack(null,fresh,{now}).tracks.length,1);
  assert.equal(catalogSnapshotFresh(fresh,now+1),false);
  assert.equal(catalogSnapshotFresh({...fresh,observed_at:new Date(now+2001).toISOString()},now),false);
});
test('collecting the same identity updates facts without duplicating or mutating the crate',()=>{
  const fresh={...records[0],observed_at:new Date(now).toISOString()};
  const original=collectCatalogTrack(null,fresh,{now});
  const next=collectCatalogTrack(original,{...fresh,bpm:125,token:'PRIVATE_FIXTURE'},{now});
  assert.equal(next.tracks.length,1);assert.equal(next.tracks[0].bpm,125);
  assert.equal(original.tracks[0].bpm,120);
  assert.doesNotMatch(JSON.stringify(next),/PRIVATE_FIXTURE|token/);
});
test('full collection rejects another track without losing the existing fourteen',()=>{
  let crate=null;
  const fresh={...records[0],observed_at:new Date(now).toISOString()};
  for(let i=0;i<14;i++){
    const id=i.toString(16).repeat(64);
    crate=collectCatalogTrack(crate,{...fresh,id:`virtualdj:${id}`,engine:{...fresh.engine,selection_id:id}},{now});
  }
  assert.equal(crate.tracks.length,14);
  const id='e'.repeat(64);
  assert.throws(()=>collectCatalogTrack(crate,{...fresh,id:`virtualdj:${id}`,engine:{...fresh.engine,selection_id:id}},{now}),/holds 14/);
  assert.equal(crate.tracks.length,14);
});
