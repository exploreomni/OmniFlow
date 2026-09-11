# Validation Diagnostics and Upgrading

## First distinguish the failure

| Report finding | Meaning | Safe next step |
| --- | --- | --- |
| Content validation / Dashboard filter or Query | Omni returned a readable validation issue. | Inspect that document/query in the same model and branch before changing a filter or field. |
| Content validation / coverage unavailable | OmniFlow could not read the issue details. This is incomplete evidence, not proof of a particular content defect. | Keep the gate blocked; gather sanitized original evidence privately. |
| Downstream contracts / Coverage unavailable | Dependency analysis could not establish the affected consumers. | Verify the Action revision, relationship endpoints, model identity and reference-search access. |
| Validation execution failed / incomplete | A check could not finish because of an API, context, or internal failure. | Retain completed findings, resolve the failed check, then rerun. Later checks marked “not run” are not successful. |
| Advisory warning | The finding does not block under the recorded policy. | Review separately from blockers. Model warnings still block when `model_validation.fail_on_warnings` is enabled. |

No returned references is not proof of no consumers when coverage is unavailable. Content history counts (new/existing/resolved) describe content validation only; they are not the total blocking count across model, lint, contract and other checks. Missing optional results do not establish that those checks ran successfully.

## Upgrading from 0.4.0: relationship-coverage errors

An old semantic-diff defect omitted joined-view metadata on valid added or modified relationships. The resulting false blocker reads:

> Relationship impact could not be derived because the semantic diff did not identify joined views.

