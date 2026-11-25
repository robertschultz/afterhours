// Offline, bounded musical scoring. A draft never sends an engine command.
import {harmonicDistance} from './planner.js';
import {cleanPreferences,fitsPreferences,genrePenalty} from './music-preferences.js';
export const DRAFT_KEY='afterhours.engine.set-draft.v1';
export const CURVES=['gradual','steady','rise-settle'];
// Catalog search may return entire DJ mixes as if they were songs. They are
// unsuitable for this bounded two-deck set workflow, even with valid BPM/key.
export function catalogSetTracks(tracks) {
  return tracks.filter(t=>(!Number.isFinite(t.duration)||t.duration<=1200)&&
    !/continuous\s+mix|(?:full|entire)\s+(?:album|dj)\s+mix/i.test(`${t.title||''} ${t.remix||''}`));
}
const text=(value,fallback='')=>typeof value==='string'?value.trim().slice(0,300):fallback;
const number=(value,low,high)=>typeof value==='number'&&Number.isFinite(value)&&value>=low&&value<=high?value:null;
const key=value=>/^(?:[1-9]|1[0-2])[AB]$/.test(value)?value:null;

function cleanMetadata(value) {
  if(!value||typeof value!=='object'||Array.isArray(value))return null;
  const confidence=value.confidence||{};
  return {source:['virtualdj.browser','local.analysis','user'].includes(value.source)?value.source:'unknown',
    observedAt:typeof value.observedAt==='string'&&Number.isFinite(Date.parse(value.observedAt))?new Date(value.observedAt).toISOString():null,
    genre:text(value.genre),album:text(value.album),remix:text(value.remix),label:text(value.label),
    year:Number.isInteger(value.year)&&value.year>=1000&&value.year<=9999?value.year:null,
    confidence:{bpm:number(confidence.bpm,0,1),camelot:number(confidence.camelot,0,1),energy:number(confidence.energy,0,1)}};
}
function usable(track,field) {
  const confidence=track?.metadata?.confidence?.[field];
  return typeof confidence==='number'&&confidence<.5?null:track?.[field]??null;
}

