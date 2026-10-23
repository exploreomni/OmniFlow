# OmniFlow Troubleshooting

## Leader/follower release PR targets a different base branch

Version 1 registers one base branch per model. Do not retarget a legitimate release
promotion PR into development just to pass that check. Use the reviewed
[version 2 environment registry](ENVIRONMENT_TARGETS.md) and its environment-bound
validation workflow. Model IDs, hosts and Git settings must identify the real targets.

For version 2, an unverified candidate means validation coverage is incomplete, not
that there are no breaking changes. Confirm the environment credential, follower
**Always create branches** setting and release branch synchronization. Exact authored
YAML must match the immutable PR head before and after checks. Missing branches never
fall back to production's base model. Check synchronization before a bounded rerun;
do not bypass the check or reuse development's deployment-state variable.

Start with the exact exit code and the redacted public report. Do not paste an API key, raw Omni payload, private model YAML, customer URLs, or restricted artifacts into an issue.

For the 0.4.0 relationship-coverage defect, unreadable dashboard-filter errors, and the revised report, see [Validation Diagnostics and Upgrading](VALIDATION_DIAGNOSTICS.md).

## Quick Diagnostic

From a trusted local checkout, an administrator can make `OMNI_API_KEY` available through the organization's approved local secret-injection method, then run:

```bash
omniflow doctor --auto
```

Do not put the key in command history or a local file. Remove it from the process environment when finished. `doctor` validates model discovery, API access, branch resolution where applicable, and Omni Git configuration metadata when the token permits it. It does not identify the credential type, test every enabled endpoint, or establish complete content coverage.

If the model read succeeds but Git metadata access is denied, `doctor` reports **incomplete**, not ready. Model paths and branch names are case-sensitive; spaces are valid. A trailing directory slash is normalized for the legacy model-path comparison only, not branch names.

For configuration failures, use the safe `config_key`, `expected_type`, `source_line`/`source_column` and 1-based `entry_index` when present. The actual invalid value is deliberately omitted. Missing policy or metadata means the trusted file was not found; `policy_inaccessible` or `model_metadata_inaccessible` means it could not be read. PR-only policy edits cannot authorize themselves: correct the target-branch file and fetch that trusted history first.

`changed_files_unavailable` means Git comparisons failed, not that no relevant files changed. Restore the checkout and required revisions, then rerun. Missing credentials have separate codes for validation (`OMNI_API_KEY`), repair (`OMNI_REPAIR_API_KEY`) and synchronization (`OMNI_SYNC_API_KEY`); use the documented trusted secret injection, never committed credentials.

## Common Results

### Candidate Verification: Git and Omni Snapshots Differ

In default strict mode, `candidate_verification_failed` with exit code `2` means OmniFlow could not establish
that it is validating the proposed Git revision. It does **not** establish a model
or dashboard defect. Check **Check Execution**: checks marked `not_run` have not
validated anything. A failure during the final recheck also leaves validation incomplete.

Use the public report's `candidate_reason`, `validation_scope` (`branch` or `base`),
and available `git_file_count`, `omni_file_count`, `git_only_count`, `omni_only_count`.
Safe mismatch hints include `case_only_count` and `suffix_only_count` for
uniquely paired unmatched filenames, not proposed automatic fixes. Older reports
also used `directory_only_count`; organizational folders need not match when typed
identity establishes a unique counterpart. Schema/database identity is preserved. Counts may not explain every unmatched file and do
not prove equality. Successful scoped proofs may include `excluded_file_count`
for reviewed ancillary files. No mismatch paths or rule text are made public.

