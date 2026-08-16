from __future__ import annotations

from typing import Any

import yaml
from yaml.events import AliasEvent, MappingEndEvent, MappingStartEvent, SequenceEndEvent, SequenceStartEvent

from .exceptions import ConfigError, SecurityPolicyError

MAX_YAML_FILES = 5_000
MAX_YAML_FILE_BYTES = 5 * 1024 * 1024
MAX_YAML_TOTAL_BYTES = 50 * 1024 * 1024
MAX_YAML_DEPTH = 100
MAX_YAML_ALIASES = 50
MAX_YAML_EVENTS = 250_000


def parse_secure_yaml(text: str, *, source: str) -> Any:
    encoded_size = len(text.encode("utf-8"))
    if encoded_size > MAX_YAML_FILE_BYTES:
        raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' exceeds the 5 MiB safety limit")
    try:
        _validate_event_limits(text, source=source)
        value = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise _sanitized_yaml_error(source, exc) from exc
    except RecursionError as exc:
        raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' exceeds safe nesting limits") from exc
    _validate_constructed_graph(value, source=source)
    return value


def _validate_event_limits(text: str, *, source: str) -> None:
    depth = 0
    aliases = 0
    events = 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        events += 1
        if events > MAX_YAML_EVENTS:
            raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' contains too many nodes")
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            depth += 1
            if depth > MAX_YAML_DEPTH:
                raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' exceeds safe nesting depth")
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            depth = max(0, depth - 1)
        elif isinstance(event, AliasEvent):
            aliases += 1
            if aliases > MAX_YAML_ALIASES:
                raise SecurityPolicyError(f"Omni YAML file '{_source_label(source)}' contains too many aliases")


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


def _sanitized_yaml_error(source: str, exc: yaml.YAMLError) -> ConfigError:
    mark = getattr(exc, "problem_mark", None)
    location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
    return ConfigError(f"Could not parse Omni YAML file '{_source_label(source)}'{location}")


def _source_label(source: str) -> str:
    return str(source).replace("\r", "").replace("\n", "")[:240]
