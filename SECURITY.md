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
- Standard reports use diagnostic schema 2: fixed categories/statuses, bounded counts, boolean flags, and generated anonymous references. Both standard and strict settings exclude customer text, direct resource IDs/paths, and Git/Action references; neither setting is a comprehensive PII/PHI/PCI detector or compliance guarantee.
- Detailed model and dependency artifacts are deleted by default and remain restricted when retention is explicitly enabled. Upload only the explicitly listed supported diagnostic files, never a whole output directory or arbitrary stale files. Verify model/branch/job/commit identity through trusted workflow and authorized private evidence, not the shareable report.
- Core validation does not request warehouse query execution and must not persist raw query results. Optional AI eval has a separate execution contract and must not inherit a no-query/no-cost guarantee from core validation.

See [Diagnostic Privacy](docs/DIAGNOSTIC_PRIVACY.md) for the schema boundary, private support handoff, and historical-artifact limitations, and the README for live-testing and least-privilege guidance.
