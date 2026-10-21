# OmniFlow Security Model

This document describes the supported installation boundary, protected data, expected threats, and operator responsibilities for OmniFlow's controlled alpha.

## Supported Installation

The supported customer path is the pinned GitHub Action in [the installation guide](INSTALLATION.md):

- GitHub-hosted `ubuntu-latest` on x86_64.
- Python 3.11 from a full-SHA-pinned `actions/setup-python` step.
- A full 40-character OmniFlow action commit SHA.
- OmniFlow's checked-in, hash-verified action dependency lock.
- An operator-selected Omni Organization API Key or dedicated service-user PAT supplied through the action's `omni-api-key` input, with the scope tradeoff below reviewed before installation.

The optional dbt sync path is supported only inside the same protected production job that successfully deploys dbt, or a reviewed manual deployment. It requires `deployment.dbt_sync.enabled`, a protected environment, the separate `sync-api-key` action input, a base-branch `push` or `workflow_dispatch` event, and exact `base_branch` agreement with trusted metadata.

OmniFlow does not currently publish an official PyPI package. A floating branch, floating tag, third-party package, modified dependency lock, or workflow that executes pull-request-head code with the Omni token is outside the supported security boundary.

The AI Repair development scaffold is outside the supported customer security boundary. It must remain disabled until Omni publishes the required Modeling Agent API contract. Its proposed controls are documented in [the AI Repair guide](AI_REPAIR.md).

## Trust Boundaries

The privileged `pull_request_target` workflow checks out the trusted base revision. OmniFlow reads `.omni/flow.json` and `.omniflow.yml` from that base revision and obtains changed filenames through GitHub's API. It does not check out or execute the proposed pull-request revision.

The root `.omniflow.yml` policy is required and is distinct from `.github/workflows/omniflow.yml`. Bootstrap it on the protected base before PR validation. Missing policy fails with configuration exit code `2`; neither PR-head-only policy nor environment/identity overrides can substitute for it. Customer PR workflows must use repository-relative custom configuration paths to retain the trusted-base lookup. Absolute paths read a local file directly and are reserved for explicitly trusted local use, not proposed PR policy.

The pull-request description is untrusted. An optional marker may select a model, path, and branch only when each value agrees with trusted metadata and GitHub event context. It cannot supply an Omni host.

New-only PR model/content validation requires immutable Git base/head evidence and verifies authored live base/candidate YAML against the corresponding revisions before and after checks. Missing or mismatched snapshots block rather than selecting another baseline. This is sampled consistency verification, not an atomic lock over live content/drafts or a guarantee of exhaustive API validation depth. Orchestrated no-branch, post-sync, and repair verification remain strict-health checks, not self-comparisons.

With valid trusted policy, non-Omni pull requests return a successful `skipped` decision unless an enabled optional check applies. Fork pull requests do not receive the Omni credential. Fork changes to Omni files fail closed and require a maintainer-controlled same-repository branch for full validation.

## Credentials

The operator chooses an Omni Organization API Key or a dedicated service-user PAT and passes it through the existing `omni-api-key` action input. Both use the `OMNI_API_KEY` secret and environment-variable name; no separate credential-type configuration is required or currently supported. The client sends the credential as a Bearer token. Personal-folder validation additionally verifies the credential scope and distinct user/membership identities using `whoami`; it never substitutes the credential owner's personal content for an unresolved acting user. Shared-only validation does not require this identity lookup. A successful `doctor` check verifies the access and settings it actually checks, not credential type, every endpoint permission, or complete content coverage.

After installation, a credential-free route step reads only trusted metadata and GitHub changed-file context. The composite action exposes the selected credential as `OMNI_API_KEY` only when that route selects at least one Omni model context. Install, routing, and skipped-report steps do not receive it as an environment variable. This selection does not change GitHub's automatic `GITHUB_TOKEN` or its workflow permissions.

The unreleased AI Repair scaffold uses a different `OMNIFLOW_REPAIR_API_KEY` during maintainer testing, exposed only to the repair process after protected-environment approval. It must never reuse the routine validation credential. Its constrained, dedicated-user PAT requirement is unchanged; Organization API Keys are not an alternative for this scaffold. A dedicated Modeler-scoped user limited to a non-production model is the conservative development starting point; any narrower custom role remains unverified. Keep AI Repair disabled for customer installation.

