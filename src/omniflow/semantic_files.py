from __future__ import annotations

from typing import Any


def infer_file_kind(file_path: str, payload: Any) -> str:
    """Use the same file identity rules at API ingestion and graph construction."""
    lower = file_path.lower()
    basename = lower.rsplit("/", 1)[-1]
    typed_name = basename
    for suffix in (".yaml", ".yml"):
        if typed_name.endswith(suffix):
            typed_name = typed_name[:-len(suffix)]
            break
    # Explicit Omni file types outrank reserved basenames and payload hints.
    if typed_name.endswith(".view"):
        return "view"
    if typed_name.endswith((".topic", ".composite_topic")):
        return "topic"
    if typed_name.endswith(".relationships"):
        return "relationship"
    stem = basename.rsplit(".", 1)[0]
    if stem == "model" or (isinstance(payload, dict) and payload.get("type") == "model"):
        return "model"
    if lower.endswith(".topic") or ".topic." in lower:
        return "topic"
    if lower.endswith(".composite_topic") or ".composite_topic." in lower:
        return "topic"
    if (
        stem == "relationships"
        or stem.endswith(".relationships")
        or lower.endswith(".relationships")
        or isinstance(payload, list)
    ):
        return "relationship"
    if isinstance(payload, dict) and (payload.get("type") == "topic" or "base_view" in payload):
        return "topic"
    return "view"
