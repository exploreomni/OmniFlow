"""Target only required drafts; missing evidence must never waive a regression."""
import copy
import json
from unittest.mock import Mock

import pytest
from test_content_draft_identity import (
    BRANCH,
    DRAFT,
    MESSAGE,
    PUBLISHED,
    client_for,
    document,
    draft,
    validate,
)

from omniflow.exceptions import ConfigError, OmniAPIError, OmniAuthError, OmniResponseError
from omniflow.validators.content import ContentEvidenceError


@pytest.mark.parametrize("extra", [0, 1])
def test_large_debt_and_unrelated_denied_documents_do_not_affect_comparison(tmp_path, extra):
    filters = [{"filter_name": f"filter-{i}", "issues": [MESSAGE]} for i in range(150)]
    current = copy.deepcopy(filters)
    if extra:
        current[0]["issues"].append(MESSAGE)
    client = client_for(
        current=[document(DRAFT, filters=current)], baseline=[document(PUBLISHED, filters=filters)],
        records=[{"identifier": f"unrelated-{i}"} for i in range(500)] + [{"identifier": PUBLISHED}],
    )

    def selected_only(parent):
        if parent != PUBLISHED:
            raise OmniAuthError("Do not read unrelated private content")
        return [draft()]

    client.list_document_drafts.side_effect = selected_only
    report, code = validate(tmp_path, client)
    assert code == extra
    assert report["existing_issues"] == 150
    assert report["new_issues"] == extra
    assert report["comparison_status"] == "complete"
    assert client.get_document_identity.call_count == client.list_document_drafts.call_count == 2
    assert all(call.args == (DRAFT,) for call in client.get_document_identity.call_args_list)
    assert all(call.args == (PUBLISHED,) for call in client.list_document_drafts.call_args_list)


def test_unrelated_stale_archived_and_changing_drafts_do_not_poison_selected_proof(tmp_path):
    client = client_for()
    other = draft(identifier="unrelated", branch={"id": "another-branch"}, draftOutOfDate=True)
    archived = draft(identifier="archived", status="archived")
    client.list_document_drafts.side_effect = [
        [draft(), other, archived], [archived, draft(), {**other, "updatedAt": "later"}],
    ]
    report, code = validate(tmp_path, client)
    assert code == 0 and report["existing_issues"] == 1


@pytest.mark.parametrize("failure,expected,transport,status", [
    (OmniAuthError("PRIVATE", reason_code="omni_forbidden", diagnostic_context={"http_status": 403}),
     "document_identity_permission_denied", "omni_forbidden", 403),
    (OmniAPIError("PRIVATE", reason_code="omni_http_error", diagnostic_context={"http_status": 404}),
     "document_identity_not_found", "omni_http_error", 404),
    (OmniAPIError("PRIVATE", reason_code="omni_timeout"),
     "document_identity_transport_unavailable", "omni_timeout", None),
    (OmniAPIError("PRIVATE", reason_code="omni_tls_error"),
     "document_identity_transport_unavailable", "omni_tls_error", None),
    (OmniAPIError("PRIVATE", reason_code="omni_rate_limited", diagnostic_context={"http_status": 429}),
     "document_identity_transport_unavailable", "omni_rate_limited", 429),
    (OmniResponseError("invalid_json_response"), "document_identity_invalid_response", "invalid_json_response", None),
    (OmniAPIError("PRIVATE", reason_code="omni_response_limit_exceeded"),
     "document_identity_invalid_response", "omni_response_limit_exceeded", None),
    (ConfigError("PRIVATE", reason_code="document_identity_scope_unsupported"),
     "document_identity_scope_unsupported", None, None),
])
@pytest.mark.parametrize("endpoint", ["get_document_identity", "list_document_drafts"])
def test_required_identity_failure_retains_safe_cause_without_becoming_content_defect(
    tmp_path, failure, expected, transport, status, endpoint,
):
    client = client_for()
    failure.diagnostic_context.update({
        "attempt_count": 1, "max_attempts": 1, "body": "PRIVATE", "url": "PRIVATE",
    })
    getattr(client, endpoint).side_effect = failure
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)
    finding = caught.value.report_issue()
    assert finding["reason_code"] == expected
    assert finding.get("http_status") == status
    assert finding.get("transport_reason") == transport
    assert finding["document_reference"] == "document-1"
    assert finding["api_operation"] == ("document_state" if endpoint == "get_document_identity" else "document_drafts")
    assert finding["attempt_count"] == finding["max_attempts"] == 1
    assert finding["blocking"] is True and finding["regression_status"] == "unknown"
    assert "PRIVATE" not in json.dumps(finding)
    assert not (tmp_path / "private/history.json").exists()


@pytest.mark.parametrize("change", ["reparent", "published", "archived", "branch", "version"])
def test_selected_identity_races_invalidate_comparison(tmp_path, change):
    client = client_for()
    if change in {"reparent", "published"}:
        client.get_document_identity.side_effect = [
            {"identifier": DRAFT, "publishedIdentifier": PUBLISHED},
            {"identifier": DRAFT, "publishedIdentifier": "new-parent"} if change == "reparent" else
            OmniAPIError("PRIVATE", reason_code="document_identity_invalid_response"),
        ]
    else:
        after = {
            "archived": draft(status="archived"),
            "branch": draft(branch={"id": "changed-branch"}),
            "version": draft(updatedAt="new-version"),
        }[change]
        client.list_document_drafts.side_effect = [[draft()], [after]]
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)
    assert caught.value.reason_code in {"document_identity_changed", "document_identity_invalid_response"}
    assert not (tmp_path / "private/history.json").exists()


def test_actor_scope_is_forwarded_without_substitution(tmp_path):
    client = client_for()
    validate(tmp_path, client)
    assert client.get_document_identity.call_args.kwargs == {
        "user_id": "synthetic-user", "include_personal_folders": False,
    }


def test_identity_lookup_failure_retains_the_actual_operation(tmp_path):
    client = client_for()
    client.get_document_identity.side_effect = OmniAuthError(
        "PRIVATE", reason_code="omni_unauthorized", diagnostic_context={
            "api_operation": "identity_lookup", "http_status": 401,
        },
    )
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)
    assert caught.value.report_issue()["api_operation"] == "identity_lookup"


@pytest.mark.parametrize("identity", [None, {}, {"identifier": DRAFT},
    {"identifier": DRAFT, "publishedIdentifier": DRAFT},
    {"identifier": "wrong-draft", "publishedIdentifier": PUBLISHED}])
def test_missing_parent_is_never_new_published_document_proof(tmp_path, identity):
    client = client_for()
    client.get_document_identity = Mock(return_value=identity)
    with pytest.raises(ContentEvidenceError, match="identity response") as caught:
        validate(tmp_path, client)
    assert caught.value.reason_code == "document_identity_invalid_response"
    client.list_document_drafts.assert_not_called()


def test_new_parent_with_clean_baseline_is_still_a_new_finding(tmp_path):
    client = client_for(baseline=[], records=[])
    report, code = validate(tmp_path, client)
    assert code == 1 and report["new_issues"] == 1
    assert report["existing_issues"] == 0


def test_selected_conflicting_draft_cannot_hide_among_unrelated_rows(tmp_path):
    client = client_for(drafts=[
        draft(identifier="other", branch={"id": "unrelated"}),
        draft(), draft(identifier="conflicting", branch={"id": BRANCH}),
    ])
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)
    assert caught.value.reason_code == "ambiguous_document_identity"
