from __future__ import annotations

from typing import Any

import yaml
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from yaml.tokens import DirectiveToken

from .exceptions import ConfigError, SecurityPolicyError

MAX_YAML_FILES = 5_000
MAX_YAML_FILE_BYTES = 5 * 1024 * 1024
MAX_YAML_TOTAL_BYTES = 50 * 1024 * 1024
MAX_YAML_DEPTH = 100
MAX_YAML_ALIASES = 50
MAX_YAML_EVENTS = 250_000


class UnsupportedComparisonYaml(ConfigError):
    """The document uses YAML features outside the source comparison contract."""


def parse_secure_yaml(text: str, *, source: str, source_kind: str = "model_yaml", comparison: bool = False) -> Any:
    encoded_size = len(text.encode("utf-8"))
    if encoded_size > MAX_YAML_FILE_BYTES:
        raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' exceeds the 5 MiB safety limit")
    try:
        _validate_event_limits(text, source=source, comparison=comparison)
        # Reserved directives may disappear from the parser's event stream.
        if comparison and "%" in text and any(isinstance(t, DirectiveToken) for t in yaml.scan(text, Loader=yaml.SafeLoader)):
            raise UnsupportedComparisonYaml("YAML directives are unsupported in source comparison")
        value = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise _sanitized_yaml_error(source, exc, source_kind=source_kind) from exc
    except RecursionError as exc:
        raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' exceeds safe nesting limits") from exc
    _validate_constructed_graph(value, source=source)
    return value


def _validate_event_limits(text: str, *, source: str, comparison: bool = False) -> None:
    # Track the cost of each anchor's expanded subtree before safe_load can
    # flatten merge keys. Counting the aliases themselves misses repeated merges.
    collections: list[tuple[str | None, int]] = []
    anchor_sizes: dict[str, int] = {}
    expanded_nodes = 0
    aliases = 0
    events = 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        events += 1
        if events > MAX_YAML_EVENTS:
            raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' contains too many nodes")
        if comparison:
            tag = getattr(event, "tag", None)
            if tag and tag != "!" and tag not in yaml.SafeLoader.yaml_constructors:
                raise ConfigError("Unknown YAML tag in source comparison")
            if (getattr(event, "anchor", None) or tag
                    or isinstance(event, ScalarEvent) and event.style is None and event.value == "<<"):
                raise UnsupportedComparisonYaml("Unsupported YAML construct in source comparison")
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            collections.append((event.anchor, expanded_nodes))
            expanded_nodes += 1
            if len(collections) > MAX_YAML_DEPTH:
                raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' exceeds safe nesting depth")
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            anchor, start = collections.pop()
            if anchor is not None:
                anchor_sizes[anchor] = expanded_nodes - start
        elif isinstance(event, ScalarEvent):
            expanded_nodes += 1
            if event.anchor is not None:
                anchor_sizes[event.anchor] = 1
        elif isinstance(event, AliasEvent):
            aliases += 1
            if aliases > MAX_YAML_ALIASES:
                raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' contains too many aliases")
            if event.anchor not in anchor_sizes:
                raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' contains an undefined or cyclic alias")
            expanded_nodes += anchor_sizes[event.anchor]
        if expanded_nodes > MAX_YAML_EVENTS:
            raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' exceeds the expanded node limit")


def _validate_constructed_graph(value: Any, *, source: str) -> None:
    active: set[int] = set()
    visited = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal visited
        if depth > MAX_YAML_DEPTH:
            raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' exceeds safe nesting depth")
        if not isinstance(item, (dict, list)):
            return
        identity = id(item)
        if identity in active:
            raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' contains a cyclic alias")
        visited += 1
        if visited > MAX_YAML_EVENTS:
            raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' contains too many nodes")
        active.add(identity)
        try:
            children = item.items() if isinstance(item, dict) else enumerate(item)
            for key, child in children:
                if isinstance(key, (dict, list)):
                    walk(key, depth + 1)
                walk(child, depth + 1)
        finally:
            active.remove(identity)

    walk(value, 0)


def _sanitized_yaml_error(source: str, exc: yaml.YAMLError, *, source_kind: str = "model_yaml") -> ConfigError:
    mark = getattr(exc, "problem_mark", None)
    location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
    context = {"source_kind": source_kind if source_kind in {"policy", "model_yaml"} else "model_yaml"}
    if mark is not None:
        context.update(source_line=mark.line + 1, source_column=mark.column + 1)
    return ConfigError(f"Could not parse YAML{location}", reason_code="yaml_parse_error",
                       diagnostic_context=context)


def _source_label(source: str) -> str:
    return str(source).replace("\r", "").replace("\n", "")[:240]
