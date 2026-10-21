from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from omniflow.exceptions import OmniAuthError
from omniflow.query_transport import QueryEvidenceError, QueryTransport, propose_query

JOB = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"


class Response:
    def __init__(self, value, status=200, *, raw=False):
        self.status_code = status
        self.body = value if raw else json.dumps(value).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def client(responses):
    return SimpleNamespace(base_url="https://example.omniapp.co", timeout=60, session=Session(responses))


def ndjson(*records):
    return Response(b"\n".join(json.dumps(value).encode() for value in records), raw=True)


def test_documented_json_array_and_fixed_nonexecution_controls():
    response = Response([{"fact.count": 5}])
    api = client([response])
    transport = QueryTransport(api)
    assert transport.run({"modelId": "model", "fields": ["fact.count"], "limit": 2}, branch_id="branch",
                         environment_connection_id="env", user_id="service", timezone="UTC") == [{"fact.count": 5}]
    method, url, options = api.session.calls[0]
    assert method == "POST" and url.endswith("/api/v1/query/run")
    assert options["json"] == {
        "query": {"modelId": "model", "fields": ["fact.count"], "limit": 2}, "branchId": "branch",
        "environmentConnectionId": "env", "cache": "SkipCache", "resultType": "json",
        "formatResults": False, "workbookUrl": False, "timezone": "UTC",
    }
    assert options["params"] == {"userId": "service"} and options["allow_redirects"] is False
    assert options["stream"] and response.closed


def test_default_timezone_inherits_connection_instead_of_sending_unsupported_override():
    api = client([Response([{"fact.count": 5}])])
    QueryTransport(api).run({"modelId": "model", "fields": ["fact.count"], "limit": 2}, branch_id=None)
    assert "timezone" not in api.session.calls[0][2]["json"]


def test_explicit_timezone_error_is_not_silently_retried_without_override():
    api = client([Response({"detail": "timezone requires user-specific timezones enabled"}, 400)])
    with pytest.raises(QueryEvidenceError) as error:
        QueryTransport(api).run({}, branch_id=None, timezone="UTC")
    assert error.value.diagnostic_context["http_status"] == 400
    assert len(api.session.calls) == 1 and api.session.calls[0][2]["json"]["timezone"] == "UTC"


@pytest.mark.parametrize("status,reason,exception", [
    (401, "omni_unauthorized", OmniAuthError), (403, "omni_forbidden", OmniAuthError),
    (429, "omni_rate_limited", QueryEvidenceError), (500, "omni_service_unavailable", QueryEvidenceError),
    (302, "omni_http_error", QueryEvidenceError),
])
def test_auth_redirects_and_transient_http_are_never_blindly_post_retried(status, reason, exception):
    response = Response({"raw_secret": "SECRET"}, status)
    api = client([response])
    with pytest.raises(exception) as error:
        QueryTransport(api).run({}, branch_id=None)
    assert error.value.reason_code == reason and len(api.session.calls) == 1 and response.closed
    assert "SECRET" not in str(error.value) and "SECRET" not in str(error.value.diagnostic_context)


@pytest.mark.parametrize("failure,reason", [
    (requests.Timeout("SECRET"), "omni_timeout"),
    (requests.ConnectionError("SECRET"), "omni_connection_error"),
    (requests.exceptions.SSLError("SECRET"), "omni_tls_error"),
])
def test_ambiguous_transport_failure_is_not_retried(failure, reason):
    api = client([failure])
    with pytest.raises(QueryEvidenceError) as error:
        QueryTransport(api).run({}, branch_id=None)
    assert error.value.reason_code == reason and error.value.diagnostic_context["remote_execution_may_continue"]
    assert len(api.session.calls) == 1 and "SECRET" not in str(error.value)


@pytest.mark.parametrize("raw,reason", [
    (b"", "empty_api_response"), (b"<html>SECRET</html>", "invalid_json_response"),
    (b'[{"a":1,"a":2}]', "invalid_json_response"), (b'[{"a":NaN}]', "invalid_json_response"),
    (b'[{"a":1e100000}]', "invalid_json_response"),
    (b'{"rows":[]}', "query_result_schema_invalid"),
])
def test_unsupported_or_ambiguous_json_never_becomes_clean_evidence(raw, reason):
    with pytest.raises(QueryEvidenceError) as error:
        QueryTransport(client([Response(raw, raw=True)])).run({}, branch_id=None)
    assert error.value.reason_code == reason


def test_budget_stops_before_another_post_and_large_response_is_closed():
    response = Response(b"x" * 1025, raw=True)
    api = client([response])
    with pytest.raises(QueryEvidenceError) as error:
        QueryTransport(api, max_response_bytes=1024).run({}, branch_id=None)
    assert error.value.reason_code == "omni_response_limit_exceeded" and response.closed
    api = client([Response([])])
    transport = QueryTransport(api, max_queries=1)
    transport.run({}, branch_id=None)
    with pytest.raises(QueryEvidenceError, match="evidence") as error:
        transport.run({}, branch_id=None)
    assert error.value.reason_code == "query_budget_exhausted" and len(api.session.calls) == 1


