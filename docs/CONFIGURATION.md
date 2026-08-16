# OmniFlow Configuration

OmniFlow separates trusted model identity, optional policy, and secrets so each has a clear owner and security boundary.

| Input | File or location | Required |
| --- | --- | --- |
| Model identity and routing | `.omni/flow.json` | Yes for `omniflow run --auto` |
| Validation and reporting policy | `.omniflow.yml` | No |
| Omni API key | `OMNI_API_KEY` environment variable or GitHub Actions secret | Yes for Omni validation |
| dbt sync API key | `OMNIFLOW_SYNC_API_KEY` protected GitHub environment secret | Only for optional post-deployment sync |
| AI repair API key | `OMNIFLOW_REPAIR_API_KEY` protected GitHub environment secret | Only for optional AI Repair |
| Pull-request branch | GitHub event context | Discovered automatically |

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

- `version` must be `1`.
- Model IDs and model paths must be unique.
- Paths must stay inside the repository.
- `base_url` must be an HTTPS Omni origin in GitHub Actions.
- Secret-like keys are rejected.
- A pull-request marker cannot provide or override `base_url`.

## Optional Policy

Start from [the complete example](../.omniflow.example.yml). A minimal recommended policy is:

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

security:
  redaction_level: standard
```

The policy file is optional and must not contain credentials. In a privileged GitHub pull-request run, OmniFlow reads policy from the trusted base branch rather than from proposed pull-request changes.

## Contract Gates

`contracts.enabled` defaults to `true`. The following gates also default to `true`:

| Setting under `contracts.fail_on` | Behavior |
| --- | --- |
| `deleted_referenced_fields` | Fails when downstream content references a deleted field. |
| `renamed_referenced_fields` | Fails when downstream content references a field that appears renamed. |
| `referenced_field_type_changes` | Fails when a referenced field changes type. |
| `referenced_join_cardinality_changes` | Fails when a referenced relationship changes cardinality. |
| `coverage_gaps` | Fails when OmniFlow cannot complete a required dependency search. |

An unreferenced breaking change remains visible as risk but does not fail the default contract gate.

## Validation Checks

### Content Validation

- `enabled`: defaults to `true`.
- `fail_on_new_only`: defaults to `false`; the checked-in example recommends `true` for gradual adoption.
- `labels`: optional local filter applied after Omni validates the full model server-side.

### Model Validation

- `enabled`: defaults to `true`.
- `fail_on_warnings`: defaults to `false`. Model errors always fail.

### dbt Exposures

- `enabled`: defaults to `false`.
- `fail_on_unavailable`: defaults to `false`.

The Omni dbt exposures endpoint has its own permission requirement. Reports distinguish dashboard records analyzed, mapped exposures, unmapped dashboards, and `available`, `partial`, or `unavailable` coverage. The endpoint covers published shared dashboards; private dashboards and workbooks are outside its documented scope. This check supplements downstream contract analysis; it does not replace Content Validator reference searches or run dbt commands.

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

The sync token is read only from `OMNIFLOW_SYNC_API_KEY`. It must be a dedicated PAT with Modeler permission for a connection containing one shared model or Connection Admin permission for a connection containing multiple shared models. When `post_sync_validation` is enabled, that same dedicated deployment user also needs the read permissions required by the enabled checks. Pull-request validation and AI repair action modes never receive this token.

`omniflow dbt sync --auto` selects all trusted model records whose `base_branch` exactly matches the current deployment branch. In GitHub Actions it accepts only `push` and `workflow_dispatch`, rejects tags and pull requests, and writes no raw response or query-result data. See [Post-Deployment dbt Synchronization](DBT_SYNC.md).

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
```

The output directory must be a relative path inside the repository. Supported format names are `json`, `markdown` or `md`, `sarif`, and `junit` or `xml`.

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
- `strict` additionally removes content names, query names, owners, labels, and free-text messages from public output.
- `allow_raw_response_output` cannot be enabled through policy.
- Restricted artifacts are deleted by default. Opt-in retention writes owner-only files but should be used only on isolated, ephemeral runners. The official example uploads only redacted public artifacts.
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

The API key is always read from `OMNI_API_KEY`. Never add it to either configuration file.

For an explicit local dbt sync test, `--base-url`, `--model-id`, and `--base-branch` are all required. The supported protected workflow uses `omniflow dbt sync --auto` and trusted `.omni/flow.json` metadata.

`omniflow repair ai --auto` is intentionally not a local debug shortcut. It additionally requires a trusted same-repository `pull_request_target` label event, protected workflow, GitHub token, and `OMNIFLOW_REPAIR_API_KEY`.
