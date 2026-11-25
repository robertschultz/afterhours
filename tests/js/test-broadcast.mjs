import test from 'node:test';import assert from 'node:assert/strict';
import {presentProgram,contourBars,energyAt} from '../../src/afterhours/web/broadcast-data.js';
const now=1700000000000;
const map={coverage:1,duration:100,contour:[[0,50,-20,-25,-30],[50,50,-10,-18,-20]]};
const program={connected:true,playing:true,current:'a',library:[{id:'a',title:'Music',camelot:'2B'}],clock:{sampledAtUnixMs:now-1000,bpm:155},positionSeconds:20,durationSeconds:100,transitionInSeconds:8,energyMap:map,nextTrack:{title:'KJZ'},setElapsedSeconds:10};
test('all displays project observed track, position, tempo and countdown',()=>{const p=presentProgram(program,now);assert.equal(p.position,21);assert.equal(p.remaining,7);assert.equal(p.energyDb,-20);assert.equal(p.status,'LIVE');assert.equal(p.key,'2B');assert.equal(p.progress,.21);});
test('paused and stale states never extrapolate or claim live BPM',()=>{const paused=presentProgram({...program,playing:false},now);assert.equal(paused.position,20);assert.equal(paused.status,'READY');const stale=presentProgram(program,now+3000);assert.equal(stale.position,20);assert.equal(stale.bpm,null);assert.equal(stale.status,'OFFLINE');assert.equal(stale.playing,false);});
test('missing data has no fabricated waveform, level or room count',()=>{const p=presentProgram(null,now);assert.equal(p.track,null);assert.equal(p.energyDb,null);assert.equal(p.duration,null);assert.equal(p.next,null);assert.equal(contourBars(null).length,0);assert.equal(energyAt(map,120),null);assert.equal(energyAt({...map,coverage:.5},5),null);});
test('energy contour amplitudes preserve measured ratios',()=>{const bars=contourBars(map);assert.equal(bars.length,2);assert.equal(bars[1].amplitude,1);assert.ok(Math.abs(bars[0].amplitude-.31623)<.0001);});
