"""Breaking-change hold policy for single-branch dbt and Omni monorepos.

Omni promotes authored model YAML as soon as a pull request merges into the
configured base branch. When the same repository also deploys dbt, a breaking
semantic change can reach the shared Omni model before the warehouse actually
contains the renamed or migrated objects, which breaks production content until
the dbt deployment finishes.

This module never contacts Omni or the warehouse. It compares the semantic diff
that OmniFlow already computed against repository path evidence, so it stays
inside the tool's read-only validation contract.
"""

from __future__ import annotations

import re

# Git is invoked without a shell and with bounded arguments.
import subprocess  # nosec B603,B404
from pathlib import Path
from typing import Any

from .config import BreakingChangeHoldSettings
from .exceptions import SecurityPolicyError
from .git import git_executable

VALIDATOR = "breaking_change_hold"
SAME_PR_RULE = "breaking_change_with_dbt_in_same_pull_request"
PENDING_RULE = "breaking_change_with_pending_dbt_deployment"
STATE_RULE = "breaking_change_sync_state_unavailable"
SAFE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
MAX_SAMPLE_CHANGES = 10


def evaluate_breaking_hold(
    *,
    diff_result: dict[str, Any] | None,
    changed_files: list[str],
    last_sync_sha: str | None,
    settings: BreakingChangeHoldSettings,
) -> list[dict[str, Any]]:
    """Return hold issues when a breaking Omni change is unsafe to merge yet."""
    if not settings.enabled or not isinstance(diff_result, dict):
        return []

    breaking_changes = [
        change
        for change in diff_result.get("changes", [])
        if isinstance(change, dict) and change.get("risk") == "breaking"
    ]
    if not breaking_changes:
        return []

    severity = "error" if settings.action == "fail" else "warning"

    overlapping = _dbt_overlap(changed_files, settings.dbt_paths)
    if overlapping:
        return [
            _issue(
                rule=SAME_PR_RULE,
                severity=severity,
                message=(
                    "This pull request changes dbt sources and makes breaking Omni model changes. "
                    "Omni promotes model YAML on merge, so the breaking references can reach production "
                    "before the dbt deployment updates the warehouse. Use expand/contract: deploy an "
                    "additive warehouse change retaining old names, synchronize, then update Omni and "
                    "all consumers. Remove old warehouse names only after consumer validation."
                ),
                breaking_changes=breaking_changes,
                dbt_paths=overlapping,
            )
        ]

    pending = _pending_dbt_paths(last_sync_sha, settings.dbt_paths)
    if pending is None:
        return [_issue(
            rule=STATE_RULE, severity=severity,
            message="Cannot establish deployment readiness: sync state is missing, unreachable, or not an ancestor "
                    "of the trusted base. Record a verified successful sync and fetch complete base history.",
            breaking_changes=breaking_changes, dbt_paths=[], last_sync_sha=last_sync_sha,
        )]
    if pending:
        return [
            _issue(
                rule=PENDING_RULE,
                severity=severity,
                message=(
                    "This pull request makes breaking Omni model changes while a dbt deployment is still "
                    "pending. dbt sources changed on the base branch after the last recorded "
                    "'omniflow dbt sync', so the warehouse may not contain the referenced objects yet. "
                    "Wait for the protected deployment to finish, or confirm the warehouse already has "
                    "the required schema."
                ),
                breaking_changes=breaking_changes,
                dbt_paths=pending,
                last_sync_sha=last_sync_sha,
            )
        ]

    return []


def hold_triggered(issues: list[dict[str, Any]]) -> bool:
    return any(issue.get("validator") == VALIDATOR for issue in issues)


def _issue(
    *,
    rule: str,
    severity: str,
    message: str,
    breaking_changes: list[dict[str, Any]],
    dbt_paths: list[str],
    last_sync_sha: str | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "validator": VALIDATOR,
        "rule": rule,
        "severity": severity,
        "message": message,
        "breaking_change_count": len(breaking_changes),
        "breaking_changes": [
            {
                "type": change.get("type"),
                "file": change.get("file"),
                "field": change.get("field"),
                "previous_field": change.get("previous_field"),
                "view": change.get("view"),
                "topic": change.get("topic"),
            }
            for change in breaking_changes[:MAX_SAMPLE_CHANGES]
        ],
        "dbt_paths": dbt_paths,
    }
    if last_sync_sha:
        issue["last_sync_sha"] = last_sync_sha
    return issue


def _dbt_overlap(changed_files: list[str], dbt_paths: list[str]) -> list[str]:
    matched: list[str] = []
    for path in changed_files:
        if not isinstance(path, str):
            continue
        for dbt_path in dbt_paths:
            if _is_under(path, dbt_path) and dbt_path not in matched:
                matched.append(dbt_path)
    return sorted(matched)


def _is_under(path: str, dbt_path: str) -> bool:
    normalized_path = path.strip().strip("/")
    normalized_dbt_path = dbt_path.strip().strip("/")
    if not normalized_path or not normalized_dbt_path:
        return False
    if normalized_dbt_path == ".":
        return True
    return normalized_path == normalized_dbt_path or normalized_path.startswith(f"{normalized_dbt_path}/")


def _pending_dbt_paths(last_sync_sha: str | None, dbt_paths: list[str]) -> list[str] | None:
    """Return dbt paths changed since the last recorded successful sync.

    Missing or unreachable state is incomplete evidence, never proof of readiness.
    """
    if not last_sync_sha:
        return None
    sha = last_sync_sha.strip()
    if not sha:
        return None
    if not SAFE_SHA_RE.fullmatch(sha):
        raise SecurityPolicyError(
            "OMNIFLOW_LAST_SYNC_SHA must be a hexadecimal Git commit SHA between 7 and 64 characters"
        )
    changed = _git_changed_files_since(sha)
    if changed is None:
        return None
    return _dbt_overlap(changed, dbt_paths)


def _git_changed_files_since(sha: str) -> list[str] | None:
    """List repository paths changed between a commit and HEAD.

    Returns None when history cannot establish the recorded deployment ancestor.
    """
    try:
        subprocess.run(  # nosec B603
            [git_executable(), "merge-base", "--is-ancestor", sha, "HEAD"],
            check=True, capture_output=True, timeout=10,
        )
        # Arguments are passed directly to Git, never through a shell.
        result = subprocess.run(  # nosec B603
            [git_executable(), "diff", "--name-only", "--no-renames", f"{sha}..HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    files: list[str] = []
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_absolute() or ".." in path.parts:
            continue
        if candidate not in files:
            files.append(candidate)
    return files
