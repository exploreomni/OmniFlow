from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any

from ..exceptions import ExitCodes, OmniAPIError, OmniFlowError
from ..omni_client import AI_EVAL_STATES, AI_EVAL_TERMINAL_STATES, OmniClient
from ..timestamps import utc_now_iso


class EvalFailure(OmniAPIError):
    """A fixed reason code; remote prompt or error text must never enter public output."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"AI eval could not establish a complete comparison: {reason}")


def run_ai_eval_validation(
    *,
    client: OmniClient,
    model_id: str,
    branch_id: str | None,
    prompt_sets: list[dict[str, str]],
    fail_on_regression: bool,
    poll_interval_seconds: int,
    timeout_seconds: int,
    scoring_grace_seconds: int,
    max_samples: int = 20,
    record_runs: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Compare complete binary-scored runs, retaining customer content only in detail.

    ``record_runs`` persists restricted recovery evidence immediately after creation.
    Report sampling never changes the gate. Operational failures always fail closed,
    even when accuracy regressions are configured as warnings.
    """
    generated_at = utc_now_iso()
    report: dict[str, Any] = {
        "tool": "omniflow",
        "validator": "ai_eval",
        "generated_at": generated_at,
        "model_id": model_id,
        "branch_id": branch_id,
        "fail_on_regression": fail_on_regression,
        "raw_query_results_stored": False,
        "prompt_set_summaries": [],
        "issues": [],
    }
    detail = {**report, "prompt_set_results": [], "owned_runs": []}
    journal: dict[str, Any] = {
        "model_id": model_id,
        "branch_id": branch_id,
        "owned_runs": detail["owned_runs"],
        "creation_outcome": "not_started",
        "cleanup_required": False,
    }
    set_results: list[dict[str, Any]] = []
    failure: OmniFlowError | None = None

    def persist() -> None:
        if record_runs is not None:
            record_runs(journal)

    try:
        if not branch_id:
            report["note"] = "No Omni branch is available; AI eval comparison was skipped."
        else:
            selected = []
            for configured in prompt_sets:
                if configured.get("model_id") not in (None, model_id):
                    continue
                prompt_set = client.get_ai_eval_prompt_set(configured["id"])
                if configured.get("model_id") and prompt_set["model_id"] != model_id:
                    raise EvalFailure("prompt_set_model_mismatch")
                if prompt_set["model_id"] == model_id:
                    selected.append((configured, _expected_prompts(prompt_set)))
            if not selected:
                report["note"] = "No configured AI eval prompt set belongs to this model; comparison was skipped."
            # All selected sets are preflighted before the first mutation.
            for configured, expected in selected:
                owned = []
                for target_branch in (None, branch_id):
                    journal["creation_outcome"] = "pending_or_ambiguous"
                    persist()
                    run_id = client.start_ai_eval_run(
                        prompt_set_id=configured["id"],
                        description="OmniFlow AI eval baseline (main)"
                        if target_branch is None
                        else "OmniFlow AI eval branch",
                        branch_id=target_branch,
                    )
                    if any(entry["run_id"] == run_id for entry in journal["owned_runs"]):
                        raise EvalFailure("duplicate_run_id")
                    entry = {
                        "run_id": run_id,
                        "model_id": model_id,
                        "prompt_set_id": configured["id"],
                        "branch_id": target_branch,
                        "status": "UNKNOWN",
                        "cleanup": "not_needed",
                    }
                    journal["owned_runs"].append(entry)
                    owned.append(entry)
                    journal["creation_outcome"] = "confirmed"
                    persist()
                    # Bind the returned run before starting more work.
                    _read_bound_run(client, entry)
                    persist()
                main_run, branch_run = _poll_both(
                    client,
                    owned,
                    expected=expected,
                    poll_interval_seconds=poll_interval_seconds,
                    timeout_seconds=timeout_seconds,
                    scoring_grace_seconds=scoring_grace_seconds,
                    sleep=sleep,
                    monotonic=monotonic,
                    persist=persist,
                )
                rows = build_comparison(main_run, branch_run)
                for row in rows:
                    row["prompt_id"] = expected[row["prompt"]]
                set_results.append({"set": configured, "main_run": main_run, "branch_run": branch_run, "rows": rows})
    except OmniFlowError as exc:
        failure = exc
    finally:
        # A killed runner cannot run cleanup; its last journal is the recovery evidence.
        journal["cleanup_required"] = _cleanup_runs(client, journal["owned_runs"]) or (
            journal["creation_outcome"] == "pending_or_ambiguous"
        )
        persist()

    total_regressions = 0
    samples_remaining = max(0, max_samples)
    for sr in set_results:
        regressions = [row for row in sr["rows"] if row["status"] == "regressed"]
        improvements = [row for row in sr["rows"] if row["status"] == "improved"]
        total_regressions += len(regressions)
        m_acc, m_pass, m_n = accuracy(sr["main_run"]["results"])
        b_acc, b_pass, b_n = accuracy(sr["branch_run"]["results"])
        report["prompt_set_summaries"].append(
            {
                "prompt_set_id": sr["set"]["id"],
                "main_accuracy": m_acc,
                "main_passed": m_pass,
                "main_total": m_n,
                "branch_accuracy": b_acc,
                "branch_passed": b_pass,
                "branch_total": b_n,
                "accuracy_delta_pts": round((b_acc - m_acc) * 100, 1),
                "regressed_count": len(regressions),
                "improved_count": len(improvements),
            }
        )
        # One fixed aggregate issue remains even when no samples are requested.
        if regressions:
            report["issues"].append(
                {
                    "validator": "ai_eval",
                    "severity": "error" if fail_on_regression else "warning",
                    "prompt_set_id": sr["set"]["id"],
                    "reason": "accuracy_regression",
                    "message": f"{len(regressions)} AI eval prompts regressed from binary pass to fail.",
                    "prompt_ids": [row["prompt_id"] for row in regressions[:samples_remaining]],
                    "active": True,
                }
            )
            samples_remaining = max(0, samples_remaining - len(regressions))
    if failure is not None or journal["cleanup_required"]:
        reason = failure.reason if isinstance(failure, EvalFailure) else "request_failed"
        if failure is None:
            reason = "cleanup_unconfirmed"
        report["issues"].append(
            {
                "validator": "ai_eval",
                "severity": "error",
                "reason": reason,
                "message": f"AI eval comparison is incomplete ({reason}); review restricted run evidence.",
                "active": True,
            }
        )
    report["regressed_count"] = total_regressions
    report["operational_failure"] = failure is not None or journal["cleanup_required"]
    report["cleanup_required"] = journal["cleanup_required"]
    report["summary"] = {
        "total_issues": len(report["issues"]),
        "errors": sum(issue["severity"] == "error" for issue in report["issues"]),
        "warnings": sum(issue["severity"] == "warning" for issue in report["issues"]),
    }
    detail.update({"prompt_set_results": set_results, "lifecycle": journal})
    if report["operational_failure"]:
        exit_code = failure.exit_code if failure is not None else ExitCodes.OMNI_API_ERROR
    else:
        exit_code = ExitCodes.VALIDATION_FAILED if fail_on_regression and total_regressions else ExitCodes.SUCCESS
    return report, detail, exit_code


