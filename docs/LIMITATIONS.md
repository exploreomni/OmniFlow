# OmniFlow Limitations And Support Boundary

OmniFlow is open-source software released under the MIT License. It runs in the adopter's GitHub Actions environment and communicates directly with the adopter's Omni instance. There is no hosted OmniFlow control plane, telemetry service, or customer-metadata store.

This release is a controlled alpha intended for evaluation and non-production validation. It has broad automated coverage and has completed the core pull-request flow against a maintainer-controlled non-production model, but it is not a stable package release, managed service, warranty, service-level commitment, compliance certification, or substitute for adopter-specific testing.

## Installation And Support

- Install the GitHub Action from a reviewed full 40-character commit SHA. Do not use `@main`, a floating tag, or an unrelated package from PyPI.
- GitHub Actions on Ubuntu Linux x86_64 with Python 3.11 is the supported action runtime. The Python library is tested on Python 3.11, 3.12, and 3.13, but direct package installation and other CI providers are not supported customer installation paths in this alpha.
- Support is provided through this repository on a best-effort open-source basis unless a separate agreement explicitly says otherwise.
- Omni product support does not automatically cover this open-source project. Use Omni support for documented Omni product or API behavior and this repository for OmniFlow behavior.
- Adopters own their GitHub configuration, runner security, branch protection, token lifecycle, Omni permissions, policy choices, and production acceptance.

## Identity And Pull-Request Discovery

Omni's public documentation does not currently define a stable machine-readable pull-request payload containing every host and model identifier OmniFlow needs. For that reason, adopters commit non-secret routing metadata to `.omni/flow.json` on the protected base branch.

OmniFlow discovers the pull-request branch automatically and matches changed paths against trusted model registrations. It does not trust a pull-request body to supply an API host. The optional `omniflow-context` marker can disambiguate an already-registered model, but it cannot introduce a host or model that is absent from trusted metadata.

Consequences:

- A missing, stale, overlapping, or ambiguous model registration blocks an Omni-like change rather than guessing.
- Non-Omni pull requests return a successful `skipped` decision without receiving the Omni token.
- Fork pull requests never receive the Omni token. An Omni change from a fork fails closed and must be moved to a trusted same-repository branch for full validation.
- The privileged `pull_request_target` workflow must never check out or execute pull-request-head code. Adding such a step breaks the documented security boundary.

## Omni API And Validation Coverage

OmniFlow uses documented Omni APIs, but its result can cover only resources visible to the dedicated token and supported by those APIs.

- Model validation is Omni's server-side validation result. OmniFlow classifies and reports it; it does not reproduce Omni's validator locally.
- The Content Validator validates the full model server-side. Label filtering is applied locally after content metadata lookup and does not reduce the server-side validation scope.
- Downstream impact uses targeted Content Validator searches for changed fields, views, topics, and relationships. Inaccessible, private, unsupported, or unreturned content cannot be claimed as covered.
- A failed or incomplete downstream search is a coverage gap and fails by default. Relaxing that policy can allow an unknown impact through review.
- Semantic diff and lint inspect authored YAML structure. They do not prove that every runtime query, visualization, custom SQL expression, embedded workflow, or external consumer remains behaviorally identical.
- Rename detection is inference from the before-and-after semantic state. Reviewers must confirm intended renames and migrated references.
- OmniFlow does not execute warehouse queries during core validation and does not capture or persist query results. Omni's own server-side validators still run within the adopter's Omni instance according to Omni's product behavior.

## Downstream Contracts And Governance

Referenced deleted or renamed fields, referenced type changes, and referenced relationship cardinality changes fail by default. Unreferenced breaking changes are reported as risk, not automatically treated as runtime-safe.

The dependency result is evidence about content returned by the configured Omni APIs and token. It is not a universal lineage guarantee for:

- content the token cannot view;
- private or unsupported content types;
- consumers outside Omni;
- hand-written SQL, warehouse jobs, notebooks, reverse ETL, or application code;
- external dbt consumers not represented in the optional exposures response.

Governance checks are policy automation, not legal or regulatory determinations. OmniFlow reports configured metadata and findings but does not confer SOC 1, SOC 2, PCI, HIPAA, GDPR, or other compliance status.

