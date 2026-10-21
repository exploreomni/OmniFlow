"""Bounded, non-retrying query transport for the documented Omni JSON API.

No query SQL, rows, credentials, or job IDs enter exception diagnostics. A client
deadline is not server cancellation. Wait can reconcile known jobs, but this
first adapter does not decode Arrow and never invents a successful JSON result.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable
from typing import Any

import requests

from .exceptions import OmniAPIError, OmniAuthError
from .json_evidence import reject_duplicate_keys
from .security import validate_base_url


class QueryEvidenceError(OmniAPIError):
    def __init__(self, reason_code: str, **context: Any) -> None:
        super().__init__(
            "Query validation evidence is unavailable; inspect the safe reason category.",
            reason_code=reason_code, diagnostic_context=context,
        )


def _decode(data: bytes) -> Any:
    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON")
        return parsed

    try:
        if not data.strip():
            raise QueryEvidenceError("empty_api_response")
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=reject_duplicate_keys,
            parse_float=finite_float,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise QueryEvidenceError("invalid_json_response") from exc


def _job_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise QueryEvidenceError("query_job_evidence_invalid")
    try:
        result = tuple(str(uuid.UUID(item)) for item in value if isinstance(item, str))
    except ValueError as exc:
        raise QueryEvidenceError("query_job_evidence_invalid") from exc
    if len(result) != len(value) or len(set(result)) != len(result):
        raise QueryEvidenceError("query_job_evidence_invalid")
    return result


class QueryTransport:
    def __init__(
        self, client: Any, *, timeout_seconds: int = 120, max_response_bytes: int = 1_048_576,
        max_queries: int = 12, max_polls: int = 2, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.base_url = validate_base_url(client.base_url)
        self.clock = clock
        self.deadline = clock() + timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_queries = max_queries
        self.max_polls = max_polls
        self.submissions = 0
        self.polls = 0
        self.pending_job_ids: tuple[str, ...] = ()

    def _remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise QueryEvidenceError(
                "query_budget_exhausted", pending_job_count=len(self.pending_job_ids),
                remote_execution_may_continue=bool(self.pending_job_ids),
            )
        return remaining

    def _request(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> tuple[int, bytes]:
        operation = "query_wait" if method == "GET" else "query_proposal" if path.endswith("generate-query") else "query_run"
        context = {"service": "omni", "api_operation": operation, "attempt_count": 1, "max_attempts": 1,
                   "retryable": False}
        remaining = self._remaining()
        try:
            response = self.client.session.request(
                method, self.base_url + path, json=body, params=params, allow_redirects=False, stream=True,
                timeout=min(float(self.client.timeout), remaining),
            )
        except requests.RequestException as exc:
            reason = ("omni_tls_error" if isinstance(exc, requests.exceptions.SSLError) else
                      "omni_timeout" if isinstance(exc, requests.Timeout) else
                      "omni_connection_error" if isinstance(exc, requests.ConnectionError) else "omni_request_failed")
            raise QueryEvidenceError(reason, **context, remote_execution_may_continue=method == "POST") from exc
        status = response.status_code
        context["http_status"] = status
        try:
            if status in {401, 403}:
                raise OmniAuthError(
                    "Omni denied query validation access.",
                    reason_code="omni_unauthorized" if status == 401 else "omni_forbidden",
                    diagnostic_context=context,
                )
            if status != 408 and not 200 <= status < 300:
                reason = ("omni_rate_limited" if status == 429 else
                          "omni_service_unavailable" if 500 <= status < 600 else "omni_http_error")
                raise QueryEvidenceError(reason, **context, remote_execution_may_continue=method == "POST")
            data = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=16_384):
                    self._remaining()
                    if not isinstance(chunk, bytes):
                        raise QueryEvidenceError("invalid_json_response", **context)
                    data.extend(chunk)
                    if len(data) > self.max_response_bytes:
                        raise QueryEvidenceError("omni_response_limit_exceeded", **context,
                                                 remote_execution_may_continue=method == "POST")
            except requests.RequestException as exc:
                raise QueryEvidenceError("omni_response_interrupted", **context,
                                         remote_execution_may_continue=method == "POST") from exc
            return status, bytes(data)
        finally:
            response.close()

    def run(self, query: dict, *, branch_id: str | None, environment_connection_id: str = "",
            user_id: str = "", timezone: str = "") -> list[dict]:
        if self.submissions >= self.max_queries:
            raise QueryEvidenceError("query_budget_exhausted")
        if "branchId" in query or "branch_id" in query:
            raise QueryEvidenceError("query_scope_invalid")
        body = {
            "query": query, "cache": "SkipCache", "resultType": "json", "formatResults": False,
            "workbookUrl": False,
        }
        # Connections without user-specific timezones reject this optional
        # override. Never invent UTC or retry an explicit unsupported override.
        if timezone:
            body["timezone"] = timezone
        if branch_id:
            body["branchId"] = branch_id
        if environment_connection_id:
            body["environmentConnectionId"] = environment_connection_id
        self.submissions += 1
        status, raw = self._request(
            "POST", "/api/v1/query/run", body=body, params={"userId": user_id} if user_id else None,
        )
        payload = _decode(raw)
        if status == 408:
            if not isinstance(payload, dict):
                raise QueryEvidenceError("query_job_evidence_invalid", remote_execution_may_continue=True)
            self.pending_job_ids = _job_ids(payload.get("remaining_job_ids"))
            self._reconcile_pending()
            # Wait is documented as NDJSON + Arrow, not JSON rows. Completing
            # remote work is not equivalent to recovering comparable evidence.
            raise QueryEvidenceError("query_async_result_unavailable", pending_job_count=0)
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise QueryEvidenceError("query_result_schema_invalid")
        return payload

    def _reconcile_pending(self) -> None:
        while self.pending_job_ids and self.polls < self.max_polls:
            previous = set(self.pending_job_ids)
            self.polls += 1
            _, raw = self._request("GET", "/api/v1/query/wait", params={
                "jobIds": ",".join(self.pending_job_ids), "completionOnly": "true",
            })
            records = [_decode(line) for line in raw.splitlines() if line.strip()]
            if not records or any(not isinstance(item, dict) for item in records):
                raise QueryEvidenceError("query_job_evidence_invalid")
            footer = records[-1]
            remaining_raw = footer.get("remaining_job_ids")
            if not isinstance(remaining_raw, list):
                raise QueryEvidenceError("query_job_evidence_invalid")
            remaining = set(_job_ids(remaining_raw)) if remaining_raw else set()
            timed_out = footer.get("timed_out")
            if (not remaining <= previous or type(timed_out) not in {bool, str}
                    or timed_out not in {True, False, "true", "false"}):
                raise QueryEvidenceError("query_job_evidence_invalid")
            completed: set[str] = set()
            failed = False
            for record in records[:-1]:
                job_id = record.get("job_id")
                status = record.get("status")
                if (not isinstance(job_id, str) or job_id not in previous or job_id in completed
                        or not isinstance(status, str) or status not in {
                    "COMPLETE", "ERROR", "FAILED", "MISSING",
                }):
                    raise QueryEvidenceError("query_job_evidence_invalid")
                completed.add(job_id)
                failed |= record["status"] != "COMPLETE"
            if completed & remaining or completed | remaining != previous:
                raise QueryEvidenceError("query_job_evidence_invalid")
            if bool(remaining) != (timed_out in {True, "true"}):
                raise QueryEvidenceError("query_job_evidence_invalid")
            self.pending_job_ids = tuple(sorted(remaining))
            if failed:
                raise QueryEvidenceError("query_execution_failed", pending_job_count=len(remaining),
                                         remote_execution_may_continue=bool(remaining))
        if self.pending_job_ids:
            raise QueryEvidenceError("query_unfinished", pending_job_count=len(self.pending_job_ids),
                                     remote_execution_may_continue=True)


def propose_query(client: Any, model_id: str, *, topic: str, prompt: str, branch_id: str | None = None) -> dict:
    """Return one private, unreviewed proposal. This function never runs a query.

    The result may contain customer field names and filters; callers must save it
    privately, not include it in a PR report or execute it automatically.
    """
    if (not all(isinstance(value, str) and value.strip() for value in (model_id, topic, prompt))
            or len(prompt.encode()) > 16_384 or len(topic) > 256):
        raise QueryEvidenceError("query_proposal_invalid")
    body = {
        "modelId": model_id, "currentTopicName": topic, "prompt": prompt,
        "runQuery": False, "workbookUrl": False, "queryAllViews": False,
    }
    if branch_id:
        body["branchId"] = branch_id
    transport = QueryTransport(client, max_queries=1)
    status, raw = transport._request("POST", "/api/v1/ai/generate-query", body=body)
    if status == 408:
        raise QueryEvidenceError("query_proposal_unavailable")
    payload = _decode(raw)
    generated = payload.get("query", {}).get("model_job") if isinstance(payload, dict) and isinstance(
        payload.get("query"), dict
    ) else None
    if not isinstance(generated, dict) or generated.get("model_id") != model_id:
        raise QueryEvidenceError("query_proposal_invalid")
    if (not isinstance(generated.get("table"), str) or not isinstance(generated.get("fields"), list)
            or not generated["fields"] or any(not isinstance(item, str) for item in generated["fields"])
            or any(key in generated for key in ("branchId", "branch_id"))):
        raise QueryEvidenceError("query_proposal_invalid")
    # Preserve the complete single proposal privately. Unsupported query features
    # must be reviewed/translated, never silently removed before execution.
    frozen = json.dumps(generated, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "status": "review_required", "executed": False, "model_job": generated,
        "proposal_sha256": hashlib.sha256(frozen.encode()).hexdigest(),
        "contract_ready": False,
    }
