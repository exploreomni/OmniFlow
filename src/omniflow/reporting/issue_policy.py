"""Consistent policy interpretation for every report format.

Severity describes a finding's health impact. An explicit policy decision can
make an existing error nonblocking without changing that observed severity.
"""
from __future__ import annotations

from typing import Any


def is_blocking(issue: dict[str, Any]) -> bool:
    if issue.get("active") is False:
        return False
    blocking = issue.get("blocking")
    if type(blocking) is bool:
        return blocking
    # Reports written before explicit policy flags used error severity to gate.
    return issue.get("severity") == "error"
