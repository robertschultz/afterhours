// The bridge owns network state only. Audio buffers and transport stay in the tab.
export class AudioBridge {
  constructor({execute,snapshot,onCommand=()=>{},onStatus=()=>{},onSession=()=>{},
    fetcher=(...args)=>fetch(...args),client=crypto.randomUUID(),timeoutMs=2000,now=Date.now}) {
    Object.assign(this,{execute,snapshot,onCommand,onStatus,onSession,fetcher,client,timeoutMs,now});
    this.token=null;this.cursor=0;this.needsClaim=true;this.failures=0;
    this.phase='connecting';this.inflight=null;this.running=false;this.timer=null;
  }
  status(phase,message) {
    if(this.phase===phase)return;
    this.phase=phase;this.onStatus({phase,message});
  }
  async request(path,data) {
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),this.timeoutMs);
    try {
      const response=await this.fetcher(path,{method:data?'POST':'GET',cache:'no-store',signal:controller.signal,
        headers:{'Content-Type':'application/json','X-DJ-Token':this.token||''},
        ...(data?{body:JSON.stringify(data)}:{})});
      const json=await response.json();
      if(!response.ok){const error=new Error(json.error||'Local bridge unavailable');error.status=response.status;throw error;}
      return json;
    } finally {clearTimeout(timer);}
  }
  async connect() {
    const config=await this.request('/api/config');
    if(typeof config.token!=='string'||!config.token)throw new Error('Invalid bridge session');
    if(config.token!==this.token){this.onSession();this.token=config.token;}
    const claim=await this.request('/api/claim',{client:this.client});
    if(!Number.isSafeInteger(claim.cursor)||claim.cursor<0)throw new Error('Invalid bridge cursor');
    // Begin after the current queue, including after an uncertain delivery.
    // Commands submitted while disconnected must be deliberately sent again.
    this.cursor=claim.cursor;
    await this.request('/api/state',{...this.snapshot(),client:this.client});
    this.needsClaim=false;
  }
  tick() {
    if(this.inflight)return this.inflight;
    this.inflight=this.exchange().finally(()=>{this.inflight=null;});
    return this.inflight;
  }
  async exchange() {
    try {
      if(this.needsClaim)await this.connect();
      else {
        const result=await this.request(`/api/commands?after=${this.cursor}&client=${encodeURIComponent(this.client)}`);
        if(!Array.isArray(result.commands)||result.commands.length>100||result.commands.some((c,i,all)=>
          !c||!Number.isSafeInteger(c.id)||c.id<1||!Number.isFinite(c.created)||
          (i>0&&c.id<=all[i-1].id)))throw new Error('Invalid command response');
        for(const command of result.commands){
          if(command.id<=this.cursor)continue;
          this.cursor=command.id; // Advance before execution; never replay on a failed state post.
          if(this.now()-command.created*1000>=15000)continue;
          try {
            await this.execute(command);
            this.onCommand({id:command.id,action:command.action,result:'executed',time:new Date(this.now()).toISOString()});
          } catch(error){this.onCommand({id:command.id,action:command.action,result:'failed',error:error.message});}
        }
        await this.request('/api/state',{...this.snapshot(),client:this.client});
      }
      this.failures=0;
      this.status('ready','Ready · The connected client can inspect and direct this session');
    } catch(error){
      this.needsClaim=true;this.failures++;
      if(error.status===409)this.status('occupied','Another tab owns session control · close it to connect here. Local controls remain available.');
      else this.status('reconnecting','Reconnecting · local playback continues. Keep this tab open.');
    }
  }
  start() {
    if(this.running)return;
    this.running=true;
    const loop=async()=>{
      await this.tick();
      if(this.running)this.timer=setTimeout(loop,this.failures?Math.min(5000,1000*2**Math.min(this.failures-1,3)):700);
    };
    void loop();
  }
  stop(){this.running=false;clearTimeout(this.timer);}
}
