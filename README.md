# OmniFlow

OmniFlow is an open-source, self-hosted CI/CD companion for teams developing the Omni semantic layer in GitHub. It runs in the adopter's own GitHub Actions environment, calls the adopter's Omni instance directly, and does not send model metadata to an OmniFlow service. The project is released under the [MIT License](LICENSE).

OmniFlow starts when an Omni model branch opens a pull request. It routes unrelated pull requests safely, validates the changed model, assesses downstream dashboard and report impact, applies configurable semantic and governance rules, and produces audit-friendly evidence before merge. Repositories that deploy dbt can also opt into a protected post-deployment Omni schema refresh and revalidation stage.

[Installation](docs/INSTALLATION.md) | [Architecture](docs/ARCHITECTURE.md) | [Limitations](docs/LIMITATIONS.md) | [Security model](docs/SECURITY_MODEL.md) | [Support](SUPPORT.md)

> **Project status: controlled alpha.** The core Omni-created pull-request path has passed automated, simulated, GitHub Actions, and maintainer-controlled non-production testing. This is not a hosted Omni service, a stable package release, or a compliance certification. Every adopter must complete the documented live gate against its own Omni instance, permissions, Git settings, content, GitHub plan, and branch protections before relying on OmniFlow as a required merge check. The optional dbt synchronization stage still requires its first live non-production connection test. See the complete [limitations and support boundary](docs/LIMITATIONS.md).

## What It Does

| Capability | Outcome |
| --- | --- |
| Pull-request routing | Runs Omni checks for registered model paths, skips unrelated changes, and fails closed when an Omni change cannot be identified safely. |
| Omni validation | Runs documented model and Content Validator APIs against the pull-request branch. |
| Semantic quality | Diffs authored YAML and evaluates configurable lint, contract, security, and governance rules. |
| Downstream impact | Searches visible Omni dashboards, reports, and queries for references to changed semantic objects. |
| dbt integration | Optionally enriches evidence with dbt exposures and refreshes Omni metadata after a protected production dbt deployment. |
| Review evidence | Produces redacted Markdown, JSON, SARIF, JUnit, evidence, and artifact-integrity records. |

OmniFlow does **not** host customer data, execute dbt, query the warehouse during core validation, merge or approve pull requests, deploy model YAML, promote Omni branches, or guarantee visibility into content the configured token cannot access. Its AI Repair code is an unreleased development scaffold and must remain disabled. These boundaries are explained in [Limitations](docs/LIMITATIONS.md).

## Quick Start

The complete walkthrough is in [Install OmniFlow In A GitHub Repository](docs/INSTALLATION.md). The short path is:

1. Confirm the Omni model already uses Git integration and Branch Mode with the target GitHub repository.
2. Copy [`.omni/flow.example.json`](.omni/flow.example.json) to `.omni/flow.json`, then add the tenant URL, model ID, repository model path, and base branch.
3. Copy [the workflow example](.github/workflow-examples/omniflow.yml) to `.github/workflows/omniflow.yml`, pin both action references to the same reviewed 40-character OmniFlow commit SHA, and add CODEOWNERS coverage.
4. Protect the base branch before storing any Omni credential. Require pull requests, normal reviewer approval, and review from the owners of the workflow and trusted metadata.
5. Optionally copy [`.omniflow.example.yml`](.omniflow.example.yml) to `.omniflow.yml`, then merge the setup files through the protected process.
6. Create a dedicated least-privilege Omni personal access token and store it as the GitHub Actions secret `OMNI_API_KEY`. Do not use an Organization API Key.
7. Create a harmless model change in an Omni branch, select **Create pull request**, and confirm OmniFlow validates the resulting GitHub pull request.
8. For repositories that deploy dbt, optionally add the protected [post-deployment dbt sync](docs/DBT_SYNC.md) after the production dbt command succeeds.

