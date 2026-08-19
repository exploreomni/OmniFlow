"""dbt manifest parsing for schema-impact analysis.

A committed dbt ``manifest.json`` is the precise source for which warehouse
relation a model produces and which columns it declares. This module extracts
only the minimum needed to compare two manifests: relation identity and column
names. Everything else in the manifest, including compiled SQL, is ignored so
warehouse content and credentials never enter OmniFlow's memory or reports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .exceptions import ConfigError, SecurityPolicyError

MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_NODES = 100_000
MAX_COLUMNS_PER_NODE = 10_000
RELATION_NODE_TYPES = {"model", "seed", "snapshot"}


@dataclass
class DbtModel:
    """A dbt node that materializes into a queryable warehouse relation."""

    unique_id: str
    name: str
    relation_name: str | None = None
    database: str | None = None
    schema_name: str | None = None
    alias: str | None = None
    materialized: str | None = None
    columns: set[str] = field(default_factory=set)

    def relation_candidates(self) -> set[str]:
        """Return every normalized name an Omni view might use for this model.

        Omni's ``sql_table_name`` can be fully qualified, partially qualified, or
        bare, so a match is attempted against each plausible form.
        """
        candidates: set[str] = set()
        table = (self.alias or self.name or "").strip()
        if table:
            candidates.add(table.lower())
        if self.schema_name and table:
            candidates.add(f"{self.schema_name}.{table}".lower())
        if self.database and self.schema_name and table:
            candidates.add(f"{self.database}.{self.schema_name}.{table}".lower())
        if self.relation_name:
            cleaned = self.relation_name.replace('"', "").replace("`", "").strip()
            if cleaned:
                candidates.add(cleaned.lower())
                parts = [part for part in cleaned.split(".") if part]
                if parts:
                    candidates.add(parts[-1].lower())
                if len(parts) >= 2:
                    candidates.add(".".join(parts[-2:]).lower())
        return candidates


def parse_manifest(text: str, *, source: str = "manifest.json") -> dict[str, DbtModel]:
    """Parse a dbt manifest into models keyed by unique_id.

    Only ``nodes`` of a materializing type are retained. Ephemeral models are
    excluded because they never produce a relation an Omni view can reference.
    """
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise SecurityPolicyError(f"dbt manifest '{source}' exceeds the 256 MiB safety limit")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Could not parse dbt manifest '{source}': {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"dbt manifest '{source}' must contain a JSON object")
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        raise ConfigError(f"dbt manifest '{source}' must include a nodes object")
    if len(nodes) > MAX_MANIFEST_NODES:
        raise SecurityPolicyError(f"dbt manifest '{source}' contains more than {MAX_MANIFEST_NODES} nodes")

    models: dict[str, DbtModel] = {}
    for unique_id, node in nodes.items():
        if not isinstance(node, dict) or not isinstance(unique_id, str):
            continue
        resource_type = node.get("resource_type")
        if resource_type not in RELATION_NODE_TYPES:
            continue
        materialized = _config_materialization(node)
        if materialized == "ephemeral":
            continue
        name = node.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        models[unique_id] = DbtModel(
            unique_id=unique_id,
            name=name.strip(),
            relation_name=_optional_string(node.get("relation_name")),
            database=_optional_string(node.get("database")),
            schema_name=_optional_string(node.get("schema")),
            alias=_optional_string(node.get("alias")),
            materialized=materialized,
            columns=_node_columns(node, source=source),
        )
    return models


def _node_columns(node: dict[str, Any], *, source: str) -> set[str]:
    columns = node.get("columns")
    if not isinstance(columns, dict):
        return set()
    if len(columns) > MAX_COLUMNS_PER_NODE:
        raise SecurityPolicyError(
            f"dbt manifest '{source}' declares more than {MAX_COLUMNS_PER_NODE} columns for one node"
        )
    names: set[str] = set()
    for key, value in columns.items():
        candidate = key
        if isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"].strip():
            candidate = value["name"]
        if isinstance(candidate, str) and candidate.strip():
            names.add(candidate.strip().lower())
    return names


def _config_materialization(node: dict[str, Any]) -> str | None:
    config = node.get("config")
    if isinstance(config, dict):
        value = config.get("materialized")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def diff_manifests(
    base: dict[str, DbtModel], head: dict[str, DbtModel]
) -> tuple[dict[str, DbtModel], dict[str, set[str]]]:
    """Compare two manifests.

    Returns removed models keyed by unique_id, and per-surviving-model removed
    column names. A column is 'removed' only when the base manifest actually
    declared columns for that node, so a repository that does not document
    columns never produces phantom findings.
    """
    removed_models = {
        unique_id: model for unique_id, model in base.items() if unique_id not in head
    }
    removed_columns: dict[str, set[str]] = {}
    for unique_id, base_model in base.items():
        head_model = head.get(unique_id)
        if head_model is None or not base_model.columns:
            continue
        missing = base_model.columns - head_model.columns
        if missing:
            removed_columns[unique_id] = missing
    return removed_models, removed_columns
