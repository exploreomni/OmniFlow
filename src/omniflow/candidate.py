"""Read-only evidence that an Omni candidate matches an immutable PR snapshot."""

from __future__ import annotations

import hashlib
import os
import subprocess  # nosec B404
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path, PurePosixPath
from typing import Any

from .artifact_identity import ArtifactIdentityError, identity_digest, match_artifacts
from .candidate_change_scope import equivalent, scope_digest, select_scope, unproven_absence
from .candidate_difference import difference_details, shareable_difference_fields
from .exceptions import ConfigError, OmniFlowError, exception_diagnostic
from .file_scope import FileScopeError, NonOmniRule, excluded_semantic_paths
from .git import git_executable
from .model_paths import (
    AmbiguousModelPath,
    filename_identity,
    identity_index,
    is_git_model_file,
    is_repo_control_file,
    is_semantic_file,
)
from .revision_data import FULL_SHA, pull_request_revision, read_git_text, read_github_text
from .view_identity import normalize_api_view_names
from .yaml_security import MAX_YAML_FILE_BYTES, MAX_YAML_FILES, MAX_YAML_TOTAL_BYTES

MAX_TREE_BYTES = 2 * 1024 * 1024
MAX_TREE_ENTRIES = 10_000
MAX_CANDIDATE_SAMPLES = 5
CANDIDATE_COUNT_FIELDS = frozenset({
    "git_file_count", "omni_file_count", "git_only_count", "omni_only_count",
    "case_only_count", "directory_only_count", "suffix_only_count",
    "candidate_sample_count", "candidate_sample_omitted_count", "compared_file_count",
})


class CandidateVerificationError(ConfigError):
    """Public diagnostics use categories only, never model paths or contents."""

    def __init__(self, reason: str, *, candidate_samples: Sequence[dict] = (), **counts: int) -> None:
        self.candidate_reason = reason
        # Private observations only. Renderers separately authorize disclosure.
        self.candidate_samples = [dict(sample) for sample in candidate_samples[:MAX_CANDIDATE_SAMPLES]]
        self.candidate_counts = {
            key: value for key, value in counts.items()
            if key in CANDIDATE_COUNT_FIELDS and type(value) is int and 0 <= value <= 2**31 - 1
        }
        super().__init__(
            f"Candidate provenance verification failed ({reason}). "
            "The selected Omni candidate or base must exactly match the corresponding immutable PR authored YAML. "
            "Check candidate/base synchronization and sanitized snapshot evidence; do not bypass verification."
        )


class _GitUnavailable(Exception):
    def __init__(self, reason_code: str = "git_revision_unavailable") -> None:
        self.reason_code = reason_code


def _retain_candidate_cause(exc: OmniFlowError, reason: str) -> None:
    """Keep the original auth/API/security type and structured transport cause."""
    exc.candidate_reason = reason
    cause = exception_diagnostic(exc).get("reason_code")
    exc.diagnostic_context["candidate_reason"] = reason
    if isinstance(cause, str):
        exc.diagnostic_context.setdefault("cause_code", cause)


def _bind_samples(exc: CandidateVerificationError, *, sha: str, model_id: str, branch_id: str | None,
                  comparison: str) -> CandidateVerificationError:
    # These are private binding evidence, never permission to disclose a sample.
    exc.candidate_git_sha = sha
    exc.candidate_model_id = model_id
    exc.candidate_branch_id = branch_id
    exc.contents_comparison = comparison
    exc.diagnostic_context["contents_comparison"] = comparison
    return exc


