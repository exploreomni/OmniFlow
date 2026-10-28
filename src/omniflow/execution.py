from __future__ import annotations

import sys
import time
from typing import Any

from .exceptions import ExitCodes, OmniFlowError


class ValidationProgress:
    """In-memory execution evidence retained when a later validation stage fails."""

    def __init__(self, config: Any, *, branch_id: str | None, enforce_breaking_hold: bool) -> None:
        hold = enforce_breaking_hold and config.breaking_change_hold.enabled
        enabled = {
            "context": True,
            "content": config.content_validation.enabled,
            "model": config.model_validation.enabled,
            "ai_eval": config.ai_eval.enabled and bool(config.ai_eval.prompt_sets),
            "query_validation": config.query_validation.enabled,
            "semantic_diff": config.semantic_lint.enabled or config.contracts.enabled or hold,
            "semantic_lint": config.semantic_lint.enabled,
            "downstream": config.contracts.enabled,
            "contracts": config.contracts.enabled,
            "dbt_exposures": config.dbt_exposures.enabled,
            "breaking_change_hold": hold,
            "reporting": True,
        }
        self.states = {
            name: {"validator": name, "status": "not_run" if active else "disabled"}
            for name, active in enabled.items()
        }
        self.current = "context"
        self.branch_id = branch_id
        self.issues: list[dict[str, Any]] = []
        self.reports: list[dict[str, Any]] = []
        self._started = {"context": time.monotonic()}

    def begin(self, name: str) -> None:
        self.current = name
        self.states.setdefault(name, {"validator": name})["status"] = "running"
        self.states[name].pop("blocked_by", None)
        self.states[name].pop("incomplete_reason", None)
        self._started[name] = time.monotonic()
        print(f"OmniFlow: starting {name} check.", file=sys.stderr, flush=True)

    def complete(self, name: str, exit_code: int = 0, *, execution_status: str | None = None,
                 skipped_reason: str | None = None) -> None:
        status = "completed" if exit_code in {0, 1} else "failed"
        if execution_status in {"partial", "unavailable", "skipped"}:
            status = execution_status
        self.states[name].update(
            status=status, exit_code=exit_code, elapsed_ms=self._elapsed(name),
        )
        self.states[name].pop("blocked_by", None)
        self.states[name].pop("incomplete_reason", None)
        if skipped_reason in {"no_branch", "no_matching_prompt_set"}:
            self.states[name]["skipped_reason"] = skipped_reason
        print(f"OmniFlow: {name} check {status} ({self.states[name]['elapsed_ms']} ms).",
              file=sys.stderr, flush=True)

    def _elapsed(self, name: str) -> int:
        return max(0, int((time.monotonic() - self._started.get(name, time.monotonic())) * 1000))

    def fail(self, exc: Exception) -> None:
        self.states[self.current].update(
            status="failed", exit_code=exc.exit_code if isinstance(exc, OmniFlowError) else ExitCodes.INTERNAL_ERROR,
            elapsed_ms=self._elapsed(self.current),
        )
        for state in self.states.values():
            if state["status"] == "not_run":
                state["blocked_by"] = self.current
            elif state["status"] == "running":
                # Candidate verification deliberately spans the other checks:
                # an initial sample is not complete until its final recheck.
                # A terminal failure must not leave that suspended check looking
                # like a job that is still running in a downloaded report.
                state.update(status="partial", blocked_by=self.current,
                             elapsed_ms=self._elapsed(state["validator"]),
                             incomplete_reason=("final_recheck_not_run" if state["validator"] == "candidate"
                                                else "check_interrupted"))

    def evidence(self) -> dict[str, Any]:
        return {
            "validation_complete": all(state["status"] in {"completed", "disabled"} for state in self.states.values()),
            "check_states": [dict(state) for state in self.states.values()],
        }


def scope_findings(issues: list[dict[str, Any]], *, model_id: str, branch_id: str | None, branch_name: str | None) -> None:
    for issue in issues:
        # Trusted execution identity takes precedence over API metadata.
        issue.update(model_id=model_id, branch_id=branch_id, branch_name=branch_name)
