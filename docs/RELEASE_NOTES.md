# Release notes

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
