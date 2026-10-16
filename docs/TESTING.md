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
| Model validation | Unit parser and policy tests | Valid and intentionally invalid branch checks |
| Content validation | Unit extraction, labels, history, base comparison, and redaction tests | Tenant Content Validator completes with expected scope |
| YAML pull | Unit security and manifest tests | Authored and fully resolved pulls complete with checksums |
| Semantic diff and lint | Unit change and rule tests | A harmless semantic change produces the expected risk summary |
| Downstream contracts | Unit and simulator reference tests | Targeted content searches complete without coverage gaps |
| dbt exposures | Unit normalization, partial coverage, failure policy, and simulator tests | Base and branch calls return expected dashboard and dependency counts |
| Post-deployment dbt sync | Unit API contract, polling, event, branch, timeout, action, and evidence tests | Controlled refresh completes after a real dbt deployment and Git side effects are understood |
| Breaking change hold | Unit detection, durable state, and current-head revalidation tests | A held PR stays blocked after sync until explicit fresh validation attaches success to its unchanged head; manual merge follows review |
| dbt impact analysis | Unit manifest, bounded head acquisition, heuristic limits, relation change, and routing tests | A referenced column removal or relation move blocks; unsupported heuristic evidence remains explicitly incomplete |
| AI eval (opt-in, binary scoring only) | Full CLI/artifact regressions for zero sampling, incomplete/unsupported results, model routing, privacy and cleanup; loopback enabled-Action CI fixture | Confirm tenant scoring/permission/quota contracts, a real controlled regression, cancellation/recovery, and final public/restricted artifacts before enabling |
| JSON, Markdown, SARIF, and JUnit | Schema-2 projection, render, synthetic sensitive-marker, and packaged action tests | Supported public files open, use anonymous context references, and omit customer text/direct identities/paths; workflow uploads are explicitly file-listed |
| GitHub annotations and PR summary | Unit escaping tests | A controlled PR displays warnings or errors and updates one bot comment |
| AI Repair development scaffold | Unit rollback and safety tests | Maintainer-only non-production failure, repair, rerun, and rollback exercises |
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

## Live dbt Exposure Gate

The [Omni dbt exposures API](https://docs.omni.co/api/dbt/get-dbt-exposures) requires Connection Admin permission and supports `branch_id`. Before enabling the check as a gate:

1. Confirm the Omni connection has a formal dbt integration.
2. Confirm at least one published shared dashboard references dbt models through Omni model references.
3. Use a dedicated Personal Access Token with only the permissions needed for the enabled checks.
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
3. Create the dedicated `OMNIFLOW_SYNC_API_KEY` protected-environment secret with the required Modeler or Connection Admin permission.
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
- Verify authored candidate/base YAML against immutable PR head/base before and after new-only validation. Missing Git evidence, out-of-sync YAML, and changes between observations must block, even with an empty current issue list. Treat sampled consistency and API default content depth as explicit coverage limits, not an atomic or exhaustive guarantee.
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
