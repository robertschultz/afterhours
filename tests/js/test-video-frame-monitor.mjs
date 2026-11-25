import test from 'node:test';
import assert from 'node:assert/strict';
import {VideoFrameMonitor} from '../../src/afterhours/web/video-frame-monitor.js';
import {DirectorController} from '../../src/afterhours/web/director-controller.js';

function fixture(callbacks=true){
  let callback,quality={totalVideoFrames:1,droppedVideoFrames:0},cancelled=0;
  const seen=[],health=[],video={readyState:4,paused:false,ended:false,
    getVideoPlaybackQuality:()=>quality,srcObject:{getVideoTracks:()=>[{muted:false,readyState:'live'}]}};
  if(callbacks){video.requestVideoFrameCallback=fn=>{callback=fn;return 1;};video.cancelVideoFrameCallback=()=>cancelled++;}
  const monitor=new VideoFrameMonitor(video,{onFrame:n=>seen.push(n),onHealth:h=>health.push(h),document:{hidden:false}});
  monitor.start();
  return {monitor,video,seen,health,emit:n=>callback(0,{presentedFrames:n}),quality:q=>quality=q,get cancelled(){return cancelled;}};
}
test('advancing compositor frames survive a frozen legacy quality counter',()=>{
  const f=fixture();for(let n=1;n<=960;n++){f.emit(n);f.monitor.poll();}
  assert.equal(f.seen.at(-1),960);assert.equal(f.health.at(-1).totalVideoFrames,1);
  assert.equal(f.health.at(-1).source,'video-frame-callback');f.monitor.stop();
});
test('polling never manufactures frames; metadata jumps and counter resets count new frames only',()=>{
  const f=fixture();f.emit(500);f.emit(510);f.emit(510);f.emit(1);f.emit(2);
  for(let n=0;n<100;n++)f.monitor.poll();assert.deepEqual(f.seen,[1,11,12,13]);
  f.monitor.stop();f.emit(99);assert.equal(f.seen.at(-1),13);assert.equal(f.cancelled,1);
});
test('older browsers use the quality counter, subtract dropped frames and ignore unavailable video',()=>{
  const f=fixture(false);f.video.readyState=0;f.monitor.poll();assert.equal(f.seen.length,0);
  f.video.readyState=4;f.monitor.poll();f.quality({totalVideoFrames:51,droppedVideoFrames:20});f.monitor.poll();
  assert.deepEqual(f.seen,[1,31]);assert.equal(f.health.at(-1).source,'playback-quality');f.monitor.stop();
});
test('replacing a video track keeps the session frame count monotonic; a new session resets it',()=>{
  const f=fixture();f.emit(1);f.emit(240);
  f.monitor.start({preserveCount:true});f.emit(1);f.emit(24);
  assert.deepEqual(f.seen,[1,240,241,264]);
  f.monitor.start();f.emit(1);assert.equal(f.seen.at(-1),1);f.monitor.stop();
});
test('fresh received counters in a hidden live preview do not falsely stop or count as presented',async()=>{
  let clock=1000;const c=new DirectorController({now:()=>clock,open:()=>({send(){},close:async()=>{}})});
  c.start({prompt:'Test',imageUrl:'https://example.invalid',mode:'live',sampleSeconds:null,generatedLimitSeconds:285,deadline:421});
  c.message({type:'configured',prompt_version:1});c.frame(1);
  for(let n=0;n<40;n++){clock+=1000;c.mediaHealth({source:'video-frame-callback',totalVideoFrames:n*24,droppedVideoFrames:n*24,presentedFrames:1,documentHidden:true,readyState:4});c.checkStall();}
  assert.equal(c.state.phase,'live');assert.equal(c.state.frames,1);
  assert.equal(c.state.stallCount,0);clock+=8000;c.checkStall();assert.equal(c.state.phase,'stopped');
});
test('sample, paused preview and stopped receiving retain the eight-second guard',async()=>{
  for(const mode of ['sample','live']){
    let clock=1000;const c=new DirectorController({now:()=>clock,open:()=>({send(){},close:async()=>{}})});
    c.start({prompt:'Test',imageUrl:'https://example.invalid',mode,sampleSeconds:mode==='sample'?30:null,generatedLimitSeconds:mode==='sample'?45:285,deadline:421});c.message({type:'configured',prompt_version:1});c.frame(1);
    c.mediaHealth({totalVideoFrames:1,documentHidden:true,paused:mode==='live'});clock=9000;
    c.mediaHealth({totalVideoFrames:240,documentHidden:true,paused:mode==='live',url:'private',key:'secret'});c.checkStall();
    assert.equal(c.state.phase,'stopped');assert.equal(c.state.frames,1);assert.doesNotMatch(JSON.stringify(c.receipt()),/private|secret/);
  }
});
