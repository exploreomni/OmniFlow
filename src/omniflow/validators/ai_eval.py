from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..exceptions import OmniAPIError
from ..omni_client import AI_EVAL_TERMINAL_STATES, OmniClient
from ..timestamps import utc_now_iso

MAX_PROMPT_CHARS_IN_PUBLIC_ISSUE = 300


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
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Run each configured prompt set against `main` and the PR branch, and compare.

    Returns (public_summary_report, detail_report, exit_code). The public report
    carries only aggregate accuracy/cost numbers and a bounded list of regressed
    prompts; the detail report carries full per-prompt rows (cost breakdown,
    conversation IDs, timing) and is intended for restricted-artifact storage
    only, mirroring how the Omni AI job status API discards prompt/result text
    from anything written to a public surface.
    """
    generated_at = utc_now_iso()
    if not branch_id:
        note = "No Omni branch is available for this context; AI eval requires a branch to compare against main."
        empty = _empty_report(model_id=model_id, branch_id=branch_id, generated_at=generated_at, note=note)
        return empty, {**empty, "prompt_set_results": []}, 0

    set_results = []
    for prompt_set in prompt_sets:
        main_run_id = client.start_ai_eval_run(
            prompt_set_id=prompt_set["id"],
            description="OmniFlow AI eval baseline (main)",
        )
        branch_run_id = client.start_ai_eval_run(
            prompt_set_id=prompt_set["id"],
            description="OmniFlow AI eval branch",
            branch_id=branch_id,
        )
        main_run, branch_run = _poll_both(
            client,
            main_run_id,
            branch_run_id,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            scoring_grace_seconds=scoring_grace_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )
        rows = build_comparison(main_run, branch_run)
        set_results.append({"set": prompt_set, "main_run": main_run, "branch_run": branch_run, "rows": rows})

    public_report, issues = _public_report(
        model_id=model_id,
        branch_id=branch_id,
        generated_at=generated_at,
        set_results=set_results,
        fail_on_regression=fail_on_regression,
        max_samples=max_samples,
    )
    detail_report = _detail_report(
        model_id=model_id,
        branch_id=branch_id,
        generated_at=generated_at,
        set_results=set_results,
    )
    exit_code = 1 if (fail_on_regression and issues) else 0
    return public_report, detail_report, exit_code


def _empty_report(*, model_id: str, branch_id: str | None, generated_at: str, note: str) -> dict[str, Any]:
    return {
        "tool": "omniflow",
        "validator": "ai_eval",
        "generated_at": generated_at,
        "model_id": model_id,
        "branch_id": branch_id,
        "note": note,
        "prompt_set_summaries": [],
        "issues": [],
        "summary": {"total_issues": 0, "errors": 0, "warnings": 0},
    }


def _public_report(
    *,
    model_id: str,
    branch_id: str | None,
    generated_at: str,
    set_results: list[dict[str, Any]],
    fail_on_regression: bool,
    max_samples: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summaries = []
    issues: list[dict[str, Any]] = []
    severity = "error" if fail_on_regression else "warning"
    for sr in set_results:
        set_info = sr["set"]
        rows = sr["rows"]
        m_acc, m_pass, m_n = accuracy(sr["main_run"].get("results", []))
        b_acc, b_pass, b_n = accuracy(sr["branch_run"].get("results", []))
        m_cost, b_cost = run_cost(sr["main_run"]), run_cost(sr["branch_run"])
        regressions = [row for row in rows if row["status"] == "regressed"]
        improvements = [row for row in rows if row["status"] == "improved"]
        summaries.append(
            {
                "prompt_set_id": set_info["id"],
                "prompt_set_label": set_info["label"],
                "main_accuracy": m_acc,
                "main_passed": m_pass,
                "main_total": m_n,
                "branch_accuracy": b_acc,
                "branch_passed": b_pass,
                "branch_total": b_n,
                "accuracy_delta_pts": None if (m_acc is None or b_acc is None) else round((b_acc - m_acc) * 100, 1),
                "main_cost_usd": round(m_cost, 4),
                "branch_cost_usd": round(b_cost, 4),
                "cost_delta_usd": round(b_cost - m_cost, 4),
                "regressed_count": len(regressions),
                "improved_count": len(improvements),
            }
        )
        for row in regressions[:max_samples]:
            note = f" — {row['branch_error']}" if row.get("branch_error") else ""
            issues.append(
                {
                    "validator": "ai_eval",
                    "severity": severity,
                    "prompt_set_id": set_info["id"],
                    "prompt_set_label": set_info["label"],
                    "prompt": _truncate(row["prompt"], MAX_PROMPT_CHARS_IN_PUBLIC_ISSUE),
                    "message": f"Prompt regressed on branch (passed on main, failed on branch){note}",
                    "active": True,
                }
            )
    report = {
        "tool": "omniflow",
        "validator": "ai_eval",
        "generated_at": generated_at,
        "model_id": model_id,
        "branch_id": branch_id,
        "fail_on_regression": fail_on_regression,
        "raw_query_results_stored": False,
        "prompt_set_summaries": summaries,
        "issues": issues,
        "summary": {
            "total_issues": len(issues),
            "errors": sum(1 for issue in issues if issue["severity"] == "error"),
            "warnings": sum(1 for issue in issues if issue["severity"] == "warning"),
        },
    }
    return report, issues


def _detail_report(
    *,
    model_id: str,
    branch_id: str | None,
    generated_at: str,
    set_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "tool": "omniflow",
        "validator": "ai_eval",
        "generated_at": generated_at,
        "model_id": model_id,
        "branch_id": branch_id,
        "raw_query_results_stored": False,
        "prompt_set_results": [
            {
                "prompt_set_id": sr["set"]["id"],
                "prompt_set_label": sr["set"]["label"],
                "main_run": sr["main_run"],
                "branch_run": sr["branch_run"],
                "comparison": sr["rows"],
            }
            for sr in set_results
        ],
    }


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def result_settled(result: dict[str, Any]) -> bool:
    """A per-prompt result is done once it has a score or has errored. Scores
    land a few seconds AFTER the run status flips to a terminal state, so
    status alone is not a safe signal to read scores."""
    return result.get("score") is not None or result.get("error_reason") is not None


def run_fully_scored(run: dict[str, Any]) -> bool:
    results = run.get("results", [])
    return bool(results) and all(result_settled(result) for result in results)


def _poll_both(
    client: OmniClient,
    main_run_id: str,
    branch_run_id: str,
    *,
    poll_interval_seconds: int,
    timeout_seconds: int,
    scoring_grace_seconds: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Poll two runs concurrently (Omni allows at most 2 in-progress eval runs
    org-wide), and keep polling after a run reaches a terminal state until every
    prompt is scored. If scores are still missing scoring_grace_seconds after a
    run went terminal, give up on the stragglers and report whatever is present
    rather than hang indefinitely.
    """
    started = monotonic()
    runs: dict[str, dict[str, Any] | None] = {main_run_id: None, branch_run_id: None}
    terminal_since: dict[str, float | None] = {main_run_id: None, branch_run_id: None}

    def settled(run_id: str, now: float) -> bool:
        run = runs[run_id]
        if run is None or run["status"] not in AI_EVAL_TERMINAL_STATES:
            return False
        if run_fully_scored(run):
            return True
        return terminal_since[run_id] is not None and (now - terminal_since[run_id]) >= scoring_grace_seconds

    while True:
        now = monotonic()
        for run_id in (main_run_id, branch_run_id):
            if not settled(run_id, now):
                run = client.get_ai_eval_run(run_id)
                runs[run_id] = run
                if run["status"] in AI_EVAL_TERMINAL_STATES and terminal_since[run_id] is None:
                    terminal_since[run_id] = now
        now = monotonic()
        if settled(main_run_id, now) and settled(branch_run_id, now):
            return runs[main_run_id], runs[branch_run_id]
        if now - started > timeout_seconds:
            raise OmniAPIError(f"AI eval runs did not finish within {timeout_seconds} seconds")
        sleep(poll_interval_seconds)


