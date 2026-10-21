from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from omniflow.exceptions import ConfigError, OmniAuthError
from omniflow.query_transport import QueryEvidenceError
from omniflow.query_validation import QueryProvenance, QueryValidationSettings, parse_query_validation
from omniflow.validators.query_validation import run_query_validation

FIELDS = ["fact.population", "fact.distinct", "fact.nulls", "dim.groups", "fact.total"]
PROVENANCE = QueryProvenance("model", "branch", "a" * 40, "b" * 40, True, True)
CLIENT = SimpleNamespace()


def policy(**overrides):
    value = {
        "mode": "enforce", "native_qualified": True, "snapshot_attestation": "reviewed stable snapshot evidence",
        "environment_connection_id": "environment", "user_id": "service-user", "timezone": "America/Chicago",
        "contracts": [{
            "id": "private-contract", "reviewed": True, "aggregate_only": True,
            "independent_oracle": True, "fixed_window": True,
            "query": {"table": "orders", "join_paths_from_topic_name": "orders", "fields": FIELDS,
                      "filters": {"fact.date": {"is": "2026-01-01"}}},
            "population_field": "fact.population", "minimum_population": 1,
            "assertions": [
                {"type": "unique_grain", "population": "fact.population", "distinct": "fact.distinct", "maximum": 0},
                {"type": "null_rate", "numerator": "fact.nulls", "denominator": "fact.population", "maximum": 0},
                {"type": "group_count", "field": "dim.groups", "minimum": 3, "maximum": 3},
                {"type": "aggregate_delta", "field": "fact.total", "max_absolute_change": 0},
            ],
        }],
    }
    value.update(overrides)
    return value


def row(**changes):
    result = dict(zip(FIELDS, (100, 100, 0, 3, 149116), strict=True))
    result.update(changes)
    return [result]


class Transport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.submissions = self.polls = 0

    def run(self, query, **kwargs):
        self.calls.append((copy.deepcopy(query), kwargs))
        self.submissions += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def validate(responses, *, settings=None, provenance=PROVENANCE):
    engine = Transport(responses)
    report, code = run_query_validation(
        CLIENT, "model", branch_id="branch", settings=settings or parse_query_validation(policy()),
        provenance=provenance, transport=engine,
    )
    return report, code, engine


def test_off_is_default_and_never_executes():
    report, code, engine = validate([], settings=QueryValidationSettings(), provenance=None)
    assert (report["query_validation_status"], code, engine.calls) == ("disabled", 0, [])
    assert QueryValidationSettings().timezone == ""
    assert parse_query_validation({"timezone": ""}).timezone == ""


def test_identical_fixed_queries_base_head_base_with_same_environment_and_no_rows_in_report():
    report, code, engine = validate([row(), row(), row()])
    assert code == 0 and report["query_validation_status"] == "passed"
    assert report["validation_complete"] and report["contracts_checked"] == 1
    assert [options["branch_id"] for _, options in engine.calls] == [None, "branch", None]
    assert all(query == engine.calls[0][0] for query, _ in engine.calls)
    assert engine.calls[0][0]["limit"] == 2
    assert all({k: v for k, v in options.items() if k != "branch_id"} == {
        "environment_connection_id": "environment", "user_id": "service-user", "timezone": "America/Chicago",
    } for _, options in engine.calls)
    assert not any(secret in json.dumps(report) for secret in ["private-contract", "149116", "fact.", "service-user"])


def test_wrong_join_total_unchanged_but_groups_collapse_and_nulls_block():
    report, code, _ = validate([row(), row(**{"dim.groups": 1, "fact.nulls": 100}), row()])
    assert code == 1 and report["new_issue_count"] == 2
    assert {issue["query_assertion_type"] for issue in report["issues"]} == {"null_rate", "group_count"}


