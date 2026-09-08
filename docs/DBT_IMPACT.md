# dbt Impact Analysis

OmniFlow's dbt impact analysis catches the case where a dbt-only pull request removes a warehouse column or model that the committed Omni model YAML still references. It is optional, disabled by default, and requires no Omni credential because it compares files that already exist in the repository.

## The Problem It Solves

The [breaking change hold](BREAKING_CHANGE_HOLD.md) stops a breaking **Omni** change from merging before its dbt deployment. This check covers the opposite direction.

Consider two dbt pull requests that both rename columns, with no Omni YAML changes in either:

1. PR A renames `revenue` to `total_revenue` in `models/marts/orders.sql`.
2. PR B renames a column in another model.
3. Neither PR touches Omni files, so OmniFlow skips both and they merge.
4. dbt deploys. The warehouse no longer has `revenue`.
5. The Omni model still defines a `revenue` field pointing at that column.
6. Every dashboard using that field breaks.

Before this check, the breakage surfaced only after deployment, when `post_sync_validation` reran model validation against the refreshed schema. By then the damage was already live.

The impact check moves that detection to pull-request time, so the dbt change is blocked until the Omni model is updated in the same deployment sequence.

## What It Detects

| Situation | Result |
| --- | --- |
| dbt removes a column an Omni field references | Fail |
| dbt renames a column an Omni field references | Fail (rename reads as a removal plus an addition) |
| dbt deletes a model or changes a surviving node's relation identity | Fail when the old relation is referenced |
| dbt removes a column no Omni field references | Pass |
| dbt adds a column | Pass |
| dbt changes a model no Omni view references | Pass |
| Pull request also changes Omni model YAML | Not evaluated here; normal validation and the breaking change hold apply |
| Required manifest, columns, Omni YAML, or supported SQL evidence is missing | Incomplete coverage; fail by default |

The check runs only on pull requests that OmniFlow would otherwise skip: those with dbt-path changes and no Omni model changes.

## Two Analysis Modes

### Manifest mode (preferred with complete contracts)

When the repository commits a dbt `manifest.json`, OmniFlow compares immutable base and head revisions. Fully qualified relation names improve matching. A manifest's documented columns are not automatically the warehouse's complete output schema: gating requires nonempty, enforced dbt model contracts on both sides. Missing contracts or column inventories are explicitly incomplete coverage. A surviving node whose alias, schema, database, or `relation_name` changes is treated as removing its old relation.

Set the path in policy:

```yaml
checks:
  dbt_impact:
    enabled: true
    manifest_path: target/manifest.json
```

The trusted build process must generate and commit current artifacts for both revisions. An unchanged manifest accompanying dbt source changes is flagged as incomplete. OmniFlow does not execute dbt or attest that a committed manifest was generated from the source; that provenance remains a required adopter build control. Undocumented columns cannot establish a clean gate.

Ephemeral models are excluded because they never materialize into a relation an Omni view can reference. Seeds and snapshots are included.

### SQL heuristic mode (no extra artifacts)

Without a manifest, OmniFlow parses the model SQL to extract output column names. It:

- Strips Jinja blocks, line comments, and block comments
- Isolates the final top-level `SELECT`, skipping CTEs
- Reads explicit `AS alias` names and plain or qualified column references
- Derives the model name from the file path

This mode is deliberately conservative. Unsupported projections produce an explicit coverage issue rather than a clean pass:

- Bare and qualified stars cannot enumerate output columns
- Unparseable head or base, set operations, DISTINCT, and literals require stronger evidence
- Expressions without an alias make the entire projection incomplete
- Python models, macro changes, seed files, and configuration changes require manifest evidence

Simple `ref()` and `source()` calls in source positions are tolerated because they do not define the projection. Dynamic projection macros are unsupported. Prefer complete manifest contracts when gating production changes.

## Matching Omni References

Omni views reference the warehouse two ways, and OmniFlow reads both from the committed YAML:

1. **The source relation**, from either a single `sql_table_name` or the split `catalog`/`schema`/`table_name` form that Omni's dbt integration writes. When no table is declared, Omni defaults it to the view name. Quoting styles (`"schema"."table"` and `[schema].[table]`) are normalized.
2. **The columns**, from field `sql:` expressions. Both `fields:` and the `dimensions:`/`measures:` blocks are read. A field with no `sql` maps to a column of the same name, which is the common case for dbt-generated views.

Column matching:

- `${TABLE}.column` tokens are matched exactly
- Other SQL is matched on word boundaries, so removing `customer` never flags a field referencing `customer_id`
- Comparison is case-insensitive, matching warehouse behavior

Relation matching tries the bare name, the schema-qualified name, and the fully qualified name. A model that matches through several of those forms still produces a single finding.

Views are indexed by **file path**, not by view name. A repository that materializes the same table into several schemas has several view files with the same name, and keying by name would silently collapse them and hide all but one from this check.

### Same-Named Tables In Different Schemas

A common dbt pattern produces the same table in several schemas, for example a shared `analytics_marts` alongside per-developer `dbt_<user>_marts` targets. Each gets its own Omni view file with the same view name.

In SQL heuristic mode OmniFlow only knows the dbt model's file name, so it cannot tell which schema changed. When an unqualified relation name matches more than one distinct Omni relation, the finding is reported with:

