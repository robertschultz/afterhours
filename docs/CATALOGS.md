# Catalog access

The engine works with local music and with catalogs available to VirtualDJ. Sign into a streaming service inside the native application where required. The engine does not obtain a provider account, download protected tracks, or expose provider locators to clients.

A catalog record identifies a candidate. Native load and analysis establish whether it can play and which cues are usable. The agent keeps these states separate and can replace a candidate that fails verification.

Use `./dj catalog-status` to inspect the selected catalog. See [VirtualDJ setup](VDJ-BRIDGE.md) and [record identity and metadata](CATALOG-METADATA.md).

### VirtualDJ music sources

VirtualDJ manages account access and playback for Beatport and Apple Music.
Select the source in VirtualDJ before searching or preparing a set. Afterhours
keeps recording identities tied to the exact native catalog entry. Its verified
transport and cue APIs are shared by both sources.

Clients can read `GET /api/set-session?view=live` for the queue, deck readings
and current mix state. When Afterhours has artwork for an exact recording, it
supplies an opaque `artworkPath` in the live state and program. Fetch it from
the local Afterhours service for an image. For a client served from a separate
local origin, forward that path through the client’s server. The same contract
covers both catalogs.