Post-deployment dbt sync uses a separate `OMNIFLOW_SYNC_API_KEY`, exposed only to the `dbt-sync` action mode. Independently choose an Organization API Key or a dedicated service-user PAT with the Modeler or Connection Admin permission required by Omni's schema refresh endpoint plus read access required by enabled post-sync checks. Pull-request validation and AI repair action modes never receive this credential. Customers may technically map the same GitHub secret to multiple inputs, but separate credentials and identities are the recommended isolation boundary. Choosing an Organization API Key for validation does not enable dbt sync, AI eval, or AI repair.

Choose the credential scope deliberately:

- **Organization API Key:** Organization Admin permissions provide broad organizational access for governance workflows, with a correspondingly larger impact if the credential is exposed or misused. Routine validation's read-oriented behavior does not make this credential read-only.
- **Dedicated service-user PAT:** inherits the user's model, content, and role permissions. It can support broader governance when that user is granted the necessary access, or a narrower boundary when only selected models and content are in scope. A PAT from an admin user still has that user's administrative access.

Neither option changes which models OmniFlow routes or which content and checks are configured. Personal folders are excluded by default, labels can narrow results, and endpoint behavior and limits still apply. A passing run is not proof that every item across an instance was inventoried or exhaustively validated. Validate the chosen credential's actual coverage in the adopter's non-production live gate. The current `omni.user_id`/`OMNI_USER_ID` option has credential-specific API semantics; review [Configuration](CONFIGURATION.md) before using it rather than assuming it expands either credential's scope.

