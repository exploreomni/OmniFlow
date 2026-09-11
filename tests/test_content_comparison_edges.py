import copy
import json
from types import SimpleNamespace

import pytest

from omniflow.exceptions import OmniAPIError
from omniflow.validators.content import (
    HISTORY_SCHEMA_VERSION,
    QUERY_IDENTITY_KEYS,
    extract_issues,
    normalize_issues,
    run_content_validation,
)


def payload(*queries, filters=(), document="document-1"):
    return {"content": [{
        "document_id": document, "identifier": document, "name": "Private dashboard",
        "queries_and_issues": list(queries), "dashboard_filter_issues": list(filters),
    }]}


def query(key="query_presentation_id", value="query-1", *, count=1):
    return {key: value, "query_name": "Private query", "issues": ["Broken field"] * count}


def validate(tmp_path, current, base=None, **overrides):
    client = SimpleNamespace(
        validate_content=lambda *args, branch_id=None, **kwargs: current if branch_id or base is None else base,
        list_content=lambda **kwargs: [],
    )
    kwargs = {
        "client": client, "model_id": "model", "branch_id": "branch", "user_id": None,
        "include_personal_folders": False, "labels": [], "fail_on_new_only": True,
        "history_in": tmp_path / "history.json", "history_out": tmp_path / "history.json",
        "report_out": tmp_path / "report.json",
    }
    kwargs.update(overrides)
    return run_content_validation(**kwargs)


@pytest.mark.parametrize("key", QUERY_IDENTITY_KEYS)
@pytest.mark.parametrize("replacement", [False, True])
def test_distinct_query_identities_cannot_be_preexisting(tmp_path, key, replacement):
    first, second = query(key, "first"), query(key, "second")
    current = payload(second) if replacement else payload(first, second)
    report, code = validate(tmp_path, current, payload(first))
    assert code == 1
    assert report["new_issues"] == 1
    assert report["existing_issues"] == int(not replacement)
    assert report["resolved_issues"] == int(replacement)
    assert report["new_issue_samples"][0][key] == "second"


@pytest.mark.parametrize("ambiguity", ["missing_query", "duplicate_query", "missing_document"])
def test_ambiguous_identity_never_suppresses_current_defects(tmp_path, ambiguity):
    current = payload(query())
    if ambiguity == "missing_query":
        current["content"][0]["queries_and_issues"][0].pop("query_presentation_id")
    elif ambiguity == "duplicate_query":
        current = payload(query(), query())
    else:
        for key in ("document_id", "identifier"):
            current["content"][0].pop(key)
    report, code = validate(tmp_path, current, copy.deepcopy(current))
    assert code == 1
    assert report["existing_issues"] == report["resolved_issues"] == 0
    assert report["new_issues"] == len(current["content"][0]["queries_and_issues"])
    assert report["comparison_ambiguous_issues"] > 0


def test_query_identity_is_document_scoped_and_auxiliary_metadata_is_not_identity(tmp_path):
    first = payload({**query(), "query_id_map_key": "1"})
    report, code = validate(tmp_path, payload(query(), document="other-document"), first)
    assert code == 1 and report["new_issues"] == report["resolved_issues"] == 1
    report, code = validate(tmp_path, payload({**query(), "query_name": "Renamed"}), first)
    assert code == 0 and report["existing_issues"] == 1


@pytest.mark.parametrize("kind", ["filter", "query"])
@pytest.mark.parametrize("before,after,new,resolved", [(1, 2, 1, 0), (2, 1, 0, 1), (2, 2, 0, 0)])
def test_issue_occurrences_are_matched_one_to_one(tmp_path, kind, before, after, new, resolved):
    def repeated(count):
        return payload(filters=["Broken filter"] * count) if kind == "filter" else payload(query(count=count))
    report, code = validate(tmp_path, repeated(after), repeated(before))
    assert code == int(new > 0)
    assert report["new_issues"] == new
    assert report["resolved_issues"] == resolved
    assert report["existing_issues"] == min(before, after)


def history(current):
    return {
        "schema_version": HISTORY_SCHEMA_VERSION, "model_id": "model", "branch_id": None,
        "user_id": "user-1", "include_personal_folders": False, "labels": [],
        "issues": normalize_issues(extract_issues(current)),
    }


