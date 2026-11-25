"""Local programme ownership around injected Music Runtime handles.

This foundation never constructs a DJ, starts a worker, requests a model/grant,
opens audio or dispatches transport. The stock VirtualDJ master is one exclusive
resource. Handles retain the existing queue/agent implementation and authority.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading

MAX_PROGRAMMES = 16
MAX_COMMANDS = 256  # Refuse admission; do not evict IDs and accidentally replay.
MAX_METADATA_BYTES = 1024 * 1024
STOCK_OUTPUT = "virtualdj-default/master"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
ACTIONS = {"plan_queue", "set_brief", "manual_takeover"}


class ProgrammeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ProgrammeError(code, message)


def identifier(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class ProgrammeHandle:
    session: object
    agent: object


class MusicProgrammeRegistry:
    """Single-owner metadata registry; callers provide existing or idle handles.

    ``factory(programme_id)`` is explicit dependency injection, never a default
    process/model/audio constructor. It must return a ProgrammeHandle with an
    idle session/agent and separately scoped harness persistence.
    """
    def __init__(self, metadata_path=None, *, factory=None):
        self.path = Path(metadata_path) if metadata_path is not None else None
        self.factory = factory
        self.lock = threading.RLock()
        self.handles = {}
        self._owner = None
        self._closed = self._failed = False
        self.state = {"schemaVersion": 1, "defaultProgrammeId": None, "programmes": {},
                      "outputs": {STOCK_OUTPUT: {"epoch": 0, "programmeId": None, "sessionId": None, "state": "available"}},
                      "commands": []}
        try:
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                require(not self.path.is_symlink(), "unsafe_storage", "Programme metadata cannot be a symlink.")
                self._owner = os.open(self.path.with_suffix(self.path.suffix + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise ProgrammeError("owner_active", "Another registry owns this metadata file.") from error
                if self.path.exists():
                    with self.path.open("rb") as handle:
                        raw = handle.read(MAX_METADATA_BYTES + 1)
                    require(len(raw) <= MAX_METADATA_BYTES, "metadata_limit", "Programme metadata is too large.")
                    def unique(pairs):
                        result = {}
                        for key, value in pairs:
                            require(key not in result, "invalid_metadata", "Duplicate programme metadata field.")
                            result[key] = value
                        return result
                    self.state = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
                    self._validate_state()
                    for lease in self.state["outputs"].values():
                        if lease["state"] != "available":
                            lease.update(state="uncertain", epoch=lease["epoch"] + 1)
                    for receipt in self.state["commands"]:
                        if receipt["status"] == "dispatching":
                            receipt["status"] = "uncertain"
                self._persist()
        except Exception:
            self.close()
            raise

    def _validate_state(self):
        state = self.state
        require(isinstance(state, dict) and set(state) == {"schemaVersion", "defaultProgrammeId", "programmes", "outputs", "commands"} and type(state["schemaVersion"]) is int and state["schemaVersion"] == 1,
                "invalid_metadata", "Unsupported programme metadata.")
        programmes = state["programmes"]
        require(isinstance(programmes, dict) and len(programmes) <= MAX_PROGRAMMES, "invalid_metadata", "Invalid saved programme registry.")
        for pid, row in programmes.items():
            require(identifier(pid) and isinstance(row, dict) and set(row) == {"revision", "sessionId"} and type(row["revision"]) is int and row["revision"] >= 0 and isinstance(row["sessionId"], str) and 1 <= len(row["sessionId"]) <= 128,
                    "invalid_metadata", "Invalid saved programme identity.")
        require(state["defaultProgrammeId"] is None or (identifier(state["defaultProgrammeId"]) and state["defaultProgrammeId"] in programmes), "invalid_metadata", "Invalid default programme.")
        require(isinstance(state["outputs"], dict) and set(state["outputs"]) == {STOCK_OUTPUT}, "invalid_metadata", "Unsupported output resources.")
        lease = state["outputs"][STOCK_OUTPUT]
        require(isinstance(lease, dict) and set(lease) == {"epoch", "programmeId", "sessionId", "state"} and type(lease["epoch"]) is int and lease["epoch"] >= 0 and lease["state"] in {"available", "held", "uncertain"}, "invalid_metadata", "Invalid output lease.")
        require((lease["state"] == "available" and lease["programmeId"] is None and lease["sessionId"] is None) or
                (lease["state"] != "available" and identifier(lease["programmeId"]) and lease["programmeId"] in programmes and lease["sessionId"] == programmes[lease["programmeId"]]["sessionId"]), "invalid_metadata", "Invalid output owner.")
        require(isinstance(state["commands"], list) and len(state["commands"]) <= MAX_COMMANDS, "invalid_metadata", "Invalid command ledger.")
        seen = set()
        for row in state["commands"]:
            require(isinstance(row, dict) and set(row) == {"clientId", "programmeId", "commandId", "action", "fingerprint", "sessionId", "status", "result"}, "invalid_metadata", "Invalid saved command receipt.")
            key = (row["clientId"], row["programmeId"], row["commandId"])
            require(all(identifier(value) for value in key) and key not in seen and row["programmeId"] in programmes and row["action"] in ACTIONS and isinstance(row["fingerprint"], str) and re.fullmatch(r"[a-f0-9]{64}", row["fingerprint"]) and isinstance(row["sessionId"], str) and row["status"] in {"dispatching", "completed", "rejected", "uncertain"} and (row["result"] is None or isinstance(row["result"], dict)), "invalid_metadata", "Invalid saved command scope.")
            seen.add(key)

    def _persist(self):
        # All writes, including the final receipt of an admitted command, share
        # the ownership lifecycle lock. A closed/failed owner cannot overwrite
        # recovery metadata subsequently published by its replacement.
        with self.lock:
            self._ready()
            return self._write_metadata()

    def _write_metadata(self):
        if self.path is None:
            return
        temporary = None
        try:
            payload = canonical(self.state).encode()
            require(len(payload) <= MAX_METADATA_BYTES, "metadata_limit", "Programme metadata capacity reached.")
            # Unique, exclusive temporary file; do not follow a preplanted link.
            import tempfile
            descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            self._failed = True
            raise
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def _ready(self):
        require(not self._closed and not self._failed, "registry_unavailable", "Reopen and reconcile the programme registry before another mutation.")

    @contextmanager
    def _scope(self, pid):
        self._ready()
        require(identifier(pid), "invalid_id", "Use a bounded programme ID.")
        require(pid in self.handles, "programme_unattached", "Attach this programme's existing harness before using it.")
        handle = self.handles[pid]
        with handle.agent.lock, handle.session.lock:
            require(handle.session.snapshot()["id"] == self.state["programmes"][pid]["sessionId"], "session_changed", "The attached harness changed session; reconcile its ownership before using it.")
            yield handle

    def _idle(self, handle):
        snapshot = handle.session.snapshot()
        require(not snapshot.get("active") and not any(row.get("playing") for row in (snapshot.get("telemetry") or {}).get("decks", [])), "live_transport_unsupported", "This foundation accepts metadata controls only while the programme is idle.")
        for owner in (handle.session, handle.agent):
            worker = getattr(owner, "worker", None)
            require(not (worker and worker.is_alive()), "programme_busy", "Wait for existing programme work to finish.")
        require(not getattr(handle.agent, "pipeline", None), "programme_busy", "Finish existing preparation before editing metadata.")
        for name in ("catalog_job", "dig_job", "background_job"):
            job = getattr(handle.session, name, None)
            require(job is None or getattr(job, "done", False), "programme_busy", "Finish existing native work before editing metadata.")
        return snapshot

    def attach(self, programme_id, session, agent):
        """Attach existing ownership; preserve exact session, queue and agent objects."""
        return self._attach(programme_id, session, agent, persist=True)

    def _attach(self, programme_id, session, agent, *, persist):
        with self.lock:
            self._ready()
            require(identifier(programme_id), "invalid_id", "Use a bounded programme ID.")
            require(getattr(agent, "session", None) is session, "handle_mismatch", "The Music Agent must own this exact session.")
            require(all(handle.session is not session and handle.agent is not agent for pid, handle in self.handles.items() if pid != programme_id), "shared_harness", "Independent programmes cannot share a session or agent handle.")
            prior = self.handles.get(programme_id)
            require(prior is None or (prior.session is session and prior.agent is agent), "already_attached", "This programme already has an attached owner.")
            with agent.lock, session.lock:
                sid = session.snapshot()["id"]
                require(isinstance(sid, str) and 1 <= len(sid) <= 128, "invalid_session", "The harness requires a stable session ID.")
                saved = self.state["programmes"].get(programme_id)
                if saved is None:
                    require(len(self.state["programmes"]) < MAX_PROGRAMMES, "programme_limit", "Programme registry capacity reached.")
                    self.state["programmes"][programme_id] = {"revision": 0, "sessionId": sid}
                else:
                    require(saved["sessionId"] == sid, "session_changed", "Restore the saved harness session identity before attaching; no output was reassigned.")
                self.handles[programme_id] = ProgrammeHandle(session, agent)
                if persist:
                    self._persist()
                return self.read(programme_id)

    def adopt_default(self, session, agent, programme_id="default"):
        with self.lock:
            self._ready()
            existing = self.state["defaultProgrammeId"]
            require(existing is None or existing == programme_id, "default_exists", "The existing default programme cannot be replaced.")
            lease = self.state["outputs"][STOCK_OUTPUT]
            require(lease["state"] == "available" or lease["programmeId"] == programme_id, "output_busy", "The VirtualDJ master belongs to another programme.")
            # Publish programme + default identity + stock ownership in one
            # atomic record. A crash cannot leave a known live default with
            # its output incorrectly persisted as free.
            self._attach(programme_id, session, agent, persist=False)
            self.state["defaultProgrammeId"] = programme_id
            lease = self.state["outputs"][STOCK_OUTPUT]
            if lease["state"] == "available":
                self._allocate(programme_id)
            else:
                require(lease["programmeId"] == programme_id, "output_busy", "The VirtualDJ master belongs to another programme.")
            self._persist()
            return self.read(programme_id)

    def add_programme(self, programme_id):
        with self.lock:
            self._ready()
            require(identifier(programme_id) and programme_id not in self.state["programmes"], "invalid_id", "Choose a new bounded programme ID.")
            require(len(self.state["programmes"]) < MAX_PROGRAMMES, "programme_limit", "Programme registry capacity reached.")
            require(callable(self.factory), "factory_required", "Provide an explicit idle programme factory.")
            handle = self.factory(programme_id)
            require(isinstance(handle, ProgrammeHandle), "invalid_handle", "The factory must return a ProgrammeHandle.")
            with handle.agent.lock, handle.session.lock:
                self._idle(handle)
                return self.attach(programme_id, handle.session, handle.agent)

    def rebind_recovered(self, programme_id, session, agent, *, prior_session_id, observed_stopped):
        """Explicit recovery to a new idle harness after the old output stopped.

        The caller restores queues through existing recovery first and supplies
        authoritative stopped evidence. The lease stays uncertain/fenced until
        separately reconciled; this method cannot adopt an active old worker.
        """
        with self.lock:
            self._ready()
            require(identifier(programme_id) and programme_id in self.state["programmes"] and programme_id not in self.handles, "recovery_scope", "Recover an existing, unattached programme only.")
            saved = self.state["programmes"][programme_id]
            require(prior_session_id == saved["sessionId"] and observed_stopped is True, "invalid_reconciliation", "Confirm the prior session's output stopped before binding its replacement.")
            require(getattr(agent, "session", None) is session and all(handle.session is not session and handle.agent is not agent for handle in self.handles.values()), "handle_mismatch", "Recovery requires a distinct matching session/agent pair.")
            with agent.lock, session.lock:
                handle = ProgrammeHandle(session, agent)
                snapshot = self._idle(handle)
                sid = snapshot.get("id")
                require(isinstance(sid, str) and 1 <= len(sid) <= 128 and sid != prior_session_id, "invalid_session", "Recovery requires the new harness session identity.")
                saved.update(sessionId=sid, revision=saved["revision"] + 1)
                lease = self.state["outputs"][STOCK_OUTPUT]
                if lease["programmeId"] == programme_id:
                    lease.update(sessionId=sid, epoch=lease["epoch"] + 1, state="uncertain")
                self.handles[programme_id] = handle
                self._persist()
                return self.read(programme_id)

    def _context(self, pid, handle):
        from afterhours.dj_agent import context_key
        snapshot = handle.session.snapshot()
        return {"programmeId": pid, "registryRevision": self.state["programmes"][pid]["revision"],
                "sessionId": snapshot["id"], "queueContext": context_key(snapshot),
                "agentContext": handle.agent.settings.expected(snapshot, handle.agent.state["revision"])}

    def expected_context(self, programme_id):
        with self.lock, self._scope(programme_id) as handle:
            return deepcopy(self._context(programme_id, handle))

    def read(self, programme_id):
        with self.lock, self._scope(programme_id) as handle:
            snapshot = handle.session.snapshot()
            lease = self.state["outputs"][STOCK_OUTPUT]
            own_lease = deepcopy(lease) if lease["programmeId"] == programme_id else None
            return {"programmeId": programme_id, "isDefault": programme_id == self.state["defaultProgrammeId"],
                    "sessionId": snapshot["id"], "queueIds": [row["id"] for row in snapshot.get("order", [])],
                    "agent": {"revision": handle.agent.state["revision"], "direction": handle.agent.state.get("direction", ""), "status": handle.agent.state.get("status")},
                    "expected": self._context(programme_id, handle), "output": own_lease,
                    "outputStatus": own_lease["state"] if own_lease else "unassigned",
                    "liveTransportAvailable": False,
                    "receipts": deepcopy([row for row in self.state["commands"] if row["programmeId"] == programme_id][-16:])}

    def capabilities(self):
        with self.lock:
            self._ready()
            return {"schemaVersion": 1, "programmeLimit": MAX_PROGRAMMES, "commandCapacity": MAX_COMMANDS,
                    "defaultProgrammeId": self.state["defaultProgrammeId"], "actions": sorted(ACTIONS),
                    "liveTransportCommands": False, "modelRequests": False, "createsAllowances": False,
                    "outputs": [{"id": STOCK_OUTPUT, "adapter": "virtualdj", "independentProgrammeCapacity": 1,
                                 **deepcopy(self.state["outputs"][STOCK_OUTPUT])}]}

    def _check_expected(self, pid, handle, expected):
        require(expected == self._context(pid, handle), "stale_context", "Programme/session/queue/direction ownership changed; inspect this programme again.")

    def _allocate(self, pid):
        lease = self.state["outputs"][STOCK_OUTPUT]
        require(lease["state"] == "available", "output_busy", "This output is held or uncertain; it cannot host another independent set.")
        lease.update(epoch=lease["epoch"] + 1, programmeId=pid, sessionId=self.state["programmes"][pid]["sessionId"], state="held")
        self.state["programmes"][pid]["revision"] += 1

    def allocate_output(self, programme_id, output_id, expected):
        with self.lock, self._scope(programme_id) as handle:
            require(output_id == STOCK_OUTPUT, "unsupported_output", "No verified independent adapter output is registered for this ID.")
            self._check_expected(programme_id, handle, expected)
            lease = self.state["outputs"][STOCK_OUTPUT]
            if lease["programmeId"] != programme_id or lease["state"] != "held":
                self._allocate(programme_id)
                self._persist()
            return deepcopy(lease)

    def reconcile_output(self, programme_id, output_id, lease_epoch, *, observed_session_id, stopped, retain=False):
        """Trusted owner's explicit readback reconciliation; never query/stop audio.

        ``stopped=True`` is required to release. Retaining requires the observed
        owner session to match this exact attached harness. Call only after real
        adapter readback or explicit operator inspection, never model inference.
        """
        with self.lock, self._scope(programme_id) as handle:
            require(output_id == STOCK_OUTPUT, "unsupported_output", "Unknown output resource.")
            lease = self.state["outputs"][output_id]
            require(type(lease_epoch) is int and lease_epoch == lease["epoch"] and lease["programmeId"] == programme_id and lease["state"] in {"held", "uncertain"}, "stale_lease", "This caller no longer owns the fenced output lease.")
            require(type(stopped) is bool and type(retain) is bool and observed_session_id == handle.session.snapshot()["id"] == lease["sessionId"], "invalid_reconciliation", "Reconcile against this exact native session's observed owner.")
            require(retain or stopped, "output_not_stopped", "Confirm this output stopped before releasing its ownership.")
            lease["epoch"] += 1
            if retain:
                lease["state"] = "held"
            else:
                lease.update(programmeId=None, sessionId=None, state="available")
            self.state["programmes"][programme_id]["revision"] += 1
            self._persist()
            return deepcopy(lease)

    def command(self, client_id, programme_id, command_id, action, arguments, expected):
        """Metadata-only admission; no playback/start/model/grant action exists."""
        with self.lock, self._scope(programme_id) as handle:
            require(identifier(client_id) and identifier(command_id), "invalid_id", "Use bounded client and command IDs.")
            require(isinstance(action, str) and action in ACTIONS and isinstance(arguments, dict), "unsupported_action", "Only programme metadata commands are available in this stage.")
            if action == "plan_queue":
                ids = arguments.get("ids")
                require(set(arguments) == {"ids"} and isinstance(ids, list) and 2 <= len(ids) <= 100 and all(isinstance(tid, str) and re.fullmatch(r"virtualdj:[a-f0-9]{64}", tid) for tid in ids), "invalid_arguments", "Choose 2–100 exact catalog recording IDs.")
            elif action == "set_brief":
                require(set(arguments) == {"text"} and isinstance(arguments["text"], str) and 1 <= len(arguments["text"].strip()) <= 1600, "invalid_arguments", "Use 1–1600 characters for this programme's brief.")
            else:
                require(not arguments, "invalid_arguments", "Manual takeover takes no extra arguments.")
            try:
                fingerprint = hashlib.sha256(canonical([action, arguments, expected]).encode()).hexdigest()
            except (ValueError, TypeError) as error:
                raise ProgrammeError("invalid_context", "Use JSON-safe command context.") from error
            key = (client_id, programme_id, command_id)
            for receipt in self.state["commands"]:
                if (receipt["clientId"], receipt["programmeId"], receipt["commandId"]) == key:
                    require(receipt["fingerprint"] == fingerprint, "idempotency_conflict", "This command ID already names different programme work.")
                    return deepcopy(receipt)
            self._check_expected(programme_id, handle, expected)
            lease = self.state["outputs"][STOCK_OUTPUT]
            require(lease["programmeId"] != programme_id or lease["state"] != "uncertain", "output_uncertain", "Reconcile this programme's uncertain output before new edits.")
            snapshot = self._idle(handle)
            require(len(self.state["commands"]) < MAX_COMMANDS, "command_limit", "Receipt capacity reached; no identities were evicted and no command ran.")
            receipt = {"clientId": client_id, "programmeId": programme_id, "commandId": command_id, "action": action,
                       "fingerprint": fingerprint, "sessionId": snapshot["id"], "status": "dispatching", "result": None}
            self.state["commands"].append(receipt)
            self._persist()  # Durable claim precedes every harness mutation.
            native_id = hashlib.sha256(canonical(key).encode()).hexdigest()
            try:
                if action == "plan_queue":
                    handle.session.plan(list(arguments["ids"]), seconds=snapshot.get("secondsPerTrack", 30), excerpt_bars=snapshot.get("excerptBars", 16), max_tempo_shift=snapshot.get("maxTempoShift", 8), mix_style=snapshot.get("mixStyle", "phrase"), source="operator")
                elif action == "set_brief":
                    handle.agent.settings_action(native_id, {"kind": "brief", "value": arguments["text"]}, handle.agent.settings.expected(snapshot, handle.agent.state["revision"]))
                else:
                    from afterhours import copilot_commands
                    handle.agent.copilot_action("off", native_id, copilot_commands.context(snapshot))
                receipt.update(status="completed", result={"applied": True, "harnessReceiptId": native_id})
            except ValueError:
                receipt.update(status="rejected", result={"applied": False, "code": "harness_rejected"})
            except Exception:
                receipt.update(status="uncertain", result={"applied": None, "code": "harness_outcome_unknown"})
            self.state["programmes"][programme_id]["revision"] += 1
            self._persist()
            return deepcopy(receipt)

    def close(self):
        """Release metadata ownership only. Never stop/start injected harnesses."""
        # An in-flight command retains both the mutation lock and file owner
        # until its final durable outcome. No replacement can fence the file
        # while this owner still has a write to complete.
        with self.lock:
            self._closed = True
            owner, self._owner = self._owner, None
            if owner is not None:
                os.close(owner)
