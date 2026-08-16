from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..exceptions import OmniAPIError, SecurityPolicyError
from ..omni_client import OmniClient
from ..yaml_pull import _extract_checksums, _extract_file_map
from ..yaml_security import MAX_YAML_FILE_BYTES, MAX_YAML_FILES, MAX_YAML_TOTAL_BYTES


@dataclass(frozen=True)
class SnapshotFile:
    name: str
    text: str
    checksum: str
    sha256: str


@dataclass(frozen=True)
class ModelSnapshot:
    files: dict[str, SnapshotFile]

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for name, record in sorted(self.files.items()):
            digest.update(name.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(record.sha256.encode("ascii"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def content_matches(self, other: ModelSnapshot) -> bool:
        return self.fingerprint == other.fingerprint and set(self.files) == set(other.files)


def fetch_authored_snapshot(*, client: OmniClient, model_id: str, branch_id: str) -> ModelSnapshot:
    if not branch_id:
        raise SecurityPolicyError("AI repair snapshots require an existing Omni branch ID")
    payload = client.get_model_yaml(
        model_id,
        branch_id=branch_id,
        mode="combined",
        include_checksums=True,
        fully_resolved=False,
    )
    files = _extract_file_map(payload)
    checksums = _extract_checksums(payload)
    if not files:
        raise OmniAPIError("Omni model YAML response did not contain any authored files")
    if len(files) > MAX_YAML_FILES:
        raise SecurityPolicyError("Omni YAML response contains more than 5,000 authored files")

    snapshot_files: dict[str, SnapshotFile] = {}
    total_bytes = 0
    for file_name, text in files.items():
        normalized_name = _safe_snapshot_file_name(file_name)
        size = len(text.encode("utf-8"))
        if size > MAX_YAML_FILE_BYTES:
            raise SecurityPolicyError(f"Omni YAML file '{normalized_name[:240]}' exceeds the 5 MiB safety limit")
        total_bytes += size
        if total_bytes > MAX_YAML_TOTAL_BYTES:
            raise SecurityPolicyError("Omni YAML response exceeds the 50 MiB aggregate safety limit")
        checksum = checksums.get(normalized_name)
        if not isinstance(checksum, str) or not checksum.strip():
            raise SecurityPolicyError(
                f"Omni YAML file '{normalized_name[:240]}' is missing the checksum required for safe repair"
            )
        snapshot_files[normalized_name] = SnapshotFile(
            name=normalized_name,
            text=text,
            checksum=checksum.strip(),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    return ModelSnapshot(files=snapshot_files)


def restore_snapshot(
    *,
    client: OmniClient,
    model_id: str,
    branch_id: str,
    desired: ModelSnapshot,
    expected_current: ModelSnapshot,
) -> dict[str, Any]:
    current = fetch_authored_snapshot(client=client, model_id=model_id, branch_id=branch_id)
    if not current.content_matches(expected_current):
        raise SecurityPolicyError("Omni branch changed after repair inspection; rollback stopped to avoid overwriting it")

    restored = 0
    recreated = 0
    deleted = 0
    for file_name, desired_file in sorted(desired.files.items()):
        current_file = current.files.get(file_name)
        if current_file is not None and current_file.sha256 == desired_file.sha256:
            continue
        client.update_model_yaml(
            model_id,
            branch_id=branch_id,
            file_name=file_name,
            yaml_text=desired_file.text,
            previous_checksum=current_file.checksum if current_file is not None else None,
            commit_message="OmniFlow AI repair rollback",
        )
        if current_file is None:
            recreated += 1
        else:
            restored += 1

    for file_name in sorted(set(current.files) - set(desired.files)):
        # Omni's delete endpoint has no checksum precondition. Re-read immediately before deletion.
        latest = fetch_authored_snapshot(client=client, model_id=model_id, branch_id=branch_id)
        latest_file = latest.files.get(file_name)
        expected_file = current.files[file_name]
        if latest_file is None or latest_file.sha256 != expected_file.sha256:
            raise SecurityPolicyError(
                "An AI-created YAML file changed before rollback; deletion stopped to avoid overwriting it"
            )
        client.delete_model_yaml(
            model_id,
            branch_id=branch_id,
            file_name=file_name,
            commit_message="OmniFlow AI repair rollback",
        )
        deleted += 1

    verified = fetch_authored_snapshot(client=client, model_id=model_id, branch_id=branch_id)
    if not verified.content_matches(desired):
        raise SecurityPolicyError("OmniFlow could not verify an exact AI repair rollback")
    return {"restored_files": restored, "recreated_files": recreated, "deleted_files": deleted, "verified": True}


def _safe_snapshot_file_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1_024:
        raise SecurityPolicyError("Omni YAML response contained an unsafe file path")
    if any(character in value for character in ("\x00", "\r", "\n", "\\")):
        raise SecurityPolicyError("Omni YAML response contained an unsafe file path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise SecurityPolicyError("Omni YAML response contained an unsafe file path")
    return value.strip()
