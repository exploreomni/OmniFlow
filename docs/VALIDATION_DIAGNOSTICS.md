# Validation Diagnostics and Upgrading

## First distinguish the failure

| Report finding | Meaning | Safe next step |
| --- | --- | --- |
| Content validation / Dashboard filter or Query | Omni returned a readable validation issue. | Inspect that document/query in the same model and branch before changing a filter or field. |
| Content validation / coverage unavailable | OmniFlow could not read the issue details. This is incomplete evidence, not proof of a particular content defect. | Keep the gate blocked; gather sanitized original evidence privately. |
| `empty_api_response` / `invalid_json_response` | The API returned an empty body or bytes that could not be decoded as JSON. This is not a dashboard-filter finding. | Check the API/service response through authorized support, then rerun; do not infer a clean baseline. |
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

The [published Content Validator API](https://docs.omni.co/api/content-validator/validate-content) describes dashboard-filter and query issue messages as strings. OmniFlow also retains compatibility with objects containing a nonempty string `message`. An isolated live test additionally reproduced this dashboard-filter response (values below are synthetic):

```json
{
  "dashboard_filter_issues": [
    {"filter_name": "Synthetic filter", "issues": ["Synthetic missing-field error"]}
  ]
}
```

The adapter explicitly supports this grouped shape: a nonempty string `filter_name` and a list of nonempty string messages. A valid empty nested list contains no issues. It does not recursively search unknown objects for error text. The healthy fixture passed; a deliberately missing filter field returned this group and reproduced the old exit-4 failure; restoring the field passed again. That is evidence for this compatibility fix, not confirmation of the customer's exact defect or its age.

A comment-only PR can trigger validation of existing dashboard content. The old parser rejected the grouped response before baseline comparison. With the adapter, the same issue can be compared normally: new findings block, while unchanged identity-matched findings are informational only under explicitly configured new-only policy.

Null, blank, non-string or unsupported message shapes produce `content_evidence_unavailable`, with a fixed issue category, structural reason code, and branch/base scope. The shareable schema does not expose document/query identities or names; authorized investigators must obtain those separately. The run remains failed with exit `4` (Omni API error). This differs from a readable validation defect, which follows the existing exit `1`/new-only policy. Validation stops for that model context, so later checks may not have run. Neither case authorizes ignoring the gate.

| `reason_code` | Structural meaning |
| --- | --- |
| `invalid_issue_container` | The issue container was not a list. `null` is not treated as a valid empty list. |
| `invalid_issue_entry` | A list entry was not a supported string or message object. |
| `missing_issue_message` | A message object did not contain `message`. |
| `invalid_issue_message` | The message was blank or was not a string. |
| `invalid_filter_identity` | A grouped filter name was missing, blank, or not a string. |
| `unsupported_issue_shape` | An unsupported nested shape or unclassified structural error was encountered. |

These codes describe structure only. They do not expose the invalid value, assert a particular broken filter, or establish whether the API or the adapter needs to change. Empty issue lists remain valid. Unknown shapes remain blocking.

For an affected user, request:

- A schema-version-2 public report, initially limited to finding type, structural reason, validation scope (branch or base), and comparison status. Obtain the failing run/PR and exact Action reference through an authorized channel separately; those references are no longer copied into shareable reports.
- The original shape of one failing issue, not the already-normalized `message: null` report. Obtain it through an approved private support channel; replace customer names, identifiers, URLs, field values and sensitive text with placeholders while retaining keys and value types. Never share credentials or a whole raw response.
- What Content Validator shows for the same model and branch: document/query identity, whether an actual dashboard filter is identified, and its readable error if present.

Do not assume a draft document is invalid merely because it is a draft: the [Content Validator guide](https://docs.omni.co/modeling/develop/content-validator) describes branch validation of relevant drafts as well as published content. Do not delete the draft, change an arbitrary filter, or disable validation based only on an unreadable message.

The grouped adapter is based on observed live behavior, not a claim that every possible response shape is supported. Any further unsupported shape requires separately sanitized evidence and contract investigation. A fresh consumer run on the reviewed fixing revision remains required before claiming the customer's issue is resolved. See [Diagnostic Privacy](DIAGNOSTIC_PRIVACY.md) before collecting or sharing additional evidence.

## Evidence must be valid before it can pass

- Model validation requires a list of issue objects with string messages. A present `is_warning` must be a real boolean; values such as `"false"`, `0`, or `null` are rejected rather than interpreted as warnings. An omitted flag conservatively remains an error. Optional YAML paths and documented auto-fix descriptions must have the expected types.
- Content catalog pages require a `records` list containing objects. Malformed envelopes, rows, or continuation metadata produce an API failure rather than an empty catalog. An absent `pageInfo` remains compatible with single-page responses; a valid empty `records` list is still valid evidence. Label filtering additionally requires usable record identifiers.
- Unknown model auto-fix metadata is discarded. The minimal public schema excludes all auto-fix descriptions and other customer-derived free text under both standard and strict settings. Detailed local/restricted evidence remains subject to its separate handling policy.

These checks intentionally expose previously hidden upstream response problems. Do not weaken the gate or convert malformed evidence to an empty list to obtain a pass.

## How content comparisons stay in scope

The global default remains `checks.content_validation.fail_on_new_only: false`: every current readable issue blocks. A reviewed PR-validation policy may explicitly opt into regression gating in its trusted configuration:

```yaml
checks:
  content_validation:
    enabled: true
    fail_on_new_only: true
```

This is a policy decision, not a parser fix. The workflow must resolve the intended Omni branch and trusted model context. For a branch run, new-only compares current branch findings against a fresh base-model result using the same client, user scope, labels, and personal-folder setting. No branch or an unverified/mismatched candidate is not proof of a valid PR regression baseline. Do not change credentials or user IDs merely to hide findings.

Readable, identity-matched issues already present on the baseline can remain nonblocking under this policy; genuinely new issues and additional occurrences block. A passing regression gate therefore means **no new issues were detected in the compared scope**, not that existing content is healthy. Scheduled or whole-environment health checks can retain all-issues enforcement.

Unreadable evidence from either side remains exit `4`. If branch evidence cannot be parsed, processing stops before collecting the base response; the report must not infer that the failure is inherited. Identical unreadable error messages on both sides are not an acceptable baseline match.

Content issues are matched by occurrence, not just by membership in a set. If one identical error becomes two, the additional occurrence is new. Query comparisons use stable document-scoped identity (`query_presentation_id`, then `query_id_map_key`, then supported `query_id`). Missing or duplicate query identity cannot establish that an issue is preexisting; such current findings remain new even when the message is unchanged. Query names alone are insufficient.

Grouped dashboard-filter findings retain the exact filter name as private, document-scoped comparison identity; names and message text never enter the public schema. Identical messages on different filters remain different findings. Duplicate group names are ambiguous and cannot suppress current findings as preexisting. Filter renames conservatively count as new/resolved because this response provides no durable filter ID. Reordering groups or messages does not create a regression; additional occurrences do.

Persisted history now has a versioned schema and must match the current model, branch, explicit user ID, personal-folder setting, and labels. Label ordering does not matter. Legacy history or a different version/context is invalidated, not reused to suppress findings. Corrupt history fails with exit `4`. History reuse also requires an explicit `omni.user_id` (or `OMNI_USER_ID`): when it is absent, OmniFlow cannot prove that a later run uses the same authenticated principal and invalidates the baseline. It does not persist credentials or infer user identity from them.

This can cause a one-time increase in findings after an upgrade, or repeated new findings when historical comparison lacks an explicit user scope. Review the findings and confirm the intended validation user; do not substitute an arbitrary user ID merely to restore an old comparison. Branch validation with `fail_on_new_only` still compares against a freshly retrieved base-model result using the same client and does not depend on persisted history.

Legacy string/message-object fingerprints remain unchanged. Schema-2 public diagnostics are not trusted history (`invalidated_public_diagnostic`); copying a shareable report into the history input cannot establish a clean or matching baseline.

The content check's JSON includes:

- `comparison_mode`: `all_issues` or `new_only`.
- `comparison_source`, `history_status`, and `comparison_ambiguous_issues`.
- `comparison_status`: `complete`, `ambiguous`, `baseline_unavailable`, or `unavailable` when evidence cannot be parsed.
- `baseline_issues` and `current_issues`; a missing baseline is `null`, not an invented zero.
- `content_health`: `issues_in_scope`, `no_issues_in_scope`, or `unknown`.
- `regression_status`: `new_issues_detected`, `no_new_issues_detected`, or `unknown` when a complete comparison cannot be established.

Historical/resolved evidence is reprocessed under the current privacy boundary before appearing in a shareable report. Limited scope and absent evidence must not be presented as complete instance health.

## Why a comment-only PR may still fail

Model routing is based on changed paths and trusted metadata, not a semantic interpretation of the edited lines. Content Validator evaluates content associated with the selected model, and the content check runs before semantic-diff analysis. A comment in a model file can therefore trigger the same pre-existing defect or unsupported response as a substantive change.

The optional `checks.content_validation.classify_comment_only: true` setting enables explanatory classification only; its default is `false`. It inspects pinned Git snapshots conservatively without executing PR-head code. Unsupported YAML/dependencies, mixed changes, or missing evidence remain unverified. Even a `comment_only` classification records `content_validation_required: true`; it never skips Content Validator or changes the gate result. Git snapshots cannot establish the state of all live Omni content and attached drafts. A future bypass would require separately demonstrated candidate/content provenance and a reviewed policy, not merely an unchanged parsed YAML document.

## Reading the improved report

- Public reports use `diagnostic_schema_version: 2` with fixed categories, safe reason codes, bounded counts, boolean coverage information, and generated context references. Customer-authored text and direct resource IDs/paths are excluded under both standard and strict settings. Missing details never become a raw JSON dump.
- Content evidence gaps and dependency coverage gaps remain explicit. Detailed object attribution belongs in authorized private investigation, not a shareable artifact.
- **Input/PR Git SHA** and **OmniFlow Action revision** are separate provenance facts. Obtain them from authorized workflow/private evidence; public JSON and evidence deliberately omit both. A floating Action tag is not independently attested by an environment-supplied reference.
- The aggregate exit reason preserves operational API failures rather than mislabeling them ordinary validation failures.
- **Check Execution** records completed, failed operationally, not run, and disabled checks for each model/branch. “Completed” means execution finished, not that the check passed its configured policy. JSON records these as `completed`, `failed`, `not_run`, and `disabled`; `validation_complete` describes execution completeness separately from the pass/fail decision.
- When one check fails, completed findings remain visible. Unfinished analysis is explicitly incomplete; zero recorded impacts or gaps is not proof of no impact. Older failed reports without execution metadata remain unknown rather than being treated as complete.
- Identical findings in different model/branch contexts retain separate generated context references and coverage counts within a report, without publishing direct identity. Unknown or object-like diagnostic text is omitted; guidance uses fixed vocabulary.
- A warning can block under policy without becoming an error. GitHub annotations say **Blocks under configured policy**, and SARIF records `properties.policy_blocking` separately from severity.
- If unexpected execution or report generation fails, OmniFlow writes fresh minimal failed JSON, Markdown, SARIF, JUnit, and evidence outputs, without exposing raw exception details or reusing the failed renderer. This replaces stale standard reports on writable, approved paths; rejected or unwritable paths still fail closed and require operator attention. Do not upload old artifacts after a filesystem failure.

The PR20 baseline above fixes the relationship defect but does **not** include this later diagnostic/reporting enhancement. Consumers need a separately reviewed, published revision containing these improvements to receive the revised report. Local tests establish development confidence; a fresh consumer workflow and live branch check establish customer acceptance.
