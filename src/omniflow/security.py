from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .exceptions import SecurityPolicyError

SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password)", re.IGNORECASE)
SECRET_VALUE_RE = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"(OMNI_API_KEY=)[^\s]+|"
    r"([?&](?:api[_-]?key|token|secret|password)=)[^&\s]+",
    re.IGNORECASE,
)
EMAIL_VALUE_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
URL_VALUE_RE = re.compile(r"https?://[^\s<>\]\[)('`\"]+", re.IGNORECASE)
SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
RAW_KEYS = {"raw", "raw_issue", "raw_payload", "raw_response", "payload"}
STANDARD_PUBLIC_REDACT_KEYS = {
    "email",
    "owner_email",
    "content_url",
    "document_url",
    "url",
    "web_url",
    "folder_path",
    "folder_name",
}
STRICT_PUBLIC_REDACT_KEYS = STANDARD_PUBLIC_REDACT_KEYS | {
    "owner",
    "document_owner",
    "owner_name",
    "name",
    "content_name",
    "document_name",
    "query_name",
    "folder",
    "labels",
}
STRICT_TEXT_REDACT_KEYS = {"message", "summary"}


def contains_secret_key(key: Any) -> bool:
    return isinstance(key, str) and bool(SECRET_KEY_RE.search(key))


def find_secret_keys(payload: Any, prefix: str = "") -> list[str]:
    matches: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if contains_secret_key(key):
                matches.append(path)
            matches.extend(find_secret_keys(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            matches.extend(find_secret_keys(value, f"{prefix}[{index}]"))
    return matches


def reject_secret_keys(payload: Any, *, source: str) -> None:
    keys = find_secret_keys(payload)
    if keys:
        joined = ", ".join(sorted(keys))
        raise SecurityPolicyError(
            f"Secret-like keys are not allowed in {source}: {joined}. "
            "Use environment variables or a secret manager instead."
        )


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            redacted[key] = "[REDACTED]" if contains_secret_key(key) else redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        redacted = SECRET_VALUE_RE.sub(lambda match: _redact_match(match), value)
        secret = os.getenv("OMNI_API_KEY")
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "[REDACTED]")
        return EMAIL_VALUE_RE.sub("[REDACTED_EMAIL]", URL_VALUE_RE.sub("[REDACTED_URL]", redacted))
    return value


def public_safe(value: Any, *, redaction_level: str = "standard") -> Any:
    if redaction_level not in {"standard", "strict"}:
        raise SecurityPolicyError("security.redaction_level must be 'standard' or 'strict'")
    return _public_safe(value, strict=redaction_level == "strict")


def _public_safe(value: Any, *, strict: bool) -> Any:
    if isinstance(value, Mapping):
        safe = {}
        redact_keys = STRICT_PUBLIC_REDACT_KEYS if strict else STANDARD_PUBLIC_REDACT_KEYS
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in RAW_KEYS:
                continue
            if strict and normalized in STRICT_TEXT_REDACT_KEYS and isinstance(item, str):
                safe[key] = "[REDACTED]"
                continue
            if (
                contains_secret_key(key)
                or normalized in redact_keys
                or normalized.endswith("_url")
                or normalized.endswith("_email")
            ):
                safe[key] = "[REDACTED]"
            else:
                safe[key] = _public_safe(item, strict=strict)
        return safe
    if isinstance(value, list):
        return [_public_safe(item, strict=strict) for item in value]
    return redact(value)


def validate_base_url(value: str) -> str:
    parsed = urlparse(value)
    is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in ({"https"} if not is_loopback else {"http", "https"}):
        raise SecurityPolicyError("Omni base URL must use HTTPS (HTTP is allowed only for loopback testing)")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SecurityPolicyError("Omni base URL must contain only a trusted scheme, host, and optional port")
    if parsed.path not in {"", "/"}:
        raise SecurityPolicyError("Omni base URL must not include an API path")
    return value.rstrip("/")


def validate_path_segment(value: str, *, name: str) -> str:
    if not SAFE_PATH_SEGMENT_RE.fullmatch(value):
        raise SecurityPolicyError(f"{name} contains characters that are unsafe for an API path")
    return value


def validate_branch_name(value: str) -> str:
    if (
        not SAFE_BRANCH_RE.fullmatch(value)
        or value.startswith("-")
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
    ):
        raise SecurityPolicyError("branch_name is not a safe Git branch name")
    return value


def validate_repo_output_path(value: str | Path) -> Path:
    repo_root = Path.cwd().resolve()
    candidate = repo_root / Path(value)
    target = candidate.resolve()
    try:
        target.relative_to(repo_root)
    except ValueError as exc:
        raise SecurityPolicyError("OmniFlow output directory must resolve inside the repository") from exc
    lexical = Path(os.path.abspath(candidate))
    if target != lexical:
        raise SecurityPolicyError("OmniFlow output paths must not traverse symbolic links")
    return Path(value)


def secure_mkdir(path: str | Path, *, enforce_private: bool = False) -> Path:
    target = Path(path)
    if target.is_symlink():
        raise SecurityPolicyError("OmniFlow output directories must not be symbolic links")
    existed = target.exists()
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and (enforce_private or not existed):
        target.chmod(0o700)
    return target


def secure_write_text(path: str | Path, value: str) -> None:
    target = Path(path)
    secure_mkdir(target.parent)
    if target.is_symlink():
        raise SecurityPolicyError("OmniFlow output files must not be symbolic links")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        raise SecurityPolicyError(f"Could not safely write OmniFlow output file '{target}'") from exc
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(value)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _redact_match(match: re.Match[str]) -> str:
    for index in range(1, len(match.groups()) + 1):
        prefix = match.group(index)
        if prefix:
            return f"{prefix}[REDACTED]"
    return "[REDACTED]"


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True
