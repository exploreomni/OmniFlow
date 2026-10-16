"""Documented active-draft endpoint shape and private metadata boundary."""

import copy
import json
from unittest.mock import Mock

import pytest

from omniflow.exceptions import ConfigError, OmniAPIError, OmniAuthError, OmniResponseError, SecurityPolicyError
from omniflow.omni_client import OmniClient


def draft(**changes):
    return {
        "identifier": "synthetic-draft",
        "publishedIdentifier": "synthetic-published",
        "branch": {"id": "synthetic-branch"},
        "status": "active",
        "draftOutOfDate": False,
        "updatedAt": "2026-10-15T14:00:00.000Z",
        **changes,
    }


def client_for(payload):
    client = OmniClient(base_url="https://omni.example", api_key="synthetic-draft-test")
    client._request = Mock(return_value=payload)
    return client


@pytest.mark.parametrize("payload", [[], [draft()]])
def test_active_draft_request_is_a_single_get_without_archived_or_pagination(payload):
    client = client_for(payload)
    assert client.list_document_drafts("synthetic-published") == payload
    client._request.assert_called_once_with("GET", "/api/v1/documents/synthetic-published/drafts")


def test_metadata_allowlist_discards_actors_names_and_workbook_contents_without_mutation():
    marker = "SYNTHETIC_PRIVATE_ACTOR_AND_WORKBOOK"
    row = draft(
        branch={"id": "synthetic-branch", "name": marker, "unknown": marker},
        workbookModelId=marker, createdAt=marker,
        createdBy={"name": marker}, lastEditedBy={"name": marker},
        workbook={"queries": marker}, dashboard={"filters": marker}, unknown=marker,
    )
    original = copy.deepcopy(row)
    actual = client_for([row]).list_document_drafts("synthetic-published")
    assert actual == [draft()]
    assert marker not in json.dumps(actual)
    assert row == original
    actual[0]["branch"]["id"] = "changed-result"
    assert row["branch"]["id"] == "synthetic-branch"


def test_adapter_preserves_unattached_archived_stale_duplicate_and_mismatched_mapping_evidence():
    rows = [draft(branch=None), draft(status="archived", draftOutOfDate=True),
            draft(publishedIdentifier="other-published"), draft(), draft()]
    assert client_for(rows).list_document_drafts("synthetic-published") == rows


@pytest.mark.parametrize("payload", [None, {}, {"records": []}, {"drafts": []}, "private-invalid", [None], [12], [[]]])
def test_malformed_envelopes_and_records_fail_closed_without_echoing_values(payload):
    with pytest.raises(OmniAPIError) as caught:
        client_for(payload).list_document_drafts("synthetic-published")
    assert caught.value.exit_code == 4
    assert "private-invalid" not in str(caught.value)


@pytest.mark.parametrize("key", list(draft()))
def test_missing_mapping_metadata_is_not_defaulted(key):
    row = draft()
    del row[key]
    with pytest.raises(OmniAPIError, match="invalid metadata"):
        client_for([row]).list_document_drafts("synthetic-published")


@pytest.mark.parametrize("key,value", [
    ("identifier", None), ("identifier", ""), ("identifier", " "), ("identifier", 123),
    ("publishedIdentifier", []), ("publishedIdentifier", ""), ("publishedIdentifier", " "),
    ("branch", []), ("branch", "private-invalid"), ("branch", {}),
    ("branch", {"id": None}), ("branch", {"id": 1}), ("branch", {"id": " "}),
    ("status", None), ("status", []), ("status", {}), ("status", "ACTIVE"), ("status", "private-invalid"),
    ("draftOutOfDate", "false"), ("draftOutOfDate", 0), ("draftOutOfDate", None),
    ("updatedAt", None), ("updatedAt", 123), ("updatedAt", ""), ("updatedAt", " "),
])
def test_malformed_metadata_is_rejected_without_coercion(key, value):
    with pytest.raises(OmniAPIError, match="invalid metadata") as caught:
        client_for([draft(**{key: value})]).list_document_drafts("synthetic-published")
    assert "private-invalid" not in str(caught.value)


@pytest.mark.parametrize("identifier", ["../private", "x/private", "x?include=archived", "x#fragment",
                                        "x%2fprivate", "x\\private", "https://evil.example", " private ", "x\n"])
def test_unsafe_published_identifier_never_reaches_the_api(identifier):
    client = client_for([])
    with pytest.raises(SecurityPolicyError):
        client.list_document_drafts(identifier)
    client._request.assert_not_called()


@pytest.mark.parametrize("identifier", [None, 1, [], {}, ""])
def test_invalid_published_identifier_types_fail_before_request(identifier):
    client = client_for([])
    with pytest.raises(ConfigError):
        client.list_document_drafts(identifier)
    client._request.assert_not_called()


@pytest.mark.parametrize("error,expected_type,exit_code", [
    (OmniAPIError("private document/path"), OmniAPIError, 4),
    (OmniAuthError("private document/permission"), OmniAuthError, 3),
    (OmniResponseError("invalid_json_response"), OmniAPIError, 4),
])
def test_transport_failure_preserves_failure_class_without_document_identity(error, expected_type, exit_code):
    client = client_for([])
    client._request.side_effect = error
    with pytest.raises(expected_type) as caught:
        client.list_document_drafts("private-document-id")
    assert caught.value.exit_code == exit_code
    assert "private" not in str(caught.value)
    assert caught.value.__suppress_context__ is True
