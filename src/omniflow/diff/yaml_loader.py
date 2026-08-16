from __future__ import annotations

from pathlib import Path
from typing import Any

from ..exceptions import ConfigError, SecurityPolicyError
from ..yaml_security import MAX_YAML_FILE_BYTES, MAX_YAML_FILES, MAX_YAML_TOTAL_BYTES, parse_secure_yaml


def load_yaml_files(root: str | Path) -> dict[str, Any]:
    base = Path(root)
    files: dict[str, Any] = {}
    total_bytes = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file() or not _is_yaml_model_file(path):
            continue
        if path.is_symlink():
            raise SecurityPolicyError("Omni YAML input files must not be symbolic links")
        if len(files) >= MAX_YAML_FILES:
            raise SecurityPolicyError("Omni YAML input contains more than 5,000 files")
        rel = path.relative_to(base).as_posix()
        size = path.stat().st_size
        if size > MAX_YAML_FILE_BYTES:
            raise SecurityPolicyError(f"Omni YAML file '{rel}' exceeds the 5 MiB safety limit")
        total_bytes += size
        if total_bytes > MAX_YAML_TOTAL_BYTES:
            raise SecurityPolicyError("Omni YAML input exceeds the 50 MiB aggregate safety limit")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"Omni YAML file '{rel}' is not valid UTF-8") from exc
        files[rel] = _parse_yaml(text, source=rel)
    return files


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
