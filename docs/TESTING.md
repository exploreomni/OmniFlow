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
| JSON, Markdown, SARIF, and JUnit | Unit render tests and packaged action tests | Public artifact downloads open and contain only redacted evidence |
| GitHub annotations and PR summary | Unit escaping tests | A controlled PR displays warnings or errors and updates one bot comment |
| AI Repair development scaffold | Unit rollback and safety tests | Maintainer-only non-production failure, repair, rerun, and rollback exercises |
| Release supply chain | Workflow and repository hardening tests plus manual signed preflight | Protected release environment produces signed artifacts, checksums, and SBOM |

## Local Regression Gate

During development, run the focused regression for each fix once, then run this broader gate once at an integration boundary. Re-run failures and directly affected tests after a correction; do not repeatedly execute every layer for unchanged code. Live checks use only an explicitly approved non-production target. Track unexecuted gates in [release readiness](RELEASE_READINESS.md).

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade "pip==26.2" "setuptools==83.0.0"
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
2. With a PAT, leave `omni.user_id`, `OMNI_USER_ID`, and `--user-id` unset. With an Org key, verify any intended acting-user scope. Review the current personal-folder metadata and identifier limitations in [Configuration](CONFIGURATION.md#credential-choice-and-user-scope); do not assume broader privileges resolve them.
3. Validate one known-good branch and one controlled invalid change affecting known content. Confirm the expected content is actually represented and the invalid change fails the gate. A successful `doctor` run alone is insufficient.
4. Check a controlled permission failure on a required validation endpoint blocks the run, not an empty or clean report. Optional checks must make unavailable coverage visible and follow their configured failure policy; for example, dbt exposures warn on unavailability unless `checks.dbt_exposures.fail_on_unavailable` is enabled. Inspect the downloaded public reports for metadata the GitHub audience should not receive; use strict redaction where required.
5. Record uncovered models/content and validation-depth limits. Org keys do not enable whole-instance discovery, personal folders remain excluded by default, and OmniFlow does not currently request the API's `force_full_validation` option.

An adopter needs to validate its selected credential, not obtain both types merely to install OmniFlow. Maintainers should record separate controlled live evidence for each type before claiming tenant-tested support for both. Unexecuted checks remain coverage gaps; local tests and this checklist are not production acceptance.

## Live dbt Exposure Gate

The [Omni dbt exposures API](https://docs.omni.co/api/dbt/get-dbt-exposures) requires Connection Admin permission and supports `branch_id`. Before enabling the check as a gate:

1. Confirm the Omni connection has a formal dbt integration.
2. Confirm at least one published shared dashboard references dbt models through Omni model references.
3. Choose a dedicated Organization API Key or service-user PAT with access required by the endpoint and enabled checks. A PAT inherits its user's permissions; an Org key carries Organization Admin privileges. Review [credential tradeoffs](../README.md#2-add-the-api-secret).
4. Temporarily set `checks.dbt_exposures.fail_on_unavailable: true` for the first controlled run.
5. Run both the base model and an existing Omni branch.
6. Verify `total_records`, `total_exposures`, `unmapped_dashboards`, dependency counts, and `coverage_status` in the JSON and Markdown reports.
7. Verify no owner email, dashboard URL, token, or raw query result appears in public artifacts.

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
9. Download `dbt-sync.json`, `report.json`, and `evidence.json`; verify model, branch, job, commit, and policy metadata, then confirm no token, raw payload, authored YAML, or query result is present.
10. Exercise one controlled API failure or insufficient-permission token and confirm exit code `3` or `4` fails deployment completion without implying that the preceding dbt deployment was rolled back.

Record this as adopter-specific live evidence. OmniFlow automation can prove that the documented API exchange and configured checks completed; it cannot independently prove that dbt transformed the intended production data or that a human approved a system-sync pull request.

## Intentionally Bounded Tests

- Do not run `--unsafe-raw-output` against customer content as a routine test. Its behavior is covered with synthetic payloads.
- Do not send secrets to fork pull requests. Fork routing is proven with withheld-secret tests and should fail closed for Omni changes.
- Do not test AI rollback or destructive model changes against production models.
- Do not test hard schema removals against production. Prove dbt sync with an additive non-production change first.
- Dispatch the Release workflow on protected `main` to test the build, SPDX SBOM, checksums, and keyless signatures without publishing a release.
- Do not publish a release merely to test release automation. Use a version tag and the protected release gate only for intentional versions.

Record the workflow URL, commit SHA, policy decision, artifact hash or retention ID, and any coverage gaps for each adopter live gate. A local pass does not establish tenant permissions, branch protection, or production readiness.
