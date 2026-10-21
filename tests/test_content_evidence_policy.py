import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow.validators.content import (
    HISTORY_SCHEMA_VERSION,
    ContentEvidenceError,
    extract_issues,
    run_content_validation,
)


def payload(*issues):
    return {"content": [{
        "document_id": "document-1", "identifier": "document-1",
        "dashboard_filter_issues": list(issues),
    }]}


def validate(tmp_path, current, baseline, *, new_only=True):
    fetch = Mock(side_effect=lambda *args, branch_id=None, **kwargs: current if branch_id else baseline)
    client = SimpleNamespace(
        validate_content=fetch,
        # Both document identities used by these tests are published catalog records.
        list_content=lambda **kwargs: [{"identifier": "document-1"}, {"identifier": "other-document"}],
    )
    result = run_content_validation(
        client=client, model_id="model", branch_id="branch", user_id=None,
        include_personal_folders=False, labels=[], fail_on_new_only=new_only,
        history_in=tmp_path / "missing.json", history_out=tmp_path / "history.json",
        report_out=tmp_path / "report.json",
    )
    return (*result, fetch)


@pytest.mark.parametrize("container,reason", [
    (None, "invalid_issue_container"),
    ({"private_detail": "never publish"}, "invalid_issue_container"),
    ([None], "invalid_issue_entry"),
    ([False], "invalid_issue_entry"),
    ([42], "invalid_issue_entry"),
    ([{}], "missing_issue_message"),
    ([{"private_detail": "never publish"}], "missing_issue_message"),
    ([{"message": None}], "invalid_issue_message"),
    ([{"message": {"private_detail": "never publish"}}], "invalid_issue_message"),
    (["  "], "invalid_issue_message"),
    ([[{"private_detail": "never publish"}]], "unsupported_issue_shape"),
])
def test_evidence_reason_is_structural_and_never_serializes_bad_values(container, reason):
    current = payload()
    current["content"][0]["dashboard_filter_issues"] = container
    with pytest.raises(ContentEvidenceError) as caught:
        extract_issues(current)
    error = caught.value
    finding = error.report_issue()
    assert error.exit_code == 4
    assert finding["reason_code"] == reason
    assert reason in str(error)
    assert finding["comparison_status"] == "unavailable"
    assert finding["content_health"] == finding["regression_status"] == "unknown"
    assert "private_detail" not in json.dumps(finding)
    assert "never publish" not in json.dumps(finding)


@pytest.mark.parametrize("untrusted", ["private detail never publish", None, {"private": "never publish"}])
def test_unknown_reason_cannot_be_used_as_a_free_text_output_channel(untrusted):
    error = ContentEvidenceError(reason_code=untrusted)
    assert error.reason_code == "unsupported_issue_shape"
    assert "never publish" not in str(error)
    assert "private" not in json.dumps(error.report_issue())


def test_existing_defects_pass_regression_gate_without_claiming_content_is_healthy(tmp_path):
    current = payload("Existing missing field")
    report, code, fetch = validate(tmp_path, current, copy.deepcopy(current))
    assert code == 0
    assert report["comparison_mode"] == "new_only"
    assert report["comparison_source"] == "base_model"
    assert report["comparison_status"] == "complete"
    assert report["baseline_issues"] == report["current_issues"] == report["existing_issues"] == 1
    assert report["new_issues"] == 0
    assert report["content_health"] == "issues_in_scope"
    assert report["regression_status"] == "no_new_issues_detected"
    assert [call.kwargs["branch_id"] for call in fetch.call_args_list] == [None, "branch"]
    assert json.loads((tmp_path / "report.json").read_text())["content_health"] == "issues_in_scope"


@pytest.mark.parametrize("current,expected_new", [
    (payload("Existing missing field", "New missing field"), 1),
    (payload("Existing missing field", "Existing missing field"), 1),
    (payload("Changed validation failure"), 1),
])
def test_new_changed_or_additional_occurrence_blocks_regression_gate(tmp_path, current, expected_new):
    report, code, _ = validate(tmp_path, current, payload("Existing missing field"))
    assert code == 1
    assert report["new_issues"] == expected_new
    assert report["regression_status"] == "new_issues_detected"
    assert report["comparison_status"] == "complete"


