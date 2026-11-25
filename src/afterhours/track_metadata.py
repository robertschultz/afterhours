"""Provider-neutral track facts. No network, audio access or invented analysis."""
from datetime import datetime, timezone
import math
import re

SCHEMA = "afterhours.track.v1"
TEXT_FIELDS = ("title", "artist", "album", "genre", "remix", "label")
FACT_FIELDS = (*TEXT_FIELDS, "year", "bpm", "key", "camelot", "duration", "energy")
# Pitch classes C through B. A is minor; B is major in Camelot notation.
WHEEL = {
    "major": ("8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"),
    "minor": ("5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"),
}
NOTES = ("C", "D-flat", "D", "E-flat", "E", "F", "G-flat", "G", "A-flat", "A", "B-flat", "B")
PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def clean_text(value, limit=300):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())[:limit]
    return value or None


def finite_number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and low <= result <= high else None


def musical_key(value):
    """Return canonical name and Camelot code, or (None, None), never guess mode."""
    value = clean_text(value, 40)
    if not value:
        return None, None
    wheel = re.fullmatch(r"0?([1-9]|1[0-2])([AB])", value, re.I)
    if wheel:
        camelot = f"{int(wheel[1])}{wheel[2].upper()}"
        mode = "minor" if camelot.endswith("A") else "major"
        pitch = WHEEL[mode].index(camelot)
    else:
        value = value.replace("♯", "#").replace("♭", "b")
        value = re.sub(r"-flat", "b", value, flags=re.I)
        value = re.sub(r"-sharp", "#", value, flags=re.I)
        parsed = re.fullmatch(r"([A-Ga-g])([#b]?)(?:\s*(major|minor|maj|min|M|m))?", value)
        if not parsed:
            # Full words are case-insensitive; M and m retain their distinct modes.
            parsed = re.fullmatch(r"([A-Ga-g])([#b]?)\s*(major|minor|maj|min)", value, re.I)
        if not parsed:
            return None, None
        suffix = parsed[3] or "major"  # Bare note names conventionally mean major.
        mode = "minor" if suffix == "m" or suffix.lower() in ("minor", "min") else "major"
        pitch = (PITCH[parsed[1].upper()] + {"": 0, "#": 1, "b": -1}[parsed[2]]) % 12
        camelot = WHEEL[mode][pitch]
    return f"{NOTES[pitch]} {mode}", camelot


def vdj_track(raw, *, observed_at=None):
    """Normalize a guarded browser selection; origin within VDJ remains unknown.

    VDJ tags may come from a provider, local analysis or user edits. Do not label
    them Beatport-authoritative or infer playability from a populated row.
    """
    if (not isinstance(raw, dict) or not isinstance(raw.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw["id"])):
        raise ValueError("Track needs the selection digest returned by VirtualDJ")
    folder = raw.get("folder")
    if (not isinstance(folder, dict) or not isinstance(folder.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", folder["id"])):
        raise ValueError("Track needs a VirtualDJ folder digest")
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    key, camelot = musical_key(raw.get("key"))
    milliseconds = finite_number(raw.get("duration_ms"), 1, 86_400_000)
    year = finite_number(raw.get("year"), 1000, 9999)
    track = {
        "schema": SCHEMA, "id": "virtualdj:" + raw["id"],
        "engine": {"name": "virtualdj", "selection_id": raw["id"], "folder_id": folder["id"]},
        "catalog": {"provider": None, "track_id": None},
        "observed_at": observed_at,
        **{field: clean_text(raw.get(field)) for field in TEXT_FIELDS},
        "year": int(year) if year is not None and year.is_integer() else None,
        "bpm": finite_number(raw.get("bpm"), 20, 400),
        "key": key, "camelot": camelot,
        "duration": milliseconds / 1000 if milliseconds is not None else None,
        "energy": None,
        "playback_verified": False,
    }
    track["provenance"] = {
        field: {"source": "virtualdj.browser" if track[field] is not None else None,
                "confidence": None, "estimated": None,
                "transform": "key-notation" if field in ("key", "camelot") and track[field] is not None
                             else "milliseconds-to-seconds" if field == "duration" and track[field] is not None else None}
        for field in FACT_FIELDS
    }
    track["missing_fields"] = [field for field in FACT_FIELDS if track[field] is None]
    return track
