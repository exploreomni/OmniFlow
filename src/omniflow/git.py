from __future__ import annotations

import json
import os
import shutil

# Git is invoked without a shell and with bounded arguments.
import subprocess  # nosec B404
from pathlib import Path


def git_value(*args: str) -> str | None:
    try:
        # Arguments are passed directly to Git, never through a shell.
        result = subprocess.run(  # nosec B603
            [git_executable(), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        raise OSError("Git executable was not found on PATH")
    return executable


def current_sha() -> str | None:
    pull_request = _pull_request_payload()
    head_sha = (pull_request.get("head") or {}).get("sha")
    return head_sha or os.getenv("GITHUB_SHA") or git_value("rev-parse", "HEAD")


def current_branch() -> str | None:
    pull_request = _pull_request_payload()
    head_ref = (pull_request.get("head") or {}).get("ref")
    return (
        os.getenv("GITHUB_HEAD_REF")
        or head_ref
        or os.getenv("GITHUB_REF_NAME")
        or git_value("branch", "--show-current")
    )


def pr_number() -> str | None:
    explicit = os.getenv("GITHUB_EVENT_NUMBER")
    if explicit:
        return explicit
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if event_path:
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            number = event.get("number") or (event.get("pull_request") or {}).get("number")
            if number is not None:
                return str(number)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    ref = os.getenv("GITHUB_REF", "")
    parts = ref.split("/")
    if len(parts) >= 3 and parts[0:2] == ["refs", "pull"] and parts[2].isdigit():
        return parts[2]
    return None


def event_name() -> str | None:
    return os.getenv("GITHUB_EVENT_NAME")


def is_pull_request_event() -> bool:
    return event_name() in {"pull_request", "pull_request_target"}


def github_event_payload() -> dict:
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pull_request_payload() -> dict:
    payload = github_event_payload().get("pull_request")
    return payload if isinstance(payload, dict) else {}