def test_all_issues_policy_still_blocks_and_missing_history_is_not_a_clean_baseline(tmp_path):
    current = payload("Existing missing field")
    report, code, fetch = validate(tmp_path, current, copy.deepcopy(current), new_only=False)
    assert code == 1
    assert report["comparison_mode"] == "all_issues"
    assert report["comparison_status"] == "baseline_unavailable"
    assert report["baseline_issues"] is None
    assert report["current_issues"] == 1
    assert report["regression_status"] == "unknown"
    assert fetch.call_count == 1


def test_ambiguous_identity_is_blocking_and_never_claims_verified_regression(tmp_path):
    current = payload("Missing identity")
    current["content"][0].pop("document_id")
    current["content"][0].pop("identifier")
    report, code, _ = validate(tmp_path, current, copy.deepcopy(current))
    assert code == 4
    assert report["comparison_status"] == "ambiguous"
    assert report["regression_status"] == "unknown"
    assert report["existing_issues"] == 0


@pytest.mark.parametrize("scope", ["branch", "base", "both"])
def test_malformed_branch_or_base_remains_unavailable_not_preexisting(tmp_path, scope):
    malformed = payload({"private_detail": "never publish"})
    current = malformed if scope in {"branch", "both"} else payload()
    baseline = malformed if scope in {"base", "both"} else payload()
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, current, baseline)
    error = caught.value
    finding = error.report_issue()
    assert error.exit_code == 4
    assert finding["validation_scope"] == ("branch" if scope == "branch" else "base")
    assert finding["comparison_mode"] == "new_only"
    assert finding["comparison_status"] == "unavailable"
    assert finding["reason_code"] == "missing_issue_message"
    assert finding["regression_status"] == finding["content_health"] == "unknown"
    assert not (tmp_path / "history.json").exists()
    assert not (tmp_path / "report.json").exists()


def test_valid_empty_lists_are_healthy_in_scope_not_unreadable(tmp_path):
    report, code, _ = validate(tmp_path, payload(), payload())
    assert code == 0
    assert report["baseline_issues"] == report["current_issues"] == 0
    assert report["content_health"] == "no_issues_in_scope"
    assert report["regression_status"] == "no_new_issues_detected"


def group(name="Filter A", *messages):
    return {"filter_name": name, "issues": list(messages)}


def test_observed_grouped_response_is_readable_and_existing_errors_follow_policy(tmp_path):
    current = payload(group("Exact Filter Name", "Broken field"))
    report, code, _ = validate(tmp_path, current, payload())
    assert code == 1
    assert report["new_issues"] == 1
    assert report["issues"][0]["filter_name"] == "Exact Filter Name"
    assert report["issues"][0]["raw"]["filter_name"] == "Exact Filter Name"
    assert report["issues"][0]["message"] == "Broken field"
    report, code, _ = validate(tmp_path, current, copy.deepcopy(current))
    assert code == 0
    assert report["existing_issues"] == 1 and report["new_issues"] == 0
    assert report["content_health"] == "issues_in_scope"
    assert report["regression_status"] == "no_new_issues_detected"
    assert validate(tmp_path, current, current, new_only=False)[1] == 1
    # Empty nested lists are explicit absence of issues, unlike null/missing.
    empty, code, _ = validate(tmp_path, payload(group("Filter A")), payload())
    assert code == 0 and empty["total_issues"] == 0


@pytest.mark.parametrize("change", ["replace_filter", "add_filter", "replace_document", "change_message", "additional_occurrence"])
def test_grouped_filter_identity_and_occurrences_cannot_mask_new_defects(tmp_path, change):
    baseline = payload(group("Filter A", "Broken field"))
    current = copy.deepcopy(baseline)
    if change == "replace_filter":
        current = payload(group("Filter B", "Broken field"))
    elif change == "add_filter":
        current["content"][0]["dashboard_filter_issues"].append(group("Filter B", "Broken field"))
    elif change == "replace_document":
        current["content"][0].update(document_id="other-document", identifier="other-document")
    elif change == "change_message":
        current = payload(group("Filter A", "Different broken field"))
    else:
        current = payload(group("Filter A", "Broken field", "Broken field"))
    report, code, _ = validate(tmp_path, current, baseline)
    assert code == 1 and report["new_issues"] == 1
    retained = change in {"add_filter", "additional_occurrence"}
    assert report["existing_issues"] == int(retained)
    assert report["resolved_issues"] == int(not retained)