def verify_candidate(
    client: Any, context: Any, *, revision_side: str = "head", non_omni_rules: Sequence[NonOmniRule] = (),
    changed_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compare complete authored semantic snapshots without checking out PR code.

    Call before and after validation and compare the complete returned proofs.
    This is sampled content equality, not an atomic validation/revision lock.
    """
    try:
        return _verify_snapshot(client, context, revision_side=revision_side, non_omni_rules=non_omni_rules,
                                changed_paths=changed_paths)
    except OmniFlowError as exc:
        exc.validation_scope = "base" if revision_side == "base" else "branch"
        exc.diagnostic_context["validation_scope"] = exc.validation_scope
        for sample in getattr(exc, "candidate_samples", []):
            sample["validation_scope"] = exc.validation_scope
        raise


def _verify_snapshot(
    client: Any, context: Any, *, revision_side: str, non_omni_rules: Sequence[NonOmniRule] = (),
    local_diagnosis_revision: tuple[str, str] | None = None,
    local_preview: Callable[[str, str, dict], None] | None = None,
    changed_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    if revision_side not in {"head", "base"}:
        raise CandidateVerificationError("invalid_pr_revision")
    branch_id = getattr(context, "branch_id", None) if revision_side == "head" else None
    if revision_side == "head" and (not isinstance(branch_id, str) or not branch_id.strip()):
        raise CandidateVerificationError("missing_candidate_branch")
    scope = getattr(context, "model_path", None)
    model_path = "." if scope == "." else _safe_path(scope)
    try:
        revision = local_diagnosis_revision or pull_request_revision(revision_side)
    except ConfigError as exc:
        error = CandidateVerificationError("invalid_pr_revision")
        error.diagnostic_context.update(exception_diagnostic(exc))
        raise error from exc
    except OmniFlowError as exc:
        _retain_candidate_cause(exc, "invalid_pr_revision")
        raise
    except (AttributeError, TypeError, ValueError):
        raise CandidateVerificationError("invalid_pr_revision") from None
    if revision is None:
        raise CandidateVerificationError("missing_pr_revision")
    sha, repository = revision
    root = Path(".")
    try:
        entries = (_local_tree(sha, root=root, local_only=True) if local_diagnosis_revision
                   else _local_tree(sha, root=root))
        remote = False
    except _GitUnavailable as local_error:
        if local_diagnosis_revision is not None:
            raise CandidateVerificationError("git_snapshot_unavailable") from None
        try:
            entries = _remote_tree(repository, sha)
        except OmniFlowError as exc:
            exc.diagnostic_context["local_git_reason"] = local_error.reason_code
            raise
        remote = True
    expected = _semantic_inventory(entries, model_path, include_unrecognized=True)
    if not expected and changed_paths is None:
        raise CandidateVerificationError("empty_git_snapshot")

    try:
        payload = client.get_model_yaml(
            context.model_id, branch_id=branch_id, mode="combined", include_checksums=False,
            fully_resolved=False,
        )
    except OmniFlowError as exc:
        _retain_candidate_cause(exc, "api_snapshot_unavailable")
        raise
    actual = _api_files(payload)
    counts = {"git_file_count": len(expected), "omni_file_count": len(actual)}
    # Keep the complete inventories through path, type, size and collision
    # validation. Rules cannot erase malformed evidence or real Omni sources.
    try:
        if changed_paths is None:
            identity_index(expected)
    except AmbiguousModelPath:
        raise CandidateVerificationError("ambiguous_file_identity", **counts) from None
    try:
        excluded = excluded_semantic_paths(expected, actual, model_path=model_path, rules=non_omni_rules)
    except FileScopeError as exc:
        raise CandidateVerificationError(exc.file_scope_reason, **counts) from None
    if excluded:
        expected = {path: entry for path, entry in expected.items() if path not in excluded}
    expected = _source_inventory(expected, actual)
    scope = None
    if changed_paths is not None:
        scope = select_scope(changed_paths, model_path, expected, actual, payload)
        expected, actual = scope.git, scope.api
        try:
            identity_index(expected)
        except AmbiguousModelPath:
            raise CandidateVerificationError("ambiguous_file_identity") from None
    counts.update(git_file_count=len(expected), omni_file_count=len(actual))
    if not expected and scope is None:
        raise CandidateVerificationError("empty_git_snapshot", **counts)
    # Read only immutable Git objects, binding every returned byte to the tree's
    # blob hash before using authored qualification to establish correspondence.
    git_files = {}
    for path, entry in sorted(expected.items()):
        try:
            if local_diagnosis_revision is not None:
                # Read the already inventoried blob directly. Never lazy-fetch
                # missing objects or invoke configured remote credential helpers.
                try:
                    text = _bounded_git(["cat-file", "blob", entry["sha"]], root=root,
                                        limit=MAX_YAML_FILE_BYTES, local_only=True).decode("utf-8")
                except _GitUnavailable:
                    raise CandidateVerificationError("git_snapshot_unavailable") from None
                except UnicodeError:
                    raise CandidateVerificationError("invalid_snapshot_encoding") from None
            elif remote:
                text = read_github_text(repository, sha, entry["path"], max_bytes=MAX_YAML_FILE_BYTES)
            else:
                text = read_git_text(sha, entry["path"], root=root, max_bytes=MAX_YAML_FILE_BYTES)
        except OmniFlowError as exc:
            _retain_candidate_cause(exc, "git_snapshot_unavailable")
            raise
        if not isinstance(text, str):
            raise CandidateVerificationError("incomplete_git_snapshot")
        raw = _utf8(text)
        # Bind the helper's content to the blob in the independently checked
        # inventory, including when local Git replacement refs are configured.
        algorithm = hashlib.sha1 if len(entry["sha"]) == 40 else hashlib.sha256  # nosec B303,B324
        blob = algorithm(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()  # nosec B324
        if len(raw) != entry["size"] or blob != entry["sha"].lower():
            raise CandidateVerificationError("incomplete_git_snapshot")
        git_files[path] = text
    omni_files = {path: raw.decode("utf-8") for path, raw in actual.items()}
    view_names = scope.view_names if scope is not None else normalize_api_view_names(
        payload["viewNames"], omni_files, parse_contents=False,
    ) if "viewNames" in payload else None
    try:
        correspondence = match_artifacts(git_files, omni_files, omni_view_names=view_names)
    except ArtifactIdentityError as exc:
        samples = [{"git_path": item.get("git_path"), "omni_path": item.get("omni_path"),
                    "comparison": "not_compared", "pairing": "ambiguous_identity"}
                   for item in exc.candidates[:MAX_CANDIDATE_SAMPLES]]
        raise _bind_samples(CandidateVerificationError(
            "ambiguous_file_identity", candidate_samples=samples, **counts,
            candidate_sample_count=len(samples),
            candidate_sample_omitted_count=max(0, exc.candidate_count - len(samples)), compared_file_count=0,
        ), sha=sha, model_id=context.model_id, branch_id=branch_id, comparison="not_compared") from None
    counts.update(git_only_count=len(correspondence.unmatched_git),
                  omni_only_count=len(correspondence.unmatched_omni))
    if counts["git_only_count"] or counts["omni_only_count"]:
        counts.update(_mismatch_category_counts(correspondence.unmatched_git, correspondence.unmatched_omni))
        pairs = _mismatch_pairs(correspondence.unmatched_git, correspondence.unmatched_omni)
        samples = [{"git_path": left, "omni_path": right, "comparison": "not_compared", "pairing": pairing}
                   for left, right, pairing in pairs[:MAX_CANDIDATE_SAMPLES]]
        raise _bind_samples(CandidateVerificationError(
            "pr_change_not_synchronized" if scope is not None else "file_inventory_mismatch",
            candidate_samples=samples, **counts,
            candidate_sample_count=len(samples), candidate_sample_omitted_count=len(pairs) - len(samples),
            compared_file_count=0,
        ), sha=sha, model_id=context.model_id, branch_id=branch_id, comparison="not_compared")
    if scope is not None:
        # A survivor with a coincidentally unique basename cannot prove which
        # previously colliding view was removed. Never rely on identity-by-bytes.
        def other_inventory() -> set[str]:
            opposite = pull_request_revision("base" if revision_side == "head" else "head")
            if opposite is None or opposite[1] != repository:
                raise CandidateVerificationError("invalid_pr_revision")
            try:
                other_tree = _local_tree(opposite[0], root=root)
            except _GitUnavailable:
                other_tree = _remote_tree(opposite[1], opposite[0])
            return set(_semantic_inventory(other_tree, model_path, include_unrecognized=True))
        if unproven_absence(scope, correspondence.pairing_methods, other_inventory):
            raise CandidateVerificationError("pr_change_not_synchronized", **counts)
    digest = hashlib.sha256()
    to_compare = (git_files if scope is None else {p: t for p, t in git_files.items() if p in scope.touched})
    for compared_count, (path, text) in enumerate(sorted(to_compare.items()), start=1):
        raw = _utf8(text)
        api_path = correspondence.pairs[path]
        if raw != actual[api_path] and (scope is None or not equivalent(text, omni_files[api_path])):
            sample = {"git_path": path, "omni_path": api_path,
                      "comparison": "different", "pairing": "authored_identity"}
            detail = difference_details(text, omni_files[api_path])
            sample.update(detail)
            error = CandidateVerificationError(
                "pr_change_not_synchronized" if scope is not None else "content_mismatch", **counts,
                candidate_samples=[sample],
                candidate_sample_count=1, candidate_sample_omitted_count=0, compared_file_count=compared_count,
            )
            # Never place paths, line positions or source text in public context.
            error.diagnostic_context.update(shareable_difference_fields(detail))
            if local_diagnosis_revision is not None and local_preview is not None:
                local_preview(text, omni_files[api_path], detail)
            raise _bind_samples(error, sha=sha, model_id=context.model_id, branch_id=branch_id, comparison="different")
        name = path.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name + len(raw).to_bytes(8, "big") + raw)
    if scope is not None:
        return {
            "status": "verified" if scope.touched else "scope_empty", "method": "pr_changed_files",
            f"{revision_side}_sha": sha, "file_count": len(to_compare), "changed_file_count": len(scope.touched),
            "absent_file_count": len(scope.touched - git_files.keys()),
            "scope_digest": scope_digest(scope, correspondence.pairs, sha=sha, side=revision_side,
                                         model_path=model_path, model_id=context.model_id, branch_id=branch_id),
            "identity_digest": identity_digest(correspondence.pairs, omni_view_names=view_names),
        }
    proof = {
        "status": "verified", "method": "authored_yaml_snapshot", f"{revision_side}_sha": sha,
        "file_count": len(expected), "snapshot_digest": digest.hexdigest(),
        "identity_digest": identity_digest(correspondence.pairs, omni_view_names=view_names),
    }
    if non_omni_rules:
        proof["excluded_file_count"] = len(excluded)
    return proof


def _mismatch_category_counts(expected: set[str], actual: set[str]) -> dict[str, int]:
    """Describe unique unmatched pairs without relaxing equality or exposing names."""
    pairs = _mismatch_pairs(expected, actual)
    return {category: sum(pairing == hint for _, _, pairing in pairs) for category, hint in (
        ("case_only_count", "case_hint"), ("directory_only_count", "basename_hint"),
        ("suffix_only_count", "suffix_hint"),
    )}


def _mismatch_pairs(expected: set[str], actual: set[str]) -> list[tuple[str | None, str | None, str]]:
    """Observational hints; never used to select files or establish equality."""
    git_only, omni_only = expected - actual, actual - expected
    result = []
    for hint, key, normalized_case in (
        ("case_hint", str.casefold, False),
        ("case_hint", lambda path: filename_identity(path).casefold(), True),
        ("basename_hint", lambda path: PurePosixPath(path).name, False),
        ("suffix_hint", _without_yaml_suffixes, False),
    ):
        left: dict[str, list[str]] = defaultdict(list)
        right: dict[str, list[str]] = defaultdict(list)
        for path in git_only:
            left[key(path)].append(path)
        for path in omni_only:
            right[key(path)].append(path)
        pairs = sorted((paths[0], right[name][0]) for name, paths in left.items()
                       if len(paths) == 1 and len(right.get(name, [])) == 1
                       and (not normalized_case or filename_identity(paths[0]) != filename_identity(right[name][0])))
        result.extend((left, right, hint) for left, right in pairs)
        git_only.difference_update(path for path, _ in pairs)
        omni_only.difference_update(path for _, path in pairs)
    result.extend((path, None, "unpaired") for path in sorted(git_only))
    result.extend((None, path, "unpaired") for path in sorted(omni_only))
    return result


def _without_yaml_suffixes(path: str) -> str:
    # Reporting only: unlike authored_file_identity, this can describe generic
    # or repeated wrappers. It must never determine snapshot correspondence.
    while path.endswith((".yaml", ".yml")):
        path = path.rsplit(".", 1)[0]
    return path


def _safe_path(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "\\" in value or ":" in value or PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts or PurePosixPath(value).as_posix() != value or value == "."
    ):
        raise CandidateVerificationError("unsafe_snapshot_path")
    _utf8(value)
    return value


def _semantic_path(path: str) -> bool:
    return is_semantic_file(path)


def _utf8(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeError:
        raise CandidateVerificationError("invalid_snapshot_encoding") from None


def _api_files(payload: Any) -> dict[str, bytes]:
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict) or not files:
        raise CandidateVerificationError("invalid_api_snapshot")
    if len(files) > MAX_YAML_FILES:
        raise CandidateVerificationError("snapshot_limit_exceeded")
    result: dict[str, bytes] = {}
    total = 0
    for path, value in files.items():
        path = _safe_path(path)
        if path in result or not _semantic_path(path):
            raise CandidateVerificationError("invalid_api_snapshot")
        if isinstance(value, dict):
            variants = [value[key] for key in ("content", "contents") if key in value]
            if not variants or any(not isinstance(item, str) for item in variants) or len(set(variants)) != 1:
                raise CandidateVerificationError("invalid_api_snapshot")
            value = variants[0]
        if not isinstance(value, str):
            raise CandidateVerificationError("invalid_api_snapshot")
        raw = _utf8(value)
        total += len(raw)
        if len(raw) > MAX_YAML_FILE_BYTES or total > MAX_YAML_TOTAL_BYTES:
            raise CandidateVerificationError("snapshot_limit_exceeded")
        result[path] = raw
    return result


def _source_inventory(inventory: dict[str, dict], api_files: dict) -> dict[str, dict]:
    """Keep recognized Git sources and exact API-confirmed custom YAML names.

    Unrelated Git-only YAML is ignored without reading its contents. API source
    evidence is never discarded merely because its name is not a default.
    """
    api_names = {filename_identity(path) for path in api_files}
    selected = {
        path: entry for path, entry in inventory.items()
        if is_git_model_file(path) or filename_identity(path) in api_names
    }
    if (len(selected) > MAX_YAML_FILES
            or any(entry["size"] > MAX_YAML_FILE_BYTES for entry in selected.values())
            or sum(entry["size"] for entry in selected.values()) > MAX_YAML_TOTAL_BYTES):
        raise CandidateVerificationError("snapshot_limit_exceeded")
    return selected


def _semantic_inventory(
    entries: list[dict], model_path: str, *, include_unrecognized: bool = False,
) -> dict[str, dict]:
    if len(entries) > MAX_TREE_ENTRIES:
        raise CandidateVerificationError("snapshot_limit_exceeded")
    seen: set[str] = set()
    selected: dict[str, dict] = {}
    prefix = "" if model_path == "." else model_path + "/"
    ancestors = {str(parent) for parent in PurePosixPath(model_path).parents if str(parent) != "."}
    for entry in entries:
        if not isinstance(entry, dict):
            raise CandidateVerificationError("invalid_git_inventory")
        path = _safe_path(entry.get("path"))
        if path in seen:
            raise CandidateVerificationError("duplicate_git_path")
        seen.add(path)
        # These repository-control files are not Omni authored model files,
        # including when the registered model lives at the repository root.
        if is_repo_control_file(path):
            continue
        if path == model_path or path in ancestors:
            if entry.get("type") != "tree" or entry.get("mode") != "040000":
                raise CandidateVerificationError("unsafe_git_entry")
            continue
        if not path.startswith(prefix):
            continue
        if entry.get("type") == "tree" and entry.get("mode") == "040000":
            continue
        if entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
            raise CandidateVerificationError("unsafe_git_entry")
        relative = path[len(prefix):]
        if not (is_semantic_file(relative) if include_unrecognized else is_git_model_file(relative)):
            continue
        size, blob_sha = entry.get("size"), entry.get("sha")
        if type(size) is not int or size < 0 or not isinstance(blob_sha, str) or not FULL_SHA.fullmatch(blob_sha):
            raise CandidateVerificationError("invalid_git_inventory")
        selected[relative] = entry
    # A broad inventory is bounded by tree limits. Source byte/count limits
    # apply after API correspondence so unrelated YAML cannot exhaust them.
    return selected if include_unrecognized else _source_inventory(selected, {})


def _bounded_git(args: list[str], *, root: Path, limit: int, local_only: bool = False) -> bytes:
    """Bound captured bytes as well as duration; kill before draining overflow."""
    try:
        executable = git_executable()
    except OSError:
        raise _GitUnavailable("git_missing_executable") from None
    try:
        options = ["--no-replace-objects"]
        env = None
        if local_only:
            options += ["--no-optional-locks", "--no-pager", "-c", "protocol.allow=never", "-c", "gc.auto=0"]
            env = {**os.environ, "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
        with subprocess.Popen(  # nosec B603
            [executable, *options, *args], cwd=root, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
            with ThreadPoolExecutor(max_workers=1) as reader:
                future = reader.submit(process.stdout.read, limit + 1)
                try:
                    value = future.result(timeout=15)
                    if len(value) > limit:
                        raise CandidateVerificationError("snapshot_limit_exceeded")
                    if process.wait(timeout=15) != 0:
                        raise _GitUnavailable()
                    return value
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
    except (subprocess.TimeoutExpired, TimeoutError):
        raise _GitUnavailable("git_timeout") from None
    except OSError:
        raise _GitUnavailable() from None
    except subprocess.SubprocessError:
        raise _GitUnavailable() from None


def _local_tree(sha: str, *, root: Path, local_only: bool = False) -> list[dict]:
    if _bounded_git(["cat-file", "-t", sha], root=root, limit=32, local_only=local_only).strip() != b"commit":
        raise CandidateVerificationError("invalid_pr_revision")
    raw = _bounded_git(["ls-tree", "--full-tree", "-r", "-l", "-z", sha], root=root,
                       limit=MAX_TREE_BYTES, local_only=local_only)
    if raw and not raw.endswith(b"\0"):
        raise CandidateVerificationError("invalid_git_inventory")
    entries = []
    try:
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, path = record.split(b"\t", 1)
            mode, kind, blob_sha, size = metadata.decode("ascii").split()
            entries.append({"path": path.decode("utf-8"), "mode": mode, "type": kind, "sha": blob_sha,
                            "size": None if size == "-" else int(size)})
    except (ValueError, UnicodeError):
        raise CandidateVerificationError("invalid_git_inventory") from None
    return entries


def _remote_tree(repository: str, sha: str) -> list[dict]:
    # revision_data._github_tree drops duplicate paths. Inspect the raw bounded
    # list here; its blob reader remains usable after our independent checks.
    from .github.revalidation import GitHubRepository

    token = os.getenv("OMNIFLOW_GITHUB_TOKEN")
    if not token:
        exc = ConfigError(
            "OMNIFLOW_GITHUB_TOKEN is required to read exact pull request data",
            reason_code="github_missing_token",
            diagnostic_context={"service": "github", "api_operation": "github_tree", "retryable": False},
        )
        _retain_candidate_cause(exc, "git_snapshot_unavailable")
        raise exc
    try:
        payload = GitHubRepository(repository, token).request(
            "GET", f"/git/trees/{sha}?recursive=1", max_bytes=MAX_TREE_BYTES,
        )
    except OmniFlowError as exc:
        _retain_candidate_cause(exc, "git_snapshot_unavailable")
        raise
    if not isinstance(payload, dict) or payload.get("truncated") is not False or not isinstance(payload.get("tree"), list):
        raise CandidateVerificationError("incomplete_git_snapshot")
    return payload["tree"]