| Reason | What to check |
| --- | --- |
| `file_inventory_mismatch` | Check the exact trusted `model_path`, initial model export, and Git/Omni synchronization for that scope. Git-only generic YAML is ignored automatically; recognized Omni sources and generic YAML returned by Omni still require matching Git counterparts. |
| `ambiguous_file_identity` | Inspect the private path examples. Repeated view basenames across databases/schemas are valid; retain canonical view identity or authored `catalog`/`schema`/`table_name` qualification. Resolve duplicate topic names, model/global relationships definitions, or duplicate export copies. Do not rename valid schema-scoped views. Identical bytes cannot disambiguate identity. |
| `content_mismatch` | File identities match, but bytes differ. Save/synchronize the intended Omni branch or base and its corresponding PR Git revision. Comments and whitespace still count as byte differences. |
| `changed_during_validation` | The snapshot changed during the checks. Let synchronization finish, then rerun against the current PR revision. |
| `api_snapshot_unavailable` / `git_snapshot_unavailable` | Restore API/repository access or availability and complete Git history before rerunning. |
| `non_omni_source_conflict` | A classification rule covers a known Omni/control file or a source returned by Omni. Narrow/remove the trusted rule; do not suppress the source. |
| `non_omni_rule_conflict` | Multiple rules classify the same file or duplicate a tag/pattern. Resolve the ambiguity on the trusted base; there is no first-match precedence. |

**Native filename compatibility:** supporting revisions match one trailing `.yaml`
or `.yml` on known Omni names: `model`, `relationships`, and typed
`.view`, `.query.view`, `.topic`, `.composite_topic`, `.relationships` files.
For example, Git `SCHEMA/orders.view.yaml` can match API `Reporting/orders.view`
when the typed name is unique in both complete inventories. Repeated view filenames
can match exact authored paths, canonical view filenames, or unambiguous matching
catalog/schema/table qualification. A directory label alone is not assumed to be a
database or schema. Filename capitalization and spaces stay significant; all authored
source contents must still match exactly.
Generic API-authored filenames require exact basename counterparts;
unknown names and repeated suffixes do not acquire guessed aliases.

Topics and singleton definitions must be unique within that model; views may repeat
basenames when qualified identities distinguish them. SQL/query views keep canonical
Omni identity rather than requiring a physical table. Do not change connection view
naming settings to work around an inventory error. A file outside
`model_path` cannot satisfy a missing artifact inside it, even with the same name.
A new Omni file is not skipped merely because the API lacks it: save/synchronize
it to the candidate so the validators actually evaluate the proposed addition.
This matching change does not change new-only versus all-issues validation policy.

Older Action pins can report `file_inventory_mismatch` even when native exported
files and API files have identical contents. Upgrade **every** applicable OmniFlow
`uses:` entry to the same reviewed full commit SHA containing the filename fix;
merge the workflow update into the trusted default branch through normal review,
then run a fresh non-production PR validation. A green upgrade PR can still have
run the old trusted workflow. No new policy switch is required. Do not rename model
folders, remove `.yaml` suffixes, reinstall, or disable verification to work around it.

Keep `.omni/flow.json` at the repository root's `.omni` directory, separate from
model files. A path such as `omni/Snowflake Analytics` must match the real directory
exactly. Source-file differences are intentionally not printed in public diagnostics;
compare them inside your authorized repository and Omni instance. Matching file
counts alone do not prove matching inventories or contents.

Initial ordinary inventory/content/missing-branch synchronization failures receive
one retry after two seconds per affected scope. Final drift is never retried away.
After an ordinary sync failure, available model/content results may be shown as
**health only**. They do not establish what this PR broke; new/existing/resolved
attribution is withheld and unfinished checks remain `not_run`.

### Exit 0 But The Report Says Advisory Or Incomplete

Check the trusted root `.omniflow.yml` for `validation.mode: advisory`. This is
onboarding-only: eligible synchronization gaps can be nonblocking while the report
still says `policy_decision: advisory`, `decision_status: incomplete`,
`validation_complete: false`, and unknown PR impact. It is not a verified pass.
An enabled breaking-change hold prevents this downgrade, including when the hold
could not run. Security/scope conflicts, malformed evidence, API/authentication
failures and independent blockers remain blocking.

Resolve synchronization and restore `validation.mode: strict` for production merge
gating. Do not interpret an exit code, green job, or skipped advisory JUnit finding
as successful validation without reading the report's completeness and policy.
Checks marked `not_run`, including contracts or lint, have not passed.

### Ancillary YAML Is Not An Omni Model File

