import {draftOrder} from './set-run.js';
export function localPlaybackReady(state,now=Date.now()) {
  return state?.connected===true&&state.app==='Afterhours'&&state.backend==='Web Audio'&&
    state.audioEnabled===true&&state.capabilities?.includes('set-run-v1')&&
    typeof state.sessionId==='string'&&Number.isFinite(state.updated)&&now-state.updated*1000<5000&&now-state.updated*1000>=-2000;
}
export class SetRunClient {
  constructor({fetcher=(...args)=>fetch(...args),now=Date.now,sleep=ms=>new Promise(r=>setTimeout(r,ms))}={}) {
    Object.assign(this,{fetcher,now,sleep});this.busy=false;this.uncertain=false;
  }
  async request(path,data,token) {
    const response=await this.fetcher(path,{method:data?'POST':'GET',cache:'no-store',signal:AbortSignal.timeout(3000),
      headers:{'Content-Type':'application/json',...(token?{'X-DJ-Token':token}:{})},...(data?{body:JSON.stringify(data)}:{})});
    const json=await response.json();
    if(!response.ok){const e=new Error(json.error||'Local playback is unavailable.');e.confirmed=true;throw e;}
    return json;
  }
  async inspect(){const state=await this.request('/api/state');this.uncertain=false;return state;}
  async send(action,draft,onQueued=()=>{}) {
    if(this.busy)throw new Error('Wait for the current playback request.');
    if(this.uncertain&&action!=='takeover')throw new Error('The last result is unconfirmed. Check local playback before sending another set.');
    this.busy=true;let sent=false;
    try{
      const ids=action==='play-draft'?draftOrder(draft):null;
      const state=await this.request('/api/state');
      if(!localPlaybackReady(state,this.now()))throw new Error('Open the audio console and click Start set once to enable audio. Keep it open, then check local playback here.');
      if(ids?.some(id=>!state.library?.some(t=>t.id===id)))throw new Error('A draft track is missing from the audio console. Read local crate and rebuild the draft.');
      const {token}=await this.request('/api/config');
      const requestId=crypto.randomUUID();
      sent=true;
      const accepted=await this.request('/api/command',{action,requestId,sessionId:state.sessionId,...(ids?{trackIds:ids}:{})},token);
      if(accepted.accepted!==true||!Number.isSafeInteger(accepted.id))throw new Error('Playback acknowledgement was invalid.');
      onQueued();
      for(let i=0;i<12;i++){
        await this.sleep(400);
        const current=await this.request('/api/state');
        if(current.sessionId!==state.sessionId)throw new Error('The audio session changed before acknowledgement.');
        const result=(current.commandResults||[]).find(r=>r.id===accepted.id)|| (current.lastCommand?.id===accepted.id?current.lastCommand:null);
        if(result?.result==='failed'){const e=new Error(result.error||'The console rejected this request.');e.confirmed=true;throw e;}
        if(result?.result==='executed')return current;
      }
      throw new Error('The audio console did not acknowledge this request.');
    }catch(error){
      if(sent&&!error.confirmed){this.uncertain=true;throw new Error(`${error.message} It may have applied. Check local playback; do not resend blindly.`);}
      throw error;
    }finally{this.busy=false;}
  }
}
