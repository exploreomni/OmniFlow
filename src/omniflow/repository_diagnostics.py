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

from .artifact_identity import identity_digest, match_artifacts
from .candidate import (
    MAX_TREE_ENTRIES,
    _api_files,
    _GitUnavailable,
    _local_tree,
    _remote_tree,
    _safe_path,
    _semantic_inventory,
    _source_inventory,
)
from .exceptions import ConfigError, SecurityPolicyError
from .file_scope import excluded_semantic_paths
from .model_paths import identity_index
from .repository_locations import SnapshotIndex
from .repository_locations import _path as diagnostic_path
from .repository_policy import POLICY_REASONS, RepositoryPolicy, resolve_repository_policy
from .revision_data import FULL_SHA, read_git_text, read_github_text
from .security import secure_write_text
from .view_identity import normalize_api_view_names
from .yaml_security import MAX_YAML_FILE_BYTES

REPORT_NAMES = ("report.json", "report.md", "report.sarif", "junit.xml")
_session: ContextVar[Any] = ContextVar("repository_diagnostics", default=None)


@dataclass
class Snapshot:
    sha: str
    files: dict[str, str]
    digest: str
    index: SnapshotIndex
    model_path: str
    identity_digest: str | None = None


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
    unavailable_reasons: dict[str, int] = field(default_factory=dict)
    report_failed: bool = False


def _unavailable(state: RepositorySession, reason: str) -> None:
    state.unavailable_reasons[reason] = state.unavailable_reasons.get(reason, 0) + 1


def record_candidate_diagnostics(context: Any, exc: Exception, issue: dict) -> None:
    """Authorize *observations*, not correspondence, for the private audience.

    A mismatch cannot require a successful match to explain itself. We instead
    require the existing live private-repo policy, immutable registration and
    sampled Git revision. This never creates a candidate proof or source line.
    """
    from .candidate import CandidateVerificationError

    state = _session.get()
    if state is None or not state.policy.enabled or not isinstance(exc, CandidateVerificationError):
        return
    side = "base" if getattr(exc, "validation_scope", None) == "base" else "head"
    sha = state.policy.base_sha if side == "base" else state.policy.head_sha
    try:
        if (getattr(exc, "candidate_git_sha", None) != sha
                or getattr(exc, "candidate_model_id", None) != context.model_id
                or getattr(exc, "candidate_branch_id", None) != (None if side == "base" else context.branch_id)
                or not _registered_context(state.policy, context)):
            _unavailable(state, "candidate_sample_context_unverified")
            return
        entries = _semantic_inventory(_tree(state.policy, sha), context.model_path, include_unrecognized=True)
        samples = []
        for sample in getattr(exc, "candidate_samples", [])[:5]:
            if not isinstance(sample, dict):
                continue
            git_path, omni_path = sample.get("git_path"), sample.get("omni_path")
            if git_path is not None and (not diagnostic_path(git_path) or git_path not in entries):
                continue
            if omni_path is not None and not diagnostic_path(omni_path):
                continue
            if git_path is None and omni_path is None:
                continue
            safe = {}
            if git_path is not None:
                full_path = git_path if context.model_path == "." else f"{context.model_path}/{git_path}"
                if not diagnostic_path(full_path):
                    continue
                safe["git_path"] = full_path
            if omni_path is not None:
                safe["omni_path"] = omni_path
            safe["comparison"] = "different" if sample.get("comparison") == "different" else "not_compared"
            safe["pairing"] = sample.get("pairing") if sample.get("pairing") in {
                "basename_hint", "case_hint", "suffix_hint", "unpaired", "authored_identity", "ambiguous_identity"
            } else "unpaired"
            samples.append(safe)
        if not samples:
            _unavailable(state, "candidate_samples_unavailable")
            return
        state.details[id(issue)] = (issue, {
            "candidate_samples": samples, "source_revision": side, "git_revision": sha,
            "candidate_evidence_kind": "observations_not_correspondence",
        })
        state.contexts_verified += 1  # Disclosure eligibility, NOT candidate verification.
    except Exception:
        _unavailable(state, "candidate_samples_unavailable")


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


