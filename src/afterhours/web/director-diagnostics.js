// Do not retain provider error bodies, credentials, SDP, candidate addresses or URLs.
export function connectionFailure(error){
  const text=typeof error==='string'?error:String(error?.message||'');
  const match=text.match(/(?:HTTP|status)\s+(\d{3})\b/i),status=match?Number(match[1]):null;
  if(/credit_verification_unavailable|could not verify session credit|unable to verify session credit/i.test(text))return 'Fal could not verify session credit (HTTP 503). Check that this key belongs to the account showing your credits. If it does, contact Fal support. No automatic retry.';
  if(status===401)return 'Fal rejected the API key (HTTP 401). Reconnect a valid key; no automatic retry.';
  if(status===402)return 'Fal rejected the connection (HTTP 402). Check Fal credits and billing; no automatic retry.';
  if(status===403)return 'The connection was denied (HTTP 403). Check key permissions and local session access; no automatic retry.';
  if(status===422)return 'Fal rejected the video/audio connection offer (HTTP 422). See Session evidence; no automatic retry.';
  if(status===429)return 'Fal limited the connection (HTTP 429). Check account limits or capacity; no automatic retry.';
  if(status===503)return 'Fal could not open the video session (HTTP 503). See Session evidence for details. No automatic retry was started.';
  if(status)return `Video connection returned HTTP ${status}. No automatic retry was started.`;
  if(/Director request blocked|session expired|local performance authorization/i.test(text))return 'The local video session is no longer authorized. No automatic retry was started.';
  if(/TURN relay|ICE gathering|ICE failed|connection failed.*ICE|peer connection/i.test(text))return 'The WebRTC video connection failed. Check the network diagnostic counts in Session evidence; no automatic retry.';
  if(/timeout|timed out|AbortError/i.test(text))return 'Video connection setup timed out. No automatic retry was started.';
  if(/fetch|network|signaling failed/i.test(text))return 'Video signaling could not complete. Check the local server and network; no automatic retry.';
  return 'Video connection failed before an explanation was available. No automatic retry was started.';
}
export function connectionDiagnostic(event){
  if(!event||typeof event!=='object')return null;
  if(event.kind==='failure')return {source:'failure',label:connectionFailure(event.message)};
  const detail=event.detail||{},phase=event.phase;
  if(phase==='billing'&&detail.issue==='credit_verification_unavailable')return {source:'billing',label:'Fal could not verify session credit; this does not confirm an empty balance.'};
  if(phase==='validation'){
    const fields=new Set(['body','sdp','type','app_id','offer','audio','video','media','session_id','request']);
    const parts=typeof detail.field==='string'?detail.field.split('.'):[];
    const field=parts.length>0&&parts.length<=3&&parts.every(value=>fields.has(value))?parts.join('.'):'request';
    const issues={missing:'required field missing',literal_error:'unsupported value',string_type:'expected text',extra_forbidden:'unexpected field',value_error:'invalid value',json_invalid:'invalid JSON',invalid_value:'invalid value',media_contract:'incompatible media offer',invalid_offer:'invalid connection offer'};
    return {source:'validation',label:`${field}: ${Object.hasOwn(issues,detail.issue)?issues[detail.issue]:'validation failed'}`};
  }
  if(phase==='signaling'&&['ice','ice-fallback','session','heartbeat'].includes(detail.step)){
    const status=Number.isInteger(detail.status)&&detail.status>=100&&detail.status<=599?detail.status:0;
    return {source:'signaling',label:`${detail.step}: ${status?'HTTP '+status:'request started'}`};
  }
  if(phase==='ice-gathering'){
    const count=key=>Number.isInteger(detail[key])&&detail[key]>=0&&detail[key]<1000?detail[key]:0;
    return {source:phase,label:`Candidates: host ${count('host')}, server-reflexive ${count('srflx')}, relay ${count('relay')}`};
  }
  if(phase==='ice-servers'){
    const source=['bridge','app-fallback','empty-ice-response','app-managed; trying app fallback','bridge-unavailable; trying app fallback'].includes(detail.source)?detail.source:'ICE discovery unavailable';
    return {source:phase,label:source};
  }
  if(phase==='connection-state')return {source:phase,label:'WebRTC connection state changed'};
  return null;
}
