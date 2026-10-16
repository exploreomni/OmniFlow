import json

import pytest

from omniflow import repository_locations
from omniflow.diagnostic_catalog import CHANGE_MESSAGES, LINT_MESSAGES
from omniflow.repository_locations import DIAGNOSTIC_MESSAGES, SnapshotIndex

ORDERS = """# Authored view
name: orders
dimensions:
  id:
    sql: ${TABLE}.private_identifier
  revenue:
    sql: ${TABLE}.private_revenue
measures:
  total:
    aggregate_type: sum
    sql: ${orders.revenue}
description: Synthetic patient diagnosis MRN-TEST-123 CVV-123 4242424242424242
"""
FILES = {
    "views/orders.view": ORDERS,
    "topics/orders.topic": "# Topic header\nname: orders\nbase_view: orders\n",
    "model.yaml": "topics:\n  finance:\n    base_view: orders\n",
}


def index(files=None, model_path="omni/sales"):
    return SnapshotIndex(model_path, FILES if files is None else files)


@pytest.mark.parametrize("rule,identity,file,line", [
    ("require_field_descriptions", {"field": "orders.id"}, "views/orders.view", 4),
    ("require_measure_descriptions", {"field": "orders.total"}, "views/orders.view", 9),
    ("require_primary_keys", {"view": "orders"}, "views/orders.view", 2),
    ("require_topic_labels", {"topic": "orders"}, "topics/orders.topic", 2),
    ("require_owner_metadata", {"topic": "finance"}, "model.yaml", 2),
])
def test_lint_uses_authored_identity_and_actual_source_key(rule, identity, file, line):
    detail = index().detail({"validator": "semantic_lint", "rule_id": rule, "file": file,
                             "message": "Synthetic private API message", **identity})
    assert detail["file"] == f"omni/sales/{file}"
    assert detail["line"] == line
    assert all(detail[key] == value for key, value in identity.items())
    assert detail["diagnostic_code"] == rule
    assert detail["message"] == DIAGNOSTIC_MESSAGES[rule]
    assert detail["message"] == LINT_MESSAGES[rule]
    assert "Synthetic private" not in json.dumps(detail)


@pytest.mark.parametrize("reference", ["views/orders.view", "omni/sales/views/orders.view"])
def test_model_comma_path_maps_only_exact_authored_keys(reference):
    detail = index().detail({"validator": "model", "yaml_path": f"{reference},measures,total,sql",
                             "message": "Private SQL or unrelated cause", "line": 999})
    assert detail == {
        "file": "omni/sales/views/orders.view", "line": 11, "field": "orders.total", "view": "orders",
        "diagnostic_code": "model_repository_issue", "message": DIAGNOSTIC_MESSAGES["model_repository_issue"],
    }


def test_exact_yaml_property_line_is_preserved_when_structured_field_is_also_supplied():
    detail = index().detail({"validator": "model", "yaml_path": "views/orders.view,measures,total,sql",
                             "field": "orders.total", "view": "orders"})
    assert detail["line"] == 11  # SQL key, not total's definition on line 9.
    assert detail["field"] == "orders.total"
    assert "Inspect the definition at this key" in detail["message"]
    assert "same model branch in Omni" in detail["message"]


@pytest.mark.parametrize("validator", ["contracts", "semantic_diff"])
@pytest.mark.parametrize("kind", ["field_deleted", "field_type_changed", "governance_changed"])
def test_recognized_semantic_change_keeps_fixed_cause_and_corrective_guidance(validator, kind):
    detail = index().detail({"validator": validator, "type": kind, "file": "views/orders.view",
                             "message": "Private API change detail", "field": "orders.revenue"})
    assert detail["diagnostic_code"] == kind
    assert detail["message"] == CHANGE_MESSAGES[kind]
    assert detail["line"] == 6 and detail["field"] == "orders.revenue"
    assert "Private API" not in json.dumps(detail)


