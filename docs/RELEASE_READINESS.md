# Release readiness and adopter acceptance

## Supported scope

OmniFlow remains controlled alpha. A merge, successful CI run, signed preflight, release publication, and adopter acceptance are different states. No checklist entry is satisfied merely by this document existing.

| Capability | Candidate policy | Acceptance needed before required production gating |
| --- | --- | --- |
| Model/content validation, trusted discovery, semantic diff/lint, downstream contracts, public reports | Core candidate scope | Exact-SHA CI and release evidence plus adopter-specific valid/invalid checks, permissions, privacy, and recovery |
| dbt impact | Optional, separately qualified | Real Action base/head acquisition, manifest relation changes, credential-free routing, explicit incomplete coverage |
| dbt sync and holds | Optional, separately qualified | Approved non-production deployment/refresh, durable state, fresh current-head readiness check, failure recovery |
| AI eval | Optional, separately qualified | Approved scoring policy, prompt-set/model identity, privacy, quota/run lifecycle, controlled live regression |
| AI repair | Unsupported development scaffold | Excluded from customer release scope |

The official Action and release locks target Linux x86_64 / CPython 3.11. Unit CI covers Python 3.11–3.13; it does not establish that the Linux wheel lock works on other architectures. Pin a reviewed full 40-character Action SHA, never a floating branch.

## Maintainer release gate

1. Independently review changes and resolve all blocking findings. Require current Python tests, package, Action integration, dependency audit/review, workflow analysis, and SAST. Keep administrator enforcement enabled; do not bypass failed checks.
2. Configure independent CODEOWNERS for workflows, locks, discovery/trust, credentials, client, and reporting paths before requiring owner review. The PR author cannot be the only eligible owner. Assign an independent protected-release reviewer and prevent self-review once that roster is agreed.
3. Merge only fresh, green reviewed PRs. The signed preflight must run from the current protected `main` SHA. `scripts/verify_release_candidate.py` checks the latest main-push executions of Test, Dependency Scan, Actions Security, and SAST, including required successful jobs. Missing, skipped, failed, stale, or still-running evidence blocks release.
4. Audit both exact dependency locks. Runner-provided pip is the initial trust boundary: it installs only the hash-verified binary lock, including the pinned audited pip version. Subsequent local project installation disables dependency resolution and build isolation. Keep the runner image and pinned setup action within the supply-chain review.
5. Dispatch Release on protected main without creating a tag. Inspect the exact-lock wheel smoke result, distributions, SPDX SBOM, SHA256SUMS, release-evidence.json, and Sigstore bundles in the SHA-named signed preflight artifact. Verify hashes and signature identity against the repository/workflow/candidate; an unsigned build artifact is not sufficient.
6. Record the preflight URL and artifact retention/download location. GitHub artifact retention is not permanent release storage. Download accepted evidence into the approved internal evidence store before expiration; do not include restricted tenant artifacts in public release files.
7. Complete the relevant adopter acceptance rows below. Obtain a separate release decision. Only then create the matching version tag through the protected maintainer process. The workflow labels GitHub releases as prereleases and does not publish to PyPI. Stable promotion requires a subsequent explicit scope and policy decision.

The package version alone is not a stability claim. Use the exact Git commit, distribution checksums, supported-mode matrix, and prerelease status together.

## Adopter evidence record

Copy this record for each capability and installation. Leave missing evidence marked `pending`, not `pass`.

| Field | Value |
| --- | --- |
| Capability and allowed operations | Pending agreement |
| Non-production tenant, repository, connection, model and branch IDs | Pending explicit selection |
| Test owner / recovery owner / independent reviewer | Pending named assignments |
| Candidate SHA / policy hash / configured Action SHA | Pending candidate selection |
| Expected decision and exact test input identity | Pending approved fixture |
| Actual exit code, coverage, run/check URL and head SHA | Pending execution |
| Public artifact checksums and redaction inspection | Pending inspection |
| Restricted run/job records and retention policy | Pending review; never publish raw payloads |
| Failure recovery result and residual state | Pending exercise |
| Reviewer decision, limitations, supported modes | Pending acceptance |

Core cases: valid and intentionally invalid models/content; correct discovery/branch mapping; representative semantic and downstream changes; malformed/unavailable evidence; withheld-secret/fork behavior; public JSON, Markdown, SARIF, JUnit and log inspection; blocked invalid/unreviewed merge; reviewed recovery.

dbt impact cases: additive change, referenced column/model removal, surviving node changing physical relation, distinct base/head content, Jinja/SELECT */unsupported SQL or missing manifest, untrusted/fork data, irrelevant skip. Record analyzer and coverage; no heuristic result is comprehensive validation.

dbt sync/hold cases: successful deploy/refresh/post-validation; failed deploy prevents refresh; refresh failure after successful deploy records partial state; current PR head changing during sync/revalidation; absent/unreachable sync state; failed state write/reread; custom hold label; no deployment loop; manual merge only after the current-head readiness check and review. Use expand → migrate → contract. A hold cannot make a hard warehouse rename safe by itself.

AI eval cases: zero sample limit still blocks regressions; cancelled/incomplete/missing scores; unsupported fractional scores; multiple models; duplicate/missing prompt rows; partial or ambiguous run creation; timeout and known-run reconciliation; strict privacy through CLI/Action/artifacts. Confirm actual API execution, permissions, quota, and cost with the tenant owner. Do not assume core validation's no-query behavior applies.

## Operations, upgrade and recovery

- Start with a bounded non-production pilot. Move to advisory production observation only after evidence review; agree duration, monitoring, owners, and escalation before enabling a required production check.
- Upgrade through a reviewed PR changing the pinned Action SHA and any policy/schema changes together. Preserve the previous verified SHA, policy hash, and evidence. Re-run only affected capability acceptance plus the core smoke checks.
- Recover a validator regression by a reviewed revert or restoring the prior verified Action SHA. Preserve branch protections. If urgent governance recovery is necessary, a named administrator must record the reason, exact temporary change and restoration; it is not approval to force a known-invalid merge.
- On a partial dbt failure, pause further changes, inspect the warehouse and recorded refresh/job state, restore compatibility or use a reviewed forward fix, then explicitly revalidate the current PR head. Completed dbt/warehouse changes do not automatically roll back when Omni fails.
- For an interrupted AI eval, reconcile known owned run IDs before retrying. Ambiguous creation must be investigated, not blindly retried. Restrict lifecycle records and apply the approved retention policy.
- For suspected credential exposure, stop affected workflows, revoke/rotate the affected credential, restrict affected artifacts, preserve minimal incident evidence, and use the canonical private security-reporting channel. Avoid public issue/PR disclosure of secrets or customer content.
- Maintenance, on-call response, artifact retention, credentials, API access and tenant recovery need named owners. No SLA or ownership commitment is implied by the alpha software.
