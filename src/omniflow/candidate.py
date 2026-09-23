"""Read-only evidence that an Omni candidate matches an immutable PR snapshot."""

from __future__ import annotations

import hashlib
import os
import subprocess  # nosec B404
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path, PurePosixPath
from typing import Any

from .exceptions import ConfigError, OmniFlowError
from .git import git_executable
from .revision_data import FULL_SHA, pull_request_revision, read_git_text, read_github_text
from .yaml_security import MAX_YAML_FILE_BYTES, MAX_YAML_FILES, MAX_YAML_TOTAL_BYTES

MAX_TREE_BYTES = 2 * 1024 * 1024
MAX_TREE_ENTRIES = 10_000
SEMANTIC_SUFFIXES = {".view", ".topic", ".composite_topic", ".relationships", ".yaml", ".yml"}


class CandidateVerificationError(ConfigError):
    """Public diagnostics use categories only, never model paths or contents."""

    def __init__(self, reason: str) -> None:
        self.candidate_reason = reason
        super().__init__(
            f"Candidate provenance verification failed ({reason}). "
            "The selected Omni branch must exactly match the immutable pull request's authored YAML. "
            "Check branch synchronization and sanitized snapshot evidence; do not bypass candidate verification."
        )


class _GitUnavailable(Exception):
    pass


def verify_candidate(client: Any, context: Any) -> dict[str, Any]:
    """Compare complete authored semantic snapshots without checking out PR code.

    Call before and after validation and compare the complete returned proofs.
    This is sampled content equality, not an atomic validation/revision lock.
    """
    branch_id = getattr(context, "branch_id", None)
    if not isinstance(branch_id, str) or not branch_id.strip():
        raise CandidateVerificationError("missing_candidate_branch")
    scope = getattr(context, "model_path", None)
    model_path = "." if scope == "." else _safe_path(scope)
    try:
        revision = pull_request_revision("head")
    except (OmniFlowError, AttributeError, TypeError, ValueError):
        raise CandidateVerificationError("invalid_pr_revision") from None
    if revision is None:
        raise CandidateVerificationError("missing_pr_revision")
    sha, repository = revision
    root = Path(".")
    try:
        entries = _local_tree(sha, root=root)
        remote = False
    except _GitUnavailable:
        entries = _remote_tree(repository, sha)
        remote = True
    expected = _semantic_inventory(entries, model_path)
    if not expected:
        raise CandidateVerificationError("empty_git_snapshot")

    try:
        payload = client.get_model_yaml(
            context.model_id, branch_id=branch_id, mode="combined", include_checksums=False,
            fully_resolved=False,
        )
    except OmniFlowError:
        raise CandidateVerificationError("api_snapshot_unavailable") from None
    actual = _api_files(payload)
    if set(actual) != set(expected):
        raise CandidateVerificationError("file_inventory_mismatch")

    digest = hashlib.sha256()
    for path, entry in sorted(expected.items()):
        try:
            if remote:
                text = read_github_text(repository, sha, entry["path"], max_bytes=MAX_YAML_FILE_BYTES)
            else:
                text = read_git_text(sha, entry["path"], root=root, max_bytes=MAX_YAML_FILE_BYTES)
        except OmniFlowError:
            raise CandidateVerificationError("git_snapshot_unavailable") from None
        if not isinstance(text, str):
            raise CandidateVerificationError("incomplete_git_snapshot")
        raw = _utf8(text)
        # Bind the helper's content to the blob in the independently checked
        # inventory, including when local Git replacement refs are configured.
        algorithm = hashlib.sha1 if len(entry["sha"]) == 40 else hashlib.sha256  # nosec B303,B324
        blob = algorithm(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()  # nosec B324
        if len(raw) != entry["size"] or blob != entry["sha"].lower():
            raise CandidateVerificationError("incomplete_git_snapshot")
        if raw != actual[path]:
            raise CandidateVerificationError("content_mismatch")
        name = path.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name + len(raw).to_bytes(8, "big") + raw)
    return {
        "status": "verified", "method": "authored_yaml_snapshot", "head_sha": sha,
        "file_count": len(expected), "snapshot_digest": digest.hexdigest(),
    }


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
    value = PurePosixPath(path)
    return value.name in {"model", "relationships"} or value.suffix in SEMANTIC_SUFFIXES


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


def _semantic_inventory(entries: list[dict], model_path: str) -> dict[str, dict]:
    if len(entries) > MAX_TREE_ENTRIES:
        raise CandidateVerificationError("snapshot_limit_exceeded")
    seen: set[str] = set()
    selected: dict[str, dict] = {}
    total = 0
    prefix = "" if model_path == "." else model_path + "/"
    ancestors = {str(parent) for parent in PurePosixPath(model_path).parents if str(parent) != "."}
    for entry in entries:
        if not isinstance(entry, dict):
            raise CandidateVerificationError("invalid_git_inventory")
        path = _safe_path(entry.get("path"))
        if path in seen:
            raise CandidateVerificationError("duplicate_git_path")
        seen.add(path)
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
        if not _semantic_path(relative):
            continue
        size, blob_sha = entry.get("size"), entry.get("sha")
        if type(size) is not int or size < 0 or not isinstance(blob_sha, str) or not FULL_SHA.fullmatch(blob_sha):
            raise CandidateVerificationError("invalid_git_inventory")
        total += size
        if size > MAX_YAML_FILE_BYTES or total > MAX_YAML_TOTAL_BYTES or len(selected) >= MAX_YAML_FILES:
            raise CandidateVerificationError("snapshot_limit_exceeded")
        selected[relative] = entry
    return selected


def _bounded_git(args: list[str], *, root: Path, limit: int) -> bytes:
    """Bound captured bytes as well as duration; kill before draining overflow."""
    try:
        with subprocess.Popen(  # nosec B603
            [git_executable(), "--no-replace-objects", *args], cwd=root,
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
    except (OSError, subprocess.SubprocessError, TimeoutError):
        raise _GitUnavailable() from None


def _local_tree(sha: str, *, root: Path) -> list[dict]:
    if _bounded_git(["cat-file", "-t", sha], root=root, limit=32).strip() != b"commit":
        raise CandidateVerificationError("invalid_pr_revision")
    raw = _bounded_git(["ls-tree", "--full-tree", "-r", "-l", "-z", sha], root=root, limit=MAX_TREE_BYTES)
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
        raise CandidateVerificationError("git_snapshot_unavailable")
    try:
        payload = GitHubRepository(repository, token).request(
            "GET", f"/git/trees/{sha}?recursive=1", max_bytes=MAX_TREE_BYTES,
        )
    except OmniFlowError:
        raise CandidateVerificationError("git_snapshot_unavailable") from None
    if not isinstance(payload, dict) or payload.get("truncated") is not False or not isinstance(payload.get("tree"), list):
        raise CandidateVerificationError("incomplete_git_snapshot")
    return payload["tree"]
