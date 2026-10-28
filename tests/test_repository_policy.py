import copy
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow import repository_policy
from omniflow.config import OmniRepositorySettings, _to_config, load_config
from omniflow.exceptions import ConfigError
from omniflow.repository_policy import POLICY_REASONS, RepositoryPolicy, resolve_repository_policy

REPOSITORY = "synthetic/private-repo"
HEAD = "a" * 40
BASE = "b" * 40
REPO = {"id": 123, "full_name": REPOSITORY}


@pytest.fixture
def context(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("GITHUB_", "OMNIFLOW_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_HEAD_REF": "synthetic-change", "GITHUB_BASE_REF": "main",
        "GITHUB_SHA": BASE, "OMNIFLOW_GITHUB_TOKEN": "synthetic-test-token",
    }.items():
        monkeypatch.setenv(name, value)
    pr = {"number": 7, "state": "open", "head": {
        "sha": HEAD, "ref": "synthetic-change", "repo": dict(REPO),
    }, "base": {"sha": BASE, "ref": "main", "repo": dict(REPO)}}
    event = {"number": 7, "repository": dict(REPO), "pull_request": copy.deepcopy(pr)}
    metadata = {**REPO, "private": True, "visibility": "private", "default_branch": "main"}
    state = SimpleNamespace(
        event=event, metadata=metadata, pr=pr, trusted_text="checks: {}\n",
        checkout=BASE, root=tmp_path, branch_sha=BASE, calls=[], policy_reads=[],
    )
    monkeypatch.setattr(repository_policy, "github_event_payload", lambda: state.event)

    def request(api, method, path, **kwargs):
        state.calls.append((method, path, kwargs))
        assert method == "GET", "The disclosure gate must never mutate GitHub"
        assert kwargs["max_bytes"] == repository_policy.MAX_METADATA_BYTES
        if path == "":
            return copy.deepcopy(state.metadata)
        if path == "/pulls/7":
            return copy.deepcopy(state.pr)
        if path == "/git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": state.branch_sha}}
        raise AssertionError("Unexpected GitHub request")

    def read_policy(repository, sha, path, *, max_bytes):
        state.policy_reads.append((repository, sha, path, max_bytes))
        return state.trusted_text

    def checkout_value(*args):
        if args == ("rev-parse", "HEAD"):
            return state.checkout
        if args == ("rev-parse", "--show-toplevel"):
            return str(state.root)
        raise AssertionError("Unexpected Git command")

    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository.request", request)
    monkeypatch.setattr(repository_policy, "read_github_text", read_policy)
    monkeypatch.setattr(repository_policy, "_checkout_value", checkout_value)
    state.config = _to_config({}, Path(".omniflow.yml"))
    return state


def assert_minimal(policy, reason):
    assert policy == RepositoryPolicy(False, reason)
    assert policy.reason in POLICY_REASONS


def test_config_defaults_to_repository_and_has_literal_minimal_optout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _to_config({}, None).reporting.diagnostics_mode == "repository"
    path = tmp_path / ".omniflow.yml"
    path.write_text("reporting:\n  diagnostics:\n    mode: minimal\n")
    assert load_config(path).reporting.diagnostics_mode == "minimal"


