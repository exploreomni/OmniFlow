"""Bounded, non-executing reads of repository data at exact Git revisions."""

from __future__ import annotations

import base64
import binascii
import os
import re
import subprocess  # nosec B404
from functools import lru_cache
from pathlib import Path

from .exceptions import ConfigError, SecurityPolicyError
from .git import git_executable, github_event_payload, is_pull_request_event
from .trust import SAFE_GIT_REF_RE, _safe_repo_path

FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def pull_request_changed_files(*, root: Path = Path(".")) -> list[str] | None:
    """Use the immutable comparison, not the PR files endpoint's current head."""
    base = pull_request_revision("base")
    head = pull_request_revision("head")
    if base is None or head is None:
        return None
    try:
        result = subprocess.run(  # nosec B603
            [git_executable(), "diff", "--name-only", "--no-renames", "-z", f"{base[0]}...{head[0]}"],
            cwd=root, check=True, capture_output=True, timeout=15,
        )
        if len(result.stdout) > 1024 * 1024:
            raise SecurityPolicyError("Immutable dbt changed-file inventory exceeds 1 MiB")
        files = [item for item in result.stdout.decode("utf-8").split("\x00") if item]
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        # Share the bounded GitHub transport; lazy import avoids a module cycle.
        from .github.revalidation import GitHubRepository

        api = GitHubRepository(base[1], os.getenv("OMNIFLOW_GITHUB_TOKEN", ""))
        result = api.request("GET", f"/compare/{base[0]}...{head[0]}")
        records = result.get("files") if isinstance(result, dict) else None
        # The compare API returns at most 300 files. No remaining-file pagination
        # contract exists, so reaching the limit is conservatively incomplete.
        if not isinstance(records, list) or len(records) >= 300:
            raise ConfigError("Exact dbt comparison is incomplete; make both Git revisions available locally") from None
        files = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("filename"), str):
                raise ConfigError("Exact dbt comparison returned an invalid file record") from None
            files.append(record["filename"])
            if record.get("status") == "renamed":
                if not isinstance(record.get("previous_filename"), str):
                    raise ConfigError("Renamed dbt file is missing its previous path") from None
                files.append(record["previous_filename"])
    return list(dict.fromkeys(_safe_repo_path(Path(path)) for path in files))


def pull_request_revision(side: str) -> tuple[str, str] | None:
    if not is_pull_request_event():
        return None
    pr = github_event_payload().get("pull_request", {})
    value = pr.get(side, {}) if isinstance(pr, dict) else {}
    sha = value.get("sha") if isinstance(value, dict) else None
    repo = value.get("repo", {}).get("full_name") if isinstance(value.get("repo"), dict) else None
    if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha):
        raise ConfigError(f"Pull request {side} must identify an exact commit SHA")
    if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo):
        raise ConfigError(f"Pull request {side} must identify a repository")
    return sha, repo