def test_symmetric_total_alone_is_not_fanout_oracle_independent_grain_catches_duplicate_rows():
    report, code, _ = validate([row(), row(**{"fact.population": 200}), row()])
    assert code == 1 and report["new_issue_count"] == 1
    assert report["issues"][0]["query_assertion_type"] == "unique_grain"
    # Same semantic total/distinct measures can mask fanout: contracts need an
    # independently reviewed raw grain population, not just measure agreement.
    with_no_grain = policy()
    with_no_grain["contracts"][0]["independent_oracle"] = False
    report, code, engine = validate([], settings=parse_query_validation(with_no_grain))
    assert code == 4 and not engine.calls
    assert report["issues"][0]["reason_code"] == "query_contract_unreviewed"


@pytest.mark.parametrize("before,after,code,new,existing", [(10, 10, 0, 0, 1), (10, 5, 0, 0, 1), (10, 20, 1, 1, 0)])
def test_unchanged_improved_and_worsened_null_debt(before, after, code, new, existing):
    report, actual, _ = validate([row(**{"fact.nulls": before}), row(**{"fact.nulls": after}),
                                row(**{"fact.nulls": before})])
    assert (actual, report["new_issue_count"], report["existing_issue_count"]) == (code, new, existing)


@pytest.mark.parametrize("bad,reason", [
    ([], "query_population_insufficient"),
    (row(**{"fact.population": 0}), "query_population_insufficient"),
    (row() + row(), "query_result_not_scalar"),
    ([{"unexpected": 1}], "query_result_schema_invalid"),
    (row(**{"fact.total": None}), "query_result_schema_invalid"),
    (row(**{"fact.total": float("nan")}), "query_result_schema_invalid"),
    (row(**{"fact.total": "149116"}), "query_result_schema_invalid"),
    (row(**{"fact.total": 10**400}), "query_metric_invalid"),
])
def test_empty_truncated_and_invalid_result_evidence_is_never_a_pass(bad, reason):
    report, code, _ = validate([bad])
    assert code == 4 and not report["validation_complete"]
    assert report["issues"][0]["reason_code"] == reason


def test_base_reread_drift_invalidates_apparent_regression():
    report, code, _ = validate([row(), row(**{"fact.nulls": 100}), row(**{"fact.total": 149117})])
    assert code == 4 and report["new_issue_count"] == 0
    assert report["issues"][0]["reason_code"] == "query_data_drift"


@pytest.mark.parametrize("provenance", [None, replace(PROVENANCE, base_verified=False),
    replace(PROVENANCE, head_verified=False), replace(PROVENANCE, model_id="other"),
    replace(PROVENANCE, branch_id="other"), replace(PROVENANCE, head_sha="main")])
def test_source_identity_required_even_if_other_validators_are_disabled(provenance):
    report, code, engine = validate([], provenance=provenance)
    assert code == 4 and not engine.calls
    assert report["issues"][0]["reason_code"] == "query_provenance_unverified"


@pytest.mark.parametrize("change,reason", [
    ({"native_qualified": False}, "query_native_qualification_required"),
    ({"snapshot_attestation": ""}, "query_snapshot_unattested"),
    ({"max_queries": 2}, "query_budget_exhausted"),
])
def test_enforcement_prerequisites_before_spend(change, reason):
    report, code, engine = validate([], settings=parse_query_validation(policy(**change)))
    assert code == 4 and not engine.calls and report["issues"][0]["reason_code"] == reason


def test_report_mode_never_enforces_and_unattested_data_never_claims_pr_causality():
    settings = parse_query_validation(policy(mode="report", snapshot_attestation="", native_qualified=False))
    report, code, _ = validate([row(), row(**{"fact.nulls": 10}), row()], settings=settings)
    assert code == 0 and not report["validation_complete"] and report["new_issue_count"] == 0
    assert report["execution_status"] == "partial"
    assert all(not item["blocking"] for item in report["issues"])


