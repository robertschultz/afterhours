"""Experimental catalog operations through the documented VirtualDJ OSC interface.

The user signs into a provider and selects its folder in VirtualDJ first.
This module does not fetch protected audio or infer a streaming entitlement.
Wire behavior is tested with a simulated peer; live validation is separate.
"""
import hashlib
import math
import time

from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.track_metadata import vdj_track
from afterhours.native_recording import ExactRecording
from afterhours.native_sources import catalog_reference


def identity(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def text_value(value):
    if not isinstance(value, str):
        raise BridgeError("VirtualDJ did not return text for a catalog property")
    return value


def search_literal(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError("Search needs 1 to 160 characters")
    # OSC's documented transport replaces spaces with slashes. Literal slashes
    # cannot round-trip. Quotes/backticks/control chars must not become script.
    # Limit address-pattern characters too; preserve normal Unicode artist names.
    if any(ord(c) < 32 or ord(c) == 127 or c in '\"`/\\*?[]{}#' for c in value):
        raise ValueError("Use search keywords without quotes, slashes, backticks or wildcard characters")
    value = " ".join(value.split())
    return '"' + value + '"'


def count_value(value):
    if isinstance(value, bool):
        raise BridgeError("Invalid catalog count")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BridgeError("Invalid catalog count") from exc
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise BridgeError("Invalid catalog count")
    return int(number)


def folder_state(client):
    locator = text_value(client.query("get_browsed_folder_path", "text"))
    name = text_value(client.query("get_browsed_folder", "text"))
    if not locator:
        raise BridgeError("Select a catalog or local music folder in VirtualDJ first")
    return {"id": identity(locator), "name": name}


def selection(client, include_details=False):
    folder = folder_state(client)
    locator = text_value(client.query("get_browsed_filepath", "text"))
    if not locator:
        return None
    source, provider_id = catalog_reference(locator)
    track = {
        "id": identity(locator),
        "catalog": {"provider": source.id, "track_id": provider_id} if source else None,
        "title": text_value(client.query("get_browsed_title", "text")),
        "artist": text_value(client.query("get_browsed_artist", "text")),
        "bpm": client.query("get_browsed_bpm"),
        "key": text_value(client.query("get_browsed_key", "text")),
        "folder": folder,
    }
    if include_details:
        # Read-only, fixed verbs. Keep this enrichment out of load/takeover
        # guards so optional catalog detail never delays an audio mutation.
        detail_queries = (
            ("album", "get_browsed_album"), ("genre", "get_browsed_genre"),
            ("remix", "get_browsed_song 'remix'"),
            ("label", "get_browsed_song 'label'"),
            ("year", "get_browsed_song 'year'"),
            ("duration_ms", "get_browsed_song 'length' 'ms'"),
        )
        unread = []
        for index, (field, script) in enumerate(detail_queries):
            try:
                track[field] = (client.query(script) if field == "duration_ms"
                                else text_value(client.query(script, "text")))
            except BridgeError:
                # Do not spend every per-query timeout on unsupported details.
                # Preserve basic facts only if the final identity guard succeeds.
                unread = [name for name, _ in detail_queries[index:]]
                break
        # The explicit ms argument avoids guessing a unit from a formatted
        # length string. Empty/invalid values stay unknown during normalization.
        track["detail_read_complete"] = not unread
        track["unread_fields"] = ["duration" if name == "duration_ms" else name for name in unread]
    # Do not produce a mixed metadata snapshot if the user changes selection.
    if (text_value(client.query("get_browsed_filepath", "text")) != locator
            or folder_state(client)["id"] != folder["id"]):
        raise BridgeError("Browser selection changed while reading it; inspect it again")
    try:
        bpm = float(track["bpm"]) if not isinstance(track["bpm"], bool) else float("nan")
        track["bpm"] = bpm if math.isfinite(bpm) and bpm > 0 else None
    except (TypeError, ValueError):
        track["bpm"] = None
    if include_details:
        track["metadata"] = vdj_track(track)
        track.pop("duration_ms", None)
    # Only a digest is returned; provider locators can contain private data.
    return track


def catalog_status(client):
    build = client.query("get_build")
    folder = folder_state(client)
    count = count_value(client.query("file_count"))
    selected = selection(client, include_details=True) if count else None
    if folder_state(client)["id"] != folder["id"]:
        raise BridgeError("Browser folder changed while reading it; inspect it again")
    return {"connected": True, "build": build, "folder": folder,
            "visible_count": count, "selected": selected,
            "playback_verified": False, "snapshot_atomic": False,
            "scope": "selected-track", "complete_catalog": False,
            "note": "These are browser metadata, not confirmation of provider sign-in or playable audio."}


def require_selection(client, expected_id, expected_folder):
    if not expected_id or not expected_folder:
        raise ValueError("Use --selection and --folder IDs from catalog-status")
    selected = selection(client)
    if not selected or selected["id"] != expected_id:
        raise BridgeError("The selected track changed; inspect catalog-status again")
    if selected["folder"]["id"] != expected_folder:
        raise BridgeError("The catalog folder changed; inspect catalog-status again")
    return selected


def preview(script, note):
    return {"mode": "preview", "sent": False, "script": script, "note": note}


def dispatch_only(address, note, **extra):
    return {"mode": "live", "sent": True, "verified": False,
            "address": address, "note": note, **extra}


def catalog_search(client, query, expected_folder=None, execute=False):
    script = "search " + search_literal(query)
    if not execute:
        return preview(script, "Select the provider folder in VirtualDJ, then pass its --folder ID and --execute.")
    if not expected_folder:
        raise ValueError("Use --folder from catalog-status; the provider must be selected in VirtualDJ")
    before = folder_state(client)
    if before["id"] != expected_folder:
        raise BridgeError("The catalog folder changed; search was not sent")
    address = client.send(script)
    return dispatch_only(address, "Search sent once. Wait for results in VirtualDJ, then inspect catalog-status. Results are not yet verified.", folder=before)


def load_selected(client, deck, expected_id=None, expected_folder=None, execute=False, wait=None):
    if type(deck) is not int or deck not in (1, 2, 3, 4):
        raise ValueError("Deck must be 1 to 4")
    # Recheck play state inside the DJ engine as well as before dispatch, so a
    # physical Play press between the read and the action can prevent the load.
    script = f"deck {deck} play ? nothing : deck {deck} load"
    if not execute:
        return preview(script, "Loads a selected track into an idle deck without issuing Play. Pass --selection, --folder and --execute.")
    selected = require_selection(client, expected_id, expected_folder)
    if as_bool(client.query("automix", "bool")):
        raise BridgeError("Disable Automix before loading a deck manually")
    if as_bool(client.query(f"deck {deck} play", "bool")):
        raise BridgeError("The target deck is playing; no load was sent")
    # Re-read identity after the deck checks. A user can still change a browser
    # row after this read: never auto-play, and verify the loaded locator below.
    require_selection(client, expected_id, expected_folder)
    address = client.send(script)
    return _confirm_loaded(client, deck, expected_id, address, selected, wait)


def paused_load_target(client, deck):
    """Bind an exact restoration to the unchanged, paused target deck."""
    if type(deck) is not int or deck not in (1, 2, 3, 4):
        raise ValueError('Choose a native deck from 1 to 4.')
    if as_bool(client.query(f'deck {deck} play', 'bool')):
        raise BridgeError('The target deck started manually; no load was sent.')
    loaded = as_bool(client.query(f'deck {deck} loaded', 'bool'))
    locator = text_value(client.query(f'deck {deck} get_filepath', 'text')) if loaded else ''
    if loaded and not locator:
        raise BridgeError('The target deck identity is unconfirmed; no load was sent.')
    controls = {}
    for name in ('volume', 'pitch'):
        raw = client.query(f'deck {deck} {name}')
        try:
            value = float(raw) if not isinstance(raw, bool) else float('nan')
        except (TypeError, ValueError):
            value = float('nan')
        if not math.isfinite(value):
            raise BridgeError('The target deck controls are unconfirmed; no load was sent.')
        controls[name] = value
    controls['pfl'] = as_bool(client.query(f'deck {deck} pfl', 'bool'))
    return {'loaded': loaded, 'selectionId': identity(locator) if locator else None, **controls}


class ExactRestoreCleanupUnconfirmed(BridgeError):
    """Private native staging was not confirmed cleared; never replay Load."""


def _cleared_native_text(value):
    return type(value) in (int, float) and value == 0 or value in ('0', '0.0')


def dispatch_exact_load(client, deck, recording, expected_id, expected_folder, target, before_dispatch=None):
    """One identity-bound load; the caller owns confirmation and deck cleanup."""
    if type(deck) is not int or deck not in (1, 2, 3, 4):
        raise ValueError('Choose a native deck from 1 to 4.')
    if not isinstance(recording, ExactRecording):
        raise BridgeError('Exact restoration needs a verified private recording locator.')
    value = recording.value_for(expected_id)
    # The installed host loses an implicit OSC argument inside a conditional.
    # Stage literal data with set_var instead and pass it through a text cast.
    # Load treats a backtick expression as a literal filename on this host.
    # Four fixed private slots bound native state.
    name = f'$afterhours_restore_{deck}'
    read = f"get_var '{name}'"
    clear = f"set_var '{name}' 0"
    cleared = False
    try:
        client.send_text(f"set_var '{name}'", value)
        if client.query(read, 'text') != value:
            raise BridgeError('Exact restoration data was not verified; no load was sent.')
        if not expected_folder or folder_state(client)['id'] != expected_folder:
            raise BridgeError('The catalog folder changed before exact restoration; no load was sent.')
        if as_bool(client.query('automix', 'bool')):
            raise BridgeError('Disable Automix before loading a deck manually')
        current = paused_load_target(client, deck)
        if (not isinstance(target, dict) or target.keys() != current.keys() or
                any(abs(current[k] - target[k]) > .0001 if k in ('volume', 'pitch') else current[k] != target[k]
                    for k in current)):
            raise BridgeError('The target recording or controls changed before exact restoration; no load was sent.')
        if folder_state(client)['id'] != expected_folder:
            raise BridgeError('The catalog folder changed before exact restoration; no load was sent.')
        # Ownership is latched only after staging and all host checks. The
        # native Play guard remains atomic with Load. Both native branches
        # clear private data after its value was consumed, never before Load.
        if before_dispatch:
            before_dispatch()
        # Contain the implicit text parameter so it cannot flow into cleanup.
        # Unlike get_text interpolation, this feeds a string to Load itself.
        script = (f"deck {deck} play ? {clear} : "
                  f"( {read} & param_cast 'text' & deck {deck} load ) & {clear}")
        address = client.send(script)
        cleared = _cleared_native_text(client.query(read, 'text'))
        if not cleared:
            raise BridgeError('Exact restoration dispatch remains unconfirmed. No repeat Load or Play was sent.')
        return address
    finally:
        if not cleared:
            # This is private-data cleanup, never another Load or Play. A
            # failed readback remains explicit rather than claiming cleanup.
            try:
                client.send(clear)
                if not _cleared_native_text(client.query(read, 'text')):
                    raise BridgeError('Private restoration data was not cleared.')
            except (BridgeError, OSError):
                raise ExactRestoreCleanupUnconfirmed(
                    'Private restoration data cleanup remains unconfirmed. No repeat Load or Play was sent.') from None


def load_exact_recording(client, deck, recording, expected_id, expected_folder, target, wait=None, before_dispatch=None):
    # Decks 3 and 4 are loaded here only by a four-deck rotation, never while
    # track preparation holds them: the session releases the deck first.
    if type(deck) is not int or deck not in (1, 2, 3, 4):
        raise ValueError('The foreground loader owns decks 1 to 4 only.')
    address = dispatch_exact_load(client, deck, recording, expected_id, expected_folder, target, before_dispatch)
    return _confirm_loaded(client, deck, expected_id, address,
                           {'id': expected_id, 'folder': {'id': expected_folder}, 'source': 'native-exact-recording'}, wait)


def _confirm_loaded(client, deck, expected_id, address, selected, wait):
    last_error = None
    deadline = min(time.monotonic() + 4, getattr(client, 'deadline', None) or float('inf'))
    for _ in range(30):
        if time.monotonic() >= deadline:
            break
        try:
            (wait or time.sleep)(.1)
            loaded = as_bool(client.query(f"deck {deck} loaded", "bool"))
            failed = as_bool(client.query(f"deck {deck} deck_has_error", "bool"))
            if failed:
                return dispatch_only(address, "VirtualDJ reported a deck error; check provider sign-in or track availability.", deck=deck)
            # The dedicated deck verb matches get_browsed_filepath. On live
            # build 9729 the generic property getter returns a different value.
            locator = text_value(client.query(f"deck {deck} get_filepath", "text"))
            if loaded and locator and identity(locator) == expected_id:
                return {"mode": "live", "sent": True, "verified": True,
                        "address": address, "deck": deck, "selected": selected,
                        "playback_verified": False,
                        "note": "The expected track is loaded. Audible playback still needs a separate check."}
        except (BridgeError, OSError) as exc:
            # Stream preparation can briefly starve OSC replies. Retry only the
            # read checks inside the original deadline, never Load or Play.
            last_error = str(exc)
    if last_error:
        return dispatch_only(address, "Load may have occurred. No retry or Play was sent; inspect the deck before continuing.", error=last_error, deck=deck)
    return dispatch_only(address, "Expected track load was not confirmed. No retry or Play was sent; inspect the deck before continuing.", deck=deck)


def queue_selected(client, expected_id=None, expected_folder=None, execute=False):
    if not execute:
        return preview("playlist_add", "Append the selected track(s) to Automix. Select one row in VirtualDJ and pass --selection, --folder and --execute.")
    selected = require_selection(client, expected_id, expected_folder)
    before = count_value(client.query("file_count automix"))
    address = client.send("playlist_add")
    # A count change does not prove identity; keep this explicitly unverified.
    after = None
    try:
        after = count_value(client.query("file_count automix"))
    except (BridgeError, OSError):
        pass
    return dispatch_only(address, "Queue append sent once. Verify the Automix list in VirtualDJ; multiple selected rows may be appended.",
                         selected=selected, queue_count_before=before, queue_count_after=after)
