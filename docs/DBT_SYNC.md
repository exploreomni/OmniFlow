# Post-Deployment dbt Synchronization

OmniFlow can refresh Omni after a production dbt deployment so new dbt metadata is available before the deployment is considered complete. This stage is optional, disabled by default, and does nothing in repositories that do not use dbt.

## Supported Sequence

1. A developer opens a pull request and the normal dbt and OmniFlow validation jobs run.
2. Reviewers approve and merge the pull request.
3. The protected production job deploys dbt to the warehouse.
4. Only after that command succeeds, the job runs `omniflow dbt sync --auto`.
5. OmniFlow selects every model whose trusted `.omni/flow.json` `base_branch` matches the deployed Git branch.
6. It resolves each model's documented `connectionId`, verifies that every affected shared model is registered on the deployed base branch, and preflights all read access before starting a write.
7. When semantic lint or contracts are enabled, it captures each affected model's authored YAML in the restricted workspace before refresh so the later diff has a true pre-deployment baseline.
8. It starts only one refresh per tenant, connection, and Omni branch, then polls each returned job at the configured interval until it completes, fails, or reaches the timeout. The default is five seconds; Omni recommends two to five seconds.
9. After a successful refresh, OmniFlow reruns model validation, Content Validator, semantic lint, contract impact, and any enabled dbt exposure checks for every affected configured model.
10. GitHub uploads redacted reports and evidence even when refresh or validation fails.

Post-sync model and content validation enforce strict health, even when PR policy has either `fail_on_new_only` switch enabled. Existing failures cannot be suppressed by comparing the refreshed base with itself or by reusing history. Model warnings still follow `fail_on_warnings`.

OmniFlow does not run dbt itself. The customer's reviewed production dbt command remains the source of truth for whether warehouse deployment succeeded.

## Enable The Policy

Add this opt-in block to the trusted `.omniflow.yml` on the protected base branch:

```yaml
deployment:
  dbt_sync:
    enabled: true
    refresh_mode: hard
    poll_interval_seconds: 5
    timeout_seconds: 900
    post_sync_validation: true
```

`hard` incorporates additions and removals. `soft` is additive only. Omni documents that selective schema or table refresh parameters can be used only for soft refreshes; OmniFlow does not expose selective refreshes in this release.

## Add The Protected Job

Use [the dbt sync workflow example](../.github/workflow-examples/omniflow-dbt-sync.yml) as a template, or place this action step immediately after the existing production dbt deployment command:

```yaml
- name: Synchronize dbt metadata into Omni
  uses: exploreomni/OmniFlow@<pinned-commit-sha>
  with:
    mode: dbt-sync
    config: .omniflow.yml
    sync-api-key: ${{ secrets.OMNIFLOW_SYNC_API_KEY }}
```

Configure the job with:

- A protected GitHub environment such as `omniflow-production`, with required reviewers where appropriate.
- A separate `OMNIFLOW_SYNC_API_KEY` environment secret containing the operator's choice of Organization API Key or dedicated service-user Personal Access Token (PAT). For a PAT, a Modeler can refresh a connection with exactly one shared model; a Connection Admin is required for connections with multiple shared models. An Org key carries Organization Admin privileges. The selected credential also needs access for every enabled post-sync validation check. Do not reuse the validation or AI repair credential.
- `contents: read` permissions only, unless the customer's dbt deployment needs additional permissions.
- A `push` trigger restricted to the production base branch and dbt source paths, or `workflow_dispatch` for a reviewed manual deployment.

Do not trigger this job for `pull_request` or `pull_request_target`. OmniFlow rejects those events with exit code `5`, even if a workflow is misconfigured.

