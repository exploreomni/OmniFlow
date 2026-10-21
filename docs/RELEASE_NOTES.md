# Release notes

## Unreleased — verified relocation, source locations and result-quality probes

- Legitimate numeric/date repository paths no longer lose all private source
  locations. Unsafe or sensitive-looking path values remain withheld; original
  mapping failure categories are preserved.
- An unchanged topic/global relationship relocation is informational only when
  every explicit topic join can be proven equivalent. Changed or unresolved
  definitions remain findings; automatic/inherited/ambiguous join semantics are
  not guessed.
- Short current-head/run summaries prioritize blockers and next actions, collapse
  lint detail, and distinguish query-result checks from model/content validity.
  Downloaded reports identify themselves as pre-upload snapshots. Current-run
  upload receipts remain the delivery authority.
- The updated workflow maintains a stable PR summary and deduplicated failure/
  recovery comments, with freshness and bounded-pagination checks. Update the
  workflow as well as the Action pin. GitHub controls email preferences/delivery;
  a successful comment write is not evidence that an email arrived.
- Experimental [query-result contracts](QUERY_VALIDATION.md) are disabled by
  default. Reviewed scalar aggregate probes can compare base/head results for
  grouping, uniqueness, NULLs and aggregate changes. AI proposals are private,
  local and nonexecuting; enforcement requires separate native qualification and
  stable-data evidence. Incomplete results never establish a clean comparison.

## Unreleased — diagnostic clarity and confirmed artifact publication

- All 11 built-in lint rules now retain rule-specific next actions. Only unknown rules receive unknown-rule guidance.
- Content transport and malformed-response errors retain base/branch scope and comparison mode without changing
  calls, retries, exit codes or validation policy. Invalid view-identity metadata has a response-contract diagnosis,
  not instructions to rearrange model folders.
- Safe configuration coordinates, API status/retry evidence and metadata reasons survive Markdown, SARIF, JUnit
  and annotations. JUnit includes nonblocking findings in `system-out` without changing failure counts.
- Repository report status is now `generated`, not `available`, with `publication_status: unconfirmed`.
  The manifest explicitly inventories runner-local files. The caller workflow owns upload confirmation.
- Updated validation examples clear only known public outputs after safe path checks, guard against stale reports,
  and publish post-upload receipts with current-run links. Private eligibility, explicit filenames, seven-day
  retention, secret handling and public-only comments remain enforced. Upload failures remain visible as delivery
  failures and do not rewrite validation decisions.

