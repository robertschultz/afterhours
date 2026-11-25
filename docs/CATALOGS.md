# Catalog access

The engine works with local music and with catalogs available to VirtualDJ. Sign into a streaming service inside the native application where required. The engine does not obtain a provider account, download protected tracks, or expose provider locators to clients.

A catalog record identifies a candidate. Native load and analysis establish whether it can play and which cues are usable. The agent keeps these states separate and can replace a candidate that fails verification.

Use `./dj catalog-status` to inspect the selected catalog. See [VirtualDJ setup](VDJ-BRIDGE.md) and [record identity and metadata](CATALOG-METADATA.md).
