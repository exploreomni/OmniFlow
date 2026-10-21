"""Bounded synthetic checks for run-local evidence and experimental targeting."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow.exceptions import OmniAPIError, OmniResponseError
from omniflow.validation_scope import ValidationEvidence, plan_content_scope
from omniflow.validators.content import ContentEvidenceError, run_content_validation
from omniflow.validators.model import run_model_validation

CHANGE = {"type": "field_renamed", "previous_field": "orders.old", "field": "orders.new"}


def qualified(changes=None, **kwargs):
    return plan_content_scope(
        {"changes": [CHANGE] if changes is None else changes},
        enabled=True, coverage_complete=True, drafts_verified_absent=True, **kwargs,
    )


def document(*, messages=("Existing issue",), queries=True):
    return {
        "identifier": "document", "document_id": "physical",
        "dashboard_filter_issues": [],
        **({"queries_and_issues": [{"query_id_map_key": "query", "issues": list(messages)}]} if queries else {}),
    }


def client_for(fetch=None):
    return SimpleNamespace(
        validate_content=Mock(side_effect=fetch or (lambda *a, **kw: {"content": [document()]})),
        list_content=Mock(return_value=[{"identifier": "document"}]),
    )


def validate(tmp_path, client, **kwargs):
    return run_content_validation(
        client=client, model_id="model", branch_id="branch", user_id="user",
        include_personal_folders=False, labels=[], fail_on_new_only=True,
        history_in=tmp_path / "history.json", history_out=tmp_path / "history.json",
        report_out=tmp_path / "report.json", **kwargs,
    )


def test_targeting_requires_separate_semantic_and_native_draft_qualification():
    diff = {"changes": [CHANGE]}
    assert plan_content_scope(diff).reason == "targeted_disabled"
    assert plan_content_scope(diff, enabled=True).reason == "semantic_coverage_unverified"
    assert plan_content_scope(diff, enabled=True, coverage_complete=True).reason == "draft_coverage_unverified"
    assert qualified().mode == "targeted"


def test_before_after_and_affected_references_deduplicate_deterministically():
    plan = qualified([CHANGE, CHANGE], affected_references=[
        {"type": "FIELD", "name": "orders.old"}, {"type": "VIEW", "name": "customers"},
    ])
    assert plan.selectors == (("FIELD", "orders.new"), ("FIELD", "orders.old"), ("VIEW", "customers"))


@pytest.mark.parametrize("change", [
    {"type": "relationship_modified", "affected_views": ["orders"]},
    {"type": "governance_changed", "scope": "field", "field": "orders.id"},
    {"type": "unknown_future_change"},
    {"type": "file_renamed", "entity_type": "view", "name": "orders"},
])
def test_unproven_global_relationship_and_unknown_impacts_fall_back(change):
    assert qualified([change]).reason == "unsupported_semantic_change"


@pytest.mark.parametrize("changes,reason", [
    ([], "empty_semantic_scope"),
    ([{"type": "field_renamed", "field": "orders.id"}], "semantic_coverage_unverified"),
    ([{"type": "field_deleted", "field": "orders\n.id"}], "semantic_coverage_unverified"),
    ([{"type": "view_deleted", "name": f"view{index}"} for index in range(9)], "targeted_request_limit"),
])
def test_missing_old_identity_empty_and_excessive_scope_fall_back(changes, reason):
    assert qualified(changes).reason == reason


@pytest.mark.parametrize("new", [False, True])
def test_qualified_scope_is_symmetric_baseline_first_deduped_and_not_history(tmp_path, new):
    def fetch(*args, branch_id=None, **kwargs):
        messages = ("Existing issue", "New issue") if branch_id and new else ("Existing issue",)
        return {"content": [document(messages=messages)]}
    client = client_for(fetch)
    report, code = validate(tmp_path, client, scope_plan=qualified(), force_full_validation=True)
    assert code == int(new)
    assert report["existing_issues"] == 1 and report["new_issues"] == int(new)
    assert report["content_scope_mode"] == "targeted" and report["content_selector_count"] == 2
    calls = [call.kwargs for call in client.validate_content.call_args_list]
    assert [(call["branch_id"], call["find"]) for call in calls] == [
        (None, "orders.new"), (None, "orders.old"), ("branch", "orders.new"), ("branch", "orders.old"),
    ]
    assert all(call["force_full_validation"] is True for call in calls)
    assert not (tmp_path / "history.json").exists()


@pytest.mark.parametrize("problem,reason", [
    ("empty", "targeted_inventory_unverified"),
    ("missing_queries", "targeted_inventory_unverified"),
    ("query_disappears", "targeted_inventory_changed"),
    ("document_disappears", "targeted_inventory_unverified"),
    ("overlap", "targeted_overlap_inconsistent"),
    ("legacy", "targeted_inventory_unverified"),
    ("partial", "targeted_inventory_unverified"),
])
def test_unqualified_targeted_results_reacquire_broad_pair_without_waiving_breaks(tmp_path, problem, reason):
    def fetch(*args, branch_id=None, find=None, **kwargs):
        if find:
            if problem == "empty" or (problem == "document_disappears" and branch_id):
                return {"content": []}
            if problem == "legacy":
                return {"issues": []}
            doc = document()
            if problem == "missing_queries":
                doc.pop("queries_and_issues")
            elif problem == "query_disappears" and branch_id:
                doc["queries_and_issues"] = []
            elif problem == "overlap" and find == "orders.old":
                doc["queries_and_issues"][0]["issues"].append("Another issue")
            return {"content": [doc], **({"partial": True} if problem == "partial" else {})}
        messages = ("Existing issue", "Actual new break") if branch_id else ("Existing issue",)
        return {"content": [document(messages=messages)]}
    client = client_for(fetch)
    report, code = validate(tmp_path, client, scope_plan=qualified())
    assert code == 1 and report["new_issues"] == 1 and report["resolved_issues"] == 0
    assert report["content_scope_mode"] == "broad" and report["content_scope_reason"] == reason
    assert report["content_selector_count"] == 0
    last = [call.kwargs for call in client.validate_content.call_args_list[-2:]]
    assert [call["branch_id"] for call in last] == [None, "branch"]
    assert all("find" not in call for call in last)


def test_duplicate_identity_within_single_search_fails_closed_instead_of_deduping(tmp_path):
    client = client_for(lambda *a, **kw: {"content": [document(), document()]})
    with pytest.raises(ContentEvidenceError) as raised:
        validate(tmp_path, client, scope_plan=qualified(), force_full_validation=True)
    assert raised.value.reason_code == "ambiguous_document_identity"
    assert raised.value.validation_scope == "base" and raised.value.requested_validation_depth == "full"
    assert client.validate_content.call_count == 2
    assert not (tmp_path / "history.json").exists()


@pytest.mark.parametrize("error", [OmniAPIError("Synthetic timeout"), OmniResponseError("invalid_json_response")])
def test_targeted_service_failures_are_not_downgraded_or_retried_broad(tmp_path, error):
    client = client_for()
    client.validate_content.side_effect = error
    with pytest.raises(type(error)):
        validate(tmp_path, client, scope_plan=qualified(), force_full_validation=True)
    assert client.validate_content.call_count == 1
    assert not (tmp_path / "history.json").exists()


def test_malformed_targeted_issues_fail_before_scope_fallback(tmp_path):
    doc = document()
    doc["queries_and_issues"][0]["issues"] = [{"message": "Readable", "errors": ["Hidden"]}]
    client = client_for(lambda *a, **kw: {"content": [doc]})
    with pytest.raises(ContentEvidenceError, match="unreadable"):
        validate(tmp_path, client, scope_plan=qualified())
    assert client.validate_content.call_count == 1


def test_run_evidence_is_copy_isolated_scope_keyed_refreshable_and_never_global():
    snapshots = ValidationEvidence()
    client = SimpleNamespace(validate_content=Mock(return_value={"content": []}))
    result = snapshots.fetch(client, "validate_content", "model", branch_id=None)
    result["content"].append("mutation")
    assert snapshots.fetch(client, "validate_content", "model", branch_id=None) == {"content": []}
    assert client.validate_content.call_count == 1
    for params in (
        {"branch_id": "branch"}, {"branch_id": None, "force_full_validation": True},
        {"branch_id": None, "find": "orders.id", "find_type": "FIELD"},
        {"branch_id": None, "user_id": "different-user"},
    ):
        snapshots.fetch(client, "validate_content", "model", **params)
    snapshots.fetch(client, "validate_content", "model", branch_id=None, refresh=True)
    ValidationEvidence().fetch(client, "validate_content", "model", branch_id=None)
    assert client.validate_content.call_count == 7
    second = SimpleNamespace(validate_content=Mock(return_value={"content": []}))
    snapshots.fetch(second, "validate_content", "model", branch_id=None)
    second.validate_content.assert_called_once()


def test_failed_requests_are_never_reused_as_empty_evidence():
    snapshots = ValidationEvidence()
    client = SimpleNamespace(validate_model=Mock(side_effect=[OmniAPIError("Synthetic failure"), []]))
    with pytest.raises(OmniAPIError):
        snapshots.fetch(client, "validate_model", "model", branch_id=None)
    assert snapshots.fetch(client, "validate_model", "model", branch_id=None) == []
    assert client.validate_model.call_count == 2


@pytest.mark.parametrize("changed", [("https://other.example", "a" * 64), ("https://omni.example", "b" * 64)])
def test_cached_evidence_rejects_changed_origin_or_credential_and_cannot_be_revived(changed):
    original = ("https://omni.example", "a" * 64)
    client = SimpleNamespace(binding=original, validate_model=Mock(return_value=[]))
    client.validation_evidence_binding = lambda: client.binding
    snapshots = ValidationEvidence()
    assert snapshots.fetch(client, "validate_model", "model") == []
    client.binding = changed
    with pytest.raises(OmniAPIError) as caught:
        snapshots.fetch(client, "validate_model", "model")
    assert caught.value.reason_code == "validation_identity_changed"
    client.binding = original
    with pytest.raises(OmniAPIError):
        snapshots.fetch(client, "validate_model", "model", refresh=True)
    assert client.validate_model.call_count == 1
    assert "a" * 64 not in str(caught.value) and "https://" not in str(caught.value)


@pytest.mark.parametrize("request_fails", [False, True])
def test_identity_change_during_fetch_never_caches_response(request_fails):
    client = SimpleNamespace(binding=("https://omni.example", "a" * 64))
    client.validation_evidence_binding = lambda: client.binding
    def fetch(*args, **kwargs):
        client.binding = ("https://omni.example", "b" * 64)
        if request_fails:
            raise OmniAPIError("Unrelated transport failure")
        return []
    client.validate_model = fetch
    snapshots = ValidationEvidence()
    with pytest.raises(OmniAPIError) as caught:
        snapshots.fetch(client, "validate_model", "model")
    assert caught.value.reason_code == "validation_identity_changed"
    assert not snapshots._snapshots


@pytest.mark.parametrize("binding", [None, ("https://omni.example", "not-a-digest"), ("", "a" * 64)])
def test_present_but_invalid_binding_contract_is_not_treated_as_legacy_client(binding):
    client = SimpleNamespace(validation_evidence_binding=lambda: binding, validate_model=Mock(return_value=[]))
    with pytest.raises(OmniAPIError) as caught:
        ValidationEvidence().fetch(client, "validate_model", "model")
    assert caught.value.reason_code == "validation_identity_changed"
    client.validate_model.assert_not_called()


def test_model_and_content_reuse_same_run_evidence_without_sharing_between_runs(tmp_path):
    snapshots = ValidationEvidence()
    client = client_for()
    client.validate_model = Mock(return_value=[])
    for _ in range(2):
        validate(tmp_path, client, evidence=snapshots)
        run_model_validation(client=client, model_id="model", branch_id="branch",
                             fail_on_new_only=True, evidence=snapshots)
    assert client.validate_content.call_count == client.validate_model.call_count == 2
    assert [call.kwargs["branch_id"] for call in client.validate_model.call_args_list] == [None, "branch"]
    validate(tmp_path, client)
    assert client.validate_content.call_count == 4


def test_targeted_overlap_reordering_does_not_duplicate_existing_occurrences(tmp_path):
    doc = document(messages=("First", "Second", "First"))
    def fetch(*args, find=None, **kwargs):
        value = deepcopy(doc)
        if find == "orders.old":
            value["queries_and_issues"][0]["issues"].reverse()
        return {"content": [value]}
    report, code = validate(tmp_path, client_for(fetch), scope_plan=qualified())
    assert code == 0 and report["existing_issues"] == 3
