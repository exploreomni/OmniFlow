"""Fail-closed eligibility for repository-backed (never raw API) diagnostics.

The config object can restrict disclosure, but cannot authorize it. Eligibility
is independently established from GitHub's live repository/PR metadata and a
literal policy read at the verified immutable trusted revision. No PR code is
checked out or executed. Unsupported contexts keep the minimal public schema.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .config import DEFAULT_CONFIG_PATH, OmniFlowConfig, _diagnostics_mode, _validate_config_schema
from .exceptions import OmniFlowError
from .git import git_executable, github_event_payload
from .revision_data import FULL_SHA, REPOSITORY, read_github_text
from .security import reject_secret_keys
from .trust import MAX_TRUSTED_FILE_BYTES, _safe_repo_path
from .yaml_security import parse_secure_yaml

POLICY_REASONS = frozenset({
    "repository_verified", "minimal_requested", "not_github_actions", "unsupported_event",
    "unsupported_github_origin", "repository_unverified", "repository_not_private", "fork_pull_request",
    "revision_unverified", "stale_revision", "policy_unavailable", "policy_invalid", "unsafe_policy_path",
    "github_lookup_unavailable",
})
MAX_METADATA_BYTES = 256 * 1024


@dataclass(frozen=True)
class RepositoryPolicy:
    enabled: bool
    reason: str
    repository: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    external_diagnostics_allowed: bool = False


class _Ineligible(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def resolve_repository_policy(config: OmniFlowConfig) -> RepositoryPolicy:
    """Return a fixed safe reason on failure; never expose exception/payload text.

