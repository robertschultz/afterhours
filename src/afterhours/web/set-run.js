// A fixed local order. The audio engine alone owns scheduling and beat timing.
export function draftOrder(draft) {
  const tracks=[...(draft?.anchor?[draft.anchor]:[]),...(draft?.tracks||[])];
  if(!draft?.tracks?.length)throw new Error('Build a draft with at least one track first.');
  if(tracks.some(t=>t.metadata?.source==='virtualdj.browser'))throw new Error('Catalog drafts need VirtualDJ playback. Choose Read local crate and build a local draft.');
  const ids=tracks.map(t=>t.id);
  if(ids.length>13||new Set(ids).size!==ids.length||ids.some(id=>typeof id!=='string'||!id||id.length>160))throw new Error('This draft has invalid or repeated tracks. Rebuild it from the local crate.');
  return ids;
}

export function validateRun(command,{sessionId,tracks,decodedIds}) {
  if(command.sessionId!==sessionId)throw new Error('The audio session changed. Check local playback and try again.');
  if(typeof command.requestId!=='string'||command.requestId.length<8||command.requestId.length>80)throw new Error('Invalid set request.');
  const ids=command.trackIds;
  if(!Array.isArray(ids)||!ids.length||ids.length>13||new Set(ids).size!==ids.length||ids.some(id=>typeof id!=='string'||!id||id.length>160))throw new Error('Choose 1–13 distinct local tracks.');
  for(const id of ids){
    const track=tracks.find(t=>t.id===id);
    if(!track||!decodedIds.has(id))throw new Error('A planned track is missing from the enabled audio console. Add it there, then rebuild the draft.');
    if(!Number.isFinite(track.bpm)||track.bpm<=0||!Number.isFinite(track.duration)||track.duration<=4)throw new Error('A planned track needs valid tempo and duration before playback.');
  }
  return [...ids];
}

export function createRun(id,order,now=Date.now()) {
  return {id,order:[...order],position:0,status:'running',startedAt:new Date(now).toISOString()};
}
export function nextInRun(run) {return run?.status==='running'?run.order[run.position+1]||null:null;}
export function advanceRun(run,currentId) {
  if(run?.status!=='running'||run.order[run.position]===currentId)return run;
  if(nextInRun(run)===currentId)return {...run,position:run.position+1};
  return {...run,status:'released',reason:'The active deck changed outside this order.'};
}
export function finishRun(run) {
  return run?.status==='running'?{...run,status:run.position===run.order.length-1?'completed':'interrupted'}:run;
}
export function releaseRun(run,reason='Manual takeover') {
  return run?.status==='running'?{...run,status:'released',reason}:run;
}
