"""Run-level policy: distinguish health observations from verified PR impact."""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from .candidate import CandidateVerificationError, verify_candidate

# Only ordinary synchronization failures may be retried or made advisory.
# Unsafe sources, ambiguous identities and exclusion conflicts remain hard errors.
SYNC_REASONS = frozenset({
    "file_inventory_mismatch", "content_mismatch", "missing_candidate_branch",
    "changed_during_validation", "pr_change_not_synchronized",
})


def verify_with_retry(client: Any, context: Any, *, revision_side: str = "head",
                      non_omni_rules: tuple = (), retry: bool = True,
                      changed_paths: Sequence[str] | None = None) -> dict:
    """One bounded synchronization retry, never an unbounded CI polling loop."""
    kwargs = {"revision_side": revision_side}
    if non_omni_rules:
        kwargs["non_omni_rules"] = non_omni_rules
    if changed_paths is not None:
        kwargs["changed_paths"] = changed_paths
    try:
        return verify_candidate(client, context, **kwargs)
    except CandidateVerificationError as exc:
        if not retry or exc.candidate_reason not in SYNC_REASONS - {"changed_during_validation"}:
            raise
    print("OmniFlow: candidate synchronization is not yet verified; retrying once in 2 seconds.", flush=True)
    time.sleep(2)
    return verify_candidate(client, context, **kwargs)


def mark_health_only(report: dict) -> None:
    """Evidence from an unverified candidate must not claim PR causality."""
    report.update(
        health_only=True, regression_status="unknown", comparison_status="unavailable",
        comparison_mode="unknown", comparison_source="none", validation_complete=False,
        blocking_issues=0,
    )
    for key in ("new_issues", "existing_issues", "resolved_issues", "improved_issues"):
        report.pop(key, None)
    for issue in report.get("issues", []):
        hard_failure = issue.get("operational_failure") or issue.get("coverage_complete") is False
        issue.update(blocking=bool(hard_failure), health_only=True, regression_status="unknown")
        issue.pop("state", None)
    report["blocking_issues"] = sum(bool(issue.get("blocking")) for issue in report.get("issues", []))


def may_advise(config: Any, exc: Exception, issues: list[dict]) -> bool:
    """An explicit onboarding choice is not permission to bypass security."""
    return (
        getattr(getattr(config, "validation", None), "mode", "strict") == "advisory"
        and not getattr(getattr(config, "breaking_change_hold", None), "enabled", False)
        and isinstance(exc, CandidateVerificationError)
        and exc.candidate_reason in SYNC_REASONS
        and not any(issue.get("operational_failure") or issue.get("coverage_complete") is False
                    or (issue.get("blocking", issue.get("severity") == "error")
                        and not issue.get("health_only"))
                    for issue in issues)
    )
