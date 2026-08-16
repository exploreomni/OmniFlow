from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from .exceptions import ConfigError, ExitCodes, OmniAPIError, SecurityPolicyError
from .git import current_branch, event_name, is_pull_request_event
from .timestamps import utc_now_iso


def validate_dbt_sync_environment(contexts: list[Any]) -> str:
    if not contexts:
        raise ConfigError("dbt synchronization requires at least one Omni model context")
    if is_pull_request_event():
        raise SecurityPolicyError("dbt synchronization is prohibited for pull request events")

    event = event_name()
    if os.getenv("GITHUB_ACTIONS") == "true":
        if event not in {"push", "workflow_dispatch"}:
            raise SecurityPolicyError(
                "dbt synchronization in GitHub Actions is allowed only for push or workflow_dispatch events"
            )
        if os.getenv("GITHUB_REF_TYPE") not in {None, "", "branch"}:
            raise SecurityPolicyError("dbt synchronization is prohibited for Git tags")

    deployment_branch = current_branch()
    if not deployment_branch:
        raise ConfigError("Could not determine the deployment Git branch for dbt synchronization")
    for context in contexts:
        if not context.base_branch:
            raise ConfigError(
                f"Model {context.model_id} is missing base_branch required for protected dbt synchronization"
            )
        if context.base_branch != deployment_branch:
            raise SecurityPolicyError(
                f"dbt synchronization for model {context.model_id} is restricted to trusted base branch "
                f"'{context.base_branch}', not '{deployment_branch}'"
            )
    return deployment_branch


def run_dbt_sync(
    *,
    client: Any,
    model_id: str,
    branch_id: str | None,
    refresh_mode: str,
    poll_interval_seconds: int,
    timeout_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], int]:
    if refresh_mode not in {"hard", "soft"}:
        raise ConfigError("dbt synchronization refresh_mode must be 'hard' or 'soft'")
    if not 2 <= poll_interval_seconds <= 30:
        raise ConfigError("dbt synchronization poll interval must be between 2 and 30 seconds")
    if not 30 <= timeout_seconds <= 3_600:
        raise ConfigError("dbt synchronization timeout must be between 30 and 3600 seconds")

    started_at = utc_now_iso()
    started = monotonic()
    refresh = client.start_schema_refresh(
        model_id,
        branch_id=branch_id,
        hard_refresh=refresh_mode == "hard",
    )
    job_id = refresh["job_id"]
    status = refresh["status"]
    polls = 0

    while status == "RUNNING":
        remaining = timeout_seconds - (monotonic() - started)
        if remaining <= 0:
            return _sync_report(
                model_id=model_id,
                branch_id=branch_id,
                job_id=job_id,
                refresh_mode=refresh_mode,
                status="timed_out",
                omni_status=status,
                polls=polls,
                started_at=started_at,
                elapsed_seconds=timeout_seconds,
                message=f"Schema refresh did not finish within {timeout_seconds} seconds",
            ), ExitCodes.OMNI_API_ERROR
        sleep(min(float(poll_interval_seconds), remaining))
        job = client.get_schema_refresh_job_status(job_id)
        polls += 1
        if job.get("model_id") not in {None, model_id}:
            raise OmniAPIError("Schema refresh job status returned a mismatched model ID")
        status = job["status"]

    elapsed_seconds = max(0, round(monotonic() - started, 3))
    if status == "FAILED":
        return _sync_report(
            model_id=model_id,
            branch_id=branch_id,
            job_id=job_id,
            refresh_mode=refresh_mode,
            status="failed",
            omni_status=status,
            polls=polls,
            started_at=started_at,
            elapsed_seconds=elapsed_seconds,
            message="Omni schema refresh job failed",
        ), ExitCodes.OMNI_API_ERROR
    if status != "COMPLETED":
        raise OmniAPIError("Schema refresh finished with an unsupported status")
    return _sync_report(
        model_id=model_id,
        branch_id=branch_id,
        job_id=job_id,
        refresh_mode=refresh_mode,
        status="completed",
        omni_status=status,
        polls=polls,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
    ), ExitCodes.SUCCESS


def _sync_report(
    *,
    model_id: str,
    branch_id: str | None,
    job_id: str,
    refresh_mode: str,
    status: str,
    omni_status: str,
    polls: int,
    started_at: str,
    elapsed_seconds: float | int,
    message: str | None = None,
) -> dict[str, Any]:
    issues = []
    if message:
        issues.append({"validator": "dbt_sync", "severity": "error", "message": message})
    return {
        "tool": "omniflow",
        "operation": "dbt_sync",
        "generated_at": utc_now_iso(),
        "started_at": started_at,
        "model_id": model_id,
        "branch_id": branch_id,
        "job_id": job_id,
        "refresh_mode": refresh_mode,
        "status": status,
        "omni_status": omni_status,
        "polls": polls,
        "elapsed_seconds": elapsed_seconds,
        "raw_query_results_stored": False,
        "issues": issues,
        "summary": {
            "total_issues": len(issues),
            "errors": len(issues),
            "warnings": 0,
        },
    }
