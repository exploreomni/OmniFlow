from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from ..exceptions import ConfigError, OmniAPIError, SecurityPolicyError
from ..git import github_event_payload
from ..security import validate_branch_name

REPAIR_LABEL = "omniflow-ai-repair"
ATTEMPT_MARKER_PREFIX = "<!-- omniflow-ai-repair-attempt sha="
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_COMMENT_PAGES = 10
MAX_GITHUB_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RepairEventContext:
    repository: str
    pull_request_number: int
    head_sha: str
    head_branch: str
    base_branch: str
    actor: str

    @property
    def marker(self) -> str:
        return f"{ATTEMPT_MARKER_PREFIX}{self.head_sha} -->"


def load_repair_event() -> RepairEventContext:
    if os.getenv("GITHUB_ACTIONS") != "true":
        raise SecurityPolicyError("AI repair runs only inside the protected GitHub Actions workflow")
    if os.getenv("GITHUB_EVENT_NAME") != "pull_request_target":
        raise SecurityPolicyError("AI repair runs only from a pull_request_target label event")
    event = github_event_payload()
    label = event.get("label") if isinstance(event.get("label"), dict) else {}
    if event.get("action") != "labeled" or label.get("name") != REPAIR_LABEL:
        raise SecurityPolicyError(f"AI repair requires the {REPAIR_LABEL} pull request label event")

    repository = event.get("repository") if isinstance(event.get("repository"), dict) else {}
    repository_name = repository.get("full_name")
    pull_request = event.get("pull_request") if isinstance(event.get("pull_request"), dict) else {}
    head = pull_request.get("head") if isinstance(pull_request.get("head"), dict) else {}
    base = pull_request.get("base") if isinstance(pull_request.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    number = event.get("number") or pull_request.get("number")

    if not isinstance(repository_name, str) or not REPOSITORY_RE.fullmatch(repository_name):
        raise ConfigError("Repair event did not contain a valid repository name")
    if os.getenv("GITHUB_REPOSITORY") and os.getenv("GITHUB_REPOSITORY") != repository_name:
        raise SecurityPolicyError("Repair event repository does not match the GitHub workflow repository")
    if head_repo.get("full_name") != repository_name:
        raise SecurityPolicyError("AI repair is disabled for fork pull requests")
    if not isinstance(number, int) or number < 1:
        raise ConfigError("Repair event did not contain a valid pull request number")

    head_sha = head.get("sha")
    head_branch = head.get("ref")
    base_branch = base.get("ref")
    actor = sender.get("login")
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        raise ConfigError("Repair event did not contain a valid pull request head SHA")
    if not isinstance(head_branch, str) or not head_branch:
        raise ConfigError("Repair event did not contain a pull request head branch")
    if not isinstance(base_branch, str) or not base_branch:
        raise ConfigError("Repair event did not contain a pull request base branch")
    head_branch = validate_branch_name(head_branch)
    base_branch = validate_branch_name(base_branch)
    if head_branch == base_branch:
        raise SecurityPolicyError("AI repair cannot run against the pull request base branch")
    if not isinstance(actor, str) or not actor.strip():
        raise ConfigError("Repair event did not identify the actor who applied the label")
    if os.getenv("GITHUB_ACTOR") and os.getenv("GITHUB_ACTOR") != actor.strip():
        raise SecurityPolicyError("Repair event actor does not match the GitHub workflow actor")
    return RepairEventContext(
        repository=repository_name,
        pull_request_number=number,
        head_sha=head_sha,
        head_branch=head_branch,
        base_branch=base_branch,
        actor=actor.strip(),
    )


class GitHubRepairAttemptGuard:
    def __init__(
        self,
        *,
        token: str,
        api_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if not isinstance(token, str) or not token:
            raise ConfigError("OMNIFLOW_GITHUB_TOKEN is required to enforce one AI repair attempt per head SHA")
        self.api_url = _github_api_url(api_url or os.getenv("GITHUB_API_URL", "https://api.github.com"))
        self.session = session or requests.Session()
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def claim(self, context: RepairEventContext) -> int:
        comments_path = f"/repos/{context.repository}/issues/{context.pull_request_number}/comments"
        for page in range(1, MAX_COMMENT_PAGES + 1):
            payload = self._request("GET", comments_path, params={"per_page": 100, "page": page})
            if not isinstance(payload, list):
                raise OmniAPIError("GitHub repair-attempt lookup returned an unexpected response shape")
            for comment in payload:
                if not isinstance(comment, dict):
                    continue
                user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
                body = comment.get("body")
                if user.get("login") == "github-actions[bot]" and isinstance(body, str) and context.marker in body:
                    raise SecurityPolicyError("OmniFlow AI repair was already attempted for this pull request head SHA")
            if len(payload) < 100:
                break
        else:
            raise SecurityPolicyError(
                "GitHub repair-attempt history exceeded the bounded comment scan; duplicate safety is unproven"
            )

        created = self._request(
            "POST",
            comments_path,
            json_payload={
                "body": (
                    f"{context.marker}\n"
                    f"OmniFlow AI repair started for `{context.head_sha}` after `{REPAIR_LABEL}` was applied."
                )
            },
        )
        comment_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(comment_id, int) or comment_id < 1:
            raise OmniAPIError("GitHub did not return an AI repair attempt comment ID")
        return comment_id

    def complete(self, context: RepairEventContext, *, comment_id: int, status: str) -> None:
        status_messages = {
            "committed": "passed every configured gate and updated the existing pull request",
            "not_needed": "found no model validation errors and made no changes",
            "rolled_back": "was rejected or failed validation and was rolled back",
            "failed": "failed before a safe pull request update could be made",
            "manual_review_required": "stopped and requires manual review before any merge or deployment",
        }
        if status not in status_messages:
            raise ConfigError("Unsupported AI repair attempt status")
        if not isinstance(comment_id, int) or comment_id < 1:
            raise ConfigError("AI repair attempt comment ID must be a positive integer")
        self._request(
            "PATCH",
            f"/repos/{context.repository}/issues/comments/{comment_id}",
            json_payload={"body": f"{context.marker}\nOmniFlow AI repair {status_messages[status]}."},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self.session.request(
                method,
                f"{self.api_url}{path}",
                headers=self.headers,
                params=params,
                json=json_payload,
                timeout=30,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise OmniAPIError("GitHub repair-attempt request failed") from exc
        if not response.ok:
            raise OmniAPIError(f"GitHub repair-attempt request failed with HTTP {response.status_code}")
        content_length = getattr(response, "headers", {}).get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_GITHUB_RESPONSE_BYTES:
                    raise OmniAPIError("GitHub repair-attempt response exceeded the 8 MiB safety limit")
            except ValueError:
                pass
        try:
            payload = response.json()
        except ValueError as exc:
            raise OmniAPIError("GitHub repair-attempt request returned invalid JSON") from exc
        try:
            encoded_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise OmniAPIError("GitHub repair-attempt request returned an invalid JSON value") from exc
        if encoded_size > MAX_GITHUB_RESPONSE_BYTES:
            raise OmniAPIError("GitHub repair-attempt response exceeded the 8 MiB safety limit")
        return payload


def _github_api_url(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SecurityPolicyError("GITHUB_API_URL must be a trusted HTTPS origin")
    return normalized
