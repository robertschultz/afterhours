// A metadata-only bridge into the existing bounded planner. No playback calls,
// provider requests, credentials, raw audio or fabricated energy estimates.
import {cleanTracks,buildDraft,draftRows} from './set-plan.js';
const digest=value=>typeof value==='string'&&/^[0-9a-f]{64}$/.test(value);
const confidence=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=1?value:null;

export function catalogSnapshotFresh(record,now=Date.now()) {
  const age=now-Date.parse(record?.observed_at);
  return Number.isFinite(age)&&age>=-2000&&age<15000;
}

export function collectCatalogTrack(crate,record,{now=Date.now()}={}) {
  if(!catalogSnapshotFresh(record,now))throw new Error('Read selection again before keeping this track.');
  const next=readCatalogCrate([record],{now});
  const tracks=crate?.metadataOnly===true?cleanTracks(crate.tracks):[];
  const index=tracks.findIndex(track=>track.id===next.tracks[0].id);
  if(index<0)tracks.push(next.tracks[0]);else tracks[index]=next.tracks[0];
  if(tracks.length>14)throw new Error('The planning crate holds 14 tracks. Remove one before keeping another.');
  return {...next,tracks};
}

export function readCatalogCrate(records,{currentId=null,now=Date.now()}={}) {
  if(!Array.isArray(records)||records.length>14)throw new Error('Choose up to 14 catalog track snapshots for this planning crate.');
  const seen=new Set();
  const tracks=records.map(record=>{
    if(record?.schema!=='afterhours.track.v1'||record.engine?.name!=='virtualdj'
      ||!digest(record.engine.selection_id)||!digest(record.engine.folder_id)
      ||record.id!==`virtualdj:${record.engine.selection_id}`
      ||typeof record.observed_at!=='string'||!Number.isFinite(Date.parse(record.observed_at))
      ||Date.parse(record.observed_at)>now+2000)throw new Error('Invalid or future-dated catalog snapshot. Read the track again.');
    if(seen.has(record.id))throw new Error('The same track appears twice. Keep one snapshot per track.');
    seen.add(record.id);
    const provenance=record.provenance||{};
    return {...record,metadata:{source:'virtualdj.browser',observedAt:record.observed_at,
      genre:record.genre,album:record.album,remix:record.remix,label:record.label,year:record.year,
      confidence:{bpm:confidence(provenance.bpm?.confidence),camelot:confidence(provenance.camelot?.confidence),energy:confidence(provenance.energy?.confidence)}}};
  });
  if(currentId!==null&&!seen.has(currentId))throw new Error('The starting track is missing from this catalog crate.');
  const cleaned=cleanTracks(tracks);
  return {tracks:cleaned,current:cleaned.find(t=>t.id===currentId)||null,
    source:'VirtualDJ selected-track metadata',metadataOnly:true,live:false,completeCatalog:false,
    readAt:new Date(now).toISOString()};
}

export function planCatalogSet(records,settings,options={}) {
  const now=options.now??Date.now(),crate=readCatalogCrate(records,{...options,now});
  const draft=buildDraft(crate,settings,now);
  return {purpose:'Metadata-only planning; no queue applied or playback verified',
    source:crate.source,completeCatalog:false,playbackVerified:false,draft,comparisons:draftRows(draft)};
}
