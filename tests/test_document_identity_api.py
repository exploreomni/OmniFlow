"""Direct draft-parent discovery preserves scope and discards document content."""

import copy
import json
import time

import pytest
import requests

from omniflow.exceptions import ConfigError, OmniAPIError, OmniAuthError, SecurityPolicyError
from omniflow.omni_client import MAX_RESPONSE_BYTES, OmniClient, OmniReadBudget
from omniflow.validators.content_identity import DocumentIdentityError, resolve_documents

USER = "00000000-0000-0000-0000-000000000001"
MEMBER = "00000000-0000-0000-0000-000000000002"
PRIVATE = "SYNTHETIC_PRIVATE_DOCUMENT_CONTENT"


class Response:
    def __init__(self, payload=None, *, status=200, body=None, headers=None):
        self.body = json.dumps(payload).encode() if body is None else body
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = headers or {}
        self.closed = False
        self.read = False

    def iter_content(self, chunk_size):
        self.read = True
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def client_for(*responses, timeout=60):
    session = Session(responses)
    return OmniClient(base_url="https://omni.example", api_key="synthetic", session=session, timeout=timeout)


def state():
    return {"draftOf": {"identifier": "published-doc", "name": PRIVATE},
            "name": PRIVATE, "description": PRIVATE,
            "queryPresentations": {"data": {"1": {"sql": PRIVATE}}},
            "controls": {"filters": PRIVATE}, "settings": {"unknown": PRIVATE},
            "containers": [{"name": PRIVATE}], "app": {"url": PRIVATE}}


def identity(scope):
    return Response({"keyScope": scope, "user": {"id": USER, "membershipId": MEMBER}})


def test_direct_lookup_projects_only_parent_metadata_and_sends_no_actor_or_content(caplog):
    payload = state()
    original = copy.deepcopy(payload)
    response = Response(payload)
    client = client_for(response)
    result = client.get_document_identity("draft-doc")
    assert result == {"identifier": "draft-doc", "publishedIdentifier": "published-doc"}
    assert PRIVATE not in json.dumps(result) + caplog.text
    assert payload == original
    assert response.closed
    assert len(client.session.calls) == 1
    method, url, options = client.session.calls[0]
    assert (method, url) == ("GET", "https://omni.example/api/v2/documents/draft-doc")
    assert options["params"] is None and options["json"] is None
    assert options["stream"] is True and options["allow_redirects"] is False
    assert 0 < options["timeout"] <= 60
    assert client._read_budget is None


@pytest.mark.parametrize("payload", [
    None, [], {}, {"draftOf": None}, {"draftOf": []}, {"draftOf": {}},
    {"draftOf": {"identifier": None}}, {"draftOf": {"identifier": 1}},
    {"draftOf": {"identifier": ""}}, {"draftOf": {"identifier": "../private"}},
    {"draftOf": {"identifier": "draft-doc"}},
])
def test_missing_or_invalid_parent_is_not_new_document_evidence(payload):
    client = client_for(Response(payload))
    with pytest.raises(OmniAPIError) as caught:
        client.get_document_identity("draft-doc")
    assert caught.value.reason_code == "document_identity_invalid_response"
    assert caught.value.diagnostic_context == {"service": "omni", "api_operation": "document_state"}
    assert "../private" not in str(caught.value)


@pytest.mark.parametrize("document_id", [None, 1, "", "../private", "draft?query=private"])
def test_invalid_request_identity_does_not_reach_transport(document_id):
    client = client_for()
    with pytest.raises((ConfigError, SecurityPolicyError)):
        client.get_document_identity(document_id)
    assert not client.session.calls


@pytest.mark.parametrize("status,reason,error_type", [
    (401, "omni_unauthorized", OmniAuthError),
    (403, "omni_forbidden", OmniAuthError),
    (404, "omni_http_error", OmniAPIError),
    (429, "omni_rate_limited", OmniAPIError),
    (503, "omni_service_unavailable", OmniAPIError),
])
def test_http_failure_keeps_safe_evidence_without_body_or_retries(status, reason, error_type, caplog):
    response = Response({"message": PRIVATE}, status=status)
    client = client_for(response)
    with pytest.raises(error_type) as caught:
        client.get_document_identity("private-draft")
    assert caught.value.reason_code == reason
    assert caught.value.diagnostic_context == {
        "service": "omni", "api_operation": "document_state", "http_status": status,
        "attempt_count": 1, "max_attempts": 1, "retryable": False,
    }
    assert response.closed and not response.read
    assert len(client.session.calls) == 1 and client._read_budget is None
    assert PRIVATE not in str(caught.value) + caplog.text
    assert "private-draft" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("error,reason", [
    (requests.Timeout(PRIVATE), "omni_timeout"),
    (requests.ConnectionError(PRIVATE), "omni_connection_error"),
    (requests.exceptions.SSLError(PRIVATE), "omni_tls_error"),
])
def test_network_failure_retains_category_without_private_message(error, reason, caplog):
    client = client_for(error)
    with pytest.raises(OmniAPIError) as caught:
        client.get_document_identity("draft-doc")
    assert caught.value.reason_code == reason
    assert caught.value.diagnostic_context["api_operation"] == "document_state"
    assert caught.value.diagnostic_context["attempt_count"] == 1
    assert len(client.session.calls) == 1
    assert PRIVATE not in str(caught.value) + caplog.text
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("body,reason", [
    (b"", "empty_api_response"),
    (PRIVATE.encode(), "invalid_json_response"),
    (b'{"draftOf":{"identifier":"first","identifier":"second"}}', "invalid_json_response"),
])
def test_invalid_json_is_safe_and_distinct_from_invalid_metadata(body, reason):
    response = Response(body=body)
    client = client_for(response)
    with pytest.raises(OmniAPIError) as caught:
        client.get_document_identity("draft-doc")
    assert caught.value.reason_code == reason
    assert caught.value.diagnostic_context["http_status"] == 200
    assert PRIVATE not in str(caught.value)
    assert response.closed


