# Validation Diagnostics and Upgrading

## First distinguish the failure

| Report finding | Meaning | Safe next step |
| --- | --- | --- |
| Content validation / Dashboard filter or Query | Omni returned a readable validation issue. | Inspect that document/query in the same model and branch before changing a filter or field. |
| Content validation / coverage unavailable | OmniFlow could not read the issue details. This is incomplete evidence, not proof of a particular content defect. | Keep the gate blocked; gather sanitized original evidence privately. |
| `empty_api_response` / `invalid_json_response` | The API returned an empty body or bytes that could not be decoded as JSON. This is not a dashboard-filter finding. | Check the API/service response through authorized support, then rerun; do not infer a clean baseline. |
| `document_identity_unavailable` | A branch document could not be established as a published document or an exact active draft of one. | Check Viewer-or-higher access to the published document and its draft metadata, then rerun. An absent lookup does not prove a new document. |
| `ambiguous_document_identity` | Duplicate or conflicting document/draft identity evidence prevents safe comparison. | Resolve conflicting evidence in the same model and branch; do not match using names or messages. |
| `stale_document_draft` | A mapped draft is out of date with its published document. | Update the draft in Omni and rerun. |
| `document_identity_changed` | Validator evidence, selected labels, or draft version changed between observations. | Rerun after edits/publication finish; the interrupted comparison is not a new-defect conclusion. |
| `document_identity_limit_exceeded` | Identity discovery hit its bounded lookup budget. | Investigate the scope privately. No unresolved documents are silently omitted or waived. |
| `query_identity_unavailable` | A mapped draft lacks unique query tile/map keys. | Inspect the draft in Omni; query names or presentation IDs cannot safely substitute for missing tile identity. |
| Downstream contracts / Coverage unavailable | Dependency analysis could not establish the affected consumers. | Verify the Action revision, relationship endpoints, model identity and reference-search access. |
| Validation execution failed / incomplete | A check could not finish because of an API, context, or internal failure. | Retain completed findings, resolve the failed check, then rerun. Later checks marked “not run” are not successful. |
| Advisory warning | The finding does not block under the recorded policy. | Review separately from blockers. Model warnings still block when `model_validation.fail_on_warnings` is enabled. |

No returned references is not proof of no consumers when coverage is unavailable. Content history counts (new/existing/resolved) describe content validation only; they are not the total blocking count across model, lint, contract and other checks. Missing optional results do not establish that those checks ran successfully.

### Reading an actionable report

Start with **Blocking findings**, not the total recorded errors or warnings. For example, a run with three errors
can have only one blocker when two errors are matched baseline findings under new-only policy. Nonblocking findings
are grouped separately by their safe category and occurrence count, rather than repeating one generic message.

Semantic lint reports retain built-in rule IDs such as `require_field_descriptions`, `require_primary_keys`, and
`require_owner_metadata`, each with a fixed corrective step. Lint findings describe the checked model and are not
automatically classified as new-versus-existing PR defects. A new dashboard-filter finding identifies that check,
its branch scope, and the next investigation step; it does not invent the underlying field or filter defect from
an anonymous report. Open Content Validator in the same authorized Omni model and branch for those exact details.

Public JSON, SARIF and JUnit retain individual findings. Markdown and GitHub annotations group only identical safe
projections, preserve policy and context distinctions, and report omitted categories when presentation limits apply.
No PII/PHI/PCI-bearing messages, field values, SQL, names, or resource IDs are enabled in public reports by these reporting improvements.

### Finding the affected code in a private repository

Eligible private same-repository GitHub.com validation runs now default to a separate **repository diagnostics** artifact. It can identify the exact authored field/view/topic, source file and source-key line, alongside the fixed explanation and corrective guidance. The JSON contains all findings; Markdown prioritizes blockers and explicitly counts any omitted findings. A location labeled `base` refers to the old snapshot (for example, a deleted field), not the current PR file. No line number is invented when only a file is known.

The ordinary PR comment stays minimal. Download `omniflow-repository-diagnostics` from the run to inspect its `report.md` and `report.json`. If absent, check the safe console fallback reason and local `repository/status.json`: unknown repository eligibility, unavailable trusted policy, mismatched authored snapshots, or unavailable report generation will withhold detailed output without changing validation policy. Existing workflows need the separately guarded upload step from the updated workflow example. Do not upload the whole `.omniflow` directory.

Content-only dashboard/filter identities that are not authored in the repository still require inspection in Omni. Unrecognized API messages are not copied into the report, and a source location alone does not establish the precise root cause.