def _expected_prompts(prompt_set: dict[str, Any]) -> dict[str, str]:
    if prompt_set.get("is_archived") is not False:
        raise EvalFailure("prompt_set_not_active")
    prompts = prompt_set.get("prompts")
    if not isinstance(prompts, list) or not prompts:
        raise EvalFailure("empty_or_missing_prompts")
    expected: dict[str, str] = {}
    ids = set()
    for prompt in prompts:
        if not isinstance(prompt, dict):
            raise EvalFailure("malformed_prompt_set")
        text, prompt_id = prompt.get("prompt_text"), prompt.get("id")
        if not isinstance(text, str) or not text.strip() or not isinstance(prompt_id, str) or not prompt_id.strip():
            raise EvalFailure("malformed_prompt_set")
        if text in expected or prompt_id in ids:
            # Run result IDs are row IDs, not source prompt IDs. Text is the only
            # documented join key, so duplicated prompt text is ambiguous.
            raise EvalFailure("duplicate_prompt_identity")
        expected[text] = "prompt-" + hashlib.sha256(prompt_id.encode("utf-8")).hexdigest()[:16]
        ids.add(prompt_id)
    return expected


def _read_bound_run(client: OmniClient, entry: dict[str, Any]) -> dict[str, Any]:
    run = client.get_ai_eval_run(entry["run_id"])
    if not isinstance(run, dict) or any(
        key not in run or run[key] != entry[expected]
        for key, expected in (
            ("id", "run_id"),
            ("model_id", "model_id"),
            ("prompt_set_id", "prompt_set_id"),
            ("branch_id", "branch_id"),
        )
    ):
        entry["cleanup"] = "identity_unconfirmed"
        raise EvalFailure("run_identity_mismatch")
    if run.get("status") not in AI_EVAL_STATES:
        raise EvalFailure("unsupported_run_state")
    entry["status"] = run["status"]
    entry["last_run"] = run
    return run


def _cleanup_runs(client: OmniClient, entries: list[dict[str, Any]]) -> bool:
    unresolved = False
    for entry in entries:
        if entry["status"] in AI_EVAL_TERMINAL_STATES:
            continue
        try:
            run = _read_bound_run(client, entry)
            if run["status"] not in AI_EVAL_TERMINAL_STATES:
                entry["cleanup"] = "cancel_requested"
                try:
                    client.cancel_ai_eval_run(entry["run_id"])
                except OmniFlowError:
                    # Reconcile a lost response once; never repeat the mutation.
                    pass
                run = _read_bound_run(client, entry)
            if run["status"] not in AI_EVAL_TERMINAL_STATES:
                raise EvalFailure("cleanup_unconfirmed")
            entry["cleanup"] = "terminal_confirmed"
        except OmniFlowError:
            entry["cleanup"] = "manual_review_required"
            unresolved = True
    return unresolved


