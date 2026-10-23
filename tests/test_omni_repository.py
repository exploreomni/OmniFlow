"""Cross-repository source proof, using fake GitHub reads and real local dbt Git."""
import base64
import copy
import hashlib
import json
import os
import subprocess
from unittest import mock

import pytest

from omniflow.config import DbtContentImpactSettings, DbtImpactSettings, OmniRepositorySettings
from omniflow.dbt_column_impact import collect_column_impacts
from omniflow.dbt_impact import COVERAGE_RULE, ORPHANED_COLUMN_RULE, _load_omni_snapshot, evaluate_dbt_impact
from omniflow.exceptions import ConfigError, GitHubAPIError, OmniFlowError, SecurityPolicyError
from omniflow.omni_repository import OmniRepositorySnapshot, public_snapshot, recorded_snapshot
from omniflow.revision_data import read_github_text

SOURCE = "fixture/omni-source"
DBT_REPO = "fixture/dbt-source"
OMNI_SHA = "a" * 40
VIEW_PATH = "omni/orders.view.yaml"
VIEW = "sql_table_name: orders\ndimensions:\n  id: {}\n  net: {}\n"


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    with mock.patch.dict(os.environ, {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    }, clear=True):
        monkeypatch.setattr("requests.request", mock.Mock(side_effect=AssertionError("No live HTTP in source tests")))
        yield


class FakeRepository:
    def __init__(self, files=None, *, repository=SOURCE, ref="release/stable"):
        self.repository = repository
        self.ref = ref
        self.sha = OMNI_SHA
        self.calls = []
        self.blob_error = None
        self.blobs = {}
        entries = []
        for path, text in (files if files is not None else {VIEW_PATH: VIEW}).items():
            value = text.encode()
            sha = hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()
            entries.append({"path": path, "type": "blob", "mode": "100644", "sha": sha, "size": len(value)})
            self.blobs[sha] = {"sha": sha, "encoding": "base64", "size": len(value),
                               "content": base64.b64encode(value).decode()}
        self.tree_payload = {"truncated": False, "tree": entries}

    def request(self, method, path, **kwargs):
        assert method == "GET", "Source access must remain read-only"
        self.calls.append((path, kwargs))
        if path.startswith("/git/ref/heads/"):
            return {"ref": f"refs/heads/{self.ref}", "object": {"type": "commit", "sha": self.sha}}
        if path == f"/git/trees/{OMNI_SHA}?recursive=1":
            return copy.deepcopy(self.tree_payload)
        if path.startswith("/git/blobs/"):
            if self.blob_error:
                raise self.blob_error
            return copy.deepcopy(self.blobs[path.rsplit("/", 1)[-1]])
        raise AssertionError(f"Unexpected fake request: {path}")


def resolve(monkeypatch, api=None, *, source=SOURCE, expected=None):
    api = api or FakeRepository(repository=source)
    factory = mock.Mock(return_value=api)
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository", factory)
    monkeypatch.setenv("OMNIFLOW_OMNI_REPO_TOKEN", "synthetic-source-only-token")
    settings = OmniRepositorySettings(repository=source, ref="release/stable", model_paths=["omni"])
    snapshot = OmniRepositorySnapshot.resolve(settings, expected=expected)
    factory.assert_called_once_with(source, "synthetic-source-only-token")
    return snapshot, api


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def local_dbt(root, monkeypatch, *, candidate="select id, amount * .8 as net from raw.orders"):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Fixture")
    path = root / "models/orders.sql"
    path.parent.mkdir()
    path.write_text("select id, amount * .9 as net from raw.orders")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    path.write_text(candidate)
    git(root, "add", ".")
    git(root, "commit", "-qm", "candidate")
    head = git(root, "rev-parse", "HEAD")
    event = root / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "base": {"sha": base, "ref": "main", "repo": {"full_name": DBT_REPO}},
        "head": {"sha": head, "ref": "candidate", "repo": {"full_name": DBT_REPO}},
    }}))
    for name, value in {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request_target",
                        "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": DBT_REPO,
                        "GITHUB_BASE_REF": "main"}.items():
        monkeypatch.setenv(name, value)
    return base, head


def impact_args(root, base, snapshot):
    return dict(changed_files=["models/orders.sql"], dbt_paths=["models"],
                settings=DbtImpactSettings(content_impact=DbtContentImpactSettings(enabled=True)),
                omni_yaml_paths=["omni"], base_ref=base, repo_root=root, omni_snapshot=snapshot)


def test_source_token_never_falls_back_to_workflow_token(monkeypatch):
    monkeypatch.setenv("OMNIFLOW_GITHUB_TOKEN", "synthetic-workflow-token")
    factory = mock.Mock(side_effect=AssertionError("No client before source credential"))
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository", factory)
    settings = OmniRepositorySettings(SOURCE, "main", ["omni"])
    with pytest.raises(ConfigError) as caught:
        OmniRepositorySnapshot.resolve(settings)
    assert caught.value.reason_code == "omni_repository_credentials_unavailable"
    factory.assert_not_called()


