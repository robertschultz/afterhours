"""Make four original, deterministic house sketches. No samples or downloads."""
import json
import wave
import numpy as np

from afterhours.paths import data_root

ROOT = data_root() / "web" / "audio"
RATE = 22050
TRACKS = [
    ("blue-hour", "Blue Hour", 120, 57, "A minor", "8A", 0.42, "Deep house"),
    ("night-bus", "Night Bus", 122, 64, "E minor", "9A", 0.58, "Melodic house"),
    ("open-air", "Open Air", 124, 67, "G major", "9B", 0.73, "Piano house"),
    ("last-light", "Last Light", 126, 62, "D minor", "7A", 0.86, "Warehouse house"),
]


def synth(index, bpm, root, energy):
    rng = np.random.default_rng(281 + index)
    beat = 60 / bpm
    duration = 128 * beat
    out = np.zeros((int(duration * RATE), 2), dtype=np.float64)

    def add(signal, at, amp=1, pan=0):
        pos = round(at * RATE)
        n = min(len(signal), len(out) - pos)
        if n <= 0:
            return
        left, right = np.sqrt((1 - pan) / 2), np.sqrt((1 + pan) / 2)
        out[pos:pos+n, 0] += signal[:n] * amp * left
        out[pos:pos+n, 1] += signal[:n] * amp * right

    def tone(note, length, flavor="pluck"):
        t = np.arange(int(length * RATE)) / RATE
        f = 440 * 2 ** ((note - 69) / 12)
        if flavor == "bass":
            sig = np.sin(2*np.pi*f*t) + .22*np.sin(4*np.pi*f*t) + .08*np.sin(6*np.pi*f*t)
            env = (1-np.exp(-t*180)) * np.exp(-t*5)
        else:
            sig = sum(np.sin(2*np.pi*f*k*t) / k**1.7 for k in range(1, 5))
            env = (1-np.exp(-t*350)) * np.exp(-t*(5 if flavor == "pluck" else 2.5))
        return sig * env * np.minimum(1, (length-t)*35)

    kt = np.arange(int(.4 * RATE)) / RATE
    kick = np.sin(2*np.pi*(48*kt + (135-48)*.022*(1-np.exp(-kt/.022)))) * np.exp(-kt*11)
    kick += rng.normal(0, 1, len(kt)) * np.exp(-kt*240)*.13
    ht = np.arange(int(.13*RATE))/RATE
    noise = rng.normal(0, 1, len(ht))
    hat = np.concatenate([[0], np.diff(noise)]) * np.exp(-ht*50)
    ct = np.arange(int(.20*RATE))/RATE
    clap = rng.normal(0, 1, len(ct)) * np.exp(-ct*23) * (np.sin(ct*430)**2)
    minor = index != 2
    degrees = [0, 3 if minor else 4, 7, 10 if minor else 11]
    progression = [0, -2, -5, -3] if minor else [0, -5, -3, -7]
    for b in range(128):
        bar = b//4
        chord = progression[(bar//4)%4]
        breakdown = 16 <= bar < 20
        intensity = .65 if bar < 4 or bar >= 28 else 1
        if not breakdown or b % 4 == 0:
            add(kick, b*beat, .78*intensity)
        if b % 2 == 1:
            add(clap, b*beat, .13*intensity)
        add(hat, (b+.5)*beat, (.055+.025*energy)*intensity, (-1)**b*.3)
        if energy > .65 and b%2:
            add(hat, (b+.75)*beat, .024, .4)
        if 4 <= bar < 28:
            bass_note = root-24+chord
            add(tone(bass_note, beat*.8, "bass"), (b+.5)*beat, .30)
            if b%4 == 3:
                add(tone(bass_note+7, beat*.4, "bass"), (b+.875)*beat, .13)
        if 4 <= bar < 28 and b%2 == 0:
            for d in degrees[:3]:
                chord_sound = tone(root+chord+d, beat*1.6, "chord")
                add(chord_sound, (b+.25)*beat, .095, (d-3)/12)
                add(chord_sound, (b+1)*beat, .022, -(d-3)/12)
        if 8 <= bar < 28:
            seq = [0, 7, 12, degrees[1]+12, 7, degrees[1], 10 if minor else 11, 7]
            note = root+12+chord+seq[(b+index*2)%len(seq)]
            snd = tone(note, beat*1.3)
            add(snd, (b+.75)*beat, .075+.025*energy, -.25)
            add(snd, (b+1.5)*beat, .027, .5)
    # A short release prevents a discontinuity at the file boundary.
    n = int(.012*RATE)
    out[:n] *= np.linspace(0, 1, n)[:, None]
    out[-n:] *= np.linspace(1, 0, n)[:, None]
    out = np.tanh(out*1.15)
    out *= .82 / max(np.max(np.abs(out)), .001)
    return out.astype(np.float32)


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, (slug, title, bpm, root, key, camelot, energy, genre) in enumerate(TRACKS):
        audio = synth(i, bpm, root, energy)
        with wave.open(str(ROOT / f"{slug}.wav"), "wb") as f:
            f.setnchannels(2); f.setsampwidth(2); f.setframerate(RATE)
            f.writeframes((audio*32767).astype("<i2").tobytes())
        manifest.append(dict(id=slug, title=title, artist="Afterhours Originals", bpm=bpm,
            key=key, camelot=camelot, energy=energy, genre=genre, beatOffset=0,
            duration=len(audio)/RATE, source="Original demo", url=f"/audio/{slug}.wav"))
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    print(json.dumps({"tracks":len(manifest), "seconds":round(sum(t['duration'] for t in manifest)),
                      "sample_rate":RATE, "source":"original synthesis; no third-party recordings"}))


if __name__ == "__main__":
    main()
