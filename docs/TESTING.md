# Testing

From the repository root:

```sh
./setup
.venv/bin/python scripts/test-python.py
npm test
```

Use Node.js 24 or later for the client fixtures. The test runner starts only disposable loopback processes and isolates state from installed services. Python tests cover the bridge, set planning, transitions, session state, and the HTTP surface. JavaScript tests cover the client contracts and browser helpers.

Pass a module name to the Python runner to run a focused suite, for example `.venv/bin/python scripts/test-python.py test_vdj_session`. Use `--list` to see available modules. For JavaScript, use `npm test -- test-music.mjs`. Tests live in `tests/python/`, `tests/js/`, and `tests/mcp/`, with shared contract data in `tests/fixtures/`.

To check the optional MCP transport in a separate virtual environment, use a Python interpreter supported by the MCP dependencies.

```sh
python3 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -e .
.venv-mcp/bin/python -m pip install --require-hashes -r requirements/requirements-mcp.txt
.venv-mcp/bin/python scripts/test-mcp.py
```

Synthetic tests establish software behavior. Native playback, hardware, provider access, and listening acceptance are separate.

To compare the tempo-and-key ranker with the readiness-aware ranker, save a disposable `/api/set-session` snapshot and run the offline preview. The recording IDs must come from that snapshot.

```sh
.venv/bin/python scripts/preview-queue.py /path/to/snapshot.json \
  --after outgoing-recording-id --insert candidate-recording-id
```

The preview reports both shortlists, verified queue runway when the snapshot contains enough evidence, and possible insertion slots. It reads the saved snapshot without contacting VirtualDJ or a provider.

### Continuous process monitoring

Use `scripts/observe-live-stack.py` during live trials. Pass process names with
`--process LABEL=EXECUTABLE`, launcher PID files with `--pidfile LABEL=FILE`
and loopback health endpoints with `--url LABEL=URL`. The default records one
sample every five seconds for three hours. Output is JSON lines with process
restarts, memory, CPU, endpoint status, deck telemetry age and relay status.
It records measurements only and leaves the running apps in place.


For a native 60/30-second entry-cadence rehearsal, open the desired catalog
in VirtualDJ and prepare a compatible queue. Use `scripts/verify-timed-set.py`
to observe an active set, or pass `--start --prepare` to start the selected queue.
It records confirmed transitions, beat phase, loop release and mixer readbacks.

Supply `--provider apple-music` to require Apple Music for every heard recording
or `--provider-sequence beatport,apple-music,beatport,beatport,apple-music`
to validate a mixed opening. Provider checks use the exact recordings confirmed
by the handoffs. The verifier leaves the set playing after observation.
