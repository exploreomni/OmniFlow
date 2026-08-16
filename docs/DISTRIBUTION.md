# Distribution Naming

OmniFlow is the product and command name. The Python import package is `omniflow`, and the CLI command is `omniflow`.

The future PyPI distribution name is `omniflow-ci`. The `omniflow` distribution name is already used by an unrelated OMOP data-harmonization project: <https://pypi.org/project/omniflow/>. Reusing that name would create supply-chain ambiguity and an unsafe customer installation experience.

OmniFlow does not currently publish to PyPI. During controlled alpha, customers install the GitHub Action from a reviewed full commit SHA. Before publishing `omniflow-ci`, maintainers must:

1. Create the PyPI project through a reviewed Trusted Publisher.
2. Protect the release environment and default branch.
3. Publish a signed GitHub prerelease with checksums and an SPDX SBOM.
4. Verify installation in a clean environment before documenting the PyPI command.

An unrelated package using the OmniFlow product name must never be presented as an official OmniFlow release.
