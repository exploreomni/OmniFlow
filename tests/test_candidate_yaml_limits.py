"""Adversarial source stays bounded before PyYAML constructs or flattens a mapping."""

import pytest
import yaml

from omniflow.candidate_change_scope import compare_pr_definitions, equivalent
from omniflow.candidate_difference import difference_details
from omniflow.exceptions import SecurityPolicyError
from omniflow.yaml_security import parse_secure_yaml


@pytest.mark.parametrize("source", [
    "x: &name {label: example}\ny: *name\n",
    "x: {<<: {label: example}}\n",
    "x: { !!merge '<<': {label: example}}\n",
])
def test_comparison_and_diagnostics_reject_references_before_construction(monkeypatch, source):
    monkeypatch.setattr(yaml, "safe_load", lambda *_: pytest.fail("constructed unsupported YAML"))
    assert not equivalent(source, "x: different\n")
    assert difference_details(source, "x: different\n")["difference_class"] == "unclassified_text_difference"


def test_ordinary_loader_bounds_expansion_before_construction(monkeypatch):
    # Small ordinary merges remain supported outside source comparison.
    assert parse_secure_yaml("a: &a {ok: 1}\nb: {<<: *a}", source="example")["b"] == {"ok": 1}
    text = "a0: &a0 {ok: 1}\n" + "".join(
        f"a{i}: &a{i} {{<<: [*a{i-1}, *a{i-1}]}}\n" for i in range(1, 19)
    )
    monkeypatch.setattr(yaml, "safe_load", lambda *_: pytest.fail("constructed oversized YAML"))
    with pytest.raises(SecurityPolicyError, match="expanded node limit"):
        parse_secure_yaml(text, source="example")


def test_change_to_existing_field_type_requires_consumer_verification():
    base = "measures: {amount: {aggregate_type: sum, description: Original}}"
    assert compare_pr_definitions(base, base.replace("sum", "count"), view=True) == (False, True)
    assert compare_pr_definitions(base, base.replace("Original", "Revised"), view=True) == (False, False)
