"""Opt-in deterministic query observations, not AI correctness judgments.

Only trusted scalar aggregate contracts execute. Each regression probe is sent
unchanged to base/head/base. The second base read detects observed drift but
does not establish a transactional warehouse snapshot. Source revalidation
after this function remains the caller's responsibility.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, localcontext
from typing import Any

from ..exceptions import ExitCodes, OmniFlowError, exception_diagnostic
from ..query_transport import QueryEvidenceError, QueryTransport
from ..query_validation import QueryAssertion, QueryContract, QueryProvenance, QueryValidationSettings

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DECIMAL_STRING = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_SAFE_TRANSPORT = {
    "service", "api_operation", "http_status", "attempt_count", "max_attempts", "retryable",
    "pending_job_count", "remote_execution_may_continue",
}


def _issue(reason: str, *, ordinal: int | None = None, blocking: bool, state: str = "unknown", **details: Any) -> dict:
    message = (
        "A reviewed query contract detected a new or worsened deterministic finding."
        if reason == "query_contract_regression" else
        "A reviewed query contract has unchanged or improved baseline findings."
        if reason == "query_contract_existing" else
        "Query comparison is inconclusive; inspect its reason before attributing a change to the PR."
    )
    result = {
        "validator": "query_validation",
        "type": "query_validation_issue" if reason in {"query_contract_regression", "query_contract_existing"}
        else "query_validation_unavailable", "reason_code": reason,
        "severity": "error" if blocking else "warning", "message": message,
        "blocking": blocking, "state": state,
    }
    if ordinal is not None:
        result["query_contract_number"] = ordinal
    result.update(details)
    return result


def _prerequisite(settings: QueryValidationSettings, model_id: str, branch_id: str | None,
                  provenance: QueryProvenance | None) -> str | None:
    if (provenance is None or not branch_id or provenance.model_id != model_id
            or provenance.branch_id != branch_id or provenance.head_verified is not True
            or not isinstance(provenance.head_sha, str) or not _SHA.fullmatch(provenance.head_sha)):
        return "query_provenance_unverified"
    if settings.mode not in {"report", "enforce"} or not settings.contracts:
        return "query_contract_unreviewed"
    for contract in settings.contracts:
        if not all(value is True for value in (
            contract.reviewed, contract.aggregate_only, contract.independent_oracle, contract.fixed_window,
        )):
            return "query_contract_unreviewed"
        if contract.comparison == "regression":
            if (provenance.base_verified is not True or not isinstance(provenance.base_sha, str)
                    or not _SHA.fullmatch(provenance.base_sha)):
                return "query_provenance_unverified"
        elif contract.comparison == "absolute":
            if contract.id not in provenance.added_entity_contract_ids:
                return "query_added_entity_unverified"
        else:
            return "query_contract_unreviewed"
    if settings.mode == "enforce" and not settings.native_qualified:
        return "query_native_qualification_required"
    if settings.mode == "enforce" and not settings.snapshot_attestation.strip():
        return "query_snapshot_unattested"
    planned = sum(3 if item.comparison == "regression" else 2 for item in settings.contracts)
    if planned > settings.max_queries:
        return "query_budget_exhausted"
    return None


def _scalar(rows: Any, contract: QueryContract) -> dict[str, Decimal]:
    if not isinstance(rows, list) or not rows:
        raise QueryEvidenceError("query_population_insufficient")
    if len(rows) >= 2:
        raise QueryEvidenceError("query_result_not_scalar")
    row = rows[0]
    names = dict(contract.result_fields) if contract.result_fields else {name: name for name in contract.fields}
    if (set(names) != set(contract.fields) or len(set(names.values())) != len(names)
            or not isinstance(row, dict) or set(row) != set(names.values())):
        raise QueryEvidenceError("query_result_schema_invalid")
    result: dict[str, Decimal] = {}
    for key, label in names.items():
        value = row[label]
        if type(value) is int and abs(value) > 2**53:
            # Converting a larger integer to float could hide a one-unit change.
            raise QueryEvidenceError("query_metric_invalid")
        if isinstance(value, str):
            if (contract.numeric_encoding != "number_or_decimal_string" or len(value) > 100
                    or not _DECIMAL_STRING.fullmatch(value)):
                raise QueryEvidenceError("query_result_schema_invalid")
            number = Decimal(value)
            if number == number.to_integral_value() and abs(number) > 2**53:
                raise QueryEvidenceError("query_metric_invalid")
        else:
            try:
                valid = type(value) in {int, float} and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid:
                raise QueryEvidenceError("query_result_schema_invalid")
            number = Decimal(str(value))
        result[key] = number
    population = _count(result[contract.population_field])
    if population < contract.minimum_population:
        raise QueryEvidenceError("query_population_insufficient")
    return result


def _count(value: Decimal) -> Decimal:
    if value < 0 or value != value.to_integral_value() or value > 2**53:
        raise QueryEvidenceError("query_metric_invalid")
    return value


def _rate(row: dict[str, Decimal], numerator: str, denominator: str) -> Decimal:
    total, count = _count(row[denominator]), _count(row[numerator])
    if total <= 0:
        raise QueryEvidenceError("query_population_insufficient")
    if count > total:
        raise QueryEvidenceError("query_metric_invalid")
    return count / total


def _distance(assertion: QueryAssertion, row: dict[str, Decimal]) -> tuple[str | None, Decimal]:
    if assertion.kind == "null_rate":
        value = _rate(row, assertion.numerator, assertion.denominator)
    elif assertion.kind == "unique_grain":
        value = 1 - _rate(row, assertion.distinct, assertion.population)
    else:
        value = row[assertion.field]
        if assertion.kind == "group_count":
            _count(value)
    # Opposite bounds are different defects even when their magnitudes match.
    # Do not waive a newly excessive value because the baseline was too small.
    if assertion.minimum is not None and value < Decimal(str(assertion.minimum)):
        return "lower", Decimal(str(assertion.minimum)) - value
    if assertion.maximum is not None and value > Decimal(str(assertion.maximum)):
        return "upper", value - Decimal(str(assertion.maximum))
    return None, Decimal(0)


def _delta_failed(assertion: QueryAssertion, base: dict[str, Decimal], head: dict[str, Decimal]) -> bool:
    if assertion.kind == "rate_delta":
        before = _rate(base, assertion.numerator, assertion.denominator)
        after = _rate(head, assertion.numerator, assertion.denominator)
    else:
        before, after = base[assertion.field], head[assertion.field]
    delta = abs(after - before)
    if assertion.max_absolute_change is not None and delta > Decimal(str(assertion.max_absolute_change)):
        return True
    if assertion.max_relative_change is not None:
        if before == 0:
            # A relative comparison has no defined denominator, including 0/0.
            raise QueryEvidenceError("query_relative_baseline_zero")
        if delta / abs(before) > Decimal(str(assertion.max_relative_change)):
            return True
    return False


def _observations(
    contract: QueryContract, head: dict[str, Decimal], base: dict[str, Decimal] | None,
) -> list[tuple[int, str, str]]:
    observations = []
    for number, assertion in enumerate(contract.assertions, 1):
        if assertion.kind in {"aggregate_delta", "rate_delta"}:
            if base is None:
                raise QueryEvidenceError("query_metric_invalid")
            if _delta_failed(assertion, base, head):
                observations.append((number, assertion.kind, "new"))
            continue
        current_bound, current = _distance(assertion, head)
        previous_bound, previous = _distance(assertion, base) if base is not None else (None, Decimal(0))
        if current > 0 and (current_bound != previous_bound or current > previous):
            observations.append((number, assertion.kind, "new"))
        elif current > 0:
            observations.append((number, assertion.kind, "existing"))
    return observations


def run_query_validation(
    client: Any, model_id: str, *, branch_id: str | None, settings: QueryValidationSettings,
    provenance: QueryProvenance | None, transport: QueryTransport | None = None,
) -> tuple[dict, int]:
    """Safe report + exit code; raw values and private contract names never leave.

    Passing means these reviewed contracts matched under the supplied attested
    context, not that all data/model behavior is correct. Report mode never
    enforces these optional checks. Provenance failure never executes queries.
    """
    report: dict[str, Any] = {
        "validator": "query_validation", "query_validation_mode": settings.mode,
        "query_validation_status": "disabled" if not settings.enabled else "inconclusive",
        "execution_status": "skipped", "validation_complete": False,
        "comparison_status": "unavailable", "contracts_total": len(settings.contracts),
        "contracts_checked": 0, "new_issue_count": 0, "existing_issue_count": 0,
        "query_submission_count": 0, "query_poll_count": 0,
        "snapshot_attested": bool(settings.snapshot_attestation.strip()),
        "native_qualified": settings.native_qualified, "issues": [],
    }
    if not settings.enabled:
        return report, ExitCodes.SUCCESS
    enforce = settings.mode == "enforce"
    reason = _prerequisite(settings, model_id, branch_id, provenance)
    if reason:
        report["issues"].append(_issue(reason, blocking=enforce))
        return report, ExitCodes.OMNI_API_ERROR if enforce else ExitCodes.SUCCESS
    engine = transport or QueryTransport(
        client, timeout_seconds=settings.timeout_seconds, max_response_bytes=settings.max_response_bytes,
        max_queries=settings.max_queries, max_polls=settings.max_polls,
    )
    failures: list[int] = []
    observed: list[tuple[int, list[tuple[int, str, str]]]] = []
    report["execution_status"] = "completed"
    for ordinal, contract in enumerate(settings.contracts, 1):
        query = {**contract.query(), "modelId": model_id, "limit": 2}
        options = {
            "environment_connection_id": settings.environment_connection_id,
            "user_id": settings.user_id, "timezone": settings.timezone,
        }
        try:
            baseline = None
            if contract.comparison == "regression":
                baseline = _scalar(engine.run(query, branch_id=None, **options), contract)
            current = _scalar(engine.run(query, branch_id=branch_id, **options), contract)
            if baseline is not None:
                reread = _scalar(engine.run(query, branch_id=None, **options), contract)
                if reread != baseline:
                    raise QueryEvidenceError("query_data_drift")
            else:
                # An absolute new-entity probe has no base. Re-read head rather
                # than inventing a clean baseline or assuming stable results.
                reread = _scalar(engine.run(query, branch_id=branch_id, **options), contract)
                if reread != current:
                    raise QueryEvidenceError("query_data_drift")
            # Preserve up to the bounded 100-character decimal-string precision;
            # never let binary float conversion conceal an aggregate difference.
            with localcontext() as decimal_context:
                decimal_context.prec = 128
                observed.append((ordinal, _observations(contract, current, baseline)))
            report["contracts_checked"] += 1
        except OmniFlowError as exc:
            diagnostic = exception_diagnostic(exc)
            reason = diagnostic.get("reason_code", "query_evidence_unavailable")
            report["issues"].append(_issue(
                reason, ordinal=ordinal, blocking=enforce,
                **{key: value for key, value in diagnostic.items() if key in _SAFE_TRANSPORT},
            ))
            failures.append(exc.exit_code)
            report["execution_status"] = "partial"
            # Stop additional spend after unavailable evidence, including auth,
            # unfinished work, malformed data, and exhausted shared budgets.
            break
    report["query_submission_count"] = engine.submissions
    report["query_poll_count"] = engine.polls
    if not report["snapshot_attested"]:
        report["issues"].append(_issue("query_snapshot_unattested", blocking=enforce))
        failures.append(ExitCodes.OMNI_API_ERROR)
        report["execution_status"] = "partial"
    if failures:
        # Do not attribute a partial set of comparisons to the PR. Successful
        # observations are discarded when any contract lacks complete evidence.
        return report, (ExitCodes.AUTHORIZATION_ERROR if ExitCodes.AUTHORIZATION_ERROR in failures else
                        ExitCodes.OMNI_API_ERROR) if enforce else ExitCodes.SUCCESS
    for ordinal, findings in observed:
        for number, kind, state in findings:
            new = state == "new"
            report["new_issue_count" if new else "existing_issue_count"] += 1
            report["issues"].append(_issue(
                "query_contract_regression" if new else "query_contract_existing", ordinal=ordinal,
                blocking=enforce and new, state=state, occurrence_count=1,
                query_assertion_type=kind, query_assertion_number=number,
            ))
    report.update(
        validation_complete=True, comparison_status="complete",
        query_validation_status="regression" if report["new_issue_count"] else "passed",
    )
    return report, ExitCodes.VALIDATION_FAILED if enforce and report["new_issue_count"] else ExitCodes.SUCCESS
