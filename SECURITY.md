# Security Policy

## Supported Versions

OmniFlow is currently in controlled alpha and has no stable package release. Installations must pin a reviewed full commit SHA. Security fixes are applied to the latest reviewed commit until the first supported tag is published.

## Reporting A Vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/exploreomni/OmniFlow/security/advisories/new). Include the affected commit or version, reproduction steps, impact, and any suggested mitigation.

Maintainers will review complete reports on a best-effort basis. No service-level commitment is implied while the project remains in alpha.

## Security Boundaries

- Operators may choose an Omni Organization API Key or a dedicated service-user personal access token (PAT) for validation. Organization keys have Organization Admin permissions; PATs inherit the user's permissions. This is an access-scope decision, not a different GitHub credential. See [Omni authentication](https://docs.omni.co/api/authentication).
- The action exposes the selected validation credential to the validation process only, under the `OMNI_API_KEY` environment name. Dependency installation does not receive it. The current client does not identify or verify the credential type.
- Optional dbt synchronization uses a separate `OMNIFLOW_SYNC_API_KEY`, chosen independently as an Organization API Key or service-user PAT with the required endpoint access. It rejects pull requests and non-base branches and never exposes that credential to pull-request validation or AI repair modes. The protected sync process uses it for the refresh and enabled post-sync read checks.
- Pull-request policy and model host metadata are read from the trusted base branch.
- The example privileged workflow does not check out or execute pull-request code; changed filenames are read through GitHub's API.
- Public artifacts are redacted; detailed model and dependency artifacts are deleted by default and remain local when retention is explicitly enabled. Use `security.redaction_level: strict` when broader credentials may expose content names or other metadata that repository readers should not see.
- Broader credential access does not establish whole-instance coverage: validation still follows configured model routing, content filters, personal-folder settings, enabled checks, and API limits. A successful `doctor` check does not verify credential type or complete content coverage.
- Core validation does not request warehouse query execution and must not persist raw query results. Optional AI eval has a separate execution contract and must not inherit a no-query/no-cost guarantee from core validation.

See the README for the current live-testing limitations and least-privilege guidance.
