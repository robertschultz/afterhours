import test from 'node:test';import assert from 'node:assert/strict';import {timelineAt,selectSegment} from '../../src/afterhours/web/performance-client.js';
const segments=[{id:'a',start:0,duration:8,inPoint:0},{id:'b',start:8,duration:8,inPoint:1.625}];
test('shared clock advances only while playing',()=>{assert.equal(timelineAt({timeline:3,mode:'playing',serverTime:100},102000),5);assert.equal(timelineAt({timeline:3,mode:'paused',serverTime:100},102000),3);});
test('continuation skips its repeated source lead-in',()=>{const s=selectSegment({segments},9);assert.equal(s.segment.id,'b');assert.equal(s.time,2.625);assert.equal(s.fallback,false);});
test('underflow repeats only available footage and labels fallback',()=>{const s=selectSegment({segments},18);assert.equal(s.fallback,true);assert.equal(s.time,3.625);});
test('late generated segment can start after a fallback gap',()=>{const s=selectSegment({segments:[segments[0],{...segments[1],start:20}]},16);assert.equal(s.segment.id,'a');assert.equal(s.fallback,true);});
test('empty timeline gives no fabricated video',()=>assert.equal(selectSegment({segments:[]},0),null));
