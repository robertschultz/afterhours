# Live sets

1. Confirm that the OSC adapter can read the expected VirtualDJ decks.
2. Give the agent a musical brief, catalog, and limits for the session.
3. Inspect the prepared order, timing, exact recording identities, and any cues still awaiting verification.
4. Start the set explicitly from a client. Use manual takeover to release scheduled decisions before operating the decks directly.
5. Use the session and program endpoints to monitor verified playback. Listen to confirm that the transitions work on the intended audio system.

A saved set can be loaded for review without starting playback. A failed or uncertain transport response calls for a fresh state check before another action.

## Start a new queue or play the selected one

**Play this queue** keeps the selected order. Use **Start a fresh queue** to plan and play a new set from the saved crate after playback has stopped. Afterhours favors different openers from the most recent sets when the recordings fit the new brief. The local provider must be configured and the client must approve the bounded planning request.

Clients can send `POST /api/dj-agent` with the local `X-DJ-Token` and a unique message ID.

```json
{
  "action": "start-new-queue",
  "messageId": "fresh-house-001",
  "text": "Build a warm, deep house set with a steady rise in energy",
  "approveRequest": true
}
```

Omit `text` to use the saved crate without a new musical brief. Clients can separately set `approvePreparation` and `approveSessionCuration` to request the existing bounded review and ongoing-curation flows.

## Fit a track into the set

Ask the DJ to add a specific recording to the current set. The planner can request one exact catalog ID through `trackInsertion`. The engine checks viable slots in the current or staged order using tempo, harmonic fit, and available measured cue levels. It preserves existing track order, locked positions, and the playing and cued prefix. Live additions enter the existing preparation workflow.

Clients can preview or request the same placement through `POST /api/set-session` with the local token. Use `action: "preview-insertion"` to see the top three feasible slots without changing state, or `action: "insert-track"` to request the highest-ranked slot. Both take `trackId`. Slot indexes are zero-based. Transition costs are relative estimates, with lower values preferred.

The DJ agent status exposes `queueRunwaySeconds` for measured, committed playback and `curationSecondsRemaining` for the active curation allowance. Runway can be unavailable while preparing, mixing, or waiting for fresh deck data. The allowance controls queue planning. The last prepared track follows its planned ending.

Use `POST /api/dj-agent` with `action: "track-feedback"`, `trackId`, and `preference: "prefer"` or `"avoid"` to save an operator preference for the current DJ profile. An optional `reason` is included as planning evidence. Use `"clear"` to remove it.

## Timed entries and loop handoffs

For a measured phrase-mixing set, clients can send this start request to
`POST /api/set-session` using the local token:

```json
{
  "action": "start",
  "prepareWhilePlaying": true,
  "rollingQueue": true,
  "entryCadence": [60, 30],
  "loopHandoffs": true
}
```

`entryCadence` repeats the requested number of seconds between track entries.
Afterhours chooses the nearest replay-verified exit the decks can still reach
safely. The actual interval depends on cue spacing and preparation time. It
selects blend length separately from the entry interval, using verified cue
capacity, key compatibility and measured band levels. Rolling playback
keeps room for a later verified exit if preparation misses the target.

With `loopHandoffs` enabled, Afterhours verifies the outgoing beat loop before
starting the synced incoming deck. The incoming deck supplies the clock for
the blend while the outgoing playhead wraps. Afterhours reads back its bass,
lead and high-band controls, then pauses the outgoing deck and verifies release
of the loop it owns.

After choosing at least two recordings in Afterhours, run
`python3 scripts/verify-timed-set.py --start --seconds 3600` from the checkout.
It starts native playback and reports entry spacing, tempo and phase agreement,
loop wraps and release, and mixer readbacks in JSON lines. Run it without
`--start` to observe a set from its beginning that has already been started
through the API. Add `--prepare` to verify and cue the initial buffer while
paused before the clock starts. This validates longer runs with an opening
queue ready while new records are being found and measured. The script leaves
playback running and records no audio.


### Cue-driven pacing

For phrase mixing, send `"cueDriven": true` with `action: "start"` and
omit `entryCadence`. The same option on `prepare-analysis` readies a
local measured opening while playback is paused. During playback, measured
section changes, overlap fit, and nearby native markers help choose the exit of each recording. The engine uses replay-verified native
beat positions, preserves the entry already playing, and keeps enough
measured audio and tempo-settle time for the blend. Track intervals can
vary. The handoff receipt records the selected exit, measured level and
bass changes, and how they fit the incoming recording. Phrase confidence
remains estimated from the audio features. Use `rollingQueue` and
`loopHandoffs` with this mode for a sustained set with outgoing loops.
