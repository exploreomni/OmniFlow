"""Heuristic column extraction from dbt model SQL.

This is the fallback used when a repository does not commit a dbt manifest. It
reads select-list aliases out of a model's SQL so a removed or renamed output
column can be compared against Omni field references.

The heuristic is deliberately conservative. It cannot resolve Jinja, macros,
``ref()``/``source()`` indirection, or dynamic column lists, so it reports lower
confidence than manifest mode and is designed to under-report rather than invent
findings. Anything it cannot parse cleanly is skipped.
"""

from __future__ import annotations

import re

from .exceptions import SecurityPolicyError

MAX_SQL_BYTES = 5 * 1024 * 1024
# Jinja blocks are removed before parsing because their contents are unresolvable.
JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# Matches an explicit trailing alias: "expr AS alias" or "expr AS \"alias\"".
ALIAS_RE = re.compile(
    r"""\bAS\s+(?:"(?P<quoted>[^"]+)"|`(?P<backtick>[^`]+)`|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))\s*$""",
    re.IGNORECASE,
)
BARE_COLUMN_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)$")
SQL_KEYWORDS = {
    "select",
    "from",
    "where",
    "group",
    "order",
    "having",
    "limit",
    "union",
    "join",
    "on",
    "with",
    "as",
    "and",
    "or",
    "not",
    "case",
    "when",
    "then",
    "else",
    "end",
    "distinct",
    "all",
}


def extract_output_columns(sql: str) -> set[str]:
    """Return the probable output column names of a dbt model's SQL.

    Only the final top-level SELECT is considered, since that is what determines
    the materialized relation's columns. CTEs are stripped.
    """
    if not isinstance(sql, str) or not sql.strip():
        return set()
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise SecurityPolicyError("dbt model SQL exceeds the 5 MiB safety limit")

    cleaned = _strip_noise(sql)
    select_list = _final_select_list(cleaned)
    if select_list is None:
        return set()

    columns: set[str] = set()
    for item in _split_top_level(select_list):
        name = _column_name(item)
        if name:
            columns.add(name)
    return columns


def _strip_noise(sql: str) -> str:
    without_jinja = JINJA_RE.sub(" ", sql)
    without_block = BLOCK_COMMENT_RE.sub(" ", without_jinja)
    return LINE_COMMENT_RE.sub(" ", without_block)


def _final_select_list(sql: str) -> str | None:
    """Isolate the select list of the last top-level SELECT.

    Returns None when the statement uses ``SELECT *`` or cannot be parsed with
    confidence, so callers treat the model as 'columns unknown' instead of
    assuming an empty column set.
    """
    depth = 0
    select_starts: list[int] = []
    upper = sql.upper()
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and upper.startswith("SELECT", index) and _is_boundary(sql, index, len("SELECT")):
            select_starts.append(index + len("SELECT"))
        index += 1
    if not select_starts:
        return None

    start = select_starts[-1]
    end = _matching_from(sql, upper, start)
    select_list = sql[start:end].strip()
    if not select_list:
        return None
    # A bare star means the columns are inherited and cannot be enumerated here.
    if select_list.strip() == "*" or re.search(r"(^|,)\s*\*\s*($|,)", select_list):
        return None
    return select_list


def _matching_from(sql: str, upper: str, start: int) -> int:
    depth = 0
    index = start
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return index
            depth -= 1
        elif depth == 0 and upper.startswith("FROM", index) and _is_boundary(sql, index, len("FROM")):
            return index
        index += 1
    return length


def _is_boundary(sql: str, index: int, keyword_length: int) -> bool:
    before = sql[index - 1] if index > 0 else " "
    after_index = index + keyword_length
    after = sql[after_index] if after_index < len(sql) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _split_top_level(select_list: str) -> list[str]:
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for char in select_list:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        items.append("".join(current))
    return [item.strip() for item in items if item.strip()]


def _column_name(item: str) -> str | None:
    collapsed = " ".join(item.split())
    alias = ALIAS_RE.search(collapsed)
    if alias:
        name = alias.group("quoted") or alias.group("backtick") or alias.group("bare")
        return name.strip().lower() if name else None
    # No explicit alias: only accept a plain column or qualified column reference.
    bare = BARE_COLUMN_RE.match(collapsed)
    if bare:
        name = bare.group("name").strip().lower()
        return None if name in SQL_KEYWORDS else name
    return None


def diff_sql_columns(base_sql: str, head_sql: str) -> set[str]:
    """Return output columns present in the base SQL but absent from the head.

    An empty base column set means the base could not be parsed with confidence,
    so no removal is reported.
    """
    base_columns = extract_output_columns(base_sql)
    if not base_columns:
        return set()
    head_columns = extract_output_columns(head_sql)
    if not head_columns:
        # The head is unparseable; refuse to claim every base column was removed.
        return set()
    return base_columns - head_columns


def model_name_from_path(path: str) -> str | None:
    """Derive a dbt model name from its repository path."""
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = path.strip().strip("/").rsplit("/", 1)[-1]
    for suffix in (".sql", ".py"):
        if candidate.lower().endswith(suffix):
            candidate = candidate[: -len(suffix)]
            break
    else:
        return None
    stripped = candidate.strip()
    return stripped.lower() or None
