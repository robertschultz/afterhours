import {clamp, fadeCurve, transitionPlan} from './planner.js';

export class AudioEngine extends EventTarget {
  constructor() {
    super(); this.ctx=null;this.buffers=new Map();this.decks=[{},{}];
    this.active=0;this.transition=null;this.volume=.65;this.crossfader=0;this.paused=false;
    this.recorder=null;this.recording=false;this.lastRecording=null;this.renderCounter=0;
  }
  emit(type,detail={}) {this.dispatchEvent(new CustomEvent(type,{detail}));}
  async init() {
    if(!this.ctx) {
      this.ctx=new AudioContext({latencyHint:'playback'});
      this.master=this.ctx.createGain();this.master.gain.value=this.volume;
      this.limiter=this.ctx.createDynamicsCompressor();
      this.limiter.threshold.value=-5;this.limiter.knee.value=3;this.limiter.ratio.value=18;
      this.limiter.attack.value=.003;this.limiter.release.value=.15;
      this.analyser=this.ctx.createAnalyser();this.analyser.fftSize=2048;
      this.meterData=new Float32Array(this.analyser.fftSize);
      this.stream=this.ctx.createMediaStreamDestination();
      this.limiter.connect(this.master);this.master.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);this.master.connect(this.stream);
      this.decks=this.decks.map(()=>{
        const trim=this.ctx.createGain(),low=this.ctx.createBiquadFilter(),high=this.ctx.createBiquadFilter(),gain=this.ctx.createGain();
        low.type='lowshelf';low.frequency.value=180;high.type='highshelf';high.frequency.value=5500;
        trim.connect(low);low.connect(high);high.connect(gain);gain.connect(this.limiter);gain.gain.value=0;
        return {trim,low,high,gain,track:null,source:null,offset:0,rate:1,startAt:0,playing:false};
      });
      this.timer=setInterval(()=>this.tick(),100);
    }
    await this.ctx.resume();
    if(this.ctx.state!=='running') throw new Error('Audio is locked. Click Enable audio in this window.');
    return this.ctx;
  }
  async decode(id,data) {
    if(!this.ctx) throw new Error('Enable audio first.');
    const buffer=await this.ctx.decodeAudioData(data.slice(0));
    if(buffer.duration<4) throw new Error('Choose a track longer than four seconds.');
    if(buffer.duration>1200) throw new Error('Use tracks shorter than 20 minutes in this prototype.');
    this.buffers.set(id,buffer);return buffer;
  }
  load(index,track) {
    if(!this.buffers.has(track.id)) throw new Error('The track has not decoded yet.');
    this.stopDeck(index); const d=this.decks[index];
    d.track=track;d.offset=0;d.rate=1;d.playing=false;
    d.trim.gain.value=clamp(.16/Math.max(track.rms||.16,.025),.4,2);
    d.low.gain.value=0;d.high.gain.value=0;this.emit('load',{index,track});
  }
  playDeck(index,when=this.ctx.currentTime+.035,offset,rate=1) {
    const d=this.decks[index];if(!d.track) throw new Error('Load a track first.');
    this.stopDeck(index);
    d.offset=clamp(offset??d.offset,0,this.buffers.get(d.track.id).duration-.01);
    d.rate=rate;d.startAt=when;d.playing=true;
    const src=this.ctx.createBufferSource();src.buffer=this.buffers.get(d.track.id);src.playbackRate.value=rate;
    src.connect(d.trim);d.source=src;src.start(when,d.offset);
    src.onended=()=> {if(d.source===src){d.playing=false;d.source=null;this.emit('ended',{index,track:d.track});}};
    this.paused=false;this.emit('play',{index});
  }
  stopDeck(index) {
    const d=this.decks[index];if(d.source){d.source.onended=null;try{d.source.stop();}catch{}d.source.disconnect();d.source=null;}
    d.playing=false;
  }
  position(index) {
    const d=this.decks[index];if(!d.track)return 0;
    const duration=this.buffers.get(d.track.id)?.duration||d.track.duration;
    return clamp(d.offset+(d.playing?Math.max(0,this.ctx.currentTime-d.startAt)*d.rate:0),0,duration);
  }
  async start(track) {
    await this.init();this.stop();this.active=0;this.load(0,track);
    this.setCrossfader(0);this.playDeck(0);this.emit('started');
  }
  setCrossfader(value) {
    this.crossfader=clamp(value,0,1);if(!this.ctx)return;
    const now=this.ctx.currentTime;
    this.decks.forEach((d,i)=>{d.gain.gain.cancelScheduledValues(now);d.gain.gain.setTargetAtTime(i?Math.sin(this.crossfader*Math.PI/2):Math.cos(this.crossfader*Math.PI/2),now,.025);});
  }
  setVolume(value) {this.volume=clamp(value,0,1);this.master?.gain.setTargetAtTime(this.volume,this.ctx.currentTime,.02);}
  setEQ(index,band,value) {this.decks[index][band].gain.setTargetAtTime(clamp(value,-18,6),this.ctx.currentTime,.025);}
  schedule(track,quick=false,maxTempoShift=8) {
    if(this.transition)throw new Error('A transition is already scheduled.');
    const from=this.active,to=1-from,d=this.decks[from];
    if(!d.playing)throw new Error('Start the set before mixing.');
    const now=this.ctx.currentTime;
    const plan=transitionPlan({now,position:this.position(from),duration:d.track.duration,
      bpm:d.track.bpm,rate:d.rate,beatOffset:d.track.beatOffset||0,incomingBpm:track.bpm,quick,maxTempoShift});
    this.load(to,track);this.playDeck(to,plan.start,track.beatOffset||0,plan.rate);
    const incoming=this.decks[to];
    d.gain.gain.cancelScheduledValues(now);incoming.gain.gain.cancelScheduledValues(now);
    d.gain.gain.setValueAtTime(1,now);incoming.gain.gain.setValueAtTime(0,now);
    d.gain.gain.setValueCurveAtTime(fadeCurve(false),plan.start,plan.duration);
    incoming.gain.gain.setValueCurveAtTime(fadeCurve(true),plan.start,plan.duration);
    // Bass handoff keeps two kicks/basslines from accumulating throughout the blend.
    incoming.low.gain.cancelScheduledValues(now);incoming.low.gain.setValueAtTime(-12,now);
    incoming.low.gain.setValueAtTime(-12,plan.start+plan.duration*.35);
    incoming.low.gain.linearRampToValueAtTime(0,plan.start+plan.duration*.65);
    d.low.gain.setValueAtTime(d.low.gain.value,plan.start+plan.duration*.35);
    d.low.gain.linearRampToValueAtTime(-12,plan.start+plan.duration*.65);
    this.transition={...plan,from,to,track,end:plan.start+plan.duration};
    this.emit('scheduled',this.transition);return this.transition;
  }
  cancelTransition() {
    const t=this.transition;if(!t)return;
    const now=this.ctx.currentTime;
    if(now<t.start) {
      this.stopDeck(t.to);this.active=t.from;
      this.decks.forEach(d=>d.low.gain.cancelScheduledValues(now));
      this.decks[t.from].low.gain.setTargetAtTime(0,now,.05);
      this.setCrossfader(t.from);
    } else {
      const p=clamp((now-t.start)/t.duration,0,1);
      this.active=p<.5?t.from:t.to;
      this.decks.forEach(d=>{d.low.gain.cancelAndHoldAtTime(now);});
      this.setCrossfader(t.from===0?p:1-p);
    }
    this.transition=null;this.emit('takeover');
  }
  pause() {
    if(!this.ctx)return;
    this.cancelTransition();
    this.decks.forEach((d,i)=>{const position=this.position(i);this.stopDeck(i);d.offset=position;});
    this.paused=true;this.emit('pause');
  }
  async resume() {
    await this.init();
    this.decks.forEach((d,i)=>{if(d.track&&d.offset<d.track.duration-.1&&((i===0&&this.crossfader<.99)||(i===1&&this.crossfader>.01)))this.playDeck(i,undefined,d.offset,d.rate);});
    this.paused=false;
  }
  stop() {
    if(!this.ctx)return;
    this.transition=null;
    this.decks.forEach((d,i)=>{this.stopDeck(i);d.offset=0;d.gain.gain.cancelScheduledValues(this.ctx.currentTime);d.gain.gain.value=0;d.low.gain.cancelScheduledValues(this.ctx.currentTime);d.low.gain.value=0;});
    this.crossfader=0;this.paused=false;this.emit('stop');
  }
  seek(index,seconds) {
    this.cancelTransition();const d=this.decks[index];const playing=d.playing;
    const offset=clamp(seconds,0,d.track.duration-.02);
    if(playing)this.playDeck(index,undefined,offset,d.rate);else d.offset=offset;
  }
  tick() {
    const t=this.transition;
    if(t&&this.ctx.currentTime>=t.end) {
      this.stopDeck(t.from);this.decks[t.from].offset=0;
      this.active=t.to;this.transition=null;this.setCrossfader(t.to);
      this.decks[t.to].low.gain.cancelScheduledValues(this.ctx.currentTime);this.decks[t.to].low.gain.value=0;
      this.emit('transitionend',{index:t.to,track:t.track});
    }
    if(t&&this.transition) {
      const p=clamp((this.ctx.currentTime-t.start)/t.duration,0,1);this.crossfader=t.from===0?p:1-p;
    }
  }
  meter() {
    if(!this.analyser)return {rms:0,peak:0};
    this.analyser.getFloatTimeDomainData(this.meterData);
    let sum=0,peak=0;for(const x of this.meterData){sum+=x*x;peak=Math.max(peak,Math.abs(x));}
    return {rms:Math.sqrt(sum/this.meterData.length),peak};
  }
  startRecording() {
    if(!this.stream)throw new Error('Enable audio first.');
    if(!window.MediaRecorder)throw new Error('Recording is unavailable in this browser. Use Chrome.');
    const mime=['audio/webm;codecs=opus','audio/mp4','audio/webm'].find(t=>MediaRecorder.isTypeSupported(t));
    if(!mime)throw new Error('No recording format is available. Use Chrome.');
    const chunks=[];this.recorder=new MediaRecorder(this.stream.stream,{mimeType:mime});
    this.recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
    this.recorder.onstop=()=>{this.lastRecording=new Blob(chunks,{type:mime});this.emit('recorded',{blob:this.lastRecording,extension:mime.includes('mp4')?'m4a':'webm'});};
    this.recorder.start(1000);this.recording=true;this.recordStart=performance.now();
  }
  stopRecording() {if(this.recorder?.state==='recording')this.recorder.stop();this.recording=false;}
  async renderTransition(first,second) {
    const sampleRate=22050,fade=8,length=20;
    const offline=new OfflineAudioContext(2,sampleRate*length,sampleRate);
    const compressor=offline.createDynamicsCompressor();compressor.threshold.value=-5;compressor.ratio.value=18;
    const master=offline.createGain();master.gain.value=.65;compressor.connect(master);master.connect(offline.destination);
    [first,second].forEach((track,i)=>{
      const source=offline.createBufferSource(),gain=offline.createGain(),trim=offline.createGain();
      source.buffer=this.buffers.get(track.id);source.playbackRate.value=i?first.bpm/second.bpm:1;
      trim.gain.value=clamp(.16/Math.max(track.rms||.16,.025),.4,2);
      gain.gain.setValueAtTime(i?0:1,0);gain.gain.setValueCurveAtTime(fadeCurve(!!i),6,fade);
      source.connect(trim);trim.connect(gain);gain.connect(compressor);
      source.start(i?6:0,i?16*60/second.bpm:16*60/first.bpm);
      if(!i)source.stop(14);
    });
    const buffer=await offline.startRendering();this.renderCounter++;return buffer;
  }
}