Existing consumers must update the workflow steps as well as the reviewed Action SHA; see the
[upgrade checklist](VALIDATION_DIAGNOSTICS.md#upgrade-report-delivery-in-an-existing-workflow).
These are reporting changes, not a relaxation of new-only, synchronization or security policy.

## Unreleased — topic organization diagnostics

- Adding, renaming, or removing a topic's valid `group_label`, with no other
  definition changes, is reported as `topic_organization_changed` at informational
  severity, including when the topic moves between folders or has downstream
  references. Public reports explain the change without exposing group names.
- Folder-only moves remain informational file renames. Mixed edits to grouping
  and query/access logic keep their existing risk classification. Invalid grouping
  values are not downgraded; model/content validation, candidate synchronization,
  and independent policy blockers remain unchanged.
- Existing topic owner/label and unchanged topic-contained relationship lint debt
  remains existing after a verified standalone-topic relocation. Original source
  locations are retained; new/worsened findings and ambiguous moves are not waived.

## Unreleased — certificate trust-store security refresh

- Requires Certifi 2026.10.19 or newer and updates the hash-locked Action/release
  runtime to that patch, addressing GHSA-6q48-6c7w-9gw2. TLS certificate validation
  stays enabled; no custom trust exceptions or audit suppressions are added.

## Unreleased — type-specific artifact matching

- Replaces the unpublished blanket filename matcher that rejected valid ATX views
  with repeated table names across schemas. Exact authored paths remain valid;
  folder-independent matching uses typed unique names, canonical view filenames,
  or unambiguous authored catalog/schema/table qualification. Supported single
  `.yaml`/`.yml` wrappers and original Git paths are retained.
- Topic names and model/global relationships singletons remain unambiguous within
  the registered model. SQL/query views retain semantic identity without requiring
  a physical table; multiple semantic views are never collapsed by table or bytes.
- Candidate verification and private source reporting share the same correspondence.
  Missing/extra artifacts, differing contents, genuine identity ambiguity and unsafe
  evidence still block. Unrelated Git-only YAML remains ignored; exclusions cannot
  hide an Omni source and one model cannot borrow another model's file.
- This changes artifact identity, not validator policy or content equality. Existing
  debt remains governed by new-only/all-issues policy; comments and whitespace still
  count when proving the Git and Omni snapshots contain identical authored text.

Local tests are not customer or hosted ATX acceptance. Publish a reviewed Action pin
and run fresh validation before describing a customer run as resolved.

## Unreleased — actionable, privacy-bounded diagnostics

- Added a versioned catalog of fixed diagnostic codes and next actions. API/Git authentication, authorization, transport and response-contract failures retain their actual category through candidate checks instead of becoming generic configuration errors.
- Public reports include safe operation/status/retry/scope/configuration facts, tool version and a validated Action pin, with the primary blocker first. Customer Git revisions, paths, raw messages, SQL and data values remain excluded. Markdown, annotations, SARIF and JUnit preserve the same diagnostic code and do not invent a source line.
- Authorized private runs can explain candidate mismatches with at most five scoped path observations without requiring the failed correspondence check to pass. These observations are not correspondence proof. Validation gates and new-versus-existing policy are unchanged.
- Source indexing is lazy, supports ordinary spaces/Unicode and larger inventories, and states why a location is unavailable. Valid relationship lists do not hide unrelated view locations. Unknown/ambiguous identities remain unmapped.
- Config/metadata/parser failures identify built-in keys, expected types and source coordinates without exposing values. Failed Git discovery is no longer a successful empty change set. Doctor reports partial access truthfully and checks path/branch case.
- Malformed exposure rows no longer establish available coverage. A refresh whose polling fails remains `status_unknown` with its started-job evidence retained internally; it is not automatically restarted. Repair refuses an explicitly incomplete post-repair validation even if optional policy returned zero.
- Added stage progress/timing, blocked-stage explanations, truthful skipped/partial/unavailable coverage, repository-artifact availability, bootstrap guidance and a public job-summary example.

Local implementation and regression evidence are not a published release, fresh hosted ATX acceptance or customer acceptance. Review and publish a new Action pin, update the consumer workflow, then run a fresh branch/base validation before claiming the customer is fixed.

## Unreleased — regression-first validation and explicit incomplete evidence

- Model/content regression checks collect the live base before the candidate.
  Request-scoped evidence is reusable only within one run; required consistency
  rereads stay fresh. Matched existing debt remains visible and nonblocking;
  new/additional or worsened issues follow the configured policy.
- Verified regression PRs also compare ordinary semantic-lint debt with the base.
  Security-scope restrictions are never grandfathered; independent contracts and
  deployment holds are not waived.
- Added reviewed `files.non_omni` ancillary classification using exact
  `tag`/`pattern`/`reason` rules from trusted policy. Patterns are repository-relative
  and bounded; overlaps, control files, known Omni sources and API-authored sources
  fail closed. Both base and candidate inventories are checked.
- Strict enforcement remains the default. Opt-in onboarding advisory mode can make
  eligible synchronization gaps nonblocking with exit `0`, but reports explicitly
  remain incomplete/health-only with unknown PR impact. API/security/malformed
  evidence and independent blockers remain failures; enabled breaking-change
  holds prevent advisory downgrade. Unexecuted checks are not passes.
- Initial ordinary synchronization gaps receive one bounded retry; final drift is
  never retried away. Public reports add safe mismatch category counters and
  actual content scope/fallback status without source paths or customer text.
- Experimental `content_validation.targeted` remains off by default. The current
  CLI still uses broad validation even when enabled because independent dependency
  and draft coverage is not established. Staged targeted support preserves
  before/after selectors, bounded requests and broad fallback, but is not a
  completed production optimization or a comment-only/no-impact skip.

Upgrade the root `.omniflow.yml` and both workflow Action references together
through the approved trusted-base process using a reviewed supporting full SHA.
Keep `.omni/flow.json` at the root metadata location and the automation at
`.github/workflows/omniflow.yml`. No new published pin is declared here.
Fresh non-production consumer validation and adopter acceptance remain required;
synthetic tests or partial probes do not establish production coverage.

## Unreleased — native authored filename correspondence

- Candidate and base verification now recognize one `.yaml`/`.yml` serialization wrapper on known Omni authored filenames. Native Git exports can match extensionless API representations without renaming files or changing policy.
- Complete inventory comparison, exact byte comparison, immutable Git blob verification, original-path proof digests, pre/post rechecks, path bounds and duplicate detection remain enforced. Ambiguous wrapped/unwrapped aliases fail closed.
- Repository diagnostics use the same correspondence and retain real Git source paths and key-line numbers. Existing detail eligibility and path privacy restrictions are unchanged. Root-scoped routing recognizes nested typed native exports.
- Public failure diagnostics distinguish inventory, duplicate identity, content and availability problems, with bounded counts and scope. They do not expose mismatch paths, raw payloads or model contents, and do not call an unexecuted validator a model/content failure.
- Optional comment-only classification remains conservative and never skips live validation. No new configuration is required. Update all relevant Action pins to the same reviewed full SHA on the trusted default branch and perform a fresh non-production validation; source/CI evidence does not establish customer acceptance. See [troubleshooting](TROUBLESHOOTING.md#candidate-verification-git-and-omni-snapshots-differ).

## Unreleased — order-independent content evidence rechecks

- Dashboard-draft evidence rechecks now ignore ordering-only differences in dashboard-filter groups, filter messages, and query issue lists. Identical existing findings no longer produce a false `document_identity_changed` failure solely because the API returns them in a different order.
- Document/query identities, exact messages, duplicate occurrence counts, draft versions, and malformed-evidence checks remain enforced. API responses, history fingerprints, policy defaults, and diagnostic privacy are unchanged.
- Existing installations need the supporting Action commit pin; no policy change is required. Synthetic regression coverage does not establish how often a live tenant reorders API results or replace customer acceptance testing.

## Unreleased — repository-aware diagnostic default

**Upgrade notice:** `reporting.diagnostics.mode` now defaults to `repository`. Eligible private same-repository GitHub.com `omniflow run` executions generate separate source-location diagnostics. Set `mode: minimal` in the trusted target-branch policy to opt out. This nested key requires the supporting revision; coordinate the Action pin, policy and workflow upload changes through review.

- Detail requires live repository/PR identity and revision verification, literal trusted policy and model registration, and matching authored Git/Omni snapshots before and after checks. Public/fork/unknown contexts, standalone commands, stale revisions and unsupported GitHub origins stay minimal. Unknown or ambiguous source mappings never receive invented locations or causes.
- Repository reports add verified paths, source-key lines, supported authored identifiers and fixed corrective guidance. Raw responses, SQL excerpts, data values, credentials and Omni-only content identities remain excluded. Source metadata can itself be confidential; this is not a DLP guarantee.
- Public schema 2, root compatibility reports, logs, annotations, PR comments/email-facing summaries and Code Security uploads remain minimal. The updated validation examples upload separately named repository-only JSON/Markdown/SARIF/JUnit artifacts with seven-day retention, only after a fresh-run readiness receipt. A file left by an old run is not upload authorization.
- Known generated detailed files are removed on rerun, opt-out, eligibility loss and failure. Saved detailed reports cannot authorize new disclosure. Validation decisions, counts, severity and new-versus-existing comparisons are unchanged.
- Supported source locations include semantic lint, exact model YAML paths, semantic change references and narrowly recognized repository-backed content references. Unmapped content still needs investigation in the authorized Omni model/branch.

These are local development changes, not a published release or fresh live-tenant acceptance. Update the reviewed pin and workflow example before expecting customers to receive the artifact.

## Unreleased — independent model and content regression gates

- Added `checks.model_validation.fail_on_new_only`, independent from content policy and defaulting to `false` when omitted. The canonical required `.omniflow.yml` explicitly enables both new-only switches; model `fail_on_warnings` remains `false` by default. `OMNI_FAIL_ON_NEW_ONLY` continues to affect content only.
- PR branch comparisons use fresh live-base evidence for the same model and credential scope. Model findings retain identity, severity, and occurrence counts; existing findings remain visible, additional occurrences and severity changes are new, and warning blocking follows policy. Missing or ambiguous evidence cannot establish a passing comparison.
- Before and after regression-gated PR checks, live authored candidate/base YAML must match immutable Git head/base revisions. Missing or out-of-sync snapshots block even when current findings are empty. These are sampled checks, not an atomic content/draft lock; unchanged default API validation depth does not guarantee exhaustive coverage.
- Orchestrated no-branch runs, post-sync validation, and repair verification enforce strict health rather than self-comparison or historical suppression. Standalone content's explicitly selected, scoped history behavior remains separate.
- Standalone no-branch content validation defaults to strict health; explicit `--history-in` or `--fail-on-new-only` requests comparison and missing usable history exits `4`. Standalone content/model new-only validation inside GitHub PRs is rejected in favor of `omniflow run --auto` and its trusted Git verification. Existing content findings preserve error severity with nonblocking policy state.
- Older Action revisions reject the new model key. Bootstrap a compatible required file on the trusted base, then coordinate the supporting pin and new setting through review. A pass means no new blocking findings in the compared scope, not clean model/content health or proof of causality.

This is local development work, not a published release or customer acceptance. A reviewed compatible revision and fresh consumer validation are still required.

## Unreleased — required trusted policy

- **Breaking setup requirement:** the selected policy file must exist. Copy the canonical root [`.omniflow.yml`](../.omniflow.yml) into the customer repository root; it replaces the former `.omniflow.example.yml` template name and is distinct from `.github/workflows/omniflow.yml`.
- Bootstrap policy on the protected base through the approved setup process before upgrading the Action pin or validating Omni PRs. A PR-head-only file is not trusted. Missing policy exits with configuration code `2` and safe setup guidance, not silent defaults. Environment variables and identity flags cannot replace a file; explicit custom `--config` paths remain supported and required.
- Blank, comment-only, and empty top-level policies are rejected; start with the active settings in the canonical file.
- The shipped policy keeps the main safety checks enabled and explicitly sets both model and content `fail_on_new_only: true`. Label filters and optional integrations stay commented. Omitted settings in a custom policy keep their existing defaults; explicit all-issues policies are not weakened, and unreadable comparison evidence remains blocking.

This is a development candidate. Existing consumers must review the policy and updated Action pin; a source change does not update their installation automatically.

## Unreleased — minimal diagnostics and explicit content-regression evidence

This is a development candidate, not a published release or confirmation of customer acceptance.

- **Breaking public report schema 2:** public outputs now construct a minimal allowlist of fixed categories/statuses, bounded counts, boolean flags, and generated run/context references. Both standard and strict settings omit customer text, opaque prompt IDs, direct resource identities/paths, and Git/Action references. Migrate consumers that read those fields to authorized trusted/private evidence; do not use public JSON for identity-sensitive readiness decisions.
- Supported workflow uploads use explicit diagnostic filenames, not broad output-directory uploads. Existing artifacts and logs are not retroactively sanitized. This is data minimization, not a PII/PHI/PCI detection or compliance guarantee.
- Dashboard-filter validation supports the grouped `filter_name` / `issues` response reproduced in isolated live testing, while retaining legacy string/message-object compatibility. Exact filter identity and occurrence counts prevent different or increased failures from being hidden as existing issues. Duplicate group names remain ambiguous; unknown nesting and `null` are not treated as healthy evidence.
- Unreadable content issues retain blocking behavior and include fixed structural reason codes and branch/base scope, without copying unsupported values. Empty API bodies and invalid JSON have separate safe reason codes; neither is presented as a confirmed content defect.
- Content reports distinguish current versus baseline counts, comparison completeness, all-issues versus new-only policy, and content issue presence versus regression status. The underlying all-issues default for an omitted setting is unchanged; the canonical required policy now explicitly selects new-only gating as described above.
- Optional comment-only classification is conservative and explanatory only. It never skips content validation because Git snapshots do not prove live content/draft provenance.

The old parser failed with exit `4` on the live grouped response. The intended fixed outcomes are exit `1` for a new readable defect, exit `0` for an unchanged matched baseline issue under new-only policy, and exit `4` for incomplete evidence. Historical string/message fingerprints remain compatible; public reports cannot be reused as trusted history. Standalone content runs refresh standard outputs so stale reports/evidence are not uploaded as current results.

Before customer acceptance, publish a reviewed revision, update the consumer's pinned Action SHA, and rerun their original PR against the intended live branch/base scope. Do not assume a comment-only PR is healthy or that the consumer receives new code automatically. See [Diagnostic Privacy](DIAGNOSTIC_PRIVACY.md) and [Validation Diagnostics](VALIDATION_DIAGNOSTICS.md).

## Unreleased — validation fixes and environment targets (#23–#25)

This change is a development candidate, not a published release or a claim of customer acceptance.

### Breaking-change hold documentation (#23)

- Corrected the configuration and troubleshooting guides: unavailable deployment
  evidence blocks under `action: fail` and is advisory under `action: warn`.
- Clarified safe bootstrap/recovery and explicit current-head revalidation. No hold
  runtime behavior or branch protections changed.

### YAML view identity compatibility (#24)

- YAML pulls normalize both supported `viewNames` orientations: file path → canonical name and canonical name → file path. Internal snapshot manifests remain name → path, so existing normalized snapshots need no migration.
- Real-shaped responses may include empty names for model, relationship, and topic files. These entries are ignored only after their paths and non-view classification are verified. View files still require complete, unique canonical identities.
- Mixed/ambiguous maps, duplicate identities, unsafe or unknown paths, invalid value types, and incomplete view coverage remain blocking. File inventories, checksums, scoped names, query views, and authored/resolved snapshots retain their existing protections.
- Malformed file-content entries now fail explicitly instead of being silently removed from the inventory before identity checks. Raw strings and the existing `content`/`contents` string wrappers remain supported.
- Metadata failures identify the processing stage and a safe reason category in JSON. Reviewer reports distinguish this input failure from semantic findings and leave dependent checks incomplete, including under strict redaction.

After a reviewed release or commit containing this fix is published, update the consumer's pinned Action reference through its normal review process. Run fresh validation for the affected PR; do not assume existing pins receive the fix automatically.

Development regression coverage uses synthetic payloads shaped like the report in [issue #24](https://github.com/exploreomni/OmniFlow/issues/24), plus legacy-format controls. A fresh sanitized tenant payload and a successful consumer branch run remain the customer acceptance gate. See [troubleshooting](TROUBLESHOOTING.md#yaml-viewnames-metadata-processing-failed).

### Environment-scoped validation targets (#25)

- Added opt-in version 2 trusted metadata for exact PR-base routing to development
  leader and production follower targets sharing a Git model folder.
- Bound validation credentials to the selected environment and verified live Git
  settings, unique candidate branch identity, and authored YAML against the immutable
  PR head before and after checks. Missing or stale candidates never fall back to the
  base model; reports explicitly identify incomplete candidate coverage.
- Kept version 1 supported. Version 2 rejects deployment sync, AI repair and
  deployment-readiness/hold release rather than sharing repository-wide sync state
  between environments. No automatic model writes or merges were introduced.
- Added environment-scoped workflow and [setup/rollback guidance](ENVIRONMENT_TARGETS.md).
  Controlled leader/follower acceptance remains required; sampled authored-content
  equality is not an atomic snapshot lock or proof of warehouse readiness.
