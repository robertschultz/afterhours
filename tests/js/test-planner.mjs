import test from 'node:test';
import assert from 'node:assert/strict';
import {rankTracks,transitionPlan,fadeCurve,harmonicDistance} from '../../src/afterhours/web/planner.js';
import {analyze} from '../../src/afterhours/web/analysis.js';

test('harmonic wheel wraps and distinguishes relative major/minor',()=>{
  assert.equal(harmonicDistance('12A','1A'),1);
  assert.equal(harmonicDistance('8A','8B'),1);
  assert.equal(harmonicDistance('8A','8A'),0);
});
test('selector uses fresh tracks before repeating and never selects the current track',()=>{
  const a={id:'a',bpm:120,camelot:'8A',energy:.4};
  const b={id:'b',bpm:122,camelot:'9A',energy:.6};
  const c={id:'c',bpm:125,camelot:'9B',energy:.7};
  assert.equal(rankTracks([a,b,c],a,.6,[])[0].track.id,'b');
  assert.equal(rankTracks([a,b,c],a,.6,['b'])[0].track.id,'c');
  assert.ok(rankTracks([a,b,c],a,.6,['b','c']).every(t=>t.track.id!=='a'));
});
test('equal-power fades do not add a power bump at the center',()=>{
  const a=fadeCurve(false),b=fadeCurve(true);
  for(let i=0;i<a.length;i++)assert.ok(Math.abs(a[i]**2+b[i]**2-1)<1e-6);
});
test('transition fits remaining audio even when requested near the end',()=>{
  for(const remaining of [.05,.1,.2,.4,1,4,16,50]){
    const p=transitionPlan({now:10,position:64-remaining,duration:64,bpm:120,incomingBpm:122,quick:true});
    assert.ok(p.start>=10);
    assert.ok(p.start+p.duration<=10+remaining+.001,JSON.stringify({remaining,p}));
  }
});
test('extreme tempo gaps use a free-tempo transition',()=>{
  const p=transitionPlan({now:0,position:0,duration:64,bpm:90,incomingBpm:140});
  assert.equal(p.matched,false);assert.equal(p.rate,1);
});
test('tempo analyzer recovers a known transient grid',()=>{
  const rate=11025,bpm=128,samples=new Float32Array(rate*20);
  for(let at=0;at<samples.length;at+=rate*60/bpm){for(let i=0;i<900&&Math.round(at)+i<samples.length;i++)samples[Math.round(at)+i]=Math.sin(i*.18)*Math.exp(-i/190)*.8;}
  const result=analyze(samples,rate);assert.ok(Math.abs(result.bpm-bpm)<=1,JSON.stringify(result));
});
