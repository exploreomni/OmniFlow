from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..exceptions import ConfigError, SecurityPolicyError
from ..yaml_security import MAX_YAML_FILE_BYTES, MAX_YAML_FILES, MAX_YAML_TOTAL_BYTES, parse_secure_yaml


def load_view_names(root: str | Path) -> dict[str, str] | None:
    manifest = load_snapshot_manifest(root)
    if manifest is None:
        return None
    if not isinstance(manifest.get("view_names"), dict):
        raise ConfigError("YAML snapshot is missing canonical viewNames metadata; pull a fresh snapshot")
    return manifest["view_names"]


def load_snapshot_manifest(root: str | Path) -> dict[str, Any] | None:
    path = Path(root) / "manifest.json"
    if path.is_symlink():
        raise SecurityPolicyError("Omni YAML snapshot manifests must not be symbolic links")
    if not path.exists():
        return None
    if path.stat().st_size > MAX_YAML_FILE_BYTES:
        raise SecurityPolicyError("Omni YAML snapshot manifest exceeds the 5 MiB safety limit")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConfigError("Invalid Omni YAML snapshot manifest; refresh the snapshot") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ConfigError("Invalid Omni YAML snapshot file inventory; pull a fresh snapshot")
    if len(manifest["files"]) > MAX_YAML_FILES:
        raise SecurityPolicyError("Omni YAML snapshot contains more than 5,000 files")
    return manifest


def load_yaml_files(root: str | Path) -> dict[str, Any]:
    return load_yaml_snapshot(root)[0]


def load_yaml_snapshot(root: str | Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    base = Path(root)
    manifest = load_snapshot_manifest(base)
    files: dict[str, Any] = {}
    total_bytes = 0
    if manifest is None:
        paths = [path for path in sorted(base.rglob("*")) if path.is_file() and _is_yaml_model_file(path)]
    else:
        # The current manifest is authoritative. Old files from a prior pull are
        # not part of this snapshot, even when the caller reuses an output directory.
        paths = [_snapshot_target(base, name) for name in sorted(manifest["files"])]
    for path in paths:
        if path.is_symlink():
            raise SecurityPolicyError("Omni YAML input files must not be symbolic links")
        if not path.is_file():
            raise ConfigError("Omni YAML snapshot is missing an inventoried file; pull a fresh snapshot")
        if len(files) >= MAX_YAML_FILES:
            raise SecurityPolicyError("Omni YAML input contains more than 5,000 files")
        rel = path.relative_to(base).as_posix()
        size = path.stat().st_size
        if size > MAX_YAML_FILE_BYTES:
            raise SecurityPolicyError(f"Omni YAML file '{rel}' exceeds the 5 MiB safety limit")
        total_bytes += size
        if total_bytes > MAX_YAML_TOTAL_BYTES:
            raise SecurityPolicyError("Omni YAML input exceeds the 50 MiB aggregate safety limit")
        raw = path.read_bytes()
        if manifest is not None:
            entry = manifest["files"][rel]
            expected = entry.get("sha256") if isinstance(entry, dict) else None
            if not isinstance(expected, str) or hashlib.sha256(raw).hexdigest() != expected:
                raise ConfigError("Omni YAML snapshot checksum mismatch; pull a fresh snapshot")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"Omni YAML file '{rel}' is not valid UTF-8") from exc
        if _is_yaml_model_file(path):
            files[rel] = _parse_yaml(text, source=rel)
    return files, manifest


def _snapshot_target(base: Path, name: str) -> Path:
    if (
        not name or len(name) > 1024 or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or Path(name).is_absolute() or ".." in Path(name).parts or Path(name).as_posix() != name
    ):
        raise SecurityPolicyError("Omni YAML snapshot contains an unsafe file path")
    target = base
    for part in Path(name).parts:
        target /= part
        if target.is_symlink():
            raise SecurityPolicyError("Omni YAML snapshot paths must not traverse symbolic links")
    return target


def parse_yaml_file_map(files: dict[str, str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    total_bytes = 0
    for name, text in files.items():
        if isinstance(text, str):
            if len(parsed) >= MAX_YAML_FILES:
                raise SecurityPolicyError("Omni YAML input contains more than 5,000 files")
            size = len(text.encode("utf-8"))
            total_bytes += size
            if total_bytes > MAX_YAML_TOTAL_BYTES:
                raise SecurityPolicyError("Omni YAML input exceeds the 50 MiB aggregate safety limit")
            parsed[name] = _parse_yaml(text, source=name)
    return parsed


def _is_yaml_model_file(path: Path) -> bool:
    return path.name in {"model", "relationships"} or path.suffix in {
        ".yaml",
        ".yml",
        ".view",
        ".topic",
        ".composite_topic",
    }


def _parse_yaml(text: str, *, source: str) -> Any:
    return parse_secure_yaml(text, source=source)
