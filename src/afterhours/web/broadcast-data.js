// Pure projections of observed session data. No simulated room or audio values.
export const formatTime=value=>{const seconds=Math.max(0,Math.floor(Number.isFinite(value)?value:0));return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;};
export function energyAt(map,position){
  if(!map||map.coverage!==1||!Number.isFinite(position))return null;
  const row=map.contour?.find(r=>position>=r[0]&&position<r[0]+r[1]);
  return row&&Number.isFinite(row[2])?row[2]:null;
}
export function presentProgram(program,now=Date.now()){
  const age=program?.clock?.sampledAtUnixMs==null?Infinity:(now-program.clock.sampledAtUnixMs)/1000;
  const fresh=Boolean(program?.connected&&age>=-2&&age<3),playing=Boolean(fresh&&program.playing);
  const track=program?.library?.find(t=>t.id===program.current)||null;
  const position=Number.isFinite(program?.positionSeconds)?Math.max(0,program.positionSeconds+(playing?Math.max(0,age):0)):null;
  const duration=Number.isFinite(program?.durationSeconds)&&program.durationSeconds>0?program.durationSeconds:null;
  const remaining=Number.isFinite(program?.transitionInSeconds)?Math.max(0,program.transitionInSeconds-(playing?Math.max(0,age):0)):null;
  return {fresh,playing,track,position,duration,remaining,progress:duration&&position!==null?Math.min(1,position/duration):0,
    bpm:fresh&&Number.isFinite(program?.clock?.bpm)?program.clock.bpm:null,key:program?.key||track?.camelot||'—',
    energyDb:energyAt(program?.energyMap,position),next:program?.nextTrack||null,
    elapsed:program?.setElapsedSeconds||0,status:!fresh?'OFFLINE':playing?'LIVE':'READY'};
}
export function contourBars(map){
  if(map?.coverage!==1||!Array.isArray(map.contour)||!map.contour.length)return [];
  const valid=map.contour.filter(r=>r.length>=3&&r.slice(0,3).every(Number.isFinite)&&r[1]>0);
  const peak=Math.max(-80,...valid.map(r=>r[2]));
  return valid.map(r=>({seconds:r[0],duration:r[1],amplitude:Math.min(1,10**((r[2]-peak)/20))}));
}