@pytest.mark.parametrize("value", [
    {"mode": "unknown-sensitive-value"}, {"mode": "${PRIVATE_MODE}"}, {"mode": True},
    {"mode": None}, {"mode": []}, {"unknown-sensitive-key": "private"}, [], None,
])
def test_invalid_diagnostic_config_is_rejected_without_echoing_supplied_values(value, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_MODE", "repository")
    path = tmp_path / ".omniflow.yml"
    path.write_text(json.dumps({"reporting": {"diagnostics": value}}))
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "unknown-sensitive" not in str(error.value)
    assert "${PRIVATE_MODE}" not in str(error.value)


@pytest.mark.parametrize("event_name", ["pull_request_target", "pull_request"])
def test_default_eligibility_rechecks_private_repository_and_exact_same_repo_pr(context, monkeypatch, event_name):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    result = resolve_repository_policy(context.config)
    assert result == RepositoryPolicy(True, "repository_verified", REPOSITORY, HEAD, BASE)
    assert [path for _, path, _ in context.calls] == ["", "/pulls/7"]
    assert context.policy_reads == [(REPOSITORY, BASE, ".omniflow.yml", 1024 * 1024)]
    with pytest.raises(FrozenInstanceError):
        result.enabled = False


def test_trusted_minimal_overrides_local_head_repository_mode(context):
    context.trusted_text = "reporting:\n  diagnostics:\n    mode: minimal\n"
    Path(".omniflow.yml").write_text("reporting:\n  diagnostics:\n    mode: repository\n")
    assert_minimal(resolve_repository_policy(context.config), "minimal_requested")
    assert context.policy_reads[0][1] == BASE


def test_caller_can_only_narrow_trusted_policy(context):
    context.config.reporting.diagnostics_mode = "minimal"
    assert_minimal(resolve_repository_policy(context.config), "minimal_requested")
    assert context.calls == []


def _external_policy(allow=True):
    return {"repository": "source/models", "ref": "published", "model_paths": ["omni"],
            "allow_repository_diagnostics": allow}


def test_external_disclosure_requires_matching_trusted_literal_opt_in(context):
    source = _external_policy()
    context.config.dbt_impact.omni_repository = OmniRepositorySettings(**source)
    context.trusted_text = json.dumps({"checks": {"dbt_impact": {"omni_repository": source}}})
    policy = resolve_repository_policy(context.config)
    assert policy.enabled and policy.external_diagnostics_allowed is True
    assert context.policy_reads == [(REPOSITORY, BASE, ".omniflow.yml", 1024 * 1024)]
    assert [path for _, path, _ in context.calls] == ["", "/pulls/7"]


@pytest.mark.parametrize("trusted", [None, _external_policy(False),
                                      {key: value for key, value in _external_policy().items()
                                       if key != "allow_repository_diagnostics"}])
def test_caller_cannot_enable_external_disclosure_missing_from_trusted_base(context, trusted):
    context.config.dbt_impact.omni_repository = OmniRepositorySettings(**_external_policy())
    context.trusted_text = json.dumps({"checks": {"dbt_impact": {"omni_repository": trusted}}})
    # The working copy is not an authority for the re-read disclosure decision.
    Path(".omniflow.yml").write_text(json.dumps({"checks": {"dbt_impact": {"omni_repository": _external_policy()}}}))
    policy = resolve_repository_policy(context.config)
    assert policy.enabled and policy.external_diagnostics_allowed is False
    assert context.policy_reads[0][1] == BASE


@pytest.mark.parametrize("change", [
    {"repository": "another/models"}, {"ref": "other"}, {"model_paths": ["other"]},
    {"model_paths": ["omni", "another"]}, {"allow_repository_diagnostics": False},
])
def test_external_disclosure_cannot_redirect_or_broaden_trusted_source(context, change):
    context.trusted_text = json.dumps({"checks": {"dbt_impact": {"omni_repository": _external_policy()}}})
    context.config.dbt_impact.omni_repository = OmniRepositorySettings(**(_external_policy() | change))
    policy = resolve_repository_policy(context.config)
    assert policy.enabled and policy.external_diagnostics_allowed is False


@pytest.mark.parametrize("change", [
    {"allow_repository_diagnostics": "${ALLOW_DETAILS}"}, {"ref": "${SOURCE_REF}"},
    {"repository": "${SOURCE_REPOSITORY}"},
])
def test_external_opt_in_is_never_authorized_by_environment_interpolation(context, monkeypatch, change):
    for name, value in {"ALLOW_DETAILS": "true", "SOURCE_REF": "published", "SOURCE_REPOSITORY": "source/models"}.items():
        monkeypatch.setenv(name, value)
    context.config.dbt_impact.omni_repository = OmniRepositorySettings(**_external_policy())
    context.trusted_text = json.dumps({"checks": {"dbt_impact": {"omni_repository": _external_policy() | change}}})
    assert_minimal(resolve_repository_policy(context.config), "policy_invalid")


def test_external_opt_in_still_requires_live_private_same_repository_pr(context):
    context.config.dbt_impact.omni_repository = OmniRepositorySettings(**_external_policy())
    context.trusted_text = json.dumps({"checks": {"dbt_impact": {"omni_repository": _external_policy()}}})
    context.pr["head"]["repo"] = {"full_name": "external/fork", "id": 456}
    policy = resolve_repository_policy(context.config)
    assert_minimal(policy, "fork_pull_request")
    assert policy.external_diagnostics_allowed is False and context.policy_reads == []


def test_absolute_in_repo_config_still_reads_base_not_local_file(context):
    path = context.root / ".omniflow.yml"
    path.write_text("reporting:\n  diagnostics:\n    mode: repository\n")
    context.config = load_config(path)
    context.trusted_text = "reporting:\n  diagnostics:\n    mode: minimal\n"
    assert_minimal(resolve_repository_policy(context.config), "minimal_requested")
    assert context.policy_reads[0][1:3] == (BASE, ".omniflow.yml")


@pytest.mark.parametrize("source", ["../outside.yml", "/tmp/not-the-checkout/policy.yml"])
def test_policy_path_cannot_escape_repository(context, source):
    context.config.source = Path(source)
    assert_minimal(resolve_repository_policy(context.config), "unsafe_policy_path")
    assert context.policy_reads == []


def test_symlink_cannot_redirect_policy(context):
    Path("alternate.yml").write_text("checks: {}\n")
    Path(".omniflow.yml").symlink_to("alternate.yml")
    assert_minimal(resolve_repository_policy(context.config), "unsafe_policy_path")
    assert context.policy_reads == []


@pytest.mark.parametrize("trusted_text,reason", [
    (None, "policy_unavailable"), ("", "policy_invalid"), ("{}", "policy_invalid"),
    ("reporting: {diagnostics: {mode: '${PRIVATE_MODE}'}}", "policy_invalid"),
    ("reporting: {diagnostics: {mode: repository, secret_extra: private}}", "policy_invalid"),
    ("reporting: {diagnostics: {mode: [repository]}}", "policy_invalid"),
    ("unknown_private_key: true", "policy_invalid"),
])
def test_missing_invalid_or_env_expanded_base_policy_never_enables(context, monkeypatch, trusted_text, reason):
    monkeypatch.setenv("PRIVATE_MODE", "repository")
    context.trusted_text = trusted_text
    assert_minimal(resolve_repository_policy(context.config), reason)


@pytest.mark.parametrize("name,value,reason", [
    ("GITHUB_ACTIONS", "false", "not_github_actions"),
    ("GITHUB_EVENT_NAME", "schedule", "unsupported_event"),
    ("GITHUB_API_URL", "https://private.invalid", "unsupported_github_origin"),
    ("GITHUB_SERVER_URL", "https://github.invalid", "unsupported_github_origin"),
    ("GITHUB_REPOSITORY_ID", "999", "repository_unverified"),
    ("GITHUB_REPOSITORY", "synthetic/other-repo", "repository_unverified"),
    ("GITHUB_REPOSITORY_ID", "", "repository_unverified"),
    ("OMNIFLOW_GITHUB_TOKEN", "", "github_lookup_unavailable"),
])
def test_unverified_environment_falls_back_before_network(context, monkeypatch, name, value, reason):
    monkeypatch.setenv(name, value)
    assert_minimal(resolve_repository_policy(context.config), reason)
    assert context.calls == []


@pytest.mark.parametrize("private,visibility,reason", [
    (False, "public", "repository_public"), (True, "internal", "repository_internal"),
    (True, None, "repository_privacy_unverified"), (None, "private", "repository_privacy_unverified"),
    (True, "public", "repository_privacy_unverified"), (False, "private", "repository_privacy_unverified"),
    (1, "private", "repository_privacy_unverified"), (True, "PRIVATE", "repository_privacy_unverified"),
])
def test_live_private_metadata_is_required_despite_event_private_claim(context, private, visibility, reason):
    context.event["repository"]["private"] = True
    context.metadata.update(private=private, visibility=visibility)
    assert_minimal(resolve_repository_policy(context.config), reason)
    assert context.policy_reads == []


@pytest.mark.parametrize("missing", ["private", "visibility"])
def test_missing_live_privacy_field_does_not_claim_public_visibility(context, missing):
    context.metadata.pop(missing)
    assert_minimal(resolve_repository_policy(context.config), "repository_privacy_unverified")
    assert context.policy_reads == []


def test_live_repository_identity_must_match(context):
    context.metadata["id"] = 999
    assert_minimal(resolve_repository_policy(context.config), "repository_unverified")


@pytest.mark.parametrize("source,side", [("event", "head"), ("pr", "head"), ("event", "base")])
def test_fork_or_different_repository_never_receives_details(context, source, side):
    pr = context.event["pull_request"] if source == "event" else context.pr
    pr[side]["repo"] = {"full_name": "external/fork", "id": 456}
    assert_minimal(resolve_repository_policy(context.config), "fork_pull_request")
    assert context.policy_reads == []


@pytest.mark.parametrize("side", ["head", "base"])
def test_rerun_at_stale_revision_is_minimal(context, side):
    context.pr[side]["sha"] = "c" * 40
    assert_minimal(resolve_repository_policy(context.config), "stale_revision")


@pytest.mark.parametrize("change", ["short_sha", "changed_ref", "closed", "missing_repository_id"])
def test_incomplete_or_inconsistent_pr_evidence_is_minimal(context, change):
    if change == "short_sha":
        context.event["pull_request"]["head"]["sha"] = "aaaaaaa"
    elif change == "changed_ref":
        context.pr["base"]["ref"] = "other"
    elif change == "closed":
        context.pr["state"] = "closed"
    else:
        context.event["repository"].pop("id")
    assert resolve_repository_policy(context.config).enabled is False
    assert context.policy_reads == []


def test_live_lookup_failure_does_not_echo_exception(context, monkeypatch):
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository.request",
                        Mock(side_effect=ConfigError("synthetic-private-name and credential")))
    result = resolve_repository_policy(context.config)
    assert_minimal(result, "github_lookup_unavailable")
    assert "synthetic-private-name" not in repr(result)


