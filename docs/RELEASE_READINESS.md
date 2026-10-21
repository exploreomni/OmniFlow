# Release readiness and adopter acceptance

## Supported scope

OmniFlow remains controlled alpha. A merge, successful CI run, signed preflight, release publication, and adopter acceptance are different states. No checklist entry is satisfied merely by this document existing.

No release tag, publication, or production acceptance is implied by this checklist. Safer failure behavior, artifact manifest v2, and breaking public diagnostic schema 2 need adopter review before upgrading from `0.4.0` or another earlier reporting revision. Schema 2 excludes customer resource identities, paths, raw messages and prompt IDs under both standard and strict settings. Fixed tool-version/Action-pin provenance and validated GitHub run/head links on notification surfaces are distinct from customer source detail.

| Capability | Candidate policy | Acceptance needed before required production gating |
| --- | --- | --- |
| Model/content validation, trusted discovery, semantic diff/lint, downstream contracts, public reports | Core candidate scope | Exact-SHA CI and release evidence plus adopter-specific valid/invalid checks, permissions, privacy, and recovery |
| dbt impact | Optional, separately qualified | Real Action base/head acquisition, manifest relation changes, credential-free routing, explicit incomplete coverage |
| dbt sync and holds | Optional, separately qualified | Approved non-production deployment/refresh, durable state, fresh current-head readiness check, failure recovery |
| AI eval | Optional, separately qualified | Approved scoring policy, prompt-set/model identity, privacy, quota/run lifecycle, controlled live regression |
| Query-result contracts | Optional, separately qualified | Approved query permissions and budgets, representative assertions, fresh baseline/candidate result regression and recovery; structural validators are not result correctness proof |
| Local AI proposals | Optional local reviewed workflow | Explicit source and human approval, verified validation result and rollback; no autonomous promotion |
| Failure/recovery notifications and artifact delivery | Required for adopted workflow examples | Same-head/run freshness, actual failure/recovery comments, current-run upload receipts, custom output path and non-default formats; email delivery separately verified if required |
| AI repair | Unsupported development scaffold | Excluded from customer release scope |

The official Action and release locks target Linux x86_64 / CPython 3.11. Unit CI covers Python 3.11–3.13; it does not establish that the Linux wheel lock works on other architectures. Pin a reviewed full 40-character Action SHA, never a floating branch.

## Maintainer release gate

1. Independently review changes and resolve all blocking findings. Require current Python tests, package, Action integration, dependency audit/review, workflow analysis, and SAST. Keep administrator enforcement enabled; do not bypass failed checks.
2. Configure independent CODEOWNERS for workflows, locks, discovery/trust, credentials, client, and reporting paths before requiring owner review. The PR author cannot be the only eligible owner. Assign an independent protected-release reviewer and prevent self-review once that roster is agreed.
3. Merge only fresh, green reviewed PRs. The signed preflight must run from the current protected `main` SHA. `scripts/verify_release_candidate.py` checks the latest main-push executions of Test, Dependency Scan, Actions Security, SAST, and Secret Scan, including required successful jobs. Missing, skipped, failed, stale, or still-running evidence blocks release. Publication rechecks that the version tag still resolves to the candidate commit after protected-environment approval; maintainers must also protect release tags from unauthorized movement.
4. Audit both exact dependency locks. Runner-provided pip is the initial trust boundary: it installs only the hash-verified binary lock, including the pinned audited pip version. Subsequent local project installation disables dependency resolution and build isolation. Keep the runner image and pinned setup action within the supply-chain review.
5. Dispatch Release on protected main without creating a tag. Inspect the exact-lock wheel smoke result, distributions, SPDX SBOM, SHA256SUMS, release-evidence.json, and Sigstore bundles in the SHA-named signed preflight artifact. Verify hashes and signature identity against the repository/workflow/candidate; an unsigned build artifact is not sufficient.
6. Record the preflight URL and artifact retention/download location. GitHub artifact retention is not permanent release storage. Download accepted evidence into the approved internal evidence store before expiration; do not include restricted tenant artifacts in public release files.
7. Complete the relevant adopter acceptance rows below. Obtain a separate release decision. Only then create the matching version tag through the protected maintainer process. The workflow labels GitHub releases as prereleases and does not publish to PyPI. Stable promotion requires a subsequent explicit scope and policy decision.

The package version alone is not a stability claim. Use the exact Git commit, distribution checksums, supported-mode matrix, and prerelease status together.

## Adopter evidence record

Copy this record into the approved private evidence store for each capability and installation. Direct tenant/resource IDs and Git provenance belong here, not in shareable diagnostic JSON. Leave missing evidence marked `pending`, not `pass`.

| Field | Value |
| --- | --- |
| Capability and allowed operations | Pending agreement |
| Non-production tenant, repository, connection, model and branch IDs | Pending explicit selection |
| Test owner / recovery owner / independent reviewer | Pending named assignments |
| Candidate SHA / policy hash / configured Action SHA | Pending candidate selection |
| Expected decision and exact test input identity | Pending approved fixture |
| Actual exit code, coverage, run/check URL and head SHA | Pending execution |
| Public artifact checksums, schema-2 privacy inspection, and explicit upload inventory | Pending inspection |
| Restricted run/job records and retention policy | Pending review; never publish raw payloads |
| Failure recovery result and residual state | Pending exercise |
| Reviewer decision, limitations, supported modes | Pending acceptance |

