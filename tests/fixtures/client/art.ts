/**
 * Generated sleeve art. Every track gets a quiet two-colour palette and one simple shape, so sleeves are easy to tell
 * apart without real covers. Used by: header now-playing chip, Control record labels and Copilot's pick sleeve,
 * Now Playing sleeve, Room Display sleeve + glow, Catalog sleeves and crate spines.
 *
 * Production: if the library has real artwork for a track, use it for the sleeve and keep the generated palette for
 * the record label, the spine and the room glow. Never generate imitations of real covers.
 */
export const PALETTE: ReadonlyArray<readonly [string, string]> = [
  ['#22324A', '#E9DCC4'], ['#1E4A43', '#EFC35A'], ['#5E4467', '#F1E2D6'], ['#13293D', '#86BBC2'], ['#3B2B2D', '#D7A675'], ['#2E3B2C', '#C9D6A3'],
  ['#4A2F4D', '#E7B4A8'], ['#1C1C24', '#B9B4E8'], ['#6E5A3E', '#F3EBDD'], ['#28445C', '#F2C2A8'], ['#3F3F46', '#E4E4DD'], ['#1F3A3D', '#E6C8E0'],
];
export const SHAPES = ['circle', 'split', 'stripes', 'rings', 'diagonal'] as const;
export type Shape = (typeof SHAPES)[number];

/** Hand-picked palette/shape for the demo tracks (keyed by seed) so neighbouring sleeves never look alike. */
export const DEMO_ART: Record<number, readonly [number, number]> = {
  53: [9, 0], 61: [7, 4], 83: [1, 0], 71: [0, 2], 101: [6, 1], 113: [4, 3], 97: [8, 0], 109: [5, 4], 103: [3, 2], 31: [10, 1],
  127: [11, 0], 107: [2, 3], 131: [9, 2], 137: [3, 1], 139: [1, 4], 149: [5, 0], 7: [11, 4], 19: [2, 0], 43: [8, 2],
};

export function hash01(seed: number, i: number): number {
  const x = Math.sin(seed * 12.9898 + i * 78.233) * 43758.5453;
  return x - Math.floor(x);
}

export interface Art { base: string; accent: string; shape: Shape }

/** Stable art for a track. In production, seed = a stable hash of the track id. */
export function artFor(seed: number): Art {
  const m = DEMO_ART[seed];
  const p = PALETTE[m ? m[0] : Math.floor(hash01(seed, 2) * PALETTE.length)];
  const shape = SHAPES[m ? m[1] : Math.floor(hash01(seed, 1) * SHAPES.length)];
  return { base: p[0], accent: p[1], shape };
}

/** CSS background for a square sleeve. */
export function sleeveCss(seed: number): string {
  const { base: a, accent: b, shape } = artFor(seed);
  switch (shape) {
    case 'circle': return `radial-gradient(circle at 62% 58%, ${b} 0 31%, rgba(0,0,0,0) 31.6%), linear-gradient(${a}, ${a})`;
    case 'split': return `radial-gradient(circle at 30% 58%, ${a} 0 12%, rgba(0,0,0,0) 12.6%), linear-gradient(180deg, ${a} 0 58%, ${b} 58% 100%)`;
    case 'stripes': return `repeating-linear-gradient(90deg, ${b} 0 8%, ${a} 8% 20%)`;
    case 'rings': return `radial-gradient(circle at 50% 50%, ${b} 0 6%, rgba(0,0,0,0) 6.5%), repeating-radial-gradient(circle at 50% 50%, ${a} 0 9%, ${b} 9% 11%)`;
    case 'diagonal': return `radial-gradient(circle at 72% 28%, ${b} 0 9%, rgba(0,0,0,0) 9.6%), linear-gradient(135deg, ${a} 0 50%, ${b} 50% 100%)`;
  }
}

/** A record's spine in the crate: the base colour with a 3px accent cap. */
export function spineCss(seed: number): string {
  const { base, accent } = artFor(seed);
  return `linear-gradient(to bottom, ${accent} 0 3px, ${base} 3px)`;
}

/** The centre label of the spinning record uses the accent colour. */
export function recordLabel(seed: number): string { return artFor(seed).accent; }

/** Room glow behind the sleeve: accent at 22% on dark, base at 10% on light. */
export function roomGlow(seed: number, theme: 'light' | 'dark'): string {
  const { base, accent } = artFor(seed);
  const hex = theme === 'dark' ? accent : base, a = theme === 'dark' ? 0.22 : 0.10;
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/** The header's now-playing chip, on every view: 24px sleeve with the record peeking out (spinning), title, artist, time left. */
export function nowChip(seed: number, title: string, artist: string, leftSec: number) {
  const s = Math.max(0, Math.round(leftSec));
  return { sleeve: sleeveCss(seed), label: recordLabel(seed), title, artist, left: `${Math.floor(s / 60)}:${s % 60 < 10 ? '0' : ''}${s % 60}` };
}
