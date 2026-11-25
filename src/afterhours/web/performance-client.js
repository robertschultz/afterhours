export const timelineAt=(state,now=Date.now())=>Math.max(0,state.timeline+(state.mode==='playing'?Math.max(0,now/1000-state.serverTime):0));
export function selectSegment(state, elapsed) {
  const segments=state.segments||[];
  let segment=segments[0];
  for(const item of segments) {if(item.start<=elapsed)segment=item;else break;}
  if(!segment)return null;
  const since=Math.max(0,elapsed-segment.start);
  return {segment,time:segment.inPoint+since%segment.duration,fallback:since>=segment.duration};
}
export function performanceApi() {
  let token=null;
  async function request(path,body) {
    if(body && !token){const r=await fetch('/api/performance/config');if(!r.ok)throw Error('Performance server unavailable');token=(await r.json()).token;}
    const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Performance-Token':token}:{},body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(4000)});
    const data=await r.json();if(!r.ok){if(r.status===403)token=null;throw Error(data.error||'Performance server unavailable');}return data;
  }
  return {state:()=>request('/api/performance'),audio:()=>request('/api/state'),command:data=>request('/api/performance/command',data),heartbeat:data=>request('/api/performance/display',data)};
}
