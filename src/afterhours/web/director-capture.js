// Captures only the generated remote stream and, optionally, the local original study.
export class DirectorCapture {
  constructor(){this.parts=[];this.recorder=null;this.bytes=0;this.stopped=null;}
  static mime(){return ['video/mp4;codecs=avc1.42E01E,mp4a.40.2','video/webm;codecs=vp8,opus','video/webm'].find(type=>globalThis.MediaRecorder?.isTypeSupported(type))||null;}
  start(stream,audioTrack=null){
    if(this.recorder)return;
    const mimeType=DirectorCapture.mime();if(!mimeType)throw Error('This browser cannot record the stream. Open Afterhours in Chrome or Safari.');
    const tracks=[...stream.getVideoTracks(),...(audioTrack?[audioTrack]:[])];
    this.recorder=new MediaRecorder(new MediaStream(tracks),{mimeType,videoBitsPerSecond:4_000_000});
    this.stopped=new Promise((resolve,reject)=>{
      this.recorder.addEventListener('dataavailable',e=>{if(e.data.size){this.bytes+=e.data.size;this.parts.push(e.data);if(this.bytes>55_000_000&&this.recorder.state!=='inactive')this.recorder.stop();}});
      this.recorder.addEventListener('stop',()=>resolve(new Blob(this.parts,{type:this.recorder.mimeType.split(';')[0]})),{once:true});
      this.recorder.addEventListener('error',()=>reject(Error('The local recording failed.')),{once:true});
    });
    this.recorder.start(1000);
  }
  stop(){if(!this.recorder)return Promise.resolve(null);if(this.recorder.state!=='inactive')this.recorder.stop();return this.stopped;}
}