To opt out, merge `reporting.diagnostics.mode: minimal` into the root `.omniflow.yml` on the trusted target branch. `repository` is the omitted default; both modes retain minimal public reports, logs, annotations and PR comments. See [Diagnostic Privacy](DIAGNOSTIC_PRIVACY.md).

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

A comment-only PR can trigger validation of existing dashboard content. The old parser rejected the grouped response before baseline comparison. With the adapter, the same issue can be compared normally: new findings block, while unchanged identity-matched findings are reported as existing and nonblocking under explicitly configured new-only policy. Their original error severity is preserved; `blocking: false` does not mean the issue is fixed or merely informational.

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

## Model and content regression gates versus health

The canonical required policy explicitly sets `fail_on_new_only: true` independently under `checks.model_validation` and `checks.content_validation`. For PR branch runs, each validator compares its current findings with a fresh response for the same model's live base, using the same credential and applicable visibility scope. Neither switch changes the other; omitted custom settings remain `false`, and `OMNI_FAIL_ON_NEW_ONLY` affects content only.

The orchestrator first requires immutable PR base/head Git revisions and verifies authored live YAML against each: candidate versus head, live base versus trusted base. It repeats the checks after validation. Missing, unverifiable, or out-of-sync snapshots cannot authorize regression gating, even if the current issue list is empty. Resolve synchronization before rerunning. Before/after matches are sampled consistency evidence, not an atomic live-content or draft lock; no missing-repository or identity override replaces them.

Model comparisons match private issue identity, severity, and occurrence counts. If one matching error becomes two, the additional occurrence is new; a warning that becomes an error cannot be hidden as an existing warning. Existing matched model findings stay visible but nonblocking in new-only mode. New model errors block, while new warnings block only when `fail_on_warnings: true` (default `false`). Missing or ambiguous identity and malformed/unavailable responses cannot establish a clean comparison.

For both validators, a pass means **no new blocking findings were detected in the compared scope**. It does not certify a healthy model/content estate, prove full instance coverage, or establish that the PR caused every finding. The baseline is a fresh live observation, not an immutable historic snapshot of every model and draft. Other enabled checks can still block independently.

Content requests retain the API's default validation depth on both sides. The same request scope does not guarantee exhaustive API content coverage, particularly for large or changing inventories. Do not present a passing comparison as proof that every dashboard, query, or draft was validated.

Orchestrated validation without a branch, post-sync validation, and repair verification use strict health enforcement for both validators even if their new-only switches are enabled. They never pass by comparing the base to itself or by suppressing current findings with historical output. Standalone `content validate` retains its explicit scoped history mode as described below; it is not the PR or post-sync baseline mechanism.

Inside a GitHub PR, standalone content/model commands cannot perform new-only validation: they return configuration exit `2` and require `omniflow run --auto` for trusted Git head/base verification. They do not silently fall back to an unverified regression gate.