def read_git_text(ref: str, path: str, *, root: Path, max_bytes: int) -> str | None:
    path = _safe_repo_path(Path(path))
    if not SAFE_GIT_REF_RE.fullmatch(ref) or ".." in ref or "@{" in ref:
        raise SecurityPolicyError("Unsafe data revision")
    try:
        subprocess.run(  # nosec B603
            [git_executable(), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=root, check=True, capture_output=True, timeout=10,
        )
        entry = subprocess.run(  # nosec B603
            [git_executable(), "ls-tree", ref, "--", path],
            cwd=root, check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not entry:
            return None
        if not entry.startswith(("100644 blob ", "100755 blob ")):
            raise SecurityPolicyError("Revision data must be a regular file, not a symlink or tree")
        object_id = entry.split()[2]
        size = subprocess.run(  # nosec B603
            [git_executable(), "cat-file", "-s", object_id],
            cwd=root, check=True, capture_output=True, text=True, timeout=10,
        )
        if int(size.stdout) > max_bytes:
            raise SecurityPolicyError("Revision data exceeds its file size limit")
        value = subprocess.run(  # nosec B603
            [git_executable(), "cat-file", "blob", object_id],
            cwd=root, check=True, capture_output=True, timeout=10,
        ).stdout
        if len(value) > max_bytes:
            raise SecurityPolicyError("Revision data exceeds its file size limit")
        return value.decode("utf-8")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ConfigError("Could not read the required Git data revision") from exc


def read_head_text(path: str, *, root: Path, max_bytes: int) -> str | None:
    revision = pull_request_revision("head")
    if revision is None:
        candidate = root / _safe_repo_path(Path(path))
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
            raise SecurityPolicyError("Revision data must stay inside the checkout without symlinks")
        try:
            with candidate.open("rb") as stream:
                value = stream.read(max_bytes + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError("Could not read local revision data") from exc
        if len(value) > max_bytes:
            raise SecurityPolicyError("Revision data exceeds its file size limit")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError("Revision data must be valid UTF-8") from exc
    sha, repository = revision
    # The PR head is never checked out, imported, compiled, or executed. Missing
    # objects are read through GitHub's bounded Contents API at the immutable SHA.
    try:
        return read_git_text(sha, path, root=root, max_bytes=max_bytes)
    except ConfigError:
        return read_github_text(repository, sha, path, max_bytes=max_bytes)


def read_github_text(repository: str, sha: str, path: str, *, max_bytes: int) -> str | None:
    path = _safe_repo_path(Path(path))
    if not REPOSITORY.fullmatch(repository) or not FULL_SHA.fullmatch(sha):
        raise SecurityPolicyError("GitHub data read requires a repository and exact SHA")
    token = os.getenv("OMNIFLOW_GITHUB_TOKEN")
    if not token:
        raise ConfigError("OMNIFLOW_GITHUB_TOKEN is required to read exact pull request data")
    from .github.revalidation import GitHubRepository

    api = GitHubRepository(repository, token)
    tree = _github_tree(repository, sha, os.getenv("GITHUB_API_URL", "https://api.github.com"))
    for parent in Path(path).parents:
        parent_entry = tree.get(parent.as_posix())
        if parent_entry and parent_entry.get("type") != "tree":
            raise SecurityPolicyError("GitHub revision path traverses a non-directory")
    entry = tree.get(path)
    if entry is None:
        return None
    if entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
        raise SecurityPolicyError("GitHub revision data must be a regular file, not a symlink")
    if not isinstance(entry.get("size"), int) or entry["size"] > max_bytes:
        raise SecurityPolicyError("GitHub revision file exceeds its size limit")
    blob_sha = entry.get("sha")
    if not isinstance(blob_sha, str) or not FULL_SHA.fullmatch(blob_sha):
        raise ConfigError("GitHub revision tree contains an invalid blob SHA")
    payload = api.request("GET", f"/git/blobs/{blob_sha}", max_bytes=max_bytes * 2 + 65536)
    if not isinstance(payload, dict) or payload.get("encoding") != "base64" or payload.get("sha") != blob_sha:
        raise ConfigError("GitHub did not return complete immutable blob data")
    if not isinstance(payload.get("size"), int) or payload["size"] > max_bytes:
        raise SecurityPolicyError("GitHub revision file exceeds its size limit")
    try:
        value = base64.b64decode("".join(payload["content"].split()), validate=True)
        if len(value) != payload["size"] or len(value) > max_bytes:
            raise ConfigError("GitHub revision file has incomplete content")
        return value.decode("utf-8")
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise ConfigError("GitHub revision file has invalid content") from exc


@lru_cache(maxsize=4)
def _github_tree(repository: str, sha: str, api_url: str) -> dict:
    from .github.revalidation import GitHubRepository

    api = GitHubRepository(repository, os.getenv("OMNIFLOW_GITHUB_TOKEN", ""))
    payload = api.request("GET", f"/git/trees/{sha}?recursive=1")
    if not isinstance(payload, dict) or payload.get("truncated") is not False or not isinstance(payload.get("tree"), list):
        raise ConfigError("GitHub revision tree is incomplete")
    entries = payload["tree"]
    if len(entries) > 10000 or any(not isinstance(item, dict) or not isinstance(item.get("path"), str) for item in entries):
        raise ConfigError("GitHub revision tree exceeds the bounded file inventory or is invalid")
    return {item["path"]: item for item in entries}