@pytest.mark.parametrize("event_name", ["pull_request", "pull_request_target"])
def test_fork_is_rejected_before_client_or_source_credential_use(tmp_path, monkeypatch, event_name):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "base": {"sha": "b" * 40, "repo": {"full_name": DBT_REPO}},
        "head": {"sha": "c" * 40, "repo": {"full_name": "untrusted/fork"}},
    }}))
    for key, value in {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": event_name,
                       "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": DBT_REPO,
                       "GITHUB_BASE_REF": "main", "OMNIFLOW_OMNI_REPO_TOKEN": "synthetic-token"}.items():
        monkeypatch.setenv(key, value)
    factory = mock.Mock(side_effect=AssertionError("Fork must not create source client"))
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository", factory)
    with pytest.raises(SecurityPolicyError) as caught:
        OmniRepositorySnapshot.resolve(OmniRepositorySettings(SOURCE, "main", ["omni"]))
    assert caught.value.reason_code == "omni_repository_context_untrusted"
    factory.assert_not_called()


@pytest.mark.parametrize("event_name", ["schedule", "issue_comment", "workflow_run"])
def test_unsupported_actions_trigger_cannot_read_external_source(monkeypatch, event_name):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    with pytest.raises(SecurityPolicyError):
        OmniRepositorySnapshot.resolve(OmniRepositorySettings(SOURCE, "main", ["omni"]))


def test_one_published_snapshot_serves_both_dbt_sides_and_keeps_source_identity(tmp_path, monkeypatch):
    base, head = local_dbt(tmp_path, monkeypatch)
    monkeypatch.setenv("OMNIFLOW_GITHUB_TOKEN", "synthetic-workflow-token")
    snapshot, api = resolve(monkeypatch)
    # A same-path dbt checkout file is not a substitute for the external source.
    local_view = tmp_path / VIEW_PATH
    local_view.parent.mkdir()
    local_view.write_text("sql_table_name: unrelated\ndimensions:\n  decoy: {}\n")
    result = collect_column_impacts(**impact_args(tmp_path, base, snapshot))
    assert result["coverage_complete"] is True
    assert {(item["field_name"], item["source_revision"]) for item in result["impacts"]} == {
        ("net", "base"), ("net", "head")}
    assert all(item["source_repository"] == SOURCE and item["source_sha"] == OMNI_SHA
               for item in result["impacts"])
    assert result["dbt_impact_snapshot"] == {
        "source_fingerprint": snapshot.evidence()["source_fingerprint"],
        "omni_commit_sha": OMNI_SHA, "dbt_base_sha": base, "dbt_head_sha": head}
    paths = [path for path, _kwargs in api.calls]
    assert paths.count("/git/ref/heads/release%2Fstable") == 1
    assert paths.count(f"/git/trees/{OMNI_SHA}?recursive=1") == 1
    assert sum(path.startswith("/git/blobs/") for path in paths) == 1
    assert os.environ["OMNIFLOW_GITHUB_TOKEN"] == "synthetic-workflow-token"


def test_external_published_reference_still_blocks_a_real_column_removal(tmp_path, monkeypatch):
    base, _ = local_dbt(tmp_path, monkeypatch, candidate="select id from raw.orders")
    snapshot, api = resolve(monkeypatch)
    report, issues = evaluate_dbt_impact(**impact_args(tmp_path, base, snapshot))
    assert report["coverage_complete"] is True
    assert any(issue["rule"] == ORPHANED_COLUMN_RULE and issue["column"] == "net" for issue in issues)
    assert sum(path.startswith("/git/trees/") for path, _ in api.calls) == 1
    assert sum(path.startswith("/git/blobs/") for path, _ in api.calls) == 1


def test_branch_drift_is_rejected_after_cached_reads(monkeypatch):
    snapshot, api = resolve(monkeypatch)
    assert snapshot.read_text(VIEW_PATH, max_bytes=1024) == VIEW
    api.sha = "d" * 40
    with pytest.raises(ConfigError) as caught:
        snapshot.assert_unchanged()
    assert caught.value.reason_code == "omni_repository_snapshot_changed"
    assert snapshot.sha == OMNI_SHA


@pytest.mark.parametrize("change", ["commit", "repository", "ref", "paths"])
def test_prior_snapshot_receipt_cannot_authorize_a_changed_source(monkeypatch, change):
    snapshot, _ = resolve(monkeypatch)
    expected = snapshot.evidence()
    if change == "commit":
        expected["omni_commit_sha"] = "d" * 40
    else:
        original = snapshot.settings
        settings = OmniRepositorySettings(
            "fixture/other" if change == "repository" else original.repository,
            "other" if change == "ref" else original.ref,
            ["different"] if change == "paths" else original.model_paths,
        )
        expected = OmniRepositorySnapshot(settings, OMNI_SHA, snapshot.api).evidence()
    with pytest.raises(ConfigError) as caught:
        resolve(monkeypatch, expected=expected)
    assert caught.value.reason_code == "omni_repository_snapshot_changed"


