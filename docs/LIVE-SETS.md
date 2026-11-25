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
