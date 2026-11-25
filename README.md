# Afterhours

Afterhours is a local-first, agentic DJ engine. It turns musical direction into a set plan, prepares a queue, and supervises transitions using deck readback. It exposes a local HTTP API for clients and uses VirtualDJ for audio playback and mixing.

- **Plan a set** from an available catalog, a target tempo and energy range, and measured cue candidates.
- **Run or supervise the mix** with a guarded playback queue, transition planning, and manual takeover.
- **Build a client** around session state, playback telemetry, recaps, and optional guest requests.
- **Add integrations** for planning, voice, or performance media when you supply credentials and enable them.

## Quick start

Use Python 3.11 or later on a POSIX system. Use Node.js 24 or later for the JavaScript tests. The optional native analysis bridge targets Apple Silicon Macs.

```sh
git clone https://github.com/robertschultz/afterhours.git
cd afterhours
./setup
./start-afterhours
```

The music service listens on `127.0.0.1:8765` and the performance service on `127.0.0.1:8768`. For a music-only process, run `./start-dj`. Startup leaves the decks idle. If your default `python3` is older, run `AFTERHOURS_PYTHON=/path/to/python3 ./setup`.

For live mixing on a host with VirtualDJ, follow the [OSC setup guide](docs/VDJ-BRIDGE.md). For a dry run, start Afterhours without VirtualDJ and inspect the idle session or run the test suites.

Runtime data defaults to `${XDG_DATA_HOME:-$HOME/.local/share}/afterhours`. To reuse data from the Crategent checkout, set `AFTERHOURS_DATA_ROOT` to its existing data directory. See the [upgrade notes](docs/CONFIGURATION.md#upgrading-from-crategent).

## Connect a client

```sh
curl --fail http://127.0.0.1:8765/api/set-session
curl --fail http://127.0.0.1:8765/api/program
curl --fail http://127.0.0.1:8768/api/performance
```

The music service identifies itself as `afterhours-music`. Clients obtain a local session token from `/api/config` and send it as `X-DJ-Token` for protected actions. Performance endpoints use a separate token and `X-Performance-Token`. Tokens are local process credentials. Keep the services bound to loopback.

The music adapter reports unavailable or stale deck readings when it cannot verify native state. Model planning, media generation, hardware response, and listening quality each require their own verification.

## Develop

```sh
./setup
.venv/bin/python scripts/test-python.py
npm test
```

Tests use disposable state and simulated peers. See [testing](docs/TESTING.md), [architecture](docs/AGENT-ARCHITECTURE.md), and [configuration](docs/CONFIGURATION.md).

The project is licensed under the [MIT License](LICENSE). VirtualDJ and its SDK are separate dependencies. See [release notes for maintainers](RELEASE-REVIEW.md) for the publication checklist.
