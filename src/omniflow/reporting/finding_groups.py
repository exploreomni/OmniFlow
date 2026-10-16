"""Presentation grouping only; never changes findings or policy decisions."""
from __future__ import annotations

import json
from typing import Any

from .issue_policy import is_blocking


def group_findings(findings: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """Group identical public projections, not private objects or issue identities.

    Call only after public projection. Count occurrences, never imply unique
    objects. All fields participate so scope, state, severity and evidence
    distinctions survive; the original per-issue arrays remain unchanged.
    """
    groups: dict[str, tuple[dict[str, Any], int]] = {}
    for finding in findings:
        key = json.dumps(finding, sort_keys=True)
        previous, count = groups.get(key, (finding, 0))
        groups[key] = previous, count + 1
    return sorted(groups.values(), key=lambda group: (
        not is_blocking(group[0]), group[0].get("active") is False,
    ))
