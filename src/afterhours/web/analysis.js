// This worker never uploads audio. Tempo, phase and key are estimates.
export function analyze(samples, sampleRate) {
  const seconds = samples.length/sampleRate;
  const hop = Math.max(1, Math.round(sampleRate/100));
  const envelope = [], rms = [];
  let previous=0, total=0, peak=0;
  for(let s=0;s<samples.length;s+=hop) {
    let sum=0, edge=0;
    for(let i=s;i<Math.min(s+hop,samples.length);i++) {
      const x=samples[i]; sum+=x*x; edge+=Math.abs(x-previous); previous=x;
      total+=x*x; peak=Math.max(peak,Math.abs(x));
    }
    rms.push(Math.sqrt(sum/hop)); envelope.push(Math.sqrt(sum/hop)+edge/hop*.12);
  }
  const flux=envelope.map((v,i)=>Math.max(0,v-(envelope[Math.max(0,i-2)]+envelope[Math.max(0,i-1)])/2));
  const scores=[];
  for(let bpm=70;bpm<=180;bpm+=.25) {
    const lag=6000/bpm;
    let corr=0, norm=0;
    for(let i=Math.ceil(lag*2);i<flux.length;i++) {
      const p=i-lag, j=Math.floor(p), f=p-j;
      const paired=flux[j]*(1-f)+flux[j+1]*f;
      corr+=flux[i]*paired;
      norm+=flux[i]*flux[i];
    }
    // A weak prior only breaks close half/double-time estimates.
    const prior=.88+.12*Math.exp(-(((bpm-122)/42)**2));
    scores.push({bpm,score:corr/Math.max(norm,1e-9)*prior});
  }
  scores.sort((a,b)=>b.score-a.score);
  let bpm=scores[0]?.bpm || 120;
  const period=6000/bpm;
  let bestPhase=0, phaseScore=-1;
  for(let phase=0;phase<period;phase++) {
    let score=0;
    for(let i=phase;i<flux.length;i+=period) score+=flux[Math.round(i)]||0;
    if(score>phaseScore) {phaseScore=score;bestPhase=phase;}
  }
  // Onset flux peaks slightly after a transient; compensate one analysis hop.
  const beatOffset=Math.max(0,(bestPhase-1)/100);
  const peaks=[];
  for(let b=0;b<320;b++) {
    let max=0;
    const begin=Math.floor(b*samples.length/320), end=Math.floor((b+1)*samples.length/320);
    for(let i=begin;i<end;i+=4) max=Math.max(max,Math.abs(samples[i]));
    peaks.push(max);
  }
  const key = estimateKey(samples,sampleRate);
  const globalRms=Math.sqrt(total/Math.max(1,samples.length));
  return {bpm:Math.round(bpm*10)/10,beatOffset,confidence:Math.min(1,(scores[0]?.score||0)*1.7),
    duration:seconds,energy:Math.max(.1,Math.min(.95,globalRms*3.4)),rms:globalRms,peak,peaks,
    ...key, estimated:true};
}

function fft(re, im) {
  const n=re.length;
  for(let i=1,j=0;i<n;i++) {
    let bit=n>>1;
    for(;j&bit;bit>>=1) j^=bit;
    j^=bit;
    if(i<j) { [re[i],re[j]]=[re[j],re[i]]; [im[i],im[j]]=[im[j],im[i]]; }
  }
  for(let size=2;size<=n;size*=2) {
    const angle=-2*Math.PI/size;
    for(let start=0;start<n;start+=size) {
      for(let k=0;k<size/2;k++) {
        const c=Math.cos(angle*k),s=Math.sin(angle*k),a=start+k,b=a+size/2;
        const tr=re[b]*c-im[b]*s,ti=re[b]*s+im[b]*c;
        re[b]=re[a]-tr;im[b]=im[a]-ti;re[a]+=tr;im[a]+=ti;
      }
    }
  }
}

function estimateKey(samples,rate) {
  const chroma=new Float64Array(12), n=4096;
  const stride=Math.max(1,Math.floor(rate/11025)), sr=rate/stride;
  const available=Math.floor(samples.length/stride);
  const frames=Math.min(80,Math.floor(available/n));
  if(frames<2) return {key:'Unknown',camelot:'',keyConfidence:0};
  for(let frame=0;frame<frames;frame++) {
    const at=Math.floor(frame*(available-n)/Math.max(1,frames-1));
    const re=new Float64Array(n),im=new Float64Array(n);
    for(let i=0;i<n;i++) re[i]=samples[(at+i)*stride]*(.5-.5*Math.cos(2*Math.PI*i/(n-1)));
    fft(re,im);
    for(let bin=1;bin<n/2;bin++) {
      const hz=bin*sr/n;
      if(hz<80 || hz>3000) continue;
      const note=69+12*Math.log2(hz/440), nearest=Math.round(note);
      const weight=Math.max(0,1-Math.abs(note-nearest)*2);
      chroma[(nearest%12+12)%12]+=Math.sqrt(Math.hypot(re[bin],im[bin]))*weight;
    }
  }
  const profiles=[[6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88],
    [6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17]];
  const names=['C','C♯','D','E♭','E','F','F♯','G','A♭','A','B♭','B'];
  const wheel=[['8B','3B','10B','5B','12B','7B','2B','9B','4B','11B','6B','1B'],
    ['5A','12A','7A','2A','9A','4A','11A','6A','1A','8A','3A','10A']];
  const mean=chroma.reduce((a,b)=>a+b,0)/12;
  const ranked=[];
  for(let mode=0;mode<2;mode++) for(let root=0;root<12;root++) {
    const p=profiles[mode],pm=p.reduce((a,b)=>a+b,0)/12;
    let dot=0,aa=0,bb=0;
    for(let i=0;i<12;i++) {const a=chroma[(i+root)%12]-mean,b=p[i]-pm;dot+=a*b;aa+=a*a;bb+=b*b;}
    ranked.push({mode,root,score:dot/Math.sqrt(Math.max(aa*bb,1e-12))});
  }
  ranked.sort((a,b)=>b.score-a.score);
  const best=ranked[0];
  return {key:`${names[best.root]} ${best.mode?'minor':'major'}`,camelot:wheel[best.mode][best.root],keyConfidence:Math.max(0,best.score)};
}

if(typeof WorkerGlobalScope!=='undefined' && self instanceof WorkerGlobalScope) {
  self.onmessage=e=> {
    try {self.postMessage({id:e.data.id,result:analyze(e.data.samples,e.data.sampleRate)});}
    catch(error) {self.postMessage({id:e.data.id,error:error.message});}
  };
}
