# OmniFlow AI Repair Development Scaffold

> **Release blocked:** Do not install or enable this feature for customer use.
> Omni's public Modeling Agent documentation describes model editing in the
> Model IDE, but Omni does not currently document an API for invoking that agent
> or applying its proposed YAML changes to a branch. The documented AI Jobs API
> is query-oriented. This implementation is retained for maintainer development
> and must not be represented as a supported repair integration.

The intended workflow is an optional, human-authorized repair for a failed Omni-created pull request. An authorized maintainer applies a label, a protected GitHub environment requires human approval, and OmniFlow asks Omni's Modeling Agent to make a narrow correction on the existing Omni development branch. The missing public API contract prevents this intended workflow from being released today.

AI Repair does not approve, merge, or deploy a pull request. The normal validation workflow remains read-only and does not require this feature.

## Important API Disclosure

Omni's documented [Create AI job API](https://docs.omni.co/api/ai/create-ai-job) says an AI job may generate and execute queries. That endpoint does not document a no-query or review-only switch. Its status response may include a result summary containing data values, and the result stream can include query data.

OmniFlow:

- Does not call the AI result-stream endpoint.
- Discards the prompt, result summary, progress, error body, chat URL, and raw AI payload after extracting job ID and state.
- Does not capture or persist raw query results.
- Instructs the agent not to run data queries, but does not represent that instruction as an enforceable API control.
- Requires `repairs.ai.allow_query_execution: true` as an explicit administrator acknowledgement.

Omni's [AI data security documentation](https://docs.omni.co/ai/security) governs how prompts and semantic metadata are processed by the tenant's configured AI provider. Maintainers must review that policy before any non-production investigation.

## Maintainer-Only Prerequisites

Do not follow these steps in a customer repository. They are retained to document the controls required for a future supported implementation. Maintainer testing also requires:

- A same-repository Omni pull request. Fork pull requests are never eligible.
- Omni Git integration and an existing Omni branch that matches the GitHub PR head branch.
- A dedicated Omni user limited to the target model, with the minimum role that can use Omni AI, edit the development branch, and update its Git pull request. A Modeler-scoped user is the conservative development starting point; verify the exact custom-role permissions in a test tenant.
- A separate PAT from that user. Do not reuse `OMNI_API_KEY` and do not use an Organization API Key.
- Permission to create a GitHub environment, environment secret, label, workflow, and CODEOWNERS rules.

## Step 1: Create The Protected GitHub Environment

1. Open the non-production maintainer repository's **Settings**.
2. Open **Environments** and create `omniflow-ai-repair`.
3. Add one or more trusted analytics-platform or model-owner reviewers.
4. Prevent self-review when the repository's GitHub plan supports it.
5. Restrict deployment branches to the protected base branch used by the workflow.
6. Keep environment administrators limited to trusted maintainers.

The workflow cannot receive the repair PAT until an environment reviewer approves that individual run.

## Step 2: Add The Separate Repair PAT

Inside the `omniflow-ai-repair` environment, create an environment secret named:

```text
OMNIFLOW_REPAIR_API_KEY
```

This PAT is write-capable and must be managed separately from the read-oriented `OMNI_API_KEY` used by routine validation. Define rotation and revocation procedures, and audit access to the GitHub environment.

Never place either token in `.omniflow.yml`, `.omni/flow.json`, workflow source, a pull-request body, a comment, or a support bundle.

## Step 3: Enable The Explicit Policy

Add the following to `.omniflow.yml` through the protected base-branch review process:

```yaml
repairs:
  ai:
    enabled: true
    allow_query_execution: true
    max_changed_files: 3
    max_changed_lines: 200
    poll_timeout_seconds: 300
```

The limits may be lowered. OmniFlow rejects values outside its built-in safety bounds. Pull-request changes cannot enable or weaken this policy because `pull_request_target` reads it from the trusted base branch.

## Step 4: Add The Repair Workflow

1. Copy [the repair workflow example](../.github/workflow-examples/omniflow-ai-repair.yml) to `.github/workflows/omniflow-ai-repair.yml`.
2. Replace `<pinned-commit-sha>` with the same reviewed full 40-character OmniFlow commit used by validation.
3. Add CODEOWNERS coverage for the workflow and `.omniflow.yml`.
4. Merge the workflow through the protected base branch.

The workflow must retain these controls:

- Trigger only on `pull_request_target` with `types: [labeled]`.
- Require the exact `omniflow-ai-repair` label.
- Require `head.repo.full_name == github.repository`.
- Use the `omniflow-ai-repair` protected environment.
- Keep `cancel-in-progress: false` so GitHub does not terminate a write workflow between mutation and rollback.
- Check out only the trusted base branch with `persist-credentials: false`.
- Pass only `OMNIFLOW_REPAIR_API_KEY` to `mode: repair`.
- Upload only `.omniflow/public` repair evidence.

