# Breaking Change Hold

OmniFlow's breaking change hold lets a monorepo keep dbt, Omni, and other tooling on a single protected branch without exposing production content to a schema gap. It is optional, disabled by default, and does nothing in repositories that do not deploy dbt.

## The Problem It Solves

Omni promotes authored model YAML as soon as a pull request merges into the configured base branch. That promotion is Omni Git integration behavior, not an OmniFlow API write, so no CI tool can intercept it.

Additive work is safe. New columns, tables, views, and measures do not invalidate anything that already exists, so the old warehouse state keeps serving content until dbt deploys and OmniFlow refreshes Omni.

Breaking work is not safe. Consider a column rename:

1. A pull request renames `customer_id` to `customer_key` in dbt and updates the Omni view to match.
2. The pull request merges. Omni's webhook promotes the model immediately.
3. The shared Omni model now references `customer_key`.
4. The production dbt deployment has not run yet, so the warehouse still has `customer_id`.
5. Every dashboard, report, and query touching that field fails until dbt finishes.

Teams usually work around this with two branches, such as `main` for dbt and `omni-main` for Omni, plus a merge-forward job. That is safe but clunky, and it makes Omni's Git history diverge from the repository's.

The hold policy removes the need for the second branch by refusing to let the unsafe merge happen in the first place.

## What It Detects

The policy evaluates the semantic diff OmniFlow already computes. It only acts when a change carries `breaking` risk: a deleted field, a renamed field, a field type change, a deleted relationship, or a relationship cardinality change.

### Same Pull Request

Fires when a pull request contains breaking Omni model changes and also modifies a configured dbt source path. This is the combined change described above.

### Pending Deployment

Fires when a pull request contains breaking Omni model changes and dbt sources changed on the base branch after the last recorded successful `omniflow dbt sync`. This catches the wrong-order case: an Omni-only pull request that references schema the warehouse does not have yet because the dbt deployment is still in flight.

Pending detection is evidence-based and fails open. When no sync commit has been recorded, or when the recorded commit is unreachable in the runner's checkout, OmniFlow prints a warning and skips the check rather than blocking a merge on incomplete history.

### What Does Not Fire

- Additive Omni changes, with or without dbt changes
- dbt changes with no breaking Omni changes
- Breaking Omni changes in a repository that has no configured dbt paths
- Breaking Omni changes when dbt has not changed since the last successful sync

## Enable The Policy

Add the opt-in block to the trusted `.omniflow.yml` on the protected base branch:

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
  dbt_sync:
    enabled: true
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turns the policy on. Nothing is evaluated while this is false. |
| `action` | `fail` | `fail` blocks the merge through the required check. `warn` reports without changing the exit code. |
| `dbt_paths` | `models`, `seeds`, `snapshots`, `macros` | Relative repository paths that indicate a dbt schema change. Must match the `push.paths` filter on the deployment workflow. Maximum 50 entries. |
| `pending_label` | `omniflow/awaiting-deploy` | Label the workflow applies while a pull request is held, and the label the deployment job releases. |

Paths must be relative and inside the repository. Absolute paths, `..` traversal, and control characters are rejected at config load.

## The Resulting Workflow

### Additive Change

```text
pull request (dbt + Omni) -> OmniFlow passes -> merge
  -> dbt deploys -> omniflow dbt sync -> Omni refreshed
```

Nothing changes for the developer. This is the common case.

### Breaking Change

```text
combined pull request -> OmniFlow FAILS with split guidance
                      -> pull request labeled omniflow/awaiting-deploy

pull request 1 (dbt only)   -> passes -> merge -> dbt deploys
                            -> omniflow dbt sync -> Omni refreshed
                            -> synchronized commit recorded

pull request 2 (Omni only)  -> hold clears, contracts validated
                            -> label removed, auto-merge completes
                            -> Omni promotes against a warehouse that is ready
```

The developer opens both pull requests. OmniFlow sequences them.

## Wire Up The Workflows

Two workflow changes make the sequencing automatic. Both are in [the workflow examples](../.github/workflow-examples/).

### 1. Validation Workflow

