import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow import candidate
from omniflow.candidate import CandidateVerificationError, verify_candidate
from omniflow.candidate_change_scope import equivalent
from omniflow.exceptions import ConfigError
from omniflow.file_scope import NonOmniRule


def compare(monkeypatch, git, omni, changed, *, side="head", names=None, rules=(), other=None):
    sha = ("a" if side == "base" else "b") * 40
    other_sha = ("b" if side == "base" else "a") * 40
    def tree(files):
        entries = []
        for path, text in files.items():
            raw = text.encode()
            blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            entries.append({"path": "omni/" + path, "mode": "100644", "type": "blob", "size": len(raw), "sha": blob})
        return entries
    reader = Mock(side_effect=lambda revision, path, **kwargs: git[path.removeprefix("omni/")])
    monkeypatch.setattr(candidate, "pull_request_revision", lambda rev_side: (sha if rev_side == side else other_sha, "fixture/repo"))
    monkeypatch.setattr(candidate, "_local_tree", lambda revision, **kwargs: tree(git if revision == sha else other or {}))
    monkeypatch.setattr(candidate, "read_git_text", reader)
    payload = {"files": omni}
    if names is not None:
        payload["viewNames"] = names
    client = Mock()
    client.get_model_yaml.return_value = payload
    context = SimpleNamespace(model_id="fixture-model", model_path="omni", branch_id="fixture-branch")
    proof = verify_candidate(client, context, revision_side=side, changed_paths=changed, non_omni_rules=rules)
    assert client.get_model_yaml.call_args.kwargs["branch_id"] == ("fixture-branch" if side == "head" else None)
    assert all(c.args[0] == sha for c in reader.call_args_list)
    return proof, {c.args[1] for c in reader.call_args_list}


@pytest.mark.parametrize("before,after", [
    ("label: orders", "label: orders\n"),
    ("fields:\r\n  id: {type: number}\r\n", "fields:\n  id:\n    type: number\n"),
    ("{label: 'orders'} # first\n", 'label: "orders"\n'),
    ("sql: |-\n  select 1", 'sql: "select 1"\n'),
])
def test_scope_matches_structure_without_reading_unrelated_files(monkeypatch, before, after):
    proof, reads = compare(monkeypatch, {"folder/orders.view.yaml": before, "unrelated.topic": "x: bad"},
                           {"api/orders.view": after, "other/topic.topic": "[broken", "other.view": "sql: stale"},
                           ["omni/folder/orders.view.yaml", "elsewhere/document.md"])
    assert proof["method"] == "pr_changed_files" and proof["status"] == "verified"
    assert proof["changed_file_count"] == 1 and proof["file_count"] == 1 and proof["absent_file_count"] == 0
    assert "snapshot_digest" not in proof and len(proof["scope_digest"]) == 64 and proof["head_sha"] == "b" * 40
    assert reads == {"omni/folder/orders.view.yaml"}


@pytest.mark.parametrize("before,after", [
    ("sql: |\n  select 1\n", "sql: |\n  select 1"),       # chomp changes authored SQL scalar
    ("x: true", "x: 1"), ("term: '#foo'", "term: '#bar'"), ("label: [a, b]", "label: [b, a]"),
    ("x: 1\nx: 2", "x: 2"), ("x: &x 1\ny: *x", "x: 1\ny: 1"),
])
def test_substantive_or_unprovable_changed_yaml_never_qualifies(monkeypatch, before, after):
    assert not equivalent(before, after)
    with pytest.raises(CandidateVerificationError) as caught:
        compare(monkeypatch, {"orders.view": before}, {"orders.view": after}, ["omni/orders.view"])
    assert caught.value.candidate_reason == "pr_change_not_synchronized" and caught.value.exit_code == 2
    assert caught.value.contents_comparison == "different" and caught.value.candidate_counts["compared_file_count"] == 1


@pytest.mark.parametrize("mode", ["add", "delete"])
def test_create_delete_requires_presence_or_absence_on_each_revision(monkeypatch, mode):
    unchanged = {"model": "label: stable\n"}
    exists = {**unchanged, "sales.topic.yaml": "base_view: orders"}
    for side in ("base", "head"):
        present = (mode == "add") == (side == "head")
        expected = exists if present else unchanged
        proof, _ = compare(monkeypatch, expected, expected, ["omni/sales.topic.yaml"], side=side)
        assert proof["changed_file_count"] == 1 and (proof["file_count"], proof["absent_file_count"]) == (int(present), int(not present))
        with pytest.raises(CandidateVerificationError) as caught:
            compare(monkeypatch, expected, unchanged if present else exists, ["omni/sales.topic.yaml"], side=side)
        assert caught.value.candidate_reason == "pr_change_not_synchronized"