Omni tokens do not expire automatically, so maintainers must define rotation, revocation, and access-review procedures for either type. See [Omni API authentication](https://docs.omni.co/api/authentication). Keep credentials out of repository files and use separate environment-scoped secrets for distinct targets.

OmniFlow rejects secret-like configuration keys and environment expansion of secret-like names. The standard report path constructs a minimal allowlisted schema, and public failure messages/annotations use fixed vocabulary instead of customer-authored diagnostics. Legacy redaction helpers are not comprehensive PII, PHI, PCI, or credential detection. Separate approved handling is required for local/restricted evidence and any other workflow output.

## Network And Data Handling

OmniFlow sends authenticated requests only to the HTTPS origin in trusted metadata. Redirects are disabled, TLS verification remains enabled, request timeouts are bounded, retries apply only to throttling and transient server errors, and response bodies are never included in API errors.

The normal orchestrated validation client uses read-only GET operations. It does not query a warehouse, capture query results, merge pull requests, or write model YAML to Omni.

The opt-in dbt sync client sends one non-idempotent POST to start a schema refresh and bounded GET requests to poll its job. The POST is never retried because a lost response could otherwise create duplicate jobs. The command rejects pull-request events, tags, unsupported GitHub events, and deployment branches that differ from trusted `base_branch`. Its normalized internal evidence includes the job identity; shareable reports expose only permitted status/count information, not job/model/branch identifiers. Raw responses and warehouse results are not stored by this client.

Omni Git integration may commit system-generated refresh changes or require a separate pull request, depending on the connection's branch-based refresh and Git system-sync settings. Workflow path filters must include dbt sources and exclude generated Omni model paths to prevent refresh loops. OmniFlow does not approve or merge a system-sync pull request.

The optional, disabled-by-default AI eval check preflights prompt-set/model ownership, starts a baseline/branch pair, and validates run identity while polling documented `/v1/ai/eval/runs` operations. Creation and cancellation POSTs are not retried. Known unfinished runs are reconciled and cancelled on handled failures; unresolved or ambiguous creation requires review. Immediate run journals and allowed per-prompt fields stay restricted. Public formats contain permitted counts, check/finding categories, and operational/cleanup flags; they exclude even opaque prompt IDs, as well as prompt text, configured labels, judge errors, and conversation details. See [AI Eval](AI_EVAL.md) for scoring, permissions, retention, and live-acceptance limits.

The optional AI Repair path uses documented AI-job, YAML-write/delete, and Git-commit endpoints against an existing development branch. Omni documents that AI jobs may execute queries and does not expose a no-query switch on that endpoint. OmniFlow never calls the AI result stream, stores no result summary or raw query data, cancels a non-terminal job before rollback, rejects sensitive or out-of-scope YAML changes, reruns every configured gate, and never approves, merges, or deploys.

Non-idempotent AI creation, YAML mutation, deletion, and Git commit requests are not retried. AI cancellation is documented as idempotent. Ambiguous job creation, cancellation, commit, concurrent edit, or rollback results fail closed for manual review.

API responses, pagination, trusted files, YAML files, YAML totals, nesting, aliases, and model counts have explicit safety limits. Repeated pagination cursors, cyclic YAML aliases, unsafe paths, symbolic-link output targets, unknown policy keys, and ambiguous routing fail closed.

## Artifacts

Public report schema 2 permits only fixed categories/statuses/reason codes, bounded counts, boolean flags, and generated run/context references. It excludes customer text, direct resource IDs, authored paths, tenant URLs, and Git/Action references. Standard and strict settings use the same minimal public boundary; strict is not a separate route to safe disclosure. Unknown fields and values are omitted rather than serialized. This is data minimization, not a guarantee of legal compliance or a PII/PHI/PCI detection service. See [Diagnostic Privacy](DIAGNOSTIC_PRIVACY.md).

Restricted YAML, dependency, semantic-diff, content, and contract files are deleted by default. When a local operator explicitly enables retention, OmniFlow creates owner-only directories and files. Reviewed workflows explicitly list supported public report filenames and the minimal artifact manifest; they must not upload an entire public/output directory or glob unknown stale files. Filesystem permissions do not protect an archive after it is uploaded to a broader audience.

The distinct repository-only diagnostic class defaults on for eligible private same-repository GitHub.com validation runs. It adds verified source locations and supported authored identifiers, not API prose, data values, SQL or Omni-only content identities. A live repository/revision check and matching authored snapshots are required; unknown eligibility stays minimal. The example validation workflows upload explicitly listed repository reports separately with seven-day retention and a fresh-run readiness receipt. PR comments, logs, annotations and Code Security uploads remain minimal. Opt out with `reporting.diagnostics.mode: minimal` on the trusted target branch. Repository readers can download repository-only metadata; it is not approved for external forwarding merely because it excludes raw values.

Credential choice does not alter these output boundaries. Strict redaction is not an opt-out from repository-only diagnostics; use the trusted `reporting.diagnostics.mode` setting to select `minimal` or eligible `repository` output. An administrative credential still needs protection even when its reports are minimal.

AI Repair keeps pre- and post-change YAML snapshots in process memory. Public repair evidence uses the same minimal schema; model/branch/job/commit identity must be verified from authorized private evidence and the actual Omni/Git state. The AI prompt, authored YAML, AI result summary, chat URL, progress, error body, and query data are not persisted by this repair path.

Readiness and synchronization decisions use trusted, detailed internal evidence rather than public JSON. Generated context references are not credentials or attestations. Historical reports, logs, and artifacts created before schema 2 are not retroactively sanitized and require their own retention/access review.

Do not retain restricted artifacts on a persistent or shared self-hosted runner. A self-hosted runner is outside the recommended alpha path unless it is isolated, ephemeral, access-controlled, and cleaned after every job.

## Supply Chain

All first-party workflow actions are pinned to full commit SHAs. The action runtime and release build use exact package versions and SHA-256 hashes, binary wheels only, disabled build isolation, and dependency-free installation of the reviewed checkout.

Repository security automation includes tests, Ruff, Bandit, pip-audit, gitleaks, CodeQL, zizmor, Dependabot, SBOM generation, checksums, and Sigstore signing. GitHub repository rules, CODEOWNERS, private vulnerability reporting, and release-environment approval are operational controls and must remain enabled.

## Operator Responsibilities

- Protect the base branch before adding either Omni credential type.
- Require review from trusted owners of workflows, metadata, policy, dependency locks, and security code.
- Keep the action and every third-party action pinned to reviewed commit SHAs.
- Review Dependabot and code-scanning alerts promptly.
- Review credential scope and rotate or revoke Organization API Keys and PATs according to policy.
- Verify the first real Omni-created pull request and post-merge Omni promotion before making the check mandatory.
- Run the first dbt refresh against a controlled non-production target and verify Git side effects before enabling production sync.
- Keep dbt sync after the production dbt command, in a protected environment, and restrict its trigger paths to dbt sources.
- Never upload restricted artifacts or raw Omni responses to a public issue.
- Keep AI Repair disabled until the normal validation workflow has passed a real Omni-created PR.
- Require protected-environment approval for every AI repair and review the resulting PR diff independently.
- Revoke the repair PAT and stop merge activity immediately after `rollback_failed` or an ambiguous write outcome.

## Reporting A Vulnerability

Follow [SECURITY.md](../SECURITY.md). Do not disclose suspected vulnerabilities, credentials, private tenant metadata, or customer YAML in a public issue.
