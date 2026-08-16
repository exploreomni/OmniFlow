# OmniFlow Support

OmniFlow is an open-source project in controlled alpha.

Support is provided through this repository on a best-effort community basis unless a separate agreement explicitly says otherwise. Review the [limitations and support boundary](docs/LIMITATIONS.md) before relying on OmniFlow as a merge gate.

## Before Opening An Issue

1. Review the [installation guide](docs/INSTALLATION.md).
2. Check [troubleshooting](docs/TROUBLESHOOTING.md).
3. Confirm the problem against the latest reviewed OmniFlow commit without weakening workflow security.
4. Reproduce the issue with the smallest non-customer-specific example possible.

## Safe Issue Details

Include:

- OmniFlow version or full action commit SHA.
- Python version and GitHub event type.
- Exit code and redacted error text.
- Which check failed: discovery, model validation, content validation, lint, diff, contracts, dbt refresh or polling, reporting, or workflow routing.
- A minimal metadata-only example using placeholder IDs and names.
- The redacted public report when it is safe to share.

Do not include:

- `OMNI_API_KEY`, `OMNIFLOW_SYNC_API_KEY`, or any credential.
- Private tenant URLs, model IDs, branch IDs, repository URLs, or user IDs.
- Customer model YAML or full Omni API responses.
- Raw query results, PII, email addresses, document URLs, or restricted artifacts.

Use a normal GitHub issue for non-sensitive bugs and documentation requests. Use GitHub private vulnerability reporting for suspected security issues as described in [SECURITY.md](SECURITY.md).
