# OmniFlow Troubleshooting

Start with the exact exit code and the redacted public report. Do not paste an API key, raw Omni payload, private model YAML, customer URLs, or restricted artifacts into an issue.

## Quick Diagnostic

From a trusted local checkout, an administrator can make `OMNI_API_KEY` available through the organization's approved local secret-injection method, then run:

```bash
omniflow doctor --auto
```

Do not put the key in command history or a local file. Remove it from the process environment when finished. `doctor` validates model discovery, API access, branch resolution where applicable, and Omni Git configuration metadata when the token permits it.

## Common Results

### The Pull Request Was Skipped

**Message:** The policy decision is `skipped`.

This is expected when changed files do not belong to a registered Omni model path and no Omni context marker is present. dbt-only and application-only pull requests should not fail because OmniFlow is installed.

If the pull request contains Omni files, verify that `model_path` in `.omni/flow.json` is the exact repository prefix for those files.

### Missing `OMNI_API_KEY`

Confirm that:

- The secret is named exactly `OMNI_API_KEY`.
- The secret is available to the repository or selected environment.
- The workflow passes the secret only through the action's `omni-api-key` input.
- The pull request comes from a branch in the same repository.

Fork pull requests intentionally do not receive the secret. Forked Omni changes fail closed; move the reviewed change to a same-repository branch for validation.

### Trusted `.omni/flow.json` Is Missing

The file must exist on the pull request's protected base branch. Adding it only to the proposed pull-request branch is not sufficient because OmniFlow refuses to trust model hosts supplied by unmerged code.

Check the filename, JSON syntax, `version: 1`, and the pull request's target branch.

### Omni Files Are Outside Every Registered Model Path

Update `.omni/flow.json` on the protected base branch so each Omni model's `model_path` matches its repository directory. Do not broaden a path to hide a routing error. Multi-model repositories should use one unique path per model.

### Omni Branch Could Not Be Resolved

Confirm that:

- The pull request was created from the intended Omni model branch.
- The GitHub head branch matches the Omni branch created through Git integration.
- The API key's Omni user can list model branches.
- The `model_id` identifies the base model rather than an unrelated model or old branch.

### Omni Git Configuration Mismatch

`omniflow doctor --auto` compares configured model path, base branch, provider, and repository URL with Omni when the API allows it. Correct the stale value in `.omni/flow.json` or the Omni Git integration. Do not disable the comparison without resolving which system is authoritative.

### HTTP 401 Or 403

- `401` usually means the API key is missing, invalid, or no longer active.
- `403` usually means the Omni user behind the key lacks access to the requested model or endpoint.

Use the least privilege needed for enabled checks. The optional dbt exposures endpoint can require more permission than core validation.

### Dependency Coverage Gap

OmniFlow fails closed by default when it cannot complete a downstream reference search for a breaking semantic element. This prevents an API or permission failure from being reported as "nothing depends on this field."

Investigate the associated Omni API status, model access, and element identifier. Change `contracts.fail_on.coverage_gaps` only after the governance owner accepts the reduced assurance.

### No Pull-Request Comment

Confirm that:

- The workflow has `pull-requests: write` permission.
- The event is `pull_request_target`.
- `.omniflow/public/report.md` was produced.
- Repository or organization policy does not block GitHub Actions from commenting.

The validation result is still available in the check and public artifact even if commenting fails.

### SARIF Was Not Uploaded To Code Scanning

The official workflow always includes `report.sarif` in the public evidence artifact. Publishing it into GitHub code scanning is opt-in because GitHub Code Security is not available for every repository or plan.

To enable code-scanning upload:

1. Confirm GitHub Code Security is enabled for the repository.
2. Add a repository Actions variable named `OMNIFLOW_UPLOAD_SARIF` with the value `true`.
3. Confirm the workflow has `actions: read` and `security-events: write` permissions.

The upload remains non-blocking so a GitHub platform or subscription limitation cannot hide the OmniFlow validation result.