def test_auth_failure_preserves_safe_cause_and_stops_more_submissions():
    failure = OmniAuthError("SECRET", reason_code="omni_forbidden", diagnostic_context={"http_status": 403})
    report, code, engine = validate([failure])
    assert code == 3 and len(engine.calls) == 1
    assert report["issues"][0]["http_status"] == 403 and "SECRET" not in json.dumps(report)


def test_absolute_added_entity_requires_root_verified_membership_and_rereads_head():
    value = policy()
    value["contracts"][0]["comparison"] = "absolute"
    value["contracts"][0]["assertions"] = [{"type": "group_count", "field": "dim.groups", "minimum": 3}]
    settings = parse_query_validation(value)
    report, code, engine = validate([], settings=settings)
    assert code == 4 and not engine.calls and report["issues"][0]["reason_code"] == "query_added_entity_unverified"
    provenance = replace(PROVENANCE, base_verified=False, added_entity_contract_ids=frozenset({"private-contract"}))
    report, code, engine = validate([row(**{"dim.groups": 1})] * 2, settings=settings, provenance=provenance)
    assert code == 1 and [options["branch_id"] for _, options in engine.calls] == ["branch", "branch"]


def test_relative_zero_and_invalid_count_metrics_are_inconclusive():
    value = policy()
    value["contracts"][0]["assertions"] = [{"type": "aggregate_delta", "field": "fact.total", "max_relative_change": .1}]
    report, code, _ = validate([row(**{"fact.total": 0})] * 3, settings=parse_query_validation(value))
    assert code == 4 and report["issues"][0]["reason_code"] == "query_relative_baseline_zero"
    report, code, _ = validate([row(), row(**{"fact.nulls": 101}), row()])
    assert code == 4 and report["issues"][0]["reason_code"] == "query_metric_invalid"


@pytest.mark.parametrize("modification", [
    lambda value: value.update(mode=[]), lambda value: value.update(max_queries=True),
    lambda value: value.update(unknown=1), lambda value: value.update(native_qualified="yes"),
    lambda value: value.update(timezone="not-a-zone"), lambda value: value.update(snapshot_attestation="${TOKEN}"),
    lambda value: value["contracts"][0]["query"].update(branch_id="different"),
    lambda value: value["contracts"][0]["query"].update(sql="select 1"),
    lambda value: value["contracts"][0]["query"]["filters"].update(dynamic="${WINDOW}"),
    lambda value: value["contracts"][0]["assertions"][0].update(maximum=float("nan")),
])
def test_strict_configuration_and_no_runtime_interpolation(modification):
    value = policy()
    modification(value)
    with pytest.raises(ConfigError) as error:
        parse_query_validation(value)
    assert error.value.reason_code == "invalid_query_validation_config"


def test_shared_budget_timeout_never_silently_uses_partial_contracts():
    report, code, _ = validate([row(), QueryEvidenceError("query_budget_exhausted")])
    assert code == 4 and report["contracts_checked"] == 0 and report["new_issue_count"] == 0


def test_integer_precision_loss_cannot_hide_a_one_unit_aggregate_regression():
    report, code, engine = validate([row(**{"fact.total": 2**53}), row(**{"fact.total": 2**53 + 1})])
    assert code == 4 and report["query_validation_status"] == "inconclusive"
    assert report["issues"][0]["reason_code"] == "query_metric_invalid" and len(engine.calls) == 2


@pytest.mark.parametrize("assertion,change", [
    ({"type": "rate_delta", "numerator": "fact.nulls", "denominator": "fact.population",
      "max_absolute_change": .01}, {"fact.nulls": 2}),
    ({"type": "aggregate_range", "field": "fact.total", "minimum": 100, "maximum": 150000},
     {"fact.total": 150001}),
    ({"type": "aggregate_delta", "field": "fact.total", "max_relative_change": .01},
     {"fact.total": 151000}),
])
def test_rate_and_aggregate_assertions_are_deterministic(assertion, change):
    value = policy()
    value["contracts"][0]["assertions"] = [assertion]
    report, code, _ = validate([row(), row(**change), row()], settings=parse_query_validation(value))
    assert code == 1 and report["new_issue_count"] == 1
    assert report["issues"][0]["query_assertion_type"] == assertion["type"]


