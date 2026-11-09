"""Recovery instructions for an already projected, public-safe finding."""
from __future__ import annotations

import re

from .reporting.issue_policy import is_blocking

# Each anchor has a matching procedure in docs/ERROR_CODES.md.
CHECK_GUIDES = {
    "setup": "setup", "context": "setup", "candidate": "compare-branches",
    "model": "model-validation", "content": "content-validation",
    "semantic_diff": "semantic-changes", "semantic_lint": "semantic-changes",
    "contracts": "dependencies", "downstream": "dependencies",
    "dbt_exposures": "dbt-impact", "dbt_impact": "dbt-impact", "dbt_sync": "dbt-sync",
    "ai_eval": "ai-evaluation", "ai_repair": "repair", "query_validation": "query-validation",
    "breaking_change_hold": "deployment-hold", "reporting": "publish-reports", "execution": "get-help",
}
GUIDE_ANCHORS = frozenset({*CHECK_GUIDES.values(), "file-contents", "api-access", "api-response"})
ACTION_REPOSITORIES = frozenset({"exploreomni/OmniFlow", "atx-omni/OmniFlow"})
ENDING_NAMES = {"lf": "LF", "crlf": "CRLF", "cr": "CR", "mixed": "mixed line endings"}


def _mismatch_remedy(issue: dict) -> tuple[str, str]:
    """Describe only what the first mismatched pair proves, using fixed vocabulary."""
    kind = issue.get("difference_class")
    base = issue.get("validation_scope") == "base"
    git_side = "target branch" if base else "PR branch"
    omni_side = "shared Omni model" if base else "Omni branch"
    if kind == "terminal_newline_only":
        missing = issue.get("newline_missing_from")
        if missing == "git":
            return ("The Git file is missing the final newline present in Omni.",
                    f"Add the final newline to the Git file, commit the fix to the {git_side}, and rerun after sync.")
        if missing == "omni":
            return ("The Omni file is missing the final newline present in Git.",
                    f"Open the file in the {omni_side}, match the intended final newline, save and sync, then rerun.")
        return ("Only the trailing line breaks differ.",
                "Match the line breaks at the end of the two files, save and sync the intended version, then rerun.")
    if kind == "line_endings_only":
        left, right = ENDING_NAMES.get(issue.get("git_line_ending")), ENDING_NAMES.get(issue.get("omni_line_ending"))
        detail = f" Git uses {left} and Omni uses {right}." if left and right else ""
        return ("The two copies use different line endings." + detail,
                f"Use your editor's line-ending setting to match the intended file on the {git_side} and in Omni, then sync and rerun.")
    if kind == "yaml_comment_or_whitespace_only":
        return ("Only YAML comments or formatting differ in this pair.",
                "Find the differing line in the private Summary or local comparison, match the intended comment or formatting, then sync and rerun.")
    if kind == "authored_text_changed":
        return ("The authored YAML text differs beyond comments or whitespace.",
                f"Compare the Git and Omni lines in the local diagnostic. Save the intended definition in the {omni_side} and {git_side}, then rerun.")
    return ("A model file has different text in Git and Omni.",
            "Use the local comparison in the guide to locate the differing line, then sync the version you intend to validate.")


def guide_url(anchor: str, action_revision: str | None = None, action_repository: str | None = None) -> str:
    if not isinstance(anchor, str) or anchor not in GUIDE_ANCHORS:
        return ""
    ref = action_revision.lower() if isinstance(action_revision, str) and re.fullmatch("[0-9a-fA-F]{40}", action_revision) else "main"
    repo = action_repository if isinstance(action_repository, str) and action_repository in ACTION_REPOSITORIES else "exploreomni/OmniFlow"
    return f"https://github.com/{repo}/blob/{ref}/docs/ERROR_CODES.md#guide-{anchor}"


