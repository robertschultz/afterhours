# Record identity and metadata

Catalog records carry a stable exact-recording identifier and any available title, artist, release, tempo, key, duration, or artwork metadata. Missing fields remain unknown. A similarly named recording is not an interchangeable replacement.

The planner uses bounded preferences and measured cue candidates. An ordered list identifies intended recordings, and a native load must confirm the exact deck occupant before adding verified playback or cue evidence. Local libraries and provider metadata remain in the configured data directory.

Client catalog requests use `/api/catalog-crate` with the local session token. Pagination and filters limit response size. Reading a page does not grant playback or authorize a provider search.