GitHub Actions' trusted runner/workflow is the authority for event/environment
inputs. This gate cannot make executing hostile workflow code safe. Only the
GitHub.com API origin is supported; enterprise/custom origins remain minimal.
    """
    try:
        return _resolve(config)
    except _Ineligible as exc:
        return RepositoryPolicy(False, exc.reason)
    except (OmniFlowError, OSError, ValueError, TypeError, AttributeError, KeyError):
        return RepositoryPolicy(False, "github_lookup_unavailable")


def _resolve(config: OmniFlowConfig) -> RepositoryPolicy:
    # A caller-supplied minimal setting only narrows disclosure. A supplied
    # repository setting is not evidence that the base policy authorizes it.
    mode = getattr(getattr(config, "reporting", None), "diagnostics_mode", None)
    if mode == "minimal":
        raise _Ineligible("minimal_requested")
    if mode != "repository":
        raise _Ineligible("policy_invalid")
    if os.getenv("GITHUB_ACTIONS") != "true":
        raise _Ineligible("not_github_actions")
    event_name = os.getenv("GITHUB_EVENT_NAME")
    if event_name not in {"pull_request", "pull_request_target", "push", "workflow_dispatch"}:
        raise _Ineligible("unsupported_event")
    if (os.getenv("GITHUB_SERVER_URL") != "https://github.com"
            or os.getenv("GITHUB_API_URL") != "https://api.github.com"):
        raise _Ineligible("unsupported_github_origin")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    repository_id = os.getenv("GITHUB_REPOSITORY_ID", "")
    if not REPOSITORY.fullmatch(repository) or not repository_id.isascii() or not repository_id.isdecimal():
        raise _Ineligible("repository_unverified")
    identifier = int(repository_id)
    if identifier < 1:
        raise _Ineligible("repository_unverified")
    event = github_event_payload()
    if not _same_repository(event.get("repository"), repository, identifier):
        raise _Ineligible("repository_unverified")
    token = os.getenv("OMNIFLOW_GITHUB_TOKEN", "")
    if not token:
        raise _Ineligible("github_lookup_unavailable")
    # Reuse the bounded transport, with origin independently constrained above.
    from .github.revalidation import GitHubRepository

    api = GitHubRepository(repository, token)
    metadata = api.request("GET", "", max_bytes=MAX_METADATA_BYTES)
    if not _same_repository(metadata, repository, identifier):
        raise _Ineligible("repository_unverified")
    if metadata.get("private") is not True or metadata.get("visibility") != "private":
        raise _Ineligible("repository_not_private")
    if event_name in {"pull_request", "pull_request_target"}:
        head, base = _pull_request_revisions(api, event, repository, identifier)
    else:
        head = base = _default_branch_revision(api, event, metadata, event_name)
    path = _policy_path(config.source)
    try:
        text = read_github_text(repository, base, path, max_bytes=MAX_TRUSTED_FILE_BYTES)
    except OmniFlowError:
        raise _Ineligible("policy_unavailable") from None
    if text is None:
        raise _Ineligible("policy_unavailable")
    try:
        payload = parse_secure_yaml(text, source="trusted repository policy")
        if not isinstance(payload, dict) or not payload:
            raise _Ineligible("policy_invalid")
        reject_secret_keys(payload, source="trusted repository policy")
        _validate_config_schema(payload)
        trusted_mode = _diagnostics_mode(payload.get("reporting") or {})
        from .config import _omni_repository_settings

        trusted_dbt = ((payload.get("checks") or {}).get("dbt_impact") or {})
        trusted_external = _omni_repository_settings(trusted_dbt.get("omni_repository"))
    except OmniFlowError:
        raise _Ineligible("policy_invalid") from None
    if trusted_mode != "repository":
        raise _Ineligible("minimal_requested")
    # Private destination access does not itself authorize copying metadata
    # from another repository. Require the same literal source scope and opt-in
    # from the independently re-read trusted policy; caller config can only
    # narrow this authority, never create or redirect it.
    external_allowed = (trusted_external is not None and trusted_external.allow_repository_diagnostics is True
                        and trusted_external == config.dbt_impact.omni_repository)
    return RepositoryPolicy(True, "repository_verified", repository, head, base,
                            external_diagnostics_allowed=external_allowed)


def _same_repository(value, repository: str, identifier: int) -> bool:
    return (isinstance(value, dict) and value.get("full_name") == repository
            and type(value.get("id")) is int and value["id"] == identifier)


def _sha(value) -> str:
    if not isinstance(value, str) or not FULL_SHA.fullmatch(value):
        raise _Ineligible("revision_unverified")
    return value.lower()


def _pull_request_revisions(api, event: dict, repository: str, identifier: int) -> tuple[str, str]:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        raise _Ineligible("revision_unverified")
    number = event.get("number", pr.get("number"))
    if type(number) is not int or number < 1:
        raise _Ineligible("revision_unverified")
    if "number" in pr and pr["number"] != number:
        raise _Ineligible("revision_unverified")
    for side in ("head", "base"):
        value = pr.get(side)
        if not isinstance(value, dict):
            raise _Ineligible("revision_unverified")
        if not _same_repository(value.get("repo"), repository, identifier):
            raise _Ineligible("fork_pull_request")
    current = api.request("GET", f"/pulls/{number}", max_bytes=MAX_METADATA_BYTES)
    if not isinstance(current, dict) or current.get("number") != number or current.get("state") != "open":
        raise _Ineligible("revision_unverified")
    revisions = []
    for side, env_ref in (("head", "GITHUB_HEAD_REF"), ("base", "GITHUB_BASE_REF")):
        recorded, live = pr[side], current.get(side)
        if not isinstance(live, dict) or not _same_repository(live.get("repo"), repository, identifier):
            raise _Ineligible("fork_pull_request")
        sha = _sha(recorded.get("sha"))
        if _sha(live.get("sha")) != sha:
            raise _Ineligible("stale_revision")
        ref = recorded.get("ref")
        if not isinstance(ref, str) or not ref or ref != live.get("ref") or ref != os.getenv(env_ref):
            raise _Ineligible("revision_unverified")
        revisions.append(sha)
    return revisions[0], revisions[1]


def _default_branch_revision(api, event: dict, metadata: dict, event_name: str) -> str:
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch or len(branch) > 255:
        raise _Ineligible("revision_unverified")
    ref = f"refs/heads/{branch}"
    if os.getenv("GITHUB_REF") != ref:
        raise _Ineligible("revision_unverified")
    sha = _sha(os.getenv("GITHUB_SHA"))
    if event_name == "push":
        if event.get("ref") != ref or event.get("deleted") is not False or _sha(event.get("after")) != sha:
            raise _Ineligible("revision_unverified")
    elif "ref" in event and event["ref"] not in {branch, ref}:
        raise _Ineligible("revision_unverified")
    if _checkout_value("rev-parse", "HEAD") != sha:
        raise _Ineligible("revision_unverified")
    live = api.request("GET", f"/git/ref/heads/{quote(branch, safe='')}", max_bytes=MAX_METADATA_BYTES)
    obj = live.get("object") if isinstance(live, dict) else None
    if not isinstance(obj, dict) or obj.get("type") != "commit" or live.get("ref") != ref:
        raise _Ineligible("revision_unverified")
    if _sha(obj.get("sha")) != sha:
        raise _Ineligible("stale_revision")
    return sha


def _checkout_value(*args: str) -> str:
    try:
        result = subprocess.run(  # nosec B603
            [git_executable(), "--no-replace-objects", *args], check=True,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise _Ineligible("revision_unverified") from None
    return result.stdout.strip()


def _policy_path(source: Path | None) -> str:
    """Interpret paths, never local policy contents; reject symlink indirection."""
    root = Path(_checkout_value("rev-parse", "--show-toplevel")).resolve()
    source = Path(source or DEFAULT_CONFIG_PATH)
    if ".." in source.parts:
        raise _Ineligible("unsafe_policy_path")
    absolute = source if source.is_absolute() else Path.cwd() / source
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        raise _Ineligible("unsafe_policy_path") from None
    if any(path.is_symlink() for path in (absolute, *absolute.parents) if path != root and root in path.parents):
        raise _Ineligible("unsafe_policy_path")
    try:
        return _safe_repo_path(relative)
    except OmniFlowError:
        raise _Ineligible("unsafe_policy_path") from None
