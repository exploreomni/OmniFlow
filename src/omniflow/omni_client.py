from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import requests

from . import __version__
from .exceptions import ConfigError, OmniAPIError, OmniAuthError, OmniResponseError
from .json_evidence import reject_duplicate_keys
from .model_response import normalize_model_response
from .model_yaml import validate_editable_yaml_file_name
from .security import redact, validate_base_url, validate_path_segment

LOG = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_PAGINATION_PAGES = 500
MAX_PAGINATION_RECORDS = 50_000
MAX_AI_PROMPT_BYTES = 16 * 1024
AI_JOB_STATES = {"CANCELLED", "COMPLETE", "DELIVERING", "EXECUTING", "FAILED", "QUEUED"}
AI_EVAL_TERMINAL_STATES = {"COMPLETE", "CANCELLED", "FAILED"}
AI_EVAL_STATES = AI_EVAL_TERMINAL_STATES | {"RUNNING"}
MAX_AI_EVAL_DESCRIPTION_CHARS = 200
SCHEMA_REFRESH_STATES = {"RUNNING", "COMPLETED", "FAILED"}
SCHEMA_REFRESH_MODEL_KINDS = {"SHARED", "SHARED_EXTENSION"}


@dataclass
class OmniReadBudget:
    """One bounded advisory operation, shared across its clients; no retry budget."""

    deadline: float
    max_requests: int
    requests: int = 0

    def remaining(self) -> float:
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise OmniAPIError("Read-only analysis reached its time limit.",
                               reason_code="content_impact_timeout")
        return seconds

    def consume(self) -> float:
        remaining = self.remaining()
        if self.requests >= self.max_requests:
            raise OmniAPIError("Read-only analysis reached its request limit.",
                               reason_code="content_impact_budget_exhausted")
        self.requests += 1
        return remaining


class OmniClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: int = 60,
        session: requests.Session | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ConfigError("Omni API key must be a non-empty string")
        if not isinstance(timeout, int) or not 1 <= timeout <= 300:
            raise ConfigError("Omni API timeout must be between 1 and 300 seconds")
        self.base_url = validate_base_url(base_url)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": f"omniflow/{__version__}",
            }
        )
        # Identity only, never content or permission evidence. Bind this run-local
        # cache to the instance and actual credential, not the requested actor.
        self._content_identity_cache: tuple[tuple[str, str], tuple[str, str, str]] | None = None
        self._read_budget: OmniReadBudget | None = None

    @contextmanager
    def bounded_reads(self, budget: OmniReadBudget):
        """Apply a shared deadline/request bound without changing other workflows."""
        previous = self._read_budget
        if previous is not None:
            raise ConfigError("Read-only budgets cannot be nested.")
        self._read_budget = budget
        try:
            yield self
        finally:
            self._read_budget = previous

    def validation_evidence_binding(self) -> tuple[str, str]:
        """Private run-local binding; never expose credential hashes in reports."""
        authorization = self.session.headers.get("Authorization", "")
        return (self.base_url, hashlib.sha256(str(authorization).encode()).hexdigest())

    def _content_identity(self) -> tuple[str, str, str]:
        """Read the credential owner's identity, not an arbitrary acting user."""
        binding = self.validation_evidence_binding()
        cached = self._content_identity_cache
        if cached is not None and cached[0] == binding:
            key_scope, caller_id, membership_id = cached[1]
        else:
            self._content_identity_cache = None
            payload = self._request("GET", "/api/v1/whoami")
            try:
                key_scope = payload["keyScope"]
                caller_id = str(UUID(payload["user"]["id"]))
                membership_id = str(UUID(payload["user"]["membershipId"]))
                if key_scope not in {"user", "organization"}:
                    raise ValueError("unsupported credential scope")
            except (KeyError, TypeError, ValueError, AttributeError):
                raise OmniAPIError(
                    "Content validation scope could not be established from Omni identity metadata. "
                    "Verify the credential's user and organization membership before rerunning.",
                    reason_code="content_identity_invalid",
                    diagnostic_context={"service": "omni", "api_operation": "identity_lookup"},
                ) from None
            if self.validation_evidence_binding() != binding:
                raise OmniAPIError(
                    "The Omni instance or credential changed during identity lookup. "
                    "Rerun validation with one unchanged identity and scope.",
                    reason_code="validation_identity_changed",
                    diagnostic_context={"service": "omni", "api_operation": "identity_lookup"},
                )
            self._content_identity_cache = (binding, (key_scope, caller_id, membership_id))
        return key_scope, caller_id, membership_id

    def _resolve_content_actor(
        self, user_id: str | None, *, include_personal_folders: bool,
    ) -> tuple[str, str, str] | None:
        """Map an explicitly selected owner to membership without guessing scope.

        Configuration retains the global user ID. Content Validator's userId
        and the restricted catalog's creatorId both require membership IDs.
        Default shared-only validation needs no identity lookup or actor.
        """
        if user_id is None and not include_personal_folders:
            return None
        key_scope, caller_id, membership_id = self._content_identity()
        context = {"service": "omni", "api_operation": "identity_lookup"}
        if key_scope == "user":
            if user_id is not None:
                raise ConfigError(
                    "Content validation with a PAT must omit omni.user_id, OMNI_USER_ID, "
                    "and --user-id. OmniFlow uses the authenticated user's scope without impersonation.",
                    reason_code="content_pat_impersonation_unsupported", diagnostic_context=context,
                )
            return key_scope, caller_id, membership_id
        if not user_id and include_personal_folders:
            raise ConfigError(
                "Personal-folder validation with an organization key requires an explicit omni.user_id. "
                "Use the credential owner's user ID, not their membership ID; other actors cannot yet "
                "be mapped safely by the documented API.",
                reason_code="content_personal_user_required", diagnostic_context=context,
            )
        try:
            intended_user_id = str(UUID(user_id))
        except (TypeError, ValueError, AttributeError):
            intended_user_id = None
        if intended_user_id != caller_id:
            raise ConfigError(
                "The selected content-validation actor cannot be mapped to a verified organization membership. "
                "Use that user's PAT without user_id, or explicitly select the organization credential's "
                "owner using their global user ID. OmniFlow will not substitute another account's scope.",
                reason_code=("content_personal_actor_unresolved" if include_personal_folders
                             else "content_actor_unresolved"), diagnostic_context=context,
            )
        return key_scope, caller_id, membership_id

    def _personal_content_membership_id(self, user_id: str | None) -> str:
        actor = self._resolve_content_actor(user_id, include_personal_folders=True)
        if actor is None:
            raise OmniAPIError("Personal-folder identity could not be verified.",
                               reason_code="content_identity_invalid")
        return actor[2]

    def content_identity_context(
        self, *, user_id: str | None = None, include_personal_folders: bool = False,
    ) -> dict[str, str]:
        """Opaque private-history scope binding, not permission/coverage proof."""
        actor = self._resolve_content_actor(user_id, include_personal_folders=include_personal_folders)
        encoded = json.dumps(
            ["membership_v1", self.validation_evidence_binding(), actor, include_personal_folders],
            separators=(",", ":"),
        ).encode()
        return {"identity_contract": "membership_v1", "identity_fingerprint": hashlib.sha256(encoded).hexdigest()}

    def get_model_yaml(
        self,
        model_id: str,
        branch_id: str | None = None,
        mode: str = "combined",
        include_checksums: bool = True,
        fully_resolved: bool = False,
    ) -> dict[str, Any]:
        model_id = validate_path_segment(model_id, name="model_id")
        params: dict[str, Any] = {
            "mode": mode,
            "includeChecksums": str(include_checksums).lower(),
            "fullyResolved": str(fully_resolved).lower(),
        }
        if branch_id:
            params["branchId"] = branch_id
        return self._request("GET", f"/api/v1/models/{model_id}/yaml", params=params)

    def validate_model(self, model_id: str, branch_id: str | None = None) -> list[dict[str, Any]]:
        model_id = validate_path_segment(model_id, name="model_id")
        params = {"branchId": branch_id} if branch_id else None
        payload = self._request("GET", f"/api/v1/models/{model_id}/validate", params=params)
        return normalize_model_response(payload)

    def validate_content(
        self,
        model_id: str,
        branch_id: str | None = None,
        user_id: str | None = None,
        include_personal_folders: bool = False,
        find: str | None = None,
        find_type: str | None = None,
        force_full_validation: bool = False,
    ) -> Any:
        model_id = validate_path_segment(model_id, name="model_id")
        if type(force_full_validation) is not bool:
            raise ConfigError("force_full_validation must be a boolean")
        actor = self._resolve_content_actor(user_id, include_personal_folders=include_personal_folders)
        params: dict[str, Any] = {}
        if force_full_validation:
            params["force_full_validation"] = "true"
        if actor is not None and actor[0] == "organization":
            params["userId"] = actor[2]
        if branch_id:
            params["branch_id"] = branch_id
        if include_personal_folders:
            params["include_personal_folders"] = "true"
        if find:
            params["find"] = find
        if find_type:
            params["find_type"] = _content_validator_find_type(find_type)
        payload = self._request("GET", f"/api/v1/models/{model_id}/content-validator", params=params)
        _validate_content_response_identity(payload, model_id=model_id, branch_id=branch_id or None)
        return payload

    def search_content_references(
        self,
        model_id: str,
        *,
        find: str,
        find_type: str,
        branch_id: str | None = None,
        user_id: str | None = None,
        include_personal_folders: bool = False,
    ) -> Any:
        return self.validate_content(
            model_id,
            branch_id=branch_id,
            user_id=user_id,
            include_personal_folders=include_personal_folders,
            find=find,
            find_type=find_type,
        )

    def get_git_configuration(self, model_id: str) -> dict[str, Any]:
        model_id = validate_path_segment(model_id, name="model_id")
        payload = self._request("GET", f"/api/v1/models/{model_id}/git")
        if not isinstance(payload, dict):
            raise OmniAPIError("Git configuration returned an unexpected response shape")
        return payload

    def get_document_queries(
        self, document_id: str, *, user_id: str | None = None,
        include_personal_folders: bool = False,
    ) -> dict[str, Any]:
        """Read saved query definitions for a search-discovered document only."""
        document_id = validate_path_segment(document_id, name="document_id")
        actor = self._resolve_content_actor(user_id, include_personal_folders=include_personal_folders)
        params = {"userId": actor[2]} if actor is not None and actor[0] == "organization" else {}
        payload = self._request("GET", f"/api/v1/documents/{document_id}/queries", params=params)
        if (not isinstance(payload, dict) or not isinstance(payload.get("queries"), list)
                or any(not isinstance(query, dict) for query in payload["queries"])):
            raise OmniAPIError("Saved query metadata is unavailable.",
                               reason_code="content_impact_invalid_queries")
        return payload

    def get_document_state(
        self, document_id: str, *, user_id: str | None = None,
        include_personal_folders: bool = False,
    ) -> dict[str, Any]:
        """Read published layout without silently broadening an acting-user scope."""
        document_id = validate_path_segment(document_id, name="document_id")
        self._resolve_content_actor(user_id, include_personal_folders=include_personal_folders)
        if user_id is not None:
            raise ConfigError(
                "The document-state API does not document acting-user scope. "
                "Use that user's PAT without user_id to inspect layout in the same scope.",
                reason_code="content_impact_document_scope_unavailable",
            )
        payload = self._request("GET", f"/api/v2/documents/{document_id}")
        if not isinstance(payload, dict):
            raise OmniAPIError("Published document state is unavailable.",
                               reason_code="content_impact_invalid_document")
        if payload.get("draftOf") is not None:
            raise OmniAPIError("The document response identifies a draft, not published content.",
                               reason_code="content_impact_draft_document")
        return payload

    def get_dbt_exposures(self, model_id: str, branch_id: str | None = None) -> dict[str, Any]:
        model_id = validate_path_segment(model_id, name="model_id")
        params = {"branch_id": branch_id} if branch_id else {}
        records = self._paginate(f"/api/v1/models/{model_id}/dbt-exposures", params=params)
        return {"records": records}

    def start_schema_refresh(
        self,
        model_id: str,
        *,
        branch_id: str | None = None,
        hard_refresh: bool = True,
    ) -> dict[str, str]:
        model_id = validate_path_segment(model_id, name="model_id")
        if not isinstance(hard_refresh, bool):
            raise ConfigError("hard_refresh must be a boolean")
        params: dict[str, str] = {"hard_refresh": str(hard_refresh).lower()}
        if branch_id:
            params["branch_id"] = validate_path_segment(branch_id, name="branch_id")
        payload = self._request(
            "POST",
            f"/api/v1/models/{model_id}/refresh",
            params=params,
            retry_transient=False,
        )
        if not isinstance(payload, dict):
            raise OmniAPIError("Schema refresh returned an unexpected response shape")
        job_id = payload.get("jobId")
        response_model_id = payload.get("modelId")
        status = _schema_refresh_status(payload.get("status"))
        if not isinstance(job_id, str) or not job_id.strip():
            raise OmniAPIError("Schema refresh did not return a job ID")
        if response_model_id != model_id:
            raise OmniAPIError("Schema refresh returned a mismatched model ID")
        return {
            "job_id": validate_path_segment(job_id.strip(), name="job_id"),
            "model_id": model_id,
            "status": status,
        }

    def get_schema_refresh_job_status(self, job_id: str) -> dict[str, str]:
        job_id = validate_path_segment(job_id, name="job_id")
        payload = self._request("GET", f"/api/v1/jobs/{job_id}/status")
        if not isinstance(payload, dict):
            raise OmniAPIError("Schema refresh job status returned an unexpected response shape")
        response_job_id = payload.get("job_id")
        job_type = payload.get("job_type")
        status = _schema_refresh_status(payload.get("status"))
        if response_job_id != job_id:
            raise OmniAPIError("Schema refresh job status returned a mismatched job ID")
        if job_type != "refresh_schema":
            raise OmniAPIError("Schema refresh job status returned an unexpected job type")
        result = {"job_id": job_id, "job_type": job_type, "status": status}
        model_id = payload.get("model_id")
        if isinstance(model_id, str) and model_id.strip():
            result["model_id"] = validate_path_segment(model_id.strip(), name="model_id")
        # Timestamps and any future data-bearing fields are intentionally discarded.
        return result

    def list_models(
        self,
        model_kind: str | None = None,
        base_model_id: str | None = None,
        name: str | None = None,
        model_id: str | None = None,
        connection_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if model_kind:
            params["modelKind"] = model_kind
        if base_model_id:
            params["baseModelId"] = base_model_id
        if name:
            params["name"] = name
        if model_id:
            params["modelId"] = validate_path_segment(model_id, name="model_id")
        if connection_id:
            params["connectionId"] = validate_path_segment(connection_id, name="connection_id")
        return self._paginate("/api/v1/models", params=params)

    def get_model_metadata(self, model_id: str) -> dict[str, str]:
        model_id = validate_path_segment(model_id, name="model_id")
        matching = [record for record in self.list_models(model_id=model_id) if record.get("id") == model_id]
        if len(matching) != 1:
            raise OmniAPIError("Model metadata lookup did not return exactly one matching model")
        connection_id = matching[0].get("connectionId")
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise OmniAPIError("Model metadata lookup did not return a connection ID")
        return {
            "model_id": model_id,
            "connection_id": validate_path_segment(connection_id.strip(), name="connection_id"),
        }

    def list_refresh_affected_model_ids(self, connection_id: str) -> list[str]:
        connection_id = validate_path_segment(connection_id, name="connection_id")
        model_ids: set[str] = set()
        for record in self.list_models(connection_id=connection_id):
            if record.get("modelKind") not in SCHEMA_REFRESH_MODEL_KINDS:
                continue
            if record.get("connectionId") != connection_id:
                raise OmniAPIError("Connection model lookup returned a mismatched connection ID")
            model_id = record.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                raise OmniAPIError("Connection model lookup returned an invalid model ID")
            model_ids.add(validate_path_segment(model_id.strip(), name="model_id"))
        if not model_ids:
            raise OmniAPIError("Connection model lookup did not return any shared models")
        return sorted(model_ids)

    def resolve_branch_id(self, model_id: str, branch_name: str | None) -> str | None:
        if not branch_name:
            return None
        matches = [record for record in self.list_models(model_kind="BRANCH", base_model_id=model_id, name=branch_name)
                   if record.get("modelKind") == "BRANCH" and record.get("baseModelId") == model_id
                   and record.get("name") == branch_name]
        if not matches:
            return None
        if len(matches) != 1:
            raise ConfigError("More than one Omni branch matches the selection", reason_code="branch_ambiguous")
        identifier = matches[0].get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ConfigError("Selected Omni branch has no usable ID", reason_code="branch_identity_invalid")
        return validate_path_segment(identifier, name="branch_id")

    def list_content(
        self,
        labels: list[str] | None = None,
        branch_id: str | None = None,
        include_personal_folders: bool = False,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # Content metadata is not branch-scoped in the documented API.
        del branch_id
        membership_id = self._personal_content_membership_id(user_id) if include_personal_folders else None
        params: dict[str, Any] = {"include": "labels", "scope": "organization"}
        if labels:
            params["labels"] = ",".join(labels)
        records = self._paginate("/api/v1/content", params=params)
        if membership_id is not None:
            restricted_params = dict(params)
            restricted_params.update({"scope": "restricted", "creatorId": membership_id})
            records.extend(self._paginate("/api/v1/content", params=restricted_params))
        return records

    def list_document_drafts(self, published_identifier: str) -> list[dict[str, Any]]:
        """Read active draft identity metadata, never workbook or actor details.

        The documented endpoint returns an array, not a paginated envelope.
        Omitting ``include=archived`` requests active drafts only. Preserve any
        returned status/staleness/branch values for the resolver to evaluate;
        this adapter does not select, silently discard, or authorize a draft.
        """
        if not isinstance(published_identifier, str) or not published_identifier:
            raise ConfigError("Published document identifier must be a non-empty string")
        published_identifier = validate_path_segment(published_identifier, name="published_identifier")
        try:
            payload = self._request("GET", f"/api/v1/documents/{published_identifier}/drafts")
        except OmniAuthError as exc:
            exc.args = ("Document draft lookup was not authorized",)
            raise exc from None
        except OmniAPIError as exc:
            # The shared transport error can contain the requested API path.
            # Do not copy the document identifier into this adapter's errors.
            exc.args = ("Document draft lookup failed",)
            raise exc from None
        if not isinstance(payload, list):
            raise OmniAPIError("Document draft lookup returned an invalid response shape")
        return [_document_draft_metadata(record) for record in payload]

    def create_ai_job(self, model_id: str, *, branch_id: str, prompt: str) -> dict[str, str]:
        model_id = validate_path_segment(model_id, name="model_id")
        branch_id = validate_path_segment(branch_id, name="branch_id")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConfigError("AI repair prompt must be a non-empty string")
        prompt = prompt.strip()
        if len(prompt.encode("utf-8")) > MAX_AI_PROMPT_BYTES:
            raise ConfigError("AI repair prompt exceeds the 16 KiB safety limit")
        payload = self._request(
            "POST",
            "/api/v1/ai/jobs",
            json_payload={"modelId": model_id, "branchId": branch_id, "prompt": prompt},
            retry_transient=False,
        )
        if not isinstance(payload, dict):
            raise OmniAPIError("AI job creation returned an unexpected response shape")
        job_id = payload.get("jobId")
        if not isinstance(job_id, str) or not job_id.strip():
            raise OmniAPIError("AI job creation did not return a job ID")
        return {"job_id": validate_path_segment(job_id.strip(), name="job_id")}

    def get_ai_job_status(self, job_id: str) -> dict[str, str]:
        job_id = validate_path_segment(job_id, name="job_id")
        payload = self._request("GET", f"/api/v1/ai/jobs/{job_id}")
        if not isinstance(payload, dict):
            raise OmniAPIError("AI job status returned an unexpected response shape")
        response_job_id = payload.get("id")
        state = payload.get("state")
        if response_job_id != job_id:
            raise OmniAPIError("AI job status returned a mismatched job ID")
        if not isinstance(state, str) or state not in AI_JOB_STATES:
            raise OmniAPIError("AI job status returned an unsupported state")
        # Prompt, resultSummary, progress, and error can contain customer data and are intentionally discarded.
        return {"job_id": job_id, "state": state}

    def cancel_ai_job(self, job_id: str) -> dict[str, str]:
        job_id = validate_path_segment(job_id, name="job_id")
        payload = self._request("POST", f"/api/v1/ai/jobs/{job_id}/cancel")
        if not isinstance(payload, dict) or payload.get("jobId") != job_id:
            raise OmniAPIError("AI job cancellation returned an unexpected response shape")
        state = payload.get("state")
        if state not in {"CANCELLED", "COMPLETE", "FAILED"}:
            raise OmniAPIError("AI job cancellation did not return a terminal state")
        return {"job_id": job_id, "state": state}

    def get_ai_eval_prompt_set(self, prompt_set_id: str) -> dict[str, Any]:
        prompt_set_id = validate_path_segment(prompt_set_id, name="prompt_set_id")
        payload = self._request("GET", f"/api/v1/ai/eval/prompt-sets/{prompt_set_id}")
        if not isinstance(payload, dict) or not isinstance(payload.get("prompt_set"), dict):
            raise OmniAPIError("AI eval prompt set returned an unexpected response shape")
        prompt_set = payload["prompt_set"]
        if prompt_set.get("id") != prompt_set_id:
            raise OmniAPIError("AI eval prompt set returned a mismatched ID")
        if not isinstance(prompt_set.get("model_id"), str) or not prompt_set["model_id"].strip():
            raise OmniAPIError("AI eval prompt set did not identify its model")
        if not isinstance(prompt_set.get("prompts"), list):
            raise OmniAPIError("AI eval prompt set returned an unexpected prompts shape")
        return prompt_set

    def start_ai_eval_run(
        self,
        *,
        prompt_set_id: str,
        description: str,
        branch_id: str | None = None,
    ) -> str:
        prompt_set_id = validate_path_segment(prompt_set_id, name="prompt_set_id")
        if not isinstance(description, str) or not description.strip():
            raise ConfigError("AI eval run description must be a non-empty string")
        description = description.strip()
        if len(description) > MAX_AI_EVAL_DESCRIPTION_CHARS:
            raise ConfigError(
                f"AI eval run description must be {MAX_AI_EVAL_DESCRIPTION_CHARS} characters or fewer"
            )
        request_payload: dict[str, Any] = {"prompt_set_id": prompt_set_id, "description": description}
        if branch_id:
            request_payload["run_config"] = {"branch_id": validate_path_segment(branch_id, name="branch_id")}
        payload = self._request(
            "POST",
            "/api/v1/ai/eval/runs",
            json_payload=request_payload,
            retry_transient=False,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("run"), dict):
            raise OmniAPIError("AI eval run creation returned an unexpected response shape")
        run_id = payload["run"].get("id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise OmniAPIError("AI eval run creation did not return a run ID")
        return validate_path_segment(run_id.strip(), name="run_id")

    def get_ai_eval_run(self, run_id: str) -> dict[str, Any]:
        run_id = validate_path_segment(run_id, name="run_id")
        payload = self._request("GET", f"/api/v1/ai/eval/runs/{run_id}")
        if not isinstance(payload, dict) or not isinstance(payload.get("run"), dict):
            raise OmniAPIError("AI eval run status returned an unexpected response shape")
        run = payload["run"]
        if run.get("id") != run_id:
            raise OmniAPIError("AI eval run status returned a mismatched run ID")
        status = run.get("status")
        if not isinstance(status, str) or status not in AI_EVAL_STATES:
            raise OmniAPIError("AI eval run status returned an invalid status")
        results = run.get("results")
        if results is not None and not isinstance(results, list):
            raise OmniAPIError("AI eval run status returned an unexpected results shape")
        if isinstance(results, list) and any(not isinstance(row, dict) for row in results):
            raise OmniAPIError("AI eval run status returned a malformed result row")
        # Keep only documented comparison and recovery fields. Future response
        # additions must not silently persist query results or answer content.
        safe_run = {key: run[key] for key in ("id", "model_id", "prompt_set_id", "branch_id", "status") if key in run}
        safe_run["results"] = None if results is None else [
            {key: value for key, value in row.items()
             if key in {"id", "prompt", "score", "error_reason", "timing_ms", "query_count", "cost", "scoring_cost"}}
            for row in results
        ]
        for original, safe_row in zip(results or [], safe_run["results"] or [], strict=True):
            job = original.get("agentic_job")
            if isinstance(job, dict):
                safe_row["agentic_job"] = {
                    key: job[key] for key in ("id", "state", "conversation_id") if key in job
                }
        return safe_run

    def cancel_ai_eval_run(self, run_id: str) -> dict[str, Any]:
        run_id = validate_path_segment(run_id, name="run_id")
        payload = self._request(
            "POST", f"/api/v1/ai/eval/runs/{run_id}/cancel", retry_transient=False
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("run"), dict):
            raise OmniAPIError("AI eval cancellation returned an unexpected response shape")
        run = payload["run"]
        if run.get("id") != run_id or run.get("status") not in AI_EVAL_TERMINAL_STATES:
            raise OmniAPIError("AI eval cancellation did not confirm the requested run was terminal")
        return run

    def update_model_yaml(
        self,
        model_id: str,
        *,
        branch_id: str,
        file_name: str,
        yaml_text: str,
        previous_checksum: str | None,
        commit_message: str,
    ) -> dict[str, Any]:
        model_id = validate_path_segment(model_id, name="model_id")
        branch_id = validate_path_segment(branch_id, name="branch_id")
        file_name = validate_editable_yaml_file_name(file_name)
        if not isinstance(yaml_text, str):
            raise ConfigError("YAML repair content must be a string")
        request_payload: dict[str, Any] = {
            "fileName": file_name,
            "yaml": yaml_text,
            "mode": "combined",
            "branchId": branch_id,
            "commitMessage": _commit_message(commit_message),
        }
        if previous_checksum is not None:
            if not isinstance(previous_checksum, str) or not previous_checksum.strip():
                raise ConfigError("previous_checksum must be a non-empty string when provided")
            request_payload["previousChecksum"] = previous_checksum.strip()
        payload = self._request(
            "POST",
            f"/api/v1/models/{model_id}/yaml",
            json_payload=request_payload,
            retry_transient=False,
        )
        return _yaml_write_result(payload, file_name=file_name, operation="update")

    def delete_model_yaml(
        self,
        model_id: str,
        *,
        branch_id: str,
        file_name: str,
        commit_message: str,
    ) -> dict[str, Any]:
        model_id = validate_path_segment(model_id, name="model_id")
        branch_id = validate_path_segment(branch_id, name="branch_id")
        file_name = validate_editable_yaml_file_name(file_name, allow_special_files=False)
        payload = self._request(
            "DELETE",
            f"/api/v1/models/{model_id}/yaml",
            params={
                "fileName": file_name,
                "branchId": branch_id,
                "mode": "combined",
                "commitMessage": _commit_message(commit_message),
            },
            retry_transient=False,
        )
        return _yaml_write_result(payload, file_name=file_name, operation="delete")

    def commit_model_branch(
        self,
        model_id: str,
        *,
        branch_id: str,
        commit_message: str,
    ) -> dict[str, Any]:
        model_id = validate_path_segment(model_id, name="model_id")
        branch_id = validate_path_segment(branch_id, name="branch_id")
        payload = self._request(
            "POST",
            f"/api/v1/models/{model_id}/git/commit",
            json_payload={
                "branch_id": branch_id,
                "commit_message": _commit_message(commit_message),
                "allow_branch_exists": True,
                "require_branch_exists": True,
            },
            retry_transient=False,
        )
        if not isinstance(payload, dict):
            raise OmniAPIError("Git commit returned an unexpected response shape")
        git_sha = payload.get("git_sha")
        pr_url = payload.get("pr_url")
        if not isinstance(git_sha, str) or not git_sha.strip():
            raise OmniAPIError("Git commit did not return a commit SHA")
        if not isinstance(pr_url, str) or not pr_url.strip():
            raise OmniAPIError("Git commit did not return a pull request URL")
        return {
            "git_sha": git_sha.strip(),
            "pr_url": pr_url.strip(),
            "in_sync": bool(payload.get("in_sync")),
            "did_sync": bool(payload.get("did_sync")),
        }

    def _paginate(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        def failure(message: str, *, limit: bool = False) -> OmniAPIError:
            return OmniAPIError(
                message,
                reason_code="omni_pagination_limit_exceeded" if limit else "omni_pagination_invalid",
                diagnostic_context={"service": "omni", "api_operation": _api_operation(path), "retryable": False},
            )

        records: list[dict[str, Any]] = []
        record_count = 0
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_PAGINATION_PAGES):
            page_params = dict(params or {})
            page_params.setdefault("pageSize", 100)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._request("GET", path, params=page_params)
            if not isinstance(payload, Mapping):
                raise failure("Omni pagination returned an unexpected response shape")
            page_records = payload.get("records")
            if not isinstance(page_records, list):
                raise failure("Omni pagination returned an unexpected records shape")
            if any(not isinstance(item, dict) for item in page_records):
                raise failure("Omni pagination returned an invalid record")
            record_count += len(page_records)
            if record_count > MAX_PAGINATION_RECORDS:
                raise failure("Omni pagination exceeded the 50,000 record safety limit", limit=True)
            records.extend(page_records)
            # Preserve single-page compatibility when pageInfo is omitted;
            # explicit malformed metadata cannot establish a complete inventory.
            page_info = payload.get("pageInfo", {})
            if not isinstance(page_info, Mapping):
                raise failure("Omni pagination returned an unexpected pageInfo shape")
            if "hasNextPage" in page_info and not isinstance(page_info["hasNextPage"], bool):
                raise failure("Omni pagination returned an invalid hasNextPage flag")
            next_cursor = page_info.get("nextCursor")
            if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor.strip()):
                raise failure("Omni pagination returned an invalid cursor")
            cursor = next_cursor.strip() if isinstance(next_cursor, str) else None
            if "hasNextPage" in page_info and page_info["hasNextPage"] != bool(cursor):
                raise failure("Omni pagination returned inconsistent continuation metadata")
            if not cursor:
                return records
            if cursor in seen_cursors:
                raise failure("Omni pagination repeated a cursor and was stopped")
            seen_cursors.add(cursor)
        raise failure("Omni pagination exceeded the 500 page safety limit", limit=True)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        retry_transient: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        budget = self._read_budget
        if budget is not None:
            if method != "GET":
                raise ConfigError("Read-only analysis cannot make write requests.")
            retry_transient = False
        attempts = 4 if retry_transient else 1
        for attempt in range(attempts):
            timeout = min(self.timeout, budget.consume()) if budget is not None else self.timeout
            context = {
                "service": "omni", "api_operation": _api_operation(path),
                "attempt_count": attempt + 1, "max_attempts": attempts,
            }
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_payload,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == attempts - 1:
                    reason = _request_failure_reason(exc)
                    raise OmniAPIError(
                        "Omni API request could not complete; review the connection failure category.",
                        reason_code=reason,
                        diagnostic_context={**context, "retryable": retry_transient and reason != "omni_tls_error"},
                    ) from exc
                time.sleep(2**attempt)
                continue

            if response.status_code in {401, 403}:
                _close_response(response)
                raise OmniAuthError(
                    f"Omni authorization failed: {response.status_code}",
                    reason_code="omni_unauthorized" if response.status_code == 401 else "omni_forbidden",
                    diagnostic_context={**context, "http_status": response.status_code, "retryable": False},
                )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < attempts - 1:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    _close_response(response)
                    time.sleep(retry_after if retry_after is not None else 2**attempt)
                    continue
            if not response.ok:
                _close_response(response)
                reason = ("omni_rate_limited" if response.status_code == 429 else
                          "omni_service_unavailable" if 500 <= response.status_code < 600 else "omni_http_error")
                raise OmniAPIError(
                    f"Omni API request failed: HTTP {response.status_code}", reason_code=reason,
                    diagnostic_context={
                        **context, "http_status": response.status_code,
                        "retryable": retry_transient and (response.status_code == 429 or 500 <= response.status_code < 600),
                    },
                )
            try:
                if budget is not None:
                    payload = _bounded_response_json(response, deadline=budget.deadline)
                    budget.remaining()
                    return payload
                return _bounded_response_json(response)
            except OmniAPIError as exc:
                exc.diagnostic_context.update({
                    **context, "http_status": response.status_code,
                    "retryable": retry_transient and getattr(exc, "reason_code", None) == "omni_response_interrupted",
                })
                raise
            finally:
                _close_response(response)

        LOG.debug("Final Omni request error: %s", redact(str(last_error)))
        raise OmniAPIError("Omni API request failed after retries", reason_code="omni_request_failed")


def _request_failure_reason(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.exceptions.SSLError):
        return "omni_tls_error"
    if isinstance(exc, requests.Timeout):
        return "omni_timeout"
    if isinstance(exc, requests.ConnectionError):
        return "omni_connection_error"
    return "omni_request_failed"


def _api_operation(path: str) -> str:
    """Only fixed labels enter diagnostics, never endpoint identifiers or queries."""
    parts = path.split("?", 1)[0].strip("/").split("/")
    if parts[:3] == ["api", "v1", "models"]:
        if len(parts) == 3:
            return "model_list"
        if len(parts) == 5:
            return {"yaml": "model_yaml", "validate": "model_validation", "content-validator": "content_validation",
                    "git": "git_configuration", "dbt-exposures": "dbt_exposures", "refresh": "schema_refresh"}.get(
                        parts[4], "omni_request")
        if parts[4:] == ["git", "commit"]:
            return "git_commit"
    if parts == ["api", "v1", "content"]:
        return "content_list"
    if parts == ["api", "v1", "whoami"]:
        return "identity_lookup"
    if parts[:3] == ["api", "v1", "documents"] and len(parts) == 5 and parts[4] == "drafts":
        return "document_drafts"
    if parts[:3] == ["api", "v1", "documents"] and len(parts) == 5 and parts[4] == "queries":
        return "document_queries"
    if parts[:3] == ["api", "v2", "documents"] and len(parts) == 4:
        return "document_state"
    if parts[:3] == ["api", "v1", "jobs"] and len(parts) == 5 and parts[4] == "status":
        return "job_status"
    if parts[:4] == ["api", "v1", "ai", "jobs"]:
        return "ai_job" if len(parts) == 4 else "ai_job_cancel" if parts[-1] == "cancel" else "ai_job_status"
    if parts[:5] == ["api", "v1", "ai", "eval", "prompt-sets"]:
        return "ai_prompt_set"
    if parts[:5] == ["api", "v1", "ai", "eval", "runs"]:
        return "ai_eval_run" if len(parts) == 5 else "ai_eval_cancel" if parts[-1] == "cancel" else "ai_eval_status"
    return "omni_request"


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, min(60, int(value)))
    except ValueError:
        return None


def _document_draft_metadata(record: Any) -> dict[str, Any]:
    required = {"identifier", "publishedIdentifier", "branch", "status", "draftOutOfDate", "updatedAt"}
    if not isinstance(record, Mapping) or not required.issubset(record):
        raise OmniAPIError("Document draft lookup returned invalid metadata")
    if any(not isinstance(record[key], str) or not record[key].strip()
           for key in ("identifier", "publishedIdentifier", "updatedAt")):
        raise OmniAPIError("Document draft lookup returned invalid metadata")
    if record["status"] not in ("active", "archived") or type(record["draftOutOfDate"]) is not bool:
        raise OmniAPIError("Document draft lookup returned invalid metadata")
    branch = record["branch"]
    if branch is not None and (
        not isinstance(branch, Mapping) or not isinstance(branch.get("id"), str) or not branch["id"].strip()
    ):
        raise OmniAPIError("Document draft lookup returned invalid metadata")
    return {
        "identifier": record["identifier"],
        "publishedIdentifier": record["publishedIdentifier"],
        "branch": {"id": branch["id"]} if branch is not None else None,
        "status": record["status"],
        "draftOutOfDate": record["draftOutOfDate"],
        "updatedAt": record["updatedAt"],
    }


def _validate_content_response_identity(payload: Any, *, model_id: str, branch_id: str | None) -> None:
    """Reject contradictory optional identity metadata shared by validation and searches.

    The API documents model_id and branch {id, name} | null. Omitted metadata
    cannot establish identity, but must not be confused with explicit main/null.
    Content envelope validation remains with the existing content parser.
    """
    if not isinstance(payload, dict):
        return
    if "model_id" in payload:
        actual_model_id = payload["model_id"]
        if not isinstance(actual_model_id, str) or not actual_model_id.strip():
            raise OmniAPIError("Content Validator response contains an invalid model identity")
        if actual_model_id != model_id:
            raise OmniAPIError("Content Validator response identifies a different model")
    if "branch" not in payload:
        return
    branch = payload["branch"]
    if branch is None:
        if branch_id is not None:
            raise OmniAPIError("Content Validator response identifies main instead of the requested branch")
        return
    if not isinstance(branch, dict):
        raise OmniAPIError("Content Validator response contains invalid branch metadata")
    if branch_id is None:
        raise OmniAPIError("Content Validator response identifies a branch instead of main")
    if "id" in branch:
        actual_branch_id = branch["id"]
        if not isinstance(actual_branch_id, str) or not actual_branch_id.strip():
            raise OmniAPIError("Content Validator response contains an invalid branch identity")
        if actual_branch_id != branch_id:
            raise OmniAPIError("Content Validator response identifies a different branch")


def _content_validator_find_type(value: str) -> str:
    normalized = value.strip().upper()
    aliases = {
        "FIELD": "FIELD",
        "VIEW": "VIEW",
        "TOPIC": "TOPIC",
    }
    if normalized not in aliases:
        raise ConfigError("Content Validator find_type must be VIEW, FIELD, or TOPIC")
    return aliases[normalized]


def _schema_refresh_status(value: Any) -> str:
    if not isinstance(value, str):
        raise OmniAPIError("Schema refresh returned an invalid status")
    normalized = value.strip().upper()
    if normalized not in SCHEMA_REFRESH_STATES:
        raise OmniAPIError("Schema refresh returned an unsupported status")
    return normalized


def _commit_message(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("Omni write commit message must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 500 or any(character in normalized for character in ("\x00", "\r", "\n")):
        raise ConfigError("Omni write commit message must be one line and no longer than 500 characters")
    return normalized


def _yaml_write_result(payload: Any, *, file_name: str, operation: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("fileName") != file_name:
        raise OmniAPIError(f"YAML {operation} returned an unexpected response shape")
    return {"file_name": file_name, "success": True}


def _bounded_response_json(response: Any, *, deadline: float | None = None) -> Any:
    content_length = response.headers.get("Content-Length") if isinstance(response.headers, Mapping) else None
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise OmniAPIError("Omni API response exceeded the 64 MiB safety limit", reason_code="omni_response_limit_exceeded")
        except ValueError:
            pass

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        body = bytearray()
        try:
            for chunk in iter_content(chunk_size=64 * 1024):
                if deadline is not None and time.monotonic() >= deadline:
                    raise OmniAPIError("Read-only analysis reached its time limit.",
                                       reason_code="content_impact_timeout")
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise OmniAPIError("Omni API response exceeded the 64 MiB safety limit", reason_code="omni_response_limit_exceeded")
        except requests.RequestException as exc:
            raise OmniAPIError(
                "Omni API response could not be read safely", reason_code="omni_response_interrupted",
                diagnostic_context={"cause_code": _request_failure_reason(exc)},
            ) from exc
        if not body:
            raise OmniResponseError("empty_api_response")
        try:
            return json.loads(body, object_pairs_hook=reject_duplicate_keys)
        except (UnicodeDecodeError, ValueError) as exc:
            raise OmniResponseError("invalid_json_response") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise OmniResponseError("invalid_json_response") from exc
    try:
        encoded_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise OmniResponseError("invalid_json_response") from exc
    if encoded_size > MAX_RESPONSE_BYTES:
        raise OmniAPIError("Omni API response exceeded the 64 MiB safety limit", reason_code="omni_response_limit_exceeded")
    return payload


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()
