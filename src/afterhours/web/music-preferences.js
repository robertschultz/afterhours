export const PREFERENCES_KEY='afterhours.engine.music-preferences.v1';
export const DEFAULT_PREFERENCES=Object.freeze({bpmMin:40,bpmMax:300,energyCeiling:.95,preferredGenre:'',maxTempoShift:8,allowRepeats:true});
export function cleanPreferences(value={}) {
  const p={...DEFAULT_PREFERENCES,...value};
  if(!Number.isFinite(p.bpmMin)||!Number.isFinite(p.bpmMax)||p.bpmMin<40||p.bpmMax>300||p.bpmMin>p.bpmMax)throw new Error('Choose a tempo range from 40 to 300 BPM, with minimum below maximum.');
  if(!Number.isFinite(p.energyCeiling)||p.energyCeiling<.2||p.energyCeiling>.95)throw new Error('Choose an energy ceiling from 20–95%.');
  if(!Number.isFinite(p.maxTempoShift)||p.maxTempoShift<0||p.maxTempoShift>8)throw new Error('Choose a tempo adjustment limit from 0–8%.');
  if(typeof p.preferredGenre!=='string'||p.preferredGenre.length>80||typeof p.allowRepeats!=='boolean')throw new Error('Check the genre and repeat preferences.');
  return {bpmMin:p.bpmMin,bpmMax:p.bpmMax,energyCeiling:p.energyCeiling,preferredGenre:p.preferredGenre.trim(),maxTempoShift:p.maxTempoShift,allowRepeats:p.allowRepeats};
}
export function readPreferences(storage) {
  const raw=storage.getItem(PREFERENCES_KEY);
  if(!raw)return {...DEFAULT_PREFERENCES};
  if(raw.length>4000)throw new Error('Saved music preferences could not be read.');
  return cleanPreferences(JSON.parse(raw));
}
export function fitsPreferences(track,p) {
  return Number.isFinite(track.bpm)&&track.bpm>=p.bpmMin&&track.bpm<=p.bpmMax&&(!Number.isFinite(track.energy)||track.energy<=p.energyCeiling);
}
export function genrePenalty(track,p) {
  return p.preferredGenre&&!`${track.genre||''} ${track.metadata?.genre||''}`.toLowerCase().includes(p.preferredGenre.toLowerCase())?10:0;
}
