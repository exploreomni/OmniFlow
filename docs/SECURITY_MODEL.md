# OmniFlow Security Model

This document describes the supported installation boundary, protected data, expected threats, and operator responsibilities for OmniFlow's controlled alpha.

## Supported Installation

The supported customer path is the pinned GitHub Action in [the installation guide](INSTALLATION.md):

- GitHub-hosted `ubuntu-latest` on x86_64.
- Python 3.11 from a full-SHA-pinned `actions/setup-python` step.
- A full 40-character OmniFlow action commit SHA.
- OmniFlow's checked-in, hash-verified action dependency lock.
- A dedicated least-privilege Omni PAT supplied through the action's `omni-api-key` input.

The optional dbt sync path is supported only inside the same protected production job that successfully deploys dbt, or a reviewed manual deployment. It requires `deployment.dbt_sync.enabled`, a protected environment, the separate `sync-api-key` action input, a base-branch `push` or `workflow_dispatch` event, and exact `base_branch` agreement with trusted metadata.

OmniFlow does not currently publish an official PyPI package. A floating branch, floating tag, third-party package, modified dependency lock, or workflow that executes pull-request-head code with the Omni token is outside the supported security boundary.

The AI Repair development scaffold is outside the supported customer security boundary. It must remain disabled until Omni publishes the required Modeling Agent API contract. Its proposed controls are documented in [the AI Repair guide](AI_REPAIR.md).

## Trust Boundaries

The privileged `pull_request_target` workflow checks out the trusted base revision. OmniFlow reads `.omni/flow.json` and `.omniflow.yml` from that base revision and obtains changed filenames through GitHub's API. It does not check out or execute the proposed pull-request revision.

The pull-request description is untrusted. An optional marker may select a model, path, and branch only when each value agrees with trusted metadata and GitHub event context. It cannot supply an Omni host.

Non-Omni pull requests return a successful `skipped` decision. Fork pull requests do not receive the Omni PAT. Fork changes to Omni files fail closed and require a maintainer-controlled same-repository branch for full validation.

## Credentials

The GitHub workflow passes a dedicated Omni PAT as an action input. After installation, a credential-free route step reads only trusted metadata and GitHub changed-file context. The composite action exposes the PAT as `OMNI_API_KEY` only when that route selects at least one Omni model context. Install, routing, and skipped-report steps do not receive the token as an environment variable.

The unreleased AI Repair scaffold uses a different `OMNIFLOW_REPAIR_API_KEY` during maintainer testing, exposed only to the repair process after protected-environment approval. It must never reuse the routine validation PAT. A dedicated Modeler-scoped user limited to a non-production model is the conservative development starting point; any narrower custom role remains unverified.

Post-deployment dbt sync uses `OMNIFLOW_SYNC_API_KEY`, exposed only to the `dbt-sync` action mode. Use a dedicated PAT with the Modeler or Connection Admin permission required by Omni's schema refresh endpoint plus read access required by enabled post-sync checks. Pull-request validation and AI repair action modes never receive this token. Customers may technically map the same GitHub secret to multiple inputs, but separate identities are the recommended least-privilege boundary.

Omni documents that Organization API Keys have Organization Admin permissions. Use a PAT from a dedicated user whose access is limited to the models and content being validated. Omni tokens do not expire automatically, so maintainers must define rotation, revocation, and access-review procedures.

OmniFlow rejects secret-like configuration keys and environment expansion of secret-like names. Logs, errors, public reports, Markdown, annotations, and support guidance redact credentials and common sensitive metadata.

## Network And Data Handling

OmniFlow sends authenticated requests only to the HTTPS origin in trusted metadata. Redirects are disabled, TLS verification remains enabled, request timeouts are bounded, retries apply only to throttling and transient server errors, and response bodies are never included in API errors.

The normal orchestrated validation client uses read-only GET operations. It does not query a warehouse, capture query results, merge pull requests, or write model YAML to Omni.

