# Agent architecture

The agent observes the current session, proposes a musical action, and passes it through the runtime's validators. The executor owns beat timing and native deck changes. It records acknowledgements against exact recording identities.

The Python runtime lives in [`src/afterhours/`](../src/afterhours). Both HTTP services load their bundled browser assets from the package. The SDK-dependent native bridge lives under [`native/`](../native), and the development suites and fixtures live under [`tests/`](../tests).

| Layer | Modules in `src/afterhours/` |
| --- | --- |
| Brief and set interpretation | `prompt_plan.py`, `dj_settings.py`, `set_plan.py` |
| Agent decisions and queue curation | `dj_agent.py`, `agent_loop.py`, `session_curation.py`, `queue_planning.py` |
| Cue and transition planning | `cue_planner.py`, `tempo_planner.py`, `vdj_mixing.py` |
| Native observation and execution | `vdj_session.py`, `dj_bridge.py`, `vdj_controls.py` |
| Session recovery and recaps | `session_recovery.py`, `run_archive.py` |
| HTTP and optional agent integration | `server.py`, `mcp_control.py`, `mcp_stdio.py` |

A model proposes decisions at musical boundaries. The execution loop checks that the observation and recording identity are still valid, bounds permitted actions, and verifies native readback. Model responses do not drive the beat clock.

Starting a process leaves playback idle. Operator approval and session budgets govern actions that affect playback or incur provider costs. Status distinguishes a proposal, dispatch, verified result, and uncertain outcome. An uncertain native command is never replayed automatically.

For a request to add one supplied recording, the planner delegates placement to the engine. The engine ranks viable slots and applies the result through the same queue and native preparation guards. Automatic lookup and decision work uses the remaining measured queue runway when a fresh deck clock is available.


## Optional music research and live advice

With an existing OpenAI credential, run a public catalog research job before a set.

```sh
.venv/bin/python -m afterhours.set_research --style house --anchor "Dom Dolla" \
  --brief "Follow Define with a gradual tempo lift and a mix of familiar and deeper house." --save
```

This explicit job uses the OpenAI Agents API and can incur model and search charges.
It returns track pages and search terms. The local hints expire after 24 hours and
apply only when the style and recent artist match. Search hints enter the same native
catalog lookup and preparation path as other prospects. VirtualDJ verifies the
exact recording, then Afterhours checks tempo and measured cue compatibility.
Omit `--save` to inspect the shortlist without providing hints to the booth.

For an opt-in comparison, start the engine with
`AFTERHOURS_ROLLING_ADVISOR=gpt-6.2-sol`. During an active rolling set, Sol can
rank a short list of eligible records when preparation has time to spare. If a
result misses the deadline, local ranking continues. This setting leaves the
voice model and the wider set-planning model on their configured routes.
Set `AFTERHOURS_ROLLING_ADVISOR_LOG_RECEIPTS=1` for a benchmark run to log
up to 16 shortlist receipts in the local engine diagnostic log. The receipts
distinguish a ranking from queue acceptance. Compare them with native handoff
receipts to identify what actually played.

While a measured set is paused, the protected `prepare-stem-windows` set
action can profile up to four exact track IDs on an isolated spare deck.
It stores time-local vocal, bass, percussion and instrument envelopes.
That evidence can rank future selections and guide cue choice and overlap
length. VirtualDJ has to expose ready Stems 2.0 for the recording. When
stem measurements are absent, Afterhours uses measured full-mix features and EQ.
