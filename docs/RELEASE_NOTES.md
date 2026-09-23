# Release notes

## Unreleased — validation fixes and environment targets (#23–#25)

This change is a development candidate, not a published release or a claim of customer acceptance.

### Breaking-change hold documentation (#23)

- Corrected the configuration and troubleshooting guides: unavailable deployment
  evidence blocks under `action: fail` and is advisory under `action: warn`.
- Clarified safe bootstrap/recovery and explicit current-head revalidation. No hold
  runtime behavior or branch protections changed.

### YAML view identity compatibility (#24)

- YAML pulls normalize both supported `viewNames` orientations: file path → canonical name and canonical name → file path. Internal snapshot manifests remain name → path, so existing normalized snapshots need no migration.
- Real-shaped responses may include empty names for model, relationship, and topic files. These entries are ignored only after their paths and non-view classification are verified. View files still require complete, unique canonical identities.
- Mixed/ambiguous maps, duplicate identities, unsafe or unknown paths, invalid value types, and incomplete view coverage remain blocking. File inventories, checksums, scoped names, query views, and authored/resolved snapshots retain their existing protections.
- Malformed file-content entries now fail explicitly instead of being silently removed from the inventory before identity checks. Raw strings and the existing `content`/`contents` string wrappers remain supported.
- Metadata failures identify the processing stage and a safe reason category in JSON. Reviewer reports distinguish this input failure from semantic findings and leave dependent checks incomplete, including under strict redaction.

After a reviewed release or commit containing this fix is published, update the consumer's pinned Action reference through its normal review process. Run fresh validation for the affected PR; do not assume existing pins receive the fix automatically.

Development regression coverage uses synthetic payloads shaped like the report in [issue #24](https://github.com/exploreomni/OmniFlow/issues/24), plus legacy-format controls. A fresh sanitized tenant payload and a successful consumer branch run remain the customer acceptance gate. See [troubleshooting](TROUBLESHOOTING.md#yaml-viewnames-metadata-processing-failed).

### Environment-scoped validation targets (#25)

- Added opt-in version 2 trusted metadata for exact PR-base routing to development
  leader and production follower targets sharing a Git model folder.
- Bound validation credentials to the selected environment and verified live Git
  settings, unique candidate branch identity, and authored YAML against the immutable
  PR head before and after checks. Missing or stale candidates never fall back to the
  base model; reports explicitly identify incomplete candidate coverage.
- Kept version 1 supported. Version 2 rejects deployment sync, AI repair and
  deployment-readiness/hold release rather than sharing repository-wide sync state
  between environments. No automatic model writes or merges were introduced.
- Added environment-scoped workflow and [setup/rollback guidance](ENVIRONMENT_TARGETS.md).
  Controlled leader/follower acceptance remains required; sampled authored-content
  equality is not an atomic snapshot lock or proof of warehouse readiness.