The client does not detect credential type or offer a credential-type policy setting. The same [user-scope limitations](CONFIGURATION.md#credential-choice-and-user-scope) apply to post-sync Content Validator checks: leave `omni.user_id`, `OMNI_USER_ID`, and `--user-id` unset with a PAT. Verify the chosen credential and expected content scope in the [live sync gate](TESTING.md#live-post-deployment-dbt-sync-gate).

## Branch-Based Schema Refresh

Omni's API contract differs by connection setting:

- When Branch based schema refresh is off, do not supply an Omni branch ID. The protected post-deployment job can refresh the shared model directly.
- When Branch based schema refresh is on, Omni requires an existing `branch_id`. Provide `OMNI_BRANCH_ID` or `OMNI_BRANCH_NAME` from the protected deployment environment. OmniFlow refreshes and validates that branch, but it does not promote, merge, or approve it.

This means branch-based refresh introduces a separate human promotion step. A completed refresh is evidence that metadata was synchronized into the selected Omni branch, not evidence that the branch reached the shared model.

## Loop Prevention

Omni Git integration can commit system-generated model changes or require a pull request for system syncs, depending on its Git settings. Limit the deployment workflow's `push.paths` to dbt source files and keep generated Omni model paths out of that list. This prevents an Omni-generated commit from starting another dbt deployment and refresh loop.

Also review Omni's **Pull request required > Require for system syncs** and **Branch based schema refresh** settings before enabling this job. Those settings determine whether refresh changes reach the base branch directly or require another reviewed promotion.

## Evidence And Failure Behavior

Each run emits:

```text
.omniflow/
  report.json
  report.md
  report.sarif
  junit.xml
  evidence.json
  dbt-sync.json
  artifact-manifest.json
  public/
```

The public sync artifact uses diagnostic schema 2: permitted status/count/policy fields and generated anonymous references, not model, branch, refresh-job, or Git identities. Standard and strict settings share this minimal boundary. Verify the exact target and refresh job through trusted workflow/Omni state and authorized private execution evidence. Public output does not contain API keys, raw Omni responses, authored YAML, warehouse rows, or query results. See [Diagnostic Privacy](DIAGNOSTIC_PRIVACY.md).

Pre-refresh and post-refresh authored YAML snapshots are restricted artifacts. They are deleted by default and are never included in the example evidence upload. If `security.retain_restricted_artifacts` is explicitly enabled, they remain only in the runner workspace unless a customer separately changes artifact handling.

Synchronization across multiple connections is not transactional. If one connection refresh succeeds and a later connection fails, OmniFlow records the partial outcome and fails the job, but it cannot roll back the completed refresh or the preceding dbt deployment. Operators should keep the deployment open, review the evidence, and remediate or rerun the failed connection.

Exit behavior:

- `0`: no blocking operation or validation failure. Inspect `validation_complete` and `policy_decision`: incomplete nonblocking checks or disabled post-sync validation are advisory, not a completed validation pass.
- `1`: refresh completed, but post-sync validation failed.
- `2`: sync is disabled or trusted model/branch metadata is incomplete.
- `3`: the sync token is unauthorized.
- `4`: Omni rejected the refresh, the job failed, or polling timed out.
- `5`: the event or Git branch violates deployment policy.
- `6`: an internal OmniFlow error occurred.

## Single-Branch Monorepos

Repositories that keep dbt and Omni model YAML on the same protected branch can pair this stage with the [breaking change hold](BREAKING_CHANGE_HOLD.md). The hold blocks unsafe promotion when deployment evidence is pending or unavailable. [omniflow-dbt-sync-with-release.yml](../.github/workflow-examples/omniflow-dbt-sync-with-release.yml) records and rereads the synchronized commit only after the reviewed deployment and confirmed shared-scope refresh; missing credentials or failed persistence fail the workflow. It never releases a PR itself. Dispatch [omniflow-revalidate-held.yml](../.github/workflow-examples/omniflow-revalidate-held.yml) for fresh validation of the current PR head before a human merges. Adopt the required `OmniFlow deployment readiness` check and up-to-date branch protection explicitly.

Synchronization and readiness are separate facts. The Action exposes fixed boolean outputs:

- `dbt-sync-complete`: every requested connection refresh finished successfully.
- `dbt-sync-state-eligible`: every refresh completed against shared scope, not an unpromoted branch. Only this result, together with a successful reviewed dbt deployment, can authorize recording `OMNIFLOW_LAST_SYNC_SHA`.
- `post-sync-validation-complete`: enabled post-sync checks returned complete evidence for every model. Completeness alone does not establish a pass; inspect the decision and findings too.

A refresh can finish while an optional check is unavailable. Its nested status is `incomplete`, not `passed`; the zero-exit aggregate is `advisory`. Disabled post-sync validation is `not_run`. Failed or unknown refresh state cannot advance synchronized state. Recording a verified synchronized commit does not waive incomplete validation, clear a hold label or authorize a merge. Branch-based refresh cannot establish shared-model synchronization without a separately reviewed promotion.

For destructive changes, deploy an additive expansion first while retaining old warehouse names, synchronize, migrate Omni and other consumers, validate adoption, then remove obsolete names. Deploying a destructive dbt rename before updating consumers is not a safe sequence.

## Official Omni References

- [Refresh schema API](https://docs.omni.co/api/models/refresh-schema)
- [Get schema refresh job status](https://docs.omni.co/api/jobs/get-job-status)
- [Schema refresh behavior](https://docs.omni.co/modeling/develop/schema-refreshes)
- [Git integration settings and system syncs](https://docs.omni.co/integrations/git/settings)