def labeled_policy():
    value = policy()
    value["contracts"][0]["result_fields"] = dict(zip(FIELDS, [
        "Orders Count", "Orders Unique Count", "Orders Null Count", "Stores Count", "Orders Total",
    ], strict=True))
    value["contracts"][0]["numeric_encoding"] = "number_or_decimal_string"
    return value


def labeled_row(**changes):
    original = row(**changes)[0]
    mapping = labeled_policy()["contracts"][0]["result_fields"]
    return [{mapping[key]: str(value) for key, value in original.items()}]


def test_explicit_reviewed_label_mapping_and_decimal_strings_same_base_head_contract():
    settings = parse_query_validation(labeled_policy())
    report, code, engine = validate([labeled_row(), labeled_row(**{"dim.groups": 1, "fact.nulls": 100}), labeled_row()],
                                    settings=settings)
    assert code == 1 and report["new_issue_count"] == 2
    assert all(query["fields"] == FIELDS for query, _ in engine.calls)
    assert "Orders Count" not in json.dumps(report)
    assert "Orders Count" not in json.dumps(engine.calls)  # Output labels never rewrite the executed query.


def test_labels_are_never_guessed_and_numeric_string_acceptance_is_explicit():
    report, code, _ = validate([labeled_row()])
    assert code == 4 and report["issues"][0]["reason_code"] == "query_result_schema_invalid"
    value = labeled_policy()
    value["contracts"][0]["numeric_encoding"] = "json_number"
    report, code, _ = validate([labeled_row()], settings=parse_query_validation(value))
    assert code == 4 and report["issues"][0]["reason_code"] == "query_result_schema_invalid"


@pytest.mark.parametrize("bad", ["1,000", "$100", "NaN", "Infinity", "1e2", " 100 ", "", "001", "0" * 101])
def test_decimal_strings_reject_formatting_nonfinite_and_unbounded_values(bad):
    data = labeled_row()
    data[0]["Orders Total"] = bad
    report, code, _ = validate([data], settings=parse_query_validation(labeled_policy()))
    assert code == 4 and report["issues"][0]["reason_code"] == "query_result_schema_invalid"


def test_decimal_precision_is_preserved_and_integer_precision_limits_remain_enforced():
    before = labeled_row()
    after = labeled_row()
    before[0]["Orders Total"] = "1.00000000000000000000000000001"
    after[0]["Orders Total"] = "1.00000000000000000000000000002"
    report, code, _ = validate([before, after, before], settings=parse_query_validation(labeled_policy()))
    assert code == 1 and report["new_issue_count"] == 1
    after[0]["Orders Total"] = "9007199254740993.0"
    report, code, _ = validate([after], settings=parse_query_validation(labeled_policy()))
    assert code == 4 and report["issues"][0]["reason_code"] == "query_metric_invalid"


@pytest.mark.parametrize("modification", [
    lambda contract: contract["result_fields"].pop("fact.total"),
    lambda contract: contract["result_fields"].update(extra="Extra"),
    lambda contract: contract["result_fields"].update({"fact.total": "Orders Count"}),
    lambda contract: contract["result_fields"].update({"fact.total": "${DYNAMIC}"}),
    lambda contract: contract.update(numeric_encoding="guess"),
])
def test_result_mapping_requires_reviewed_complete_bijection(modification):
    value = labeled_policy()
    modification(value["contracts"][0])
    with pytest.raises(ConfigError):
        parse_query_validation(value)


def test_changed_or_extra_returned_labels_remain_schema_failures():
    data = labeled_row()
    data[0]["Unexpected Label"] = data[0].pop("Orders Total")
    report, code, _ = validate([data], settings=parse_query_validation(labeled_policy()))
    assert code == 4 and report["issues"][0]["reason_code"] == "query_result_schema_invalid"
