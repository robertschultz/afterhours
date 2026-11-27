#!/usr/bin/env python3
"""Local VirtualDJ OSC control prototype. Python standard library only.

No LLM, credentials, downloads, audio capture, or background automation.
Commands preview by default; --execute permits a single live action.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import plistlib
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PORT = 7474
DEFAULT_REPLY_PORT = 7475
SETUP_HINT = (
    "Start VirtualDJ with a Pro license; in Settings > Options set "
    "oscPort=7474 (or the chosen --port) and oscPortBack=7475 "
    "(or the chosen --reply-port)."
)


class BridgeError(RuntimeError):
    pass


class OSCReplyTimeout(BridgeError):
    pass


def osc_string(value: str) -> bytes:
    if "\0" in value:
        raise ValueError("OSC strings cannot contain NUL")
    raw = value.encode("utf-8") + b"\0"
    return raw + b"\0" * (-len(raw) % 4)


def encode_osc(address: str, *values) -> bytes:
    if not address.startswith("/") or any(c.isspace() for c in address):
        raise ValueError("Invalid OSC address")
    tags, chunks = [], []
    for value in values:
        if isinstance(value, bool):
            tags.append("T" if value else "F")
        elif isinstance(value, int):
            tags.append("i")
            chunks.append(struct.pack(">i", value))
        elif isinstance(value, float) and math.isfinite(value):
            tags.append("f")
            chunks.append(struct.pack(">f", value))
        elif isinstance(value, str):
            tags.append("s")
            chunks.append(osc_string(value))
        elif value is None:
            tags.append("N")
        else:
            raise ValueError("Unsupported OSC value")
    return osc_string(address) + osc_string("," + "".join(tags)) + b"".join(chunks)


def decode_osc(data: bytes):
    """Decode one OSC message, rejecting malformed or unrecognized payloads."""
    def string_at(offset):
        try:
            end = data.index(0, offset)
            value = data[offset:end].decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid OSC string") from exc
        next_offset = (end + 4) & ~3
        if next_offset > len(data) or any(data[end:next_offset]):
            raise ValueError("Invalid OSC padding")
        return value, next_offset

    address, pos = string_at(0)
    if not address.startswith("/"):
        raise ValueError("Expected an OSC message, not a bundle")
    tags, pos = string_at(pos)
    if not tags.startswith(","):
        raise ValueError("Missing OSC type tags")
    values = []
    for tag in tags[1:]:
        if tag in "ifhd":
            size = 8 if tag in "hd" else 4
            if pos + size > len(data):
                raise ValueError("Truncated OSC number")
            fmt = {"i": ">i", "f": ">f", "h": ">q", "d": ">d"}[tag]
            value = struct.unpack_from(fmt, data, pos)[0]
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Nonfinite OSC number")
            values.append(value)
            pos += size
        elif tag == "s":
            value, pos = string_at(pos)
            values.append(value)
        elif tag in "TFN":
            values.append({"T": True, "F": False, "N": None}[tag])
        else:
            raise ValueError(f"Unsupported OSC tag: {tag}")
    if pos != len(data):
        raise ValueError("Unexpected data after OSC message")
    return address, values


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "0", "1"):
        return value.lower() in ("true", "1")
    raise BridgeError("VirtualDJ did not return a definite boolean state")


class VirtualDJ:
    def __init__(self, port=DEFAULT_PORT, timeout=1.5, budget_seconds=None,
                 reply_port=DEFAULT_REPLY_PORT):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Port must be between 1024 and 65535")
        if type(reply_port) is not int or (reply_port != 0 and not 1024 <= reply_port <= 65535):
            raise ValueError("Reply port must be zero or between 1024 and 65535")
        if reply_port == port:
            raise ValueError("Reply port must differ from the VirtualDJ port")
        for name, value in (("timeout", timeout), ("budget_seconds", budget_seconds)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be a positive finite number")
        if timeout is None:
            raise ValueError("timeout must be a positive finite number")
        self.peer = ("127.0.0.1", port)
        self.timeout = timeout
        self.deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(("127.0.0.1", reply_port))
        except OSError as exc:
            self.sock.close()
            raise BridgeError(f"Cannot open local OSC reply port {reply_port}; "
                              "another connection check may be using it. No command was sent.") from exc

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.sock.close()

    @staticmethod
    def path(script):
        # Only module-generated scripts reach this function. Catalog keywords
        # are constrained by vdj_catalog.search_literal before interpolation.
        return "/".join(script.split())

    def send(self, script):
        self.check_budget()
        address = "/vdj/" + self.path(script)
        self.sock.sendto(encode_osc(address), self.peer)
        return address

    def send_text(self, script, value):
        """Keep a native text parameter out of the slash-delimited script.

        Verified with synthetic get_text/set_var on the installed host. This
        does not propagate through conditionals: stage with set_var and use
        an explicit get_var expression there. The fixed action and its literal
        value remain separate OSC fields, never filename interpolation.
        """
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or '\0' in value:
            raise ValueError('A bounded literal native text parameter is required.')
        self.check_budget()
        address = '/vdj/' + self.path(script)
        self.sock.sendto(encode_osc(address, value), self.peer)
        return address

    def check_budget(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise BridgeError("VirtualDJ request deadline reached; inspect current state before retrying.")

    def query(self, script, kind=None):
        self.check_budget()
        # Live build 9729 returns empty OSC strings for boolean queries even
        # with the documented (bool) prefix. A read-only VDJScript conditional
        # returns explicit integer values instead. Never treat blank as false.
        if kind == "bool":
            script = f"{script} ? get_constant 1 : get_constant 0"
            kind = None
        path = self.path(script)
        prefix = f"({kind})/" if kind else ""
        address = "/vdj/query/" + prefix + path
        expected = {"/vdj/" + path, "/vdj/" + prefix + path}
        deadline = time.monotonic() + self.timeout
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        self.sock.sendto(encode_osc(address), self.peer)
        while (remaining := deadline - time.monotonic()) > 0:
            self.sock.settimeout(remaining)
            try:
                data, peer = self.sock.recvfrom(65535)
            except socket.timeout:
                break
            if peer != self.peer:
                continue
            try:
                received, values = decode_osc(data)
            except ValueError:
                continue
            if received in expected:
                if len(values) != 1 or values[0] is None:
                    raise BridgeError(f"No usable value for {script!r}; command not verified")
                return values[0]
        self.check_budget()
        raise OSCReplyTimeout(f"No OSC response from VirtualDJ. {SETUP_HINT}")

    def status(self):
        # Establish a read-only response before any playback command is allowed.
        master_bpm = self.query("get_bpm")
        decks = []
        for deck in (1, 2):
            loaded = as_bool(self.query(f"deck {deck} loaded", "bool"))
            decks.append({
                "deck": deck,
                "loaded": loaded,
                # Empty decks report localized instructional text as a title.
                "title": self.query(f"deck {deck} get_title", "text") if loaded else "",
                "artist": self.query(f"deck {deck} get_artist", "text") if loaded else "",
                "bpm": self.query(f"deck {deck} get_bpm") if loaded else None,
                "playing": as_bool(self.query(f"deck {deck} play", "bool")),
            })
        return {"connected": True, "master_bpm": master_bpm, "decks": decks,
                "automix": as_bool(self.query("automix", "bool"))}


def action_plan(action, deck):
    if deck not in (1, 2):
        raise ValueError("Deck must be 1 or 2")
    scripts = {
        "play": f"deck {deck} play",
        "pause": f"deck {deck} pause",
        "automix-start": "automix on",
        "automix-stop": "automix off",
        "takeover": "automix off",
        "next": "automix_skip",
    }
    if action not in scripts:
        raise ValueError("Unknown action")
    return scripts[action]


def perform(client, action, deck, execute=False):
    script = action_plan(action, deck)
    if not execute:
        return {"mode": "preview", "script": script, "sent": False,
                "note": "Add --execute to send this action after a connection check."}
    before = client.status()
    if action == "play" and not str(before["decks"][deck-1]["title"]).strip():
        raise BridgeError("Load a track into this deck first")
    if action == "next" and not before["automix"]:
        raise BridgeError("The next command requires Automix to be active")
    address = client.send(script)  # One attempt only; UDP delivery is not an acknowledgement.
    if action == "next":
        return {"mode": "live", "sent": True, "verified": False, "address": address,
                "note": "Transition requested once. Confirm the new track after the fade."}
    check = f"deck {deck} play" if action in ("play", "pause") else "automix"
    target = action in ("play", "automix-start")
    observed = None
    try:
        for _ in range(4):
            time.sleep(0.075)
            observed = as_bool(client.query(check, "bool"))
            if observed is target:
                return {"mode": "live", "sent": True, "verified": True,
                        "address": address, "observed_state": observed}
    except BridgeError as exc:
        return {"mode": "live", "sent": True, "verified": False,
                "address": address, "error": str(exc),
                "note": "Action may have occurred. Check VirtualDJ before retrying."}
    return {"mode": "live", "sent": True, "verified": False,
            "address": address, "observed_state": observed,
            "note": "Requested state was not confirmed. Check VirtualDJ before retrying."}


def doctor(port, reply_port=DEFAULT_REPLY_PORT):
    applications = []
    for root in (Path("/Applications"), Path.home()/"Applications"):
        if root.is_dir():
            for item in root.iterdir():
                if item.suffix == ".app" and "virtualdj" in item.name.lower().replace(" ", ""):
                    info = {}
                    try:
                        with (item/"Contents/Info.plist").open("rb") as handle:
                            info = plistlib.load(handle)
                    except (OSError, plistlib.InvalidFileException):
                        pass
                    applications.append({"path": str(item),
                        "version": info.get("CFBundleShortVersionString"),
                        "build": info.get("CFBundleVersion")})
    devices = []
    inventory_error = None
    try:
        run = subprocess.run(["/usr/sbin/system_profiler", "SPUSBDataType",
            "SPAudioDataType", "-json"], capture_output=True, text=True, timeout=25, check=True)
        def walk(value):
            if isinstance(value, dict):
                name = str(value.get("_name", ""))
                if any(token in name.lower() for token in ("denon", "prime go", "primego", "inmusic")):
                    devices.append(name)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(json.loads(run.stdout))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        inventory_error = type(exc).__name__
    try:
        with VirtualDJ(port, timeout=0.5, reply_port=reply_port) as client:
            bpm = client.query("get_bpm")
        osc = {"responding": True, "reported_bpm": bpm}
    except (BridgeError, OSError) as exc:
        osc = {"responding": False, "reason": str(exc)}
    version = platform.mac_ver()[0]
    return {"platform": platform.system(), "macOS": version, "applications": applications,
            "matching_usb_audio_devices": sorted(set(devices)), "inventory_error": inventory_error,
            "osc": osc,
            "compatibility_note": "As checked 2026-10-24, Denon lists macOS 27 unsupported; "
                "VirtualDJ's supported OS list ends at macOS 26. Live compatibility needs testing.",
            "hardware_playback_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["doctor", "status", "play", "pause",
        "automix-start", "automix-stop", "takeover", "next", "catalog-status",
        "catalog-search", "load-selected", "queue-selected", "queue-status"])
    parser.add_argument("value", nargs="?", help="Search keywords for catalog-search")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--reply-port", type=int, default=DEFAULT_REPLY_PORT,
                        help="Match oscPortBack; use 0 for replies to an ephemeral source port")
    parser.add_argument("--deck", type=int, choices=[1, 2], default=1)
    parser.add_argument("--selection", help="Selected track ID returned by catalog-status")
    parser.add_argument("--folder", help="Folder ID returned by catalog-status")
    parser.add_argument("--execute", action="store_true", help="Permit one live playback action")
    args = parser.parse_args()
    try:
        if args.action in {"catalog-status", "catalog-search", "load-selected", "queue-selected"}:
            # When this file is the entrypoint, share the same exception class
            # with the catalog module instead of importing this file twice.
            sys.modules.setdefault("dj_bridge", sys.modules[__name__])
            from afterhours.vdj_catalog import catalog_status, catalog_search, load_selected, queue_selected
            def catalog_action(client):
                if args.action == "catalog-status":
                    return catalog_status(client)
                if args.action == "catalog-search":
                    return catalog_search(client, args.value, args.folder, args.execute)
                fn = load_selected if args.action == "load-selected" else queue_selected
                options = dict(expected_id=args.selection, expected_folder=args.folder, execute=args.execute)
                if args.action == "load-selected":
                    options["deck"] = args.deck
                return fn(client, **options)
            if args.action != "catalog-status" and not args.execute:
                result = catalog_action(None)
            else:
                with VirtualDJ(args.port, timeout=.8, budget_seconds=4 if args.action == "catalog-status" else 6, reply_port=args.reply_port) as client:
                    result = catalog_action(client)
        elif args.action == "queue-status":
            sys.modules.setdefault("dj_bridge", sys.modules[__name__])
            from afterhours.vdj_queue import queue_status
            with VirtualDJ(args.port, timeout=.8, budget_seconds=4, reply_port=args.reply_port) as client:
                result = queue_status(client)
        elif args.action == "doctor":
            result = doctor(args.port, args.reply_port)
        elif args.action != "status" and not args.execute:
            result = perform(None, args.action, args.deck)
        else:
            with VirtualDJ(args.port, reply_port=args.reply_port) as client:
                result = client.status() if args.action == "status" else perform(
                    client, args.action, args.deck, execute=args.execute)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2 if result.get("sent") and not result.get("verified", False) else 0
    except (BridgeError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