### Package Installation Failed

Confirm that the workflow:

- Uses the official Linux x86_64 runner and Python 3.11 action runtime.
- Pins the OmniFlow action to a full commit SHA.
- Has outbound access to Python package dependencies.
- Has not changed the action's hash-locked dependency file or replaced the trusted installation step.

## Post-Deployment dbt Sync Results

### Missing `OMNIFLOW_SYNC_API_KEY`

Confirm the secret exists inside the protected `omniflow-production` GitHub environment and the action passes it through `sync-api-key`. Do not put it in `.omniflow.yml` or `.omni/flow.json`. Use a dedicated PAT rather than the validation or AI repair identity.

### Sync Is Disabled

`omniflow dbt sync --auto` requires `deployment.dbt_sync.enabled: true` in trusted policy. This fail-closed behavior prevents a workflow addition from silently introducing a write-capable deployment stage before policy review.

### Wrong Event Or Branch

The sync command accepts only base-branch `push` and reviewed `workflow_dispatch` runs in GitHub Actions. Pull requests, tags, schedules, and branch names that differ from `.omni/flow.json` `base_branch` exit with security code `5`. Run ordinary `omniflow run --auto` for pull-request validation.

### Refresh Returns HTTP 400

Check Omni's **Branch based schema refresh** setting:

- When disabled, remove `OMNI_BRANCH_ID` and `OMNI_BRANCH_NAME` from the deployment environment.
- When enabled, provide an existing branch ID or a branch name the token can resolve.

Omni's API rejects a branch ID when the setting is disabled and requires one when it is enabled. OmniFlow does not infer or create a deployment branch because that could refresh or promote the wrong model state.

### Refresh Job Failed Or Timed Out

Open the model or connection in Omni and inspect the schema refresh status. Confirm dbt finished successfully, the token has Modeler or Connection Admin permission required for that connection, and the configured timeout is sufficient for the model size. OmniFlow records only the job ID and normalized status; do not paste raw API responses into an issue.

### Refresh Triggered Another Deployment

Limit the dbt deployment workflow's `push.paths` to dbt source files and exclude generated Omni model paths. Review Omni Git settings for **Require for system syncs** and Branch based schema refresh. Stop the workflow until the path filter is corrected; repeated refreshes can create repeated generated commits or pull requests.

## AI Repair Results

AI Repair is an unreleased maintainer scaffold and must not be enabled for customer use. See [the AI Repair guide](AI_REPAIR.md) for the release blocker. Never troubleshoot repair by pasting model YAML, the AI prompt, a result summary, or a token into a GitHub issue.

### Missing `OMNIFLOW_REPAIR_API_KEY`

Confirm the secret exists inside the protected `omniflow-ai-repair` GitHub environment, the workflow job names that environment, and the action passes it through `repair-api-key`. Do not substitute `OMNI_API_KEY`.

### AI Repair Is Disabled Or Query Execution Is Not Acknowledged

Both `repairs.ai.enabled` and `repairs.ai.allow_query_execution` must be `true` in trusted base-branch policy. The second value acknowledges that Omni's documented AI Jobs API may execute queries and has no no-query switch. Do not enable it merely to clear an error without completing the security review.

### Repair Was Already Attempted For This SHA

One AI attempt is allowed per pull-request head SHA. Review the prior bot-authored repair comment and evidence. Make a reviewed manual correction or push a new commit before requesting another repair; do not delete the marker to bypass the control.

### `rolled_back`

The AI job, safety inspection, forced model validation, or full configured gate failed, and OmniFlow verified restoration of the original authored YAML. Review the public repair evidence and correct the model manually or create a new reviewed commit.

### `manual_review_required`

OmniFlow could not confirm cancellation or the Git commit result. It deliberately did not overwrite an active or ambiguous branch state. Inspect the Omni branch, AI job, and GitHub PR before any new label, merge, or deployment.

### `rollback_failed`