Do not move `.omni/flow.json`, rename real source files, or use a blanket exclusion.
Current revisions automatically ignore ordinary Git-only generic YAML during
candidate verification and do not route it as Omni source solely because it is
inside a model folder. No `files.non_omni` rule is needed for that default.
If Omni's authored API returns the file, however, it is retained as source and
must match its Git counterpart exactly.

Optional reviewed rules still require `tag`, repository-relative `pattern`, and
`reason`; they cannot hide known or API-returned Omni sources on either side.
Custom API-authored generic YAML-only PRs are not automatically routed: use
standard native export names or explicit registered-model selection. See
[source families and pattern grammar](CONFIGURATION.md#trusted-non-omni-file-classification).

### Existing Lint Debt Still Appears In The Report

Presence and blocking are different. In a verified regression PR, matched ordinary
lint debt is `existing` and nonblocking; new/additional findings follow rule
severity. Model/content debt requires the corresponding `fail_on_new_only: true`.
The personal-folder scope restriction and other security controls are not
grandfathered. No-branch health checks remain strict.

### Targeted Content Is Enabled But Requests Are Still Broad

This is intentional. `checks.content_validation.targeted` is experimental and
defaults to `false`; the current CLI cannot prove complete dependency scope and
absence of branch drafts. `true` therefore reports `content_scope_mode: broad`
with `semantic_coverage_unverified`. It does not skip comment-only changes or
establish no impact. Do not change validation depth or disable checks to force
targeting; production qualification is still required.

### Required Policy File Is Missing

**Result:** Configuration exit code `2` with setup guidance.

Copy the canonical [`.omniflow.yml`](../.omniflow.yml) into the customer repository root and merge it onto the protected base branch through the approved setup process. It is separate from `.github/workflows/omniflow.yml`; having the workflow alone is not enough. A policy added only in the PR head is deliberately not trusted by the privileged workflow.

Check the exact filename, the workflow checkout's trusted base revision, the PR target branch, and any explicit Action `config` / CLI `--config` path. A custom selected file is supported but must exist. Environment variables or identity flags cannot replace the file, and missing policy does not silently revert to built-in defaults. For an upgrade from a policy-free installation, bootstrap the policy before updating the Action pin. See [installation step 5](INSTALLATION.md#step-5-merge-the-setup-into-the-base-branch).

### The Pull Request Was Skipped

**Message:** The policy decision is `skipped`.

With valid trusted policy, this is expected when changed files do not belong to a registered Omni model path and no Omni context marker is present (unless an enabled optional check applies). dbt-only and application-only pull requests should not fail merely because OmniFlow is installed. Missing policy is a setup failure, not a skip.

If the pull request contains Omni files, verify that `model_path` in `.omni/flow.json` is the exact repository prefix for those files.

### Missing `OMNI_API_KEY`

Confirm that:

- The secret is named exactly `OMNI_API_KEY`.
- Its value is the chosen Omni Organization API Key or Personal Access Token (PAT), not a GitHub token. See [credential choice](CONFIGURATION.md#credential-choice-and-user-scope).
- The secret is available to the repository or selected environment.
- The workflow passes the secret only through the action's `omni-api-key` input.
- The pull request comes from a branch in the same repository.

Fork pull requests intentionally do not receive the secret. Forked Omni changes fail closed; move the reviewed change to a same-repository branch for validation.

### Trusted `.omni/flow.json` Is Missing

The file must exist on the pull request's protected base branch. Adding it only to the proposed pull-request branch is not sufficient because OmniFlow refuses to trust model hosts supplied by unmerged code.

Check the filename, JSON syntax, the selected schema version (`1` for the single-target format or `2` for the environment registry), and the pull request's target branch.

### Omni Files Are Outside Every Registered Model Path

Update `.omni/flow.json` on the protected base branch so each Omni model's `model_path` matches its repository directory. Do not broaden a path to hide a routing error. Multi-model repositories should use one unique path per model.

### Omni Branch Could Not Be Resolved

Confirm that:

- The pull request was created from the intended Omni model branch.
- The GitHub head branch matches the Omni branch created through Git integration.
- The selected Omni credential can list the model's branches.
- The `model_id` identifies the base model rather than an unrelated model or old branch.

### Omni Git Configuration Mismatch

PR new-only validation additionally verifies live authored candidate/base YAML against immutable Git head/base before and after checks. Missing, unverifiable, or out-of-sync snapshots cannot establish a verified pass even when current issue lists are empty; strict mode blocks them. Confirm the exact selected model/branch and Git objects, synchronize the Omni candidate and base, then rerun. Do not use missing-repository or identity overrides to manufacture a baseline. Sampled matches and the API's default validation depth are not atomic or exhaustive content-coverage guarantees.

`omniflow doctor --auto` compares configured model path, base branch, provider, and repository URL with Omni when the API allows it. Correct the stale value in `.omni/flow.json` or the Omni Git integration. Do not disable the comparison without resolving which system is authoritative.

### HTTP 401 Or 403

- `401` usually means the API key is missing, invalid, or no longer active.
- `403` can mean the credential lacks access to the requested model or endpoint. It can also mean a parameter is incompatible with that credential type: Content Validator rejects `userId` with a PAT, even when the user can access the model.

For a PAT, leave `omni.user_id`, `OMNI_USER_ID`, and `--user-id` unset. Do not switch to an admin credential merely to work around that parameter error. For an Organization API Key, confirm that any explicitly selected user and model belong to the intended tenant. See [user-scope limitations](CONFIGURATION.md#credential-choice-and-user-scope).

Review the access needed for all enabled checks before changing permissions. PATs inherit their user's permissions; Organization API Keys carry Organization Admin privileges. The optional dbt exposures endpoint can require more permission than core validation. Neither credential type guarantees complete content coverage.

### Dependency Coverage Gap

OmniFlow fails closed by default when it cannot complete a downstream reference search for a breaking semantic element. This prevents an API or permission failure from being reported as "nothing depends on this field."

Investigate the associated Omni API status, model access, and element identifier. Change `contracts.fail_on.coverage_gaps` only after the governance owner accepts the reduced assurance.

### YAML `viewNames` Metadata Processing Failed

The message begins `Omni YAML metadata processing failed (viewNames: ...)`. The report identifies the stage as `metadata_stage: view_names` and includes a bounded `metadata_reason`. This is an input-processing failure, not proof of a semantic model defect. The run exits with a configuration error (`2`); completed findings are retained, `validation_complete` is false, and dependent checks marked `not_run` have not passed.

OmniFlow accepts both file-path → canonical-name API maps and canonical-name → file-path maps. It normalizes them to name → path in the snapshot. Empty names are allowed only for recognized non-view files; view identities must be complete, unique, and case-sensitive. Mixed or ambiguous orientations are rejected instead of guessed.

An earlier `Omni YAML file inventory` error means a file entry itself has a non-string path or unsupported content type. That is an API error (`4`), not this metadata configuration error. Malformed entries are never dropped to make identity coverage appear complete.

| Reason category | What to investigate |
| --- | --- |
| `invalid_mapping` | The map's value types or entry count do not match the file inventory. |
| `invalid_path`, `unsupported_orientation` | Unsafe, unknown, or case-mismatched paths, or a mixture of mapping orientations. |
| `ambiguous_orientation` | Both keys and values match inventory paths, so the intended identity cannot be chosen safely. |
| `duplicate_name`, `duplicate_path` | More than one view file claims the same name, or multiple names point to one file. |
| `invalid_identity`, `non_view_identity`, `incomplete_identity` | Invalid/empty view names, names attached to non-views, missing view entries, or unusable view definitions. |

For issue [#24](https://github.com/exploreomni/OmniFlow/issues/24), an older build rejected reported path → name responses with `Invalid or ambiguous Omni viewNames metadata; refresh the YAML snapshot`. Use a reviewed, published revision containing the compatibility fix when available; merging a change here does not update a consumer's pinned Action. See [release notes](RELEASE_NOTES.md).

If a current build still rejects the payload, capture the exact Action revision, base/branch scope, YAML mode, and redacted public report. Share only a sanitized example of the map and matching file inventory through an approved support channel, retaining keys and value types while replacing sensitive names. Do not post raw YAML, credentials, or restricted snapshots. Confirm the response contract before changing it; do not invent view names, invert mixed entries individually, disable identity checks, or assume repeated refreshes repair an unsupported format. After correction, run fresh validation against the intended candidate branch and review any remaining findings separately.

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

Confirm the secret exists inside the protected `omniflow-production` GitHub environment and the action passes it through `sync-api-key`. Do not put it in `.omniflow.yml` or `.omni/flow.json`. Choose a separate deployment Organization API Key or service-user PAT rather than reusing the validation or AI repair credential. See [sync credential requirements](DBT_SYNC.md#add-the-protected-job).

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

Open the model or connection in Omni and inspect the schema refresh status. Confirm dbt finished successfully, the PAT has the Modeler or Connection Admin permission required for that connection (or the selected Org key has Organization Admin access), and the configured timeout is sufficient for the model size. OmniFlow records only the job ID and normalized status; do not paste raw API responses into an issue.

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

Detection is path-based. A pull request that touches a configured `dbt_paths` entry while making breaking Omni changes is held even when the two are unrelated. Narrow `deployment.breaking_change_hold.dbt_paths` to the directories that really carry warehouse schema, or split the unrelated change into its own pull request. `action: warn` is an explicitly advisory policy choice while tuning paths, not proof that a deployment is ready. Do not use it to bypass missing sync evidence.

### The hold never fires even though the policy is enabled

Check, in order:

1. The trusted base-branch policy has `deployment.breaking_change_hold.enabled: true`. If you expect a blocking finding, use `action: fail`; `action: warn` reports findings without failing validation for this hold.
2. The semantic diff has a `breaking` change. Additive changes never trigger the hold. A failed semantic diff is incomplete validation, not evidence that a change is safe.
3. For same-pull-request detection, a changed file matches a `dbt_paths` entry. Matching requires a directory boundary, so `models` does not match `models_archive`.
4. Without same-pull-request overlap, OmniFlow checks for dbt changes since `OMNIFLOW_LAST_SYNC_SHA`. A verified sync with no later dbt changes produces no hold. Missing, blank, unreachable, or non-ancestor state instead produces `breaking_change_sync_state_unavailable` and blocks a breaking change under `action: fail`.

### `breaking_change_sync_state_unavailable`

The report says: "Cannot establish deployment readiness: sync state is missing, unreachable, or not an ancestor of the trusted base. Record a verified successful sync and fetch complete base history."

This finding means an enabled hold found a breaking Omni change without same-pull-request dbt overlap, but could not establish deployment readiness. It is an error under `action: fail` and an advisory warning only under `action: warn`; pending detection has not safely passed.

To recover:

1. Fetch complete history for the trusted base checkout using `fetch-depth: 0`. This fixes missing history, not a missing deployment record or a commit from the wrong history.
2. Verify that `OMNIFLOW_LAST_SYNC_SHA` identifies an actual successful protected deployment followed by successful `omniflow dbt sync`, and that the commit is an ancestor of the trusted base. Confirm the validation action receives that recorded value through `last-sync-sha`.
3. If no verified sync exists yet, bootstrap it through the protected deployment/sync workflow before adopting the required readiness gate. Preserve existing warehouse references during the deployment; do not deploy a destructive rename first. Record the successfully synchronized commit with `OMNIFLOW_SYNC_STATE_TOKEN` and verify the durable value was written. Do not substitute the current HEAD or another arbitrary SHA merely to clear the hold.
4. After correcting history or state, dispatch [fresh current-head revalidation](BREAKING_CHANGE_HOLD.md#3-current-head-readiness-workflow) on the current protected base for the open PR. Rerunning an old `pull_request_target` job retains its old event and is not fresh readiness evidence.

A non-hexadecimal or otherwise malformed SHA produces a separate security-policy error, not this hold finding. Correct it from verified deployment evidence; more Git history will not repair an invalid value.

### A held pull request was never released

Successful `omniflow dbt sync` and a recorded commit do not automatically release or merge a pull request. Confirm the deployment job recorded and reread `OMNIFLOW_LAST_SYNC_SHA`, then dispatch the current-head readiness workflow on the current protected base with the open PR number. It reruns validation and clears the configured `deployment.breaking_change_hold.pending_label` only after readiness succeeds. The PR must still satisfy required checks, up-to-date branch protection, and reviews before a human merges it. See [Breaking Change Hold](BREAKING_CHANGE_HOLD.md#3-current-head-readiness-workflow).

## dbt Impact Analysis

### A blocking `OF-DBT-IMPACT-ORPHANED-COLUMN` or `OF-DBT-IMPACT-ORPHANED-MODEL` finding

The dbt change removes a column, or removes or moves a relation, that the candidate Omni YAML still references. Update the referencing Omni fields or views in the same deployment sequence, or keep the old name until Omni and its content are migrated (expand, migrate, then contract). The private repository report and `dbt-impact.json` list the affected fields.

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

It evaluates dbt-only and mixed pull requests. Confirm `checks.dbt_impact.enabled` is `true`, that a changed file matches `deployment.breaking_change_hold.dbt_paths`, and that an Omni model path is resolvable from `checks.dbt_impact.omni_yaml_paths` or `.omni/flow.json`. Optional column-to-content lookup additionally requires `checks.dbt_impact.content_impact.enabled` in trusted base policy, same-repository scope, and an Omni credential with suitable read access. Partial/unavailable impact coverage is not zero affected content and does not replace the validation gate.

### `no Omni model path is available`

The policy is enabled but OmniFlow could not locate the Omni YAML. Add `checks.dbt_impact.omni_yaml_paths`, or ensure `.omni/flow.json` includes a `model_path` for each model.

### The PR shows impact counts but no affected-content link

The short comment and public report deliberately exclude document identities.
Adopt the current workflow example's **Upload affected content** step and its
`CONTENT_IMPACT_READY` / `IMPACT_*` publication inputs; a successful ordinary
diagnostics upload is not enough. The dedicated upload is permitted only after
fresh private-repository policy verification. With minimal diagnostics, a public
repository, a failed upload, or missing current-run receipts, the comment explains
the missing link instead of substituting an older artifact. An upload failure does
not change the advisory analysis into a validation failure.

### Impact coverage is partial or unavailable

Read **Coverage gaps and recovery** in `column-impact.md` when private details are
available, or the safe explanation in the public report. Unmatched views are not
substituted with same-named views from another schema. Unsupported saved
calculations, macros/compiled SQL, permissions, and lookup limits can leave gaps;
neither an omitted document nor a zero observed count proves it is unaffected.
See [coverage categories](DBT_IMPACT.md#interpreting-incomplete-impact-coverage).

### A configured dbt-to-Omni view mapping could not be resolved

`dbt_explicit_view_mapping_unresolved` means an explicit
`checks.dbt_impact.table_mapping[].omni_view` value matched no authored view across
the registered model roots for the recorded base or head revision. It is not a
confirmed zero-impact result. Other successfully resolved mappings still contribute
their observed impacts; the unresolved part remains unknown.

1. Open **Coverage gaps and recovery** in the authorized private `column-impact.md`
   artifact to find the configured view and affected revision. Public reports omit
   that identifier.
2. Check the mapping in the trusted base branch's `.omniflow.yml` against the authored
   Omni view identity, including its exact spelling. Review whether the view was
   deliberately renamed or removed in the affected revision.
3. Correct stale configuration through your normal reviewed workflow, then rerun.
   Do not substitute a similarly named view or delete the mapping merely to obtain
   a clean result. A valid mapping in one registered root need not exist in every root.

The lookup remains advisory and does not change the existing validation decision.
Proven comment-only SQL changes still skip this analysis.

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success, intentional non-Omni skip, or explicitly nonblocking onboarding advisory. Check policy/completeness; advisory is not a verified pass. |
| `1` | Validation or policy gate failed. |
| `2` | Configuration or discovery error. |
| `3` | Authentication or authorization error. |
| `4` | Omni API error. |
| `5` | Security policy violation. |
| `6` | Unexpected internal error. |

If the issue remains, follow [Support](../SUPPORT.md) and share only redacted public evidence.
