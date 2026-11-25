// Receiver for explicit operator directions. Parent origin/source checks belong
// to the embedding listener. A run-bound command can be consumed only once.
export class DirectorOperatorDirection {
  constructor({context,direct,emit,now=()=>Date.now()}){
    Object.assign(this,{context,direct,emit,now});this.commands=new Map();this.holdUntil=0;
  }
  receipt(c,status){if(c.status===status)return;c.status=status;
    this.emit({type:'afterhours:performance-receipt',id:c.id,ownerId:c.ownerId,runId:c.runId,status});}
  receive(data){
    if(data?.type!=='afterhours:performance-direction'||typeof data.id!=='string'||
        !/^[a-f0-9]{32}$/.test(data.id)||typeof data.ownerId!=='string'||data.ownerId.length>64)return false;
    if(this.commands.has(data.id))return false;
    const c={id:data.id,ownerId:data.ownerId,runId:data.runId},state=this.context();
    this.commands.set(c.id,c);
    if(this.commands.size>100)this.commands.delete(this.commands.keys().next().value);
    if(!state.ready||state.runId!==data.runId||!Number.isFinite(data.expiresAt)||data.expiresAt*1000<=this.now()||
       data.expiresAt*1000>this.now()+60000||typeof data.prompt!=='string'||!data.prompt.trim()||data.prompt.length>800){
      this.receipt(c,'unavailable');return false;
    }
    try{
      // Hold automatic musical prompt changes briefly so they cannot immediately
      // replace the operator's gesture. This is not a frame/beat timing promise.
      this.holdUntil=this.now()+12000;
      c.version=this.direct(data.prompt);this.receipt(c,'sent');return true;
    }catch{this.receipt(c,'unavailable');return false;}
  }
  observe(state){
    for(const c of this.commands.values()){
      if(['applied','rejected','unavailable'].includes(c.status))continue;
      if(state.phase==='stopped'){this.receipt(c,'unavailable');continue;}
      const ack=state.promptAck;
      if(ack?.version===c.version&&ack.status==='prompt_applied')this.receipt(c,'applied');
      else if(ack?.version===c.version&&ack.status==='prompt_rejected')this.receipt(c,'rejected');
    }
  }
}
