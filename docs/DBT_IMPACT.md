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
| dbt deletes a model an Omni view references | Fail |
| dbt removes a column no Omni field references | Pass |
| dbt adds a column | Pass |
| dbt changes a model no Omni view references | Pass |
| Pull request also changes Omni model YAML | Not evaluated here; normal validation and the breaking change hold apply |

The check runs only on pull requests that OmniFlow would otherwise skip: those with dbt-path changes and no Omni model changes.

## Two Analysis Modes

### Manifest mode (precise, recommended)

When the repository commits a dbt `manifest.json`, OmniFlow compares the base and head manifests. The manifest supplies exact column lists and the fully qualified `relation_name` for every model, so mapping a dbt model to an Omni `sql_table_name` is unambiguous.

Set the path in policy:

```yaml
checks:
  dbt_impact:
    enabled: true
    manifest_path: target/manifest.json
```

The dbt CI job must commit that artifact for the comparison to work on both sides of the diff. A column is treated as removed only when the base manifest actually documented columns for that node, so a project that does not document columns never produces phantom findings.

Ephemeral models are excluded because they never materialize into a relation an Omni view can reference. Seeds and snapshots are included.

### SQL heuristic mode (no extra artifacts)

Without a manifest, OmniFlow parses the model SQL to extract output column names. It:

- Strips Jinja blocks, line comments, and block comments
- Isolates the final top-level `SELECT`, skipping CTEs
- Reads explicit `AS alias` names and plain or qualified column references
- Derives the model name from the file path

This mode is deliberately conservative. It reports nothing when it cannot parse confidently:

- `SELECT *` yields no columns, so no removal is claimed
- An unparseable head or base yields no removal
- Expressions without an alias are skipped

Because it cannot resolve `ref()`, `source()`, macros, or dynamic column lists, prefer manifest mode when precision matters.

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
| `omni_yaml_paths` | `model_path` entries from `.omni/flow.json` | Directories holding Omni model YAML. |
| `table_mapping` | none | Explicit `dbt_model` to `sql_table_name` or `omni_view` overrides, for custom schema macros. Maximum 500 entries. |

The check reuses `deployment.breaking_change_hold.dbt_paths` to decide which files are dbt sources, so the two features stay consistent.

## Checkout Requirement

The analysis compares the current files against the base. OmniFlow resolves the comparison ref by trying `origin/<base branch>`, then the bare base branch name, then `HEAD~1`, and uses the first one Git can resolve.

Use a checkout with enough history for one of those to exist. With a single-commit shallow checkout, no comparison base is available and the check reports that in `notes` rather than guessing.

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

The artifact records the analysis mode, the dbt files analyzed, index counts, any fallback notes, and per-finding detail: the relation, the removed column, and the orphaned Omni view and field names. It does not contain warehouse rows, query results, compiled SQL, or credentials.

## Limitations

State these plainly when planning an adoption.

- **Static analysis only.** OmniFlow never queries the warehouse, so it cannot confirm whether a column actually exists today. It reasons from committed dbt and Omni files.
- **Heuristic mode is approximate.** Without a manifest it cannot resolve Jinja, macros, `ref()`, `source()`, or dynamic column lists, and it under-reports by design.
- **Custom schema macros need help.** When a dbt model's warehouse relation is not derivable from its name or manifest, add a `table_mapping` entry.
- **Word-boundary matching can still over-report.** A column name appearing incidentally in a field's SQL is treated as a reference. Review findings before assuming a hard break.
- **Unqualified matches can be ambiguous.** In heuristic mode a bare model name cannot distinguish same-named tables across schemas. Those findings are marked `ambiguous_relation_match` with the candidate list rather than silently picking one.
- **Only runs on Omni-free pull requests.** When a pull request changes both dbt and Omni files, the breaking change hold and normal contract validation cover it instead.
- **A pass is not proof of safety.** An empty finding list can mean nothing is affected or that the evidence was insufficient. Check `analysis_mode` and `notes` in the artifact.

## Related

- [Breaking Change Hold](BREAKING_CHANGE_HOLD.md) covers breaking Omni changes merging ahead of dbt.
- [Post-Deployment dbt Synchronization](DBT_SYNC.md) refreshes Omni after dbt deploys and reruns validation.
