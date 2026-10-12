#!/usr/bin/env python3
"""Local, persistent send guard. Reads one JSON request from stdin; never browses.

Every invocation locks the state file for its entire read/check/write transaction.
Run ownership and unresolved attempts persist after the process exits. There is
deliberately no timeout, reset, force-unlock, or automatic retry operation.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid


INTERVAL = 20


class GuardError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.result = {"ok": False, "error": code, "message": message, **details}


def require(condition, code, message, **details):
    if not condition:
        raise GuardError(code, message, **details)


def string(data, name):
    value = data.get(name)
    require(isinstance(value, str) and value.strip(), "INVALID_INPUT", f"{name} must be a nonempty string")
    return value.strip()


def approved(data):
    require(data.get("approved") is True, "APPROVAL_REQUIRED", "Explicit approval for this operation is required")
    return string(data, "approvalEvidence")


def canonical_job(value):
    require(isinstance(value, str), "INVALID_INPUT", "jobUrl must be a BOSS job-detail URL")
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and parsed.hostname in {"www.zhipin.com", "zhipin.com"}
            and parsed.username is None and parsed.password is None
            and re.fullmatch(r"/job_detail/[A-Za-z0-9_-]+\.html", parsed.path),
            "INVALID_INPUT", "jobUrl must be an observed https://www.zhipin.com/job_detail/<id>.html URL")
    return "https://www.zhipin.com" + parsed.path


def account_for(state, key):
    require(key in state["accounts"], "ACCOUNT_NOT_CONFIGURED", "Configure this account explicitly first")
    return state["accounts"][key]


def run_for(state, run_id):
    require(run_id in state["runs"], "RUN_NOT_FOUND", "Unknown runId; use status to recover the original run")
    return state["runs"][run_id]


def verify_observed(observed, key, account, space_id, check_city=True):
    require(isinstance(observed, dict), "IDENTITY_MISMATCH", "Fresh page observations are required")
    expected = {"accountKey": key, "profileId": account["profileId"], "identity": account["identity"],
                "spaceId": space_id}
    if check_city:
        expected["city"] = account["filters"]["city"]
    for field, value in expected.items():
        require(observed.get(field) == value, "IDENTITY_MISMATCH",
                f"Observed {field} does not match the bound account/run")


def unresolved(state, key):
    return [item["attemptId"] for item in state["attempts"].values()
            if item["accountKey"] == key and item["result"] in {"sending", "unknown"}]


def stop(state, run, reason):
    run["status"] = "halted"
    run["haltReason"] = reason
    state["accounts"][run["accountKey"]]["haltReason"] = reason


def verify_run_observed(state, run, observed):
    account = account_for(state, run["accountKey"])
    try:
        verify_observed(observed, run["accountKey"], account, run["spaceId"])
    except GuardError:
        stop(state, run, "Account, profile, space, or city changed")
        raise
    return account


def public_run(run):
    return {**{key: value for key, value in run.items()
               if key not in {"approvalEvidence", "resumeEvidence", "finishApprovalEvidence", "finishEvidence", "parallel"}},
            "concurrencyScope": "account"}


def acquire_execution(state, run):
    for other in state["runs"].values():
        if other["runId"] == run["runId"] or other["status"] != "active":
            continue
        require(other["accountKey"] != run["accountKey"] and other["profileId"] != run["profileId"],
                "RUN_EXISTS", "This account or profile already has an executing owner")
        require(other["spaceId"] != run["spaceId"], "SPACE_IN_USE", "Each run must own a different task space")


def dispatch(state, request):
    op = string(request, "op")
    now = time.time()
    if op == "configure":
        approval = approved(request)
        key = string(request, "accountKey")
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key), "INVALID_INPUT", "Use a stable non-personal accountKey")
        profile = string(request, "profileId")
        identity = string(request, "identity")
        require(not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", identity), "INVALID_INPUT", "Use a masked visible identity, never a full phone number")
        string(request, "greeting")
        greeting = request["greeting"]  # Preserve the approved text byte-for-byte.
        filters = request.get("filters")
        require(isinstance(filters, dict), "INVALID_INPUT", "filters must contain the approved filter snapshot")
        string(filters, "city")
        require(filters["city"] == filters["city"].strip(), "INVALID_INPUT", "filters.city must match the observed city exactly")
        previous = state["accounts"].get(key)
        if previous:
            require(not previous["activeRun"], "RUN_EXISTS", "An active or halted run makes its account configuration read-only")
            require(previous["profileId"] == profile and previous["identity"] == identity,
                    "BINDING_IMMUTABLE", "Cannot rebind an existing accountKey/profile/identity")
        for other_key, other in state["accounts"].items():
            if other_key != key:
                require(other["identity"] != identity, "IDENTITY_ALREADY_BOUND", "This identity already belongs to another accountKey")
                require(other["profileId"] != profile, "PROFILE_ALREADY_BOUND", "Each account needs its own browser profile")
        state["accounts"][key] = {
            "profileId": profile, "identity": identity, "greeting": greeting, "filters": filters,
            "approvalEvidence": approval, "configuredAt": now,
            "activeRun": None, "haltReason": previous["haltReason"] if previous else None,
            "nextAllowedAt": previous["nextAllowedAt"] if previous else 0,
        }
        return {"accountKey": key, "configured": True}

    if op == "status":
        key = request.get("accountKey")
        selected_run = request.get("runId")
        if selected_run:
            run = run_for(state, selected_run)
            require(key is None or key == run["accountKey"], "INVALID_INPUT", "accountKey and runId disagree")
            key = run["accountKey"]
        if key is not None:
            account_for(state, key)
        return {
            "concurrencyScope": "account",
            "accounts": {k: {"profileId": a["profileId"], "city": a["filters"]["city"],
                              "filters": a["filters"],
                              "activeRun": a["activeRun"], "haltReason": a["haltReason"],
                              "nextAllowedAt": a["nextAllowedAt"], "pending": unresolved(state, k)}
                         for k, a in state["accounts"].items() if key is None or k == key},
            "runs": {k: public_run(r) for k, r in state["runs"].items()
                     if (key is None or r["accountKey"] == key) and (not selected_run or k == selected_run)},
            "attempts": {k: {name: a[name] for name in ("attemptId", "runId", "accountKey", "recruiterId", "jobUrl", "result", "createdAt")}
                         for k, a in state["attempts"].items()
                         if (key is None or a["accountKey"] == key) and (not selected_run or a["runId"] == selected_run)},
        }

    if op == "start":
        approval = approved(request)
        key = string(request, "accountKey")
        account = account_for(state, key)
        target = request.get("target")
        require(type(target) is int and target > 0, "INVALID_INPUT", "target must be this run's explicitly requested positive integer")
        space = string(request, "spaceId")
        require(not account["activeRun"], "RUN_EXISTS", "Recover the original run; a new run cannot replace it", runId=account["activeRun"])
        require(not account["haltReason"], "HALTED", "Resolve and resume the original halted run first")
        require(not unresolved(state, key), "PENDING", "Unresolved sends block a new run")
        verify_observed(request.get("observed"), key, account, space)
        run_id = uuid.uuid4().hex
        run = {
            "runId": run_id, "accountKey": key, "profileId": account["profileId"], "spaceId": space,
            "city": account["filters"]["city"], "target": target, "sent": 0, "pending": None,
            "status": "active", "haltReason": None,
            "approvalEvidence": approval, "startedAt": now,
        }
        acquire_execution(state, run)
        state["runs"][run_id] = run
        account["activeRun"] = run_id
        return public_run(state["runs"][run_id])

    run_id = string(request, "runId")
    run = run_for(state, run_id)
    account = account_for(state, run["accountKey"])
    require(run["status"] != "finished", "FINISHED", "This run is closed")

    if op == "halt":
        stop(state, run, string(request, "reason"))
        return public_run(run)

    if op == "resume":
        approval = approved(request)
        require(run["status"] == "halted", "NOT_HALTED", "Run is not halted")
        require(not unresolved(state, run["accountKey"]), "PENDING", "Read-only verification must resolve every unknown/sending attempt before resuming")
        verify_run_observed(state, run, request.get("observed"))
        acquire_execution(state, run)
        run.update(status="active", haltReason=None, resumeEvidence=approval)
        account["haltReason"] = None
        return public_run(run)

    if op == "finish":
        reason = string(request, "reason")
        require(not unresolved(state, run["accountKey"]), "PENDING", "Cannot close a run with unverified sends")
        if run["status"] == "halted":
            approval = approved(request)
            require(request.get("executionStopped") is True, "EXECUTION_STOP_REQUIRED",
                    "Stop all commands that could send messages before closing a halted run")
            verify_observed(request.get("observed"), run["accountKey"], account, run["spaceId"], check_city=False)
            evidence = string(request, "evidence")
            run.update(finishApprovalEvidence=approval, finishEvidence=evidence, executionStopped=True)
        run.update(status="finished", finishedAt=now, finishReason=reason)
        account["activeRun"] = None
        account["haltReason"] = None
        return public_run(run)

    if op == "prepare":
        require(run["status"] == "active" and not account["haltReason"], "HALTED", "Explicitly resume the halted run first")
        require(not unresolved(state, run["accountKey"]), "PENDING", "Only verify the outstanding send; never click send again")
        verify_run_observed(state, run, request.get("observed"))
        recruiter = string(request, "recruiterId")
        job = canonical_job(request.get("jobUrl"))
        require(request.get("candidateCity") == run["city"], "CITY_MISMATCH", "Candidate city must match the current approved city")
        require(request.get("checksPassed") is True and request.get("historyChecked") is True,
                "CHECKS_REQUIRED", "Verify all filters and prior conversation history before preparing")
        evidence = string(request, "evidence")
        require(run["sent"] < run["target"], "QUOTA_REACHED", "No sends remain in this run")
        for item in state["attempts"].values():
            if item["accountKey"] == run["accountKey"] and item["result"] in {"confirmed", "unexpected_sent"}:
                require(item["recruiterId"] != recruiter and item["jobUrl"] != job,
                        "DUPLICATE", "This account already attempted this recruiter or job", attemptId=item["attemptId"], result=item["result"])
        require(now >= account["nextAllowedAt"], "COOLDOWN", "The persistent 20-second interval has not elapsed", retryAfterSeconds=round(account["nextAllowedAt"] - now, 3))
        attempt_id = uuid.uuid4().hex
        state["attempts"][attempt_id] = {
            "attemptId": attempt_id, "runId": run_id, "accountKey": run["accountKey"],
            "recruiterId": recruiter, "jobUrl": job, "result": "sending", "createdAt": now,
            "evidence": evidence,
        }
        run["pending"] = attempt_id
        account["nextAllowedAt"] = now + INTERVAL
        return {"allowed": True, "attemptId": attempt_id, "runId": run_id, "greeting": account["greeting"],
                "instruction": "One immediate send attempt only. On interruption, halt and verify; never reuse this permission."}

    if op == "resolve":
        attempt_id = string(request, "attemptId")
        require(attempt_id in state["attempts"] and state["attempts"][attempt_id]["runId"] == run_id,
                "ATTEMPT_NOT_FOUND", "attemptId must belong to this original run")
        attempt = state["attempts"][attempt_id]
        result = request.get("result")
        require(result in {"confirmed", "unknown", "failed", "not_sent", "unexpected_sent"}, "INVALID_INPUT", "Unsupported result")
        evidence = string(request, "evidence")
        verify_run_observed(state, run, request.get("observed"))
        if attempt["result"] not in {"sending", "unknown"}:
            require(attempt["result"] == result, "ALREADY_RESOLVED", "A terminal result cannot be overwritten")
            return {"attemptId": attempt_id, "result": result, "sent": run["sent"], "status": run["status"], "alreadyResolved": True}
        if result == "not_sent":
            require(request.get("clicked") is False, "CANNOT_ASSERT_NOT_SENT",
                    "not_sent requires proof that the actual message submission function was never invoked")
            if attempt["result"] == "unknown":
                correction = request.get("preSubmitCorrection")
                require(request.get("executionStopped") is True and isinstance(correction, dict),
                        "CANNOT_ASSERT_NOT_SENT", "Correcting a pre-submit misclassification requires stopped execution plus separate execution and entry evidence; missing messages alone are insufficient")
                execution_evidence = string(correction, "executionEvidence")
                entry_evidence = string(correction, "entryEvidence")
                attempt["preSubmitCorrection"] = {
                    "previousResult": attempt["result"], "previousEvidence": attempt["evidence"],
                    "previousResolvedAt": attempt["resolvedAt"], "executionEvidence": execution_evidence,
                    "entryEvidence": entry_evidence, "executionStopped": True, "correctedAt": now,
                }
        if result == "failed":
            require(request.get("failureVisible") is True, "FAILURE_EVIDENCE_REQUIRED", "failed requires an explicit visible failure, not missing delivery evidence")
        attempt.update(result=result, evidence=evidence, resolvedAt=now)
        if result != "not_sent":
            account["nextAllowedAt"] = max(account["nextAllowedAt"], now + INTERVAL)
        if result == "unknown":
            stop(state, run, "Unverified send; read-only verification only")
        else:
            run["pending"] = None
            if result == "confirmed":
                run["sent"] += 1
            elif result == "failed":
                stop(state, run, "Page explicitly reported send failure")
            elif result == "unexpected_sent":
                stop(state, run, "Page confirmed a default, incomplete, or unexpected message was sent")
        return {"attemptId": attempt_id, "result": result, "sent": run["sent"], "target": run["target"],
                "status": run["status"], "nextAllowedAt": account["nextAllowedAt"]}

    raise GuardError("INVALID_INPUT", "Unknown operation")


def atomic_save(path, state):
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute(request, directory):
    require(isinstance(request, dict), "INVALID_INPUT", "Request must be one JSON object")
    directory = Path(directory).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    lock_fd = os.open(directory / "state.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "state.json"
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            require(isinstance(state, dict) and state.get("version") == 1 and all(isinstance(state.get(k), dict) for k in ("accounts", "runs", "attempts")),
                    "INVALID_STATE", "State is invalid; do not reset or discard send history")
        else:
            state = {"version": 1, "accounts": {}, "runs": {}, "attempts": {}}
        before = json.dumps(state, sort_keys=True)
        try:
            result = {"ok": True, **dispatch(state, request)}
        except GuardError as error:
            result = error.result
        if json.dumps(state, sort_keys=True) != before:
            atomic_save(path, state)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser.add_argument("--state-dir", default=str(codex_home / "boss-ai-job-outreach"))
    args = parser.parse_args()
    try:
        result = execute(json.load(sys.stdin), args.state_dir)
    except GuardError as error:
        result = error.result
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"ok": False, "error": "STATE_OR_INPUT_ERROR", "message": str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
