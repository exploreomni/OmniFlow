"""Write an authorized candidate location to the private Actions run summary."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .markdown_report import candidate_location_lines
from .recovery import first_step, mismatch_description


def write_private_candidate_summary(projected: dict) -> bool:
    """Called only by the live repository-policy gate, never when rendering a saved report."""
    destination, runner_temp = os.getenv("GITHUB_STEP_SUMMARY"), os.getenv("RUNNER_TEMP")
    if not destination or not runner_temp or os.getenv("GITHUB_ACTIONS") != "true":
        return False
    path = Path(destination)
    if not path.is_absolute() or not re.fullmatch(r"step_summary_[0-9a-fA-F-]{36}", path.name):
        return False
    try:
        real_parent = Path(runner_temp).resolve(strict=True) / "_runner_file_commands"
        # GitHub may mount the same command directory here for container jobs.
        if path.parent not in {real_parent, Path("/github/file_commands")} or path.parent.resolve(strict=True) != path.parent:
            return False
        cards = []
        for finding in projected.get("issues", []):
            location = candidate_location_lines(finding, projected)
            if location:
                heading = "PR change not synchronized" if finding.get("candidate_reason") == "pr_change_not_synchronized" else "Content mismatch"
                cards.extend([f"## {heading}", "", *location, "", mismatch_description(finding), "", "**Fix:** " + first_step(finding), ""])
                if len(cards) >= 30:
                    break
        if not cards:
            return False
        # Only append to an existing runner-created regular file. Never create an
        # output, follow a symlink, or block on a pipe supplied in its place.
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), "w", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return False
            stream.write("\n".join([*cards, ""]))
        return True
    except (OSError, RuntimeError):
        # Summary delivery is optional and cannot affect the validation result.
        return False
