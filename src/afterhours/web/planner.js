// Musical decisions are explainable local scoring. A connected client can propose plan changes.
import {cleanPreferences,fitsPreferences,genrePenalty} from './music-preferences.js';
export const clamp = (x, low, high) => Math.max(low, Math.min(high, x));

export function harmonicDistance(a, b) {
  const parse = s => /^(\d{1,2})([AB])$/.exec(s || '');
  const x = parse(a), y = parse(b);
  if (!x || !y) return 2;
  const n = Math.abs(Number(x[1]) - Number(y[1]));
  return Math.min(n, 12 - n) + (x[2] !== y[2] ? 1 : 0);
}

export function rankTracks(tracks, current, targetEnergy, played = [],preferences) {
  const p=cleanPreferences(preferences);targetEnergy=Math.min(targetEnergy,p.energyCeiling);
  const others = tracks.filter(t => t.id !== current?.id && !t.error&&fitsPreferences(t,p));
  const fresh = others.filter(t => !played.includes(t.id));
  const pool = fresh.length ? fresh : p.allowRepeats?others:[];
  return pool.map(track => {
    const tempoGap = current ? Math.abs(track.bpm - current.bpm) / current.bpm : 0;
    const keyGap = current ? harmonicDistance(track.camelot, current.camelot) : 0;
    const energyGap = Math.abs(track.energy - targetEnergy);
    const score = 100 - tempoGap * 170 - keyGap * 7 - energyGap * 55-genrePenalty(track,p);
    const tempoText = current ? `${Math.abs(track.bpm-current.bpm).toFixed(0)} BPM apart` : `${track.bpm.toFixed(0)} BPM opening`;
    const keyText = current ? keyGap <= 1 ? 'neighboring musical key' : 'a contrasting key' : track.key;
    const reason = `${tempoText}, ${keyText}, and ${Math.round(track.energy*100)}% energy for your ${Math.round(targetEnergy*100)}% target.`;
    return {track, score, reason};
  }).sort((a,b) => b.score-a.score || a.track.id.localeCompare(b.track.id));
}

export function transitionPlan({now, position, duration, bpm, rate=1, beatOffset=0, incomingBpm, quick=false,maxTempoShift=8}) {
  const beat = 60 / (bpm * rate);
  const bar = beat * 4;
  const remaining = Math.max(0, (duration-position)/rate);
  const lead = Math.min(.18, Math.max(.005, remaining*.1));
  const fade = Math.min(beat*(quick ? 16 : 32), Math.max(.01, remaining-lead-.01));
  const anchor = now - (position-beatOffset)/rate;
  const earliest = now+lead;
  let desired = quick ? earliest : Math.max(earliest, now+remaining-fade-.15);
  // Automatic transitions start at an eight-bar boundary when possible.
  const grid = quick ? bar : bar*8;
  let start = anchor + (quick ? Math.ceil((desired-anchor)/grid) : Math.floor((desired-anchor)/grid))*grid;
  if (start < earliest) start = anchor+Math.ceil((earliest-anchor)/bar)*bar;
  if (start+fade > now+remaining) start = Math.max(earliest, now+remaining-fade-.03);
  const actualFade = Math.min(fade, Math.max(.01, now+remaining-start-.01));
  const ratio = bpm*rate/incomingBpm;
  const limit=clamp(maxTempoShift,0,8)/100;
  const matched = ratio >= 1-limit && ratio <= 1+limit;
  return {start, duration:actualFade, rate:matched ? ratio : 1, matched,
    beats:Math.round(actualFade/beat), pitchSemitones:matched ? 12*Math.log2(ratio) : 0};
}

export function fadeCurve(incoming, size=256) {
  return Float32Array.from({length:size}, (_,i) => incoming ? Math.sin(i/(size-1)*Math.PI/2) : Math.cos(i/(size-1)*Math.PI/2));
}
