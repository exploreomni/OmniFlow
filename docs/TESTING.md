# Testing OmniFlow

OmniFlow uses four evidence layers. A green result in one layer must not be presented as proof of another.

1. Unit and contract tests verify parsing, policy, redaction, retries, routing, report formats, and failure behavior.
2. The deterministic alpha simulator exercises complete CLI runs against a bounded fake Omni API.
3. GitHub Actions verifies the packaged action, pinned dependencies, supported Python versions, security scans, and artifacts.
4. The adopter live gate verifies the customer's Omni tenant, token permissions, Git integration, branch mapping, content, and GitHub protections.

## Capability Matrix

| Capability | Automated evidence | Required live evidence |
| --- | --- | --- |
| Omni and non-Omni PR routing | Unit tests and simulator | One irrelevant PR skips; an Omni PR runs; dbt-only changes run the credential-free analyzer when enabled |
| Trusted model discovery | Unit tests plus an orchestrated two-model simulator run for marker, push, fork, and ambiguity paths | Omni-created PR resolves the expected model and branch |
| Required trusted policy | Missing-file, trusted-base, custom-path, and override tests | Root `.omniflow.yml` is bootstrapped before the Action upgrade; PR-head-only policy and environment/identity-only setup cannot bypass exit `2` |
| Trusted ancillary classification | Pattern grammar, overlap, source/control conflict and base/head tests | A narrow Git-only ancillary rule works without excluding any API-authored source; PR-head-only changes cannot grant exclusions |
| Model validation | Unit parser and policy tests | Valid and intentionally invalid branch checks |
| Content validation | Unit extraction, labels, history, base comparison, and redaction tests | Tenant Content Validator completes with expected scope |
| YAML pull | Unit security and manifest tests | Authored and fully resolved pulls complete with checksums |
| Semantic diff and lint | Unit change and rule tests | A harmless semantic change produces the expected risk summary |
| Regression-first enforcement | Baseline-first calls, run-local reuse, lint debt, strict/advisory and final-drift tests | Existing debt is nonblocking only with verified PR scope; new breaks block; advisory is visibly incomplete and never a readiness claim |
| Experimental targeted content | Symmetric selector, deduplication, partial/missing/changed inventory fallback tests | Not currently qualified for CLI activation: dependency and branch-draft coverage must be independently proven; the flag alone remains broad |
| Downstream contracts | Unit and simulator reference tests | Targeted content searches complete without coverage gaps |
| dbt exposures | Unit normalization, partial coverage, failure policy, and simulator tests | Base and branch calls return expected dashboard and dependency counts |
| Post-deployment dbt sync | Unit API contract, polling, event, branch, timeout, action, and evidence tests | Controlled refresh completes after a real dbt deployment and Git side effects are understood |
| Breaking change hold | Unit detection, durable state, and current-head revalidation tests | A held PR stays blocked after sync until explicit fresh validation attaches success to its unchanged head; manual merge follows review |
| dbt impact analysis | Unit manifest, bounded head acquisition, heuristic limits, relation change, and routing tests | A referenced column removal or relation move blocks; unsupported heuristic evidence remains explicitly incomplete |
| AI eval (opt-in, binary scoring only) | Full CLI/artifact regressions for zero sampling, incomplete/unsupported results, model routing, privacy and cleanup; loopback enabled-Action CI fixture | Confirm tenant scoring/permission/quota contracts, a real controlled regression, cancellation/recovery, and final public/restricted artifacts before enabling |
| Query-result contracts (opt-in) | Fixed-policy ordinals, bounded query execution, baseline/candidate assertions, budgets, incomplete evidence and privacy tests | Approved warehouse permissions/cost, representative assertions, controlled result regression and recovery; model/content validity does not establish query correctness |
| Local AI proposal workflow | Local proposal/approval/validation safety tests | Maintainer-only reviewed proposal with source proof, explicit approval and independently checked result; not autonomous repair or production acceptance |
| JSON, Markdown, SARIF, and JUnit | Schema-2 projection, render, synthetic sensitive-marker, and packaged action tests | Supported public files open, use anonymous context references, and omit customer text/direct identities/paths; workflow uploads are explicitly file-listed |
| GitHub annotations and failure/recovery notifications | Escaping, public projection, same-head/run freshness, pagination, retry deduplication and out-of-order tests | Fresh controlled failure then recovery shows the correct current run and upload receipt; a posted comment does not verify email delivery |
| Report delivery and optional workflow freshness | Mandatory JSON under every format selection, custom/spaced directory, exact-file cleanup, failed bootstrap, intermediate held-readiness and final-operation tests | Each adopted workflow's same-run artifacts/receipts are inspected; no stale pass survives setup failure, cancellation or failed final readiness |
| AI Repair development scaffold | Unit rollback and safety tests only | Unsupported; excluded from customer adoption. Maintainer exercises do not qualify it |
| Release supply chain | Workflow and repository hardening tests plus manual signed preflight | Protected release environment produces signed artifacts, checksums, and SBOM |

