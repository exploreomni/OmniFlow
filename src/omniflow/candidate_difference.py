"""Bounded, pure classification of text already shown to differ by the exact verifier.

This explains a mismatch; none of these classifications waives byte equality.
The proof used for comment/whitespace classification is shared with PR change
classification and does not execute or render customer-controlled code.
"""
from __future__ import annotations

from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from .exceptions import OmniFlowError
from .yaml_security import UnsupportedComparisonYaml, parse_secure_yaml

MAX_CLASSIFICATION_BYTES = 128 * 1024
DIFFERENCE_CLASSES = frozenset({
    "line_endings_only", "terminal_newline_only", "yaml_comment_or_whitespace_only",
    "authored_text_changed", "unclassified_text_difference",
})
LINE_ENDING_STYLES = frozenset({"lf", "crlf", "cr", "mixed"})

_DEPENDENCY_KEYS = {"extends", "extend", "extension", "extensions", "include", "includes",
                    "import", "imports", "inherit", "inherits", "inheritance", "dependencies",
                    "dependency", "parent_model", "base_model", "shared_model", "dbt"}
_SCALAR_TAGS = {f"tag:yaml.org,2002:{name}" for name in ("str", "int", "float", "bool", "null")}


class _Unverified(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def shareable_difference_fields(evidence: dict[str, Any]) -> dict[str, str]:
    """Reconstruct a fixed schema; never propagate source values, paths or line positions."""
    kind = evidence.get("difference_class")
    if not isinstance(kind, str) or kind not in DIFFERENCE_CLASSES:
        return {}
    safe = {"difference_class": kind}
    missing = evidence.get("newline_missing_from")
    if kind == "terminal_newline_only" and isinstance(missing, str) and missing in {"git", "omni"}:
        safe["newline_missing_from"] = missing
    if kind == "line_endings_only":
        for key in ("git_line_ending", "omni_line_ending"):
            value = evidence.get(key)
            if isinstance(value, str) and value in LINE_ENDING_STYLES:
                safe[key] = value
    return safe


def _ending_style(text: str) -> str:
    crlf = text.count("\r\n")
    styles = [name for name, count in (
        ("crlf", crlf), ("cr", text.count("\r") - crlf), ("lf", text.count("\n") - crlf),
    ) if count]
    return styles[0] if len(styles) == 1 else "mixed"


def difference_details(git: str, omni: str) -> dict:
    """Describe first unequal character/EOF in bounded, already decoded source."""
    if git == omni:
        return {}
    at = next((i for i, (a, b) in enumerate(zip(git, omni, strict=False)) if a != b),
              min(len(git), len(omni)))
    prefix = git[:at]
    line = 1 + prefix.count("\n") + prefix.count("\r") - prefix.count("\r\n")
    if prefix.endswith("\r") and (git[at:at + 1] == "\n" or omni[at:at + 1] == "\n"):
        line -= 1  # A divergence *inside* CRLF is still on the preceding physical line.
    detail = {"first_differing_line": line}

    def normalize(s):
        return s.replace("\r\n", "\n").replace("\r", "\n")

    if normalize(git) == normalize(omni):
        detail.update(difference_class="line_endings_only", git_line_ending=_ending_style(git),
                      omni_line_ending=_ending_style(omni))
    elif not git.endswith(("\r", "\n")) and omni[len(git):] in {"\r\n", "\n", "\r"} and omni.startswith(git):
        detail.update(difference_class="terminal_newline_only", newline_missing_from="git")
    elif not omni.endswith(("\r", "\n")) and git[len(omni):] in {"\r\n", "\n", "\r"} and git.startswith(omni):
        detail.update(difference_class="terminal_newline_only", newline_missing_from="omni")
    elif git.rstrip("\r\n") == omni.rstrip("\r\n"):
        # A different number of trailing line breaks is not necessarily a
        # *missing* final newline. Do not invent direction for this case.
        detail["difference_class"] = "terminal_newline_only"
    elif max(len(git.encode()), len(omni.encode())) > MAX_CLASSIFICATION_BYTES:
        detail["difference_class"] = "unclassified_text_difference"
    else:
        try:
            detail["difference_class"] = ("yaml_comment_or_whitespace_only" if _yaml_signature(git) == _yaml_signature(omni)
                                          else "authored_text_changed")
        except Exception:
            # Classification is optional explanation. An unsupported/parser
            # failure must not turn the already established mismatch into an
            # internal error, change its exit code, or expose parser prose.
            detail["difference_class"] = "unclassified_text_difference"
    return detail


def _yaml_signature(text: str) -> tuple:
    try:
        # Shared limits bound file size, nesting and parser work. Construction
        # does not define equality: coercions would hide authored differences.
        parse_secure_yaml(text, source="PR snapshot", comparison=True)
        tokens = list(yaml.scan(text, Loader=yaml.SafeLoader))
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        if node is None:
            raise _Unverified("empty_yaml_document")
        _validate_node(node)
        # YAML's scanner, not regex comment stripping, identifies actual scalar
        # spans. Retain exact authored scalar bytes, types, order and delimiters;
        # a '#' inside SQL, a quoted value or a block is never discarded.
        return tuple((type(token).__name__, getattr(token, "value", None),
                      getattr(token, "style", None),
                      text[token.start_mark.index:token.end_mark.index]) for token in tokens)
    except UnsupportedComparisonYaml:
        raise _Unverified("unsupported_yaml_construct") from None
    except (yaml.YAMLError, RecursionError, OmniFlowError):
        raise _Unverified("invalid_or_unsupported_yaml") from None


def _validate_node(node: Any) -> None:
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                raise _Unverified("ambiguous_yaml_key")
            if key.value in seen:
                raise _Unverified("duplicate_yaml_key")
            seen.add(key.value)
            if key.value.casefold() in _DEPENDENCY_KEYS:
                raise _Unverified("external_dependency_unverified")
            _validate_node(value)
    elif isinstance(node, SequenceNode):
        for child in node.value:
            _validate_node(child)
    elif not isinstance(node, ScalarNode) or node.tag not in _SCALAR_TAGS:
        raise _Unverified("unsupported_yaml_construct")
    elif any(marker in node.value for marker in ("{{", "{%")):
        raise _Unverified("external_dependency_unverified")