## Step 5: Create The Authorization Label

Create a repository label named exactly:

```text
omniflow-ai-repair
```

Applying a label normally requires triage or write permission. Treat that permission as authorization to request a repair, while the protected environment remains the second human checkpoint.

## Step 6: Test On A Non-Production Model

Use a non-production model for the entire maintainer test.

1. Create an Omni branch with one small, deliberately fixable model validation error in an existing `.view` or `.topic` file.
2. Use Omni's **Create pull request** flow.
3. Confirm routine OmniFlow validation fails for the expected model error.
4. Have an authorized maintainer apply `omniflow-ai-repair`.
5. Have a different trusted reviewer approve the protected environment run when possible.
6. Confirm the repair workflow identifies the exact model, branch, PR, and head SHA.
7. Review `.omniflow/public/repair.md` and the resulting GitHub pull-request diff.
8. Confirm routine validation reruns on the new commit and passes independently.
9. Remove the label after testing. A new repair for the same head SHA is blocked; a new commit creates a new head SHA and requires a fresh label event and approval.

This test cannot authorize customer use while the required public Modeling Agent API contract remains unavailable.

## Runtime Sequence

1. An Omni-created PR fails ordinary validation.
2. A maintainer applies `omniflow-ai-repair`.
3. GitHub confirms the PR is same-repository and waits for protected-environment approval.
4. OmniFlow reads trusted base-branch metadata and resolves exactly one Omni model and branch.
5. It records one repair attempt for the exact PR head SHA in a bot-authored PR comment.
6. It fetches authored YAML with checksums into memory and maps current model errors to existing files.
7. It submits one bounded metadata-only prompt to the Omni AI job API.
8. It polls only job state. On timeout, it requests cancellation before any rollback.
9. It fetches authored YAML again and rejects additions, deletions, unrelated files, oversized changes, secrets, SQL, data-source, access, visibility, ownership, and governance changes.
10. It forces model validation, then reruns configured content validation, semantic lint, semantic diff, downstream contracts, and optional dbt exposures.
11. It rechecks that the branch did not change during validation.
12. It updates the Git-connected branch using `require_branch_exists: true` only after all gates pass.
13. GitHub shows the resulting commit for human review. Ordinary required checks and approvals still govern merge.

## Failure Outcomes

| Status | Meaning | Operator action |
| --- | --- | --- |
| `not_needed` | No model validation errors exist. No AI job or write ran. | Remove the label and continue review. |
| `committed` | The repair passed every gate and updated the Git-connected branch. | Review the actual PR diff and independent rerun before approval. |
| `rolled_back` | The AI job or a post-write gate failed; the original authored YAML was restored and verified. | Review public evidence, correct manually, or create a new commit before another request. |
| `manual_review_required` | Cancellation or Git commit outcome was ambiguous, so OmniFlow refused an unsafe overwrite. | Inspect the Omni branch and Git PR before any rerun or merge. |
| `rollback_failed` | The branch changed concurrently or exact restoration could not be verified. | Stop merge/deploy activity and reconcile the Omni branch manually. |

## Known Development Limits

- The AI Jobs API has no documented no-query switch.
- The Modeling Agent's exact API mutation behavior must be verified live for each tenant configuration.
- Omni's YAML delete endpoint has no checksum precondition. OmniFlow rejects file additions, re-reads an added file immediately before cleanup, and verifies the complete snapshot after rollback, but a narrow concurrency race remains.
- `require_branch_exists: true` is documented as Git branch update-only mode. A maintainer test must confirm the returned PR destination and existing-PR update behavior for the test Git provider configuration.
- A transport failure while creating an AI job or committing to Git can be ambiguous. OmniFlow does not retry those writes and requires manual review instead of guessing.
- One attempt per head SHA is enforced with workflow concurrency and a bot-authored PR comment. Maintainers should protect deletion or editing of automation comments through normal repository access controls.
- AI Repair handles model validation errors only. It does not autonomously rewrite dashboards, reports, content references, dbt projects, or warehouse SQL.

## Disable AI Repair

1. Remove `.github/workflows/omniflow-ai-repair.yml` from the protected base branch.
2. Set `repairs.ai.enabled: false` or remove the `repairs` section.
3. Delete the `OMNIFLOW_REPAIR_API_KEY` environment secret and revoke the Omni PAT.
4. Delete the `omniflow-ai-repair` label.
5. Retain ordinary OmniFlow validation and its read-oriented `OMNI_API_KEY`.