## Local Regression Gate

During development, run the focused regression for each fix once, then run this broader gate once at an integration boundary. Re-run failures and directly affected tests after a correction; do not repeatedly execute every layer for unchanged code. Live checks use only an explicitly approved non-production target. Track unexecuted gates in [release readiness](RELEASE_READINESS.md).

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade "pip==26.2" "setuptools==84.0.0"
python -m pip install -e ".[dev]"
pytest --cov=omniflow
ruff check .
bandit -c pyproject.toml -r src
python scripts/simulate_alpha.py
python -m build
twine check dist/*
```

The simulator opens a temporary localhost port. Run it in an environment that permits loopback network access.

## Credential And Content Scope Acceptance

The current client accepts an Organization API Key or Personal Access Token (PAT) through the same Bearer-token path. Automated tests do not establish either credential's effective permissions or content coverage in a live tenant. Before adoption or a credential change:

1. Use an explicitly approved non-production model and the credential type the operator has chosen. Record the type, target tenant/model/branch, enabled checks, labels, personal-folder setting, and expected content scope without recording the secret.
2. With a PAT, leave `omni.user_id`, `OMNI_USER_ID`, and `--user-id` unset, including the PAT owner's own ID. For an Org key with an explicit actor, configure the credential owner's global user UUID (`whoami.user.id`); verify that OmniFlow resolves it to the same owner's membership UUID for the Content Validator request. Do not substitute a membership UUID into configuration or impersonate another user to test this. Review the scope limitations in [Configuration](CONFIGURATION.md#credential-choice-and-user-scope).
3. Validate one known-good branch and one controlled invalid change affecting known content. Confirm the expected content is actually represented and the invalid change fails the gate. A successful `doctor` run alone is insufficient.
4. Check a controlled permission failure on a required validation endpoint blocks the run, not an empty or clean report. Optional checks must make unavailable coverage visible and follow their configured failure policy; for example, dbt exposures warn on unavailability unless `checks.dbt_exposures.fail_on_unavailable` is enabled. Confirm public reports exclude customer text and direct identities under both standard and strict settings. Inspect the separate repository-only diagnostics for verified authored identifiers/source locations and test `reporting.diagnostics.mode: minimal` on the trusted base to disable that tier; strict redaction is not its opt-out.
5. Record uncovered models/content and validation-depth limits. Org keys do not enable whole-instance discovery and personal folders remain excluded by default. `checks.content_validation.force_full_validation` is opt-in; compare representative runtimes at default and full depth before adoption, including a document with many queries. Confirm branch/base/rereads use the same setting, errors do not downgrade it, and reports identify requested depth without claiming exhaustive coverage.

An adopter needs to validate its selected credential, not obtain both types merely to install OmniFlow. Maintainers should record separate controlled live evidence for each type before claiming tenant-tested support for both. Unexecuted checks remain coverage gaps; local tests and this checklist are not production acceptance.

For the content-identity adapter, use synthetic transport fixtures for negative and credential-change cases; use approved same-account live reads only to qualify the actual endpoint contract:

- Shared-only requests without an actor must preserve existing behavior: no added `whoami` preflight and no `userId` parameter.
- Org-key owner configuration must send the verified membership as `userId` for base, candidate, consistency rereads and targeted/downstream searches. For personal-folder metadata, confirm the restricted catalog uses that same membership as `creatorId`. Expected known content must remain present; HTTP 200 or equal counts alone do not establish equivalent scope.
- PAT requests must omit validator `userId`, including when personal folders are enabled. A configured actor, unknown actor, malformed identity, missing membership or denied identity lookup must not cause a fallback to another user's content or an empty successful result. Missing explicit Org-key personal scope must still block.
- Instance or credential changes during a comparison must invalidate its reused evidence with `validation_identity_changed`; correct the setup and begin a fresh run. Distinct actor/visibility settings must not reuse each other's cached evidence. Preserve identical scope across base and candidate, and keep all direct identities and API response text out of public JSON, Markdown, annotations, SARIF and JUnit.
- Version-4 standalone history must reject older identity assumptions and mismatched actor scope. Regenerate an authorized baseline rather than relabeling old evidence; a missing usable baseline remains incomplete, not a clean comparison. Live PR comparisons continue to read their base fresh.

Do not classify every HTTP 404 as this adapter issue. Record its safe operation/status and investigate the selected model and scope. A controlled same-account comparison can qualify a particular endpoint and credential, not all tenants, credential types or private-content visibility.

## Draft Identity Regression Acceptance

Use a disposable non-production branch and dashboard for hosted acceptance. Do
not alter permissions or content on shared/customer dashboards to induce errors.

1. Verify one direct draft-state read returns its expected `draftOf.identifier`,
   and the parent's draft metadata confirms the selected branch, active status,
   freshness, and version. Keep only sanitized results, not raw document bodies.
2. Run a comment-only PR with existing model/content debt. Existing matched
   findings must remain nonblocking. Add one controlled new content defect in a
   separate PR; it must block. Repair it and confirm recovery.
3. Include many unrelated documents and a valid unrelated stale/archived draft.
   Only unknown identifiers returned by the validator may cause draft lookups;
   unrelated documents must not be searched or cause lookup failures.
4. Simulate a denied required identity read, timeout, malformed response and
   missing parent at the test transport boundary without changing tenant-wide
   access. Each must remain incomplete with its specific identity code, safe
   operation/status/cause, and no raw text in any public artifact.
5. Change or archive the selected disposable draft between discovery and recheck.
   The run must not reuse the earlier mapping or waive findings. Check final
   execution states: content failed, candidate partial/final recheck not run,
   and subsequent checks not run. No final state may remain running.
6. Inspect the actual PR comment, full report and download artifacts. Verify the
   separately eligible private tier, public privacy reasons, and same-actor
   constraints. Restore/clean up only owned fixtures and retain safe receipts.

Unit tests and a read-only live identity probe qualify narrower paths; neither
substitutes for hosted Action acceptance or the original customer PR rerun.

## Live dbt Exposure Gate

The [Omni dbt exposures API](https://docs.omni.co/api/dbt/get-dbt-exposures) requires Connection Admin permission and supports `branch_id`. Before enabling the check as a gate:

1. Confirm the Omni connection has a formal dbt integration.
2. Confirm at least one published shared dashboard references dbt models through Omni model references.
3. Choose a dedicated Organization API Key or service-user PAT with access required by the endpoint and enabled checks. A PAT inherits its user's permissions; an Org key carries Organization Admin privileges. Review [credential tradeoffs](../README.md#2-add-the-api-secret).
4. Temporarily set `checks.dbt_exposures.fail_on_unavailable: true` for the first controlled run.
5. Run both the base model and an existing Omni branch.
6. Verify permitted `total_records`, `total_exposures`, `unmapped_dashboards`, and `coverage_status` in schema-2 JSON where present. Verify detailed dependency identities/counts against authorized private evidence; Markdown is a minimal status/finding view, not a detailed lineage inventory.
7. Inspect each explicitly supported public artifact for absent customer text, direct IDs/paths, owner email, dashboard URL, tokens, and raw results. Use synthetic PII/PHI/payment-like markers for automated privacy tests; never introduce actual regulated data just to test redaction. Passing marker tests is not proof of compliance or zero possible exposure.

Omni documents that exposure generation covers published dashboards in shared space. Private dashboards and workbooks are not included. Direct database-table references can also prevent dbt dependency mapping; use `${model_name}` references where lineage is required. See [Pushing exposures to dbt](https://docs.omni.co/integrations/dbt/exposures).

dbt exposure enrichment supplements OmniFlow's targeted Content Validator contract analysis. It does not replace it and it does not run `dbt build`.

## Live Post-Deployment dbt Sync Gate

Do not treat mocked refresh tests as proof that a customer connection will synchronize. Before enabling this stage in production:

1. Use a non-production connection and model with the same dbt and Git settings as production.
2. Confirm `base_branch` is present for every model in `.omni/flow.json`.
3. Create the separate `OMNIFLOW_SYNC_API_KEY` protected-environment secret containing the chosen Organization API Key or service-user PAT. Confirm the PAT's required Modeler or Connection Admin permission, or the Org key's Organization Admin scope, plus access for enabled post-sync checks. See [sync credential requirements](DBT_SYNC.md#add-the-protected-job).
4. Deploy a harmless dbt metadata change, such as a description, through the actual deployment command.
5. Confirm a failed dbt command prevents the OmniFlow sync step from starting.
6. Confirm the refresh returns a job ID, polling reaches `COMPLETED`, and post-sync validation passes.
7. Verify the dbt change appears in the expected shared model or branch, depending on Branch based schema refresh.
8. Inspect the Git repository for an Omni-generated commit or system-sync pull request and confirm it does not retrigger the dbt deployment workflow.
9. Download the explicitly listed `dbt-sync.json`, `report.json`, and `evidence.json`; verify safe status/count/policy fields, schema version, and generated references. Verify exact model, branch, job, and commit identity separately using trusted workflow/Omni state and authorized private execution evidence. Confirm direct identities, paths, customer text, tokens, raw payloads, authored YAML, and query results are absent from the public files.
10. Exercise one controlled API failure or insufficient-permission token and confirm exit code `3` or `4` fails deployment completion without implying that the preceding dbt deployment was rolled back.

Record this as adopter-specific live evidence. OmniFlow automation can prove that the documented API exchange and configured checks completed; it cannot independently prove that dbt transformed the intended production data or that a human approved a system-sync pull request.

## Privacy and Content-Regression Acceptance

- Exercise the same synthetic sensitive values under both standard and strict settings; both must yield the minimal public schema across JSON, Markdown, SARIF, JUnit, annotations, and public failure messages. Check unknown fields and filenames, not only known `message` fields.
- Keep identity/provenance assertions on trusted in-memory or separately authorized private evidence. Do not re-add direct identifiers or Git/Action hashes to public output merely to preserve an older test assertion.
- Verify a complete new-only branch/base comparison permits unchanged readable baseline issues while still reporting `issues_in_scope`; new or increased occurrences must block. Missing/ambiguous/unreadable evidence must never establish a clean baseline.
- Exercise model and content policies independently and together. Model comparisons must preserve identity, severity, and multiplicity; warning-to-error changes and extra errors are new. Warning blocking must follow `fail_on_warnings`, and the content environment override must not change model policy.
- Verify authored candidate/base YAML against immutable PR head/base before and after new-only validation. Missing Git evidence, out-of-sync YAML, and changes between observations must block in strict mode, even with an empty current issue list. Initial ordinary sync gaps may retry once; final drift cannot be retried into a pass. Treat sampled consistency and API default content depth as explicit coverage limits, not an atomic or exhaustive guarantee.
- Confirm each new-only validator calls the base before its candidate with identical credential, visibility and requested depth. Reuse stays within one run and exact request scope; mandatory draft/evidence rereads bypass reuse and still detect changes.
- Confirm existing ordinary semantic-lint debt is nonblocking in verified regression PRs, while new/additional findings follow severity. Personal-folder security-scope restrictions, source-exclusion conflicts, contracts and enabled holds are never grandfathered.
- With explicit onboarding advisory mode, a permitted sync gap may exit `0` only as advisory/incomplete health evidence. New/existing/resolved attribution must be absent. API/malformed-evidence/security blockers and enabled breaking-change holds still block; contracts/lint that did not run stay unverified. JUnit's skipped advisory finding is not a successful validation assertion.
- Exercise trusted repository-relative ancillary rules on both base and head, including overlapping patterns, known-source wrappers, API-returned unknown sources and PR-head-only policy edits. Verify public mismatch categories/counts never leak rule text or paths.
- Keep targeted content disabled by default. The current CLI must remain broad when the flag is enabled without independent dependency/draft qualification. Synthetic qualified targeting must use both old/new selectors, deduplicate overlap, preserve real errors and reacquire fresh broad evidence for partial/disappearing/conflicting results. Do not treat those unit tests or a partial native probe as live coverage qualification.
- Confirm orchestrated no-branch, post-sync, and repair checks enforce strict model/content health even with both new-only switches enabled. No self-comparison or stale history may manufacture a pass. Standalone content's explicitly selected scoped history behavior remains separately covered.
- Verify opt-in comment-only classification is explanatory: `content_validation_required` remains true and the normal checks still run. No Git-only classification establishes live draft state.
- Exercise the grouped `filter_name` / `issues` format reproduced in isolated live testing: healthy fixture passes, missing-field draft produces a readable new defect (exit `1`, not `4`), restoring the filter passes. An in-memory replay of the same observed issue on both sides proves existing-issue policy but is not a live broken-baseline test. Confirm cleanup and unchanged shared-model YAML; do not publish the broken draft or execute warehouse queries.
- Test empty/non-JSON HTTP responses separately from malformed JSON issue structures. All remain exit `4` with incomplete comparison, and no response bytes may enter public output. Test branch and base failures.
- Verify filter identity and occurrence preservation, old-history version invalidation, public-report history rejection, and stale standard-output replacement for standalone content/model success and failure. A fresh consumer run is still required to confirm the customer's original PR is resolved; broader undocumented shapes require new evidence, not recursive guessing.
- For branch drafts, verify an authoritative active draft-to-published mapping before labels and comparisons: unchanged existing filter debt passes; an additional filter is exactly one new error; a distinct dashboard with identical text stays distinct; a previously healthy dashboard's first error blocks. Check query identifiers separately, including a same-tile metadata edit.
- Wrong-branch, archived, stale, missing-permission, duplicate, incomplete and changing mapping evidence must never produce a pass. Verify bounded per-document lookup caching and exclusion of canonical identities from every public output. Keep no-branch and post-sync strict health unchanged.
- Repeat hosted PR validation with both model and content baseline debt in an isolated shared model. Save authored YAML/checksums and synthetic document definitions first; verify the candidate/base against Git, deploy only the passing disposable candidate, restore by expected-last-write checks, and verify original source YAML and issue counts. Record native API deployment separately from automatic Git-webhook synchronization.

## Intentionally Bounded Tests

- Do not run `--unsafe-raw-output` against customer content as a routine test. Its behavior is covered with synthetic payloads.
- Do not send secrets to fork pull requests. Fork routing is proven with withheld-secret tests and should fail closed for Omni changes.
- Do not test AI rollback or destructive model changes against production models.
- Do not test hard schema removals against production. Prove dbt sync with an additive non-production change first.
- Dispatch the Release workflow on protected `main` to test the build, SPDX SBOM, checksums, and keyless signatures without publishing a release.
- Do not publish a release merely to test release automation. Use a version tag and the protected release gate only for intentional versions.

Record the workflow URL, commit SHA, policy decision, artifact hash or retention ID, and any coverage gaps in the approved private adopter evidence record. The public report intentionally omits commit and tenant identities. A local pass does not establish tenant permissions, branch protection, privacy compliance, or production readiness.