def test_change_type_does_not_invent_a_model_root_cause():
    detail = index().detail({"validator": "model", "type": "field_deleted", "file": "views/orders.view"})
    assert detail["diagnostic_code"] == "model_repository_issue"
    assert detail["message"] != CHANGE_MESSAGES["field_deleted"]


@pytest.mark.parametrize("yaml_path", ["views/orders.view", "views/orders.view,measures,missing",
                                      "views/orders.view,measures,total,missing", "views/orders.view,[0]"])
def test_exact_file_fallback_never_guesses_line_or_missing_field(yaml_path):
    detail = index().detail({"validator": "model", "yaml_path": yaml_path, "message": "Field not found"})
    assert detail == {"file": "omni/sales/views/orders.view", "diagnostic_code": "model_repository_issue",
                      "message": DIAGNOSTIC_MESSAGES["model_repository_issue"]}
    assert "not found" not in detail["message"]


@pytest.mark.parametrize("issue,code", [
    ({"field": "orders.revenue", "message": "An unknown API cause"}, "content_repository_issue"),
    ({"raw": {"field": "orders.revenue"}}, "content_repository_issue"),
    ({"message": "Field orders.revenue was not found"}, "content_field_reference_unavailable"),
    ({"message": "Private dashboard: Field orders.revenue was not found",
      "raw": {"message": "Field orders.revenue was not found"}}, "content_field_reference_unavailable"),
])
def test_content_uses_only_structured_verified_keys_or_complete_recognized_message(issue, code):
    detail = index().detail({"validator": "content", **issue})
    assert detail["field"] == "orders.revenue" and detail["line"] == 6
    assert detail["diagnostic_code"] == code
    assert detail["message"] == DIAGNOSTIC_MESSAGES[code]


@pytest.mark.parametrize("message", [
    "Private patient text: Field orders.revenue was not found",
    "Field orders.revenue was not found in customer private SQL",
    "Field orders.missing was not found", "Something mentions orders.revenue somewhere",
    "Field orders.revenue was not found\n", "Field not found",
])
def test_content_does_not_extract_substrings_or_invent_missing_reference_identity(message):
    assert index().detail({"validator": "content", "message": message}) == {}


@pytest.mark.parametrize("issue", [
    {"file": "topics/orders.topic", "field": "orders.id"},
    {"file": "views/orders.view", "yaml_path": "topics/orders.topic,base_view"},
    {"yaml_path": "views/orders.view,dimensions,id", "field": "orders.revenue"},
    {"file": "views/orders.view", "field": "missing.id"},
    {"file": "orders.view", "field": "orders.id"},
    {"file": "views__orders.view", "field": "orders.id"},
])
def test_conflicting_cross_file_and_inferred_api_aliases_are_not_locations(issue):
    assert index().detail({"validator": "model", **issue}) == {}


def test_duplicate_semantic_names_and_field_groups_are_not_guessed():
    duplicate_view = {**FILES, "other/orders.view": "name: orders\ndimensions:\n  id:\n    sql: 1\n"}
    assert index(duplicate_view).detail({"field": "orders.id"}) == {}
    assert index(duplicate_view).detail({"view": "orders"}) == {}
    duplicate_field = {"orders.view": "name: orders\ndimensions:\n  id: {}\nmeasures:\n  id: {}\n"}
    assert index(duplicate_field).detail({"field": "orders.id"}) == {}
    # Exact key-path coordinates remain factual, without ambiguous field metadata.
    detail = index(duplicate_field).detail({"yaml_path": "orders.view,measures,id"})
    assert detail["line"] == 5 and "field" not in detail


def test_prefixed_path_collision_is_ambiguous_even_when_both_files_exist():
    locations = index({"orders.view": ORDERS, "omni/orders.view": ORDERS}, model_path="omni")
    assert locations.detail({"file": "omni/orders.view"}) == {}


@pytest.mark.parametrize("path", ["../orders.view", "/tmp/orders.view", "./orders.view", "a//orders.view",
                                  "views\\orders.view", "views/orders.view\n", "orders.view?token=abc",
                                  "orders.view#fragment", "orders%2fsecret.view", "ssn_000_12_3456.view",
                                  "mrn_TEST123.view", "card_4242424242424242.view", "me@example.com.view"])