## GitHub And Deployment

OmniFlow validates and reports. It does not approve, merge, or deploy a pull request. After merge, branch promotion and draft-content publication depend on the adopter's Omni Git integration and webhook settings.

GitHub plan and repository visibility can affect enforcement of branch protection, rulesets, environments, required reviewers, code scanning, and private repositories. Do not represent OmniFlow as a production gate when the required protections are unavailable or unenforced.

SARIF is always preserved in the redacted evidence artifact. Uploading SARIF to GitHub code scanning is optional and depends on repository feature availability. PR comments and annotations depend on GitHub token permissions and event behavior.

## dbt Exposures

dbt exposure enrichment is optional. Repositories that do not use dbt need no dbt configuration, API permission, package, or command.

When enabled:

- OmniFlow calls Omni's documented dbt exposures API; it does not run `dbt parse`, `dbt build`, or a dbt deployment.
- The result inherits the API's documented scope, token permissions, model references, and coverage limitations.
- Private content, unsupported references, or dashboards that do not map to dbt dependencies can remain unmapped.
- Exposure evidence supplements targeted downstream contracts and does not replace them.

## Post-Deployment dbt Synchronization

The optional `omniflow dbt sync --auto` mode is disabled by default and must run only after a reviewed production dbt deployment succeeds. It calls Omni's schema-refresh API, waits for the asynchronous job, and reruns enabled checks.

Important limits:

- The stage does not deploy dbt, verify transformed warehouse data, or roll back the preceding dbt deployment.
- Multi-connection refresh is not transactional. One refresh can complete before a later refresh fails, and OmniFlow cannot undo the completed refresh.
- Branch-based schema refresh requires an existing Omni branch and a separate human promotion step. A successful job is not proof that changes reached the shared model.
- Omni-generated commits or system-sync pull requests can create automation loops unless workflow paths and Git settings are configured carefully.
- The implementation has automated API, polling, policy, evidence, and failure coverage, but this release still requires its first live non-production connection test before customer production use.

## AI Repair

AI Repair is not a released OmniFlow feature. The repository retains a guarded development scaffold so its safety model can be reviewed, but customers must leave it disabled and must not install its example workflow.

The blocker is contractual, not cosmetic: Omni's public documentation does not currently expose the Modeling Agent branch-mutation contract required by the intended workflow. The documented AI Jobs API is query-oriented and does not provide a documented no-query control. Until a supported API contract exists and live safety testing is complete, OmniFlow cannot promise a bounded automated code fix.

See [AI Repair Development Scaffold](AI_REPAIR.md) for the technical design and release blockers.

## Data, Privacy, And Evidence

OmniFlow is designed not to persist API keys, full raw Omni payloads, warehouse rows, raw query results, or PII values. Public artifacts are redacted and restricted artifacts are deleted by default.

No software can compensate for an operator deliberately weakening those controls. Adopters must not add token-printing steps, upload restricted workspaces, enable unsafe raw output in CI, execute untrusted pull-request code in a privileged workflow, or paste customer material into public issues.

Artifact retention, GitHub logs, runner images, network egress, organization audit logs, and secret-manager controls remain the adopter's responsibility. Review the [Security Model](SECURITY_MODEL.md), [Security Policy](../SECURITY.md), and [safe support guidance](../SUPPORT.md) before installation.

## Required Live Acceptance Gate

Before making OmniFlow a required check, each adopter must prove at minimum:

1. An Omni-created pull request resolves the expected registered model and Omni branch.
2. A non-Omni pull request skips without exposing the Omni token.
3. Model, content, lint, diff, and downstream searches complete with the intended permissions.
4. A controlled invalid change fails, and a safe change passes.
5. Public artifacts and comments contain only expected redacted metadata.
6. Branch protection, CODEOWNERS, webhook promotion, and fork behavior work as intended.
7. Optional dbt exposures and synchronization are tested separately before either becomes a gate.

Record the commit SHA, workflow URL, policy decision, artifact hash, permissions used, and any coverage gap. Passing project tests establishes software behavior in the tested environment; it does not establish production acceptance in an adopter's tenant.