@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch"])
def test_default_branch_runs_require_live_and_checked_out_exact_revision(context, monkeypatch, event_name):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    context.event = {"repository": dict(REPO), "ref": "refs/heads/main", "after": BASE, "deleted": False}
    assert resolve_repository_policy(context.config) == RepositoryPolicy(True, "repository_verified", REPOSITORY, BASE, BASE)
    assert [path for _, path, _ in context.calls] == ["", "/git/ref/heads/main"]


@pytest.mark.parametrize("change,reason", [
    ("nondefault", "revision_unverified"), ("local_revision", "revision_unverified"),
    ("live_revision", "stale_revision"), ("deleted", "revision_unverified"),
    ("after", "revision_unverified"),
])
def test_push_cannot_use_other_branch_or_stale_state(context, monkeypatch, change, reason):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    context.event = {"repository": dict(REPO), "ref": "refs/heads/main", "after": BASE, "deleted": False}
    if change == "nondefault":
        monkeypatch.setenv("GITHUB_REF", "refs/heads/private-mode")
    elif change == "local_revision":
        context.checkout = HEAD
    elif change == "live_revision":
        context.branch_sha = HEAD
    elif change == "deleted":
        context.event["deleted"] = True
    else:
        context.event["after"] = HEAD
    assert_minimal(resolve_repository_policy(context.config), reason)
    assert context.policy_reads == []
