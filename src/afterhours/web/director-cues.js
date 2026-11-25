import {presentProgram} from './broadcast-data.js';
export function characterDirection(_character,anchor=''){
  const identity=anchor.slice(0,1000)||'Preserve the exact identity, clothing and realistic appearance of the DJ in the approved reference.';
  return `${identity} Photoreal continuous DJ performance in the same booth. Locked medium camera, unchanged lighting, both hands anatomically natural on the mixer. Subtle breathing and blinking, relaxed shoulders, restrained head movement. Keep a believable human presence, no exaggerated dance, no camera cuts, no speech or singing. Movements and expression must transition naturally.`;
}
export const REACTIONS={
  look:{label:'Look toward the room',prompt:'Glance up from the mixer toward the audience, meet their gaze briefly with a small warm smile, then return attention to the decks. One natural acknowledgement, no waving.'},
  focus:{label:'Focus on the mix',prompt:'Listen closely with a focused, calm expression. Look down at the mixer, make one small deliberate EQ adjustment and settle the hand. Keep body movement restrained.'},
  lift:{label:'Enjoy the groove',prompt:'Let the returning groove bring a restrained smile and a subtle shoulder release. Give a small relaxed nod to the pulse while keeping the hands near the controls. No exaggerated celebration.'},
};
// No raw audio, conversation, camera frames, or invented audience observations.
export class MusicDirector {
  constructor({now=()=>Date.now()}={}){this.now=now;this.reset();}
  reset(){this.lastSent=-Infinity;this.lastSignature=null;this.lastTrack=null;this.lastEnergy=null;this.pending=null;}
  manual(kind){const reaction=REACTIONS[kind];if(reaction)this.pending={...reaction,source:'operator',signature:'manual:'+kind+':'+this.now()};}
  next(program,{bufferSeconds=0}={}){
    const now=this.now();if(now-this.lastSent<(this.pending?1000:8000))return null;
    let cue=this.pending;
    if(!cue){
      const p=presentProgram(program,now);
      if(!p.fresh||!p.playing||!p.bpm){
        if(this.lastSignature==='idle')return null;
        this.lastSent=now;this.lastSignature='idle';this.lastTrack=null;this.lastEnergy=null;
        return {source:'music',signature:'idle',label:'Wait with the decks',prompt:'The music is paused or its timing is unavailable. Wait calmly at the decks, breathing and blinking naturally. Relax your shoulders, keep your hands settled, and do not mime a beat or imply audience reactions.'};
      }
      const track=p.track?.id||program.current,lead=Math.min(18,Math.max(6,Number(bufferSeconds)||0)+4);
      const transition=p.remaining!==null&&p.remaining<=lead;
      const changed=this.lastTrack!==null&&track!==this.lastTrack;
      const lift=this.lastEnergy!==null&&p.energyDb!==null&&p.energyDb-this.lastEnergy>3;
      const kind=transition?'focus':changed||lift?'lift':Math.floor((p.position||0)/12)%3===1?'look':'groove';
      const signature=`${track}:${kind}:${kind==='groove'?Math.floor((p.position||0)/16):''}`;
      if(signature===this.lastSignature)return null;
      const base=kind==='groove'?{label:'Listen to the groove',prompt:'Stay engaged with the music, gaze between the mixer and the room, subtle relaxed nodding. Keep hands settled; avoid repetitive pantomime.'}:REACTIONS[kind];
      cue={...base,source:'music',signature,prompt:`${base.prompt} The music is at ${p.bpm.toFixed(1)} BPM. ${transition?'A transition is approaching; attend to the mixer before it arrives.':changed?'A new track has just taken over.':'Use an unhurried musical pulse, not a nod on every beat.'}`};
      this.lastTrack=track;this.lastEnergy=p.energyDb;
    }
    this.pending=null;this.lastSent=now;this.lastSignature=cue.signature;return cue;
  }
}