```json
{
  "ambiguous_relation_match": true,
  "candidate_relations": [
    "coffee_training.analytics_marts.dim_product",
    "coffee_training.dbt_austin_marts.dim_product",
    "omni_dbt_marts.dim_product"
  ]
}
```

Every candidate's orphaned fields are listed so a reviewer sees the full set, and the message says the match was unqualified. Resolve it by committing a dbt manifest, which supplies the exact `relation_name`, or by adding a `table_mapping` entry. A fully qualified relation match is never marked ambiguous and implicates only the matching schema.

## Enable The Policy

```yaml
checks:
  dbt_impact:
    enabled: true
    manifest_path: target/manifest.json
    fail_on_orphaned_references: true
    fail_on_incomplete_coverage: true
    omni_yaml_paths:
      - omni/my_model
    table_mapping:
      - dbt_model: orders_v2
        sql_table_name: analytics.marts.orders

deployment:
  breaking_change_hold:
    enabled: true
    dbt_paths:
      - models
      - seeds
      - snapshots
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turns the check on. |
| `manifest_path` | none | Repository-relative dbt manifest. Falls back to SQL heuristics when absent or unreadable. |
| `fail_on_orphaned_references` | `true` | `true` blocks the merge. `false` reports a warning only. |
| `fail_on_incomplete_coverage` | `true` | Missing or unsupported evidence blocks the gate. Set `false` only for explicitly advisory adoption. |
| `omni_yaml_paths` | `model_path` entries from `.omni/flow.json` | Directories holding Omni model YAML. |
| `table_mapping` | none | Explicit `dbt_model` to `sql_table_name` or `omni_view` overrides, for custom schema macros. Maximum 500 entries. |

The check reuses `deployment.breaking_change_hold.dbt_paths` to decide which files are dbt sources, so the two features stay consistent.

## Checkout Requirement

The supported `pull_request_target` workflow checks out trusted base code. Routing selects a separate dbt-only action step that never receives an Omni credential. The exact event base/head commit SHAs identify the data comparison; PR-head code is never checked out, imported, compiled, or executed.

Head files are read as regular Git blobs when available, or through GitHub's tree/blob APIs at the immutable SHA. Tree mode and size are verified before downloading a blob, so symlinks cannot be followed implicitly. Reads reject path traversal, incomplete or truncated trees, oversized files, and unavailable history. The exact changed-file comparison uses Git or the bounded GitHub compare endpoint; a response reaching its 300-file cap fails closed. Remote trees are limited to 2 MiB and 10,000 entries. For larger repositories or PRs, make the exact head objects available through a reviewed data-fetch step without checking them out. GitHub metadata access uses the ordinary repository token; no Omni secret is needed.

Limits are 1,000 dbt files, 5 MiB per SQL file, and 50 MiB combined SQL data. Local CLI comparisons resolve the base branch or previous commit; unavailable comparison evidence blocks. These limits and GitHub API limitations are reported as errors, not clean coverage.

## Evidence

Each run writes `dbt-impact.json` alongside the normal public reports:

```text
.omniflow/
  report.json
  report.md
  report.sarif
  junit.xml
  evidence.json
  dbt-impact.json
  artifact-manifest.json
  public/
```

The artifact records `coverage_complete`, analysis mode, analyzed files, index counts, fallback notes, and per-finding detail. `dbt_impact_incomplete_coverage` identifies evidence gaps explicitly. It does not contain warehouse rows, query results, compiled SQL, or credentials.

## Limitations

State these plainly when planning an adoption.

- **Static analysis only.** OmniFlow never queries the warehouse, so it cannot confirm whether a column actually exists today. It reasons from committed dbt and Omni files.
- **Heuristic mode supports a narrow subset.** Unsupported SQL is incomplete and blocks by default; it cannot substitute for warehouse or adapter validation.
- **Custom schema macros need help.** When a dbt model's warehouse relation is not derivable from its name or manifest, add a `table_mapping` entry.
- **Word-boundary matching can still over-report.** A column name appearing incidentally in a field's SQL is treated as a reference. Review findings before assuming a hard break.
- **Unqualified matches can be ambiguous.** In heuristic mode a bare model name cannot distinguish same-named tables across schemas. Those findings are marked `ambiguous_relation_match` with the candidate list rather than silently picking one.
- **Only runs on Omni-free pull requests.** When a pull request changes both dbt and Omni files, the breaking change hold and normal contract validation cover it instead.
- **A pass is bounded static evidence.** Complete local parsing does not establish deployment acceptance or manifest provenance. In advisory mode, inspect `coverage_complete` and warnings before making a merge decision.

For renames and removals, use expand/contract: deploy the new column or relation while preserving old names, synchronize, migrate Omni and downstream consumers, validate adoption, and only then remove obsolete warehouse names. Simply deploying a destructive dbt rename before changing Omni breaks the existing consumers.

## Related

- [Breaking Change Hold](BREAKING_CHANGE_HOLD.md) covers breaking Omni changes merging ahead of dbt.
- [Post-Deployment dbt Synchronization](DBT_SYNC.md) refreshes Omni after dbt deploys and reruns validation.
