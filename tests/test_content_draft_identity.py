"""Synthetic published/draft evidence; no tenant data or external calls."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow.artifacts import write_public_json, write_public_reports
from omniflow.diagnostics import capture_private_reports, diagnostic_session
from omniflow.exceptions import OmniAuthError
from omniflow.github.annotations import annotation_lines
from omniflow.validators import content_identity
from omniflow.validators.content import ContentEvidenceError, run_content_validation
from omniflow.validators.content_identity import CANONICAL_DOCUMENT, CANONICAL_QUERY

PUBLISHED = "synthetic-published-parent"
DRAFT = "synthetic-branch-draft"
BRANCH = "synthetic-branch-identity"
MESSAGE = "Synthetic private field is unavailable"
FILTER = "Synthetic private filter"


def document(identifier, *, filters=None, queries=()):
    return {
        "identifier": identifier, "document_id": f"physical-{identifier}",
        "name": "Synthetic private dashboard",
        "dashboard_filter_issues": (
            [{"filter_name": FILTER, "issues": [MESSAGE]}] if filters is None else filters
        ),
        "queries_and_issues": list(queries),
    }


def draft(**overrides):
    return {
        "identifier": DRAFT, "publishedIdentifier": PUBLISHED, "branch": {"id": BRANCH},
        "status": "active", "draftOutOfDate": False, "updatedAt": "2026-10-15T12:00:00.000Z",
        **overrides,
    }


def client_for(current=None, baseline=None, *, records=None, drafts=None):
    current = [document(DRAFT)] if current is None else current
    baseline = [document(PUBLISHED)] if baseline is None else baseline
    records = [{"identifier": PUBLISHED, "labels": ["Governed"]}] if records is None else records
    drafts = [draft()] if drafts is None else drafts
    return SimpleNamespace(
        validate_content=Mock(side_effect=lambda *args, branch_id=None, **kwargs: copy.deepcopy(
            {"content": current if branch_id else baseline}
        )),
        list_content=Mock(side_effect=lambda **kwargs: copy.deepcopy(records)),
        list_document_drafts=Mock(side_effect=lambda parent: copy.deepcopy(drafts)),
        get_document_identity=Mock(side_effect=lambda identifier, **kwargs: {
            "identifier": identifier, "publishedIdentifier": PUBLISHED,
        }),
    )


def validate(tmp_path, client, *, labels=()):
    return run_content_validation(
        client=client, model_id="synthetic-model-identity", branch_id=BRANCH, user_id="synthetic-user",
        include_personal_folders=False, labels=list(labels), fail_on_new_only=True,
        history_in=tmp_path / "private/missing.json", history_out=tmp_path / "private/history.json",
        report_out=tmp_path / "private/report.json",
    )


@pytest.mark.parametrize("change,new,existing", [("unchanged", 0, 1), ("new_filter", 1, 1), ("occurrence", 1, 1)])
def test_verified_draft_compares_logical_parent_without_waiving_new_errors(tmp_path, change, new, existing):
    current = document(DRAFT)
    if change == "new_filter":
        current["dashboard_filter_issues"].append({"filter_name": "Another filter", "issues": [MESSAGE]})
    elif change == "occurrence":
        current["dashboard_filter_issues"][0]["issues"].append(MESSAGE)
    client = client_for([current])
    report, code = validate(tmp_path, client)
    assert code == int(new > 0)
    assert (report["existing_issues"], report["new_issues"], report["resolved_issues"]) == (existing, new, 0)
    assert report["content_health"] == "issues_in_scope"
    assert report["comparison_status"] == "complete"
    assert all(issue["raw"][CANONICAL_DOCUMENT] == PUBLISHED for issue in report["issues"])
    assert all(issue["document_identifier"] == DRAFT for issue in report["issues"])
    assert [call.args for call in client.list_document_drafts.call_args_list] == [(PUBLISHED,), (PUBLISHED,)]
    assert [call.kwargs["branch_id"] for call in client.validate_content.call_args_list] == [None, BRANCH, None, BRANCH]


@pytest.mark.parametrize("healthy_in_validator", [False, True])
def test_first_draft_error_on_previously_healthy_document_is_new(tmp_path, healthy_in_validator):
    baseline = [document(PUBLISHED, filters=[])] if healthy_in_validator else []
    report, code = validate(tmp_path, client_for(baseline=baseline))
    assert code == 1
    assert report["new_issues"] == 1 and report["existing_issues"] == report["baseline_issues"] == 0
    assert report["regression_status"] == "new_issues_detected"


def test_distinct_published_document_with_identical_error_is_not_aliased(tmp_path):
    other = document("synthetic-new-published")
    # An upstream private alias is untrusted and cannot force a match.
    other[CANONICAL_DOCUMENT] = PUBLISHED
    client = client_for([document(DRAFT), other], records=[{"identifier": PUBLISHED}, {"identifier": other["identifier"]}])
    report, code = validate(tmp_path, client)
    assert code == 1
    assert report["existing_issues"] == report["new_issues"] == 1
    new = next(issue for issue in report["issues"] if issue["state"] == "new")
    assert CANONICAL_DOCUMENT not in new["raw"]
    assert report["resolved_issues"] == 0


def test_reused_published_identifier_does_not_waive_different_physical_document(tmp_path):
    current = document(PUBLISHED)
    current["document_id"] = "synthetic-replacement-physical-document"
    client = client_for([current], drafts=[])
    report, code = validate(tmp_path, client)
    assert code == 1
    assert report["new_issues"] == report["resolved_issues"] == 1
    assert report["existing_issues"] == 0
    assert CANONICAL_DOCUMENT not in report["issues"][0]["raw"]
    client.list_document_drafts.assert_not_called()


@pytest.mark.parametrize("selected,expected", [(PUBLISHED, 1), ("synthetic-other-parent", 0), (DRAFT, 0)])
def test_labels_select_only_verified_published_identity(tmp_path, selected, expected):
    client = client_for()
    client.list_content.side_effect = lambda *, labels, **kwargs: (
        [{"identifier": selected, "labels": ["Governed"]}] if labels else [{"identifier": PUBLISHED}]
    )
    report, code = validate(tmp_path, client, labels=["Governed"])
    assert code == 0
    assert report["existing_issues"] == report["current_issues"] == report["baseline_issues"] == expected
    assert report["new_issues"] == 0
    assert [call.kwargs["labels"] for call in client.list_content.call_args_list] == [["Governed"], [], ["Governed"]]


@pytest.mark.parametrize("problem,reason", [
    ("unknown", "document_identity_unavailable"),
    ("unpublished", "document_identity_unavailable"),
    ("denied", "document_identity_permission_denied"),
    ("wrong_branch", "document_identity_wrong_branch"),
    ("unattached", "document_identity_wrong_branch"),
    ("archived", "document_identity_unavailable"),
    ("stale", "stale_document_draft"),
    ("duplicate_draft", "ambiguous_document_identity"),
    ("duplicate_branch", "ambiguous_document_identity"),
    ("wrong_parent", "ambiguous_document_identity"),
    ("parent_and_draft", "ambiguous_document_identity"),
])
def test_unknown_stale_or_ambiguous_draft_never_becomes_existing(tmp_path, problem, reason):
    client = client_for()
    if problem == "unknown":
        client.list_document_drafts.side_effect = lambda parent: []
    elif problem == "unpublished":
        client = client_for(baseline=[], records=[], drafts=[])
    elif problem == "denied":
        client.list_document_drafts.side_effect = OmniAuthError("Synthetic private permission failure")
    elif problem == "parent_and_draft":
        client = client_for([document(PUBLISHED), document(DRAFT)])
    else:
        rows = {
            "wrong_branch": [draft(branch={"id": "another-branch"})],
            "unattached": [draft(branch=None)],
            "archived": [draft(status="archived")],
            "stale": [draft(draftOutOfDate=True)],
            "duplicate_draft": [draft(), draft()],
            "duplicate_branch": [draft(), draft(identifier="another-draft")],
            "wrong_parent": [draft(publishedIdentifier="another-parent")],
        }[problem]
        client.list_document_drafts.side_effect = lambda parent: copy.deepcopy(rows)
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)
    assert caught.value.exit_code == 4
    assert caught.value.reason_code == reason
    assert caught.value.report_issue()["regression_status"] == "unknown"
    assert caught.value.report_issue()["blocking"] is True
    assert not (tmp_path / "private/report.json").exists()
    assert not (tmp_path / "private/history.json").exists()
    assert "Synthetic private permission failure" not in str(caught.value)


@pytest.mark.parametrize("change", ["version", "mapping_removed", "branch_evidence", "base_evidence", "labels"])
def test_mapping_version_validation_or_label_changes_invalidate_comparison(tmp_path, change):
    client = client_for()
    if change == "version":
        client.list_document_drafts.side_effect = [[draft()], [draft(updatedAt="2026-10-15T12:00:01.000Z")]]
    elif change == "mapping_removed":
        client.list_document_drafts.side_effect = [[draft()], []]
    elif change in {"branch_evidence", "base_evidence"}:
        responses = [{"content": [document(PUBLISHED)]}, {"content": [document(DRAFT)]}]
        changed = document(DRAFT if change == "branch_evidence" else PUBLISHED, filters=[])
        responses.extend([
            {"content": [changed if change == "base_evidence" else document(PUBLISHED)]},
            {"content": [changed if change == "branch_evidence" else document(DRAFT)]},
        ])
        client.validate_content.side_effect = responses
    else:
        client.list_content.side_effect = [[{"identifier": PUBLISHED}], [{"identifier": PUBLISHED}], []]
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client, labels=["Governed"] if change == "labels" else [])
    assert caught.value.reason_code == "document_identity_changed"
    assert caught.value.exit_code == 4
    assert not (tmp_path / "private/history.json").exists()


def recheck_documents(kind):
    messages = [MESSAGE, MESSAGE, "Another synthetic issue"]
    filters = [{"filter_name": FILTER, "issues": messages}]
    queries = []
    if kind == "filter_groups":
        filters.append({"filter_name": "Another synthetic filter", "issues": [MESSAGE]})
    elif kind == "legacy_filters":
        filters = [MESSAGE, {"message": "Another synthetic issue"}, MESSAGE, *filters]
    elif kind == "query_messages":
        filters = []
        queries = [{"query_id_map_key": "stable-tile", "query_presentation_id": "presentation",
                    "issues": [MESSAGE, MESSAGE, {"message": "Another synthetic issue"}]}]
    return document(DRAFT, filters=copy.deepcopy(filters), queries=copy.deepcopy(queries)), document(
        PUBLISHED, filters=copy.deepcopy(filters), queries=copy.deepcopy(queries),
    )


def recheck_issue_list(doc, kind):
    if kind == "query_messages":
        return doc["queries_and_issues"][0]["issues"]
    if kind in {"filter_groups", "legacy_filters"}:
        return doc["dashboard_filter_issues"]
    return doc["dashboard_filter_issues"][0]["issues"]


@pytest.mark.parametrize("scope", ["branch", "base"])
@pytest.mark.parametrize("kind,expected", [
    ("filter_groups", 4), ("grouped_messages", 3), ("query_messages", 3), ("legacy_filters", 6),
])
def test_recheck_order_only_changes_preserve_existing_occurrences_and_payload(tmp_path, scope, kind, expected):
    current, baseline = recheck_documents(kind)
    rechecked = copy.deepcopy(current if scope == "branch" else baseline)
    recheck_issue_list(rechecked, kind).reverse()
    responses = [{"content": [item]} for item in (
        baseline, current, rechecked if scope == "base" else baseline,
        rechecked if scope == "branch" else current,
    )]
    original = copy.deepcopy(responses)
    client = client_for()
    client.validate_content.side_effect = responses

    report, code = validate(tmp_path, client)

    assert code == 0
    assert report["comparison_status"] == "complete"
    assert report["existing_issues"] == report["current_issues"] == report["baseline_issues"] == expected
    assert report["new_issues"] == report["resolved_issues"] == 0
    assert responses == original
    assert [call.kwargs["branch_id"] for call in client.validate_content.call_args_list] == [None, BRANCH, None, BRANCH]


@pytest.mark.parametrize("scope", ["branch", "base"])
@pytest.mark.parametrize("kind", ["grouped_messages", "query_messages"])
@pytest.mark.parametrize("change", ["changed_message", "additional_duplicate"])
def test_recheck_normalization_does_not_hide_changed_messages_or_duplicate_counts(tmp_path, scope, kind, change):
    current, baseline = recheck_documents(kind)
    rechecked = copy.deepcopy(current if scope == "branch" else baseline)
    issues = recheck_issue_list(rechecked, kind)
    issues.reverse()
    if change == "changed_message":
        issues[-1] = "A different synthetic issue"
    else:
        issues.append(MESSAGE)
    responses = [{"content": [item]} for item in (
        baseline, current, rechecked if scope == "base" else baseline,
        rechecked if scope == "branch" else current,
    )]
    original = copy.deepcopy(responses)
    client = client_for()
    client.validate_content.side_effect = responses

    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)

    assert caught.value.exit_code == 4
    assert caught.value.reason_code == "document_identity_changed"
    assert responses == original
    assert not (tmp_path / "private/report.json").exists()
    assert not (tmp_path / "private/history.json").exists()


@pytest.mark.parametrize("scope", ["branch", "base"])
def test_malformed_recheck_preserves_the_actual_failing_scope(tmp_path, scope):
    current, baseline = document(DRAFT), document(PUBLISHED)
    malformed = document(DRAFT if scope == "branch" else PUBLISHED, filters=[{"private": MESSAGE}])
    client = client_for()
    client.validate_content.side_effect = [
        {"content": [baseline]}, {"content": [current]},
        {"content": [malformed if scope == "base" else baseline]},
        {"content": [malformed if scope == "branch" else current]},
    ]
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)
    assert caught.value.reason_code == "missing_issue_message"
    assert caught.value.validation_scope == scope
    assert caught.value.comparison_mode == "new_only"
    assert not (tmp_path / "private/history.json").exists()


@pytest.mark.parametrize("limit", ["count", "time"])
def test_lookup_search_is_bounded_and_never_treats_limit_as_no_new_errors(tmp_path, monkeypatch, limit):
    client = client_for(current=[document(f"draft-{index}") for index in range(3)], baseline=[], records=[])
    if limit == "count":
        monkeypatch.setattr(content_identity, "MAX_DRAFT_LOOKUPS", 2)
    else:
        monkeypatch.setattr(content_identity, "monotonic", Mock(side_effect=[0, 61]))
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)
    assert caught.value.reason_code == "document_identity_limit_exceeded"
    client.list_document_drafts.assert_not_called()
    client.get_document_identity.assert_not_called()


def test_draft_lookup_is_per_document_not_per_error_and_not_cached_between_runs(tmp_path):
    current = document(DRAFT, filters=[{"filter_name": FILTER, "issues": [MESSAGE] * 10}])
    baseline = document(PUBLISHED, filters=copy.deepcopy(current["dashboard_filter_issues"]))
    client = client_for([current], [baseline])
    for expected_calls in (2, 4):
        report, code = validate(tmp_path, client)
        assert code == 0 and report["existing_issues"] == 10
        assert client.list_document_drafts.call_count == expected_calls


def query(tile, presentation):
    return {"query_id_map_key": tile, "query_presentation_id": presentation,
            "query_name": "Synthetic private tile", "issues": [MESSAGE]}


@pytest.mark.parametrize("tile", ["stable-tile", "new-tile"])
def test_verified_draft_query_uses_stable_tile_not_regenerated_presentation_id(tmp_path, tile):
    current = document(DRAFT, filters=[], queries=[query(tile, "new-presentation")])
    current["queries_and_issues"][0][CANONICAL_QUERY] = "stable-tile"
    baseline = document(PUBLISHED, filters=[], queries=[query("stable-tile", "old-presentation")])
    report, code = validate(tmp_path, client_for([current], [baseline]))
    same = tile == "stable-tile"
    assert code == int(not same)
    assert report["existing_issues"] == int(same)
    assert report["new_issues"] == report["resolved_issues"] == int(not same)
    issue = next(issue for issue in report["issues"] if issue["active"])
    assert issue["raw"][CANONICAL_QUERY] == tile
    assert issue["query_presentation_id"] == "new-presentation"


@pytest.mark.parametrize("ambiguity", ["missing", "duplicate"])
def test_draft_query_without_unique_tile_identity_remains_incomplete(tmp_path, ambiguity):
    queries = [query("stable-tile", "new-presentation")]
    if ambiguity == "missing":
        queries[0].pop("query_id_map_key")
    else:
        queries.append(query("stable-tile", "another-presentation"))
    client = client_for([document(DRAFT, filters=[], queries=queries)],
                        [document(PUBLISHED, filters=[], queries=[query("stable-tile", "old-presentation")])])
    with pytest.raises(ContentEvidenceError) as caught:
        validate(tmp_path, client)
    assert caught.value.reason_code == "query_identity_unavailable"
    assert caught.value.exit_code == 4


@pytest.mark.parametrize("failed", [False, True])
def test_canonical_identity_stays_private_in_all_rendered_formats(tmp_path, failed):
    client = client_for()
    if failed:
        client.list_document_drafts.side_effect = lambda parent: [draft(draftOutOfDate=True)]
        with pytest.raises(ContentEvidenceError) as caught:
            validate(tmp_path, client)
        report = {"issues": [caught.value.report_issue()], "exit_code": 4, "policy_decision": "fail"}
    else:
        report, code = validate(tmp_path, client)
        assert code == 0
        report.update(exit_code=code, policy_decision="pass")
    output = tmp_path / "shareable"
    with diagnostic_session(), capture_private_reports() as receipts:
        safe = write_public_reports(report, output_dir=output,
                                    formats=["json", "markdown", "sarif", "junit"], redaction_level="standard")
        write_public_json(output / "public/evidence.json", report, redaction_level="standard")
    assert len(receipts) == 1
    assert safe["diagnostic_schema_version"] == 2
    if failed:
        assert safe["issues"][0]["reason_code"] == "stale_document_draft"
    else:
        assert receipts[0]["issues"][0]["raw"][CANONICAL_DOCUMENT] == PUBLISHED
        assert safe["existing_issues"] == 1 and safe["new_issues"] == 0
        assert safe["issues"][0]["blocking"] is False
    for name in ("report.json", "report.md", "report.sarif", "junit.xml", "evidence.json"):
        assert (output / "public" / name).exists()
    rendered = "\n".join(path.read_text() for path in output.rglob("*") if path.is_file())
    rendered += "\n".join(annotation_lines(safe["issues"]))
    for private in (PUBLISHED, DRAFT, BRANCH, MESSAGE, FILTER, CANONICAL_DOCUMENT, CANONICAL_QUERY,
                    "Synthetic private dashboard", "synthetic-model-identity", "synthetic-user"):
        assert private not in rendered
    for issue in report["issues"]:
        if "id" in issue:
            assert issue["id"] not in rendered
    assert json.loads((output / "public/report.json").read_text()) == safe
