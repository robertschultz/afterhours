import test from 'node:test';
import assert from 'node:assert/strict';
import {LiveVoiceClient} from '../../src/afterhours/web/live-voice.js';
class Channel extends EventTarget {readyState='open';sent=[];send(text){this.sent.push(JSON.parse(text));}close(){this.readyState='closed';this.dispatchEvent(new Event('close'));}}
class Peer extends EventTarget {
  iceGatheringState='complete';connectionState='connected';channel=new Channel();closed=false;
  createDataChannel(){return this.channel;}addTrack(){}async createOffer(){return {type:'offer',sdp:'v=0\r\noffer'};}
  async setLocalDescription(value){this.localDescription=value;}async setRemoteDescription(value){this.remoteDescription=value;}
  close(){this.closed=true;}
}
function fixture(override={}){
  const calls=[],captions=[],delegations=[],states=[],track={enabled:true,stopped:false,stop(){this.stopped=true;}};
  const mic={getTracks:()=>[track],getAudioTracks:()=>[track]};
  const request=async(path,body)=>{calls.push({path,body});return body.action==='start'?{session:{id:'voice_fixture'},transport:{sdp:'v=0\r\nanswer'},expiresAt:Date.now()/1000+1800,maxSeconds:1800,startedAt:Date.now()/1000}:{};};
  const client=new LiveVoiceClient({request,onState:s=>states.push(s),onTranscript:f=>captions.push(f),onDelegate:d=>delegations.push(d),
    mediaDevices:{getUserMedia:async()=>mic},Peer,audio:{setAttribute(){},pause(){},async play(){}},...override});
  return {client,calls,captions,delegations,states,track,mic};
}
const flush=()=>new Promise(r=>setImmediate(r));
test('WebRTC waits for session.started; no API key reaches browser, mute is local and provider controlled',async()=>{
  const {client,calls,track}=fixture();await client.start('nova');const channel=client.channel;
  assert.equal(client.ready,false);assert.equal(channel.sent.length,0);assert.equal(calls[0].body.approveRequest,true);assert.equal(calls[0].body.maxSeconds,1800);
  client.event({type:'session.started',event_id:'start'});assert.equal(client.ready,true);
  assert.equal(channel.sent[0].type,'session.commentary.append');
  client.mute(true);assert.equal(track.enabled,false);assert.equal(channel.sent.at(-1).type,'session.input_audio.mute');
  await client.hear();assert.equal(client.phase,'muted');
  client.mute(false);assert.equal(track.enabled,true);assert.equal(channel.sent.at(-1).type,'session.input_audio.unmute');
  client.event({type:'session.closed',usage:{seconds:10}});assert.equal(client.ready,false);assert.equal(track.stopped,true);
});
test('delegations consume new speech once; status checks never resubmit the seed',async()=>{
  const {client,captions,delegations}=fixture();await client.start('nova');client.event({type:'session.started'});
  const first={type:'session.input_transcript.delta',event_id:'f1',delta:'Play house',start_ms:0,end_ms:500};
  client.event(first);client.event(first);client.event({type:'session.input_transcript.delta',event_id:'f2',delta:' around 125 BPM.',start_ms:500,end_ms:1200});
  const delegated={type:'session.delegation.created',delegation:{id:'d1',target:'client'},offset_ms:1300};client.event(delegated);client.event(delegated);
  assert.equal(captions.length,2);assert.equal(delegations.length,1);assert.equal(delegations[0].text,'Play house around 125 BPM.');
  client.context('Three tracks selected. Preparing their audio.');
  client.event({...delegated,delegation:{id:'d2',target:'client'}});
  assert.equal(delegations.length,1);
  assert.match(client.channel.sent.at(-1).content,/Do not restart discovery/);
  assert.match(client.channel.sent.at(-1).content,/Three tracks selected/);
  assert.equal(client.channel.sent.at(-1).delegation_id,'d2');
  client.event({type:'session.input_transcript.delta',delta:'Make it deeper, less vocal.'});
  client.event({...delegated,delegation:{id:'d3',target:'client'}});
  assert.equal(delegations.length,2);assert.equal(delegations[1].text,'Make it deeper, less vocal.');
  client.event({type:'session.closed',usage:{seconds:2}});
  await client.start('nova');client.event({type:'session.started'});
  client.event({...delegated,delegation:{id:'new-session',target:'client'}});
  assert.equal(delegations.length,2);assert.doesNotMatch(client.channel.sent.at(-1).content,/Three tracks/);
  client.event({type:'session.closed',usage:{seconds:0}});
});
test('close waits for final receipt while stopping capture immediately',async()=>{
  const {client,calls,track}=fixture();await client.start('rivet');client.event({type:'session.started'});const peer=client.peer;
  client.end();assert.equal(client.phase,'closing');assert.equal(track.stopped,true);assert.equal(peer.closed,false);
  assert.equal(client.channel.sent.at(-1).type,'session.close');client.event({type:'session.closed',usage:{seconds:15}});
  assert.equal(peer.closed,true);assert.equal(client.phase,'closed');assert.equal(calls.at(-1).body.finalized,true);
});
test('denied microphone never starts a paid session',async()=>{
  const error=Object.assign(Error('Denied'),{name:'NotAllowedError'});
  const {client,calls}=fixture({mediaDevices:{getUserMedia:async()=>{throw error;}}});await client.start('nova');
  assert.equal(calls.length,0);assert.equal(client.phase,'error');assert.equal(client.startPending,false);
});
test('cancelling an unresolved microphone prompt prevents session creation and stops late tracks',async()=>{
  let resolve;const {client,calls,mic,track}=fixture({mediaDevices:{getUserMedia:()=>new Promise(r=>resolve=r)}});
  const starting=client.start('nova');client.end();await client.start('nova');resolve(mic);await starting;
  assert.equal(calls.length,0);assert.equal(track.stopped,true);assert.equal(client.phase,'closed');
});
test('cancelling while server connects closes the returned session and cannot race another start',async()=>{
  let resolve,started;const reached=new Promise(r=>started=r),calls=[];
  const {client}=fixture({request:async(path,body)=>{calls.push(body);if(body.action==='start'){started();return new Promise(r=>resolve=r);}return {};}});
  const connecting=client.start('nova');await reached;client.end();await client.start('rivet');
  resolve({session:{id:'cancelled_session'},transport:{sdp:'v=0'},expiresAt:Date.now()/1000+120});await connecting;
  assert.deepEqual(calls.map(c=>c.action),['start','end']);assert.equal(calls[1].sessionId,'cancelled_session');assert.equal(client.phase,'closed');
});
test('connection loss closes remotely once and ignores late events',async()=>{
  const {client,calls}=fixture();await client.start('nova');client.event({type:'session.started'});const channel=client.channel;channel.close();await flush();
  assert.equal(calls.filter(c=>c.body.action==='end').length,1);assert.equal(client.phase,'closed');assert.equal(client.microphone,null);
});
test('missing device is actionable and never sends a paid handshake',async()=>{
  const error=Object.assign(Error('Requested device not found'),{name:'NotFoundError'});
  const {client,calls,states}=fixture({mediaDevices:{getUserMedia:async()=>{throw error;},enumerateDevices:async()=>[{kind:'audiooutput',deviceId:'speaker'}]}});
  assert.deepEqual(await client.microphones(),[]);await client.start('nova');
  assert.equal(calls.length,0);assert.match(states.at(-1).detail,/Connect a microphone/);
});
test('selected input is used and device listing does not request microphone access',async()=>{
  let constraints,access=0;const track={stop(){},enabled:true};const mic={getTracks:()=>[track],getAudioTracks:()=>[track]};
  const {client}=fixture({mediaDevices:{enumerateDevices:async()=>[{kind:'audioinput',deviceId:'usb-mic',label:'Studio Display Microphone'}],getUserMedia:async c=>{constraints=c;access++;return mic;}}});
  assert.equal((await client.microphones())[0].label,'Studio Display Microphone');assert.equal(access,0);
  await client.start('nova','usb-mic');assert.deepEqual(constraints.audio.deviceId,{exact:'usb-mic'});
  client.event({type:'session.closed',usage:{seconds:0}});
});
test('expiry remains explicit when the provider finalizes or disconnects first',async()=>{
  for(const finalReceipt of [true,false]){
    const {client,states,track}=fixture();await client.start('nova');client.event({type:'session.started'});
    client.expiresAt=Date.now()/1000-1;
    if(finalReceipt)client.event({type:'session.closed',usage:{seconds:120}});else client.channel.close();
    assert.match(states.at(-1).detail,/30-minute talk limit reached/);
    assert.match(states.at(-1).detail,/Microphone off/);assert.equal(track.stopped,true);
    assert.equal(states.at(-1).detail.includes('unconfirmed'),!finalReceipt);
  }
});
test('an explicit end cause survives finalization and does not leak into a new session',async()=>{
  const {client,states}=fixture();await client.start('nova');client.event({type:'session.started'});
  client.end('Voice stopped when this tab was hidden.');client.event({type:'session.closed',usage:{seconds:12}});
  assert.match(states.at(-1).detail,/tab was hidden/);
  await client.start('nova');client.event({type:'session.started'});client.end();client.event({type:'session.closed',usage:{seconds:2}});
  assert.doesNotMatch(states.at(-1).detail,/tab was hidden/);assert.match(states.at(-1).detail,/Talk stopped/);
});