def test_path_injection_and_sensitive_path_metadata_never_leaves_index(path):
    assert index({path: ORDERS}).detail({"file": path}) == {}
    assert index().detail({"file": path, "field": "orders.id"}) == {}
    assert index(model_path=path).detail({"field": "orders.id"}) == {}


@pytest.mark.parametrize("name", ["ssn_000_12_3456", "mrn_TEST123", "cvv_123", "account_4242424242424242",
                                  "a@example.com", "unicode_\u03a9", "[REDACTED]", "John Smith",
                                  "sk_placeholder_not_a_real_key", "ghp_not_a_real_token_fixture"])
def test_sensitive_or_unsafe_authored_identifiers_are_omitted(name):
    text = f"name: orders\ndimensions:\n  {json.dumps(name)}:\n    sql: private_value\n"
    assert index({"orders.view": text}).detail({"field": f"orders.{name}"}) == {}
    detail = index({"orders.view": text}).detail({"file": "orders.view"})
    assert name not in json.dumps(detail)
    assert "private_value" not in json.dumps(detail)


@pytest.mark.parametrize("view,field", [("patients", "diagnosis_code"), ("customers", "email"),
                                       ("patients", "ssn"), ("customers", "address"), ("accounts", "api_token")])
def test_conventional_schema_identifiers_are_metadata_not_sensitive_values(view, field):
    text = f"name: {view}\ndimensions:\n  {field}:\n    sql: 'private@example.com'\n"
    detail = index({f"{view}.view": text}).detail({"field": f"{view}.{field}"})
    assert detail["field"] == f"{view}.{field}" and detail["line"] == 3
    assert "private@example.com" not in json.dumps(detail)


def test_active_credential_guard_blocks_identifier_even_when_ascii_and_authored(monkeypatch):
    monkeypatch.setenv("OMNI_API_KEY", "SyntheticCredential")
    locations = index({"orders.view": "name: orders\ndimensions:\n  SyntheticCredential: {}\n"})
    assert locations.detail({"field": "orders.SyntheticCredential"}) == {}


@pytest.mark.parametrize("text", [
    "name: orders\nname: other\n", "name: orders\ndimensions:\n  id: {}\n  id: {}\n",
    "name: orders\nmetadata: {duplicate: 1, duplicate: 2}\n", "name: orders\nyes: yes\n",
    "name: orders\n? [complex, key]\n: value\n", "name: orders\nanchor: &x {}\ncopy: *x\n",
    "name: orders\nvalue: !!python/object:unknown {}\n", "name: orders\nvalue: !unknown secret\n",
    "name: orders\n---\nname: other\n", "name: orders\nvalue: [unterminated\n",
    "name: orders\nloop: &loop [*loop]\n",
])
def test_malformed_alias_tag_and_ambiguous_mapping_files_have_no_locations(text):
    assert index({"orders.view": text}).detail({"file": "orders.view"}) == {}


@pytest.mark.parametrize("limit", ["files", "file_bytes", "total_bytes", "depth", "events"])
def test_parser_and_inventory_limits_fail_closed(limit, monkeypatch):
    constant = {"files": "MAX_FILES", "file_bytes": "MAX_FILE_BYTES", "total_bytes": "MAX_TOTAL_BYTES",
                "depth": "MAX_DEPTH", "events": "MAX_EVENTS"}[limit]
    monkeypatch.setattr(repository_locations, constant, 1)
    assert index().detail({"field": "orders.id"}) == {}


def test_repository_root_and_authored_name_supported_without_api_name_inference():
    locations = index({"schema/orders.view": "name: schema__orders\ndimensions:\n  id: {}\n"}, model_path=".")
    detail = locations.detail({"field": "schema__orders.id"})
    assert detail["file"] == "schema/orders.view" and detail["line"] == 3
    assert locations.detail({"field": "schema.orders.id"}) == {}
    assert locations.detail({"field": "orders.id"}) == {}