def test_document_response_size_is_bounded_before_body_read():
    response = Response(state(), headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})
    with pytest.raises(OmniAPIError) as caught:
        client_for(response).get_document_identity("draft-doc")
    assert caught.value.reason_code == "omni_response_limit_exceeded"
    assert response.closed and not response.read


def test_document_response_stream_is_bounded_without_content_length(monkeypatch):
    monkeypatch.setattr("omniflow.omni_client.MAX_RESPONSE_BYTES", 32)
    response = Response(state())
    with pytest.raises(OmniAPIError) as caught:
        client_for(response).get_document_identity("draft-doc")
    assert caught.value.reason_code == "omni_response_limit_exceeded"
    assert response.closed


def test_existing_budget_is_consumed_and_not_replaced():
    client = client_for(Response(state()), timeout=300)
    budget = OmniReadBudget(time.monotonic() + 2, max_requests=1)
    with client.bounded_reads(budget):
        assert client.get_document_identity("draft-doc")["publishedIdentifier"] == "published-doc"
        assert client._read_budget is budget and budget.requests == 1
        with pytest.raises(OmniAPIError) as caught:
            client.get_document_identity("another-draft")
        assert caught.value.reason_code == "content_impact_budget_exhausted"
        assert caught.value.diagnostic_context["api_operation"] == "document_state"
    assert len(client.session.calls) == 1
    assert 0 < client.session.calls[0][2]["timeout"] <= 2
    assert client._read_budget is None


def test_expired_deadline_prevents_document_request():
    client = client_for()
    with client.bounded_reads(OmniReadBudget(time.monotonic() - 1, max_requests=2)):
        with pytest.raises(OmniAPIError) as caught:
            client.get_document_identity("draft-doc")
    assert caught.value.reason_code == "content_impact_timeout"
    assert not client.session.calls


@pytest.mark.parametrize("personal", [True, False])
def test_explicit_actor_is_not_silently_dropped(personal):
    client = client_for()
    with pytest.raises(ConfigError) as caught:
        client.get_document_identity("draft-doc", user_id=USER, include_personal_folders=personal)
    assert caught.value.reason_code == "document_identity_scope_unsupported"
    assert not client.session.calls


def test_pat_personal_content_uses_authenticated_identity_with_no_impersonation():
    client = client_for(identity("user"), Response(state()))
    assert client.get_document_identity("draft-doc", include_personal_folders=True) == {
        "identifier": "draft-doc", "publishedIdentifier": "published-doc",
    }
    assert len(client.session.calls) == 2
    assert client.session.calls[0][1].endswith("/api/v1/whoami")
    assert client.session.calls[1][2]["params"] is None
    assert client._read_budget is None


def test_org_personal_scope_cannot_fall_back_to_owner_or_shared_scope():
    client = client_for(identity("organization"))
    with pytest.raises(ConfigError) as caught:
        client.get_document_identity("draft-doc", include_personal_folders=True)
    assert caught.value.reason_code == "content_personal_user_required"
    assert len(client.session.calls) == 1
    assert client._read_budget is None


def test_existing_published_layout_reader_still_rejects_drafts():
    client = client_for(Response(state()), Response({"name": PRIVATE, "queryPresentations": {}}))
    with pytest.raises(OmniAPIError) as caught:
        client.get_document_state("draft-doc")
    assert caught.value.reason_code == "content_impact_draft_document"
    assert client.get_document_state("published-doc") == {"name": PRIVATE, "queryPresentations": {}}


@pytest.mark.parametrize("draft_payload", [
    {}, [None], [{"identifier": "draft-doc"}],
    [{"identifier": "draft-doc", "publishedIdentifier": "published-doc", "branch": {"id": "branch"},
      "status": "active", "draftOutOfDate": "false", "updatedAt": "2026-10-28T00:00:00Z"}],
])
def test_real_draft_adapter_classifies_malformed_response_through_resolver(draft_payload):
    client = client_for(Response(state()), Response(draft_payload))
    with pytest.raises(DocumentIdentityError) as caught:
        resolve_documents(
            client=client, branch_id="branch", current=[{"identifier": "draft-doc"}],
            baseline=[{"identifier": "published-doc"}], records=[],
        )
    assert caught.value.reason_code == "document_identity_invalid_response"
    assert caught.value.diagnostic_context["api_operation"] == "document_drafts"
    assert caught.value.diagnostic_context["document_reference"] == "document-1"
    assert "draft-doc" not in str(caught.value)
    assert len(client.session.calls) == 2
