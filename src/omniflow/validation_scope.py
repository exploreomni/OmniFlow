"""Explicit per-run evidence and conservative experimental content request scopes."""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from inspect import getattr_static
from typing import Any

from .exceptions import OmniAPIError

MAX_TARGETED_SELECTORS = 8
_FIELD_CHANGES = frozenset({
    "field_added", "field_deleted", "field_modified", "field_type_changed",
    "field_kind_changed", "field_renamed", "measure_aggregation_changed",
})
_NAMED_CHANGES = frozenset(
    f"{kind}_{change}" for kind in ("view", "topic") for change in ("added", "deleted", "modified")
)


@dataclass(frozen=True)
class ContentScopePlan:
    mode: str = "broad"
    reason: str = "targeted_disabled"
    selectors: tuple[tuple[str, str], ...] = ()


def plan_content_scope(
    diff_result: Any, *, enabled: bool = False, coverage_complete: bool = False,
    drafts_verified_absent: bool = False, affected_references: Any = (),
    max_selectors: int = MAX_TARGETED_SELECTORS,
) -> ContentScopePlan:
    """Build identical before/after selectors only after independent qualification.

    A semantic diff alone is NOT coverage proof: it may omit global constructs
    and it cannot establish live content/draft state. Callers must independently
    prove dependency coverage and absence of branch-attached drafts. Until then
    even an enabled policy uses broad validation.
    """
    def broad(reason):
        return ContentScopePlan(reason=reason)

    if not enabled:
        return broad("targeted_disabled")
    if coverage_complete is not True:
        return broad("semantic_coverage_unverified")
    if drafts_verified_absent is not True:
        return broad("draft_coverage_unverified")
    if (not isinstance(diff_result, dict) or not isinstance(diff_result.get("changes"), list)
            or diff_result.get("coverage_gaps")):
        return broad("semantic_coverage_unverified")
    if type(max_selectors) is not int or not 1 <= max_selectors <= MAX_TARGETED_SELECTORS:
        return broad("targeted_request_limit")
    selected: set[tuple[str, str]] = set()

    def add(kind, name):
        if (kind not in {"FIELD", "VIEW", "TOPIC"} or not isinstance(name, str)
                or not name or name != name.strip() or len(name) > 512
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or (kind == "FIELD" and "." not in name)):
            return False
        selected.add((kind, name))
        return True

    for change in diff_result["changes"]:
        if not isinstance(change, dict):
            return broad("semantic_coverage_unverified")
        kind = change.get("type")
        if kind in _FIELD_CHANGES:
            if not add("FIELD", change.get("field")):
                return broad("semantic_coverage_unverified")
            if kind == "field_renamed" or "previous_field" in change:
                if not add("FIELD", change.get("previous_field")):
                    return broad("semantic_coverage_unverified")
        elif kind in _NAMED_CHANGES:
            if not add(kind.split("_", 1)[0].upper(), change.get("name")):
                return broad("semantic_coverage_unverified")
        else:
            # Includes relationships, governance, model/global properties and
            # unknown future change types. Never guess their dependency closure.
            return broad("unsupported_semantic_change")
    if not isinstance(affected_references, (list, tuple)):
        return broad("semantic_coverage_unverified")
    for reference in affected_references:
        if not isinstance(reference, dict) or not isinstance(reference.get("type"), str):
            return broad("semantic_coverage_unverified")
        if not add(reference["type"].upper(), reference.get("name")):
            return broad("semantic_coverage_unverified")
    if not selected:
        return broad("empty_semantic_scope")
    if len(selected) > max_selectors:
        return broad("targeted_request_limit")
    return ContentScopePlan("targeted", "qualified_semantic_scope", tuple(sorted(selected)))


@dataclass
class ValidationEvidence:
    """Caller-owned run-local snapshots. Never share across runs or credentials.

    Cache keys include the exact client object, model, branch, visibility, depth
    and selector parameters. Callers receive copies; mandatory consistency
    rereads use refresh=True. Errors are never cached.
    """
    _snapshots: dict[tuple, Any] = field(default_factory=dict, repr=False)
    _clients: dict[int, Any] = field(default_factory=dict, repr=False)
    _bindings: dict[int, tuple[str, str] | None] = field(default_factory=dict, repr=False)
    _invalidated_clients: set[int] = field(default_factory=set, repr=False)

    def assert_client_binding(self, client: Any) -> None:
        """Freeze transport identity, including before serving cached evidence.

        Real clients expose a no-IO credential/origin binding. Older lightweight
        test clients without the method remain compatible, but gaining or losing
        the method mid-run is also a change. Dynamic mock attributes are not an
        implementation of this contract. Never put binding values in diagnostics.
        """
        identity = id(client)
        self._clients[identity] = client
        if identity in self._invalidated_clients:
            raise OmniAPIError("Validation identity changed during this run; start a fresh validation run",
                               reason_code="validation_identity_changed")
        binding = None
        if getattr_static(client, "validation_evidence_binding", None) is not None:
            reader = client.validation_evidence_binding
            binding = reader() if callable(reader) else None
            if (not isinstance(binding, tuple) or len(binding) != 2
                    or not isinstance(binding[0], str) or not binding[0].strip()
                    or not isinstance(binding[1], str) or re.fullmatch(r"[0-9a-f]{64}", binding[1]) is None):
                self._invalidated_clients.add(identity)
                raise OmniAPIError("Validation identity could not be verified for this run",
                                   reason_code="validation_identity_changed")
        if identity not in self._bindings:
            self._bindings[identity] = binding
        elif self._bindings[identity] != binding:
            self._invalidated_clients.add(identity)
            raise OmniAPIError("Validation identity changed during this run; start a fresh validation run",
                               reason_code="validation_identity_changed")

    def fetch(self, client: Any, method: str, model_id: str, *, refresh: bool = False, **params: Any) -> Any:
        if method not in {"validate_model", "validate_content"}:
            raise ValueError("Unsupported validation evidence method")
        self.assert_client_binding(client)
        key = (id(client), method, model_id, tuple(sorted(params.items())))
        if refresh or key not in self._snapshots:
            try:
                payload = deepcopy(getattr(client, method)(model_id, **params))
            finally:
                # Also reject identity drift during a failed request. Never
                # persist a response acquired across a changed binding.
                self.assert_client_binding(client)
            self._snapshots[key] = payload
        result = deepcopy(self._snapshots[key])
        self.assert_client_binding(client)
        return result