Stop merge and deployment activity. A concurrent edit or API failure prevented exact rollback verification. Compare the current Omni branch with the PR and reconcile it manually. Rotate the repair PAT if credential misuse is suspected.

## Breaking Change Hold

### The hold blocked a pull request that is actually safe

Detection is path-based. A pull request that touches a configured `dbt_paths` entry while making breaking Omni changes is held even when the two are unrelated. Narrow `deployment.breaking_change_hold.dbt_paths` to the directories that really carry warehouse schema, or split the unrelated change into its own pull request. Use `action: warn` while tuning the paths.

### The hold never fires even though the policy is enabled

Check, in order:

1. The semantic diff has a `breaking` change. Additive changes never trigger the hold.
2. A changed file actually matches a `dbt_paths` entry. Matching requires a directory boundary, so `models` does not match `models_archive`.
3. For pending-deployment detection, `OMNIFLOW_LAST_SYNC_SHA` is set and the checkout uses `fetch-depth: 0`. OmniFlow prints a warning when the recorded commit is unreachable and evaluates same-pull-request detection only.

### `breaking-change hold could not reach the recorded sync commit`

The runner has a shallow checkout, so pending-deployment detection was skipped. Set `fetch-depth: 0` on the `actions/checkout` step in the validation workflow. Same-pull-request detection still ran.

### A held pull request was never released

The release step runs only after `omniflow dbt sync` succeeds. Confirm the deployment job completed, that `OMNIFLOW_SYNC_STATE_TOKEN` is configured so the synchronized commit was recorded, and that the label on the pull request matches `deployment.breaking_change_hold.pending_label`. Auto-merge also waits for required checks and reviews, so a released pull request can still be pending on branch protection.

## dbt Impact Analysis

### The finding is marked `ambiguous_relation_match`

The dbt model name was unqualified and matched several Omni relations with the same table name in different schemas. OmniFlow lists every candidate in `candidate_relations` and reports all their orphaned fields rather than guessing which schema changed. Commit a dbt manifest so the exact `relation_name` is available, or add a `checks.dbt_impact.table_mapping` entry pinning the model to one relation.

### The check flagged a column that is not actually breaking

Matching is textual. A column name appearing incidentally inside a field's SQL is treated as a reference. Confirm the finding against the reported `orphaned_fields`, and add a `table_mapping` entry if the dbt model was matched to the wrong Omni view.

### The check found nothing even though a column was renamed

Read `analysis_mode` and `notes` in `dbt-impact.json`:

- `sql_heuristic` with a note about a missing manifest means precision is limited. Commit a dbt manifest and set `checks.dbt_impact.manifest_path`.
- A note about no available base ref means the checkout had no comparison point. Use a checkout with enough history for `origin/<base>`, the bare base branch, or `HEAD~1` to resolve.
- `SELECT *` in the model SQL yields no columns by design, so no removal is claimed.
- In manifest mode, a column is only reported when the base manifest documented columns for that node.

### The check did not run at all

It only evaluates pull requests with dbt-path changes and no Omni model changes. Confirm `checks.dbt_impact.enabled` is `true`, that a changed file matches `deployment.breaking_change_hold.dbt_paths`, and that an Omni model path is resolvable from `checks.dbt_impact.omni_yaml_paths` or `.omni/flow.json`. When both dbt and Omni files change, the breaking change hold and contract validation apply instead.

### `no Omni model path is available`

The policy is enabled but OmniFlow could not locate the Omni YAML. Add `checks.dbt_impact.omni_yaml_paths`, or ensure `.omni/flow.json` includes a `model_path` for each model.

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success or intentional non-Omni skip. |
| `1` | Validation or policy gate failed. |
| `2` | Configuration or discovery error. |
| `3` | Authentication or authorization error. |
| `4` | Omni API error. |
| `5` | Security policy violation. |
| `6` | Unexpected internal error. |

If the issue remains, follow [Support](../SUPPORT.md) and share only redacted public evidence.