def recovery_details(issue: dict) -> dict:
    """All free text in the returned guidance comes from the fixed catalog."""
    validator = issue.get("validator", "execution")
    category = issue["category"]
    code = issue["diagnostic_code"]
    action = issue["next_action"]
    anchor = CHECK_GUIDES.get(validator, "get-help")
    outcome = "Blocks under the configured policy" if is_blocking(issue) else "Informational"
    steps = [action]
    verify = "Run the affected check again and confirm it completed with the intended result."
    support = "Give the OmniFlow maintainer the code, failed check and run reference from this report."
    coverage = None
    difference = None

    if code == "OF-PR-CHANGE-NOT-SYNCHRONIZED":
        base = issue.get("validation_scope") == "base"
        scope = "shared Omni model" if base else "Omni branch"
        commit = "target commit" if base else "PR head commit"
        left, right = issue.get("git_only_count"), issue.get("omni_only_count")
        fix = f"Compare this PR’s changed files at the {commit} with the saved {scope}. Review the intended edit, addition or deletion and synchronize that scope."
        if issue.get("contents_comparison") == "different":
            difference = _mismatch_remedy(issue)[0]
            if issue.get("source_difference_role") == "referenced_dependency":
                difference = "A definition used by the PR has different contents in Git and Omni."
                fix = f"Compare the referenced definition at the {commit} with its saved file in the {scope}. Commit the intended dependency or remove the stale Omni change, then rerun."
            elif issue.get("source_fallback_reason"):
                fix = f"Whole-model verification found a difference. Compare the reported definition at the {commit} with its saved file in the {scope}, synchronize the intended version, then rerun."
            else:
                fix = f"Compare the changed Git definition at the {commit} with its saved file in the {scope}. Confirm the intended definition and synchronize it."
        elif left and right == 0:
            noun = "file" if left == 1 else "files"
            difference = f"Git has {left:,} {noun} in the changed scope with no matching file in the {scope}."
            fix = f"Check the added or renamed files at the {commit}. Confirm each intended definition is saved in the {scope}, then synchronize."
        elif right and left == 0:
            noun = "file" if right == 1 else "files"
            difference = f"The {scope} has {right:,} {noun} with no matching file in Git’s changed scope."
            fix = f"Review deletions and renames at the {commit}. Check whether the {scope} still contains the old definition, then synchronize the intended removal or rename."
        elif left and right:
            difference = f"The changed scope has unmatched files on both sides: Git-only {left:,}, Omni-only {right:,}."
        else:
            difference = "OmniFlow could not confirm the expected identity or absence of a changed artifact."
        steps = [fix, f"Rerun against the current {commit} after synchronization finishes."]
        verify = "The changed-file check completes and the model/content validators can compare PR impact."
    elif code == "OF-CANDIDATE-CONTENT":
        anchor = "file-contents"
        base = issue.get("validation_scope") == "base"
        revision = "target (base)" if base else "PR head"
        difference, fix = _mismatch_remedy(issue)
        steps = [
            fix,
            ("Use omniflow candidate diagnose locally with --scope base to compare the shared model and target commit." if base else
             "If the difference persists, ask an Omni Organization Admin to check the PR commit in Model → Advanced → Webhook Deliveries."),
        ]
        total, compared = issue.get("git_file_count"), issue.get("compared_file_count")
        if (type(total) is int and type(compared) is int and 0 < compared <= total
                and total == issue.get("omni_file_count") and issue.get("git_only_count") == issue.get("omni_only_count") == 0):
            if total == 1:
                coverage = "The only file matched by name. Its text differs."
            elif compared == total:
                coverage = f"All {total:,} file identities matched. Only the last file differs in text."
            else:
                remaining = total - compared
                noun = "file" if remaining == 1 else "files"
                coverage = (f"All {total:,} file identities matched. The first text difference was file {compared:,}. "
                            f"Comparison stopped there with {remaining:,} {noun} unchecked. At least one file differs.")
        verify = f"A fresh run verifies the {revision} commit and proceeds to compare PR impact."
    elif code in {"OF-MODEL-METADATA-MISSING", "OF-MISSING-POLICY-CONFIG"}:
        anchor = "setup"
        metadata = code == "OF-MODEL-METADATA-MISSING"
        filename = ".omni/flow.json" if metadata else ".omniflow.yml (or the workflow's configured policy path)"
        steps = [
            f"Open the PR target branch in GitHub and check the repository root for {filename}.",
            ("Add the model registration to .omni/flow.json on that target branch through your normal review process." if metadata else
             "Add the trusted policy file on that target branch through your normal review process."),
            "Run validation again with the updated target revision.",
        ]
        verify = "The route step loads the trusted file and selects the intended model or explains the next issue."
    elif category == "access" or code.startswith(("OF-GITHUB-", "OF-OMNI-")):
        anchor = "api-access" if category == "access" else "api-response"
        steps = ["Find the service, operation and HTTP status in the report, when available.", action]
        verify = "Confirm the original operation succeeds using the intended credential and scope."
    elif category == "service" or issue.get("reason_code") in {"empty_api_response", "invalid_json_response"}:
        anchor = "api-response"
        steps = [action, "For an operation that started a job, confirm its status before retrying."]
        verify = "The original operation returns a complete result for this scope."
    elif validator in {"model", "content"} and category == "validation":
        if issue.get("health_only"):
            steps = ["Open the primary blocker in this run and follow its recovery steps first."]
            verify = "A fresh run completes the baseline comparison and identifies any new findings."
        elif issue.get("state") in {"existing", "resolved", "improved"} and not is_blocking(issue):
            steps = ["Use the comparison counts to track existing, improved, or resolved findings separately from PR blockers."]
            verify = "Use the blocking findings section to decide what the PR needs."
        elif issue.get("state") == "existing" and is_blocking(issue):
            steps = ["Check the policy on the target branch. This existing finding blocks under that policy.", action]
            verify = "Confirm a fresh comparison no longer lists this finding as blocking."
        else:
            steps = [action, "Review the same finding on the target and PR branch before editing the affected definition."]
            verify = "A fresh comparison reports the expected new, existing or resolved count for this check."
    elif validator in {"dbt_sync", "ai_repair", "ai_eval", "query_validation"}:
        steps = [action, "If a job was started, reconcile its status before launching another."]
        verify = "Check the recorded final operation and its validation or cleanup result."
    elif anchor == "compare-branches":
        steps = ["Find the selected scope and corresponding Git revision in the failed run.", action]
        verify = "The same scope verifies against its intended revision on a fresh run."
    elif anchor == "setup":
        steps = ["Open the first failed setup or routing step and find the named setting or file.", action]
        verify = "Setup completes and the workflow selects its intended operation."
    elif anchor == "semantic-changes":
        steps = [action, "Save the reviewed definition on the branch and rerun the rule or semantic comparison."]
        verify = "Check the rule or change type on the saved branch and review its updated blocking count."
    elif anchor in {"dependencies", "dbt-impact"}:
        steps = [action, "Check the reported dependency coverage for the changed field or relation."]
        verify = "The affected dependency is covered and its contract or impact result is available."
    elif anchor == "deployment-hold":
        steps = [action, "Request revalidation for the latest PR commit after the deployment is reconciled."]
        verify = "The readiness check evaluates that commit against the confirmed deployment state."
    elif anchor == "publish-reports":
        steps = [action, "Open the upload and publication steps for this run to confirm which outputs were delivered."]
        verify = "The latest run links to the intended summary or a confirmed artifact."
    elif anchor == "get-help":
        steps = ["Open the failed step in the run and note the operation and its last reported status.", action]
        verify = "Have the maintainer identify the failure before retrying an operation with an unknown outcome."

    if (validator in {"dbt_sync", "ai_repair", "ai_eval", "query_validation"}
            and anchor in {"api-access", "api-response"}):
        steps.insert(0, "Check whether this operation already started a job and reconcile it before retrying.")

    if is_blocking(issue) and category != "validation" and (
        issue.get("coverage_complete") is False or category in {"synchronization", "incomplete_evidence", "access", "service", "configuration", "internal"}
    ):
        outcome = "Blocks the PR because validation is incomplete"
    result = {"recovery_steps": steps, "verify_recovery": verify, "support_action": support,
              "guide_anchor": anchor, "outcome_label": outcome}
    if difference:
        result["difference_description"] = difference
    if coverage:
        result["coverage_description"] = coverage
    return result