def test_duplicate_filter_group_names_are_ambiguous_but_leaf_order_is_not(tmp_path):
    ambiguous = payload(group("Filter A", "Broken field"), group("Filter A", "Other field"))
    report, code, _ = validate(tmp_path, ambiguous, copy.deepcopy(ambiguous))
    assert code == 4 and report["comparison_status"] == "ambiguous"
    assert report["new_issues"] == 2 and report["existing_issues"] == 0
    before = payload(group("Filter A", "First field", "Second field"))
    after = payload(group("Filter A", "Second field", "First field"))
    report, code, _ = validate(tmp_path, after, before)
    assert code == 0 and report["existing_issues"] == 2


@pytest.mark.parametrize("invalid,reason", [
    ({"filter_name": None, "issues": ["Broken"]}, "invalid_filter_identity"),
    ({"filter_name": " ", "issues": ["Broken"]}, "invalid_filter_identity"),
    ({"issues": ["Broken"]}, "unsupported_issue_shape"),
    ({"filter_name": "Filter A"}, "missing_issue_message"),
    ({"filter_name": "Filter A", "issues": None}, "invalid_issue_container"),
    ({"filter_name": "Filter A", "issues": "Broken"}, "invalid_issue_container"),
    ({"filter_name": "Filter A", "issues": [None]}, "invalid_issue_entry"),
    ({"filter_name": "Filter A", "issues": [""]}, "invalid_issue_message"),
    ({"filter_name": "Filter A", "issues": [["Broken"]]}, "unsupported_issue_shape"),
    ({"filter_name": "Filter A", "issues": [{"message": "Broken"}]}, "unsupported_issue_shape"),
    ({"filter_name": "Filter A", "issues": ["Broken"], "message": "Other evidence"}, "unsupported_issue_shape"),
    ({"filter_name": "Filter A", "issues": [], "errors": ["Hidden issue"]}, "unsupported_issue_shape"),
])
def test_grouped_shape_rejects_missing_ambiguous_and_unknown_evidence(tmp_path, invalid, reason):
    malformed = payload(invalid)
    for current, baseline, scope in ((malformed, payload(), "branch"), (payload(), malformed, "base")):
        with pytest.raises(ContentEvidenceError) as caught:
            validate(tmp_path, current, baseline)
        assert caught.value.exit_code == 4
        assert caught.value.reason_code == reason
        assert caught.value.validation_scope == scope
        assert "Filter A" not in str(caught.value)


@pytest.mark.parametrize("legacy_issue", ["Legacy broken filter", {"message": "Legacy broken filter"}])
def test_old_physical_document_fingerprint_history_cannot_suppress_findings(tmp_path, legacy_issue):
    # Build the old stable fingerprint independently of the new normalizer.
    raw = {"document_id": "document-1", "document_identifier": "document-1",
           "issue_type": "dashboard_filter", "message": "Legacy broken filter"}
    fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    history_path = tmp_path / "history.json"
    history_path.write_text(json.dumps({
        "schema_version": 1, "model_id": "model", "branch_id": None, "user_id": "legacy-user",
        "include_personal_folders": False, "labels": [],
        "issues": [{"id": fingerprint, "raw": raw, "summary": "Legacy broken filter", "comparison_eligible": True}],
    }))
    client = SimpleNamespace(validate_content=lambda *args, **kwargs: payload(legacy_issue), list_content=lambda **kwargs: [])
    report, code = run_content_validation(
        client=client, model_id="model", branch_id=None, user_id="legacy-user",
        include_personal_folders=False, labels=[], fail_on_new_only=True,
        history_in=history_path, history_out=history_path, report_out=tmp_path / "report.json",
    )
    assert code == 4 and report["history_status"] == "invalidated_version"
    assert report["existing_issues"] == 0 and report["new_issues"] == 1
    assert report["regression_status"] == "unknown"
    assert report["issues"][0]["blocking"] is True
    # Ordinary documents retain physical identity; version invalidation still
    # applies because schema 2 can also contain verified draft aliases.
    assert report["issues"][0]["id"] == fingerprint
    assert "filter_name" not in report["issues"][0]["raw"]
    saved = json.loads(history_path.read_text())
    assert saved["schema_version"] == HISTORY_SCHEMA_VERSION == 4
    assert saved["issues"][0]["id"] == fingerprint
