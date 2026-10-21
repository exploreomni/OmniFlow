# OmniFlow

OmniFlow is an open-source, local-first CI/CD companion for Omni semantic-layer development. It runs in a customer's GitHub repository when an Omni branch opens a pull request, validates the model and its downstream content, and can synchronize Omni after an approved production dbt deployment.

> **Status:** Controlled alpha. The complete Omni-created PR, validation, evidence, merge, and Omni synchronization path has passed on a maintainer-controlled non-production model. The post-deployment dbt schema-refresh stage has automated contract coverage but still requires its first live non-production connection test. Each adopter must complete the live installation gate against its own model, permissions, dbt deployment, Git settings, GitHub plan, and branch protections before making OmniFlow a required check.

Historical pilot success does not qualify a new commit. Core validation is the initial release candidate scope; dbt impact, sync/hold, and AI eval require separate acceptance and remain opt-in. AI repair remains unsupported. Use the [release and adopter acceptance checklist](docs/RELEASE_READINESS.md) to record the exact candidate and outstanding gates.

## Quick Start

The complete walkthrough is in [Install OmniFlow In A GitHub Repository](docs/INSTALLATION.md). The short path is:

1. Confirm the Omni model already uses Git integration and Branch Mode with the target GitHub repository.
2. Copy [`.omni/flow.example.json`](.omni/flow.example.json) to `.omni/flow.json`, then add the tenant URL, model ID, repository model path, and base branch.
3. Copy [the workflow example](.github/workflow-examples/omniflow.yml) to `.github/workflows/omniflow.yml`, pin both action references to the same reviewed 40-character OmniFlow commit SHA, and add CODEOWNERS coverage.
4. Protect the base branch before storing any Omni credential. Require pull requests, normal reviewer approval, and review from the owners of the workflow and trusted metadata.
5. Copy the canonical [`.omniflow.yml`](.omniflow.yml) into the **customer repository root**, then merge it with the workflow and model metadata through the protected setup process before validating Omni pull requests. The policy file is required; it is separate from `.github/workflows/omniflow.yml`.
6. Choose an Omni **Organization API Key or Personal Access Token (PAT)** for your governance scope, then store it as the GitHub Actions secret `OMNI_API_KEY`. See [credential choice and permissions](#2-add-the-api-secret).
7. Create a harmless model change in an Omni branch, select **Create pull request**, and confirm OmniFlow validates the resulting GitHub pull request.
8. For repositories that deploy dbt, optionally add the protected [post-deployment dbt sync](docs/DBT_SYNC.md) after the production dbt command succeeds.
9. For monorepos that keep dbt and Omni on one branch, optionally enable the [breaking change hold](docs/BREAKING_CHANGE_HOLD.md) so breaking model changes cannot merge ahead of their dbt deployment.
10. Optionally enable [dbt impact analysis](docs/DBT_IMPACT.md) so a dbt-only pull request cannot remove a column or model that the Omni model still references.

After the first live run succeeds, make the OmniFlow check required in GitHub branch protection. Continue with the [installation checklist](docs/INSTALLATION.md#step-9-verify-the-installation) before treating OmniFlow as a merge gate.

The workflow always preserves `report.sarif` in the evidence artifact. Repositories with GitHub Code Security enabled can additionally set the repository variable `OMNIFLOW_UPLOAD_SARIF=true` to publish those results into GitHub code scanning.

## What OmniFlow Checks

- Omni model validation, with independent branch-to-base regression and warning policy
- Omni Content Validator results, including branch-to-base comparison for new-only policy
- Authored Models, Topics, Views, fields, and global or topic-level Relationship YAML
- Semantic lint rules for descriptions, primary keys, labels, topic owners, cardinality, and governance
- Breaking semantic changes and the dashboards, reports, and queries that reference them
- Optional, branch-aware dbt exposure metadata
- Optional post-deployment dbt metadata refresh with asynchronous job polling and full revalidation
- Optional single-branch breaking-change hold that keeps dbt and Omni merges in a safe order
- Optional dbt impact analysis that blocks dbt changes which would orphan Omni references
- Optional [AI eval](docs/AI_EVAL.md) regression check that compares Omni AI accuracy between `main` and the branch
- Experimental, default-off [query-result contracts](docs/QUERY_VALIDATION.md) for reviewed aggregate, grouping, uniqueness and NULL-rate checks. AI can propose a query locally; it never decides correctness or automatically executes the proposal.
- JSON, Markdown, SARIF, JUnit, and evidence artifacts

Core OmniFlow validation does not execute warehouse queries, store query results, write model YAML, or merge pull requests. An [AI Repair development scaffold](docs/AI_REPAIR.md) is present but is disabled, unreleased, and not supported for customer installation because Omni does not currently document a public Modeling Agent mutation API. Optional [AI eval](docs/AI_EVAL.md) starts agentic jobs for curated prompt sets and stays disabled pending non-production acceptance. Its narrow binary-score policy blocks unsupported scores and incomplete results; permissions, service capacity, query behavior, and costs must be confirmed in the selected tenant before enablement.

## How OmniFlow Works — By Example

OmniFlow adapts to the repository's architecture and the type of change being made. These scenarios show what happens at each stage.

### Scenario 1: Omni-Only Repository (No dbt)

A team manages Omni model YAML in Git and uses OmniFlow as the required PR check.

```text
Developer edits a view in an Omni branch
  → Omni creates a pull request
  → OmniFlow validates model, content, semantic diff, and downstream contracts
  → Reviewer approves → merge → Omni promotes the branch
```

OmniFlow blocks merge when:
- The model has validation errors
- Content issues are detected by the Content Validator
- A renamed or deleted field is referenced by a published dashboard or query

### Scenario 2: Monorepo with dbt (Two-Branch Architecture)

A team uses `main` for dbt and a separate `omni-main` as the Omni base branch.

```text
Developer edits Omni model YAML
  → PR targets omni-main → OmniFlow validates → merge → Omni promotes

Developer edits dbt models
  → PR targets main → dbt CI runs → merge → dbt deploys
  → omniflow dbt sync refreshes Omni → Omni picks up new warehouse metadata
```

OmniFlow's dbt sync ensures Omni stays current after deployments without manual intervention.

### Scenario 3: Monorepo with dbt (Single-Branch Architecture + Breaking Change Hold)

A team keeps dbt, Omni, and Hightouch all on `main` and enables the breaking change hold.

**Additive change — no intervention needed:**

```text
Developer adds a new column in dbt + adds the field in Omni (same PR)
  → OmniFlow validates: additive change, no breaking risk
  → Merge → dbt deploys → omniflow dbt sync → Omni refreshes
```

**Breaking change — preserve compatibility through every phase:**

```text
1. Expand: add customer_key while retaining customer_id and compatible values.
   → Review → dbt deploys → refresh and validation succeed → sync SHA recorded.
2. Migrate: update Omni definitions and consumers to customer_key.
   → Explicitly revalidate the current PR head against durable sync evidence.
   → Fresh checks and independent review → manual merge.
3. Contract: remove customer_id only after proving no consumer requires it.
   → Review downstream evidence → deploy → verify and record recovery evidence.
```

**Breaking change in separate PRs, wrong order:**

```text
PR A (dbt): adds customer_key alongside customer_id (merges, but hasn't deployed yet)
PR B (Omni): updates the view to reference customer_key

  → OmniFlow validates PR B:
    - Semantic diff: field renamed (breaking)
    - Pending detection: dbt commits exist after last sync SHA
    → BLOCKS PR B: "Breaking Omni changes while a dbt deployment is pending"
    → PR B is labeled omniflow/awaiting-deploy

  → dbt deploys → omniflow dbt sync → sync SHA recorded
  → Maintainer explicitly dispatches current-head revalidation for PR B:
    - No pending dbt changes → hold clears
    - Contract validation: no dashboards reference old name
    → Verify head and sync evidence did not change during validation
    → Attach successful readiness check to that head and clear configured label
    → Independent review and manual merge → Omni promotes against a ready warehouse
```

### Scenario 4: Two dbt-Only PRs With Breaking Changes

Two developers rename columns in dbt. Neither PR touches Omni model YAML.

```text
PR A (dbt only): renames revenue → total_revenue in models/marts/orders.sql
  → OmniFlow would normally skip (no Omni files changed)
  → With dbt impact analysis enabled:
    - Reads the committed Omni YAML from the repo
    - Finds the Omni view 'orders' still defines a 'revenue' field
    → BLOCKS: "This dbt change removes column 'revenue' but Omni still
       references it in 1 field"

Developer updates the Omni model in the correct sequence, or pairs the
change with an Omni PR that the breaking change hold will sequence.
```

Without this check, both PRs merge cleanly and the breakage only appears after dbt deploys.

### Scenario 5: Fork Pull Request

```text
External contributor opens a PR from a fork
  → OmniFlow routes: fork detected, Omni secret is withheld
  → Non-Omni fork changes: skip cleanly with exit 0
  → Fork changes touching Omni files: fail closed with exit 2
     (cannot validate without the trusted secret)
  → A maintainer can move the change to a same-repo branch for a full run
```

### When Does OmniFlow Block vs. Pass?

| Situation | Result |
| --- | --- |
| Additive model change (new field, new view) | Pass |
| Breaking change with no downstream references | Warning (pass) |
| Breaking change referenced by a dashboard | Fail (contract violation) |
| New model errors or additional occurrences (new-only policy) | Fail |
| New model warnings (new-only policy) | Warning; fail only with `fail_on_warnings: true` |
| New Content Validator issues or additional occurrences (new-only policy) | Fail |
| Unchanged, identity-matched model/content issues on the live base | Reported; nonblocking under the respective new-only policy |
| Unchanged ordinary semantic-lint debt in a verified regression PR | Reported; nonblocking. New findings follow configured severity; security-scope controls are not waived |
| Missing, malformed, or ambiguous comparison evidence | Block; never assume inherited issues |
| Eligible synchronization gap with explicit onboarding `validation.mode: advisory` | Nonblocking but **incomplete**, not a verified pass; other blockers remain enforced |
| Breaking Omni + dbt change in same PR (hold enabled) | Fail (split required) |
| Breaking Omni while dbt deployment pending (hold enabled) | Fail (held until sync) |
| dbt-only PR removing a column Omni references (impact enabled) | Fail (orphaned reference) |
| dbt-only PR adding a column | Pass |
| dbt-only PR with no Omni files and no impact check | Skip (exit 0) |
| Fork PR without Omni files | Skip (exit 0) |
| Fork PR with Omni files | Fail closed (exit 2) |

### Coverage In Both Directions

The two optional dbt policies are complements:

| Direction | Policy |
| --- | --- |
| Omni model change merging ahead of its dbt deployment | [Breaking change hold](docs/BREAKING_CHANGE_HOLD.md) |
| dbt change orphaning an existing Omni reference | [dbt impact analysis](docs/DBT_IMPACT.md) |

## End-User Flow

1. A developer works in an Omni model branch and selects **Create pull request**.
2. Omni creates the GitHub pull request.
3. GitHub Actions starts OmniFlow from the trusted base-branch workflow.
4. A credential-free preflight routes non-Omni pull requests to a successful `skipped` result before the validation process receives `OMNI_API_KEY`.
5. OmniFlow selects the changed model from trusted base-branch metadata and resolves the Omni branch from the GitHub head branch.
6. It verifies the Git/Omni snapshots, collects each new-only validator's live-base findings before its candidate findings, computes the semantic diff and lint regressions, and searches Omni for downstream references.
7. The pull request receives a minimal reviewer summary. Verified private-repository runs also generate source-location diagnostics; the updated validation workflow uploads those separately for repository readers, never into the PR comment.
8. GitHub branch protection blocks merge when required OmniFlow checks fail.
9. After review and approval, the pull request is merged. Omni's configured pull-request webhook promotes the Omni branch and publishes associated draft content.
10. When the repository also deploys dbt, the protected production job can run `omniflow dbt sync --auto` after dbt succeeds, then rerun every enabled OmniFlow check against the refreshed model.

The unreleased AI Repair scaffold is not part of this customer workflow. It must remain disabled until Omni publishes a supported Modeling Agent API contract.

The final promotion is Omni Git integration behavior, not an OmniFlow API write. See [Omni Branch Mode](https://docs.omni.co/content/develop/branch-mode) and [Git integration settings](https://docs.omni.co/integrations/git/settings).

## Installation Details

These are the required installation components. New users should follow the [full step-by-step guide](docs/INSTALLATION.md), which includes prerequisites, exact sequencing, first-run expectations, and branch protection.

Place the files relative to the **customer repository root**, not inside the model folder:

```text
repository/
├── .omniflow.yml                     # required validation policy
├── .omni/flow.json                   # trusted model identity
├── .github/workflows/omniflow.yml    # GitHub automation
└── omni/sales_model/                 # existing authored model; use its real path
```

For an existing installation, review the new policy keys and update **both** OmniFlow
`uses:` references in the workflow to the same reviewed full commit SHA that supports
them. Bootstrap required trusted files and merge the upgrade through the approved
base-branch process, then create a fresh non-production test PR. An old workflow pin
does not acquire new behavior when this repository changes.

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

Choose either an **Organization API Key or Personal Access Token (PAT)**. The operator decides which credential fits the intended governance scope; OmniFlow does not require or default to a PAT.

| Credential | When to choose it | Permission tradeoff |
| --- | --- | --- |
| Organization API Key | Centrally managed governance across the registered models and content in an Omni instance. | Has Organization Admin permissions, including capabilities beyond validation. Protect it as an administrative credential. |
| Personal Access Token (PAT) | Validation using a dedicated service user's assigned model and content access. | Inherits that user's permissions. A PAT from an admin user is also highly privileged. |

Store the chosen credential in the GitHub Actions secret **`OMNI_API_KEY`** and pass it through the existing `omni-api-key` input. The secret name stays the same for both choices. OmniFlow sends the supplied credential as a bearer token; no `authentication`/`credential_type` configuration block is needed or accepted. Content validation uses `whoami` to verify the caller's user and membership IDs when an actor is configured or personal folders are included. Shared-only validation with no actor does not require that preflight. See [Omni API authentication](https://docs.omni.co/api/authentication) and [installation instructions for either choice](docs/INSTALLATION.md#step-6-choose-and-store-an-omni-credential).

Define a rotation schedule and revoke credentials after suspected exposure or relevant access changes. Omni's manually generated Org keys and PATs do not expire automatically. Use a dedicated credential for this integration and keep deployment credentials separate. Both standard and strict redaction settings use the same minimal public diagnostic schema. Separate repository-only diagnostics can include verified authored identifiers and source locations in eligible private repositories; set `reporting.diagnostics.mode: minimal` on the trusted base to disable that tier. Credential choice does not change these output boundaries. See [Diagnostic Privacy](docs/DIAGNOSTIC_PRIVACY.md).

The token must be able to list model branches, read model YAML, validate the model, run the Content Validator, and retrieve content labels. Comparing branch-attached dashboard drafts also requires read access to their published documents' [draft metadata](https://docs.omni.co/api/documents/list-document-drafts) (Viewer or higher on those documents). No additional write permission or configuration switch is needed. The optional dbt exposures endpoint requires Connection Admin permissions according to the [Omni API reference](https://docs.omni.co/api/dbt/get-dbt-exposures). Never place credentials in repository files.

**Access is not coverage:** an Org key does not automatically scan the entire instance. OmniFlow validates the models selected by trusted routing; label filters, personal-folder settings, enabled checks, and API behavior still determine coverage. Personal folders remain excluded by default. With a PAT, leave `OMNI_USER_ID`, `omni.user_id`, and `--user-id` unset: the Content Validator's `userId` impersonation parameter is Org-key-only. See [current user-scope limitations](docs/CONFIGURATION.md#credential-choice-and-user-scope).

For an Org key with an explicit content actor, keep `omni.user_id` as the credential owner's **global user UUID**, not their membership UUID. OmniFlow verifies the pair and sends the membership UUID required by the Content Validator; no configuration rename or manual ID substitution is needed. Selecting a different actor is currently unsupported in both shared-only and personal-folder validation. Use that user's PAT with actor settings unset instead. Personal-folder validation with an Org key requires that explicit owner selection as an OmniFlow safety policy.

**GitHub authentication is separate:** the standard validation workflow uses GitHub's automatically supplied `GITHUB_TOKEN`; you do not create a GitHub PAT for it. Keep the workflow's declared permissions. Omni's existing Git integration is a separate prerequisite; the optional [deployment-state workflow](docs/BREAKING_CHANGE_HOLD.md) has an additional GitHub credential requirement.

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

For leader/follower environments sharing a model folder across `develop` and `main`,
use the opt-in [version 2 environment-target guide](docs/ENVIRONMENT_TARGETS.md).
It selects the environment by the trusted PR base and verifies the actual candidate's
authored YAML before and after checks. This enhancement is validation-only: deployment,
repair and hold-release workflows remain outside its supported scope.

### 4. Add The Required Policy

Copy the canonical [`.omniflow.yml`](.omniflow.yml) into the customer repository root. This required policy is **not** the GitHub workflow at `.github/workflows/omniflow.yml`. It enables model validation, content validation, semantic lint, semantic diff, and downstream contracts, with independent `fail_on_new_only: true` settings for model and content regression gating. Optional integrations and label filters remain commented until deliberately configured. The new model setting requires a reviewed Action revision that supports it; follow the [upgrade sequence](docs/INSTALLATION.md#step-5-merge-the-setup-into-the-base-branch) for existing installations.

The following excerpt illustrates key gates; start with the complete canonical file when installing:

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
    fail_on_new_only: true
    fail_on_warnings: false
```

Commit the policy to the protected base branch before PR validation. A file added only in the PR head is not trusted. A missing policy stops execution with configuration exit code `2`; OmniFlow does not silently fall back to built-in defaults. Environment variables and identity flags cannot substitute for the file. An explicit `--config` path (or Action `config` input) is supported, but that selected file must exist and satisfy the same trust requirements.

### 5. Protect The Base Branch

Make the OmniFlow job a required GitHub check and require pull-request approval. Confirm the Omni pull-request webhook is configured; Omni documents that it is required to keep Git and Omni branches synchronized.

GitHub may require a Team or Enterprise organization plan to enforce rulesets or classic branch protection on a private repository. If enforcement is unavailable, use OmniFlow only for controlled testing until the repository plan or visibility supports the required protections.

### 6. AI Repair Development Status

Do not install AI Repair for customer use. The repository contains a guarded development scaffold, but the documented AI Jobs API is query-oriented and does not expose the Modeling Agent's branch-editing behavior. See [AI Repair Development Scaffold](docs/AI_REPAIR.md) for the blocker and safety design.

### 7. Optional Post-Deployment dbt Sync

Repositories that deploy dbt can enable a separate protected action mode after the production dbt command. It calls Omni's documented schema refresh API, polls the job to a terminal state, and reruns all enabled checks. It is disabled by default and never runs for repositories without the `deployment.dbt_sync.enabled` policy.

The command rejects pull-request events and non-base branches. Use a dedicated `OMNIFLOW_SYNC_API_KEY` protected environment secret and restrict workflow paths to dbt source files so Omni-generated Git commits cannot create a deployment loop. See [Post-Deployment dbt Synchronization](docs/DBT_SYNC.md).

### 8. Optional Single-Branch Breaking Change Hold

Monorepos that keep dbt, Omni, and other tooling on one protected branch can enable a pull-request policy that blocks a merge which would promote breaking Omni model changes before the matching dbt deployment reaches the warehouse.

Omni promotes model YAML on merge, so a combined column rename can break production content until dbt finishes deploying. The policy detects that case and requires an expand/contract sequence that preserves old warehouse references while consumers migrate. It also holds breaking Omni-only changes when dbt deployment is pending or sync evidence is unavailable. After a protected deployment and successful `omniflow dbt sync` are recorded, fresh current-head revalidation must pass before the hold label clears; merge remains manual and subject to required checks and reviews.

It is disabled by default and never fires for additive changes. Leave it disabled for repositories that do not deploy dbt. When enabled with `action: fail`, missing, blank, unreachable, or non-ancestor sync state blocks breaking changes; `action: warn` is explicitly advisory. It prevents the unsafe merge; it does not intercept Omni's webhook. See [Breaking Change Hold](docs/BREAKING_CHANGE_HOLD.md).

### 9. Optional dbt Impact Analysis

The breaking change hold covers breaking Omni changes. This check covers the opposite direction: a dbt-only pull request that removes a warehouse column or model the committed Omni YAML still references.

Without it, two dbt pull requests can rename columns, merge cleanly because neither touches Omni files, and break dashboards once dbt deploys. The check runs on pull requests OmniFlow would otherwise skip, needs no Omni credential, and uses a committed dbt manifest when available or conservative SQL parsing when not.

It is disabled by default. See [dbt Impact Analysis](docs/DBT_IMPACT.md).

## Trust And Routing

The example uses GitHub's `pull_request_target` event but never checks out or executes proposed PR code. A credential-free preflight retrieves changed filenames through GitHub's API and reads `.omni/flow.json`, `.omniflow.yml`, and the workflow itself from the trusted base branch. Only a selected Omni model context starts the validation process with `OMNI_API_KEY`; skipped PRs never inject it. This prevents a same-repository pull request from changing `base_url`, disabling gates, enabling unsafe output, replacing the action, or redirecting the token.

Do not add steps that check out and execute pull-request code in this privileged workflow. Keep ordinary dbt tests and application builds in separate `pull_request` workflows without the Omni secret.

Automatic routing recognizes Omni source families within each registered model
directory: `model` and `relationships`, plus `*.view` (including
`*.query.view`), `*.topic`, `*.composite_topic`, and `*.relationships` at any depth.
Each accepts its native name or one `.yaml`/`.yml` export suffix at any depth
inside `model_path`.
Unrelated generic YAML and other regular files, such as Markdown or scripts, do not trigger model validation
merely because they share that directory. Workflow, policy and registry files
remain separate trusted controls.

During a selected model's candidate/base verification, Git-only generic YAML is
ignored automatically; no `files.non_omni` rule is required for ordinary tooling
YAML. A generic YAML file **returned by Omni's authored API**, however, remains
authoritative: its exact filename counterpart and bytes must match, and an exclusion
rule cannot hide it. Unsupported API files fail closed rather than disappearing
from evidence.

**Artifact identity is type-specific, not a folder-layout policy.** Within each
registered `model_path`, candidate/base verification first recognizes exact authored
paths with one supported export wrapper. Topics match by unique case-sensitive name;
the model and global relationships definitions are singletons. Views may repeat the
same filename across databases/schemas. Their canonical Omni names and authored
`catalog`, `schema`, and `table_name` qualification distinguish them. Default-catalog
omission is retained rather than guessed from an organizational folder. Query views
do not need a physical table identity.

Organizational folder differences can match through a unique filename, a canonical
view filename, or an unambiguous qualified view counterpart. Matching uses both
complete inventories: an already matched file never makes a remaining duplicate
safe to guess. Distinct semantic views are not collapsed just because they point
to the same table or have identical contents. Unknown/ambiguous identities require
correction, not a bypass or renaming valid schema-scoped views. No connection naming
setting changes are required.

All paired contents still match exactly. New/deleted files must match the respective
Omni candidate/base, not each other. Files never match across registered models.
Original Git paths remain in authorized source locations and immutable blob reads.

**Custom-name limitation:** a PR changing only an API-authored file with an
unrecognized generic YAML name is not automatically routed from its filename.
Use standard native export names or explicitly select the registered model for
validation, such as the context marker below. Filename-only routing does not
discover every possible API-authored custom name.

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

The Content Validator validates content associated with the selected model server-side, and label filtering is applied locally after content metadata lookup. Omni documents surface checks for documents with many queries unless full validation is requested. Opt in with `checks.content_validation.force_full_validation: true` in `.omniflow.yml`; the default stays `false`. The same depth is requested for branch, base, and evidence rereads. Measure runtime on representative content before enabling it; existing bounded timeouts still apply and failures never retry at a shallower depth. Reports expose `requested_validation_depth: api_default` or `full`, which describes the request, not verified exhaustive coverage. `validation_complete` describes execution and available evidence, not full-depth or whole-instance coverage. See the [Content Validator API](https://docs.omni.co/api/content-validator/validate-content).

## Evidence And Privacy

Each run writes root and minimal public summaries (diagnostic schema version 2).
`public/report.json` is always generated as the machine decision record; the other
presentation formats follow the selected `reporting.formats`:

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
  repository/           # eligible private validation runs; source metadata, not shareable
  restricted/<model_id>/
```

Keep workflow `env.OMNIFLOW_REPORT_DIR` identical to trusted `reporting.output_dir`
(default `.omniflow`). Both support a repository-relative directory with spaces.
The complete workflow examples prepare fresh outputs, use that path throughout,
and confirm uploads separately; changing only the Action pin does not install
those workflow steps. Missing decision JSON never falls back to an older folder.
See the [report-delivery upgrade checklist](docs/VALIDATION_DIAGNOSTICS.md#upgrade-report-delivery-in-an-existing-workflow).

Public outputs are constructed from fixed diagnostic codes, statuses, bounded counts, booleans, anonymous context references and a random run reference. They do not copy customer messages, names, resource IDs, paths, URLs, Git identities, raw responses or runner details. Both legacy `standard` and `strict` settings use this minimal public schema; neither re-enables detailed public output. Automation consuming the old identity-rich schema must migrate. Deployment readiness uses private, in-memory evidence instead of public artifacts. See [Diagnostic Privacy](docs/DIAGNOSTIC_PRIVACY.md).

Start with the report's **Primary blocker** and **Next action**. Safe diagnostics preserve the API operation/status, reason code, scope, retry count, parser location or configuration key when known. The tool version and full OmniFlow Action pin identify the build; they are not customer Git revisions. Check execution distinguishes completed, skipped, partial, unavailable and not-run checks, with elapsed time and blocking-stage context. See [worked diagnostic examples](docs/VALIDATION_DIAGNOSTICS.md#read-the-cause-before-changing-the-model).

**Private-repository default:** `omniflow run` now generates a separate repository-only report when GitHub.com confirms a private same-repository run and the authored Omni snapshots match the immutable Git revisions. It adds verified source files, line numbers and supported field/view/topic identifiers with corrective guidance. No source excerpts, data values, raw API messages or Omni-only dashboard identities are copied. Unsupported findings remain unmapped rather than receiving guessed locations. Public/fork/unverifiable runs and standalone report commands remain minimal; PR comments, logs, annotations and Code Security uploads stay minimal in all modes.

A mismatch can also produce up to five **observed path examples** after the private-repository, trusted model and immutable revision checks succeed. These observations explain the mismatch; they do not establish correspondence, weaken the gate, or claim contents were compared when they were not. The public report explicitly explains when this separate artifact is unavailable.

To opt out, merge this setting into the required root `.omniflow.yml` on the trusted target branch (usually `main`):

```yaml
reporting:
  diagnostics:
    mode: minimal
```

The default is `repository` when omitted. The new nested setting requires a supporting OmniFlow revision. Upgrading can therefore increase artifact detail for eligible private runs; review the [upgrade notice](docs/RELEASE_NOTES.md) and [workflow upgrade checklist](docs/VALIDATION_DIAGNOSTICS.md#upgrade-report-delivery-in-an-existing-workflow). Changing only the Action pin does not install upload steps. Reports describe runner-local generation, not successful publication. In the updated workflow, **Diagnostic publication** confirms uploads and provides current-run download links. After a confirmed upload, open `omniflow-repository-diagnostics` to inspect `report.md` (readable summary) or `report.json` (all findings). Repository readers can access this artifact; metadata itself can be confidential. Do not forward it as a shareable support report. `repository/status.json` records a fixed fallback reason when no detailed report is produced. The standard workflow uploads only freshly authorized detail and uses seven-day retention.

Restricted artifacts are deleted by default and are never uploaded by the example workflows. Explicit retention is only for an approved private support investigation on an isolated, ephemeral runner, with owner-only files. It is not a PII/PHI/payment-data sanitizer or a compliance certification. Previously generated artifacts and copied comments are not retroactively sanitized. Upload only the explicitly listed output files, never the entire `.omniflow` or `public` directory.

Raw response output cannot be enabled in CI policy. The local `content validate --unsafe-raw-output` debugging option additionally requires explicit restricted retention, is rejected in GitHub Actions, and never places raw data in its shareable report. Standalone validation history is restricted and deleted unless retention is enabled; explicit history output must stay inside the restricted directory.

The unreleased AI Repair scaffold emits minimal `repair.json` and `repair.md` during maintainer testing: status, counts and safety outcomes, without resource IDs, filenames or commit metadata. Sensitive support evidence must not be attached to public issues or copied into PR comments.

### Existing issues versus PR regressions

The required canonical `.omniflow.yml` explicitly sets `fail_on_new_only: true` under both `checks.content_validation` and `checks.model_validation`. For PR branch validation, each validator compares its branch findings with a fresh response from the same model's live base, using the same credential and applicable visibility scope. Each switch is independent: custom policies that omit it retain the strict `false` default for that validator. `OMNI_FAIL_ON_NEW_ONLY` remains content-only and does not change model policy. A missing policy file is an error, not a request for defaults.

Unchanged, matched issues remain visible but nonblocking for the validator using new-only policy. Each validator reads the live base before the candidate, using the same credential, visibility and requested depth. Evidence may be reused only within that run and identical request scope; required consistency rereads still fetch fresh evidence. Additional occurrences and worsening model severity count as new; model warnings block only when `fail_on_warnings: true`. In verified regression PRs, ordinary semantic-lint findings also compare against base findings by identity, severity and occurrence. Existing lint debt is nonblocking; new lint findings follow configured severity. Security-scope rules such as personal-folder validation restrictions are never grandfathered. A passing regression gate means **no new blocking findings were detected in the compared scope**, not clean health or proof that the PR caused every finding. Downstream contracts, deployment holds and other enabled gates remain independent.

New-only PR runs verify live authored candidate/base YAML against the immutable Git head/base before and after checks. Strict mode is the default: missing or out-of-sync evidence blocks. Initial ordinary sync mismatches receive one retry after two seconds per affected scope; final drift is not retried away. An ordinary sync failure may still produce explicitly health-only model/content observations, never new/existing PR attribution. These are sampled checks, not an atomic live-content lock; Content Validator's default API depth also does not establish exhaustive instance coverage.

Two optional policies belong in the root `.omniflow.yml`: [trusted `files.non_omni` rules](docs/CONFIGURATION.md#trusted-non-omni-file-classification) classify reviewed Git-only ancillary files without hiding Omni/API sources; [onboarding `validation.mode: advisory`](docs/CONFIGURATION.md#enforcement-mode) makes only eligible synchronization gaps nonblocking. Advisory reports use `policy_decision: advisory`, `validation_complete: false`, and unknown PR impact even when the process exits `0`; this is not verified merge safety. Malformed evidence, security/scope conflicts and independent blockers are not waived. An enabled breaking-change hold prevents advisory downgrade, even if it could not run.

Orchestrated runs without a branch, post-sync validation, and repair verification use strict health enforcement, not base-versus-itself comparison or historical suppression. Standalone no-branch content validation also defaults to strict health; explicit `--history-in` or `--fail-on-new-only` requests scoped history comparison and a missing usable baseline exits `4`. In GitHub PRs, standalone content/model new-only validation is rejected: use `omniflow run --auto` for verified Git head/base comparison. Existing content errors remain error-severity findings with `blocking: false` under new-only policy, not informational or fixed issues. See [Validation Diagnostics](docs/VALIDATION_DIAGNOSTICS.md).

Dashboard-filter errors support both legacy messages and the grouped `filter_name` / `issues` response observed in live testing. Filter identity is retained privately so different filters and additional occurrences cannot be hidden as existing issues. Empty/non-JSON API responses remain distinct incomplete-evidence failures. Updating OmniFlow does not update a consumer's pinned Action automatically: review and update its full commit SHA, then rerun the original PR.

Branch drafts can have different API identifiers from their published dashboards. OmniFlow verifies the exact active draft-to-published relationship for the selected branch before comparing findings or applying labels. An unchanged mapped draft must not turn existing errors into new ones. Missing, conflicting, stale, or changing identity evidence blocks with a sanitized evidence reason, not a claim that the PR introduced a defect. Matching never uses dashboard names or error text alone. Private comparison history uses schema version 4 and binds requested validation depth and content actor scope. Older history is not a trusted baseline: obtain a fresh baseline through an authorized standalone validation run with the same intended credential and scope; do not edit its version or identity fields to reuse it. Live PR comparisons use a fresh base and do not require a history migration.

Optional `checks.content_validation.classify_comment_only: true` classifies narrow, verified comment/format-only Git changes without executing PR code. It is explanatory only: extra live branch edits and attached drafts cannot yet be excluded reliably, so content validation still runs. See [Validation Diagnostics](docs/VALIDATION_DIAGNOSTICS.md).

`checks.content_validation.targeted` is experimental and defaults to `false`. Setting it to `true` alone still requests broad validation: the current CLI cannot independently prove dependency coverage or absence of branch-attached drafts. There is no automatic comment-only or no-impact skip. Reports show the actual content request scope and its safe fallback reason; do not interpret this setting as a completed performance optimization.

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

YAML pulls accept API `viewNames` metadata in either file-path → canonical-name or
canonical-name → file-path form. They normalize the whole response into a canonical-name →
exact file-path map in the restricted `manifest.json`; existing normalized snapshots keep
the same format. Exact-empty entries are omitted only for verified non-view files.
Validation and snapshot diffs use this normalized mapping, including scoped names and
query views; folder names are not used to invent schema prefixes.
Missing, incomplete, or ambiguous snapshot identity metadata fails closed. Re-pull older
snapshots that lack this map. Standalone diffs of raw files still accept flat or already-qualified
filenames, but unresolved nested view names require an API snapshot. Do not add a `name:`
parameter to view YAML to work around identity errors. See Omni's
[documented view-to-file resolution](https://docs.omni.co/guides/api/data-lineage-integration#resolve-topics-and-views-from-model-yaml).
API metadata processing failures identify a safe reason category and leave dependent checks
incomplete; repeatedly refreshing an unsupported response format is not a fix. See
[metadata troubleshooting](docs/TROUBLESHOOTING.md#yaml-viewnames-metadata-processing-failed)
and [unreleased compatibility notes](docs/RELEASE_NOTES.md).
Top-level view `filters` are analyzed as filter-only fields; nested measure/topic filter
expressions are not treated as separate field definitions.

Snapshot reads use only the current manifest's file inventory and verify SHA-256 hashes.
Stale files left by an earlier pull are excluded, not deleted; missing or changed snapshot
files require a fresh pull. Explicit `.view` types take precedence over names like `model`
and `relationships`.

When inheritance is present, validation retains the authored snapshot and separately pulls
Omni's `fullyResolved=true` YAML for impact analysis. Pre-sync comparisons preserve that
resolved baseline before refresh. Standalone diffs reject unresolved inheritance: use
`omniflow yaml pull --fully-resolved` for those inputs. No local inheritance rules are invented.
See the [resolved YAML contract](https://docs.omni.co/api/models/get-model-yaml).

Same-name changes between field kinds (dimension, measure, filter) follow
`contracts.fail_on.referenced_field_type_changes`. New relationship diff events verify both
endpoint roles in every relevant revision, including self-joins. Supplied Content Validator
model/branch identities must match the request; absent optional identity metadata remains
unverified rather than being treated as a contradiction.

Explicit identity flags are for local debugging. The customer workflow uses `omniflow run --auto`.

Exit codes are `0` success, intentional skip, or explicitly nonblocking onboarding advisory; `1` validation failure; `2` configuration error; `3` authentication or authorization error; `4` Omni API error; `5` security policy violation; and `6` internal error. Always read `policy_decision` and `validation_complete`: advisory exit `0` is not a verified pass.

## Documentation

- [Historical v0.4.0 architecture guide](docs/ARCHITECTURE.md) — archival context only; use the current guides below for setup and security.
- [Step-by-step installation](docs/INSTALLATION.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Testing and live validation](docs/TESTING.md)
- [Post-deployment dbt synchronization](docs/DBT_SYNC.md)
- [Breaking change hold for single-branch monorepos](docs/BREAKING_CHANGE_HOLD.md)
- [dbt impact analysis on Omni references](docs/DBT_IMPACT.md)
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
python -m pip install --upgrade "pip==26.2" "setuptools==84.0.0"
python -m pip install -e ".[dev]"
pytest --cov=omniflow
ruff check .
bandit -c pyproject.toml -r src
python scripts/simulate_alpha.py
python -m build
twine check dist/*
```

The simulation covers same-repository, fork, and multi-model routing; contract failures; strict redaction; missing branches; malicious PR metadata; successful and partial dbt exposure coverage; one-refresh-per-connection dbt synchronization; post-sync revalidation; refresh-job failure; breaking-change hold behavior for held, unheld, and warn-only pull requests; and dbt impact analysis for orphaned columns, deleted models, additive changes, warn-only mode, and the disabled default. The maintainers have also completed end-to-end live pull-request validation on a non-production model. Neither result replaces the adopter-specific live gate for actual Omni PR metadata, tenant permissions, branch mapping, Content Validator coverage, dbt exposure coverage, schema refresh and Git side effects, GitHub annotations/comments, branch-protection enforcement, or webhook promotion. Use the [testing matrix](docs/TESTING.md) to distinguish automated, live-tenant, and release evidence.

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