def test_timeout_resumes_only_documented_known_job_without_post_and_does_not_invent_json_from_arrow():
    api = client([
        Response({"remaining_job_ids": [JOB]}, 408),
        ndjson({"job_id": JOB, "status": "COMPLETE"}, {"remaining_job_ids": [], "timed_out": "false"}),
    ])
    transport = QueryTransport(api)
    with pytest.raises(QueryEvidenceError) as error:
        transport.run({}, branch_id=None)
    assert error.value.reason_code == "query_async_result_unavailable" and not transport.pending_job_ids
    assert [method for method, _, _ in api.session.calls] == ["POST", "GET"]
    assert api.session.calls[1][2]["params"] == {"jobIds": JOB, "completionOnly": "true"}


def test_unfinished_job_has_bounded_polls_and_only_safe_pending_count():
    api = client([Response({"remaining_job_ids": [JOB]}, 408),
                  ndjson({"remaining_job_ids": [JOB], "timed_out": "true"})])
    with pytest.raises(QueryEvidenceError) as error:
        QueryTransport(api, max_polls=1).run({}, branch_id=None)
    assert error.value.reason_code == "query_unfinished"
    assert error.value.diagnostic_context == {"pending_job_count": 1, "remote_execution_may_continue": True}
    assert JOB not in str(error.value.diagnostic_context)


@pytest.mark.parametrize("records", [
    [{"job_id": OTHER, "status": "COMPLETE"}, {"remaining_job_ids": [], "timed_out": "false"}],
    [{"remaining_job_ids": [], "timed_out": "false"}],
    [{"remaining_job_ids": [JOB], "timed_out": {}}],
    [{"job_id": JOB, "status": "COMPLETE"}, {"remaining_job_ids": [JOB], "timed_out": "true"}],
    [{"job_id": {}, "status": "COMPLETE"}, {"remaining_job_ids": [], "timed_out": "false"}],
    [{"job_id": JOB, "status": []}, {"remaining_job_ids": [], "timed_out": "false"}],
])
def test_unknown_missing_and_conflicting_job_evidence_rejected(records):
    api = client([Response({"remaining_job_ids": [JOB]}, 408), ndjson(*records)])
    with pytest.raises(QueryEvidenceError) as error:
        QueryTransport(api).run({}, branch_id=None)
    assert error.value.reason_code == "query_job_evidence_invalid"


def test_deadline_prevents_submission():
    ticks = iter([0, 2])
    api = client([])
    with pytest.raises(QueryEvidenceError) as error:
        QueryTransport(api, timeout_seconds=1, clock=lambda: next(ticks)).run({}, branch_id=None)
    assert error.value.reason_code == "query_budget_exhausted" and not api.session.calls


def test_failed_async_job_has_safe_failure_without_sql_or_error_text():
    api = client([Response({"remaining_job_ids": [JOB]}, 408),
                  ndjson({"job_id": JOB, "status": "FAILED", "error_message": "SECRET SQL"},
                         {"remaining_job_ids": [], "timed_out": "false"})])
    with pytest.raises(QueryEvidenceError) as error:
        QueryTransport(api).run({}, branch_id=None)
    assert error.value.reason_code == "query_execution_failed"
    assert "SECRET" not in str(error.value) and "SECRET" not in str(error.value.diagnostic_context)


def test_ai_proposal_is_one_private_frozen_review_item_with_all_execution_flags_false():
    generated = {"model_id": "model", "table": "orders", "fields": ["fact.count"], "limit": 10}
    api = client([Response({"query": {"model_job": generated}})])
    result = propose_query(api, "model", topic="orders", prompt="private prompt", branch_id="branch")
    assert result["status"] == "review_required" and not result["executed"] and not result["contract_ready"]
    assert result["model_job"] == generated and len(result["proposal_sha256"]) == 64
    assert len(api.session.calls) == 1
    body = api.session.calls[0][2]["json"]
    assert body["runQuery"] is False and body["workbookUrl"] is False and body["queryAllViews"] is False
    assert body["branchId"] == "branch" and api.session.calls[0][1].endswith("/ai/generate-query")


def test_ai_mismatched_model_does_not_create_executable_contract():
    api = client([Response({"query": {"model_job": {"model_id": "other", "table": "orders", "fields": ["x"]}}})])
    with pytest.raises(QueryEvidenceError) as error:
        propose_query(api, "model", topic="orders", prompt="private")
    assert error.value.reason_code == "query_proposal_invalid"
