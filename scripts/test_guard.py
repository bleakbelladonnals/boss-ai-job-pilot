#!/usr/bin/env python3
"""Behavioral checks against temporary state only; no browser or network access."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import guard


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.clock = patch.object(guard.time, "time", return_value=1000)
        self.time = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.assertTrue(self.configure("a")["ok"])
        self.assertTrue(self.configure("b")["ok"])

    def call(self, op, **kwargs):
        return guard.execute({"op": op, **kwargs}, self.directory)

    def configure(self, key, **kwargs):
        data = dict(accountKey=key, profileId=f"profile-{key}", identity=f"User {key} / masked 13****{key}",
                    greeting=f"Approved greeting for {key}\nhttps://example.com/{key}\n",
                    filters={"city": "北京", "salaryMinK": 10, "salaryMaxK": 40},
                    approved=True, approvalEvidence="User approved this account and complete text")
        return self.call("configure", **(data | kwargs))

    def observed(self, key="a", **kwargs):
        return dict(accountKey=key, profileId=f"profile-{key}", identity=f"User {key} / masked 13****{key}",
                    spaceId=f"space-{key}", city="北京") | kwargs

    def start(self, key="a", **kwargs):
        return self.call("start", **(dict(accountKey=key, spaceId=f"space-{key}", target=2,
                                         observed=self.observed(key), approved=True,
                                         approvalEvidence="User explicitly requested 2 sends now") | kwargs))

    def prepare(self, run, key="a", suffix="1", **kwargs):
        return self.call("prepare", **(dict(runId=run, observed=self.observed(key), recruiterId=f"recruiter-{suffix}",
                                           jobUrl=f"https://www.zhipin.com/job_detail/job{suffix}.html?track=removed",
                                           candidateCity="北京", checksPassed=True, historyChecked=True,
                                           evidence="All filters passed; full history loaded with no prior outreach") | kwargs))

    def resolve(self, run, attempt, result="confirmed", key="a", **kwargs):
        return self.call("resolve", **(dict(runId=run, attemptId=attempt, result=result,
                                           observed=self.observed(key), evidence="Full approved message visible with delivered status") | kwargs))

    def resume(self, run, key="a", **kwargs):
        return self.call("resume", **(dict(runId=run, observed=self.observed(key), approved=True,
                                          approvalEvidence="User explicitly resumed after verifying the page") | kwargs))

    def assertError(self, result, code):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"], code, result)

    def test_same_account_cannot_start_twice(self):
        self.assertTrue(self.start()["ok"])
        self.assertError(self.start(parallel=True), "RUN_EXISTS")

    def test_profile_and_identity_cannot_be_reused_or_rebound(self):
        self.assertError(self.configure("c", profileId="profile-a"), "PROFILE_ALREADY_BOUND")
        self.assertError(self.configure("c", identity=self.observed()["identity"]), "IDENTITY_ALREADY_BOUND")
        self.assertError(self.configure("a", profileId="replacement"), "BINDING_IMMUTABLE")
        self.assertError(self.configure("a", identity="different person"), "BINDING_IMMUTABLE")

    def test_independent_accounts_start_and_prepare_concurrently_by_default(self):
        run_a = self.start()["runId"]
        run_b = self.start("b")["runId"]
        self.assertTrue(self.prepare(run_a)["ok"])
        self.assertTrue(self.prepare(run_b, key="b")["ok"])
        self.assertError(self.prepare(run_a, suffix="2"), "PENDING")
        self.assertError(self.prepare(run_b, key="b", suffix="2"), "PENDING")

    def test_obsolete_parallel_request_is_ignored_and_not_persisted(self):
        run_a = self.start(parallel=False)
        run_b = self.start("b", parallel=False)
        self.assertTrue(run_a["ok"])
        self.assertTrue(run_b["ok"])
        self.assertNotIn("parallel", run_a)
        self.assertEqual(run_a["concurrencyScope"], "account")
        state = json.loads((self.directory / "state.json").read_text())
        self.assertTrue(all("parallel" not in run for run in state["runs"].values()))

    def test_space_cannot_be_shared_even_in_parallel(self):
        self.start(parallel=True)
        self.assertError(self.start("b", parallel=True, spaceId="space-a",
                                    observed=self.observed("b", spaceId="space-a")), "SPACE_IN_USE")

    def test_halt_releases_execution_but_not_account_stop(self):
        run_a = self.start()["runId"]
        self.call("halt", runId=run_a, reason="User took over the browser; stopped all active commands")
        run_b = self.start("b")["runId"]
        self.assertError(self.start(), "RUN_EXISTS")
        self.assertError(self.prepare(run_a), "HALTED")
        self.assertError(self.call("finish", runId=run_a, reason="Try closing stopped batch"), "APPROVAL_REQUIRED")
        self.assertTrue(self.resume(run_a)["ok"])
        self.assertEqual(self.call("status", runId=run_b)["runs"][run_b]["status"], "active")
        self.call("finish", runId=run_b, reason="User stopped before sending")

    def test_unknown_cannot_retry_or_disappear_after_process_exit(self):
        run = self.start()["runId"]
        attempt = self.prepare(run)["attemptId"]
        unknown = self.resolve(run, attempt, "unknown", evidence="Click happened; result missing")
        self.assertEqual(unknown["status"], "halted")
        self.assertError(self.prepare(run), "HALTED")
        self.assertError(self.resume(run), "PENDING")
        self.assertError(self.start(), "RUN_EXISTS")
        self.assertError(self.call("finish", runId=run, reason="End"), "PENDING")
        self.assertError(self.resolve(run, attempt, "not_sent", clicked=False), "CANNOT_ASSERT_NOT_SENT")
        status = self.cli({"op": "status", "runId": run})
        self.assertEqual(status["attempts"][attempt]["result"], "unknown")
        self.assertEqual(status["accounts"]["a"]["pending"], [attempt])
        run_b = self.start("b")["runId"]
        self.assertTrue(self.prepare(run_b, key="b")["ok"])

    def test_legacy_halted_run_resumes_beside_active_account_without_resetting_state(self):
        run_a = self.start()["runId"]
        run_b = self.start("b", target=3)["runId"]
        attempt = self.prepare(run_b, key="b")["attemptId"]
        self.resolve(run_b, attempt, key="b")
        self.call("halt", runId=run_b, reason="Old global serial mode paused this account")
        path = self.directory / "state.json"
        legacy = json.loads(path.read_text())
        legacy["runs"][run_a]["parallel"] = False
        legacy["runs"][run_b]["parallel"] = False
        path.write_text(json.dumps(legacy), encoding="utf-8")
        before = path.read_bytes()
        status = self.call("status")
        self.assertEqual(status["concurrencyScope"], "account")
        self.assertNotIn("parallel", status["runs"][run_b])
        self.assertEqual(path.read_bytes(), before)  # Read-only compatibility, no migration.
        resumed = self.resume(run_b, key="b")
        self.assertTrue(resumed["ok"])
        self.assertEqual((resumed["runId"], resumed["sent"], resumed["target"], resumed["pending"]), (run_b, 1, 3, None))
        current = json.loads(path.read_text())
        self.assertEqual(current["accounts"]["b"]["nextAllowedAt"], legacy["accounts"]["b"]["nextAllowedAt"])
        self.assertEqual(current["attempts"], legacy["attempts"])
        self.assertIs(current["runs"][run_b]["parallel"], False)
        self.assertEqual(current["runs"][run_a]["status"], "active")
        self.assertError(self.prepare(run_b, key="b", suffix="2"), "COOLDOWN")
        self.assertTrue(self.prepare(run_a)["ok"])  # B cooldown does not affect A.
        self.time.return_value = 1021
        next_attempt = self.prepare(run_b, key="b", suffix="2")["attemptId"]
        self.call("halt", runId=run_b, reason="Interrupted with a pending send")
        self.assertError(self.resume(run_b, key="b"), "PENDING")
        self.assertEqual(self.call("status", runId=run_b)["accounts"]["b"]["pending"], [next_attempt])

    def test_crash_after_prepare_keeps_sending_pending(self):
        run = self.start()["runId"]
        attempt = self.prepare(run)["attemptId"]
        self.assertEqual(self.cli({"op": "status", "runId": run})["attempts"][attempt]["result"], "sending")
        self.assertError(self.prepare(run, suffix="2"), "PENDING")
        self.assertError(self.start(), "RUN_EXISTS")
        self.call("halt", runId=run, reason="Process interrupted and terminated")
        self.assertError(self.resume(run), "PENDING")
        self.assertTrue(self.resolve(run, attempt)["ok"])
        self.assertTrue(self.resume(run)["ok"])

    def test_verified_presubmit_misclassification_can_be_corrected_without_auto_resuming(self):
        run = self.start(target=3)["runId"]
        first = self.prepare(run)["attemptId"]
        self.resolve(run, first)
        self.time.return_value = 1021
        attempt = self.prepare(run, suffix="2")["attemptId"]
        original_evidence = "Entry verified empty; sendClicked=false; Input verification failed before send"
        self.resolve(run, attempt, "unknown", evidence=original_evidence)
        path = self.directory / "state.json"
        before = json.loads(path.read_text())
        self.time.return_value = 1030
        correction = {"executionEvidence": "Preserved executor trace and source show failure before submission invocation; executor has stopped",
                      "entryEvidence": "Entry checkpoint matched recipient with fully loaded history, no outgoing/default message and unsent draft"}
        result = self.resolve(run, attempt, "not_sent", clicked=False, executionStopped=True,
                              preSubmitCorrection=correction, evidence="Original pre-submit error was misclassified; independent execution and entry evidence reviewed")
        self.assertTrue(result["ok"])
        self.assertEqual((result["sent"], result["target"], result["status"]), (1, 3, "halted"))
        after = json.loads(path.read_text())
        self.assertEqual(after["attempts"][attempt]["result"], "not_sent")
        audit = after["attempts"][attempt]["preSubmitCorrection"]
        self.assertEqual((audit["previousResult"], audit["previousEvidence"], audit["previousResolvedAt"]),
                         ("unknown", original_evidence, before["attempts"][attempt]["resolvedAt"]))
        self.assertEqual(audit["executionEvidence"], correction["executionEvidence"])
        self.assertEqual(audit["entryEvidence"], correction["entryEvidence"])
        self.assertEqual(after["accounts"]["a"]["nextAllowedAt"], before["accounts"]["a"]["nextAllowedAt"])
        self.assertEqual(after["attempts"][first], before["attempts"][first])
        self.assertIsNone(after["runs"][run]["pending"])
        self.assertError(self.prepare(run, suffix="2"), "HALTED")

    def test_unknown_correction_rejects_missing_proof_or_any_submission_invocation(self):
        run = self.start()["runId"]
        attempt = self.prepare(run)["attemptId"]
        self.resolve(run, attempt, "unknown", evidence="Send outcome unverified")
        proof = {"executionEvidence": "Executor trace proves no submission invocation",
                 "entryEvidence": "Entry checkpoint proves no outgoing/default message"}
        cases = [
            ({"clicked": False}, "CANNOT_ASSERT_NOT_SENT"),
            ({"clicked": True, "executionStopped": True, "preSubmitCorrection": proof}, "CANNOT_ASSERT_NOT_SENT"),
            ({"clicked": False, "preSubmitCorrection": proof}, "CANNOT_ASSERT_NOT_SENT"),
            ({"clicked": False, "executionStopped": True}, "CANNOT_ASSERT_NOT_SENT"),
            ({"clicked": False, "executionStopped": True, "preSubmitCorrection": {"entryEvidence": proof["entryEvidence"]}}, "INVALID_INPUT"),
            ({"clicked": False, "executionStopped": True, "preSubmitCorrection": {"executionEvidence": proof["executionEvidence"]}}, "INVALID_INPUT"),
            ({"clicked": False, "executionStopped": True, "preSubmitCorrection": proof | {"executionEvidence": " "}}, "INVALID_INPUT"),
            ({"clicked": False, "executionStopped": True, "preSubmitCorrection": proof | {"entryEvidence": " "}}, "INVALID_INPUT"),
        ]
        for kwargs, code in cases:
            with self.subTest(kwargs=kwargs):
                self.assertError(self.resolve(run, attempt, "not_sent", **kwargs), code)
                status = self.call("status", runId=run)
                self.assertEqual(status["attempts"][attempt]["result"], "unknown")
                self.assertEqual(status["accounts"]["a"]["pending"], [attempt])

    def test_unknown_correction_still_requires_fresh_matching_identity(self):
        run = self.start()["runId"]
        attempt = self.prepare(run)["attemptId"]
        self.resolve(run, attempt, "unknown", evidence="Pre-submit error")
        self.assertError(self.resolve(run, attempt, "not_sent", clicked=False, executionStopped=True,
                                      preSubmitCorrection={"executionEvidence": "No submission invoked", "entryEvidence": "Entry created no message"},
                                      observed=self.observed(identity="different identity")), "IDENTITY_MISMATCH")
        self.assertEqual(self.call("status", runId=run)["accounts"]["a"]["pending"], [attempt])

    def test_late_confirmation_counts_original_run_once_and_does_not_resume(self):
        run = self.start(target=1)["runId"]
        attempt = self.prepare(run)["attemptId"]
        self.resolve(run, attempt, "unknown")
        confirmed = self.resolve(run, attempt)
        self.assertEqual((confirmed["sent"], confirmed["status"]), (1, "halted"))
        replay = self.resolve(run, attempt)
        self.assertEqual(replay["sent"], 1)
        self.assertTrue(replay["alreadyResolved"])
        self.resume(run)
        self.time.return_value = 1021
        self.assertError(self.prepare(run, suffix="2"), "QUOTA_REACHED")

    def test_persistent_cooldown_across_batches_and_greeting_changes(self):
        run = self.start(target=1)["runId"]
        prepared = self.prepare(run)
        self.assertEqual(prepared["greeting"], "Approved greeting for a\nhttps://example.com/a\n")
        self.time.return_value = 1010
        self.resolve(run, prepared["attemptId"])
        self.call("finish", runId=run, reason="Quota complete")
        self.configure("a", greeting="Updated approved text")
        second = self.start()["runId"]
        self.assertError(self.prepare(second, suffix="2"), "COOLDOWN")
        self.time.return_value = 1031
        self.assertError(self.prepare(second, jobUrl="https://www.zhipin.com/job_detail/job1.html?new=tracking"), "DUPLICATE")
        self.assertError(self.prepare(second, suffix="2", recruiterId="recruiter-1"), "DUPLICATE")
        self.assertEqual(self.prepare(second, suffix="2")["greeting"], "Updated approved text")

    def test_config_and_contact_history_are_account_specific(self):
        run_a = self.start(parallel=True)["runId"]
        run_b = self.start("b", parallel=True)["runId"]
        a = self.prepare(run_a)
        self.resolve(run_a, a["attemptId"])
        b = self.prepare(run_b, key="b")
        self.assertTrue(b["ok"])
        self.assertIn("greeting for b", b["greeting"])
        self.assertError(self.configure("a", greeting="Changed mid-run"), "RUN_EXISTS")
        status = json.dumps(self.call("status"))
        self.assertNotIn("Approved greeting", status)
        self.assertNotIn("masked", status)
        self.assertEqual(self.call("status", accountKey="a")["accounts"]["a"]["filters"]["city"], "北京")

    def test_identity_profile_space_city_mismatch_halts(self):
        for field in ("identity", "profileId", "spaceId", "city", "accountKey"):
            with self.subTest(field=field):
                run = self.start()["runId"]
                result = self.prepare(run, observed=self.observed(**{field: "unexpected"}))
                self.assertError(result, "IDENTITY_MISMATCH")
                self.assertEqual(self.call("status", runId=run)["runs"][run]["status"], "halted")
                self.resume(run)
                self.call("finish", runId=run, reason="End mismatch check")

    def test_not_sent_and_explicit_failure_have_distinct_semantics(self):
        run = self.start()["runId"]
        attempt = self.prepare(run)["attemptId"]
        self.assertError(self.resolve(run, attempt, "not_sent"), "CANNOT_ASSERT_NOT_SENT")
        self.assertError(self.resolve(run, attempt, "failed"), "FAILURE_EVIDENCE_REQUIRED")
        self.assertTrue(self.resolve(run, attempt, "not_sent", clicked=False,
                                     evidence="Entry opened only the unsent editor; action log confirms no submission click; unsent draft remains visible")["ok"])
        self.time.return_value = 1021
        again = self.prepare(run)["attemptId"]
        failed = self.resolve(run, again, "failed", failureVisible=True, evidence="Page explicitly says message failed")
        self.assertEqual((failed["sent"], failed["status"]), (0, "halted"))
        self.assertError(self.prepare(run), "HALTED")
        self.resume(run)
        self.time.return_value = 1042
        self.assertTrue(self.prepare(run)["ok"])

    def test_unexpected_message_ends_pending_without_counting_or_auto_resuming(self):
        for initial_result in ("sending", "unknown"):
            with self.subTest(initial_result=initial_result):
                self.time.return_value += 21
                run = self.start()["runId"]
                attempt = self.prepare(run, suffix=initial_result)["attemptId"]
                if initial_result == "unknown":
                    self.resolve(run, attempt, "unknown", evidence="Click occurred, result initially unavailable")
                result = self.resolve(run, attempt, "unexpected_sent", evidence="Full history shows only platform default greeting with delivered marker")
                self.assertEqual((result["sent"], result["status"]), (0, "halted"))
                status = self.call("status", runId=run)
                self.assertEqual(status["accounts"]["a"]["pending"], [])
                self.assertEqual(status["attempts"][attempt]["result"], "unexpected_sent")
                replay = self.resolve(run, attempt, "unexpected_sent", evidence="Same delivered default greeting")
                self.assertEqual(replay["sent"], 0)
                self.assertTrue(replay["alreadyResolved"])
                self.assertError(self.prepare(run, suffix="new"), "HALTED")
                self.assertTrue(self.resume(run)["ok"])
                self.assertError(self.prepare(run, suffix=initial_result), "DUPLICATE")
                self.call("finish", runId=run, reason="User ended batch after reviewing unexpected message")

    def test_explicit_halted_finish_allows_city_change_after_late_delivery(self):
        run = self.start(target=1)["runId"]
        attempt = self.prepare(run)["attemptId"]
        reason = "User stopped old target and changed city"
        self.call("halt", runId=run, reason=reason)
        self.resolve(run, attempt)
        finished = self.call("finish", runId=run, reason="User explicitly ended the old batch", approved=True,
                             approvalEvidence="User ended old batch and requested new city", executionStopped=True,
                             observed=self.observed(city="上海"),
                             evidence="All sending commands terminated; final pending message verified delivered")
        self.assertTrue(finished["ok"])
        self.assertEqual((finished["status"], finished["sent"], finished["haltReason"]), ("finished", 1, reason))
        status = self.call("status", runId=run)
        self.assertIsNone(status["accounts"]["a"]["activeRun"])
        self.assertIsNone(status["accounts"]["a"]["haltReason"])
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["runs"][run]["finishEvidence"], "All sending commands terminated; final pending message verified delivered")
        self.assertTrue(self.configure("a", filters={"city": "上海", "salaryMinK": 10, "salaryMaxK": 40})["ok"])
        self.assertError(self.start(observed=self.observed(city="上海"), approved=False), "APPROVAL_REQUIRED")
        self.assertTrue(self.start(observed=self.observed(city="上海"))["ok"])

    def test_halted_finish_requires_approval_stopped_execution_identity_and_no_pending(self):
        run = self.start()["runId"]
        self.call("halt", runId=run, reason="User interrupted the run")
        request = dict(runId=run, reason="Explicitly end", approved=True, approvalEvidence="User requested end",
                       executionStopped=True, observed=self.observed(city="上海"),
                       evidence="All commands stopped and no unverified sends remain")
        self.assertError(self.call("finish", **(request | {"approved": False})), "APPROVAL_REQUIRED")
        self.assertError(self.call("finish", **(request | {"executionStopped": False})), "EXECUTION_STOP_REQUIRED")
        self.assertError(self.call("finish", **(request | {"observed": self.observed(identity="wrong identity")})), "IDENTITY_MISMATCH")
        self.assertError(self.call("finish", **(request | {"evidence": ""})), "INVALID_INPUT")
        self.assertEqual(self.call("status", runId=run)["runs"][run]["status"], "halted")
        self.resume(run)
        attempt = self.prepare(run)["attemptId"]
        self.resolve(run, attempt, "unknown", evidence="Message may have been sent")
        self.assertError(self.call("finish", **request), "PENDING")
        self.assertEqual(self.call("status", runId=run)["accounts"]["a"]["pending"], [attempt])

    def test_missing_approval_invalid_filters_quota_and_foreign_attempt(self):
        self.assertError(self.configure("c", filters={}), "INVALID_INPUT")
        self.assertError(self.configure("c", approved=False), "APPROVAL_REQUIRED")
        self.assertError(self.configure("c", identity="13812345678"), "INVALID_INPUT")
        self.assertError(self.start(target=True), "INVALID_INPUT")
        self.assertError(self.start(approved=False), "APPROVAL_REQUIRED")
        a = self.start(parallel=True)["runId"]
        b = self.start("b", parallel=True)["runId"]
        attempt = self.prepare(a)["attemptId"]
        self.assertError(self.resolve(b, attempt, key="b"), "ATTEMPT_NOT_FOUND")

    def test_corrupt_state_is_not_reset(self):
        path = self.directory / "state.json"
        path.write_text("{broken", encoding="utf-8")
        result = self.cli({"op": "status"})
        self.assertError(result, "STATE_OR_INPUT_ERROR")
        self.assertEqual(path.read_text(), "{broken")

    def cli(self, request):
        result = subprocess.run([sys.executable, str(Path(guard.__file__)), "--state-dir", str(self.directory)],
                                input=json.dumps(request), text=True, capture_output=True, timeout=10)
        return json.loads(result.stdout)

    def test_cross_process_start_and_prepare_have_exactly_one_winner(self):
        request = dict(op="start", accountKey="a", spaceId="space-a", observed=self.observed(), target=3,
                       approved=True, approvalEvidence="User explicitly requested 3 sends now")
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(self.cli, [request] * 8))
        winners = [value for value in outcomes if value["ok"]]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(sum(value.get("error") == "RUN_EXISTS" for value in outcomes), 7)
        run = winners[0]["runId"]
        request = dict(op="prepare", runId=run, observed=self.observed(), recruiterId="same",
                       jobUrl="https://www.zhipin.com/job_detail/same.html", candidateCity="北京",
                       checksPassed=True, historyChecked=True, evidence="All filters and full conversation checked")
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(self.cli, [request] * 8))
        self.assertEqual(sum(value["ok"] for value in outcomes), 1, outcomes)
        self.assertEqual(sum(value.get("error") == "PENDING" for value in outcomes), 7)
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual((self.directory / "state.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main(verbosity=2)