@pytest.mark.parametrize("kind", ["truncated", "ambiguous", "invalid_path", "oversized_inventory"])
def test_incomplete_or_ambiguous_tree_is_not_accepted(monkeypatch, kind):
    api = FakeRepository()
    if kind == "truncated":
        api.tree_payload["truncated"] = True
    elif kind == "ambiguous":
        api.tree_payload["tree"].append(dict(api.tree_payload["tree"][0]))
    elif kind == "invalid_path":
        api.tree_payload["tree"][0]["path"] = "omni/../orders.view.yaml"
    else:
        api.tree_payload["tree"] = [{"path": f"other/{i}"} for i in range(10001)]
    snapshot, _ = resolve(monkeypatch, api)
    with pytest.raises(OmniFlowError):
        _load_omni_snapshot(None, ["omni"], revision=None, external_snapshot=snapshot)
    assert not any(path.startswith("/git/blobs/") for path, _ in api.calls)


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "oversized_file", "incomplete_blob", "unreadable_blob"])
def test_unsafe_or_unreadable_blob_cannot_become_empty_source(monkeypatch, kind):
    api = FakeRepository()
    entry = api.tree_payload["tree"][0]
    if kind == "symlink":
        entry["mode"] = "120000"
    elif kind == "parent_symlink":
        api.tree_payload["tree"].append({"path": "omni", "type": "blob", "mode": "120000"})
    elif kind == "oversized_file":
        entry["size"] = 6 * 1024 * 1024
    elif kind == "incomplete_blob":
        api.blobs[entry["sha"]]["content"] = ""
    else:
        api.blob_error = GitHubAPIError("synthetic inaccessible blob", reason_code="github_forbidden")
    snapshot, _ = resolve(monkeypatch, api)
    with pytest.raises(OmniFlowError):
        _load_omni_snapshot(None, ["omni"], revision=None, external_snapshot=snapshot)


@pytest.mark.parametrize("files", [{}, {"omni/model.yaml": "label: Metadata only\n"}])
def test_missing_or_unindexed_model_root_is_incomplete(tmp_path, monkeypatch, files):
    base, _ = local_dbt(tmp_path, monkeypatch, candidate="select id from raw.orders")
    snapshot, _ = resolve(monkeypatch, FakeRepository(files))
    report, issues = evaluate_dbt_impact(**impact_args(tmp_path, base, snapshot))
    assert report["coverage_complete"] is False
    assert report["validation_complete"] is False
    assert any(issue["rule"] == COVERAGE_RULE for issue in issues)
    advisory = collect_column_impacts(**impact_args(tmp_path, base, snapshot))
    assert advisory["coverage_complete"] is False and advisory["coverage_gaps"]


def test_explicit_source_client_must_match_repository_even_with_cached_tree():
    api = FakeRepository(repository="fixture/wrong-source")
    with pytest.raises(SecurityPolicyError):
        read_github_text(SOURCE, OMNI_SHA, VIEW_PATH, max_bytes=1024, api=api,
                         tree={entry["path"]: entry for entry in api.tree_payload["tree"]})
    assert api.calls == []


def test_snapshots_do_not_share_tree_or_blob_caches_across_repositories(monkeypatch):
    first, first_api = resolve(monkeypatch)
    second_text = VIEW.replace("net", "different")
    second_api = FakeRepository({VIEW_PATH: second_text}, repository="fixture/other-source")
    second, _ = resolve(monkeypatch, second_api, source=second_api.repository)
    assert first.read_text(VIEW_PATH, max_bytes=1024) == VIEW
    assert second.read_text(VIEW_PATH, max_bytes=1024) == second_text
    assert first.evidence()["source_fingerprint"] != second.evidence()["source_fingerprint"]
    assert sum(path.startswith("/git/trees/") for path, _ in first_api.calls) == 1
    assert sum(path.startswith("/git/trees/") for path, _ in second_api.calls) == 1


def test_public_receipt_excludes_source_names_paths_and_credentials(monkeypatch):
    snapshot, _ = resolve(monkeypatch)
    private = {**snapshot.evidence(), "repository": SOURCE, "model_paths": ["omni"],
               "token": "synthetic-source-only-token"}
    assert public_snapshot(private) == snapshot.evidence()
    assert SOURCE not in json.dumps(public_snapshot(private))
    assert public_snapshot({"omni_commit_sha": "main", "source_fingerprint": "not a digest"}) == {}
    with pytest.raises(ConfigError):
        recorded_snapshot({"model_reports": []})
    record = {"validator": "dbt_impact", "dbt_impact_snapshot": snapshot.evidence()}
    with pytest.raises(ConfigError):
        recorded_snapshot({"model_reports": [record, record]})