Use [omniflow.yml](../.github/workflow-examples/omniflow.yml). It passes the recorded sync commit into the action and applies or clears the hold label:

```yaml
- name: Run OmniFlow
  id: omniflow
  uses: exploreomni/OmniFlow@<pinned-commit-sha>
  with:
    config: .omniflow.yml
    omni-api-key: ${{ secrets.OMNI_API_KEY }}
    last-sync-sha: ${{ vars.OMNIFLOW_LAST_SYNC_SHA }}
```

The action exposes two outputs:

- `hold-triggered` is `true` when the policy parked the pull request
- `hold-pending-label` is the configured label, and is empty when the policy is disabled

The label step is inert unless the policy is enabled, so it is safe to keep in a shared workflow template.

**Set `fetch-depth: 0` on the checkout when the policy is enabled.** Pending detection compares the recorded sync commit against `HEAD`, which requires that commit to be present. With the default shallow checkout, OmniFlow warns and falls back to same-pull-request detection only.

### 2. Deployment Workflow

Use [omniflow-dbt-sync-with-release.yml](../.github/workflow-examples/omniflow-dbt-sync-with-release.yml). After `omniflow dbt sync` succeeds it records the synchronized commit and releases held pull requests:

```yaml
- name: Record synchronized commit
  if: success()
  env:
    GH_TOKEN: ${{ secrets.OMNIFLOW_SYNC_STATE_TOKEN }}
    SYNCED_SHA: ${{ github.sha }}
  run: gh variable set OMNIFLOW_LAST_SYNC_SHA --body "$SYNCED_SHA"

- name: Release held Omni model changes
  if: success()
  env:
    GH_TOKEN: ${{ github.token }}
    PENDING_LABEL: omniflow/awaiting-deploy
  run: |
    # Remove the label and enable auto-merge for each held pull request.
```

The release step uses `gh pr merge --auto`, which respects branch protection. Required checks and reviews still have to pass; the step only stops parking the pull request.

Recording a repository variable needs more permission than the default `GITHUB_TOKEN` provides. Create a fine-grained token with read and write access to repository variables only, store it as `OMNIFLOW_SYNC_STATE_TOKEN`, and scope it to the single repository. When that secret is absent the step logs a notice and the policy degrades to same-pull-request detection.

## Evidence

A triggered hold appears in the normal reviewer summary and in `report.json` with `validator: breaking_change_hold`. The restricted per-model workspace also receives `breaking-change-hold.json` containing the rule, action, matched dbt paths, and up to ten sample breaking changes. Restricted artifacts are deleted by default and are never uploaded by the example workflows.

The hold records repository paths, change types, and field names. It does not record warehouse rows, query results, or authored YAML.

## Limitations

State these plainly when planning an adoption.

- **OmniFlow cannot stop Omni's webhook.** The policy prevents the unsafe merge; it does not gate promotion after a merge happens. A merge performed with an administrative bypass still promotes immediately.
- **Detection is path-based, not semantic.** OmniFlow does not parse dbt models to determine whether a specific column actually changed. A pull request that touches `models/` while making breaking Omni changes is held even if the two are unrelated. Narrow `dbt_paths` to reduce false positives.
- **Pending detection needs Git history and a recorded commit.** Without `fetch-depth: 0` and `OMNIFLOW_LAST_SYNC_SHA`, only same-pull-request detection runs.
- **The warehouse is never inspected.** OmniFlow does not execute queries, so it cannot confirm whether a renamed object already exists. It reasons from repository and deployment evidence only.
- **Auto-merge is not a review bypass.** Held pull requests still need their required checks and approvals.

For a guarantee that no window exists under any merge path, an Omni-side promotion gate would be required. That capability is not currently documented in Omni's public API, so it is not something OmniFlow can provide.

## Official Omni References

- [Omni Branch Mode](https://docs.omni.co/content/develop/branch-mode)
- [Git integration settings](https://docs.omni.co/integrations/git/settings)
- [Git integration best practices](https://docs.omni.co/integrations/git/best-practices)
- [Refresh schema API](https://docs.omni.co/api/models/refresh-schema)
