"""Programme ownership using the real existing harness; no live capabilities."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from afterhours.dj_agent import DJAgent
from afterhours.record_crate import RecordCrate
from afterhours.recording_availability import RecordingAvailability
from afterhours.vdj_session import DJSession
from afterhours.music_programmes import MusicProgrammeRegistry, ProgrammeError, ProgrammeHandle, STOCK_OUTPUT


class Forbidden:
    def __call__(self, *args, **kwargs):
        raise AssertionError("Live I/O, model evaluation and key lookup are forbidden")
    def __getattr__(self, name):
        return self


class MusicProgrammeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "programmes.json"
        self.created, self.registries = [], []
        self.forbidden = [patch("socket.socket", Forbidden()), patch("afterhours.ai_mix.api_key", Forbidden())]
        for guard in self.forbidden:
            guard.start()

    def tearDown(self):
        for registry in self.registries:
            registry.close()
        for handle in self.created:
            handle.session.crate.close()
        for guard in self.forbidden:
            guard.stop()
        self.directory.cleanup()

    def factory(self, pid):
        session = DJSession(threading.Lock(), client_factory=Forbidden(), crate=RecordCrate(),
                            recordings=Forbidden(), availability=RecordingAvailability())
        pool = [{"id": "virtualdj:" + hashlib.sha256(f"{pid}/{i}".encode()).hexdigest(),
                 "title": f"Synthetic {pid} {i}", "artist": "Fixture", "bpm": 120 + i} for i in range(3)]
        session.update(candidates=pool)
        session.plan([row["id"] for row in pool], source="operator")
        handle = ProgrammeHandle(session, DJAgent(session, evaluator=Forbidden(), clock=lambda: 100000.))
        self.created.append(handle)
        return handle

    def registry(self, persistent=False):
        value = MusicProgrammeRegistry(self.path if persistent else None, factory=self.factory)
        self.registries.append(value)
        return value

    def pair(self, persistent=False):
        registry = self.registry(persistent)
        ember = self.factory("ember")
        registry.adopt_default(ember.session, ember.agent, "ember")
        registry.add_programme("tide")
        return registry, ember, registry.handles["tide"]

    def act(self, registry, pid, action, args, cid="request", client="operator", expected=None):
        return registry.command(client, pid, cid, action, args, registry.expected_context(pid) if expected is None else expected)

    def error(self, code, fn):
        with self.assertRaises(ProgrammeError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_adopt_default_keeps_exact_live_objects_queue_and_session(self):
        handle = self.factory("existing")
        handle.session.update(active=True, phase="playing", telemetry={"decks": [{"playing": True}]})
        before = handle.session.snapshot()
        order_object = handle.session.state["order"]
        registry = self.registry(True)
        reading = registry.adopt_default(handle.session, handle.agent)
        self.assertIs(registry.handles["default"].session, handle.session)
        self.assertIs(registry.handles["default"].agent, handle.agent)
        self.assertIs(handle.session.state["order"], order_object)
        self.assertEqual(handle.session.snapshot(), before)
        self.assertEqual(reading["sessionId"], before["id"])
        self.assertEqual(reading["outputStatus"], "held")
        self.assertFalse(reading["liveTransportAvailable"])
        self.error("live_transport_unsupported", lambda: self.act(registry, "default", "manual_takeover", {}))

    def test_extra_programme_is_idle_unassigned_and_cannot_share_master(self):
        registry, _, _ = self.pair()
        self.assertEqual(registry.read("tide")["outputStatus"], "unassigned")
        self.error("output_busy", lambda: registry.allocate_output("tide", STOCK_OUTPUT, registry.expected_context("tide")))
        self.error("unsupported_output", lambda: registry.allocate_output("tide", "invented/tide", registry.expected_context("tide")))
        self.assertEqual(registry.capabilities()["outputs"][0]["independentProgrammeCapacity"], 1)

    def test_initial_default_adoption_publishes_ownership_in_one_atomic_record(self):
        registry = self.registry(True)
        handle = self.factory("existing")
        persisted = []
        original = registry._persist
        def capture():
            persisted.append(deepcopy(registry.state))
            original()
        with patch.object(registry, "_persist", side_effect=capture):
            registry.adopt_default(handle.session, handle.agent)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["defaultProgrammeId"], "default")
        self.assertEqual(persisted[0]["outputs"][STOCK_OUTPUT]["programmeId"], "default")
        self.assertEqual(persisted[0]["outputs"][STOCK_OUTPUT]["state"], "held")

    def test_queue_brief_same_id_and_receipts_are_programme_scoped(self):
        registry, ember, tide = self.pair()
        tide_before = tide.session.snapshot()
        ids = [row["id"] for row in reversed(ember.session.state["order"])]
        self.assertEqual(self.act(registry, "ember", "plan_queue", {"ids": ids})["status"], "completed")
        self.assertEqual(tide.session.snapshot(), tide_before)
        self.assertEqual(registry.read("ember")["queueIds"], ids)
        args = {"text": "A soulful Ember set"}
        first = self.act(registry, "ember", "set_brief", args, cid="same")
        second = self.act(registry, "tide", "set_brief", {"text": "A minimal Tide set"}, cid="same")
        self.assertEqual((first["status"], second["status"]), ("completed", "completed"))
        self.assertNotEqual(first["result"]["harnessReceiptId"], second["result"]["harnessReceiptId"])
        self.assertEqual(ember.agent.state["direction"], args["text"])
        self.assertEqual(tide.agent.state["direction"], "A minimal Tide set")
        self.assertEqual((ember.agent.remaining, tide.agent.remaining), (0, 0))

    def test_exact_retry_is_idempotent_and_clients_have_independent_identities(self):
        registry, ember, _ = self.pair()
        expected = registry.expected_context("ember")
        args = {"text": "Patient house"}
        first = self.act(registry, "ember", "set_brief", args, expected=expected)
        revision = ember.agent.state["revision"]
        self.assertEqual(self.act(registry, "ember", "set_brief", args, expected=expected), first)
        self.assertEqual(ember.agent.state["revision"], revision)
        self.error("idempotency_conflict", lambda: self.act(registry, "ember", "set_brief", {"text": "Changed"}, expected=expected))
        self.assertEqual(self.act(registry, "ember", "set_brief", {"text": "Other client"}, client="other")["status"], "completed")

    def test_cross_programme_stale_queue_and_external_direction_are_rejected(self):
        registry, ember, tide = self.pair()
        expected = registry.expected_context("ember")
        self.error("stale_context", lambda: self.act(registry, "tide", "set_brief", {"text": "Wrong target"}, expected=expected))
        ember.session.plan([row["id"] for row in reversed(ember.session.state["order"])], source="operator")
        self.error("stale_context", lambda: self.act(registry, "ember", "set_brief", {"text": "Stale queue"}, expected=expected))
        expected = registry.expected_context("ember")
        ember.agent.settings_action("external", {"kind": "brief", "value": "Externally revised"}, ember.agent.settings.expected(ember.session.snapshot(), ember.agent.state["revision"]))
        self.error("stale_context", lambda: self.act(registry, "ember", "set_brief", {"text": "Stale direction"}, expected=expected))
        self.assertEqual(tide.agent.state["direction"], "")

    def test_manual_takeover_uses_existing_harness_without_touching_other_programme(self):
        registry, ember, tide = self.pair()
        before = tide.session.snapshot(), deepcopy(tide.agent.settings.state)
        result = self.act(registry, "ember", "manual_takeover", {})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(ember.session.state["copilot"]["mode"], "off")
        self.assertEqual(ember.session.state["phase"], "takeover")
        self.assertEqual((tide.session.snapshot(), tide.agent.settings.state), before)

    def test_simultaneous_master_allocation_has_one_winner(self):
        registry = self.registry(True)
        for pid in ("ember", "tide"):
            handle = self.factory(pid)
            registry.attach(pid, handle.session, handle.agent)
        barrier = threading.Barrier(2)
        def allocate(pid):
            expected = registry.expected_context(pid)
            barrier.wait()
            try:
                return registry.allocate_output(pid, STOCK_OUTPUT, expected)["programmeId"]
            except ProgrammeError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(allocate, ("ember", "tide")))
        self.assertEqual(results.count("output_busy"), 1)
        self.assertEqual(sum(result in ("ember", "tide") for result in results), 1)
        stored = json.loads(self.path.read_text())["outputs"][STOCK_OUTPUT]
        self.assertEqual(stored["state"], "held")

    def test_restart_fences_lease_and_requires_explicit_owner_reconciliation(self):
        registry, ember, tide = self.pair(True)
        before = registry.read("ember")["output"]
        registry.close()
        reopened = self.registry(True)
        reopened.adopt_default(ember.session, ember.agent, "ember")
        reopened.attach("tide", tide.session, tide.agent)
        lease = reopened.read("ember")["output"]
        self.assertEqual(lease["state"], "uncertain")
        self.assertGreater(lease["epoch"], before["epoch"])
        self.error("output_uncertain", lambda: self.act(reopened, "ember", "set_brief", {"text": "Wait for readback"}))
        self.error("stale_lease", lambda: reopened.reconcile_output("ember", STOCK_OUTPUT, before["epoch"], observed_session_id=ember.session.state["id"], stopped=True))
        self.error("invalid_reconciliation", lambda: reopened.reconcile_output("ember", STOCK_OUTPUT, lease["epoch"], observed_session_id=tide.session.state["id"], stopped=True))
        self.error("output_not_stopped", lambda: reopened.reconcile_output("ember", STOCK_OUTPUT, lease["epoch"], observed_session_id=ember.session.state["id"], stopped=False))
        held = reopened.reconcile_output("ember", STOCK_OUTPUT, lease["epoch"], observed_session_id=ember.session.state["id"], stopped=False, retain=True)
        self.assertEqual(held["state"], "held")
        released = reopened.reconcile_output("ember", STOCK_OUTPUT, held["epoch"], observed_session_id=ember.session.state["id"], stopped=True)
        self.assertEqual(released["state"], "available")
        other = reopened.allocate_output("tide", STOCK_OUTPUT, reopened.expected_context("tide"))
        self.assertGreater(other["epoch"], held["epoch"])
        self.error("stale_lease", lambda: reopened.reconcile_output("ember", STOCK_OUTPUT, held["epoch"], observed_session_id=ember.session.state["id"], stopped=True))

    def test_crash_after_harness_mutation_is_uncertain_and_never_replayed(self):
        registry, ember, _ = self.pair(True)
        expected = registry.expected_context("ember")
        args = {"text": "Applied before simulated process loss"}
        original = ember.agent.settings_action
        def crash(*values):
            original(*values)
            raise SystemExit("synthetic crash")
        with patch.object(ember.agent, "settings_action", side_effect=crash):
            with self.assertRaises(SystemExit):
                self.act(registry, "ember", "set_brief", args, expected=expected)
        self.assertEqual(json.loads(self.path.read_text())["commands"][-1]["status"], "dispatching")
        registry.close()
        reopened = self.registry(True)
        reopened.attach("ember", ember.session, ember.agent)
        with patch.object(ember.agent, "settings_action", Forbidden()):
            retry = self.act(reopened, "ember", "set_brief", args, expected=expected)
        self.assertEqual(retry["status"], "uncertain")
        self.assertEqual(ember.agent.state["direction"], args["text"])

    def test_real_restart_new_harness_requires_explicit_stopped_rebind(self):
        registry, ember, _ = self.pair(True)
        old_id = ember.session.state["id"]
        old_context = registry.expected_context("ember")
        old_epoch = registry.read("ember")["output"]["epoch"]
        queue_ids = registry.read("ember")["queueIds"]
        registry.close()
        reopened = self.registry(True)
        recovered = self.factory("ember")  # Fresh process objects, same fixture queue.
        self.assertNotEqual(recovered.session.state["id"], old_id)
        self.error("session_changed", lambda: reopened.attach("ember", recovered.session, recovered.agent))
        self.error("invalid_reconciliation", lambda: reopened.rebind_recovered("ember", recovered.session, recovered.agent, prior_session_id=old_id, observed_stopped=False))
        view = reopened.rebind_recovered("ember", recovered.session, recovered.agent, prior_session_id=old_id, observed_stopped=True)
        self.assertEqual(view["queueIds"], queue_ids)
        self.assertEqual(view["outputStatus"], "uncertain")
        self.assertGreater(view["output"]["epoch"], old_epoch)
        self.error("stale_context", lambda: self.act(reopened, "ember", "set_brief", {"text": "Old owner"}, expected=old_context))
        lease = reopened.reconcile_output("ember", STOCK_OUTPUT, view["output"]["epoch"], observed_session_id=recovered.session.state["id"], stopped=True, retain=True)
        self.assertEqual(lease["state"], "held")
        self.assertEqual(self.act(reopened, "ember", "set_brief", {"text": "Recovered owner"})["status"], "completed")

    def test_storage_failure_after_effect_retains_claim_and_blocks_new_work(self):
        registry, ember, _ = self.pair(True)
        expected = registry.expected_context("ember")
        original_replace = __import__("os").replace
        calls = []
        def replace(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("synthetic disk failure after local effect")
            return original_replace(source, target)
        with patch("afterhours.music_programmes.os.replace", side_effect=replace):
            with self.assertRaises(OSError):
                self.act(registry, "ember", "set_brief", {"text": "Applied but receipt save failed"}, expected=expected)
        self.assertEqual(json.loads(self.path.read_text())["commands"][-1]["status"], "dispatching")
        self.error("registry_unavailable", lambda: registry.expected_context("ember"))
        registry.close()
        reopened = self.registry(True)
        reopened.attach("ember", ember.session, ember.agent)
        self.assertEqual(reopened.read("ember")["receipts"][-1]["status"], "uncertain")

    def test_close_waits_for_inflight_command_before_releasing_file_owner(self):
        registry, ember, _ = self.pair(True)
        expected = registry.expected_context("ember")
        old_epoch = registry.read("ember")["output"]["epoch"]
        entered, release = threading.Event(), threading.Event()
        close_attempted, close_finished = threading.Event(), threading.Event()
        results, errors = {}, []
        original_lock, original_action = registry.lock, ember.agent.settings_action

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "programme-close":
                    close_attempted.set()  # Before attempting the held lock.
                original_lock.acquire()
                return self
            def __exit__(self, *args):
                original_lock.release()

        registry.lock = ObservedLock()
        def gated_action(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release the gated local command")
            return original_action(*args)
        def command():
            try:
                results["receipt"] = self.act(registry, "ember", "set_brief", {"text": "Finish before ownership release"}, expected=expected)
            except BaseException as error:
                errors.append(error)
        def close():
            try:
                registry.close()
                close_finished.set()
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=command, name="programme-command")
        closer = threading.Thread(target=close, name="programme-close")
        with patch.object(ember.agent, "settings_action", side_effect=gated_action):
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                closer.start()
                self.assertTrue(close_attempted.wait(3))
                self.assertFalse(close_finished.is_set())
                self.error("owner_active", lambda: MusicProgrammeRegistry(self.path))
                pending = json.loads(self.path.read_text())
                self.assertEqual(pending["commands"][-1]["status"], "dispatching")
                self.assertEqual(pending["outputs"][STOCK_OUTPUT]["epoch"], old_epoch)
            finally:
                release.set()
                worker.join(3)
                if closer.ident is not None:
                    closer.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(close_finished.is_set())
        self.assertEqual(results["receipt"]["status"], "completed")

        reopened = self.registry(True)
        recovered = json.loads(self.path.read_text())
        self.assertGreater(recovered["outputs"][STOCK_OUTPUT]["epoch"], old_epoch)
        self.assertEqual(recovered["outputs"][STOCK_OUTPUT]["state"], "uncertain")
        self.assertEqual(recovered["commands"][-1]["status"], "completed")
        self.error("registry_unavailable", registry._persist)
        self.error("registry_unavailable", lambda: self.act(registry, "ember", "set_brief", {"text": "Closed owner"}, cid="after-close", expected=expected))
        self.assertEqual(json.loads(self.path.read_text()), recovered)
        self.assertIsNotNone(reopened)

    def test_limits_never_evict_receipt_ids_or_create_handles_implicitly(self):
        registry, ember, _ = self.pair()
        with patch("afterhours.music_programmes.MAX_COMMANDS", 1):
            expected = registry.expected_context("ember")
            first = self.act(registry, "ember", "set_brief", {"text": "One"}, expected=expected)
            self.error("command_limit", lambda: self.act(registry, "ember", "set_brief", {"text": "Two"}, cid="next"))
            self.assertEqual(self.act(registry, "ember", "set_brief", {"text": "One"}, expected=expected), first)
        with patch("afterhours.music_programmes.MAX_PROGRAMMES", 2), patch.object(registry, "factory", Forbidden()):
            self.error("programme_limit", lambda: registry.add_programme("third"))
        plain = MusicProgrammeRegistry()
        self.registries.append(plain)
        self.error("factory_required", lambda: plain.add_programme("new"))
        self.error("programme_unattached", lambda: plain.read("missing"))

    def test_invalid_inputs_and_shared_or_changed_harness_are_not_admitted(self):
        registry, ember, _ = self.pair()
        self.error("shared_harness", lambda: registry.attach("alias", ember.session, ember.agent))
        for action, args in (("start", {}), ("prepare", {}), ("set_brief", {"text": ""}), ("plan_queue", {"ids": ["wrong", "ids"]}), ("manual_takeover", {"allowPaid": True})):
            with self.subTest(action=action):
                self.error("unsupported_action" if action in ("start", "prepare") else "invalid_arguments", lambda: self.act(registry, "ember", action, args))
        self.error("invalid_id", lambda: self.act(registry, "ember", "set_brief", {"text": "Fine"}, client="x" * 81))
        ember.session.new_set()
        self.error("session_changed", lambda: registry.read("ember"))

    def test_second_owner_and_unsafe_or_unknown_metadata_are_rejected(self):
        registry = self.registry(True)
        self.error("owner_active", lambda: MusicProgrammeRegistry(self.path))
        registry.close()
        self.path.write_text('{"schemaVersion":99}')
        self.error("invalid_metadata", lambda: MusicProgrammeRegistry(self.path))
        self.path.unlink()
        self.path.symlink_to(Path(self.directory.name) / "elsewhere")
        self.error("unsafe_storage", lambda: MusicProgrammeRegistry(self.path))

    def test_duplicate_or_oversized_metadata_fails_without_constructing_harnesses(self):
        self.path.write_text('{"schemaVersion":1,"schemaVersion":1}')
        self.error("invalid_metadata", lambda: MusicProgrammeRegistry(self.path, factory=Forbidden()))
        self.path.write_bytes(b" " * (1024 * 1024 + 1))
        self.error("metadata_limit", lambda: MusicProgrammeRegistry(self.path, factory=Forbidden()))

    def test_readbacks_are_detached_and_do_not_expose_private_harness_snapshots(self):
        registry, ember, _ = self.pair()
        ember.session.state["privateLocator"] = "/private/secret"
        view = registry.read("ember")
        view["queueIds"].clear()
        view["expected"]["agentContext"].clear()
        self.assertEqual(len(registry.read("ember")["queueIds"]), 3)
        self.assertNotIn("privateLocator", registry.read("ember"))
        self.assertFalse(registry.capabilities()["createsAllowances"])
        self.assertFalse(registry.capabilities()["modelRequests"])


if __name__ == "__main__":
    unittest.main()
