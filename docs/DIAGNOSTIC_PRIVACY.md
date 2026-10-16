# Diagnostic Privacy and Support Evidence

## The boundary

OmniFlow's shareable diagnostic schema is designed to avoid copying customer-authored text into standard reports. It is not a PII detector, PHI de-identification service, PCI assessment, or guarantee that every file produced by a workflow is safe to distribute.

Names, filter values, model definitions, SQL, branch names, and error messages can contain personal information, health information, payment data, or credentials. Masking familiar email and token patterns is insufficient. The public reporting path therefore constructs a new, minimal schema from explicitly permitted values instead of publishing a detailed report with selected fields removed.

This boundary does not change which Omni content the API credential can access. Requests and detailed decision evidence still exist within the runner. Use least-privilege credentials, approved runners, private repositories where appropriate, and controlled artifact retention.

## Shareable schema, version 2

Normal public JSON reports identify `diagnostic_schema_version: 2` and include a random `run_reference`. This is a diagnostic correlation reference, not an Omni resource identifier or credential. Local `context_reference` values distinguish model contexts within a report without publishing their direct identities. These references are not independent attestations of a Git commit or live Omni state.

The schema permits:

- Fixed check names, finding categories, structural reason codes, statuses, and policy decisions.
- Bounded integer counts and boolean coverage/blocking flags.
- Fixed comparison metadata distinguishing current issues, baseline issues, regression findings, and incomplete evidence.
- Generated diagnostic references and fixed explanatory text.

Safe detail is not the same as a generic error. Semantic lint retains only its recognized, built-in `rule_id`
values, with fixed explanations and corrective steps (for example, a missing description or primary key).
Recognized semantic change categories also receive fixed explanations. Unknown rule IDs and arbitrary API
messages are never copied into public text; an unclassified finding names the check and its safe investigation path.

Markdown and GitHub annotations group identical safe findings and display occurrence counts, with blockers first
and nonblocking findings separately identified. Grouping never changes the underlying decision, individual JSON/SARIF/JUnit
findings, or counts. Different contexts, severities, states, scopes, evidence reasons, and blocking flags stay separate.
Counts represent findings, not unique dashboards, fields, or people. A lint warning is not automatically a new PR regression.
Re-rendering a saved public report re-anonymizes its context labels while preserving their associations.

The public projection does not copy customer document/query names, direct model/document/query/user identities, tenant URLs, repository/model/YAML paths, branch or environment names, raw API responses, authored SQL, query result rows, customer error text, or Git/Action revision references. Unknown fields and unknown enum values are omitted, not stringified.

The public writer applies this same minimum-data schema for both `security.redaction_level: standard` and `strict`. Changing that setting does not restore detailed public reports. The legacy `public_safe`/redaction utilities remain useful on some local or restricted paths, but are not the public schema boundary and must not be treated as comprehensive sensitive-data detection.

Consumers of older JSON reports must migrate: do not use public resource IDs or Git hashes to drive readiness, authorization, or synchronization decisions. Internal decisions must use verified detailed evidence, not reconstruct identity from the shareable report. Emergency failure output may contain only the fixed failure envelope; missing optional metadata is not evidence of success.

## What remains confidential

### Repository-only diagnostics (default for eligible validation runs)

`reporting.diagnostics.mode` defaults to `repository`. This is a separate output class, **not** a relaxation of the public schema. `omniflow run` requires a live GitHub.com private-repository identity check, a same-repository PR at the verified head/base revisions (or a current default-branch push/manual run), literal policy from the immutable trusted revision, and a matching registered Omni model scope. Public repositories, forks, standalone commands, unsupported GitHub Enterprise origins, unavailable credentials/metadata, or unverifiable revisions remain minimal. A PR-head policy edit, environment expansion, local absolute-path file, or saved report cannot authorize detail.

