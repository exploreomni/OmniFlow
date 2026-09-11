from __future__ import annotations

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

    def begin(self, name: str) -> None:
        self.current = name

    def complete(self, name: str, exit_code: int = 0) -> None:
        self.states[name].update(
            status="completed" if exit_code in {0, 1} else "failed", exit_code=exit_code,
        )

    def fail(self, exc: Exception) -> None:
        self.states[self.current].update(
            status="failed", exit_code=exc.exit_code if isinstance(exc, OmniFlowError) else ExitCodes.INTERNAL_ERROR,
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "validation_complete": all(state["status"] in {"completed", "disabled"} for state in self.states.values()),
            "check_states": [dict(state) for state in self.states.values()],
        }


def scope_findings(issues: list[dict[str, Any]], *, model_id: str, branch_id: str | None, branch_name: str | None) -> None:
    for issue in issues:
        # Trusted execution identity takes precedence over API metadata.
        issue.update(model_id=model_id, branch_id=branch_id, branch_name=branch_name)
