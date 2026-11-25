# Configuration

See the [project README](../README.md) for setup. The music service owns the catalog, session queue, transition plan, and VirtualDJ controls. The performance service owns optional visuals. The crowd listener can be enabled separately for guest requests.

## Configuration

| Setting | Behavior |
| --- | --- |
| `AFTERHOURS_DATA_ROOT` | Local data and runtime workspace. Defaults to `${XDG_DATA_HOME:-$HOME/.local/share}/afterhours`. |
| `AFTERHOURS_PYTHON` | Interpreter override for setup and the launchers. An override used to launch must have Afterhours installed. |
| `AFTERHOURS_ANALYSIS_REPORTS_DIR` | Optional directory for native feature reports. Defaults to `.runtime/audio-analysis` under the data root. |
| `AFTERHOURS_TRACK_SECONDS` | Default seconds between track entries for new queues. Accepts 15–300 and defaults to 30. |
| `AFTERHOURS_CURATION_MINUTES` | Default ongoing curation allowance. Accepts 1–360 and defaults to 30. The decision and cost ceilings still apply. |
| `AFTERHOURS_PLANNER_MODEL` | Optional OpenAI text planner model override. The model must support the existing structured output and tool contracts. |
| `AFTERHOURS_PLANNER_INPUT_USD_PER_MILLION` | Required with a model override. Set the highest applicable input rate, including cache writes. |
| `AFTERHOURS_PLANNER_OUTPUT_USD_PER_MILLION` | Required with a model override. Set the applicable output rate, including reasoning tokens. |
| `OPENAI_API_KEY` | Optional planning and voice credential. Live use requires an explicit session allowance. |
| `FAL_KEY` | Optional provider credential for performance media. Submission limits default to zero. |

Create a dedicated data directory for a new workspace. Configure provider credentials in the local process environment. The planning service also reads its OpenAI key from `.env.local` in the data directory. Keep secrets and runtime outputs outside the source repository.

An override must fit the existing approved request and run budgets. Verify the model’s rates before configuring them.

## Upgrading from Crategent

Clones of the original Afterhours repository should use the [Afterhours Old repository](https://github.com/robertschultz/afterhours-old) as their remote.

- Point `AFTERHOURS_DATA_ROOT` at your previous data directory to reuse local catalogs, preferences, saved sets, and run history. The previous default was `${XDG_DATA_HOME:-$HOME/.local/share}/crategent`. Older checkouts could use their `engine/` directory.
- Switch launch scripts to `./start-afterhours` and update any `CRATEGENT_*` environment variables to `AFTERHOURS_*`. Update clients that check the service identity or call the optional MCP tools. The new service names are `afterhours-music` and `afterhours-performance`, and the MCP tools are `afterhours_state`, `afterhours_direct`, and `afterhours_receipt`.
- Recompute cue and tempo previews created before the rename. Browser drafts and guest invite sessions use a new storage namespace.
- Rebuild and install the **Afterhours Analysis** VirtualDJ bundle before using native scans. Set `AFTERHOURS_ANALYSIS_REPORTS_DIR` before building if you use a separate report directory.

## Optional integrations

- [VirtualDJ OSC](VDJ-BRIDGE.md) provides native playback and mixer controls.
- [Native analysis](../native/analysis-bridge/README.md) measures decoded audio features on supported VirtualDJ hosts.
- To build browser provider assets, run `pnpm install --frozen-lockfile && pnpm build` from the repository root. The default music and performance servers start without this optional bundle.
- For video continuation, run `.venv/bin/python -m afterhours.performance_server` with a positive `--approved-h3-jobs` and a user-provided `--h3-seed-url`. The URL must refer to media on the configured provider that the operator is entitled to use. Live performer sessions require approved references supplied by the operator.

The bundled posters and silent clips are synthetic fixtures. They do not supply performer identity or consent.
