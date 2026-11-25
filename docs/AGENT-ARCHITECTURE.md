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
