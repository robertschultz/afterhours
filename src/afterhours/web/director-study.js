// Original locally synthesized music; no catalogue recording is uploaded or captured.
export class OriginalStudy {
  async prepare(){
    this.context=new AudioContext();await this.context.resume();
    const r=await fetch('/audio/open-air.wav');if(!r.ok)throw Error('Original music study is unavailable.');
    this.buffer=await this.context.decodeAudioData(await r.arrayBuffer());this.offset=16;
    const samples=this.buffer.getChannelData(0),rate=this.buffer.sampleRate;this.contour=[];
    for(let sec=0;sec<30;sec+=.5){let square=0,count=0;for(let i=Math.floor((this.offset+sec)*rate);i<Math.min(samples.length,(this.offset+sec+.5)*rate);i+=8){square+=samples[i]*samples[i];count++;}this.contour.push([sec,.5,20*Math.log10(Math.max(1e-5,Math.sqrt(square/Math.max(1,count))))]);}
    this.destination=this.context.createMediaStreamDestination();return this;
  }
  start(){if(this.source)return;this.source=this.context.createBufferSource();this.source.buffer=this.buffer;this.source.connect(this.destination);this.source.connect(this.context.destination);this.began=this.context.currentTime;this.source.start(0,this.offset,30);}
  program(){const position=this.source?this.context.currentTime-this.began:0;return {connected:true,playing:!!this.source&&position<30,current:'original-study',positionSeconds:position,durationSeconds:30,clock:{sampledAtUnixMs:Date.now(),bpm:124},library:[{id:'original-study',title:'Original house study',artist:'Afterhours',camelot:'9B'}],energyMap:{coverage:1,contour:this.contour}};}
  track(){return this.destination?.stream.getAudioTracks()[0]||null;}
  stop(){try{this.source?.stop();}catch{}void this.context?.close();}
}
