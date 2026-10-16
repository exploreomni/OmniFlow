"""Opt-in-by-trusted-default repository detail, separate from shareable output.

Only a live run can authorize this output. Saved reports never carry authority.
Findings keep their original identities and policy; this is presentation only.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .candidate import (
    MAX_TREE_ENTRIES,
    _api_files,
    _GitUnavailable,
    _local_tree,
    _remote_tree,
    _safe_path,
    _semantic_inventory,
)
from .exceptions import ConfigError, SecurityPolicyError
from .repository_locations import SnapshotIndex
from .repository_policy import RepositoryPolicy, resolve_repository_policy
from .revision_data import FULL_SHA, read_git_text, read_github_text
from .security import secure_write_text
from .yaml_security import MAX_YAML_FILE_BYTES

REPORT_NAMES = ("report.json", "report.md", "report.sarif", "junit.xml")
_session: ContextVar[Any] = ContextVar("repository_diagnostics", default=None)


@dataclass
class Snapshot:
    sha: str
    files: dict[str, str]
    digest: str
    index: SnapshotIndex


@dataclass
class ContextEvidence:
    snapshots: dict[str, Snapshot] = field(default_factory=dict)


@dataclass
class RepositorySession:
    policy: RepositoryPolicy
    # Keep the originating object alive; a reused id or saved report cannot
    # inherit disclosure authority from another finding.
    details: dict[int, tuple[dict, dict]] = field(default_factory=dict)
    contexts_verified: int = 0
    contexts_unavailable: int = 0
    output_ready: bool = False


@contextmanager
def repository_reporting(config, *, output_dir: Path):
    clear_repository_reports(output_dir)
    state = RepositorySession(resolve_repository_policy(config))
    token = _session.set(state)
    try:
        yield
    finally:
        _session.reset(token)


def clear_repository_reports(output_dir: str | Path) -> None:
    """Remove only known generated files, never recursively delete user paths."""
    # The low-level public/report rerender API supports explicitly selected
    # absolute destinations. CLI policy applies its stricter checkout boundary
    # before reaching here; do not change that existing library contract.
    root = Path(output_dir)
    if root.is_symlink():
        raise SecurityPolicyError("Repository diagnostic output must not traverse symbolic links")
    # Canonicalize the caller-selected root (e.g. macOS /var -> /private/var).
    # Every generated descendant is then checked against that fixed root.
    directory = root.resolve() / "repository"
    state = _session.get()
    if state is not None:
        state.output_ready = False
    if directory.resolve() != directory:
        raise SecurityPolicyError("Repository diagnostic output must not traverse symbolic links")
    paths = []
    for name in (*REPORT_NAMES, "status.json"):
        path = directory / name
        if path.resolve() != path:
            raise SecurityPolicyError("Repository diagnostic output must not traverse symbolic links")
        if path.exists():
            if not path.is_file():
                raise SecurityPolicyError("Repository diagnostic output must be a regular file")
            paths.append(path)
    # Preflight every file before deleting any known output.
    for path in paths:
        path.unlink()


def _read_text(repository: str, sha: str, path: str, *, max_bytes: int) -> str | None:
    try:
        return read_git_text(sha, path, root=Path("."), max_bytes=max_bytes)
    except ConfigError:
        return read_github_text(repository, sha, path, max_bytes=max_bytes)


def _registered_context(policy: RepositoryPolicy, context: Any) -> bool:
    entries = _tree(policy, policy.base_sha)
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") == ".omni/flow.json"]
    if len(matches) != 1:
        return False
    text = _verified_text(policy, policy.base_sha, matches[0], max_bytes=1024 * 1024)
    payload = json.loads(text)
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or len(models) > 500:
        return False
    matches = [item for item in models if isinstance(item, dict)
               and item.get("model_id") == context.model_id
               and item.get("model_path") == context.model_path
               and item.get("base_url") == context.base_url
               and item.get("environment") == context.environment]
    return len(matches) == 1


def _tree(policy: RepositoryPolicy, sha: str) -> list[dict]:
    try:
        entries = _local_tree(sha, root=Path("."))
    except _GitUnavailable:
        entries = _remote_tree(policy.repository, sha)
    if len(entries) > MAX_TREE_ENTRIES:
        raise ConfigError("Repository diagnostic snapshot exceeds its inventory limit")
    return entries


def _verified_text(policy: RepositoryPolicy, sha: str, entry: dict, *, max_bytes: int) -> str:
    blob_sha, size = entry.get("sha"), entry.get("size")
    if (entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}
            or type(size) is not int or not 0 <= size <= max_bytes
            or not isinstance(blob_sha, str) or not FULL_SHA.fullmatch(blob_sha)):
        raise ConfigError("Repository diagnostic snapshot contains an unsafe entry")
    text = _read_text(policy.repository, sha, _safe_path(entry.get("path")), max_bytes=max_bytes)
    if not isinstance(text, str):
        raise ConfigError("Repository diagnostic snapshot is incomplete")
    raw = text.encode("utf-8")
    algorithm = hashlib.sha1 if len(blob_sha) == 40 else hashlib.sha256  # nosec B303,B324
    blob = algorithm(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()  # nosec B324
    if len(raw) != size or blob != blob_sha.lower():
        raise ConfigError("Repository diagnostic snapshot is inconsistent")
    return text


def _load_snapshot(policy: RepositoryPolicy, model_path: str, side: str) -> Snapshot:
    sha = policy.head_sha if side == "head" else policy.base_sha
    model_path = "." if model_path == "." else _safe_path(model_path)
    entries = _tree(policy, sha)
    inventory = _semantic_inventory(entries, model_path)
    if not inventory:
        raise ConfigError("Repository diagnostic snapshot is empty")
    files = {}
    digest = hashlib.sha256()
    for path, entry in sorted(inventory.items()):
        text = _verified_text(policy, sha, entry, max_bytes=MAX_YAML_FILE_BYTES)
        raw = text.encode("utf-8")
        name = path.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name + len(raw).to_bytes(8, "big") + raw)
        files[path] = text
    return Snapshot(sha, files, digest.hexdigest(), SnapshotIndex(model_path, files))


def _matches(snapshot: Snapshot, *, proof: dict | None, side: str, client: Any,
             context: Any, branch_id: str | None) -> bool:
    if proof:
        return (proof.get("status") in {"verified", "pending_recheck"}
                and proof.get("method") == "authored_yaml_snapshot"
                and proof.get(f"{side}_sha") == snapshot.sha
                and proof.get("snapshot_digest") == snapshot.digest
                and proof.get("file_count") == len(snapshot.files))
    actual = _api_files(client.get_model_yaml(
        context.model_id, branch_id=branch_id if side == "head" else None,
        mode="combined", include_checksums=False, fully_resolved=False,
    ))
    return actual == {path: text.encode("utf-8") for path, text in snapshot.files.items()}


def start_repository_context(context: Any, client: Any, *, branch_id: str | None,
                             head_proof: dict | None = None, base_proof: dict | None = None) -> ContextEvidence | None:
    state = _session.get()
    if state is None or not state.policy.enabled:
        return None
    evidence = ContextEvidence()
    try:
        if not _registered_context(state.policy, context):
            raise ConfigError("Repository diagnostic context is unregistered")
        for side, proof in (("head", head_proof), ("base", base_proof)):
            snapshot = _load_snapshot(state.policy, context.model_path, side)
            if _matches(snapshot, proof=proof, side=side, client=client, context=context, branch_id=branch_id):
                evidence.snapshots[side] = snapshot
    except Exception:
        # Detail unavailable is never a new validation error or an excuse to
        # publish arbitrary exception text. The main checks proceed normally.
        state.contexts_unavailable += 1
        return None
    if not evidence.snapshots:
        state.contexts_unavailable += 1
        return None
    return evidence


def finish_repository_context(evidence: ContextEvidence | None, context: Any, client: Any, issues: list[dict],
                              *, branch_id: str | None, head_proof: dict | None = None,
                              base_proof: dict | None = None) -> None:
    state = _session.get()
    if evidence is None or state is None or not state.policy.enabled:
        return
    try:
        verified = {}
        for side, snapshot in evidence.snapshots.items():
            proof = head_proof if side == "head" else base_proof
            if _matches(snapshot, proof=proof, side=side, client=client, context=context, branch_id=branch_id):
                verified[side] = snapshot
        if not verified:
            state.contexts_unavailable += 1
            return
        pending = {}
        for issue in issues:
            deleted = issue.get("type", "").endswith("_deleted") or issue.get("rule_id") == "block_deleted_fields"
            side = "base" if deleted or issue.get("state") == "resolved" or issue.get("validation_scope") == "base" else "head"
            snapshot = verified.get(side)
            if snapshot is None:
                continue
            detail = snapshot.index.detail(issue)
            if detail:
                pending[id(issue)] = (issue, {**detail, "source_revision": side})
        state.details.update(pending)
        state.contexts_verified += 1
    except Exception:
        state.contexts_unavailable += 1


def _enrich(original: Any, safe: Any, state: RepositorySession) -> Any:
    if isinstance(original, dict) and isinstance(safe, dict):
        result = {key: _enrich(original.get(key), child, state) for key, child in safe.items()}
        record = state.details.get(id(original))
        if record is not None and record[0] is original:
            result.update(record[1])
        return result
    if isinstance(original, list) and isinstance(safe, list):
        records = [item for item in original if isinstance(item, dict)]
        if len(records) == len(safe):
            return [_enrich(item, child, state) for item, child in zip(records, safe, strict=True)]
    return deepcopy(safe)


def write_repository_reports(report: dict, safe_report: dict, *, output_dir: str | Path) -> None:
    """Build a separate projection. Never enrich the public projection in place."""
    from .reporting.writer import write_reports

    clear_repository_reports(output_dir)
    state = _session.get()
    reason = state.policy.reason if state else "no_trusted_run_context"
    enabled = state is not None and state.policy.enabled
    status = {"mode": "minimal", "reason": reason, "mapped_findings": 0}
    directory = Path(output_dir) / "repository"
    try:
        if enabled and state.contexts_verified:
            projected = _enrich(report, safe_report, state)
            projected["repository_diagnostic_schema_version"] = 1
            projected["diagnostic_audience"] = "repository"
            mapped = sum("file" in item for item in projected.get("issues", []))
            status.update(mode="repository", reason="repository_verified", mapped_findings=mapped)
            projected["repository_diagnostics"] = {
                **status, "unmapped_findings": len(projected.get("issues", [])) - mapped,
                "contexts_unavailable": state.contexts_unavailable,
            }
            write_reports(projected, output_dir=directory, formats=["json", "markdown", "sarif", "junit"])
        elif enabled:
            status["reason"] = "snapshot_evidence_unavailable"
        secure_write_text(directory / "status.json", json.dumps(status, indent=2) + "\n")
        if enabled:
            state.output_ready = status["mode"] == "repository"
    except SecurityPolicyError:
        clear_repository_reports(output_dir)
        raise
    except Exception:
        clear_repository_reports(output_dir)
        secure_write_text(directory / "status.json", json.dumps({
            "mode": "minimal", "reason": "repository_report_unavailable", "mapped_findings": 0,
        }, indent=2) + "\n")


def publish_repository_output() -> None:
    """A fresh-run receipt for the workflow, never inferred from existing files."""
    state = _session.get()
    ready = state is not None and state.output_ready
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(f"repository_diagnostics_ready={'true' if ready else 'false'}\n")
        except OSError:
            print("Could not mark repository diagnostics ready; do not upload repository-detail artifacts.")
    reason = state.policy.reason if state else "no_trusted_run_context"
    if ready:
        print("Repository diagnostics: detailed source locations are available in the separate repository artifact. "
              "PR comments and logs remain minimal.")
    else:
        if state is not None and state.policy.enabled:
            reason = "snapshot_or_report_unavailable"
        print(f"Repository diagnostics: minimal ({reason}). Validation decisions are unchanged.")