def index_by_prompt(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {result["prompt"]: result for result in run.get("results", [])}


def accuracy(results: list[dict[str, Any]]) -> tuple[float | None, int, int]:
    scored = [result for result in results if result.get("score") is not None]
    if not scored:
        return None, 0, 0
    passed = sum(1 for result in scored if result["score"] == 1)
    return passed / len(scored), passed, len(scored)


def result_cost(result: dict[str, Any]) -> float | None:
    """Total raw LLM cost (USD) for one prompt: model answer + judge scoring.
    Returns None when Omni reported no cost at all."""
    cost, scoring_cost = result.get("cost"), result.get("scoring_cost")
    if cost is None and scoring_cost is None:
        return None
    return (cost or 0) + (scoring_cost or 0)


def run_cost(run: dict[str, Any]) -> float:
    return sum(result_cost(result) or 0 for result in run.get("results", []))


def build_comparison(main_run: dict[str, Any], branch_run: dict[str, Any]) -> list[dict[str, Any]]:
    main_by = index_by_prompt(main_run)
    branch_by = index_by_prompt(branch_run)
    rows = []
    for prompt in sorted(set(main_by) | set(branch_by)):
        main_result = main_by.get(prompt, {})
        branch_result = branch_by.get(prompt, {})
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
                "main_total_cost": result_cost(main_result),
                "branch_total_cost": result_cost(branch_result),
                "branch_timing_ms": branch_result.get("timing_ms"),
                "main_query_count": main_result.get("query_count"),
                "branch_query_count": branch_result.get("query_count"),
            }
        )
    return rows
