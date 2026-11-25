// One cloud session, multiple same-origin screens on this Mac. No camera capture.
function gathered(pc){
  return new Promise(resolve=>{
    if(pc.iceGatheringState==='complete')return resolve();
    const done=()=>{if(pc.iceGatheringState==='complete'){clearTimeout(timer);pc.removeEventListener('icegatheringstatechange',done);resolve();}};
    const timer=setTimeout(()=>{pc.removeEventListener('icegatheringstatechange',done);resolve();},2500);
    pc.addEventListener('icegatheringstatechange',done);
  });
}
export function videoRelay(role,onStream=()=>{},name='afterhours-stage-video-v1'){
  const id=crypto.randomUUID(),channel=new BroadcastChannel(name),peers=new Map();
  let stream=null,receiver=null,owner=null,closed=false,pending=false;
  const send=data=>{if(!closed)channel.postMessage({...data,from:id});};
  function clear(){for(const pc of peers.values())pc.close();peers.clear();receiver?.close();receiver=null;owner=null;pending=false;}
  async function offer(to){
    if(!stream?.getVideoTracks().some(t=>t.readyState==='live')||peers.has(to)||peers.size>=5)return;
    const pc=new RTCPeerConnection({iceServers:[]});peers.set(to,pc);
    setTimeout(()=>{if(peers.get(to)===pc&&pc.connectionState!=='connected'){pc.close();peers.delete(to);}},12000);
    pc.onconnectionstatechange=()=>{if(['closed','failed','disconnected'].includes(pc.connectionState)){pc.close();if(peers.get(to)===pc)peers.delete(to);}};
    for(const track of stream.getVideoTracks())pc.addTrack(track,stream);
    try{await pc.setLocalDescription(await pc.createOffer());await gathered(pc);if(peers.get(to)===pc)send({type:'offer',to,sdp:pc.localDescription.toJSON()});}
    catch{pc.close();peers.delete(to);}
  }
  channel.onmessage=async({data})=>{
    if(closed||data.from===id||(data.to&&data.to!==id))return;
    if(role==='sender'){
      if(data.type==='hello')await offer(data.from);
      if(data.type==='bye'){peers.get(data.from)?.close();peers.delete(data.from);}
      if(data.type==='answer'){try{await peers.get(data.from)?.setRemoteDescription(data.sdp);}catch{peers.get(data.from)?.close();peers.delete(data.from);}}
      return;
    }
    if(data.type==='reset'){clear();onStream(null);send({type:'hello'});}
    if(data.type==='offer'&&!pending){
      pending=true;receiver?.close();const pc=new RTCPeerConnection({iceServers:[]});receiver=pc;owner=data.from;
      pc.ontrack=e=>{if(receiver===pc)onStream(e.streams[0]||new MediaStream([e.track]));};
      pc.onconnectionstatechange=()=>{if(receiver===pc&&['failed','disconnected','closed'].includes(pc.connectionState)){clear();onStream(null);}};
      try{await pc.setRemoteDescription(data.sdp);await pc.setLocalDescription(await pc.createAnswer());await gathered(pc);if(receiver===pc)send({type:'answer',to:owner,sdp:pc.localDescription.toJSON()});}
      catch{if(receiver===pc){clear();onStream(null);}}
      finally{pending=false;}
    }
  };
  const timer=role==='receiver'?setInterval(()=>{if(!receiver&&!pending)send({type:'hello'});},2000):null;
  if(role==='receiver')send({type:'hello'});
  return {
    setStream(next){clear();stream=next;send({type:'reset'});},
    close(){send({type:'bye'});closed=true;clearInterval(timer);clear();channel.close();},
  };
}
