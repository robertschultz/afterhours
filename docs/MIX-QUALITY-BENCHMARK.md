# Mix quality benchmark

Evaluate how well Afterhours builds and plays a coherent one-hour set. Each run produces three results: a scorecard for the frozen plan, native execution measurements, and blind listening preferences. Compare like-for-like runs and retain each result independently.

## Freeze the set before evaluating

1. Version the brief, eligible catalog snapshot, recording analysis, planner prompt and model, preparation budget, engine, and audio settings. Include genre boundaries, intended energy curve, audience or set slot, repeat policy, and entry cadence. Enable stems in matched arms only when the same recordings can supply them.
2. Ask each planner for one complete 3,600-second set, including the finale. Seal its exact recording editions, order, catalog and audio-analysis provenance, entry time, audible end time, source cue, proposed overlap, planned tempo/key, loop and EQ intent, and reason for each choice. Validate before playback and preserve the original manifest. Record unavailable information explicitly.
3. Compare to the existing planner and at least two DJ-curated sets on the same brief and candidate pool. Keep at least one human reference fully held out from ranker and judge tuning. Track incompatible and generic-fit examples as controls. Build separate briefs for steady groove, energy build, and a requested genre change. Treat the alternating 60/30-second schedule as its own rapid-entry brief.
4. For a choice-only comparison, keep the mixer and input evidence fixed. For a blend-only comparison, lock order and audio while varying cue, loop, EQ, or stem policy. Evaluate adaptive queue changes in a separate run and record each reason and replacement alongside the actual timeline.

## Scorecard

| Dimension | Report for each run |
| --- | --- |
| Track selection | Exact versions, catalog eligibility, artist and recording repeats, disallowed genre time, and evidence for each choice. Judge fit and variety against the brief. |
| Track timing | Planned and actual entry intervals, per-track audible seconds, and overlap seconds. Report target hits, median error, and p95 error separately for each cadence. Track restarts and skips. |
| Genre and energy | Time share by genre over the full timeline, energy against each planned phase, and marked intentional pivots. Weight timelines by the lead track to avoid counting overlaps twice. Keep the energy calibration version fixed and report missing input coverage. |
| Transition feasibility | Cue range, verified phrase and beat alignment, permitted tempo move, overlap and loop readiness, harmonic intent, and available stem overlap measurements. Report vocal and bass conflicts only where the sources were independently analyzed. Show deliberate exceptions separately. |
| Native execution | Actual confirmed track and cue, verified handoff, matched beat and tempo, EQ or stem control readback, loop wrap and release, output silence and clipping, and uninterrupted set duration. |
| Listening | DJ-curated pairwise preferences separately for selection, section flow, and transition sound. Listen for phrasing, groove, competing vocals or bass, abrupt level changes, and musical payoff. Cite a timestamp on audible defects. |

Suggested starting gates for a matched 60/30 stress brief are a complete 3,600-second timeline, zero exact recording repeats unless the brief requests one, and at least 90% of entries within ±5 seconds for **each** target. Propose zero unintended gaps over 250 ms or clipping during a verified handoff. Compare against the current baseline and adjudicate musical exceptions before tightening thresholds. A musical brief without strict cadence should derive target dwell windows from the curated reference sets.

## Calibrate the judge

- Randomize candidate order and hide planner names. Match loudness for comparison copies and retain original output for clipping and level analysis. Sample opening, build, peak, release, close, and high-risk transitions using the same sampling rule across arms.
- Collect independent human labels first. Use at least two experienced listeners for the initial calibration, record ties or insufficient evidence, and adjudicate major disagreements. Start with pairwise A/B/tie plus a short timestamped reason.
- Calibrate a planning judge on the sealed manifests and a listening judge on playable comparison audio when authorized and supported. A metadata-only judge can rank the plan and abstains from claims about how it sounds. Benchmark against the human labels, reverse A/B positions on a subset, and track agreement, abstentions, and reliability by brief.
- An initial release gate for automated judgment is at least 80% agreement on held-out, non-tied human comparisons and no material preference reversal on order swaps. Report sample size and confidence intervals. Retain human judgment on disagreements and sparse groups.

[OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices) and [ITU-R BS.1534 listening methodology](https://www.itu.int/rec/R-REC-BS.1534/) inform this design. This benchmark uses its own DJ-specific rubric.

## Save evidence for improvement

Store versioned briefs, sealed plans, actual timelines, comparison IDs, reference provenance, review labels and timestamps, judge version, and failed examples. Group development, calibration, and held-out test sets by brief and recording family to reduce leakage from edits and remixes. Document why each preference was labeled before using it to tune constraints, weights, or prompts. Use authorized audio and reference sets only and retain provenance with any training export.

## Run the offline scorer

```sh
python3 scripts/evaluate-set-quality.py --plan incumbent.json \
    --plan candidate.json --plan curated.json
python3 scripts/evaluate-set-quality.py --trial /path/to/refill-deeper \
    --trial /path/to/sol-stem-hour
```

Each `--plan` file uses the following shape. Every track has a canonical recording ID, a source cue, a position on the master timeline, and a catalog genre. Optional energy is normalized from 0 to 1. Every overlap is derived from the previous audible end and current entry. This timeline format orders entries and audible ends. Use an empty cadence list for briefs whose entries are untimed. Set an unavailable genre or energy to null. An optional `energyCurve` takes sections such as `{"startSeconds": 0, "target": 0.55}` with ascending start times. Each target extends until the next section or set end.

```json
{
  "schemaVersion": 1,
  "setId": "set-001",
  "briefId": "steady-house-60-30-v1",
  "catalogSnapshotId": "catalog-v1",
  "targetSeconds": 3600,
  "cadenceSeconds": [60, 30],
  "allowedGenres": ["house", "deep house"],
  "tracks": [
    {
      "recordingId": "provider:exact-edition",
      "artist": "Artist",
      "genre": "house",
      "energy": 0.55,
      "entrySeconds": 0,
      "audibleEndSeconds": 64,
      "cueSeconds": 15
    }
  ]
}
```

Run the same scorer with `--observed ./private-run-prefix` for a browser endurance capture. It joins the owner record, browser heartbeat, native handoff, and independent master meter files. The report separates cadence misses with a late deck from misses with a deck ready by target, and counts quiet windows inside and outside the blend. A quiet window is a listening review point. Partial captures report their measured duration and remain partial.

The manifest excerpt is one row of a full set. The scorer reports timeline coverage, repeats, dwell, cadence, genre and energy. For archived live trials it reads `<prefix>-summary.json` and derives per-target timing from archived transition receipts or queue telemetry where available. It does not perform playback, download music, run the planner, or score audio by ear.

The current endurance archives are execution baselines. They lack frozen reference set plans and blind listening labels. Add curated sets and listening judgments before claiming a mix-quality improvement.

### Score a blind review export

Run `python3 scripts/evaluate-set-quality.py --judgments labels.json` against the versioned review output. Give each pair a stable comparison ID. Hold the underlying comparison IDs and reference metadata in the same evidence package so a preference can be traced back to the two original plans or clips.

```json
{
  "schemaVersion": 1,
  "judgeVersion": "blind-dj-rubric-v1/model-version",
  "comparisons": [
    {
      "comparisonId": "pair-001",
      "briefId": "steady-house-60-30-v1",
      "dimension": "transition-sound",
      "split": "held-out",
      "humanLabels": [
        {"reviewerId": "listener-1", "choice": "a"},
        {"reviewerId": "listener-2", "choice": "a"}
      ],
      "judgeChoice": "a",
      "swappedJudgeChoice": "b"
    }
  ]
}
```

Choices are `a`, `b`, `tie`, or null for an abstention. The swapped choice is in the reversed presentation's coordinates. The scorer requires a strict majority with two matching human votes, excludes human ties from the headline agreement rate, and includes judge abstentions in its denominator. It reports the count behind each rate, the Wilson 95% interval, and order-swap inconsistencies by brief and dimension. Only held-out comparisons count. Use the same recordings and instructions in both presentations.

### Freeze a prospective offline draft

Use `scripts/freeze-set-quality-plan.py` with copies of saved measured Afterhours set checkpoints and their local analysis reports. Choose one provider, one fixed tempo, the brief's allowed genres and an entry schedule before looking at the result. Supply the captured Unix time and the local recording-availability snapshot from the same evidence bundle.

```sh
PYTHONPATH=src python3 scripts/freeze-set-quality-plan.py \
    --checkpoint saved-opening.json --checkpoint saved-continuation.json \
    --reports ./saved-native-scans --availability ./saved-recording-availability.json \
    --captured-at 1700000000 --provider beatport --bpm 122 \
    --genre 'house' --genre 'tech house' --cadence 60 30 \
    --brief steady-house-60-30-v1 --out ./private-benchmark-pool
```

The tool joins each replay-verified cue to an exact recording edition and compares the saved whole-audio scan digest against its checkpoint. It excludes known provider restrictions, incomplete decoded coverage and tracks outside the fixed pitch range. It reports the generous maximum measured coverage before subtracting overlaps and how many recordings have a measured exit within five seconds of each cadence target.

When the pool supports a complete hour, the tool uses Afterhours' existing discovery ranking, cue fit, tempo validation and transition recipes to produce `plan.json` for the offline scorecard. The local baseline uses no unpinned live chart or stem caches. The last eight beats fit inside an existing measured exit window. The plan is a prospective draft and requires exact provider and deck revalidation before playback. The tool has no playback path.

Keep the checkpoint copies, report copies, availability snapshot and CLI parameters together for reproducibility. The output directory is new and private. A coverage shortfall exits with status 2 and saves `source-pool.json` and `coverage.json`. A complete draft additionally saves `plan.json`. Capped or absent provider playback evidence, scan files without saved replay-verified windows, and incomplete archival receipts cannot be used to fill missing portions.

An observed hour with 65 handoffs can supply an exact retrospective order and transition timings. Treat it as an execution control. Its order was found during playback, and older receipts lack the source cue positions needed for a prospective manifest.