The new model switch requires a reviewed supporting Action revision. Older versions reject the unknown key; follow the [compatible trusted-base upgrade sequence](INSTALLATION.md#step-5-merge-the-setup-into-the-base-branch). These changes remain local development work until reviewed publication and a fresh consumer run establish adoption.

## How content comparisons stay in scope

The required canonical [`.omniflow.yml`](../.omniflow.yml) explicitly selects `checks.content_validation.fail_on_new_only: true` for PR regression gating. Copy it into the customer repository root and bootstrap it on the trusted base; the workflow file alone is insufficient. Missing policy is configuration exit `2`. A custom policy that omits this setting retains the underlying `false` default (every current readable issue blocks), and explicit all-issues policies remain supported. The canonical setting is:

```yaml
checks:
  content_validation:
    enabled: true
    fail_on_new_only: true
```

This is a policy decision, not a parser fix. The workflow must resolve the intended Omni branch and trusted model context. For a branch run, new-only compares current branch findings against a fresh base-model result using the same client, user scope, labels, and personal-folder setting. No branch or an unverified/mismatched candidate is not proof of a valid PR regression baseline. Do not change credentials or user IDs merely to hide findings.

Readable, identity-matched issues already present on the baseline can remain nonblocking under this policy; genuinely new issues and additional occurrences block. A passing content regression gate therefore means **no new issues were detected in the compared scope**, not that existing content is healthy. Orchestrated no-branch health checks and post-sync/repair verification enforce all-issues policy rather than comparing the base to itself.

Unreadable evidence from either side remains exit `4`. If branch evidence cannot be parsed, processing stops before collecting the base response; the report must not infer that the failure is inherited. Identical unreadable error messages on both sides are not an acceptable baseline match.

Content issues are matched by occurrence, not just by membership in a set. If one identical error becomes two, the additional occurrence is new. Query comparisons use stable document-scoped identity (`query_presentation_id`, then `query_id_map_key`, then supported `query_id`). Missing or duplicate query identity cannot establish that an issue is preexisting; such current findings remain new even when the message is unchanged. Query names alone are insufficient.

Grouped dashboard-filter findings retain the exact filter name as private, document-scoped comparison identity; names and message text never enter the public schema. Identical messages on different filters remain different findings. Duplicate group names are ambiguous and cannot suppress current findings as preexisting. Filter renames conservatively count as new/resolved because this response provides no durable filter ID. Reordering groups or messages does not create a regression; additional occurrences do.

Without a branch, standalone `content validate` defaults to strict health even when the canonical policy sets new-only. Explicit `--history-in` or `--fail-on-new-only` requests history comparison. A missing usable baseline fails with exit `4`, even if the current issue list is empty.

For this explicitly selected history mode, persisted history has a versioned schema and must match the current model, branch, explicit user ID, personal-folder setting, and labels. Label ordering does not matter. Legacy history or a different version/context is invalidated, not reused to suppress findings. Corrupt history fails with exit `4`. History reuse also requires an explicit `omni.user_id` (or `OMNI_USER_ID`): when it is absent, OmniFlow cannot prove that a later run uses the same authenticated principal and invalidates the baseline. It does not persist credentials or infer user identity from them.

An upgrade or missing user scope can therefore invalidate a history comparison and leave it blocked with exit `4` until a usable baseline is established, even if the current issue list is empty. Do not substitute an arbitrary user ID merely to restore an old comparison. **With a PAT, leave `omni.user_id`, `OMNI_USER_ID`, and `--user-id` unset**: Omni's Content Validator rejects that parameter for user-scoped keys, so persisted-history reuse is unavailable through this path. Use strict health or the supported live PR/base comparison instead. An Organization API Key can use an explicitly chosen acting user; review [credential and user-scope rules](CONFIGURATION.md#credential-choice-and-user-scope).

Current history does not bind the baseline to a verified credential type or tenant identity. After changing credentials or tenants, establish a fresh reviewed baseline rather than assuming the old cache proves equivalent visibility. Branch validation with `fail_on_new_only` still compares against a freshly retrieved base-model result using the same client and does not depend on persisted history. For new-only checks inside a GitHub PR, use `omniflow run --auto` so the required Git base/head and live snapshot checks run; standalone validator commands are not a bypass.

Comparison history uses schema version 2 because document fingerprints now prefer verified logical document identity over draft-specific physical IDs. Older private history is reported as `invalidated_version`; explicitly requested history-only regression checks remain incomplete until a new, same-scope baseline is established. Fresh live PR/base comparisons do not depend on old history. Legacy string/message-object issue shapes remain supported. Schema-2 public diagnostics are not trusted history (`invalidated_public_diagnostic`); copying a shareable report into the history input cannot establish a clean or matching baseline.

### Branch-attached dashboard drafts

Omni can return a draft's identifiers instead of its published dashboard's identifiers. The [List document drafts API](https://docs.omni.co/api/documents/list-document-drafts) supplies the explicit relationship, branch, active status, out-of-date flag and update timestamp. OmniFlow verifies this relationship privately before label selection or issue matching. Metadata lookup uses the same credential and authorized content scope; it does not fetch workbook bodies or execute queries. Known healthy base documents are included in identity discovery so their first branch error cannot be missed.

Identity discovery is cached per published document, not per finding, and bounded to 200 lookups or 60 seconds before starting another lookup (an in-flight request still follows the API timeout/retry policy). Verified mappings are re-read around a second validator observation. These are consistency samples, not an atomic content lock. Unknown, stale, conflicting or changing evidence exits `4`; a verified new defect exits `1`; matched existing defects remain nonblocking only under new-only policy. Unresolved drafts are never discarded merely because selected labels do not contain their draft-specific IDs.

Only verified draft/parent pairs replace physical document identity during comparison. Ordinary documents retain their physical and logical identifiers, so a reused identifier cannot waive a different document's errors. The raw relationship, timestamps and resource identifiers stay private. Public reports expose fixed reason codes, states and counts only. No new policy setting, secret, or write permission is required; the credential must have Viewer-or-higher access to the published documents used for draft lookup.

Live testing also found that an unchanged or name-only edited draft can regenerate query presentation IDs while retaining `query_id_map_key`, the documented tile address. For verified draft/parent pairs, comparison uses unique tile keys scoped to the canonical document, plus exact messages and occurrence counts. Missing/duplicate tile keys are incomplete evidence. Different tiles or dashboards cannot match just because their error text is identical.

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
