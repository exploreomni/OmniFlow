import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from omniflow import nonsemantic
from omniflow.discovery import ModelContext
from omniflow.nonsemantic import classify_pr_change


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    event_path = tmp_path / "event.json"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    git("init", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Synthetic fixture")
    Path("omni").mkdir()
    Path("omni/model").write_text("connection: synthetic\n")
    Path("omni/orders.view").write_text("dimensions:\n  id:\n    type: number\n")
    Path("README.md").write_text("Documentation\n")
    git("add", ".")
    git("commit", "-m", "Base fixture")
    context = ModelContext(base_url="https://example.omniapp.co", model_id="model", model_path="omni")
    fixture = SimpleNamespace(contexts=[context], base=git("rev-parse", "HEAD"), event_path=event_path)

    def commit():
        git("add", "-u")
        git("add", "omni")
        git("commit", "--allow-empty", "-m", "Candidate fixture")
        fixture.head = git("rev-parse", "HEAD")
        fixture.event_path.write_text(json.dumps({"pull_request": {
            "base": {"sha": fixture.base, "repo": {"full_name": "owner/repo"}},
            "head": {"sha": fixture.head, "repo": {"full_name": "owner/repo"}},
        }}))
        return classify_pr_change(fixture.contexts)

    fixture.commit = commit
    return fixture


def comment():
    path = Path("omni/orders.view")
    path.write_text("# A harmless standalone YAML comment\n" + path.read_text())


def test_comment_classification_never_skips_live_content(repository):
    comment()
    result = repository.commit()
    assert result == {"classification": "comment_only", "reason_code": "live_content_provenance_unavailable",
                      "content_validation_required": True}
    assert not any(value in json.dumps(result) for value in ("orders", "omni", repository.base, repository.head))


def test_reads_immutable_commits_not_mutable_checkout(repository):
    comment()
    repository.commit()
    Path("omni/orders.view").write_text("sql: DELETE FROM sensitive\n")
    checkout = git("rev-parse", "HEAD")
    assert classify_pr_change(repository.contexts)["classification"] == "comment_only"
    assert git("rev-parse", "HEAD") == checkout
    assert "DELETE" in Path("omni/orders.view").read_text()


@pytest.mark.parametrize("replacement", [
    "# Header\ndimensions: # structural comment\n  id:\n    type: number # trailing comment\n",
    "\ndimensions:\n    id:\n        type: number\n\n",
])
def test_actual_yaml_comments_and_structural_whitespace_are_nonsemantic(repository, replacement):
    Path("omni/orders.view").write_text(replacement)
    assert repository.commit()["classification"] == "comment_only"


@pytest.mark.parametrize("replacement", [
    "dimensions:\n  id:\n    type: string\n",
    "dimensions:\n  id:\n    type: 'number'\n",  # No coercion of authored scalars.
    "dimensions:\n  id:\n    type: number\n  extra: {}\n",
    "dimensions:\n  id:\n    sql: '# this is data, not a comment'\n",
])
def test_semantic_changes_never_look_like_comments(repository, replacement):
    Path("omni/orders.view").write_text(replacement)
    result = repository.commit()
    assert result["classification"] == "meaningful"
    assert result["content_validation_required"] is True


@pytest.mark.parametrize("before,after", [
    ("sql: |\n  select 1\n", "sql: |\n  select 1\n  # not a YAML comment\n"),
    ("sql: 'select 1'\n", "sql: 'select  1'\n"),
    ("label: |\n  First\n\n", "label: |\n  First\n\n\n"),
    ("fields: [one, two]\n", "fields: [two, one]\n"),
    ("one: 1\ntwo: 2\n", "two: 2\none: 1\n"),
    ("value: 1\n", "value: 01\n"),
    ("value: true\n", "value: yes\n"),
])
def test_scalars_and_order_are_preserved_exactly(repository, before, after):
    Path("omni/orders.view").write_text(before)
    git("add", "omni")
    git("commit", "-m", "Scalar fixture")
    repository.base = git("rev-parse", "HEAD")
    Path("omni/orders.view").write_text(after)
    assert repository.commit()["classification"] == "meaningful"


@pytest.mark.parametrize("unsafe,reason", [
    ("label: first\nlabel: second\n", "duplicate_yaml_key"),
    ("label: &anchor first\nother: *anchor\n", "unsupported_yaml_construct"),
    ("label: !custom text\n", "invalid_or_unsupported_yaml"),
    ("label: !!str text\n", "unsupported_yaml_construct"),
    ("label: yes\ntrue: one\n", "ambiguous_yaml_key"),
    ("extends: another_model\n", "external_dependency_unverified"),
    ("sql: '{{ external_function() }}'\n", "external_dependency_unverified"),
    ("label: [broken\n", "invalid_or_unsupported_yaml"),
    ("created: 2026-01-01\n", "unsupported_yaml_construct"),
])
def test_ambiguous_or_unsupported_unchanged_yaml_prevents_classification(repository, unsafe, reason):
    Path("omni/model").write_text(unsafe)
    git("add", "omni/model")
    git("commit", "-m", "Unsupported base fixture")
    repository.base = git("rev-parse", "HEAD")
    comment()
    result = repository.commit()
    assert result["classification"] == "unverified"
    assert result["reason_code"] == reason
    assert result["content_validation_required"] is True


@pytest.mark.parametrize("change", ["add", "delete", "rename", "mode", "symlink"])
def test_inventory_type_and_mode_changes_prevent_comment_classification(repository, change):
    path = Path("omni/orders.view")
    if change == "add":
        Path("omni/new.view").write_text("# Added empty file\n")
    elif change == "delete":
        path.unlink()
    elif change == "rename":
        path.rename("omni/renamed.view")
    elif change == "mode":
        path.chmod(0o755)
    else:
        path.unlink()
        path.symlink_to("model")
    result = repository.commit()
    assert result["classification"] == "meaningful"
    assert result["content_validation_required"] is True


def test_mixed_non_model_change_is_not_independently_skippable(repository):
    comment()
    Path("README.md").write_text("# Changed\n")
    result = repository.commit()
    assert result["reason_code"] == "unsupported_changed_path"


def test_other_model_semantic_change_prevents_global_comment_classification(repository):
    Path("second").mkdir()
    Path("second/other.view").write_text("label: old\n")
    git("add", "second")
    git("commit", "-m", "Second model fixture")
    repository.base = git("rev-parse", "HEAD")
    comment()
    Path("second/other.view").write_text("label: new\n")
    assert repository.commit()["reason_code"] == "unsupported_changed_path"
    repository.contexts.append(ModelContext(base_url="https://example.omniapp.co", model_id="second",
                                             model_path="second"))
    assert classify_pr_change(repository.contexts)["classification"] == "meaningful"


@pytest.mark.parametrize("path", [".github/workflows/check.yml", ".omni/flow.json", "dbt_project.yml", "settings.yaml"])
def test_root_scope_does_not_misclassify_config_comments(repository, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("name: config\n")
    git("add", path)
    git("commit", "-m", "Root configuration fixture")
    repository.base = git("rev-parse", "HEAD")
    repository.contexts[0].model_path = "."
    target.write_text("# Comment\nname: config\n")
    result = repository.commit()
    assert result["classification"] == "unverified"
    assert result["reason_code"] == "unsupported_changed_path"


def test_missing_revision_objects_fail_closed(repository):
    comment()
    repository.commit()
    event = json.loads(repository.event_path.read_text())
    event["pull_request"]["base"]["sha"] = "f" * 40
    repository.event_path.write_text(json.dumps(event))
    assert classify_pr_change(repository.contexts) == {
        "classification": "unverified", "reason_code": "snapshot_unavailable", "content_validation_required": True,
    }


def test_bounded_snapshot_errors_do_not_disclose_paths_or_content(repository, monkeypatch):
    comment()
    repository.commit()
    monkeypatch.setattr(nonsemantic, "MAX_YAML_TOTAL_BYTES", 1)
    result = classify_pr_change(repository.contexts)
    assert result["reason_code"] == "snapshot_limit_exceeded"
    assert result["content_validation_required"] is True