def _load_snapshot(
    policy: RepositoryPolicy, model_path: str, side: str, *, non_omni_rules=(), api_files: dict | None = None,
    allow_empty: bool = False,
) -> Snapshot:
    sha = policy.head_sha if side == "head" else policy.base_sha
    model_path = "." if model_path == "." else _safe_path(model_path)
    entries = _tree(policy, sha)
    inventory = _semantic_inventory(entries, model_path, include_unrecognized=True)
    identity_index(inventory)
    if non_omni_rules:
        excluded = excluded_semantic_paths(inventory, api_files or {}, model_path=model_path, rules=non_omni_rules)
        inventory = {path: entry for path, entry in inventory.items() if path not in excluded}
    inventory = _source_inventory(inventory, api_files or {})
    if not inventory and not allow_empty:
        raise ConfigError("Repository diagnostic snapshot is empty")
    # Snapshot keys and digests remain exact Git paths. Independently reject
    # ambiguous serialization aliases even when a candidate proof is supplied.
    # Duplicate basenames in different schemas remain valid exact sources.
    identity_index(inventory)
    files = {}
    digest = hashlib.sha256()
    for path, entry in sorted(inventory.items()):
        text = _verified_text(policy, sha, entry, max_bytes=MAX_YAML_FILE_BYTES)
        raw = text.encode("utf-8")
        name = path.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name + len(raw).to_bytes(8, "big") + raw)
        files[path] = text
    return Snapshot(sha, files, digest.hexdigest(), SnapshotIndex(model_path, files), model_path)


def _api_evidence(client: Any, context: Any, *, side: str, branch_id: str | None) -> tuple[dict, dict | None]:
    payload = client.get_model_yaml(
        context.model_id, branch_id=branch_id if side == "head" else None,
        mode="combined", include_checksums=False, fully_resolved=False,
    )
    actual = _api_files(payload)
    names = normalize_api_view_names(
        payload["viewNames"], {path: raw.decode("utf-8") for path, raw in actual.items()},
        parse_contents=False,
    ) if "viewNames" in payload else None
    return actual, names


def _matches(snapshot: Snapshot, *, proof: dict | None, side: str, client: Any,
             context: Any, branch_id: str | None, api_files: dict | None = None,
             omni_view_names: dict[str, str] | None = None) -> bool:
    if proof:
        proof_matches = (proof.get("status") in {"verified", "pending_recheck"}
                and proof.get("method") == "authored_yaml_snapshot"
                and proof.get(f"{side}_sha") == snapshot.sha
                and proof.get("snapshot_digest") == snapshot.digest
                and proof.get("file_count") == len(snapshot.files))
        if not proof_matches:
            return False
    if api_files is None:
        actual, omni_view_names = _api_evidence(client, context, side=side, branch_id=branch_id)
    else:
        actual = api_files
    matched = match_artifacts(
        snapshot.files, {path: raw.decode("utf-8") for path, raw in actual.items()},
        omni_view_names=omni_view_names,
    )
    if matched.unmatched_git or matched.unmatched_omni or any(
        snapshot.files[git_path].encode("utf-8") != actual[api_path]
        for git_path, api_path in matched.pairs.items()
    ):
        return False
    observed_identity = identity_digest(matched.pairs, omni_view_names=omni_view_names)
    if ((proof and "identity_digest" in proof and proof["identity_digest"] != observed_identity)
            or (snapshot.identity_digest is not None and snapshot.identity_digest != observed_identity)):
        return False
    # A proof binds Git bytes, not arbitrary API names. Independently verified
    # pairings supply aliases and canonical view names for this exact snapshot.
    aliases = {api_path: git_path for git_path, api_path in matched.pairs.items()}
    canonical = {name: aliases[api_path] for name, api_path in (omni_view_names or {}).items()
                 if api_path in aliases}
    snapshot.index = SnapshotIndex(snapshot.model_path, snapshot.files,
                                   api_aliases=aliases, canonical_view_names=canonical)
    snapshot.identity_digest = observed_identity
    return True


