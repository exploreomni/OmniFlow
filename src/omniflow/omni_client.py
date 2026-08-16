from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

import requests

from . import __version__
from .exceptions import ConfigError, OmniAPIError, OmniAuthError
from .model_yaml import validate_editable_yaml_file_name
from .security import redact, validate_base_url, validate_path_segment

LOG = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_PAGINATION_PAGES = 500
MAX_PAGINATION_RECORDS = 50_000
MAX_AI_PROMPT_BYTES = 16 * 1024
AI_JOB_STATES = {"CANCELLED", "COMPLETE", "DELIVERING", "EXECUTING", "FAILED", "QUEUED"}
SCHEMA_REFRESH_STATES = {"RUNNING", "COMPLETED", "FAILED"}
SCHEMA_REFRESH_MODEL_KINDS = {"SHARED", "SHARED_EXTENSION"}


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
        if not isinstance(payload, list):
            raise OmniAPIError("Model validation returned an unexpected response shape")
        return [item for item in payload if isinstance(item, dict)]

    def validate_content(
        self,
        model_id: str,
        branch_id: str | None = None,
        user_id: str | None = None,
        include_personal_folders: bool = False,
        find: str | None = None,
        find_type: str | None = None,
    ) -> Any:
        model_id = validate_path_segment(model_id, name="model_id")
        params: dict[str, Any] = {}
        if user_id:
            params["userId"] = user_id
        if branch_id:
            params["branch_id"] = branch_id
        if include_personal_folders:
            params["include_personal_folders"] = "true"
        if find:
            params["find"] = find
        if find_type:
            params["find_type"] = _content_validator_find_type(find_type)
        return self._request("GET", f"/api/v1/models/{model_id}/content-validator", params=params)

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
        for record in self.list_models(model_kind="BRANCH", base_model_id=model_id, name=branch_name):
            if record.get("modelKind") != "BRANCH":
                continue
            if record.get("baseModelId") != model_id:
                continue
            if record.get("name") == branch_name:
                return record.get("id")
        return None

    def list_content(
        self,
        labels: list[str] | None = None,
        branch_id: str | None = None,
        include_personal_folders: bool = False,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # Content metadata is not branch-scoped in the documented API.
        del branch_id
        if include_personal_folders and labels and not user_id:
            raise ConfigError("OMNI_USER_ID is required to apply label filtering to personal-folder content safely")
        params: dict[str, Any] = {"include": "labels", "scope": "organization"}
        if labels:
            params["labels"] = ",".join(labels)
        records = self._paginate("/api/v1/content", params=params)
        if include_personal_folders and user_id:
            restricted_params = dict(params)
            restricted_params.update({"scope": "restricted", "creatorId": user_id})
            records.extend(self._paginate("/api/v1/content", params=restricted_params))
        return records

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
            page_records = payload.get("records", []) if isinstance(payload, dict) else []
            if not isinstance(page_records, list):
                raise OmniAPIError("Omni pagination returned an unexpected records shape")
            record_count += len(page_records)
            if record_count > MAX_PAGINATION_RECORDS:
                raise OmniAPIError("Omni pagination exceeded the 50,000 record safety limit")
            records.extend(item for item in page_records if isinstance(item, dict))
            page_info = payload.get("pageInfo", {}) if isinstance(payload, dict) else {}
            if not isinstance(page_info, dict):
                raise OmniAPIError("Omni pagination returned an unexpected pageInfo shape")
            next_cursor = page_info.get("nextCursor")
            if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor.strip()):
                raise OmniAPIError("Omni pagination returned an invalid cursor")
            cursor = next_cursor.strip() if isinstance(next_cursor, str) else None
            if not cursor:
                return records
            if cursor in seen_cursors:
                raise OmniAPIError("Omni pagination repeated a cursor and was stopped")
            seen_cursors.add(cursor)
        raise OmniAPIError("Omni pagination exceeded the 500 page safety limit")

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
        attempts = 4 if retry_transient else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_payload,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == attempts - 1:
                    raise OmniAPIError(f"Omni API request failed: {redact(str(exc))}") from exc
                time.sleep(2**attempt)
                continue

            if response.status_code in {401, 403}:
                _close_response(response)
                raise OmniAuthError(f"Omni authorization failed: {response.status_code}")
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < attempts - 1:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    _close_response(response)
                    time.sleep(retry_after if retry_after is not None else 2**attempt)
                    continue
            if not response.ok:
                _close_response(response)
                raise OmniAPIError(f"Omni API request failed: HTTP {response.status_code} for {method} {path}")
            try:
                return _bounded_response_json(response)
            finally:
                _close_response(response)

        LOG.debug("Final Omni request error: %s", redact(str(last_error)))
        raise OmniAPIError("Omni API request failed after retries")


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, min(60, int(value)))
    except ValueError:
        return None


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


def _bounded_response_json(response: Any) -> Any:
    content_length = response.headers.get("Content-Length") if isinstance(response.headers, Mapping) else None
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise OmniAPIError("Omni API response exceeded the 64 MiB safety limit")
        except ValueError:
            pass

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        body = bytearray()
        try:
            for chunk in iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise OmniAPIError("Omni API response exceeded the 64 MiB safety limit")
        except requests.RequestException as exc:
            raise OmniAPIError("Omni API response could not be read safely") from exc
        try:
            return json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise OmniAPIError("Omni API did not return valid JSON") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise OmniAPIError("Omni API did not return valid JSON") from exc
    try:
        encoded_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise OmniAPIError("Omni API did not return a valid JSON value") from exc
    if encoded_size > MAX_RESPONSE_BYTES:
        raise OmniAPIError("Omni API response exceeded the 64 MiB safety limit")
    return payload


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()
