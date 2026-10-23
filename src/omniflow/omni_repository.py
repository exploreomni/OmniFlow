"""An opt-in, immutable, read-only Omni source for a dbt repository.

No checkout, execution, credential fallback, or process-global source cache.
The ordinary workflow token still reads the dbt PR; this client can only read
the configured Omni repository. Mutable branch names never reach blob reads.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from urllib.parse import quote

from .config import OmniRepositorySettings
from .exceptions import ConfigError, OmniFlowError, SecurityPolicyError
from .git import event_name, is_pull_request_event
from .revision_data import FULL_SHA, github_tree, pull_request_revision, read_github_text

SNAPSHOT_FIELDS = ("source_fingerprint", "omni_commit_sha", "dbt_base_sha", "dbt_head_sha")


def trusted_source_context() -> bool:
    """Reject forks and unsupported Actions triggers before reading credentials."""
    if is_pull_request_event():
        try:
            base, head = pull_request_revision("base"), pull_request_revision("head")
            return bool(os.getenv("GITHUB_BASE_REF") and base and head
                        and base[1] == head[1] == os.getenv("GITHUB_REPOSITORY"))
        except OmniFlowError:
            return False
    return os.getenv("GITHUB_ACTIONS") != "true" or event_name() in {"push", "workflow_dispatch"}


def public_snapshot(value: object) -> dict:
    """Only exact hexadecimal receipt values; never source names or paths."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in SNAPSHOT_FIELDS:
        item = value.get(name)
        if isinstance(item, str) and FULL_SHA.fullmatch(item):
            result[name] = item.lower()
    return result


@dataclass
class OmniRepositorySnapshot:
    settings: OmniRepositorySettings
    sha: str
    api: object = field(repr=False)
    _tree: dict | None = field(default=None, repr=False)
    _texts: dict = field(default_factory=dict, repr=False)

    @classmethod
    def resolve(cls, settings: OmniRepositorySettings, *, expected: dict | None = None):
        if not trusted_source_context():
            raise SecurityPolicyError("External Omni data is unavailable in this untrusted context",
                                      reason_code="omni_repository_context_untrusted")
        token = os.getenv("OMNIFLOW_OMNI_REPO_TOKEN", "")
        if not token:
            raise ConfigError("Set the separate read-only Omni repository credential",
                              reason_code="omni_repository_credentials_unavailable")
        from .github.revalidation import GitHubRepository

        # This credential is never substituted for OMNIFLOW_GITHUB_TOKEN.
        api = GitHubRepository(settings.repository, token)
        instance = cls(settings, "", api)
        instance.sha = instance._branch_sha()
        if expected is not None and public_snapshot(expected) != instance.evidence():
            raise ConfigError("Omni source no longer matches this run's static validation snapshot",
                              reason_code="omni_repository_snapshot_changed")
        return instance

    def _branch_sha(self) -> str:
        try:
            value = self.api.request("GET", f"/git/ref/heads/{quote(self.settings.ref, safe='')}")
            obj = value.get("object") if isinstance(value, dict) else None
            if (not isinstance(obj, dict) or obj.get("type") != "commit"
                    or not isinstance(obj.get("sha"), str) or not FULL_SHA.fullmatch(obj["sha"])
                    or value.get("ref") != f"refs/heads/{self.settings.ref}"):
                raise ValueError("Not an exact branch commit")
            return obj["sha"].lower()
        except (OmniFlowError, ValueError, TypeError) as exc:
            raise ConfigError("The external Omni branch could not be resolved; impact coverage is unavailable",
                              reason_code="omni_repository_snapshot_unavailable") from exc

    def assert_unchanged(self) -> None:
        if self._branch_sha() != self.sha:
            raise ConfigError("The external Omni branch moved during analysis; run a fresh comparison",
                              reason_code="omni_repository_snapshot_changed")

    @property
    def identity(self) -> tuple[str, str]:
        return self.settings.repository, self.sha

    @property
    def tree(self) -> dict:
        if self._tree is None:
            self._tree = github_tree(self.api, self.sha)
        return self._tree

    def read_text(self, path: str, *, max_bytes: int) -> str | None:
        key = (path, max_bytes)
        if key not in self._texts:
            self._texts[key] = read_github_text(self.settings.repository, self.sha, path,
                                               max_bytes=max_bytes, api=self.api, tree=self.tree)
        return self._texts[key]

    def evidence(self) -> dict:
        identity = json.dumps([self.settings.repository, self.settings.ref, self.settings.model_paths],
                              separators=(",", ":"))
        result = {"source_fingerprint": hashlib.sha256(identity.encode()).hexdigest(),
                  "omni_commit_sha": self.sha}
        for side in ("base", "head"):
            revision = pull_request_revision(side)
            if revision:
                result[f"dbt_{side}_sha"] = revision[0].lower()
        return result


def recorded_snapshot(report: dict) -> dict:
    """The caller verifies the current-run validation receipt before using this."""
    records = [item.get("dbt_impact_snapshot") for item in report.get("model_reports", [])
               if isinstance(item, dict) and item.get("validator") == "dbt_impact"]
    if len(records) != 1 or not isinstance(records[0], dict) or not records[0].get("omni_commit_sha"):
        raise ConfigError("A fresh static external-snapshot report is required before content lookup",
                          reason_code="omni_repository_receipt_unavailable")
    return records[0]