@pytest.mark.parametrize("text", ["type: topic\nlabel: Orders\n", "base_view: orders\nlabel: Orders\n"])
def test_generic_yaml_topic_shape_does_not_fabricate_a_view(text):
    locations = index({"foo.yaml": text})
    assert locations.detail({"view": "foo"}) == {}
    assert locations.detail({"topic": "foo"})["topic"] == "foo"
    detail = locations.detail({"validator": "model", "yaml_path": "foo.yaml,label"})
    assert detail["line"] == 2 and detail["topic"] == "foo"
    assert "view" not in detail


def test_generic_yaml_unknown_shape_retains_key_location_without_guessing_identity():
    locations = index({"foo.yaml": "description: Private source text\n"})
    assert locations.detail({"view": "foo"}) == {}
    detail = locations.detail({"yaml_path": "foo.yaml,description"})
    assert detail["line"] == 1
    assert not {"field", "view", "topic"} & detail.keys()


@pytest.mark.parametrize("explicit", [None, "name", "view"])
def test_scoped_view_requires_explicit_authored_identity_but_preserves_exact_key_location(explicit):
    text = "dimensions:\n  id:\n    sql: private_source_value\n"
    if explicit:
        text = f"{explicit}: schema__orders\n" + text
    locations = index({"schema/orders.view": text})
    assert locations.detail({"field": "orders.id"}) == {}
    detail = locations.detail({"yaml_path": "schema/orders.view,dimensions,id,sql"})
    assert detail["line"] == (4 if explicit else 3)
    if explicit:
        assert detail["field"] == "schema__orders.id" and detail["view"] == "schema__orders"
    else:
        assert locations.detail({"field": "schema__orders.id"}) == {}
        assert not {"field", "view", "topic"} & detail.keys()


def test_root_filename_and_nested_inline_topic_keys_remain_explicit_and_unambiguous():
    locations = index({"orders.view": "dimensions:\n  id: {}\n",
                       "config/model.yaml": "topics:\n  finance:\n    base_view: orders\n"})
    assert locations.detail({"field": "orders.id"})["line"] == 2
    assert locations.detail({"topic": "finance"})["line"] == 2


def test_unknown_diagnostic_gets_generic_text_and_no_values_ids_or_untrusted_line():
    issue = {
        "validator": "semantic_lint", "rule_id": "unknown_custom_rule", "file": "views/orders.view",
        "message": ORDERS, "field": "orders.revenue", "document_id": "private-resource",
        "name": "Private Customer", "sql": "SELECT private", "line": 99999,
    }
    detail = index().detail(issue)
    assert detail["line"] == 6 and detail["diagnostic_code"] == "repository_validation_issue"
    assert set(detail) == {"file", "line", "field", "view", "diagnostic_code", "message"}
    assert not any(value in json.dumps(detail) for value in ("MRN-TEST", "CVV-123", "4242424242424242",
                                                            "private-resource", "SELECT", "${TABLE}", "Private Customer"))


@pytest.mark.parametrize("issue", [None, [], 7, {"file": []}, {"field": []}, {"yaml_path": {}},
                                   {"file": "views/orders.view", "rule_id": {"private": "value"}}])
def test_untrusted_shape_never_throws_or_copies_data(issue):
    detail = index().detail(issue)
    assert set(detail) <= {"file", "line", "field", "view", "topic", "diagnostic_code", "message"}
    assert "private" not in json.dumps(detail)


@pytest.mark.parametrize("filename", ["model", "config/model"])
def test_extensionless_authored_model_has_exact_inline_topic_location(filename):
    locations = SnapshotIndex("omni", {filename: "topics:\n  finance:\n    base_view: orders\n"})
    detail = locations.detail({"validator": "semantic_lint", "rule_id": "require_owner_metadata", "topic": "finance"})
    assert detail["file"] == f"omni/{filename}"
    assert detail["line"] == 2
    assert detail["topic"] == "finance"
