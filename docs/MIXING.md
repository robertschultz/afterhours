# Mixing controller

The deterministic mixing worker uses native deck state, exact recording identity, and measured timing. Model proposals and catalog metadata inform planning, while the worker owns the actual handoff.

1. Verify the outgoing deck, a usable incoming cue, and the intended tempo and overlap.
2. Prepare the incoming deck while it is paused and confirm its recording.
3. Apply the selected crossfade and, where supported, bounded EQ and channel-level ramps.
4. Verify the handoff before releasing the outgoing deck. Restore only mixer values owned by the current transition.

Tempo, key, phrasing, and energy are different signals. An unavailable reading narrows what the engine can plan. A successful handoff establishes execution. Listen separately to judge the musical result.

See [VirtualDJ's native control reference](https://virtualdj.com/manuals/virtualdj/appendix/vdjscriptverbs.html).
