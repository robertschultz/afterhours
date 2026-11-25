import test from 'node:test';
import assert from 'node:assert/strict';
import {beginDirectorSession} from '../../src/afterhours/web/director-setup.js';

test('explicit On prepares the selected reference before one admission',async()=>{
  const calls=[],progress=[],request={profile:'director-live-v1',consent:true,minutes:5};
  const admission=await beginDirectorSession({character:'nova',referenceReady:false,request,post:async(...args)=>{calls.push(args);return{lease:'fake'};},onProgress:text=>progress.push(text)});
  assert.deepEqual(calls,[['/api/director/reference',{consent:true,character:'nova'},60000],['/api/director/start',request]]);
  assert.equal(admission.lease,'fake');assert.equal(progress.length,2);
});
test('an existing reference is reused; a failed upload never starts or retries video',async()=>{
  const calls=[];
  await beginDirectorSession({character:'nova',referenceReady:true,request:{},post:async(path)=>calls.push(path)});
  assert.deepEqual(calls,['/api/director/start']);calls.length=0;
  await assert.rejects(beginDirectorSession({character:'nova',referenceReady:false,request:{},post:async(path)=>{calls.push(path);throw Error('upload denied');}}),/upload denied/);
  assert.deepEqual(calls,['/api/director/reference']);
});
