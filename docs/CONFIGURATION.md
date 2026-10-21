# OmniFlow Configuration

OmniFlow separates trusted model identity, required policy, the GitHub workflow, and secrets so each has a clear owner and security boundary.

| Input | File or location | Required |
| --- | --- | --- |
| Model identity and routing | `.omni/flow.json` | Yes for `omniflow run --auto` |
| Validation and reporting policy | `.omniflow.yml` in the customer repository root | Yes; an explicit custom `--config` file may replace this path, not the file requirement |
| GitHub Actions workflow | `.github/workflows/omniflow.yml` | Yes for the supported GitHub installation; not a substitute for the policy |
| Omni credential (Org key or PAT) | `OMNI_API_KEY` environment variable or GitHub Actions secret | Yes for Omni validation |
| dbt sync API key | `OMNIFLOW_SYNC_API_KEY` protected GitHub environment secret | Only for optional post-deployment sync |
| AI repair API key | `OMNIFLOW_REPAIR_API_KEY` protected GitHub environment secret | Only for optional AI Repair |
| Pull-request branch | GitHub event context | Discovered automatically |

## Credential Choice And User Scope

Choose an **Organization API Key or Personal Access Token (PAT)** for validation, and independently choose the credential for optional dbt sync. Neither type is selected by default. Supply the chosen value through the existing secret/input; do not put it in policy. Org keys have Organization Admin permissions; PATs inherit their user's permissions, including admin privileges when applicable. See [credential tradeoffs](../README.md#2-add-the-api-secret).

