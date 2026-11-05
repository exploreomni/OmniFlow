# Fix an OmniFlow check

Start with the primary blocker on the PR or the first failed setup step in Actions. Open the guide beside the code for that finding. A completed comparison can identify a new validation issue. **Incomplete** means the check still needs evidence to decide what the PR changes. **Informational** findings describe health or policy observations. Use the PR's blocking findings to decide what to fix first.

<a id="guide-file-contents"></a>
## A saved model file differs from Git

`OF-CANDIDATE-CONTENT` describes a mismatch found by the full-model check, including older Action pins that compared the whole inventory for ordinary PRs. Start with the **Fix** in the annotation. With current PR source verification, safely provable representation differences such as a final newline alone can be equivalent. A materially unsynchronized changed file has a separate code, [`OF-PR-CHANGE-NOT-SYNCHRONIZED`](#guide-compare-branches). If the authored definition differs, review both copies before choosing the intended version.

For an eligible private run, open **Actions → the failed run → Summary → Content mismatch**. The card identifies the matched Git and Omni files and the first differing line. For an internal or public repository, or if the card is unavailable, use the local command below. The filenames and source text stay out of shareable annotations. If the run stopped at file 379 of 1,253, 378 matched byte for byte and 874 still need comparison. That run establishes **at least one** mismatch.

1. In the failed GitHub run, identify the PR and the head commit for that event. For a base-scope finding, also identify the event's target commit. The **OmniFlow Action revision** identifies the tool itself.
2. For branch scope, ask an Omni Organization Admin to open the shared model's **Model → Advanced → Webhook Deliveries**, expand the PR delivery, and compare its commit with the run. Wait if delivery is queued or syncing. Open the delivery error if it failed. If the delivery is marked **Not started** or **Stalled**, follow Omni's redelivery procedure. For base scope compare the shared model with the target commit. [Omni webhook delivery instructions](https://docs.omni.co/integrations/git/webhook-deliveries)
3. If that commit already synced, locate the difference on an authorized workstation. Start from a clean, trusted clone of the customer repository at the current PR base commit. Fetch the PR head as a Git object. Install the same reviewed OmniFlow revision as the workflow. Set `OMNI_BASE_URL` to the intended Omni HTTPS tenant and supply `OMNI_API_KEY` and `OMNIFLOW_GITHUB_TOKEN` through your local secret manager. The GitHub token needs read access to the repository and PR. Supply the observed commits independently.

```sh
omniflow candidate diagnose \
  --repository "$REPOSITORY" --pr "$PR_NUMBER" \
  --head-sha "$PR_HEAD_SHA" --base-sha "$PR_BASE_SHA" --scope branch
```

Set `REPOSITORY` to the GitHub owner/repository, `PR_NUMBER` to the PR number, and both SHA variables to full 40-character commits from the failed event. Use `--scope base` for a base-scope finding. With multiple model registrations, add `--model-path "exact/registered directory"`. If your workflow uses a custom policy, pass that same repository-relative path with `--config`. Read the policy from the trusted base.

Use this full-model diagnostic to investigate a full-model comparison. It compares the currently saved Omni model with immutable Git objects for an open, same-repository PR targeting the default branch. A difference outside the PR's changed files can appear here even when the changed-file gate passes. It accepts version 1 registrations. For a version 2 environment registry, set `OMNIFLOW_ENVIRONMENT` to the registered environment and use that environment's credential and `OMNI_BASE_URL`. The diagnostic checks the live model's Git configuration. If the PR moved, start a fresh validation run and use its commits. For a non-default target or an unresolved environment binding, follow the governed workflow for that environment and give the maintainer its run reference. An older live Omni state cannot be reconstructed from a Git SHA.

Read the paired Git and Omni paths, the first differing line, and the classification on that workstation. Review the intended change in the corresponding file. A formatting classification still requires judgment when the YAML contains SQL block text. Save or sync the intended definition in the correct direction. Then validate the current commit. **Recovered** means candidate verification completes for that scope. Other checks can now report genuine regressions.

For an authored text difference, add `--show-diff` to that command in an approved interactive terminal to see a short Git/Omni excerpt at the first differing line. Source can contain customer data. Use this option only on an authorized device and keep the preview out of pasted logs and tickets. The default output omits source. For newline differences the direction or line-ending style already describes the change.

Internal and public repositories receive a shareable report without model file paths. The local command does not upload its findings. A private repository can also have a separate detailed artifact when the run confirms publication. A trusted `reporting.diagnostics.mode: minimal` setting disables private paths both in artifacts and in the run summary. If you need support, provide the diagnostic code, run reference, scope, delivery outcome and classification through your approved channel. Keep source text local.

<a id="guide-compare-branches"></a>
## Omni and Git selected different files, branches, or revisions

**`OF-PR-CHANGE-NOT-SYNCHRONIZED` — A changed Omni file does not match this PR revision.** If a topic was added, check that the shared model lacks it and the PR's Omni branch contains the saved definition. For a deleted topic, check the opposite. For an edit, check the definition on the scope named in the finding against the corresponding Git commit. Verify synchronization for that commit, then rerun. Recovery means the changed-file check completes and the validators can compare actual PR impact. Other model or dashboard findings may then require separate action.

1. Check whether the finding is for the PR head or the target branch. For `OF-CANDIDATE-BRANCH` or `OF-BRANCH-NOT-FOUND`, verify that the PR has a corresponding saved Omni branch for the registered model.
2. For `OF-CANDIDATE-FILES`, inspect Git-only and Omni-only counts. Compare the exact registered model path and authored file names on both sides. For ambiguous identities, preserve database and schema qualification on views. Topic group folders alone do not identify a view. For `OF-CANDIDATE-DRIFT`, finish pending changes and synchronization before starting another run.
3. Rerun on the current PR commit. This step is recovered when the selected artifacts can be verified. [Candidate troubleshooting](TROUBLESHOOTING.md#candidate-verification-git-and-omni-snapshots-differ)

<a id="guide-setup"></a>
## The workflow could not start or select a model

1. Start with the first failed step. `OF-BOOTSTRAP-RUNTIME` needs a Linux x86_64 runner and Python 3.11. For `OF-BOOTSTRAP-INSTALL`, inspect the failed dependency command and runner connectivity. For `OF-SETUP-ACTION-MODE`, correct the mode input in the workflow and use `skip-reason` only with validation.
2. For `OF-MODEL-METADATA-MISSING`, check the PR **target branch** for the repository-root `.omni/flow.json`. For `OF-MISSING-POLICY-CONFIG`, check the configured policy path (default `.omniflow.yml`). For `OF-SETUP-MODEL-PATH`, compare the registered directory with the actual model folder, including case and spaces. For a parse or type error, correct the reported key or line using the expected type. For a missing secret, a repository admin should confirm the named Actions secret is available to the corresponding trusted workflow step.
3. Apply the correction through the target branch's review process, then validate a fresh PR event. The setup step should complete and routing should select the intended model. [Setup instructions](../README.md)

<a id="guide-api-access"></a>
## The service rejected access

1. Identify the service, operation and credential used by that step. A 401 calls for checking the credential's validity. A 403 calls for verifying access to that operation on the selected model, branch or Git resource.
2. Ask the administrator of that service to verify the specific access, actor and scope. If the API returned a 404, check both the target identity and access. An API status alone cannot identify the missing permission.
3. Repeat the original read with the intended identity. If a job or write may already have started, check its status first. Recovery means the original operation succeeds and the check receives evidence for the intended scope.

<a id="guide-api-response"></a>
## The check did not receive a complete response

1. Find the service, operation, response category and attempt count. For a timeout or service failure, check runner connectivity and service status. For a rate limit, respect the retry window.
2. If the response was empty, unreadable or incomplete, ask the service or OmniFlow maintainer to inspect the operation using the run reference. For a job creation, refresh or write, reconcile the existing operation before retrying.
3. Rerun the read when service and access are healthy. The check needs a complete result, including the baseline when it compares changes.

<a id="guide-model-validation"></a>
## Omni found a model issue

1. Read the scope, comparison status and state. For a *new* blocking finding, open that same model branch in Omni and run model validation.
2. Review the definition and references identified by Omni. Correct the affected view, topic, model or relationship on the branch and save it. If the finding is an existing nonblocking issue, track it as maintenance. If the run says *health only* or the baseline is unavailable, resolve the primary evidence blocker first.
3. Synchronize the corrected branch and rerun. Confirm the comparison completes and the finding is resolved or the new count decreases as expected.

<a id="guide-content-validation"></a>
## A query, dashboard filter, or content comparison needs attention

1. Open **Content Validator** on the model and branch recorded in the finding. For a new query or dashboard-filter issue, inspect the affected object's field references and filter configuration.
2. Compare against the same access scope on the target. For a draft identity or baseline error, first establish that the validation identity can read the draft and its published parent. Use the code-specific action for denied access, missing objects or conflicting identity.
3. After correcting the reference or restoring access, rerun. Confirm a complete comparison before attributing a finding to the PR. Existing health observations can be triaged separately.

<a id="guide-semantic-changes"></a>
## A semantic change or lint rule needs review

Open the named rule or change type, then review the matching model definitions and policy. For example, a primary-key finding requires checking the view's actual grain before declaring a key. Save the intended definition and rerun the comparison. Confirm the expected new or resolved count for this rule.

<a id="guide-dependencies"></a>
## A contract or dependency check needs attention

Review the affected change in the model and the downstream coverage reported for the same scope. If the search was incomplete, restore access or correct the mapping first. Then check the dependent contract or content and rerun. Success means the check completed and the required contract holds.

<a id="guide-dbt-impact"></a>
## dbt impact or exposure mapping is incomplete

Compare the changed dbt relation or column with its registered Omni view. For an orphaned field, keep the upstream column available until the dependent content migrates, or update both sides in the reviewed deployment sequence. For a coverage gap, fix the named mapping or source access and rerun. Confirm the affected dashboards and remaining coverage gaps. [dbt impact procedures](TROUBLESHOOTING.md#dbt-impact-analysis)

<a id="guide-dbt-sync"></a>
## A dbt refresh is incomplete

Open the original refresh in Omni and establish its status before starting another job. Correct a confirmed job failure or wait for completion. Rerun post-sync validation and confirm its own result. A completed refresh and a successful validation are separate outcomes. [Post-deployment troubleshooting](TROUBLESHOOTING.md#post-deployment-dbt-sync-results)

<a id="guide-ai-evaluation"></a>
## An AI evaluation needs attention

Check the prompt set, target model, and evaluation run from the recorded operation phase. Reconcile pending evaluation or cleanup work. For a score regression, inspect the prompt's base and branch results. Run a fresh evaluation only after the previous run's status is known. Confirm comparison and cleanup both completed.

<a id="guide-query-validation"></a>
## A query result or assertion needs review

Open the numbered contract and assertion in the trusted policy. Check its grain, fixed window and expected value, then compare the same scope on both sides. Reconcile pending query jobs before another execution. Rerun and confirm the specific assertion outcome and coverage. [Query validation](QUERY_VALIDATION.md)

<a id="guide-deployment-hold"></a>
## A breaking change placed deployment on hold

Review the last successful dbt sync revision and any pending deployment. Complete the required deployment and request current-head revalidation. The readiness check must release the hold on the intended commit. [Hold troubleshooting](TROUBLESHOOTING.md#breaking-change-hold)

<a id="guide-repair"></a>
## A repair or rollback needs review

Find the original repair attempt and reconcile its writes and rollback state. If it needs manual review, ask the model owner to inspect the saved branch before starting a new attempt. Verify post-repair validation and the final commit or rollback result. [Repair statuses](TROUBLESHOOTING.md#ai-repair-results)

<a id="guide-publish-reports"></a>
## A report or PR comment is missing

Open the workflow's reporting, upload and publish steps for this run. Check whether the expected artifact was generated and whether upload succeeded. Verify the publisher can comment on this PR. A newer run can supersede an older one. Recovery means the current run's summary or confirmed artifact is available. [Report delivery troubleshooting](TROUBLESHOOTING.md#no-pull-request-comment)

<a id="guide-get-help"></a>
## The cause needs investigation

Open the first failed workflow step. Record its operation, last known status, diagnostic code, Action revision, and run reference when present. Send those safe identifiers to the OmniFlow maintainer through an approved support channel. If an operation could have written to another system, establish its final status before retrying. Verify recovery by rerunning the same affected check. [Support](../SUPPORT.md)

## Reading the JSON report

Ordinary findings include `recovery_steps`, `verify_recovery`, `support_action`, `outcome_label`, and `guide_anchor`. Large shareable reports can store identical guidance once instead. Look up a finding's `recovery_ref` in the report's top-level `recovery_catalog`. Near the publisher's size limit, the JSON can omit those additional fields. The code, reason, scope and policy decision still identify the finding, and the PR publisher reconstructs its guidance from those fields. Use the Markdown report to read the numbered steps.
