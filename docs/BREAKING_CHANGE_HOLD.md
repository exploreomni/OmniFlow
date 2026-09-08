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

Pending detection fails closed when this policy is enabled with `action: fail`. Missing, blank, unreachable, or non-ancestor sync state creates `breaking_change_sync_state_unavailable` and blocks a breaking change. Fetch complete trusted base history and record a verified successful sync before adopting this gate. `action: warn` remains explicitly advisory.

### What Does Not Fire

- Additive Omni changes, with or without dbt changes
- dbt changes with no breaking Omni changes
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

pull request 1 (additive dbt expansion) -> passes -> merge -> dbt deploys
                            -> omniflow dbt sync -> Omni refreshed
                            -> synchronized commit recorded

pull request 2 (Omni only)  -> fresh current-head revalidation dispatch
                            -> readiness check passes and configured label clears
                            -> human merges after required checks and reviews
                            -> Omni promotes against the expanded warehouse

later dbt cleanup          -> remove old names only after all consumers migrate
```

Use expand/contract. A destructive dbt rename deployed first would break the existing Omni model. Preserve both old and new warehouse names during the transition, migrate consumers, then remove old names after acceptance.

## Wire Up The Workflows

Three workflow examples provide deployment evidence and explicit revalidation. Merge remains manual.

### 1. Validation Workflow

Use [omniflow.yml](../.github/workflow-examples/omniflow.yml). It passes the recorded sync commit into the action and applies the configured hold label. Provision that label during adoption; failed label writes must remain visible.

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

**Use `fetch-depth: 0`.** Pending detection verifies the synchronized commit is an ancestor of trusted base `HEAD`, then compares their dbt paths. Missing history cannot establish readiness.

### 2. Deployment Workflow

Use [omniflow-dbt-sync-with-release.yml](../.github/workflow-examples/omniflow-dbt-sync-with-release.yml). Its historical filename remains, but after sync it only records and rereads durable state. It never removes labels or enables auto-merge:

```yaml
- name: Record synchronized commit
  if: success()
  env:
    GH_TOKEN: ${{ secrets.OMNIFLOW_SYNC_STATE_TOKEN }}
    SYNCED_SHA: ${{ github.sha }}
  run: |
    set -euo pipefail
    test -n "$GH_TOKEN"
    gh variable set OMNIFLOW_LAST_SYNC_SHA --body "$SYNCED_SHA"
    RECORDED_SHA="$(gh api "repos/${GITHUB_REPOSITORY}/actions/variables/OMNIFLOW_LAST_SYNC_SHA" --jq .value)"
    test "$RECORDED_SHA" = "$SYNCED_SHA"
```

Missing state credentials, failed writes, or mismatched rereads fail the deployment workflow. A successful schema refresh alone is insufficient to release a PR.

Recording a repository variable requires a narrowly scoped token with repository Variables read/write permission. Store it as `OMNIFLOW_SYNC_STATE_TOKEN` in the protected environment. Revalidation needs read access to that same durable state and never trusts a workflow's captured `${{ vars }}` snapshot.

### 3. Current-head readiness workflow

Install [omniflow-revalidate-held.yml](../.github/workflow-examples/omniflow-revalidate-held.yml), pin the reviewed OmniFlow commit, and configure the protected environment. After a successful deployment, dispatch this workflow on current protected `main` with the open PR number. It loads the PR and sync state from GitHub, checks the checkout matches current base, and runs the complete existing Omni validation pipeline using a fresh PR event.

The helper creates **`OmniFlow deployment readiness`** on the exact current PR head SHA. It rejects forks, drafts, stale checkout, missing state, failed/skipped model validation, and mismatched report SHA. Before completing success it rereads head, base, and durable sync SHA. If any changed, dispatch a fresh run. Only success clears the trusted policy's configured `pending_label`; it never merges or sends a PR comment. API write failure cannot publish readiness success.

**Adopter configuration is mandatory:** require `OmniFlow deployment readiness` with the expected GitHub Actions source, require the PR to be up to date with its protected base, retain independent test/security checks and reviews, and prevent bypass. For dbt-enabled repos this fresh check is the merge gate for Omni validation; an old `pull_request_target` job may still report its earlier deployment hold and must not be treated as the current-head readiness evidence. The example does not automatically modify consumer branch protection. Dispatch the readiness check for every model PR subject to this required gate, including additive PRs.

Do not use an API rerun of an old `pull_request_target` job as fresh evidence: reruns retain the original event and base snapshot. A new dispatch performs current-head validation explicitly. A PR push requires a new check, and a base change requires updating the PR and revalidation. Labels remain informational; the SHA-bound required check is authoritative.

## Evidence

A triggered hold appears in the normal reviewer summary and in `report.json` with `validator: breaking_change_hold`. The restricted per-model workspace also receives `breaking-change-hold.json` containing the rule, action, matched dbt paths, and up to ten sample breaking changes. Restricted artifacts are deleted by default and are never uploaded by the example workflows.

The hold records repository paths, change types, and field names. It does not record warehouse rows, query results, or authored YAML.

## Limitations

State these plainly when planning an adoption.

- **OmniFlow cannot stop Omni's webhook.** The policy prevents the unsafe merge; it does not gate promotion after a merge happens. A merge performed with an administrative bypass still promotes immediately.
- **Detection is path-based, not semantic.** OmniFlow does not parse dbt models to determine whether a specific column actually changed. A pull request that touches `models/` while making breaking Omni changes is held even if the two are unrelated. Narrow `dbt_paths` to reduce false positives.
- **Pending detection needs Git history and a recorded commit.** Missing evidence blocks breaking changes under the default failing action.
- **The warehouse is never inspected.** OmniFlow does not execute queries, so it cannot confirm whether a renamed object already exists. It reasons from repository and deployment evidence only.
- **Merge is manual.** Required current-head readiness, up-to-date branch protection, and reviews must be configured by adopters. A passing check proves a bounded validation snapshot; it cannot prevent later warehouse changes or administrative bypass.

For a guarantee that no window exists under any merge path, an Omni-side promotion gate would be required. That capability is not currently documented in Omni's public API, so it is not something OmniFlow can provide.

## Official Omni References

- [Omni Branch Mode](https://docs.omni.co/content/develop/branch-mode)
- [Git integration settings](https://docs.omni.co/integrations/git/settings)
- [Git integration best practices](https://docs.omni.co/integrations/git/best-practices)
- [Refresh schema API](https://docs.omni.co/api/models/refresh-schema)