def _poll_both(
    client: OmniClient,
    owned: list[dict[str, Any]],
    *,
    expected: dict[str, str],
    poll_interval_seconds: int,
    timeout_seconds: int,
    scoring_grace_seconds: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    persist: Callable[[], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = monotonic()
    terminal_since: dict[str, float] = {}
    runs: dict[str, dict[str, Any]] = {}
    while True:
        for entry in owned:
            run_id = entry["run_id"]
            if run_id in runs:
                continue
            run = _read_bound_run(client, entry)
            persist()
            if run["status"] in {"CANCELLED", "FAILED"}:
                raise EvalFailure("run_cancelled_or_failed")
            if run["status"] == "COMPLETE":
                terminal_since.setdefault(run_id, monotonic())
                _validate_result_coverage(run, expected)
                if run_fully_scored(run):
                    _validate_scores(run)
                    runs[run_id] = run
                elif monotonic() - terminal_since[run_id] >= scoring_grace_seconds:
                    raise EvalFailure("incomplete_scoring")
        if len(runs) == 2:
            return runs[owned[0]["run_id"]], runs[owned[1]["run_id"]]
        if monotonic() - started >= timeout_seconds:
            raise EvalFailure("timeout")
        sleep(poll_interval_seconds)


def _validate_result_coverage(run: dict[str, Any], expected: dict[str, str]) -> None:
    by_prompt = index_by_prompt(run)
    if set(by_prompt) != set(expected):
        raise EvalFailure("incomplete_prompt_coverage")


def _validate_scores(run: dict[str, Any]) -> None:
    for result in run["results"]:
        job = result.get("agentic_job")
        if result.get("error_reason") is not None or not isinstance(job, dict) or job.get("state") != "COMPLETE":
            raise EvalFailure("prompt_job_failed_or_incomplete")
        score = result.get("score")
        if type(score) not in (int, float) or score not in (0, 1):
            # API documents fractional scores but does not define a passing threshold.
            raise EvalFailure("unsupported_nonbinary_score")


def result_settled(result: dict[str, Any]) -> bool:
    job = result.get("agentic_job")
    return (
        result.get("score") is not None
        or result.get("error_reason") is not None
        or (isinstance(job, dict) and job.get("state") in {"FAILED", "CANCELLED"})
    )


def run_fully_scored(run: dict[str, Any]) -> bool:
    results = run.get("results", [])
    return bool(results) and all(result_settled(result) for result in results)


def index_by_prompt(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results = run.get("results")
    if not isinstance(results, list):
        raise EvalFailure("malformed_results")
    indexed = {}
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("prompt"), str) or not result["prompt"].strip():
            raise EvalFailure("malformed_result")
        if result["prompt"] in indexed:
            raise EvalFailure("duplicate_result_identity")
        indexed[result["prompt"]] = result
    return indexed


def accuracy(results: list[dict[str, Any]]) -> tuple[float | None, int, int]:
    scored = [result for result in results if type(result.get("score")) in (int, float) and result["score"] in (0, 1)]
    if not scored:
        return None, 0, 0
    passed = sum(result["score"] == 1 for result in scored)
    return passed / len(scored), passed, len(scored)


def build_comparison(main_run: dict[str, Any], branch_run: dict[str, Any]) -> list[dict[str, Any]]:
    main_by, branch_by = index_by_prompt(main_run), index_by_prompt(branch_run)
    rows = []
    for prompt in sorted(set(main_by) | set(branch_by)):
        main_result, branch_result = main_by.get(prompt, {}), branch_by.get(prompt, {})
        main_score, branch_score = main_result.get("score"), branch_result.get("score")
        if main_score == 1 and branch_score == 0:
            status = "regressed"
        elif main_score == 0 and branch_score == 1:
            status = "improved"
        elif main_score is None or branch_score is None:
            status = "unscored"
        elif main_score == branch_score:
            status = "unchanged"
        else:
            status = "changed"
        rows.append(
            {
                "prompt": prompt,
                "main_score": main_score,
                "branch_score": branch_score,
                "status": status,
                "branch_error": branch_result.get("error_reason"),
                "branch_conversation_id": (branch_result.get("agentic_job") or {}).get("conversation_id"),
                "branch_timing_ms": branch_result.get("timing_ms"),
                "main_query_count": main_result.get("query_count"),
                "branch_query_count": branch_result.get("query_count"),
            }
        )
    return rows
