# Environment-scoped PR validation

Unreleased, opt-in enhancement for issue #25. Version 1 configuration remains supported.
Version 2 adds **validation targets**, not a multi-environment deployment system.

## Why targets exist

A development leader and production follower are distinct Omni models. They may use
different Omni hosts but share the same Git model folder. A feature PR into `develop`
must validate development; a release PR into `main` must validate production's
corresponding release branch, never development or the unchanged production base.

Omni documents this workflow, matching leader/follower model paths, and enabling
**Always create branches** on followers in its
[follower-mode guide](https://docs.omni.co/integrations/git/follower-mode/best-practices).
OmniFlow does not configure these settings or refresh follower schemas for you.

## 1. Register the actual targets

Review the real model IDs, hostnames, repository URL, base branches and model path
with the model owner. Do not assume the same model ID represents both environments.
Commit this complete non-secret registry to **both protected base branches** through
reviewed bootstrap PRs before enabling version 2 validation:

```json
{
  "version": 2,
  "models": [
    {
      "environment": "development",
      "base_url": "https://dev.example.omniapp.co",
      "model_id": "development-model-id",
      "model_path": "omni/shared",
      "base_branch": "develop",
      "git_follower": false,
      "web_url": "https://github.com/company/analytics"
    },
    {
      "environment": "production",
      "base_url": "https://prod.example.omniapp.co",
      "model_id": "production-model-id",
      "model_path": "omni/shared",
      "base_branch": "main",
      "git_follower": true,
      "web_url": "https://github.com/company/analytics"
    }
  ]
}
```

- Environment names use lowercase letters, digits and hyphens, starting with a letter.
- Each environment identifies one exact base branch and Omni host. A base branch
  identifies exactly one environment and host. Wildcard routing is not supported.
- Within a target, IDs are unique and model folders cannot overlap. Distinct affected
  folders can still route to multiple models. Folders can repeat across target branches.
  Prefer a dedicated model directory: every YAML file under it participates in candidate
  comparison, including when `model_path` is `.`. Unrelated YAML in that scope blocks
  exact inventory equivalence; move to a dedicated registered directory before adoption.
- `base_branch`, `web_url` and boolean `git_follower` are required in version 2;
  `git_provider`, if supplied, must be `github`.
- PR markers are hints within the selected target. A development marker copied to a
  production release PR must be removed or corrected. Markers cannot select another
  environment or omit other affected models.
- Version 2 requires `--auto`. Remove legacy explicit host, model, path or branch
  overrides from policy, environment variables and CLI arguments.
- There is no initialization command: bootstrap is a reviewed registry/workflow edit.
  `doctor --auto` checks access and settings; it does not create registrations.

Keep the complete registry synchronized across the protected branches. A release
promotion must not replace it with a development-only file or undo the production
validation policy. PR-head changes cannot alter the registry used for that run.

## 2. Bind credentials to the selected environment

Create GitHub environments named `development` and `production`. Store a separate,
least-privilege validation PAT as `OMNI_API_KEY` in each environment. Do not place one
cross-organization credential in a repository-wide secret. Keep production approvals
and branch protections enabled. These are human setup steps, not changes OmniFlow makes.

Copy the [two-environment workflow example](../.github/workflow-examples/omniflow-environments.yml)
and replace the action placeholder with the reviewed release's full commit SHA.
The trusted workflow selects the GitHub environment from the PR base branch and passes
`validation-environment` to the action. The credential-free routing step runs first.
The validation process then requires `OMNIFLOW_ENVIRONMENT` to match the selected
registry target **before constructing an Omni client**. This binding cannot determine
a token's actual permissions; the target's API must authorize access, and credential
scope must be verified during setup. A wrong or underprivileged credential fails closed.

For a local settings check, check out the appropriate trusted base branch and supply
that environment's credential through your secret manager, then run:

```sh
OMNIFLOW_TARGET_BRANCH=main OMNIFLOW_ENVIRONMENT=production omniflow doctor --auto
```

Doctor needs read access to the selected model, branches and
[Git configuration](https://docs.omni.co/api/model-git-configuration/get-git-configuration).
Unlike version 1, version 2 cannot skip unavailable Git identity evidence. It compares
the exact base branch, model path, follower setting, GitHub provider and repository;
followers must have `branchPerPullRequest=true`. Doctor success is not PR validation.

## 3. Prove and validate the release candidate

1. Use the trusted PR base to select targets; route changed files at immutable PR revisions.
2. Resolve exactly one matching Omni branch under the selected base model. Missing,
   duplicate or inaccessible branches block validation; there is no base-model fallback.
3. Read the candidate's authored YAML using the documented
   [model YAML endpoint](https://docs.omni.co/api/models/get-model-yaml), with
   `mode=combined`, the resolved `branchId` and `fullyResolved=false`.
4. Compare the complete authored semantic-file inventory and exact UTF-8 contents with
   the PR head's Git snapshot. Git data is read at an immutable commit, never checked out
   or executed. Local Git objects or bounded, read-only GitHub tree/blob requests supply
   evidence. Missing, additional, deleted, unsafe or different files block validation.
5. Run configured model, content and semantic checks against that selected candidate.
   Content and semantic baselines still use the same environment's base model.
6. Compare the candidate again after checks. Publish environment, target branch,
   candidate branch, immutable head, file count, digest and verification status.

This is deliberately **exact authored-content comparison**, not a Git-commit claim
from an undocumented Omni field. Formatting or path representation differences also
block verification; do not disable the safeguard to force a green check. Diagnose the
sanitized difference and confirm the exporter/Git representation with the model owner.
Sampled equality before and after checks is not an atomic snapshot lock: keep candidate
editing controlled during validation, require up-to-date PR checks, and rerun after changes.
Inherited schema/runtime equivalence and warehouse readiness require separate acceptance.

If branch creation or synchronization is delayed, confirm Omni received the webhook
and the follower branch exists, then rerun the current PR once synchronization is confirmed.
OmniFlow does not retry indefinitely, create branches, sync Git, write models or merge PRs.
Repeated failure needs investigation, not repeated blind reruns.

## Boundaries, adoption and rollback

- Version 2 blocks **dbt synchronization, AI repair and deployment-readiness/hold release**.
  Its validation policy must leave `deployment.dbt_sync.enabled` and
  `deployment.breaking_change_hold.enabled` false (their defaults).
  A repository-wide `OMNIFLOW_LAST_SYNC_SHA` cannot prove readiness for a specific target.
  If your process requires the existing hold workflow, stay on version 1 until a reviewed
  environment-scoped deployment-state design is available; do not simply remove its gate.
- Version 1 single-target behavior remains available. Version 2 requires a release
  supporting this schema; older pinned releases reject it. Tenants returning reverse
  `viewNames` metadata also require the metadata compatibility fix for issue #24 in
  their selected release.
- Before adoption, run a controlled development and follower PR smoke test using the
  real IDs, credential scopes and Git settings. Verify one successful current candidate,
  one intentionally unsynchronized candidate that blocks, and correct public reports.
  Local tests establish development evidence, not production or customer acceptance.
- Roll back the action pin, trusted registry and workflow together through review. The
  prior version 1 configuration does not gain follower routing by rollback; disable
  promotion through that route or retain a separate validated manual gate. Never leave
  a required check silently skipped to make a release mergeable.