There is no `authentication` or `credential_type` configuration setting. Unknown policy keys are rejected. Only personal-folder validation performs a scope-specific [`whoami` identity lookup](https://docs.omni.co/api/who-am-i/get-current-identity-and-permissions); shared-only validation is unchanged. `omniflow doctor` checks model access and Git settings, not credential type or comprehensive content coverage.

- `omni.user_id`, `OMNI_USER_ID`, and `--user-id` set the Content Validator's `userId` impersonation parameter. **Leave them unset with a PAT**: Omni returns HTTP 403 when a user-scoped key supplies this parameter. An Org key may use it to act on behalf of an explicitly chosen user. See the [Content Validator contract](https://docs.omni.co/api/content-validator/validate-content).
- `omni.include_personal_folders` / `OMNI_INCLUDE_PERSONAL_FOLDERS` defaults to `false`. Including personal folders does not enumerate all users' private content. The semantic-lint rule `forbid_personal_folder_validation_scope` also defaults to `error`; review that policy explicitly before opting in.
- For personal-folder metadata, the [content catalog's `creatorId`](https://docs.omni.co/api/content/retrieve-content) is an **organization membership UUID**, not the Content Validator's user UUID. OmniFlow resolves the pair from `whoami`; never replace `omni.user_id` with a membership UUID. With a PAT, omit `user_id`: the verified caller's membership is used. With an Org key, explicitly set `omni.user_id` to the credential owner's user UUID. Personal-folder scope without an explicit Org-key actor fails before validation, including when labels are not configured.
- **Current limitation:** `whoami` identifies the credential owner, not another impersonated user; the documented user APIs do not provide the required membership mapping. A different Org-key actor's personal-folder scope therefore fails with `content_personal_actor_unresolved`. Use that user's PAT without `user_id`, or select the Org credential owner explicitly. This restriction does not remove Org-key impersonation for shared-only validation. Missing or malformed identity, denied metadata, and incomplete pagination fail rather than falling back to a different or smaller scope. Identity is cached only within the client, bound to the instance and credential; content catalogs are not cached.

Credential breadth does not change the registered model list or enabled checks. Empty results and completed checks do not establish whole-instance coverage. Review the known-content acceptance checks in [Testing](TESTING.md#credential-and-content-scope-acceptance).

## Trusted Model Identity

`.omni/flow.json` is read from the protected base branch during pull-request validation.

```json
{
  "version": 1,
  "models": [
    {
      "base_url": "https://company.omniapp.co",
      "model_id": "00000000-0000-0000-0000-000000000000",
      "model_path": "omni/sales_model",
      "base_branch": "main",
      "git_provider": "github",
      "web_url": "https://github.com/company/analytics"
    }
  ]
}
```

Required model fields are `base_url`, `model_id`, and `model_path`. `base_branch`, `git_provider`, and `web_url` are recommended because `omniflow doctor --auto` can compare them with Omni's Git configuration when the API key permits that read. `base_branch` is required when post-deployment dbt sync is enabled.

Rules:

- `version: 1` uses the single-target rules shown above; IDs and paths must be unique.
- Opt-in `version: 2` supports branch-scoped environment targets. See the
  [complete schema, workflow and adoption boundaries](ENVIRONMENT_TARGETS.md).
- Paths must stay inside the repository.
- `base_url` must be an HTTPS Omni origin in GitHub Actions.
- Secret-like keys are rejected.
- A pull-request marker cannot provide or override `base_url`.

## Required Policy

Copy the complete canonical [`.omniflow.yml`](../.omniflow.yml) into the customer repository root. It is the shipped policy template, not `.github/workflows/omniflow.yml`. The template enables the main safety checks and explicitly selects regression gating for both model and content validation; optional integrations and label filters remain commented. This excerpt shows key settings, not the complete installation template:

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

security:
  redaction_level: standard
```

The policy file is required and must not contain credentials. In a privileged GitHub pull-request run, OmniFlow reads policy from the trusted base branch rather than from proposed pull-request changes. Bootstrap it through the approved setup process before validating Omni PRs; a copy that exists only in the PR head is not trusted.

A missing selected file produces configuration exit code `2` with safe, actionable setup guidance. There is no silent missing-file fallback. Environment variables and CLI identity overrides are applied to an existing policy; they cannot replace it. Explicit custom paths remain supported through `--config` or the Action's `config` input, subject to the same trusted-base requirements. Defaults documented below apply to omitted settings inside a valid policy, not to an absent policy file.

The file must contain a non-empty YAML mapping; a blank file or comments alone do not satisfy setup. In customer PR workflows, keep `config` repository-relative so it is read from the trusted base. Absolute paths are intended only for explicitly trusted local files, not for loading proposed PR policy.

## Trusted Non-Omni File Classification

Automatic Git routing recognizes these Omni families, case-sensitively:

- `model` and `relationships` at the root of the registered `model_path`.
- `*.view`, including `*.query.view`, and `*.topic` at any depth.
- Existing `*.composite_topic` and typed `*.relationships` compatibility at any depth.
- Each native name above may have one `.yaml` or `.yml` export suffix.

Generic YAML such as `metadata/config.yaml`, and unrelated regular files such as Markdown or scripts, do not
trigger model validation solely by being inside a model directory. Workflow,
policy and model-registry files remain separate trusted controls. Candidate/base
verification also ignores Git-only generic YAML automatically, so no ancillary
rule is needed for ordinary tooling files.

The API is not filtered by that Git routing rule. When Omni returns a generic
YAML file in its authored inventory, the corresponding Git file is retained and
verified exactly, including its content. Unsupported API formats, missing source
counterparts, ambiguous aliases, unsafe paths and real byte mismatches still
cannot produce a verified pass.

**Limitation:** filename-only routing cannot discover a custom generic API-authored
file when that file is the PR's only change. Prefer standard native export names
or explicitly select the registered model through the supported
[context marker](../README.md#trust-and-routing) or explicit validation flow.
Standalone health validation is not a substitute for verified PR provenance.

`files.non_omni` remains an optional, reviewed classification of ancillary Git
files—not a validator ignore list or a way to bypass other checks. Explicit
rules cannot hide any known or API-returned Omni source.

```yaml
files:
  non_omni:
    - tag: build_metadata
      pattern: omni/sales_model/metadata/*.yaml
      reason: Build metadata not authored by Omni
```

Patterns start at the **repository root**, not at `model_path`. Each rule requires
exactly `tag`, `pattern`, and `reason`; no other keys are supported.

- At most 100 rules. Tags are unique lowercase identifiers matching `[a-z][a-z0-9_-]{0,63}`.
- Reasons are trimmed, printable, nonempty single-line text, at most 240 characters.
- Patterns are case-sensitive relative POSIX paths, at most 1,024 characters.
  Literal paths and `*` within a path component are supported. One complete `**`
  component may match zero or more directories, for example
  `omni/sales_model/metadata/**/*.yaml`.
- A glob needs a literal first component and a bounded final filename pattern:
  `*.yaml`, `omni/**`, and `omni/*` are rejected. No absolute paths, `.`/`..`,
  empty components, trailing slash, backslashes, `?`, bracket/brace patterns,
  `!`, `$`, or `:`. Spaces in literal directory names remain significant.
- Duplicate tags/patterns and overlapping matches are errors, not precedence rules.
  Known Omni names (`model`, `relationships`, typed views/topics/relationships,
  including supported YAML wrappers) and control files/directories
  (`.omniflow.yml`, `.omni`, `.github`, `.git`, `.omniflow`) cannot be excluded.
- Both base and candidate inventories are checked. Any rule matching an API-returned
  authored source, including a known filename alias, fails closed. A Git-only
  match is excluded from snapshot correspondence only after these checks.

Merge rules into the trusted base through review before relying on them. Proposed
PR-head policy cannot hide its own changes. Public diagnostics expose bounded
counts and fixed conflict reasons, never the rule's free text or matched paths.

## Enforcement Mode

`validation.mode` defaults to `strict`. Normal regression policy already permits
matched existing debt; changing enforcement mode is not needed for that.

```yaml
validation:
  mode: strict  # optional onboarding choice: advisory
```

`advisory` is an explicit onboarding choice for ordinary candidate synchronization
gaps only: `file_inventory_mismatch`, `content_mismatch`,
`missing_candidate_branch`, or `changed_during_validation`. When eligible and no
other hard blocker exists, the run exits `0` with `policy_decision: advisory`,
`decision_status: incomplete`, and `validation_complete: false`. Available
model/content observations are marked `health_only: true`; PR regression status
is `unknown`, and new/existing/resolved attribution is withheld. This is
**nonblocking incomplete evidence, not verified merge safety**. Checks marked
`not_run` remain unverified.

Security violations, invalid/ambiguous sources, non-Omni rule conflicts,
authentication/API failures, malformed evidence and independent policy blockers
are not made advisory. An enabled breaking-change hold prevents the downgrade,
even if the hold could not run; the configured personal-folder security restriction
is checked before collecting health evidence. The run evidence's
`validation_status: incomplete` is not `passed`. Do not use this mode as a production readiness signal.
Keep `strict` for required merge gating; return to it after onboarding.

Initial inventory/content/missing-branch sync failures get one retry after two
seconds per affected scope. Final verification never retries an observed change
away. A retry is not a synchronization write, and advisory mode does not repair
the branch.

## Contract Gates

`contracts.enabled` defaults to `true`. The following gates also default to `true`:

| Setting under `contracts.fail_on` | Behavior |
| --- | --- |
| `deleted_referenced_fields` | Fails when downstream content references a deleted field. |
| `renamed_referenced_fields` | Fails when downstream content references a field that appears renamed. |
| `referenced_field_type_changes` | Fails when a referenced field changes type or kind (dimension, measure, filter). |
| `referenced_join_cardinality_changes` | Fails when a referenced relationship changes cardinality. |
| `coverage_gaps` | Fails when OmniFlow cannot complete a required dependency search, including incomplete relationship endpoint roles or contradictory supplied API response identity. |

An unreferenced breaking change remains visible as risk but does not fail the default contract gate.

## Validation Checks

### Content Validation

- `enabled`: defaults to `true`.
- `fail_on_new_only`: the canonical `.omniflow.yml` explicitly sets `true` to block new issues while reporting unchanged, identity-matched baseline issues. The underlying default for a custom policy that omits this key remains `false` (all current issues block); an explicit `false` remains supported. A passing regression gate is not a clean bill of health for existing content, and malformed/incomplete comparison evidence still blocks.
- `classify_comment_only`: defaults to `false`. When enabled, classify immutable PR inputs conservatively and report the result. This does not skip content validation or override failures; live content/draft correspondence is not established by Git equality.
- `force_full_validation`: defaults to `false`. Set `true` to request full-depth API validation for the branch, base, and evidence rereads. Measure runtime before adoption. Existing `omni.timeout` and workflow limits still apply; an error or timeout never triggers a shallower retry.
- `targeted`: experimental, defaults to `false`. The current CLI always falls back to broad content validation even when `true`, because it cannot independently prove dependency coverage and absence of branch drafts. The staged targeted implementation additionally requires identical before/after selectors, bounded requests and comparable complete returned identities; unknown/global/relationship changes or incomplete, disappearing or conflicting responses require fresh broad base/candidate validation. This is not an enabled performance optimization or a comment-only/no-impact skip. Targeted partial results cannot become whole-model history.
- `labels`: optional local filter applied after Omni validates content for the selected model server-side.
- `OMNI_FAIL_ON_NEW_ONLY` remains an override for content validation only; it never changes model-validation policy.

Omni documents that large documents may receive surface checks unless `force_full_validation` is requested. Reports expose the safe enum `requested_validation_depth` (`api_default` or `full`), including content failures. This records request intent, not observed exhaustive coverage: `validation_complete` describes execution and available evidence, not full-depth or whole-instance coverage. See the [Content Validator API](https://docs.omni.co/api/content-validator/validate-content).

Standalone content history is scoped to the requested depth. New history uses schema 3 so older clients cannot unknowingly reuse full-depth history. Existing schema-2 history can be reused only at `api_default`; changing depth invalidates the comparison baseline rather than treating it as clean. Live PR comparisons always fetch a fresh base.

### Model Validation

- `enabled`: defaults to `true`.
- `fail_on_new_only`: the canonical `.omniflow.yml` explicitly sets `true`. A custom policy that omits this key defaults to `false`; explicit `false` enforces all current model errors. This setting is independent of content validation and has no shared environment override.
- `fail_on_warnings`: defaults to `false`, so model warnings remain advisory. With `true`, warnings block according to the selected comparison mode: new warnings in new-only PR mode, or all current warnings in strict-health mode.

### Shared regression and health boundaries

In PR branch validation, each enabled new-only validator fetches the live-base response before the candidate response for the same model. The client credential and applicable user/visibility scope stay the same. Identical request evidence can be reused only within the current run; consistency rereads bypass that reuse. Model matching retains issue identity, severity, and occurrence counts: an additional occurrence or warning-to-error change cannot be hidden as an existing issue. Content matching also preserves occurrence and supported document/query/filter identity.

New-only PR validation requires immutable Git base/head revisions and verifies live authored model YAML against those revisions before and after the checks: the Omni candidate must match the PR head, and the live base must match the trusted Git base. Unavailable, unverifiable, or out-of-sync snapshots block in strict mode; the narrow onboarding advisory exception above still reports incomplete evidence, never a verified pass. A missing repository or identity override cannot replace this evidence. These before/after observations are sampled consistency checks, not an atomic lock over live models, content, or drafts.

Known native `.yaml`/`.yml` filename wrappers are matched to their authored API identities without changing paths or content. Git-only generic YAML is ignored automatically; generic YAML present in Omni's authored API inventory remains included and must match exactly. Reviewed `files.non_omni` rules cannot hide those API sources or recognized Omni families. Duplicate aliases remain hard failures. Extra/missing authored source files and byte differences prevent a verified comparison. See [candidate verification troubleshooting](TROUBLESHOOTING.md#candidate-verification-git-and-omni-snapshots-differ) for safe counts, reason categories and upgrade guidance.

Matched existing findings remain visible without blocking that new-only validator; new model errors and new content issues block, while model warnings follow `fail_on_warnings`. Missing, malformed, or ambiguous evidence cannot prove that a finding already existed or that a comparison passed. A pass is not proof of a clean model/content inventory, complete instance health, or causal attribution to the PR. Other configured checks remain independent gates.

Content validation uses the same configured depth for both requests (API default unless opted into full depth). Matching request scope does not guarantee exhaustive content coverage, particularly for large or changing content inventories; record any API coverage limits separately from a passing regression decision.

Orchestrated runs without a branch, post-sync validation, and repair verification enforce strict health for both validators, even when their policy switches are `true`. They must not compare the base against itself or reuse historical issues to obtain a pass. Standalone `content validate` retains its separately selected and scoped history behavior; see [Validation Diagnostics](VALIDATION_DIAGNOSTICS.md).

Without a branch, standalone `content validate` also defaults to strict health, even when the canonical content policy is new-only. Explicit `--history-in` or `--fail-on-new-only` requests history comparison; a missing usable baseline exits `4`, not a successful empty comparison. Inside a GitHub PR, standalone content/model new-only validation is rejected with configuration exit `2`: use `omniflow run --auto`, which verifies both immutable Git revisions and the live snapshots.

Matched existing content issues keep their original error severity with `state: existing` and `blocking: false` under new-only policy. Nonblocking does not mean the underlying issue has become informational or has been fixed.

### Semantic Lint In Regression PRs

When a verified PR uses at least one enabled model/content new-only policy,
semantic lint also compares the candidate against the base graph. Ordinary
findings match by rule, source/entity identity, severity and occurrence count.
Matched debt remains visible with `state: existing`, `blocking: false`; new,
additional or worsened findings follow their configured rule severity.
`forbid_personal_folder_validation_scope` is an execution-scope security rule
and never receives an existing-debt waiver. Downstream contracts and configured
deployment holds remain independent gates. Non-PR and all-issues health checks
retain strict lint behavior.

The model `fail_on_new_only` key is a development change requiring a reviewed supporting Action revision, not a released-version promise. Older versions reject unknown policy keys. Use the [trusted-base upgrade sequence](INSTALLATION.md#step-5-merge-the-setup-into-the-base-branch); do not apply the new template blindly to an incompatible old pin.

### dbt Exposures

- `enabled`: defaults to `false`.
- `fail_on_unavailable`: defaults to `false`.

The Omni dbt exposures endpoint has its own permission requirement. Reports distinguish dashboard records analyzed, mapped exposures, unmapped dashboards, and `available`, `partial`, or `unavailable` coverage. The endpoint covers published shared dashboards; private dashboards and workbooks are outside its documented scope. This check supplements downstream contract analysis; it does not replace Content Validator reference searches or run dbt commands.

Mapped records must include a recognized dashboard/exposure identity and an explicit supported dependency list. Error-shaped records, missing dependency lists, or malformed dependency identities are unavailable evidence (a warning by default; blocking with `fail_on_unavailable: true`), not proof of zero dependencies. An empty inventory or explicit `dashboard_identifier` plus `exposure: null` retains its documented empty/unmapped meaning.

## Post-Deployment dbt Sync

This deployment stage is independent from `checks.dbt_exposures`. It is disabled by default and is not part of `omniflow run --auto`.

```yaml
deployment:
  dbt_sync:
    enabled: true
    refresh_mode: hard
    poll_interval_seconds: 5
    timeout_seconds: 900
    post_sync_validation: true
```

| Setting | Default | Allowed range or behavior |
| --- | --- | --- |
| `enabled` | `false` | Must be enabled in trusted base-branch policy. |
| `refresh_mode` | `hard` | `hard` removes dropped objects; `soft` is additive only. |
| `poll_interval_seconds` | `5` | `2` through `30`; Omni recommends 2-5 seconds. |
| `timeout_seconds` | `900` | `30` through `3600`. |
| `post_sync_validation` | `true` | Reruns all enabled checks after refresh completes. |

The sync token is read only from `OMNIFLOW_SYNC_API_KEY`. Choose a separate deployment Organization API Key or PAT. For a PAT, use Modeler permission for a connection containing one shared model or Connection Admin permission for a connection containing multiple shared models. An Org key carries Organization Admin privileges. When `post_sync_validation` is enabled, the deployment credential also needs access required by those checks, and the same [user-scope limitations](#credential-choice-and-user-scope) apply. Pull-request validation and AI repair action modes never receive this token. See the [schema refresh permission contract](https://docs.omni.co/api/models/refresh-schema).

`omniflow dbt sync --auto` selects all trusted model records whose `base_branch` exactly matches the current deployment branch. In GitHub Actions it accepts only `push` and `workflow_dispatch`, rejects tags and pull requests, and writes no raw response or query-result data. See [Post-Deployment dbt Synchronization](DBT_SYNC.md).

## Breaking Change Hold

This optional pull-request policy lets a monorepo keep dbt and Omni model YAML on one protected branch. It blocks a merge that would promote breaking Omni changes before the matching dbt deployment reaches the warehouse. It is disabled by default; enable it only for repositories that deploy dbt. Once enabled, missing deployment evidence can trigger the hold even if the pull request changes no dbt files.

```yaml
deployment:
  breaking_change_hold:
    enabled: true
    action: fail
    dbt_paths:
      - models
      - seeds
      - snapshots
      - macros
    pending_label: omniflow/awaiting-deploy
```

| Setting | Default | Allowed range or behavior |
| --- | --- | --- |
| `enabled` | `false` | Must be enabled in trusted base-branch policy. |
| `action` | `fail` | `fail` blocks the merge through the required check; `warn` reports only. |
| `dbt_paths` | `models`, `seeds`, `snapshots`, `macros` | Relative repository paths, maximum 50. Absolute and `..` paths are rejected. |
| `pending_label` | `omniflow/awaiting-deploy` | One line, no commas, 50 characters or fewer. |

The policy only evaluates changes the semantic diff marks `breaking`: deleted fields, renamed fields, field type changes, deleted relationships, and relationship cardinality changes. It fires in three situations:

- The pull request contains breaking Omni changes and also modifies a configured dbt path.
- The pull request contains breaking Omni changes while dbt sources changed on the base branch after the commit recorded in `OMNIFLOW_LAST_SYNC_SHA`.
- The pull request contains breaking Omni changes, has no same-pull-request dbt overlap, and deployment readiness cannot be established because the recorded sync state is missing, blank, unreachable, or not an ancestor of the trusted base.

Same-pull-request detection takes precedence. Otherwise, pending detection requires a verified successful sync commit and complete trusted-base history (`fetch-depth: 0`). Unavailable evidence produces `breaking_change_sync_state_unavailable`: an error that blocks validation under `action: fail`, or an advisory warning under `action: warn`. Warning mode does not establish deployment readiness, and other checks can still fail. A malformed SHA is rejected separately as a security-policy error.

Before enabling the failing gate, complete a protected deployment and successful `omniflow dbt sync`, then record and verify that deployment's commit as `OMNIFLOW_LAST_SYNC_SHA`. Fetching history alone cannot replace missing or invalid deployment state. Keep `dbt_paths` consistent with the `push.paths` filter on the deployment workflow. See [Breaking Change Hold](BREAKING_CHANGE_HOLD.md) and [sync-state recovery](TROUBLESHOOTING.md#breaking_change_sync_state_unavailable).

## dbt Impact Analysis

This optional check covers the opposite direction from the breaking change hold: a dbt-only pull request that removes a warehouse column or model the committed Omni YAML still references. It runs only on pull requests that have dbt-path changes and no Omni model changes, needs no Omni credential, and never queries the warehouse.

```yaml
checks:
  dbt_impact:
    enabled: true
    manifest_path: target/manifest.json
    fail_on_orphaned_references: true
    omni_yaml_paths:
      - omni/my_model
    table_mapping:
      - dbt_model: orders_v2
        sql_table_name: analytics.marts.orders
```

| Setting | Default | Allowed range or behavior |
| --- | --- | --- |
| `enabled` | `false` | Must be enabled in trusted base-branch policy. |
| `manifest_path` | none | Relative path to a committed dbt manifest. Falls back to SQL heuristics when absent. |
| `fail_on_orphaned_references` | `true` | `true` blocks the merge; `false` reports a warning only. |
| `omni_yaml_paths` | `model_path` values from `.omni/flow.json` | Relative directories holding Omni model YAML, maximum 50. |
| `table_mapping` | none | `dbt_model` plus `sql_table_name` or `omni_view` overrides, maximum 500 entries. |

The check reuses `deployment.breaking_change_hold.dbt_paths` to identify dbt source files, so both policies stay consistent. Manifest mode is precise; SQL heuristic mode is conservative and reports nothing for `SELECT *` or unparseable statements. Column matching is case-insensitive and uses word boundaries so removing `customer` never flags a field referencing `customer_id`. See [dbt Impact Analysis](DBT_IMPACT.md).

## AI Eval

This optional controlled-alpha check compares configured Omni prompt sets against `main` and the pull request's Omni branch. It supports binary scores only (`1` pass, `0` fail); unsupported numeric scores and incomplete comparisons always fail operationally. It starts agentic jobs and stays disabled pending tenant acceptance. Read [AI Eval](AI_EVAL.md) in full before enabling it.

```yaml
checks:
  ai_eval:
    enabled: true
    fail_on_regression: true
    poll_interval_seconds: 10
    timeout_seconds: 900
    scoring_grace_seconds: 180
    prompt_sets:
      - id: 00000000-0000-0000-0000-000000000000  # replace with your own prompt set ID
        label: Core revenue prompts
```

| Setting | Default | Allowed range or behavior |
| --- | --- | --- |
| `enabled` | `false` | Must be enabled in trusted base-branch policy. |
| `prompt_sets` | `[]` | `{id, label, model_id}` entries for existing sets, maximum 20. Only `id` is required; optional model routing is verified against Omni. Labels stay restricted. |
| `fail_on_regression` | `true` | `true` blocks the merge; `false` reports regressions as warnings only. |
| `poll_interval_seconds` | `10` | Bounded 2-30 seconds. |
| `timeout_seconds` | `900` | Bounded 30-3600 seconds. |
| `scoring_grace_seconds` | `180` | Bounded 0-600 seconds. |

Uses `OMNI_API_KEY`; verify eval permissions and service-identity capacity before enabling. Public samples contain opaque prompt IDs only, and sample limits never change regression gating. Operational failures cannot be downgraded by `fail_on_regression`. See [AI Eval](AI_EVAL.md).

## Semantic Lint

`checks.semantic_lint.enabled` defaults to `true`. Every rule accepts `off`, `info`, `warn`, or `error`. Only `error` is a blocking lint severity.

Supported rules:

- `require_field_descriptions`
- `require_measure_descriptions`
- `require_primary_keys`
- `require_topic_labels`
- `forbid_many_to_many_without_comment`
- `block_deleted_fields`
- `warn_field_type_change`
- `warn_measure_aggregation_change`
- `warn_relationship_cardinality_change`
- `require_owner_metadata`
- `forbid_personal_folder_validation_scope`

All rules default to `warn` except `forbid_personal_folder_validation_scope`, which defaults to `error`.

## Reporting

```yaml
reporting:
  formats: [json, markdown, sarif, junit]
  output_dir: .omniflow
  diagnostics:
    mode: repository  # default; set minimal to opt out
```

The output directory must be a relative path inside the repository. Supported presentation format names are `json`, `markdown` or `md`, `sarif`, and `junit` or `xml`. Privacy-safe `public/report.json` is always generated for automation, even with Markdown-only or SARIF-only presentation; `formats` cannot disable that decision record.

Every shipped workflow declares `env.OMNIFLOW_REPORT_DIR: .omniflow`. For a custom directory, change **both** that trusted workflow value and `reporting.output_dir` to the same path. For example, `ci results/omniflow` supports spaces. Do not use absolute paths, traversal, symlinks, protected metadata directories or upload globs. Preparation, upload, SARIF, summaries and the installed publisher consume that one explicit path. A mismatch fails with a configuration error; delivery never searches other directories or substitutes an older report.

`diagnostics.mode` accepts the literal values `repository` (default) and `minimal`; environment substitution and unknown nested keys are rejected. Commit an opt-out to the trusted target branch. Repository detail is generated only by `omniflow run` with verified private same-repository GitHub.com eligibility, a trusted immutable policy/registry, and matching authored Git/Omni snapshots. It does not change pass/fail decisions. Forks, public/unknown repositories, stale revisions, standalone commands and unsupported origins remain minimal. Missing detail never waives a validator finding.

Repository output is separate under `output_dir/repository/` (JSON, Markdown, SARIF, JUnit plus a safe status file). It always includes those four formats for consistent troubleshooting, independently of public `formats`. Only verified source metadata is added; no raw API prose, values, SQL or Omni-only document identities. Minimal public/root reports, PR comments, logs and annotations remain privacy-bounded. Updated validation workflow examples upload the explicitly listed repository files only when the Action returns `repository-diagnostics-ready: true`, with seven-day retention. Readiness and `status: generated` describe local generation, not publication; the post-upload **Diagnostic publication** receipt confirms delivery separately. Optional dbt/held-readiness workflows finalize their operation before authorizing public uploads; a failed final operation cannot publish an intermediate pass. See [Diagnostic Privacy](DIAGNOSTIC_PRIVACY.md) and the [workflow upgrade checklist](VALIDATION_DIAGNOSTICS.md#upgrade-report-delivery-in-an-existing-workflow).

## Security Settings

```yaml
security:
  redact_logs: true
  allow_raw_response_output: false
  max_report_samples: 20
  redact_document_names: false
  redaction_level: standard
  retain_restricted_artifacts: false
```

- `redaction_level` accepts `standard` or `strict`.
- Both values now use the minimal version-2 public diagnostic allowlist: fixed codes, counts and statuses, not customer text or identifiers. These legacy redaction settings do not loosen public output. See [Diagnostic Privacy](DIAGNOSTIC_PRIVACY.md) for the schema migration and restricted support boundary.
- `allow_raw_response_output` cannot be enabled through policy.
- Restricted artifacts are deleted by default. Opt-in retention writes owner-only files but should be used only on isolated, ephemeral runners. The validation examples upload minimal public artifacts and, when freshly authorized, a separate repository-metadata artifact; they never upload raw restricted artifacts.
- Unknown configuration keys fail closed so misspelled security or policy settings cannot be silently ignored.
- Personal folders are excluded by default.

## Unreleased AI Repair Scaffold

AI Repair is disabled by default, is not part of `omniflow run --auto`, and is not supported for customer installation until Omni publishes a Modeling Agent API contract.

```yaml
repairs:
  ai:
    enabled: true
    allow_query_execution: true
    max_changed_files: 3
    max_changed_lines: 200
    poll_timeout_seconds: 300
```

| Setting | Default | Allowed range or behavior |
| --- | --- | --- |
| `enabled` | `false` | Must be explicitly enabled on the trusted base branch. |
| `allow_query_execution` | `false` | Must be `true` because Omni's AI Jobs API may execute queries and has no documented no-query switch. |
| `max_changed_files` | `3` | `1` through `20`; additions and deletions are rejected regardless. |
| `max_changed_lines` | `200` | `1` through `2000`, counting added and removed lines. |
| `poll_timeout_seconds` | `300` | `30` through `900`; timeout triggers AI-job cancellation before rollback. |

The repair token is read only from `OMNIFLOW_REPAIR_API_KEY`; it cannot be placed in policy. Use the separate protected workflow in [the AI Repair guide](AI_REPAIR.md). The normal validation action never receives this token.

## Local Debugging Overrides

Explicit identity flags such as `--base-url`, `--model-id`, `--model-path`, `--branch-name`, and `--branch-id` are intended for local debugging. The customer workflow should use:

```bash
omniflow run --auto --config .omniflow.yml
```

Run from a trusted checkout containing the required policy. To use a different reviewed file, pass `--config path/to/policy.yml`; the file must exist. Identity flags and environment variables do not bypass this requirement.

The validation credential is read from `OMNI_API_KEY`; dbt sync uses `OMNIFLOW_SYNC_API_KEY`, and the unsupported repair scaffold uses `OMNIFLOW_REPAIR_API_KEY`. Never add credentials to either configuration file.

For an explicit local dbt sync test, `--base-url`, `--model-id`, and `--base-branch` are all required. The supported protected workflow uses `omniflow dbt sync --auto` and trusted `.omni/flow.json` metadata.

`omniflow repair ai --auto` is intentionally not a local debug shortcut. It additionally requires a trusted same-repository `pull_request_target` label event, protected workflow, GitHub token, and `OMNIFLOW_REPAIR_API_KEY`.