Core cases: valid and intentionally invalid models/content; correct discovery/branch mapping; representative semantic and downstream changes; malformed/unavailable evidence; withheld-secret/fork behavior; public JSON, Markdown, SARIF, JUnit and log inspection; blocked invalid/unreviewed merge; reviewed recovery.

Diagnostic migration cases: verify both standard and strict produce only the minimum-data public schema; verify generated references do not replace trusted identity checks; inspect supported-file-only uploads and stale-file exclusion. Exercise synthetic PII/PHI/payment-like markers, unknown fields, names/paths, and exception text without real regulated test data. This evidence does not certify compliance or establish zero possible leakage. Prior artifacts require a separate retention/exposure review.

Report-delivery cases: `public/report.json` must exist and remain privacy-safe for JSON-only, Markdown-only, SARIF-only and default formats. Match trusted `reporting.output_dir` with workflow `OMNIFLOW_REPORT_DIR`, including a spaced directory. A missing/malformed selected report cannot fall back to `.omniflow`. Exercise bootstrap failure, cancellation, partial writes and an intermediate held-readiness pass followed by a final source/state failure. All six examples must prevent stale publication; post-upload receipts, not downloaded pre-upload snapshots, establish publication. A completed shared dbt refresh can advance synchronized state despite incomplete optional validation, but never satisfies current-head readiness by itself.

Query/local-AI cases: validate configured query assertions with known expected results and bounded budgets; failed, partial or unsupported evidence must not become a pass. Keep raw queries/results and proposal text out of public artifacts. Test source drift and explicit approval for local proposals. Hosted native query and optional-workflow acceptance remain pending until recorded for the exact release; AI repair remains excluded.

Content-regression cases: explicitly approved new-only branch/base scope; existing readable issues remain visible without blocking, new/increased occurrences block, and incomplete evidence stays failed. Comment-only classification remains opt-in and never skips validation. Isolated live testing reproduced the grouped dashboard-filter response and justified a narrow adapter; retain evidence from the exact candidate's healthy/broken/restored replay. The actual customer response, API-owner contract agreement, and a successful live consumer pilot remain separate acceptance evidence—not satisfied by a synthetic replay or this documentation.

Model-regression cases: independent explicit model policy; same live model/base and credential scope; matching existing errors, extra occurrences, changed severity, and warning enforcement; malformed/unavailable and ambiguous identity cannot establish a pass. Verify model and content are strict for orchestrated no-branch health, post-sync, and repair verification. Passing comparisons do not certify clean health or prove PR causality.

dbt impact cases: additive change, referenced column/model removal, surviving node changing physical relation, distinct base/head content, Jinja/SELECT */unsupported SQL or missing manifest, untrusted/fork data, irrelevant skip. Record analyzer and coverage; no heuristic result is comprehensive validation.

dbt sync/hold cases: successful deploy/refresh/post-validation; failed deploy prevents refresh; refresh failure after successful deploy records partial state; current PR head changing during sync/revalidation; absent/unreachable sync state; failed state write/reread; custom hold label; no deployment loop; manual merge only after the current-head readiness check and review. Use expand → migrate → contract. A hold cannot make a hard warehouse rename safe by itself.

AI eval cases: zero sample limit still blocks regressions; cancelled/incomplete/missing scores; unsupported fractional scores; multiple models; duplicate/missing prompt rows; partial or ambiguous run creation; timeout and known-run reconciliation; minimal schema privacy through CLI/Action/artifacts in both standard and strict settings, including no public prompt IDs. Confirm actual API execution, permissions, quota, and cost with the tenant owner. Do not assume core validation's no-query behavior applies.

## Operations, upgrade and recovery

- Start with a bounded non-production pilot. Move to advisory production observation only after evidence review; agree duration, monitoring, owners, and escalation before enabling a required production check.
- If upgrading a policy-free installation, first bootstrap a compatible required root [`.omniflow.yml`](../.omniflow.yml) on the protected base through review, then coordinate the supporting Action SHA and new settings. A policy introduced only in the upgrade PR head is not trusted by that run. Older pins reject `checks.model_validation.fail_on_new_only`; do not introduce it while leaving an incompatible Action active. The canonical template explicitly enables both model and content new-only gating; review those independent choices with the owners.
- Upgrade through reviewed changes to the pinned Action SHA and policy/schema. Preserve the previous verified SHA, policy hash, and evidence. Re-run only affected capability acceptance plus the core smoke checks; verify missing policy fails with configuration exit `2` and cannot be replaced by environment or identity overrides.
- Recover a validator regression by a reviewed revert or restoring the prior verified Action SHA. Preserve branch protections. If urgent governance recovery is necessary, a named administrator must record the reason, exact temporary change and restoration; it is not approval to force a known-invalid merge.
- On a partial dbt failure, pause further changes, inspect the warehouse and recorded refresh/job state, restore compatibility or use a reviewed forward fix, then explicitly revalidate the current PR head. Completed dbt/warehouse changes do not automatically roll back when Omni fails.
- For an interrupted AI eval, reconcile known owned run IDs before retrying. Ambiguous creation must be investigated, not blindly retried. Restrict lifecycle records and apply the approved retention policy.
- For suspected credential exposure, stop affected workflows, revoke/rotate the affected credential, restrict affected artifacts, preserve minimal incident evidence, and use the canonical private security-reporting channel. Avoid public issue/PR disclosure of secrets or customer content.
- Maintenance, on-call response, artifact retention, credentials, API access and tenant recovery need named owners. No SLA or ownership commitment is implied by the alpha software.