export function cleanTracks(input) {
  if(!Array.isArray(input)||input.length>14)throw new Error('This draft supports a local crate of up to 14 tracks.');
  const ids=new Set();
  return input.map(track=>{
    if(!track||typeof track.id!=='string'||!track.id.trim()||track.id.length>160||ids.has(track.id))throw new Error('The local crate has invalid or duplicate track IDs. Read the crate again.');
    ids.add(track.id);
    const metadata=cleanMetadata(track.metadata);
    return {id:track.id,title:text(track.title)||'Untitled track',artist:text(track.artist)||'Unknown artist',
      genre:text(track.genre),bpm:number(track.bpm,40,300),energy:number(track.energy,0,1),duration:number(track.duration,0,1200),
      camelot:key(track.camelot),estimated:track.estimated===true,...(metadata?{metadata}:{})};
  });
}
export function readCrate(value,now=Date.now()) {
  if(value?.app!=='Afterhours'||value.backend!=='Web Audio')throw new Error('Open the local audio console once, then read its crate here.');
  const tracks=cleanTracks(value.library);
  const current=tracks.find(t=>t.id===value.current)||null;
  const live=value.connected===true&&Number.isFinite(value.updated)&&now-value.updated*1000<10000&&now-value.updated*1000>=-2000;
  return {tracks,current,live,readAt:new Date(now).toISOString()};
}
function options(value) {
  if(!value||!CURVES.includes(value.curve)||number(value.target,.2,.95)===null||!Number.isInteger(value.length)||value.length<1||value.length>12)throw new Error('Choose 1–12 tracks and an energy target from 20–95%.');
  return {curve:value.curve,target:value.target,length:value.length};
}
export function energyTargets(start,target,length,curve) {
  const from=Number.isFinite(start)?start:target;
  return Array.from({length},(_,i)=>{
    const progress=(i+1)/length;
    if(curve==='steady')return target;
    if(curve==='rise-settle'&&length>1){
      const peak=Math.min(.95,Math.max(from,target)+.12);
      return progress<=.6?from+(peak-from)*progress/.6:peak+(target-peak)*(progress-.6)/.4;
    }
    return from+(target-from)*progress;
  });
}
function cost(track,previous,target) {
  const bpm=usable(track,'bpm'),previousBpm=usable(previous,'bpm');
  const camelot=usable(track,'camelot'),previousKey=usable(previous,'camelot'),energy=usable(track,'energy');
  const tempo=previousBpm!==null&&bpm!==null?Math.abs(bpm-previousBpm)/previousBpm*170:8;
  const harmony=previousKey&&camelot?harmonicDistance(previousKey,camelot)*7:7;
  return tempo+harmony+(energy===null?22:Math.abs(energy-target)*55);
}
export function buildDraft(crate,rawOptions,now=Date.now(),preferences) {
  const settings=options(rawOptions),tracks=cleanTracks(crate.tracks);
  const prefs=preferences?cleanPreferences(preferences):null;
  const anchor=tracks.find(t=>t.id===crate.current?.id)||null;
  const pool=tracks.filter(t=>t.id!==anchor?.id&&(!prefs||fitsPreferences(t,prefs)));
  if(!pool.length)throw new Error(prefs?'No candidate tracks fit. Read a larger crate or widen your musical preferences.':'Add another track to the local audio crate, then read it again.');
  if(prefs)settings.target=Math.min(settings.target,prefs.energyCeiling);
  const length=Math.min(settings.length,pool.length),targets=energyTargets(usable(anchor,'energy'),settings.target,length,settings.curve);
  let beam=[{tracks:[],score:0,tie:''}];
  for(let i=0;i<length;i++){
    const next=[];
    for(const path of beam)for(const track of pool){
      if(path.tracks.some(t=>t.id===track.id))continue;
      const tempoAnchor=anchor||path.tracks[0];
      if(rawOptions.constantTempo&&tempoAnchor&&(!track.bpm||!tempoAnchor.bpm||Math.abs(tempoAnchor.bpm/track.bpm-1)>(prefs?.maxTempoShift??8)/100))continue;
      next.push({tracks:[...path.tracks,track],score:path.score+cost(track,path.tracks.at(-1)||anchor,targets[i])+(prefs?genrePenalty(track,prefs):0),tie:path.tie+'\0'+track.id});
    }
    beam=next.sort((a,b)=>a.score-b.score||a.tie.localeCompare(b.tie)).slice(0,32);
    if(!beam.length)throw new Error('There are not enough compatible tracks within the tempo limit. Reduce the track count or read a closer tempo range.');
  }
  return {version:1,createdAt:new Date(now).toISOString(),updatedAt:new Date(now).toISOString(),
    anchor,tracks:beam[0].tracks,settings:{...settings,length},requestedLength:settings.length,...(prefs?{preferences:prefs}:{}),excludedCount:tracks.length-(anchor?1:0)-pool.length};
}
export function draftRows(draft) {
  const targets=energyTargets(usable(draft.anchor,'energy'),draft.settings.target,draft.tracks.length,draft.settings.curve);
  return draft.tracks.map((track,i)=>{
    const previous=i?draft.tracks[i-1]:draft.anchor;
    const bpm=usable(track,'bpm'),previousBpm=usable(previous,'bpm');
    const camelot=usable(track,'camelot'),previousKey=usable(previous,'camelot');
    const ratio=previousBpm&&bpm?previousBpm/bpm:null;
    const tempo=ratio===null?'Tempo comparison unavailable':`${Math.abs(previousBpm-bpm).toFixed(0)} BPM apart`;
    const distance=previousKey&&camelot?harmonicDistance(previousKey,camelot):null;
    const harmony=distance===null?'key comparison unavailable':distance===0?'same musical key':distance===1?'neighboring musical key':'a key change';
    const energyValue=usable(track,'energy');
    const energy=energyValue===null?'energy unavailable':`${Math.round(energyValue*100)}% energy`;
    const uncertain=[track,previous].some(t=>t?.metadata&&Object.values(t.metadata.confidence).some(v=>typeof v==='number'&&v<.5));
    const source=track.metadata?.source==='virtualdj.browser'?' Values reported by VirtualDJ; provider origin and accuracy are unverified.':'';
    return {track,target:targets[i],reason:`${tempo}, ${harmony}, ${energy}.${energyValue===null?' Energy target not scored.':` Target ${Math.round(targets[i]*100)}%.`}${source}${uncertain?' Low-confidence values were excluded from scoring.':''}`,
      warning:ratio!==null&&(ratio<.92||ratio>1.08)?'Wide tempo gap. Review the transition before playing.':null};
  });
}
export function editDraft(draft,index,action,now=Date.now()) {
  if(!Number.isInteger(index)||index<0||index>=draft.tracks.length)throw new Error('Track is no longer in this draft.');
  const tracks=[...draft.tracks];
  if(action==='remove')tracks.splice(index,1);
  else {
    const destination=index+(action==='earlier'?-1:action==='later'?1:0);
    if(destination===index||destination<0||destination>=tracks.length)return draft;
    [tracks[index],tracks[destination]]=[tracks[destination],tracks[index]];
  }
  return {...draft,tracks,updatedAt:new Date(now).toISOString(),settings:{...draft.settings,length:Math.max(1,tracks.length)}};
}
export function parseDraft(raw) {
  if(!raw)return null;
  if(raw.length>64000)throw new Error('Saved draft is too large.');
  const value=JSON.parse(raw);
  if(value?.version!==1||!Number.isFinite(Date.parse(value.createdAt))||!Number.isFinite(Date.parse(value.updatedAt)))throw new Error('Saved draft format is not supported.');
  const tracks=cleanTracks(value.tracks),anchor=value.anchor?cleanTracks([value.anchor])[0]:null;
  if(tracks.length>12)throw new Error('Saved drafts support up to 12 tracks.');
  if(anchor&&tracks.some(t=>t.id===anchor.id))throw new Error('The opening track is duplicated in the saved draft.');
  const settings=options(value.settings);
  return {version:1,createdAt:value.createdAt,updatedAt:value.updatedAt,tracks,anchor,
    settings:{...settings,length:Math.max(1,tracks.length)},requestedLength:Math.max(1,tracks.length),excludedCount:Number.isInteger(value.excludedCount)&&value.excludedCount>=0&&value.excludedCount<=14?value.excludedCount:0,...(value.preferences?{preferences:cleanPreferences(value.preferences)}:{})};
}
