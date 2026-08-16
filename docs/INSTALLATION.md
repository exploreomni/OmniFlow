# Install OmniFlow In A GitHub Repository

This guide takes a repository from no OmniFlow setup to its first validated pull request. OmniFlow is open-source software that runs entirely in the adopter's GitHub Actions account and calls the adopter's Omni tenant directly. There is no OmniFlow service to install in Omni. Review the [limitations and support boundary](LIMITATIONS.md) before installation.

## Before You Start

Confirm that you have:

- An Omni model connected to the target GitHub repository through Omni Git integration.
- Omni Branch Mode enabled for the model.
- Permission to add repository files, GitHub Actions secrets, and branch protection rules.
- A dedicated Omni service user that can read model branches and YAML, validate the model and content, and retrieve content metadata.
- The repository's protected base branch name, usually `main`.

The optional dbt exposures check requires the additional Omni permission documented by the [dbt exposures API](https://docs.omni.co/api/dbt/get-dbt-exposures). The optional post-deployment dbt sync is installed separately after ordinary pull-request validation is proven.

## Step 1: Collect The Non-Secret Model Details

Collect these values for each Omni model connected to the repository:

| Value | Where it comes from |
| --- | --- |
| `base_url` | The Omni tenant origin, such as `https://company.omniapp.co`, with no page path. |
| `model_id` | The Omni model identifier from the model URL, Models API, or an Omni administrator. |
| `model_path` | The repository folder configured for that model in Omni Git integration. Use `.` only when the model files live at the repository root. |
| `base_branch` | The protected Git branch Omni targets, usually `main`. |
| `web_url` | The GitHub repository URL. |

You do not configure a per-pull-request branch name. OmniFlow discovers it from the GitHub pull request and resolves the corresponding Omni branch through the API.

## Step 2: Add Trusted Model Metadata

Create `.omni/flow.json` in the customer repository. Start from [the checked-in example](../.omni/flow.example.json):

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

For multiple models, add one object per model. Every `model_id` and `model_path` must be unique.

This file contains routing metadata, not credentials. Commit it to the protected base branch before testing an Omni-created pull request. OmniFlow deliberately reads this file from the trusted base branch so a pull request cannot redirect the API key to another host.

## Step 3: Add The GitHub Workflow And Optional Policy

1. Create `.github/workflows/omniflow.yml` in the customer repository.
2. Copy the complete [workflow example](../.github/workflow-examples/omniflow.yml) into that file.
3. Replace both occurrences of `<pinned-commit-sha>` with the same reviewed, full 40-character OmniFlow commit SHA.
4. If the protected branch is not `main`, update `branches: [main]` in the workflow.
5. Optionally copy [`.omniflow.example.yml`](../.omniflow.example.yml) to `.omniflow.yml` and review every changed gate with the model owners.
6. If GitHub Code Security is enabled for the repository, add a repository Actions variable named `OMNIFLOW_UPLOAD_SARIF` with the value `true`. Leave it unset otherwise; `report.sarif` is still included in the public evidence artifact.

The action reference should look like this:

```yaml
uses: exploreomni/OmniFlow@0123456789abcdef0123456789abcdef01234567
```

Do not use `@main`, a floating tag, or an unpinned GitHub branch. OmniFlow does not currently publish an official PyPI package. The supported action installs from the pinned checkout with a hash-locked Python 3.11 Linux dependency set.

The example workflow uses `pull_request_target` so it can read policy from the trusted base branch. It never checks out or executes proposed pull-request code. A credential-free preflight selects the Omni model context before the action exposes `OMNI_API_KEY` to the validation process, so non-Omni pull requests skip without injecting the token. Do not add a pull-request-head checkout or execute scripts from the proposed branch in this privileged workflow.

## Step 4: Add Ownership And Protect The Base Branch

Protect the repository before storing an Omni credential:

1. Add CODEOWNERS entries for `.github/workflows/omniflow.yml`, `.omni/flow.json`, and `.omniflow.yml` using a trusted maintainer team.
2. Require pull requests for the base branch.
3. Require the customer's normal approval count and CODEOWNER review.
4. Block force pushes and branch deletion.
5. Require existing test and security checks. Add the OmniFlow check after its first successful run makes the check name available.

Example CODEOWNERS entries:

```text
/.github/workflows/omniflow.yml @company/analytics-platform
/.omni/flow.json @company/analytics-platform
/.omniflow.yml @company/analytics-platform
```

Do not continue to the secret step until these protections are active. A user who can change a privileged workflow can otherwise redirect or expose its repository secrets.

GitHub may require a Team or Enterprise organization plan to enforce rulesets or classic branch protection on private repositories. If GitHub shows that rules will not be enforced, do not represent OmniFlow as a production merge gate. Controlled testing may continue only with tightly limited collaborator access and explicit acceptance of this temporary limitation.

## Step 5: Merge The Setup Into The Base Branch

The trusted workflow and `.omni/flow.json` must already exist on the protected base branch before they can validate an Omni-created pull request. Merge the setup through the repository's ordinary review process.

The initial setup pull request may not run OmniFlow because the trusted workflow does not exist on the base branch yet. This is expected.

## Step 6: Create And Store A Dedicated Omni PAT

Create a personal access token for the dedicated Omni service user. Do not use an Organization API Key: Omni documents that organization keys have Organization Admin permissions, while PATs inherit their user's permissions. Omni tokens do not expire automatically, so establish an explicit rotation schedule and revoke the token immediately after suspected exposure or maintainer access changes. See [Omni API authentication](https://docs.omni.co/api/authentication).

In the customer repository:

1. Open **Settings**.
2. Open **Secrets and variables**, then **Actions**.
3. Select **New repository secret**.
4. Name the secret `OMNI_API_KEY`.
5. Enter the dedicated least-privilege PAT and save it.

The workflow passes this secret through the action's `omni-api-key` input. The composite action exposes it only to the validation step, after hash-verified installation completes.

Never put the token in `.omni/flow.json`, `.omniflow.yml`, a workflow file, a pull request, or an issue. Organization-level GitHub secrets also work when access is explicitly limited to the customer repository.

## Step 7: Run The First Omni Pull Request

1. In Omni, create a model branch.
2. Make a harmless semantic change that is easy to review, such as adding a missing description.
3. Use Omni's **Create pull request** flow.
4. Open the resulting GitHub pull request.
5. Confirm the **OmniFlow** workflow starts.
6. Review the OmniFlow pull-request comment, annotations, and public evidence artifact.
7. Confirm the run selected the expected model and Omni branch.

Expected behavior:

- An Omni change runs model, content, lint, diff, and downstream contract checks.
- A non-Omni pull request finishes successfully with a `skipped` policy decision.
- An Omni change from a fork fails closed because the secret is intentionally withheld.
- A referenced breaking change fails the check and identifies affected downstream content when Omni returns that metadata.

During controlled alpha, do not make OmniFlow the only merge signal until this live test has passed against the customer's current Omni model and permissions.

## Step 8: Require The OmniFlow Check

After the first successful run:

1. Open the GitHub branch protection or ruleset for the base branch.
2. Add the OmniFlow status check, normally shown as `OmniFlow / omniflow`, as required.
3. Confirm the existing pull-request, approval, CODEOWNER, force-push, and deletion protections remain enabled.
4. Confirm Omni's pull-request webhook is configured so approved merges remain synchronized with Omni.

Core OmniFlow validation validates and reports; it does not merge the pull request or write model YAML. After approval and merge, Omni Git integration performs the configured promotion behavior.

## Step 9: Verify The Installation

The installation is ready for controlled alpha use when all of these are true:

- [ ] `.omni/flow.json` is on the protected base branch.
- [ ] `.github/workflows/omniflow.yml` is on the protected base branch.
- [ ] Both action references use the same full OmniFlow commit SHA.
- [ ] `OMNIFLOW_UPLOAD_SARIF=true` is set only when GitHub Code Security is enabled; otherwise SARIF remains artifact-only.
- [ ] Workflow and trusted metadata CODEOWNERS were active before the credential was stored.
- [ ] `OMNI_API_KEY` is a dedicated least-privilege PAT and exists only in GitHub Actions secrets.
- [ ] The action runs on Linux x86_64 with Python 3.11 and uses its checked-in hash lock unchanged.
- [ ] A real Omni-created pull request selected the expected model and branch.
- [ ] Model, content, lint, diff, and downstream checks completed.
- [ ] Public reports contain no credentials or restricted values.
- [ ] Restricted artifacts remain disabled unless an isolated ephemeral runner has an approved retention need.
- [ ] The required status check and reviewer approvals are configured.
- [ ] Omni's post-merge Git integration behavior has been verified separately.

For setup failures, continue with [Troubleshooting](TROUBLESHOOTING.md). For safe diagnostic sharing, see [Support](../SUPPORT.md).

## Step 10: Optionally Add Post-Deployment dbt Sync

Skip this step when the repository does not deploy dbt. Core OmniFlow validation and downstream dependency checks remain fully functional.

For a dbt repository:

1. Prove the ordinary Omni pull-request workflow first.
2. Confirm the repository has a reviewed production dbt deployment command. A local `dbt build` or CI parse is not a production deployment signal.
3. Add the `deployment.dbt_sync` block from [`.omniflow.example.yml`](../.omniflow.example.yml) with `enabled: true`.
4. Create a protected GitHub environment named `omniflow-production` and require deployment reviewers where appropriate.
5. Create a dedicated Modeler or Connection Admin PAT and store it as the environment secret `OMNIFLOW_SYNC_API_KEY`.
6. Copy [the dbt sync workflow example](../.github/workflow-examples/omniflow-dbt-sync.yml), replace its dbt command, pin the OmniFlow commit, and tailor `push.paths` to the actual dbt project.
7. Keep the OmniFlow step after the production dbt command so shell failure prevents synchronization.
8. Run a controlled deployment and verify `dbt-sync.json`, post-sync validation, and the Omni model's refreshed metadata.

The command rejects PR events, tags, and branches other than each model's trusted `base_branch`. Its token is separate from `OMNI_API_KEY`, and its workflow should have only `contents: read` unless the dbt deployment itself requires more.

If Omni's **Branch based schema refresh** setting is enabled, the API requires an existing Omni `branch_id`; read [the branch-based behavior and promotion limitation](DBT_SYNC.md#branch-based-schema-refresh) before enabling automation. Also restrict the workflow trigger to dbt source paths so model commits generated by Omni cannot retrigger the deployment.

## Step 11: Do Not Install AI Repair Yet

The repository contains an AI Repair development scaffold, but it is not supported for customer installation. Omni's documented AI Jobs API may execute queries and does not document the Modeling Agent mutation behavior required by the workflow.

The future design in [OmniFlow AI Repair Development Scaffold](AI_REPAIR.md) requires:

1. Create the protected `omniflow-ai-repair` GitHub environment with required reviewers.
2. Store the separate `OMNIFLOW_REPAIR_API_KEY` as an environment secret.
3. Enable `repairs.ai` with explicit query-execution acknowledgement.
4. Add the pinned `.github/workflows/omniflow-ai-repair.yml` workflow.
5. Create the exact `omniflow-ai-repair` authorization label.
6. Prove repair, rollback, PR update, and independent rerun behavior on a non-production model.

Keep `repairs.ai.enabled: false`, do not install the repair workflow, and do not create a repair PAT. Ordinary validation remains fully supported without AI Repair.