The fix was merged in [OmniFlow PR20](https://github.com/exploreomni/OmniFlow/pull/20). Merged main revision `fca86aed6d50df29e0f76ca058002699b3821cf7` contains the fix. A bounded replay reproduced two such false coverage gaps on 0.4.0 and eliminated them on the fixed code using valid relationship endpoints. This does not establish a pass for a customer's complete PR or malformed relationship YAML.

1. Find every `uses: exploreomni/OmniFlow@...` reference in the consumer's validation workflow. Follow any reusable workflow to the Action it invokes.
2. Review the changes from the currently installed revision, then update the Action to an approved full 40-character commit containing the fix. The verified baseline above is a concrete fixing revision, not a claim of a stable production release; its package version is `0.5.0a1`.
3. Preserve existing inputs, pinned dependency controls, trusted policy, credentials and branch protections. Do not enable unrelated features as part of the upgrade.
4. Start a fresh validation workflow against the same customer PR head and confirm the Action revision in the run's Action setup evidence. A merged change in OmniFlow does not automatically update a consumer's pinned workflow. A rerun that still resolves an older reusable-workflow/Action reference is not an upgrade.
5. Confirm that the two relationship-coverage gaps disappear. Review any remaining content, model or contract findings separately.

Example replacement for the Action reference only:

```yaml
uses: exploreomni/OmniFlow@fca86aed6d50df29e0f76ca058002699b3821cf7
```

The empty alias positions in a relationship identity such as `orders:->customers:` do not themselves mean a joined view is missing. Do not invent aliases or disable coverage enforcement to work around the old defect.

## Unreadable dashboard-filter evidence

The [published Content Validator API](https://docs.omni.co/api/content-validator/validate-content) describes dashboard-filter and query issue messages as strings. OmniFlow also retains compatibility with objects containing a nonempty string `message`. It does not guess how to interpret undocumented nested objects.

Null, blank, non-string or unsupported message shapes now produce `content_evidence_unavailable`, with an allowlisted issue category and document/query identity when available. The run remains failed with exit `4` (Omni API error). This differs from a readable validation defect, which follows the existing exit `1`/new-only policy. Validation stops for that model context, so later checks may not have run. Neither case authorizes ignoring the gate.

For an affected user, request:

- The failing run and customer PR head, exact OmniFlow Action reference, validation scope (branch or base), and redacted public report.
- The original shape of one failing issue, not the already-normalized `message: null` report. Obtain it through an approved private support channel; replace customer names, identifiers, URLs, field values and sensitive text with placeholders while retaining keys and value types. Never share credentials or a whole raw response.
- What Content Validator shows for the same model and branch: document/query identity, whether an actual dashboard filter is identified, and its readable error if present.

Do not assume a draft document is invalid merely because it is a draft: the [Content Validator guide](https://docs.omni.co/modeling/develop/content-validator) describes branch validation of relevant drafts as well as published content. Do not delete the draft, change an arbitrary filter, or disable validation based only on an unreadable message.

Once an original sanitized example is available, confirm the contract with the API owner. Only then add any necessary adapter, a fixture based on the actual response, and one compatibility regression. Until then, the precise underlying filter defect and whether a new adapter is needed remain unconfirmed.

## Evidence must be valid before it can pass

- Model validation requires a list of issue objects with string messages. A present `is_warning` must be a real boolean; values such as `"false"`, `0`, or `null` are rejected rather than interpreted as warnings. An omitted flag conservatively remains an error. Optional YAML paths and documented auto-fix descriptions must have the expected types.
- Content catalog pages require a `records` list containing objects. Malformed envelopes, rows, or continuation metadata produce an API failure rather than an empty catalog. An absent `pageInfo` remains compatible with single-page responses; a valid empty `records` list is still valid evidence. Label filtering additionally requires usable record identifiers.
- Unknown model auto-fix metadata is discarded. Strict public redaction removes the complete `auto_fix` value, including description text, from JSON, JUnit, and other public reports. Standard mode retains the supported descriptions under its existing redaction policy.

These checks intentionally expose previously hidden upstream response problems. Do not weaken the gate or convert malformed evidence to an empty list to obtain a pass.

## How content comparisons stay in scope

Content issues are matched by occurrence, not just by membership in a set. If one identical error becomes two, the additional occurrence is new. Query comparisons use stable document-scoped identity (`query_presentation_id`, then `query_id_map_key`, then supported `query_id`). Missing or duplicate query identity cannot establish that an issue is preexisting; such current findings remain new even when the message is unchanged. Query names alone are insufficient.

Persisted history now has a versioned schema and must match the current model, branch, explicit user ID, personal-folder setting, and labels. Label ordering does not matter. Legacy history or a different version/context is invalidated, not reused to suppress findings. Corrupt history fails with exit `4`. History reuse also requires an explicit `omni.user_id` (or `OMNI_USER_ID`): when it is absent, OmniFlow cannot prove that a later run uses the same authenticated principal and invalidates the baseline. It does not persist credentials or infer user identity from them.

This can cause a one-time increase in findings after an upgrade, or repeated new findings when historical comparison lacks an explicit user scope. Review the findings and confirm the intended validation user; do not substitute an arbitrary user ID merely to restore an old comparison. Branch validation with `fail_on_new_only` still compares against a freshly retrieved base-model result using the same client and does not depend on persisted history.

The content check's JSON includes `comparison_source`, `history_status`, and `comparison_ambiguous_issues` so a reviewer can distinguish an accepted comparison from an invalidated or ambiguous baseline. Historical/resolved evidence is reprocessed under the current privacy settings before appearing in a report.

## Reading the improved report

- Blockers are expanded with a check/category, affected object, and next investigation step. Advisory/historical details are collapsed; missing details never become a raw JSON dump.
- Content evidence gaps and dependency coverage gaps are explicit. Strict redaction removes names and error text but keeps fixed category-based guidance and allowed identifiers.
- **Input/PR Git SHA** identifies the code being checked. **OmniFlow Action revision** identifies a full-SHA Action reference supplied at runtime. They are separate.
- Local installs, floating Action tags/branches, and older runs may show Action revision as unavailable. The tool never substitutes the customer checkout SHA or claims independent attestation of an environment-supplied reference.
- Public JSON and evidence include the Action revision when available. The aggregate exit reason preserves operational API failures rather than mislabeling them ordinary validation failures.
- **Check Execution** records completed, failed operationally, not run, and disabled checks for each model/branch. “Completed” means execution finished, not that the check passed its configured policy. JSON records these as `completed`, `failed`, `not_run`, and `disabled`; `validation_complete` describes execution completeness separately from the pass/fail decision.
- When one check fails, completed findings remain visible. Unfinished analysis is explicitly incomplete; zero recorded impacts or gaps is not proof of no impact. Older failed reports without execution metadata remain unknown rather than being treated as complete.
- Identical findings in different model/branch contexts retain separate attribution and coverage counts. Long or object-like diagnostic messages use bounded formatting and safe category guidance.
- A warning can block under policy without becoming an error. GitHub annotations say **Blocks under configured policy**, and SARIF records `properties.policy_blocking` separately from severity.
- If unexpected execution or report generation fails, OmniFlow writes fresh minimal failed JSON, Markdown, SARIF, JUnit, and evidence outputs, without exposing raw exception details or reusing the failed renderer. This replaces stale standard reports on writable, approved paths; rejected or unwritable paths still fail closed and require operator attention. Do not upload old artifacts after a filesystem failure.

The PR20 baseline above fixes the relationship defect but does **not** include this later diagnostic/reporting enhancement. Consumers need a separately reviewed, published revision containing these improvements to receive the revised report. Local tests establish development confidence; a fresh consumer workflow and live branch check establish customer acceptance.
