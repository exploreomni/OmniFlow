import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow import candidate
from omniflow.candidate import CandidateVerificationError, verify_candidate


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("OMNIFLOW_GITHUB_TOKEN", "synthetic-token")
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository.request", Mock(side_effect=AssertionError("No live API calls")))
    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Synthetic test")
    files = {
        "model": "connection: synthetic\n",
        "relationships": "[]\n",
        "SCHEMA/orders.view": "dimensions: {id: {type: number}}\n",
        "QUERY/orders.query.view": "sql: select 1\n",
        "sales.topic": "base_view: schema__orders\n",
        "sales.composite_topic": "base_view: schema__orders\n",
        "legacy.relationships": "[]\n",
        "nested/settings.yaml": "type: model\n",
        "nested/more.yml": "type: model\n",
    }
    for name, text in files.items():
        path = Path("omni") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    Path("omni/README.md").write_text("Not an authored semantic file.\n")
    Path("untrusted.py").write_text("raise RuntimeError('never execute PR head')\n")
    git("add", ".")
    git("commit", "-m", "Synthetic immutable candidate fixture")
    head = git("rev-parse", "HEAD")
    event = {"pull_request": {"head": {"sha": head, "repo": {"full_name": "owner/repo"}}}}
    Path("event.json").write_text(json.dumps(event))
    context = SimpleNamespace(model_id="model", model_path="omni", branch_id="candidate-id",
                              branch_name="release-candidate", base_branch="main", environment="production")
    client = SimpleNamespace(get_model_yaml=Mock(return_value={"files": dict(files)}))
    return SimpleNamespace(files=files, context=context, client=client, head=head, event=event)


@pytest.mark.parametrize("wrapper", [None, "content", "contents", "both"])
def test_exact_candidate_uses_immutable_event_head_not_checkout_or_branch_name(snapshot, wrapper):
    files = snapshot.files
    if wrapper:
        files = {path: ({"content": text, "contents": text} if wrapper == "both" else {wrapper: text})
                 for path, text in files.items()}
    snapshot.client.get_model_yaml.return_value = {"files": files}
    Path("omni/SCHEMA/orders.view").write_text("This mutable checkout is not the PR revision.\n")
    git("add", "omni/SCHEMA/orders.view")
    git("commit", "-m", "Synthetic unrelated checkout revision")
    checkout_head = git("rev-parse", "HEAD")
    assert checkout_head != snapshot.head
    proof = verify_candidate(snapshot.client, snapshot.context)
    assert proof == verify_candidate(snapshot.client, snapshot.context)
    assert proof["status"] == "verified"
    assert proof["method"] == "authored_yaml_snapshot"
    assert proof["head_sha"] == snapshot.head
    assert proof["file_count"] == len(snapshot.files)
    assert len(proof["snapshot_digest"]) == 64
    assert git("rev-parse", "HEAD") == checkout_head
    snapshot.client.get_model_yaml.assert_called_with(
        "model", branch_id="candidate-id", mode="combined", include_checksums=False, fully_resolved=False,
    )


def test_root_model_scope_includes_all_semantic_files_but_not_readme_or_code(snapshot):
    snapshot.context.model_path = "."
    Path("root.view").write_text("dimensions: {}\n")
    git("add", "root.view")
    git("commit", "-m", "Synthetic root-scoped semantic file")
    snapshot.event["pull_request"]["head"]["sha"] = git("rev-parse", "HEAD")
    Path("event.json").write_text(json.dumps(snapshot.event))
    files = {f"omni/{path}": text for path, text in snapshot.files.items()}
    files["root.view"] = "dimensions: {}\n"
    snapshot.client.get_model_yaml.return_value = {"files": files}
    assert verify_candidate(snapshot.client, snapshot.context)["file_count"] == len(files)


@pytest.mark.parametrize("change,reason", [
    ("stale", "content_mismatch"), ("missing", "file_inventory_mismatch"),
    ("deleted_still_in_api", "file_inventory_mismatch"),
])
def test_stale_missing_and_deleted_candidate_files_fail_closed(snapshot, change, reason):
    files = snapshot.client.get_model_yaml.return_value["files"]
    if change == "stale":
        files["SCHEMA/orders.view"] += "\n"  # Even seemingly benign formatting is not guessed away.
    elif change == "missing":
        del files["relationships"]
    else:
        files["deleted.view"] = "dimensions: {}\n"
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == reason
    assert "SCHEMA" not in str(caught.value)


@pytest.mark.parametrize("field,value,reason", [
    ("branch_id", None, "missing_candidate_branch"), ("model_path", "../omni", "unsafe_snapshot_path"),
])
def test_missing_branch_or_unsafe_scope_is_not_verified(snapshot, field, value, reason):
    setattr(snapshot.context, field, value)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == reason
    snapshot.client.get_model_yaml.assert_not_called()