After the first live run succeeds, make the OmniFlow check required in GitHub branch protection. Continue with the [installation checklist](docs/INSTALLATION.md#step-9-verify-the-installation) before treating OmniFlow as a merge gate.

The workflow always preserves `report.sarif` in the evidence artifact. Repositories with GitHub Code Security enabled can additionally set the repository variable `OMNIFLOW_UPLOAD_SARIF=true` to publish those results into GitHub code scanning.

## What OmniFlow Checks

- Omni model validation, with separate error and warning policy
- Omni Content Validator results, including branch-to-base comparison for new-only policy
- Authored Models, Topics, Views, fields, and global or topic-level Relationship YAML
- Semantic lint rules for descriptions, primary keys, labels, topic owners, cardinality, and governance
- Breaking semantic changes and the dashboards, reports, and queries that reference them
- Optional, branch-aware dbt exposure metadata
- Optional post-deployment dbt metadata refresh with asynchronous job polling and full revalidation
- JSON, Markdown, SARIF, JUnit, and evidence artifacts

Core OmniFlow validation does not execute warehouse queries, store query results, write model YAML, or merge pull requests. An [AI Repair development scaffold](docs/AI_REPAIR.md) is present but is disabled, unreleased, and not supported for customer installation because Omni does not currently document a public Modeling Agent mutation API.

## End-User Flow

1. A developer works in an Omni model branch and selects **Create pull request**.
2. Omni creates the GitHub pull request.
3. GitHub Actions starts OmniFlow from the trusted base-branch workflow.
4. A credential-free preflight routes non-Omni pull requests to a successful `skipped` result before the validation process receives `OMNI_API_KEY`.
5. OmniFlow selects the changed model from trusted base-branch metadata and resolves the Omni branch from the GitHub head branch.
6. It pulls base and branch YAML, validates the model and content, computes the semantic diff, and searches Omni for downstream references.
7. The pull request receives a redacted reviewer summary; detailed artifacts remain restricted to the runner unless explicitly uploaded.
8. GitHub branch protection blocks merge when required OmniFlow checks fail.
9. After review and approval, the pull request is merged. Omni's configured pull-request webhook promotes the Omni branch and publishes associated draft content.
10. When the repository also deploys dbt, the protected production job can run `omniflow dbt sync --auto` after dbt succeeds, then rerun every enabled OmniFlow check against the refreshed model.

The unreleased AI Repair scaffold is not part of this customer workflow. It must remain disabled until Omni publishes a supported Modeling Agent API contract.

The final promotion is Omni Git integration behavior, not an OmniFlow API write. See [Omni Branch Mode](https://docs.omni.co/content/develop/branch-mode) and [Git integration settings](https://docs.omni.co/integrations/git/settings).

## Installation Details

These are the required installation components. New users should follow the [full step-by-step guide](docs/INSTALLATION.md), which includes prerequisites, exact sequencing, first-run expectations, and branch protection.

### 1. Add The Workflow

Copy `.github/workflow-examples/omniflow.yml` into the customer repository as `.github/workflows/omniflow.yml`, then replace `<pinned-commit-sha>` with a reviewed OmniFlow commit SHA.

The action installs from that pinned checkout during alpha testing:

```yaml
- uses: exploreomni/OmniFlow@<pinned-commit-sha>
  with:
    omni-api-key: ${{ secrets.OMNI_API_KEY }}
```

Do not install an unpinned branch. OmniFlow does not currently publish an official PyPI package; `pip install omniflow-ci` is not a supported installation path. The action installs from the reviewed checkout using an exact, hash-verified Python 3.11 Linux dependency lock.

The command remains `omniflow`, while the future Python distribution name is `omniflow-ci`. The `omniflow` name on PyPI belongs to an unrelated OMOP data-harmonization project. See the [distribution naming decision](docs/DISTRIBUTION.md).

### 2. Add The API Secret

Create a dedicated Omni service user with access only to the models and content being validated, then create a personal access token for that user and save it as the GitHub Actions secret `OMNI_API_KEY`. Do not use an Organization API Key: Omni documents that organization keys have Organization Admin permissions, while personal access tokens inherit their user's permissions. Tokens do not expire automatically, so define a rotation schedule and revoke them immediately after suspected exposure or maintainer access changes. See [Omni API authentication](https://docs.omni.co/api/authentication).

The token must be able to list model branches, read model YAML, validate the model, run the Content Validator, and retrieve content labels. The optional dbt exposures endpoint requires Connection Admin permissions according to the [Omni API reference](https://docs.omni.co/api/dbt/get-dbt-exposures). Never place credentials in repository files.

### 3. Commit Trusted Model Identity Once

Commit `.omni/flow.json` to the protected base branch:

```json
{
  "version": 1,
  "models": [
    {
      "base_url": "https://customer.omniapp.co",
      "model_id": "00000000-0000-0000-0000-000000000000",
      "model_path": "omni/my_model",
      "base_branch": "main",
      "git_provider": "github",
      "web_url": "https://github.com/org/repo"
    }
  ]
}
```

This is non-secret bootstrap metadata, not per-PR configuration. Branch identity is discovered automatically. Omni's public documentation guarantees a branch-content link in an Omni-created PR description, but it does not currently document a stable machine-readable PR payload containing the Omni host and model ID. Until a live PR proves a safe contract, OmniFlow deliberately does not send a token to a host parsed from PR text.

### 4. Add Optional Policy

`.omniflow.yml` is optional. Defaults run model validation, content validation, semantic lint, semantic diff, and downstream contracts. Start with `.omniflow.example.yml` when customization is needed.

```yaml
contracts:
  fail_on:
    deleted_referenced_fields: true
    renamed_referenced_fields: true
    referenced_field_type_changes: true
    referenced_join_cardinality_changes: true
    coverage_gaps: true

checks:
  content_validation:
    fail_on_new_only: true
  model_validation:
    fail_on_warnings: false
```

### 5. Protect The Base Branch

Make the OmniFlow job a required GitHub check and require pull-request approval. Confirm the Omni pull-request webhook is configured; Omni documents that it is required to keep Git and Omni branches synchronized.

GitHub may require a Team or Enterprise organization plan to enforce rulesets or classic branch protection on a private repository. If enforcement is unavailable, use OmniFlow only for controlled testing until the repository plan or visibility supports the required protections.

### 6. AI Repair Development Status

Do not install AI Repair for customer use. The repository contains a guarded development scaffold, but the documented AI Jobs API is query-oriented and does not expose the Modeling Agent's branch-editing behavior. See [AI Repair Development Scaffold](docs/AI_REPAIR.md) for the blocker and safety design.

### 7. Optional Post-Deployment dbt Sync

Repositories that deploy dbt can enable a separate protected action mode after the production dbt command. It calls Omni's documented schema refresh API, polls the job to a terminal state, and reruns all enabled checks. It is disabled by default and never runs for repositories without the `deployment.dbt_sync.enabled` policy.

The command rejects pull-request events and non-base branches. Use a dedicated `OMNIFLOW_SYNC_API_KEY` protected environment secret and restrict workflow paths to dbt source files so Omni-generated Git commits cannot create a deployment loop. See [Post-Deployment dbt Synchronization](docs/DBT_SYNC.md).

## Trust And Routing

The example uses GitHub's `pull_request_target` event but never checks out or executes proposed PR code. A credential-free preflight retrieves changed filenames through GitHub's API and reads `.omni/flow.json`, `.omniflow.yml`, and the workflow itself from the trusted base branch. Only a selected Omni model context starts the validation process with `OMNI_API_KEY`; skipped PRs never inject it. This prevents a same-repository pull request from changing `base_url`, disabling gates, enabling unsafe output, replacing the action, or redirecting the token.

Do not add steps that check out and execute pull-request code in this privileged workflow. Keep ordinary dbt tests and application builds in separate `pull_request` workflows without the Omni secret.

An optional marker can select a model for a content-only or otherwise ambiguous pull request:

```html
<!-- omniflow-context {"model_id":"uuid","model_path":"omni/my_model","branch_name":"feature/my-change"} -->
```

The marker cannot provide `base_url`; its model and path must match trusted metadata; and its branch must match the GitHub head branch. Omni does not currently document this marker, so it must be added by customer automation if used.

Fork pull requests never receive the Omni secret. Non-Omni fork changes skip cleanly; fork changes that touch Omni files fail closed because they cannot be validated without a trusted secret. A maintainer can move the proposed change to a same-repository branch for a full run. Omni-like files outside registered model paths also fail closed so stale or missing routing metadata cannot silently bypass validation.

## Downstream Contracts

For every changed field, view, or topic, OmniFlow uses the documented Content Validator `find` and `find_type` parameters to retrieve content that references that semantic element. Relationship changes are mapped to their joined views and searched as view references.

Referenced deleted or renamed fields, referenced type changes, and referenced relationship cardinality changes fail by default. Unreferenced breaking changes remain warnings. A failed or incomplete dependency search is a blocking coverage gap by default; OmniFlow never labels every full-validator result as a reference to the failed element.

Default semantic-lint findings are advisory. Teams can promote individual rules to `error` in `.omniflow.yml`; the built-in gate does not turn an unreferenced deletion into a failure by itself.

The Content Validator still validates the model server-side. Label filtering is applied locally after content metadata lookup. See the [Content Validator API](https://docs.omni.co/api/content-validator/validate-content).

## Evidence And Privacy

Each run writes root and redacted public summaries:

```text
.omniflow/
  report.json
  report.md
  report.sarif
  junit.xml
  evidence.json
  dbt-sync.json          # present for dbt synchronization runs
  artifact-manifest.json
  public/
  restricted/<model_id>/
```

Public reports exclude API keys, raw payloads, email addresses, document URLs, and folder paths. `security.redaction_level: strict` also removes content names, query names, owners, labels, and free-text messages. Restricted artifacts are deleted by default and are never uploaded by the example workflow. Opt-in retention is intended only for an isolated, ephemeral runner and writes files with owner-only permissions.

Raw response output cannot be enabled in CI policy. The explicit `--unsafe-raw-output` option exists only on the local `content validate` debugging command.

The unreleased AI Repair scaffold emits `repair.json` and `repair.md` during maintainer testing. These contain status, IDs, file names, counts, validation summaries, rollback status, and commit metadata only. Authored YAML, prompts, AI result summaries, chat URLs, and query results are not persisted by OmniFlow.

## CLI

```bash
omniflow doctor --auto
omniflow run --auto
omniflow dbt sync --auto
omniflow repair ai --auto
omniflow content validate --base-url https://example.omniapp.co --model-id <id>
omniflow model validate --base-url https://example.omniapp.co --model-id <id>
omniflow yaml pull --base-url https://example.omniapp.co --model-id <id> --out .omniflow/yaml
omniflow exposures pull --base-url https://example.omniapp.co --model-id <id>
omniflow diff --base path/to/base/yaml --head path/to/head/yaml
```

Explicit identity flags are for local debugging. The customer workflow uses `omniflow run --auto`.

Exit codes are `0` success, `1` validation failure, `2` configuration error, `3` authentication or authorization error, `4` Omni API error, `5` security policy violation, and `6` internal error.

## Documentation

- [Step-by-step installation](docs/INSTALLATION.md)
- [Limitations and support boundary](docs/LIMITATIONS.md)
- [Architecture and process flow](docs/ARCHITECTURE.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Testing and live validation](docs/TESTING.md)
- [Post-deployment dbt synchronization](docs/DBT_SYNC.md)
- [AI Repair development scaffold](docs/AI_REPAIR.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Support and safe diagnostic sharing](SUPPORT.md)
- [Security policy](SECURITY.md)
- [Security model and operator responsibilities](docs/SECURITY_MODEL.md)
- [Contributing](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [MIT License](LICENSE)

## Local Verification

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade "pip==26.1.2" "setuptools==83.0.0"
python -m pip install -e ".[dev]"
pytest --cov=omniflow
ruff check .
bandit -c pyproject.toml -r src
python scripts/simulate_alpha.py
python -m build
twine check dist/*
```

The simulation covers same-repository, fork, and multi-model routing; contract failures; strict redaction; missing branches; malicious PR metadata; successful and partial dbt exposure coverage; one-refresh-per-connection dbt synchronization; post-sync revalidation; and refresh-job failure. The maintainers have also completed end-to-end live pull-request validation on a non-production model. Neither result replaces the adopter-specific live gate for actual Omni PR metadata, tenant permissions, branch mapping, Content Validator coverage, dbt exposure coverage, schema refresh and Git side effects, GitHub annotations/comments, branch-protection enforcement, or webhook promotion. Use the [testing matrix](docs/TESTING.md) to distinguish automated, live-tenant, and release evidence.

## Maintainer Release Setup

Controlled-alpha releases are GitHub releases only. A `vX.Y.Z` tag must point to a commit contained in protected `main`; the release build uses hash-locked tooling, creates an SBOM and checksums, and signs the artifacts through Sigstore before publication through the protected `github-release` environment. Maintainers can dispatch the Release workflow on protected `main` to build and sign the same bundle as a preflight without creating a GitHub release.

PyPI publication is intentionally disabled until maintainers secure the project name, configure a reviewed Trusted Publisher, and reintroduce that path in a separately reviewed change. A package using the OmniFlow name should not be treated as official unless this repository links to it.

## Official References

- [Omni model YAML API](https://docs.omni.co/api/models/get-model-yaml)
- [Omni model validation API](https://docs.omni.co/api/models/validate-model)
- [Omni Content Validator API](https://docs.omni.co/api/content-validator/validate-content)
- [Omni dbt exposures API](https://docs.omni.co/api/dbt/get-dbt-exposures)
- [Omni schema refresh API](https://docs.omni.co/api/models/refresh-schema)
- [Omni schema refresh job status API](https://docs.omni.co/api/jobs/get-job-status)
- [Omni AI Jobs API](https://docs.omni.co/api/ai/create-ai-job)
- [Omni AI data security](https://docs.omni.co/ai/security)
- [Omni Git branch commit API](https://docs.omni.co/api/model-git-configuration/create-or-update-a-pull-request-for-a-model-branch)
- [Omni Git integration best practices](https://docs.omni.co/integrations/git/best-practices)
- [GitHub secure workflow guidance](https://docs.github.com/en/actions/reference/security/secure-use)
- [PyPI Trusted Publisher setup](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
