"""Read-only upcoming Automix metadata; no browser focus or transport changes.

Offsets are relative to the currently playing Automix item, not list indices.
The documented getter is only useful while Automix is active. This preview is
neither complete queue enumeration nor a track identity/playability guarantee.
"""
from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.vdj_catalog import count_value, text_value

PREVIEW_LIMIT = 3


def queue_status(client):
    def anchor():
        active = as_bool(client.query("automix", "bool"))
        count = count_value(client.query("file_count automix"))
        position = count_value(client.query("get_automix_position")) if active and count else None
        return active, count, position

    def read_rows():
        rows = []
        for offset in range(1, PREVIEW_LIMIT + 1):
            # Use the manual's unnumbered form for the immediate next track.
            suffix = "" if offset == 1 else f" {offset}"
            rows.append({
                "offset": offset,
                "title": text_value(client.query(f"get_automix_song 'title'{suffix}", "text")),
                "artist": text_value(client.query(f"get_automix_song 'artist'{suffix}", "text")),
            })
        return rows

    before = anchor()
    active, count, position = before
    rows = read_rows() if active and count else []
    # Repeating both metadata and anchors catches many transitions, edits and
    # same-length reorders. OSC has no atomic snapshot/version primitive: even
    # equal reads cannot prove identity or exclude a change-and-change-back.
    if anchor() != before:
        raise BridgeError("Automix changed while reading the preview; inspect it again")
    if rows and read_rows() != rows:
        raise BridgeError("Upcoming tracks changed while reading the preview; inspect it again")
    if anchor() != before:
        raise BridgeError("Automix changed while reading the preview; inspect it again")

    upcoming = [row for row in rows if row["title"].strip() or row["artist"].strip()]
    missing = [row["offset"] for row in rows if row not in upcoming]
    if not active:
        availability = "automix-off"
        note = "Automix is off. Upcoming metadata is unavailable; the queue count is still shown. No playback was started."
    elif not count:
        availability = "empty"
        note = "VirtualDJ reports zero Automix list entries."
    elif not upcoming:
        availability = "metadata-unavailable"
        note = "VirtualDJ returned no upcoming titles or artists. This does not prove the queue is empty."
    else:
        availability = "available"
        note = "Up to three upcoming tracks, relative to the current Automix item. Metadata only; not the full queue or proof of playable audio."

    return {
        "connected": True, "read_only": True, "automix": active,
        "queue_count": count, "engine_position": position,
        "availability": availability, "upcoming": upcoming,
        "missing_offsets": missing, "preview_limit": PREVIEW_LIMIT,
        "consistency": "unchanged-across-reads", "snapshot_atomic": False,
        "complete": False, "playback_verified": False, "note": note,
    }
