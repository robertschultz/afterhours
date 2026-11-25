// Protocol, limits and evidence are independent of Fal's transport and browser UI.
import {connectionFailure,connectionDiagnostic} from './director-diagnostics.js';
const TERMINAL=new Set(['configuration_timeout','initialization_timeout','invalid_initial_image','invalid_initial_audio','invalid_initial_script','invalid_input','balance_unavailable','content_policy','generation_timeout','generation_failed']);
const finite=x=>Number.isFinite(x)&&x>=0?x:null;
const FIRST_FRAME_TIMEOUT_MS=60000;
export class DirectorController {
  constructor({open,onChange=()=>{},onMedia=()=>{},onEnd=()=>{},now=()=>Date.now()}){
    Object.assign(this,{open,onChange,onMedia,onEnd,now});this.session=null;this.epoch=0;
    this.state={phase:'idle',configured:false,version:0,ack:'Ready for a live test',chunk:null,deadline:null,frames:0,presentedSeconds:0,generatedSeconds:0};
    this.events=[];
  }
  update(patch){Object.assign(this.state,patch);this.onChange({...this.state});}
  event(type,details={}){if(this.events.length<120)this.events.push({atMs:Math.max(0,this.now()-this.startedAt),type,...details});}
  start({prompt,imageUrl,referenceImages=[],model='minimax/h3-max/director',deadline,resolution='768p',mode='sample',sampleSeconds=30,generatedLimitSeconds=45}){
    if(this.session||['connecting','configuring','live'].includes(this.state.phase))throw Error('A Director session is already active.');
    if(!['480p','768p'].includes(resolution)||!['sample','live'].includes(mode)||(mode==='sample'&&sampleSeconds!==30)||(mode==='live'&&(sampleSeconds!==null||!Number.isFinite(generatedLimitSeconds)||generatedLimitSeconds<105||generatedLimitSeconds>1785)))throw Error('Invalid video session limits.');
    if(!Number.isFinite(deadline)||deadline*1000<=this.now())throw Error('The video session deadline has expired.');
    const named=model==='minimax/h3-max/reference-director';
    if(!['minimax/h3-max/director','minimax/h3-max/reference-director'].includes(model)||(named&&(!Array.isArray(referenceImages)||referenceImages.length<2||referenceImages.length>4||referenceImages.some(p=>typeof p.name!=='string'||!p.name||p.name.length>64||typeof p.url!=='string'||!p.url.startsWith('https://')))))throw Error('Invalid named character references.');
    this.model=model;
    const epoch=++this.epoch;this.startedAt=this.now();this.events=[];this.chunks=new Set();this.sentAt=new Map([[1,this.now()]]);
    this.firstFrameAt=null;this.firstFrameDeadline=this.startedAt+FIRST_FRAME_TIMEOUT_MS;this.lastFrameAt=null;this.firstFrameCount=null;this.lastFrameCount=null;this.stalling=false;this.media=null;this.lastMediaAt=null;this.mode=mode;this.sampleSeconds=sampleSeconds;this.generatedLimitSeconds=generatedLimitSeconds;
    this.update({phase:'connecting',configured:false,version:1,ack:'Connecting to Director',promptAck:null,chunk:null,deadline,resolution,frames:0,presentedSeconds:0,generatedSeconds:0,startupMs:null,stallCount:0,maxFrameGapMs:0});
    this.event('connecting');this.timer=setTimeout(()=>this.stop('Session time limit reached'),Math.max(0,deadline*1000-this.now()));
    this.watch=setInterval(()=>this.checkStall(),500);
    const valid=()=>epoch===this.epoch;
    try{
      // Director streams both tracks. Omitting audio from the offer can make
      // the runner reject negotiation before configure reaches the data channel.
      // These are receive-only slots: no microphone or local music is sent.
      const session=this.open({receive:['video','audio'],negotiationTimeoutMs:45000,
        onState:phase=>{
          if(!valid())return;
          // The official SDK emits failed before its diagnostic and onError callbacks.
          // Defer the fallback so teardown cannot invalidate the actual failure details.
          if(phase==='failed'){clearTimeout(this.failureTimer);this.failureTimer=setTimeout(()=>{if(valid())void this.stop(connectionFailure(null));},0);}
          else if(phase==='closed')void this.stop('Connection closed');
          else if(!this.state.configured)this.update({phase:phase==='live'?'configuring':'connecting'});
        },
        onMedia:stream=>{if(valid())this.onMedia(stream);},
        onData:raw=>{if(valid())this.message(raw);},
        onDiagnostic:event=>{if(valid()){const safe=connectionDiagnostic(event);if(safe)this.event('connection_diagnostic',safe);}},
        onError:error=>{if(valid()){const reason=connectionFailure(error);this.event('connection_error',{label:reason});void this.stop(reason);}}
      });
      if(!valid()){void session.close();return;}
      this.session=session;
      session.send({type:'configure',protocol_version:1,prompt_version:1,prompt,resolution,aspect_ratio:'16:9',memory:12,...(named?{reference_images:referenceImages}:{image_url:imageUrl})});
    }catch(error){void this.stop('Could not open Director');throw error;}
  }
  message(raw){
    let event;try{event=typeof raw==='string'?JSON.parse(raw):raw;}catch{return;}
    if(!event||typeof event!=='object'||this.state.phase==='stopped')return;
    if(event.type==='configured'&&event.prompt_version===1){this.event('configured');this.update({phase:'live',configured:true,ack:'Direction accepted · waiting for generated video'});return;}
    if(event.type==='stream_exhausted'){void this.stop('Provider session ended');return;}
    if(event.type==='error'){
      const code=String(event.code||'unknown').slice(0,100);this.event('provider_error',{label:code});
      if(TERMINAL.has(code))void this.stop(`Director error: ${code}`);
      else this.update({ack:`Director reported ${code}; no input replayed`});return;
    }
    if(event.type==='deadline_missed'){this.event('provider_hold');this.update({ack:'Provider is holding the picture while generation catches up'});return;}
    if(!this.state.configured)return;
    const v=event.prompt_version;
    if(['prompt_pending','prompt_applied','prompt_rejected'].includes(event.type)&&Number.isInteger(v)&&this.sentAt.has(v)){
      this.event(event.type,{version:v,latencyMs:this.now()-this.sentAt.get(v)});
      this.update({promptAck:{version:v,status:event.type}});
      if(v===this.state.version){const label={prompt_pending:'Direction queued for the next segment',prompt_applied:'Direction accepted for generation; watch for the change in a future segment',prompt_rejected:'Direction was not accepted; the current performance continues'}[event.type];this.update({ack:label});}
    }
    if(event.type==='chunk'&&Number.isInteger(event.chunk_index)&&event.chunk_index>=0&&Number.isInteger(v)&&v>0&&v<=this.state.version&&!this.chunks.has(event.chunk_index)){
      this.chunks.add(event.chunk_index);
      // Generated frames (including any context) conservatively count toward the guard.
      const seconds=Math.max(finite(event.playback_seconds)||0,(finite(event.generated_frame_count)||0)/24);
      const generatedSeconds=this.state.generatedSeconds+seconds;
      this.event('chunk',{version:v,generationSeconds:finite(event.generation_seconds),bufferSeconds:finite(event.buffer_depth_seconds),latencyMs:this.sentAt.has(v)?this.now()-this.sentAt.get(v):null});
      this.update({generatedSeconds,chunk:{index:event.chunk_index,version:v,generation:finite(event.generation_seconds),buffer:finite(event.buffer_depth_seconds)},ack:'New video generated; playback follows the provider buffer'});
      // Reserve one maximum 15-second in-flight chunk inside the selected budget.
      if(generatedSeconds>=this.generatedLimitSeconds)void this.stop('Generated footage limit reached');
    }
  }
  frame(count){
    if(!this.session||!Number.isFinite(count)||count<0||count===this.lastFrameCount)return;
    const now=this.now();
    if(this.firstFrameAt===null){this.firstFrameAt=now;this.firstFrameDeadline=null;this.firstFrameCount=count;this.event('first_frame');this.update({startupMs:now-this.startedAt});}
    if(count<this.lastFrameCount)return;
    const gap=this.lastFrameAt===null?0:now-this.lastFrameAt;
    this.lastFrameAt=now;this.lastFrameCount=count;this.stalling=false;
    const frames=count-this.firstFrameCount+1;
    this.update({frames,presentedSeconds:frames/24,maxFrameGapMs:Math.max(this.state.maxFrameGapMs,gap)});
    if(this.mode==='sample'&&frames>=this.sampleSeconds*24)void this.stop('30-second sample complete');
  }
  mediaHealth(health){
    if(!this.session||!health)return;
    // Only numeric counters and playback flags enter a receipt. No stream IDs,
    // network addresses, URLs, media content or credentials are retained.
    const safe={source:health.source==='video-frame-callback'?'video-frame-callback':'playback-quality'};
    for(const key of ['presentedFrames','totalVideoFrames','droppedVideoFrames','readyState'])safe[key]=finite(health[key]);
    for(const key of ['paused','ended','documentHidden','trackMuted','trackEnded'])safe[key]=health[key]===true;
    if(this.media&&safe.totalVideoFrames!==null&&this.media.totalVideoFrames!==null&&safe.totalVideoFrames>this.media.totalVideoFrames)this.lastMediaAt=this.now();
    this.media=safe;
  }
  checkStall(){
    if(!this.session)return;
    // Bound startup even when negotiation/configuration succeeds without a
    // decoded picture. Provider progress cannot extend this deadline.
    if(this.lastFrameAt===null){
      if(this.firstFrameDeadline!==null&&this.now()>=this.firstFrameDeadline)void this.stop('No video received within 60 seconds; no automatic retry');
      return;
    }
    const gap=this.now()-this.lastFrameAt;
    // A hidden preview may drop all frames while a separate display receives
    // the same track. Growing received-frame counters keep live transport alive,
    // but never inflate presentedSeconds or complete a recorded sample.
    const receiving=this.mode==='live'&&this.media?.documentHidden&&this.lastMediaAt!==null&&this.now()-this.lastMediaAt<2500&&!this.media.paused&&!this.media.trackEnded;
    if(gap>=2500&&!receiving&&!this.stalling){this.stalling=true;this.event('frame_stall',{media:this.media});this.update({stallCount:this.state.stallCount+1,ack:'Waiting for new video frames'});}
    if(gap>=8000&&!receiving){this.event('playback_diagnostic',{media:this.media});void this.stop('Video stalled for eight seconds');}
  }
  direct(prompt,{source='operator',label='Live direction'}={}){
    if(!this.session||!this.state.configured)throw Error('Wait for Director to confirm configuration.');
    if(typeof prompt!=='string'||!prompt.trim()||prompt.length>2000)throw Error('Enter a direction of 1–2,000 characters.');
    const version=this.state.version+1;this.sentAt.set(version,this.now());this.event('direction',{version,source:String(source).slice(0,40),label:String(label).slice(0,240)});
    this.update({version,ack:'Direction sent · waiting for acknowledgement'});
    this.session.send({type:'prompt',prompt_version:version,prompt:prompt.trim(),replan:true});return version;
  }
  receipt(){return {model:this.model||'minimax/h3-max/director',mode:this.mode,resolution:this.state.resolution,source:'fal-live-stream',targetSeconds:this.sampleSeconds,generatedLimitSeconds:this.generatedLimitSeconds,startedAt:this.startedAt,startupMs:this.state.startupMs,elapsedSeconds:(this.now()-(this.firstFrameAt??this.now()))/1000,presentedSeconds:this.state.presentedSeconds,generatedSeconds:this.state.generatedSeconds,frames:this.state.frames,stallCount:this.state.stallCount,maxFrameGapMs:this.state.maxFrameGapMs,reason:this.state.ack,media:this.media,events:[...this.events]};}
  async stop(reason='Stopped by operator'){
    if(this.state.phase==='stopped'&&!this.session)return;
    const current=this.session;this.session=null;this.firstFrameDeadline=null;++this.epoch;clearTimeout(this.timer);clearTimeout(this.failureTimer);clearInterval(this.watch);
    this.event('stop',{label:reason});this.update({phase:'stopped',configured:false,ack:reason,deadline:null});
    try{current?.send({type:'stop'});}catch{}
    // Begin recorder flush and revoke local admission before transport teardown.
    try{void this.onEnd(this.receipt());}catch{}
    this.onMedia(null);
    try{await current?.close();}catch{}
  }
}
