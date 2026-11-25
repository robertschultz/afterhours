// Count compositor frames independently of the legacy playback-quality counter.
// Generated chunks and the media clock alone are not proof of a received picture.
export class VideoFrameMonitor {
  constructor(video,{onFrame=()=>{},onHealth=()=>{},document=globalThis.document}={}){
    Object.assign(this,{video,onFrame,onHealth,document});this.running=false;this.handle=null;
  }
  start({preserveCount=false}={}){
    this.stop();this.running=true;this.count=preserveCount?(this.count||0):0;this.previous=null;
    this.callbackMode=typeof this.video.requestVideoFrameCallback==='function';
    this.observe();
  }
  observe(){
    if(!this.running||!this.callbackMode)return;
    this.handle=this.video.requestVideoFrameCallback((_,metadata)=>{
      this.handle=null;if(!this.running)return;
      this.advance(metadata?.presentedFrames);
      this.poll();this.observe();
    });
  }
  advance(value){
    if(!Number.isFinite(value)||value<1)return;
    // A changed MediaStream can reset a browser counter; only new frames count.
    const delta=this.previous===null||value<this.previous?1:Math.max(0,value-this.previous);
    this.previous=value;
    if(delta){this.count+=delta;this.onFrame(this.count);}
  }
  poll(){
    if(!this.running)return;
    const v=this.video,quality=v.getVideoPlaybackQuality?.(),track=v.srcObject?.getVideoTracks?.()[0];
    if(!this.callbackMode&&v.readyState>=2&&quality)this.advance(quality.totalVideoFrames-quality.droppedVideoFrames);
    this.onHealth({
      source:this.callbackMode?'video-frame-callback':'playback-quality',
      presentedFrames:this.count,
      totalVideoFrames:quality?.totalVideoFrames,
      droppedVideoFrames:quality?.droppedVideoFrames,
      readyState:v.readyState,paused:v.paused,ended:v.ended,
      documentHidden:this.document?.hidden===true,
      trackMuted:track?.muted===true,trackEnded:track?.readyState==='ended'
    });
  }
  stop(){
    this.running=false;
    if(this.handle!==null)this.video.cancelVideoFrameCallback?.(this.handle);
    this.handle=null;
  }
}
