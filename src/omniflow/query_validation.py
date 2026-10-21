"""Trusted, reviewed scalar query contracts; deliberately not arbitrary SQL.

The review attestations describe a human-reviewed oracle. They cannot prove
warehouse isolation, aggregate semantics, or data completeness by themselves.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .exceptions import ConfigError

MAX_CONTRACTS = 8
MAX_FIELDS = 20
MAX_QUERY_BYTES = 16_384
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,255}\Z")


def _invalid() -> ConfigError:
    return ConfigError(
        "Query validation requires reviewed, bounded scalar contracts with supported settings.",
        reason_code="invalid_query_validation_config",
    )


def _mapping(value: Any, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value) or set(value) - allowed:
        raise _invalid()
    return value


def _text(value: Any, *, optional: bool = False, limit: int = 256) -> str:
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > limit:
        raise _invalid()
    if "${" in value or any(ord(char) < 32 for char in value):
        raise _invalid()
    return value


def _number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        finite = type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise _invalid()
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise _invalid()
    return float(value)


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _invalid()
    return value


def _bool(value: Any) -> bool:
    if type(value) is not bool:
        raise _invalid()
    return value


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 12:
        raise _invalid()
    if value is None or type(value) is bool:
        return
    if type(value) in {int, float}:
        _number(value)
        return
    if isinstance(value, str):
        _text(value, optional=True, limit=2048)
        return
    if isinstance(value, list) and len(value) <= 100:
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, dict) and len(value) <= 100:
        for key, item in value.items():
            _text(key, limit=256)
            _json_value(item, depth + 1)
        return
    raise _invalid()


@dataclass(frozen=True)
class QueryAssertion:
    kind: str
    field: str = ""
    numerator: str = ""
    denominator: str = ""
    population: str = ""
    distinct: str = ""
    minimum: float | None = None
    maximum: float | None = None
    max_absolute_change: float | None = None
    max_relative_change: float | None = None


@dataclass(frozen=True)
class QueryContract:
    id: str
    query_json: str
    fields: tuple[str, ...]
    population_field: str
    minimum_population: int
    assertions: tuple[QueryAssertion, ...]
    comparison: str = "regression"
    reviewed: bool = False
    aggregate_only: bool = False
    independent_oracle: bool = False
    fixed_window: bool = False
    result_fields: tuple[tuple[str, str], ...] = ()
    numeric_encoding: str = "json_number"

    def query(self) -> dict:
        # Fresh copy for every execution; consumers cannot mutate frozen policy.
        return json.loads(self.query_json)


@dataclass(frozen=True)
class QueryValidationSettings:
    mode: str = "off"
    contracts: tuple[QueryContract, ...] = ()
    native_qualified: bool = False
    snapshot_attestation: str = ""
    environment_connection_id: str = ""
    user_id: str = ""
    timezone: str = ""
    max_queries: int = 12
    timeout_seconds: int = 120
    max_response_bytes: int = 1_048_576
    max_polls: int = 2

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


@dataclass(frozen=True)
class QueryProvenance:
    model_id: str
    branch_id: str
    base_sha: str
    head_sha: str
    base_verified: bool = False
    head_verified: bool = False
    added_entity_contract_ids: frozenset[str] = field(default_factory=frozenset)


def _assertion(value: Any, fields: tuple[str, ...], comparison: str) -> QueryAssertion:
    kinds = {
        "null_rate": {"numerator", "denominator", "maximum"},
        "unique_grain": {"population", "distinct", "maximum"},
        "group_count": {"field", "minimum", "maximum"},
        "aggregate_range": {"field", "minimum", "maximum"},
        "aggregate_delta": {"field", "max_absolute_change", "max_relative_change"},
        "rate_delta": {"numerator", "denominator", "max_absolute_change"},
    }
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or value.get("type") not in kinds:
        raise _invalid()
    kind = value["type"]
    data = _mapping(value, {"type"} | kinds[kind])
    result: dict[str, Any] = {"kind": kind}
    for key in kinds[kind] & {"field", "numerator", "denominator", "population", "distinct"}:
        if data.get(key) not in fields:
            raise _invalid()
        result[key] = data[key]
    for key in kinds[kind] - {"field", "numerator", "denominator", "population", "distinct"}:
        if key in data:
            result[key] = _number(
                data[key], minimum=0 if key.startswith("max_") or kind != "aggregate_range" else None,
                maximum=1 if kind in {"null_rate", "unique_grain", "rate_delta"} else None,
            )
    if kind in {"null_rate", "unique_grain"} and "maximum" not in result:
        raise _invalid()
    if kind in {"group_count", "aggregate_range"}:
        if not {"minimum", "maximum"} & result.keys():
            raise _invalid()
        if result.get("minimum", -math.inf) > result.get("maximum", math.inf):
            raise _invalid()
    if kind in {"aggregate_delta", "rate_delta"}:
        if comparison == "absolute" or not {"max_absolute_change", "max_relative_change"} & result.keys():
            raise _invalid()
    return QueryAssertion(**result)


def _contract(value: Any) -> QueryContract:
    data = _mapping(value, {
        "id", "query", "population_field", "minimum_population", "assertions", "comparison",
        "reviewed", "aggregate_only", "independent_oracle", "fixed_window", "result_fields", "numeric_encoding",
    })
    contract_id = _text(data.get("id"), limit=100)
    comparison = data.get("comparison", "regression")
    if not isinstance(comparison, str) or comparison not in {"regression", "absolute"}:
        raise _invalid()
    query = _mapping(data.get("query"), {"table", "fields", "filters", "join_paths_from_topic_name"})
    table = _text(query.get("table"))
    topic = _text(query.get("join_paths_from_topic_name"))
    if not _NAME.fullmatch(table) or not _NAME.fullmatch(topic):
        raise _invalid()
    fields = query.get("fields")
    if (not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS
            or any(not isinstance(item, str) or not _NAME.fullmatch(item) for item in fields)
            or len(set(fields)) != len(fields)):
        raise _invalid()
    filters = query.get("filters", {})
    if not isinstance(filters, dict):
        raise _invalid()
    _json_value(filters)
    frozen = json.dumps({**query, "filters": filters}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(frozen.encode()) > MAX_QUERY_BYTES:
        raise _invalid()
    population = data.get("population_field")
    if population not in fields:
        raise _invalid()
    result_fields = data.get("result_fields", dict(zip(fields, fields, strict=True)))
    if not isinstance(result_fields, dict) or set(result_fields) != set(fields):
        raise _invalid()
    labels = [_text(result_fields[item]) for item in fields]
    if len(set(labels)) != len(labels):
        raise _invalid()
    numeric_encoding = data.get("numeric_encoding", "json_number")
    if not isinstance(numeric_encoding, str) or numeric_encoding not in {"json_number", "number_or_decimal_string"}:
        raise _invalid()
    assertions = data.get("assertions")
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 20:
        raise _invalid()
    return QueryContract(
        id=contract_id, query_json=frozen, fields=tuple(fields), population_field=population,
        minimum_population=_integer(data.get("minimum_population", 1), 1, 10**15),
        assertions=tuple(_assertion(item, tuple(fields), comparison) for item in assertions), comparison=comparison,
        result_fields=tuple(zip(fields, labels, strict=True)), numeric_encoding=numeric_encoding,
        **{key: _bool(data.get(key, False)) for key in (
            "reviewed", "aggregate_only", "independent_oracle", "fixed_window",
        )},
    )


def parse_query_validation(value: Any = None) -> QueryValidationSettings:
    if value is None:
        return QueryValidationSettings()
    data = _mapping(value, {
        "mode", "contracts", "native_qualified", "snapshot_attestation", "environment_connection_id",
        "user_id", "timezone", "max_queries", "timeout_seconds", "max_response_bytes", "max_polls",
    })
    mode = data.get("mode", "off")
    if not isinstance(mode, str) or mode not in {"off", "report", "enforce"}:
        raise _invalid()
    raw_contracts = data.get("contracts", [])
    if not isinstance(raw_contracts, list) or len(raw_contracts) > MAX_CONTRACTS:
        raise _invalid()
    contracts = tuple(_contract(item) for item in raw_contracts)
    if len({item.id for item in contracts}) != len(contracts) or mode != "off" and not contracts:
        raise _invalid()
    timezone = _text(data.get("timezone", ""), optional=True)
    if timezone:
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise _invalid() from exc
    return QueryValidationSettings(
        mode=mode, contracts=contracts, timezone=timezone,
        native_qualified=_bool(data.get("native_qualified", False)),
        snapshot_attestation=_text(data.get("snapshot_attestation", ""), optional=True, limit=1000),
        environment_connection_id=_text(data.get("environment_connection_id", ""), optional=True),
        user_id=_text(data.get("user_id", ""), optional=True),
        max_queries=_integer(data.get("max_queries", 12), 1, 24),
        timeout_seconds=_integer(data.get("timeout_seconds", 120), 1, 600),
        max_response_bytes=_integer(data.get("max_response_bytes", 1_048_576), 1024, 4_194_304),
        max_polls=_integer(data.get("max_polls", 2), 0, 10),
    )
