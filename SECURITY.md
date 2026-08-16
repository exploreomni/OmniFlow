# Security Policy

## Supported Versions

OmniFlow is currently in controlled alpha and has no stable package release. Installations must pin a reviewed full commit SHA. Security fixes are applied to the latest reviewed commit until the first supported tag is published.

## Reporting A Vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/exploreomni/OmniFlow/security/advisories/new). Include the affected commit or version, reproduction steps, impact, and any suggested mitigation.

Maintainers will review complete reports on a best-effort basis. No service-level commitment is implied while the project remains in alpha.

## Security Boundaries

- The action exposes the dedicated Omni PAT to the validation process only, under the `OMNI_API_KEY` environment name. Dependency installation does not receive it.
- Optional dbt synchronization uses a separate `OMNIFLOW_SYNC_API_KEY`, rejects pull requests and non-base branches, and never exposes that token to pull-request validation or AI repair modes. The protected sync process uses it for the refresh and enabled post-sync read checks.
- Pull-request policy and model host metadata are read from the trusted base branch.
- The example privileged workflow does not check out or execute pull-request code; changed filenames are read through GitHub's API.
- Public artifacts are redacted; detailed model and dependency artifacts are deleted by default and remain local when retention is explicitly enabled.
- OmniFlow never runs warehouse queries and must not persist raw query results.

See [Limitations And Support Boundary](docs/LIMITATIONS.md) for current live-testing limits and [Security Model](docs/SECURITY_MODEL.md) for least-privilege guidance.