@pytest.mark.parametrize("change,status", [
    ({"model_id": "other"}, "invalidated_context"),
    ({"branch_id": "other"}, "invalidated_context"),
    ({"user_id": "other"}, "invalidated_context"),
    ({"include_personal_folders": True}, "invalidated_context"),
    ({"labels": ["other"]}, "invalidated_context"),
    ({"schema_version": 0}, "invalidated_version"),
    ({"legacy": True}, "invalidated_legacy"),
    ({}, "accepted"),
])
def test_history_must_match_version_and_full_context(tmp_path, change, status):
    current = payload(query())
    previous = history(current)
    if change == {"legacy": True}:
        previous.pop("schema_version")
    else:
        previous.update(change)
    (tmp_path / "history.json").write_text(json.dumps(previous))
    report, code = validate(tmp_path, current, branch_id=None, user_id="user-1")
    accepted = status == "accepted"
    assert code == int(not accepted)
    assert report["history_status"] == status
    assert report["new_issues"] == int(not accepted)
    assert report["existing_issues"] == int(accepted)
    saved = json.loads((tmp_path / "history.json").read_text())
    assert saved["schema_version"] == HISTORY_SCHEMA_VERSION
    assert saved["user_id"] == "user-1" and saved["branch_id"] is None


@pytest.mark.parametrize("branch_id,status,code", [
    (None, "invalidated_unscoped_user", 1),
    ("branch", "not_used_live_base", 0),
])
def test_default_user_history_cannot_suppress_findings_but_live_base_still_can(tmp_path, branch_id, status, code):
    current = payload(query())
    previous = history(current)
    previous["user_id"] = None
    (tmp_path / "history.json").write_text(json.dumps(previous))
    report, actual_code = validate(tmp_path, current, copy.deepcopy(current), branch_id=branch_id)
    assert actual_code == code
    assert report["history_status"] == status
    assert report["new_issues"] == code
    assert report["existing_issues"] == 1 - code


@pytest.mark.parametrize("invalid", ["{private malformed", "null", "[]", "42", "invalid_context", "invalid_issue"])
def test_corrupt_history_is_classified_without_raw_details(tmp_path, invalid):
    previous = history(payload(query()))
    if invalid == "invalid_context":
        previous["include_personal_folders"] = "false"
    elif invalid == "invalid_issue":
        previous["issues"][0]["id"] = "forged"
    raw = json.dumps(previous) if invalid.startswith("invalid_") else invalid
    (tmp_path / "history.json").write_text(raw)
    with pytest.raises(OmniAPIError, match="history") as caught:
        validate(tmp_path, payload(query()), branch_id=None)
    assert caught.value.exit_code == 4
    assert "private malformed" not in str(caught.value)
    assert not (tmp_path / "report.json").exists()
    assert (tmp_path / "history.json").read_text() == raw


def test_history_labels_are_order_independent_and_current_privacy_applies(tmp_path):
    current = payload(query())
    previous = history(current)
    previous["labels"] = ["B", "A"]
    (tmp_path / "history.json").write_text(json.dumps(previous))
    client = SimpleNamespace(validate_content=lambda *args, **kwargs: {"content": []}, list_content=lambda **kwargs: [])
    report, code = validate(
        tmp_path, {"content": []}, client=client, branch_id=None, user_id="user-1",
        labels=["A", "B"], redact_document_names=True,
    )
    assert code == 0 and report["history_status"] == "accepted"
    assert report["resolved_issues"] == 1
    assert "Private" not in json.dumps(report)


def test_label_catalog_needs_matching_identifier_and_valid_empty_is_allowed(tmp_path):
    client = SimpleNamespace(
        validate_content=lambda *args, **kwargs: payload(filters=["Broken filter"]),
        list_content=lambda **kwargs: [{"id": "document-1"}],
    )
    with pytest.raises(OmniAPIError, match="identity required for label filtering"):
        validate(tmp_path, None, client=client, labels=["Verified"])
    client.list_content = lambda **kwargs: []
    report, code = validate(tmp_path, None, client=client, labels=["Verified"])
    assert code == 0 and report["total_issues"] == 0