@pytest.mark.parametrize("event", ["not_pr", "short_sha", "missing_repository", "malformed_head"])
def test_verification_requires_complete_immutable_pr_event(snapshot, monkeypatch, event):
    if event == "not_pr":
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    elif event == "short_sha":
        snapshot.event["pull_request"]["head"]["sha"] = snapshot.head[:7]
    elif event == "missing_repository":
        del snapshot.event["pull_request"]["head"]["repo"]
    else:
        snapshot.event["pull_request"]["head"] = None
    Path("event.json").write_text(json.dumps(snapshot.event))
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason in {"missing_pr_revision", "invalid_pr_revision"}
    snapshot.client.get_model_yaml.assert_not_called()


@pytest.mark.parametrize("malformed", ["null", "list", "bad_wrapper", "unsafe_path", "nonsemantic_extra"])
def test_malformed_api_snapshot_is_never_filtered_into_success(snapshot, malformed):
    payload = snapshot.client.get_model_yaml.return_value
    if malformed == "null":
        snapshot.client.get_model_yaml.return_value = None
    elif malformed == "list":
        payload["files"] = []
    elif malformed == "bad_wrapper":
        payload["files"]["model"] = {"content": "PRIVATE-CONTENT", "contents": "DIFFERENT-PRIVATE-CONTENT"}
    elif malformed == "unsafe_path":
        payload["files"]["../outside.view"] = "PRIVATE-CONTENT"
    else:
        payload["files"]["README.md"] = "PRIVATE-CONTENT"
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason in {"invalid_api_snapshot", "unsafe_snapshot_path"}
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("limit", ["tree", "file", "aggregate"])
def test_snapshot_bounds_are_enforced_before_verification(snapshot, monkeypatch, limit):
    if limit == "tree":
        monkeypatch.setattr(candidate, "MAX_TREE_BYTES", 32)
    elif limit == "file":
        monkeypatch.setattr(candidate, "MAX_YAML_FILE_BYTES", 64)
        snapshot.client.get_model_yaml.return_value["files"]["model"] = "x" * 65
    else:
        monkeypatch.setattr(candidate, "MAX_YAML_TOTAL_BYTES", 64)
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == "snapshot_limit_exceeded"


def test_symlink_anywhere_under_model_scope_is_not_followed(snapshot):
    Path("omni/linked.view").symlink_to("../untrusted.py")
    git("add", "omni/linked.view")
    git("commit", "-m", "Synthetic symlink fixture")
    snapshot.event["pull_request"]["head"]["sha"] = git("rev-parse", "HEAD")
    Path("event.json").write_text(json.dumps(snapshot.event))
    with pytest.raises(CandidateVerificationError) as caught:
        verify_candidate(snapshot.client, snapshot.context)
    assert caught.value.candidate_reason == "unsafe_git_entry"
    snapshot.client.get_model_yaml.assert_not_called()


@pytest.mark.parametrize("response", ["complete", "duplicate_path", "truncated", "wrong_blob"])
def test_remote_fallback_requires_complete_duplicate_free_inventory_and_immutable_blobs(snapshot, monkeypatch, response):
    entries = []
    for path, text in snapshot.files.items():
        raw = text.encode()
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        entries.append({"path": f"omni/{path}", "type": "blob", "mode": "100644", "size": len(raw), "sha": sha})
    payload = {"tree": entries, "truncated": False}
    if response == "duplicate_path":
        entries.append(copy.deepcopy(entries[0]))
    elif response == "truncated":
        payload["truncated"] = True
    elif response == "wrong_blob":
        entries[0]["sha"] = "0" * 40
    monkeypatch.setattr(candidate, "_local_tree", Mock(side_effect=candidate._GitUnavailable))
    request = Mock(return_value=payload)
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository.request", request)
    read = Mock(side_effect=lambda repo, sha, path, **_: snapshot.files[path.removeprefix("omni/")])
    monkeypatch.setattr(candidate, "read_github_text", read)
    if response == "complete":
        assert verify_candidate(snapshot.client, snapshot.context)["head_sha"] == snapshot.head
        assert len(read.call_args_list) == len(snapshot.files)
        assert all(call.args[1] == snapshot.head for call in read.call_args_list)
    else:
        with pytest.raises(CandidateVerificationError) as caught:
            verify_candidate(snapshot.client, snapshot.context)
        assert caught.value.candidate_reason == {
            "duplicate_path": "duplicate_git_path", "truncated": "incomplete_git_snapshot",
            "wrong_blob": "incomplete_git_snapshot",
        }[response]
    request.assert_called_once_with("GET", f"/git/trees/{snapshot.head}?recursive=1", max_bytes=candidate.MAX_TREE_BYTES)