def start_repository_context(context: Any, client: Any, *, branch_id: str | None,
                             head_proof: dict | None = None, base_proof: dict | None = None,
                             non_omni_rules=()) -> ContextEvidence | None:
    state = _session.get()
    if state is None or not state.policy.enabled:
        return None
    evidence = ContextEvidence()
    try:
        if not _registered_context(state.policy, context):
            raise ConfigError("Repository diagnostic context is unregistered")
        for side, proof in (("head", head_proof), ("base", base_proof)):
            # Exclusion-aware repository detail is enabled only with a complete
            # candidate proof; an unverified rule cannot authorize disclosure.
            if non_omni_rules and not proof:
                continue
            # The candidate proof alone cannot authorize API path/canonical-name
            # aliases. Bind a fresh authored inventory and metadata to Git bytes.
            actual, names = _api_evidence(client, context, side=side, branch_id=branch_id)
            snapshot = _load_snapshot(
                state.policy, context.model_path, side, non_omni_rules=non_omni_rules, api_files=actual,
            )
            if _matches(snapshot, proof=proof, side=side, client=client, context=context,
                        branch_id=branch_id, api_files=actual, omni_view_names=names):
                evidence.snapshots[side] = snapshot
    except Exception:
        # Detail unavailable is never a new validation error or an excuse to
        # publish arbitrary exception text. The main checks proceed normally.
        state.contexts_unavailable += 1
        _unavailable(state, "snapshot_evidence_unavailable")
        return None
    if not evidence.snapshots:
        state.contexts_unavailable += 1
        _unavailable(state, "snapshot_evidence_unavailable")
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
            else:
                reason = snapshot.index.unavailable_reason
                pending[id(issue)] = (issue, {"location_unavailable_reason": reason})
                _unavailable(state, reason)
        state.details.update(pending)
        state.contexts_verified += 1
    except Exception:
        state.contexts_unavailable += 1
        _unavailable(state, "snapshot_evidence_unavailable")


def _enrich(original: Any, safe: Any, state: RepositorySession) -> Any:
    if isinstance(original, dict) and isinstance(safe, dict):
        result = {key: _enrich(original.get(key), child, state) for key, child in safe.items()}
        record = state.details.get(id(original))
        if record is not None and record[0] is original:
            details = dict(record[1])
            # Locations may add a more precise fixed explanation, but the same
            # finding keeps its catalog code across public/private formats.
            if "diagnostic_code" in details and "diagnostic_code" in result:
                details["location_diagnostic_code"] = details.pop("diagnostic_code")
            result.update(details)
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
                **status, "status": "generated", "diagnostics_reason": "repository_verified",
                "publication_status": "unconfirmed",
                "unmapped_findings": len(projected.get("issues", [])) - mapped,
                "contexts_unavailable": state.contexts_unavailable,
                "unavailable_reasons": dict(state.unavailable_reasons),
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
        if state is not None:
            state.report_failed = True
        secure_write_text(directory / "status.json", json.dumps({
            "mode": "minimal", "reason": "repository_report_unavailable", "mapped_findings": 0,
        }, indent=2) + "\n")


def repository_diagnostic_availability() -> dict:
    """Runner-local generation only. The caller workflow owns upload receipts."""
    state = _session.get()
    if state is None:
        return {"status": "unavailable", "diagnostics_reason": "no_trusted_run_context",
                "publication_status": "unconfirmed"}
    if state.output_ready:
        return {"status": "generated", "diagnostics_reason": "repository_verified",
                "publication_status": "unconfirmed"}
    reason = state.policy.reason
    if state.policy.enabled:
        reason = "repository_report_unavailable" if state.report_failed else "snapshot_evidence_unavailable"
    if reason not in POLICY_REASONS | {"repository_report_unavailable", "snapshot_evidence_unavailable"}:
        reason = "no_trusted_run_context"
    return {"status": "unavailable", "diagnostics_reason": reason, "publication_status": "unconfirmed"}


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
            print("Repository diagnostic readiness could not be handed to the workflow; "
                  "do not upload repository-detail artifacts. Check runner output permissions. "
                  "This is a reporting handoff failure, not a model defect.")
            return
    reason = repository_diagnostic_availability()["diagnostics_reason"]
    if ready:
        print("Repository diagnostics were generated on the runner; upload is not yet confirmed. "
              "Check the workflow's Diagnostic publication receipt for download status. "
              "Older workflows need the guarded repository-diagnostics upload step from the current example. "
              "PR comments and logs remain minimal.")
    else:
        print(f"Repository diagnostics unavailable ({reason}). Public report contains safe codes and recovery steps; "
              "source paths require a verified private-repository context. Validation decisions are unchanged.")