The opt-in dbt sync client sends one non-idempotent POST to start a schema refresh and bounded GET requests to poll its job. The POST is never retried because a lost response could otherwise create duplicate jobs. The command rejects pull-request events, tags, unsupported GitHub events, and deployment branches that differ from trusted `base_branch`. It persists only normalized job metadata and never stores raw responses or warehouse results.

Omni Git integration may commit system-generated refresh changes or require a separate pull request, depending on the connection's branch-based refresh and Git system-sync settings. Workflow path filters must include dbt sources and exclude generated Omni model paths to prevent refresh loops. OmniFlow does not approve or merge a system-sync pull request.

The optional AI Repair path uses documented AI-job, YAML-write/delete, and Git-commit endpoints against an existing development branch. Omni documents that AI jobs may execute queries and does not expose a no-query switch on that endpoint. OmniFlow never calls the AI result stream, stores no result summary or raw query data, cancels a non-terminal job before rollback, rejects sensitive or out-of-scope YAML changes, reruns every configured gate, and never approves, merges, or deploys.

Non-idempotent AI creation, YAML mutation, deletion, and Git commit requests are not retried. AI cancellation is documented as idempotent. Ambiguous job creation, cancellation, commit, concurrent edit, or rollback results fail closed for manual review.

API responses, pagination, trusted files, YAML files, YAML totals, nesting, aliases, and model counts have explicit safety limits. Repeated pagination cursors, cyclic YAML aliases, unsafe paths, symbolic-link output targets, unknown policy keys, and ambiguous routing fail closed.

## Artifacts

Public reports remove credentials, raw payloads, emails, document URLs, and folder paths. Strict redaction additionally removes names, owners, labels, and free-text messages.

Restricted YAML, dependency, semantic-diff, content, and contract files are deleted by default. When a local operator explicitly enables retention, OmniFlow creates owner-only directories and files. The official workflow uploads only the public directory and artifact manifest.

AI Repair keeps pre- and post-change YAML snapshots in process memory. Public repair evidence contains metadata and counts only. The AI prompt, authored YAML, AI result summary, chat URL, progress, error body, and query data are not persisted by OmniFlow.

Do not retain restricted artifacts on a persistent or shared self-hosted runner. A self-hosted runner is outside the recommended alpha path unless it is isolated, ephemeral, access-controlled, and cleaned after every job.

## Supply Chain

All first-party workflow actions are pinned to full commit SHAs. The action runtime and release build use exact package versions and SHA-256 hashes, binary wheels only, disabled build isolation, and dependency-free installation of the reviewed checkout.

Repository security automation includes tests, Ruff, Bandit, pip-audit, gitleaks, CodeQL, zizmor, Dependabot, SBOM generation, checksums, and Sigstore signing. GitHub repository rules, CODEOWNERS, private vulnerability reporting, and release-environment approval are operational controls and must remain enabled.

## Operator Responsibilities

- Protect the base branch before adding the Omni PAT.
- Require review from trusted owners of workflows, metadata, policy, dependency locks, and security code.
- Keep the action and every third-party action pinned to reviewed commit SHAs.
- Review Dependabot and code-scanning alerts promptly.
- Rotate and revoke the Omni PAT according to policy.
- Verify the first real Omni-created pull request and post-merge Omni promotion before making the check mandatory.
- Run the first dbt refresh against a controlled non-production target and verify Git side effects before enabling production sync.
- Keep dbt sync after the production dbt command, in a protected environment, and restrict its trigger paths to dbt sources.
- Never upload restricted artifacts or raw Omni responses to a public issue.
- Keep AI Repair disabled until the normal validation workflow has passed a real Omni-created PR.
- Require protected-environment approval for every AI repair and review the resulting PR diff independently.
- Revoke the repair PAT and stop merge activity immediately after `rollback_failed` or an ambiguous write outcome.

## Reporting A Vulnerability

Follow [SECURITY.md](../SECURITY.md). Do not disclose suspected vulnerabilities, credentials, private tenant metadata, or customer YAML in a public issue.