def test_directory_and_wrapper_rename_does_not_force_an_api_directory(monkeypatch):
    changed = ["omni/Old/sales.topic", "omni/New/sales.topic.yaml"]
    for side, path in (("base", "Old/sales.topic"), ("head", "New/sales.topic.yaml")):
        proof, _ = compare(monkeypatch, {path: "base_view: orders"}, {"External/sales.topic": "base_view: orders\n"},
                           changed, side=side)
        assert proof["file_count"] == proof["absent_file_count"] == 1


def test_deleted_canonical_or_duplicate_view_cannot_borrow_a_stale_survivor(monkeypatch):
    git = {"model": "label: ok", "b/orders.view": "schema: b\ntable_name: orders"}
    api = {"a/orders.view": "schema: a\ntable_name: orders"}
    with pytest.raises(CandidateVerificationError) as stale:
        compare(monkeypatch, git, api, ["omni/a/orders.view"], names={"orders": "a/orders.view"})
    assert stale.value.candidate_reason == "pr_change_not_synchronized"
    with pytest.raises(CandidateVerificationError) as alias:
        compare(monkeypatch, {"model": "label: ok"}, api, ["omni/a__orders.view.yaml"], names={"a__orders": "a/orders.view"})
    assert alias.value.candidate_reason == "pr_change_not_synchronized"
    proof, reads = compare(monkeypatch, git, {"b/orders.view": "schema: b\ntable_name: orders"}, ["omni/a/orders.view"],
                           names={"b__orders": "b/orders.view"})
    assert proof["absent_file_count"] == 1 and reads == {"omni/b/orders.view"}


def test_empty_scope_is_never_whole_model_proof_and_bad_inventory_is_not_empty(monkeypatch):
    proof, reads = compare(monkeypatch, {"model": "a: old"}, {"model": "a: different"}, ["docs/readme.md"])
    assert proof["status"] == "scope_empty" and proof["file_count"] == 0 and not reads
    with pytest.raises(ConfigError):
        compare(monkeypatch, {"model": "a: old"}, {"model": "a: different"}, ["../model"])


def test_physical_view_group_includes_siblings_for_identity_only(monkeypatch):
    before = {"a/orders.view": "schema: a\ntable_name: orders\nlabel: A",
              "b/orders.view": "schema: b\ntable_name: orders\nlabel: B", "unrelated.view": "label: Nope"}
    after = {"moved/1/orders.view": "schema: a\ntable_name: orders\nlabel: A\n",
             "moved/2/orders.view": "schema: b\ntable_name: orders\nlabel: historical drift"}
    proof, reads = compare(monkeypatch, before, after, ["omni/a/orders.view"])
    assert proof["file_count"] == 1 and reads == {"omni/a/orders.view", "omni/b/orders.view"}
    duplicate = {k: v.replace("schema: b", "schema: a") for k, v in after.items()}
    with pytest.raises(CandidateVerificationError) as ambiguous:
        compare(monkeypatch, before, duplicate, ["omni/a/orders.view"])
    assert ambiguous.value.candidate_reason == "ambiguous_file_identity"


def test_unknown_generic_alias_cannot_prove_deleted_view_absence(monkeypatch):
    with pytest.raises(CandidateVerificationError) as caught:
        compare(monkeypatch, {"model": "label: ok"}, {"unrecognized.yaml": "type: view"},
                ["omni/example__orders.view.yaml"], names={})
    assert caught.value.candidate_reason == "pr_change_not_synchronized"


def test_unique_view_folder_move_is_not_a_deleted_sibling_masquerading_as_a_survivor(monkeypatch):
    old, new = "old/orders.view", "new/orders.view"
    changed = ["omni/" + old, "omni/" + new]
    current = {new: "label: Orders", "unrelated.view": "label: unrelated"}
    api = {old: "label: Orders", "unrelated.view": "label: different"}
    other_side = {old: "label: old", "unrelated.view": "label: unrelated"}
    proof, _ = compare(monkeypatch, current, api, changed, other=other_side)
    assert proof["status"] == "verified" and proof["file_count"] == 1
    # Same current shape, but the PR actually edited a surviving view and
    # deleted another: a stale Omni view cannot stand in for the survivor.
    with pytest.raises(CandidateVerificationError) as caught:
        compare(monkeypatch, current, api, changed, other={**other_side, new: "label: prior"})
    assert caught.value.candidate_reason == "pr_change_not_synchronized"


def test_scoped_exclusion_cannot_hide_an_unrelated_omni_source(monkeypatch):
    rule = NonOmniRule("bad", "omni/*.yaml", "ancillary")
    with pytest.raises(CandidateVerificationError) as caught:
        compare(monkeypatch, {"model": "a: x", "orders.view.yaml": "sql: x"},
                {"model": "a: x", "orders.view": "sql: x"}, ["omni/model"], rules=[rule])
    assert caught.value.candidate_reason == "non_omni_source_conflict"
