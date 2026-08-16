from __future__ import annotations

import os
import re

# Git is invoked without a shell and with validated inputs.
import subprocess  # nosec B404
from pathlib import Path

from .exceptions import ConfigError
from .git import git_executable, is_pull_request_event

SAFE_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
MAX_TRUSTED_FILE_BYTES = 1024 * 1024


def is_github_pull_request() -> bool:
    return is_pull_request_event() and bool(os.getenv("GITHUB_BASE_REF"))


def read_trusted_repo_text(path: str | Path, *, max_bytes: int = MAX_TRUSTED_FILE_BYTES) -> str | None:
    candidate = Path(path)
    if candidate.is_absolute() or not is_github_pull_request():
        if candidate.is_symlink():
            raise ConfigError(f"Trusted repository file '{candidate}' must not be a symbolic link")
        try:
            with candidate.open("rb") as stream:
                value = stream.read(max_bytes + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError(f"Could not read trusted repository file '{candidate}'") from exc
        if len(value) > max_bytes:
            raise ConfigError(f"Trusted repository file '{candidate}' exceeds the 1 MiB safety limit")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"Trusted repository file '{candidate}' is not valid UTF-8") from exc

    repo_path = _safe_repo_path(candidate)
    base_ref = _safe_base_ref(os.environ["GITHUB_BASE_REF"])
    for ref in (f"refs/remotes/origin/{base_ref}", f"refs/heads/{base_ref}"):
        object_name = f"{ref}:{repo_path}"
        try:
            size_result = subprocess.run(  # nosec B603
                [git_executable(), "cat-file", "-s", object_name],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if int(size_result.stdout.strip()) > max_bytes:
                raise ConfigError(f"Trusted repository file '{candidate}' exceeds the 1 MiB safety limit")
            result = subprocess.run(  # nosec B603
                [git_executable(), "show", object_name],
                check=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise ConfigError(f"Could not read trusted base revision for '{candidate}'") from exc
        except subprocess.CalledProcessError:
            continue
        if len(result.stdout) > max_bytes:
            raise ConfigError(f"Trusted repository file '{candidate}' exceeds the 1 MiB safety limit")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"Trusted repository file '{candidate}' is not valid UTF-8") from exc
    return None


def _safe_repo_path(path: Path) -> str:
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"Trusted repository path must stay inside the checkout: {path}")
    value = path.as_posix()
    while value.startswith("./"):
        value = value[2:]
    if not value:
        raise ConfigError("Trusted repository path cannot be empty")
    if len(value) > 1_024 or any(character in value for character in ("\x00", "\r", "\n", "\\", ":")):
        raise ConfigError("Trusted repository path contains unsafe characters")
    return value


def _safe_base_ref(value: str) -> str:
    if (
        not SAFE_GIT_REF_RE.fullmatch(value)
        or value.startswith("-")
        or ".." in value
        or "@{" in value
        or value.endswith("/")
    ):
        raise ConfigError("GITHUB_BASE_REF contains an unsafe Git reference")
    return value