The implementation reads Git objects without executing proposed code. Authored Git and Omni snapshots must match before and after validation; existing candidate proofs are reused when available. Source mapping does not establish an atomic lock on the live model. Ambiguous/unsupported YAML or unmatched API paths remain unmapped, with safe guidance. List-form field definitions and API-scoped names that do not match an explicit authored name/path are not inferred; safe file-level locations may still be available. Indexing is bounded to 1,000 files, 512 KiB per file, 5 MiB total and depth 32; unavailable detail never changes validation. Conventional schema names such as `customers.email` are metadata, not customer email values; metadata can still be sensitive, and this feature is not a DLP or compliance guarantee.

Allowed additions are verified repository-relative file paths, accurate source-key lines when available, supported authored field/view/topic identifiers, head/base side labels, and fixed diagnostic explanations. Raw API prose, SQL excerpts, authored descriptions, filter values, query rows, credentials, tenant URLs, dashboard identities and unrelated Omni content are excluded. Unknown causes are not inferred from arbitrary messages.

The separate `.omniflow/repository/` JSON, Markdown, SARIF and JUnit files retain the same findings and policy decisions. `.omniflow/public/`, root compatibility reports, logs, annotations, PR comments, email-facing summaries and the example Code Security upload remain minimal. Publishing repository detail to those destinations requires a separate reviewed workflow decision; there is no automatic broad-publication switch. The validation workflow examples upload only explicitly listed repository artifacts after a fresh in-memory readiness receipt, with seven-day retention. Ordinary repository read access permits artifact access; this is not an administrator-only attachment.

Opt out in the required policy on the trusted target branch:

```yaml
reporting:
  diagnostics:
    mode: minimal
```

Known generated repository reports are cleared on reuse, downgrade and failure without recursively deleting other files or following symlinks. Rejected filesystem paths fail closed; operators must resolve them before uploading artifacts. This cannot recall previously downloaded artifacts, notifications or copies. Keep the existing minimal report for external support handoffs.

Even minimal results can reveal operational facts such as validation failures, issue counts, or check coverage. Share them only with an appropriate audience. The word `public` describes the low-detail output class; it does not authorize posting customer diagnostics on the internet.

Detailed/restricted output is different. It may include model YAML, identifiers, paths, content text, history, or API-derived diagnostics. Keep `security.retain_restricted_artifacts: false` unless separately authorized for a specific investigation. Owner-only filesystem permissions reduce local access; they do not make an uploaded archive safe or override repository/artifact permissions. Never upload the complete `.omniflow` directory or a restricted directory as a routine support artifact.

This change does not retroactively sanitize historical reports, workflow logs, cached files, or previously uploaded artifacts. Review prior exposure separately. An actual credential disclosure requires the organization's credential-rotation and incident-response process; an opaque resource ID alone is not a credential, but can still be sensitive reconnaissance metadata.

## Safe support handoff

1. Share the version-2 report through the approved support channel, after checking that it is the current run's output. Start with `type`, `reason_code`, `validation_scope`, comparison status, and relevant counts.
2. Obtain the Action revision and failing PR/run identity from the authorized workflow context separately when Support needs them. The shareable report deliberately excludes those references.
3. If Support needs the affected Omni object, provide its identity privately and only to authorized investigators. Do not expect the minimal report to identify the dashboard directly.
4. For an unreadable API response, first provide only its structural classification. If a sample is required, an authorized investigator must remove names, values, URLs, identifiers, and secrets while preserving only the necessary structure/types. Unknown response keys may themselves contain sensitive text: review them too. Do not attach an unreviewed raw response or enable broad raw-output retention merely to obtain a sample.

For the dashboard-filter incident, isolated live testing reproduced a grouped `filter_name` / `issues` response. The compatibility adapter retains filter identity privately for correct baseline comparison; neither filter names nor nested messages are public fields. This live reproduction establishes the observed format, not the customer's exact defect or whether it predated the PR. A reviewed published revision and a fresh consumer branch/base run remain necessary before claiming the customer issue is resolved. Empty/non-JSON responses remain a separate incomplete-evidence failure.

See [Validation Diagnostics](VALIDATION_DIAGNOSTICS.md) for failure categories, regression policy, and comment-only PR behavior.
