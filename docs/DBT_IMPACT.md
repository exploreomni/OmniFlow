# dbt Impact Analysis

OmniFlow's dbt impact analysis catches a pull request that removes a warehouse column or model still referenced by the candidate Omni YAML. It runs for both dbt-only and mixed dbt/Omni changes. It is optional, disabled by default, and needs no Omni credential because it compares repository data.

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
| Pull request also changes Omni model YAML | Evaluate once alongside normal validation and the breaking change hold |
| Required manifest, columns, Omni YAML, or supported SQL evidence is missing | Incomplete coverage; fail by default |

The check runs whenever an enabled policy detects dbt-path changes, independently of whether Omni validators also run. An Omni comment or label cannot suppress dbt-impact analysis. A dbt-only action still receives no Omni credential.

## Candidate References And Coverage

In a PR, both Omni inventories are read as data at the immutable base/head commits. Candidate references determine whether the proposed warehouse removal leaves an orphan; base counts provide comparison evidence. Base-only references are not blindly unioned into the candidate set, so deliberately removing an obsolete dependency does not create an orphan finding by itself. Model/content checks, contract impact and the deployment hold still govern whether that coordinated change is safe for published content and deployment order.

Every configured root must be available and readable. One missing root remains incomplete even if another root is valid. `fail_on_incomplete_coverage: false` changes the finding to a warning, not the evidence to complete; the run is advisory. Inheritance/imports that require unavailable external sources remain incomplete.

A directory containing only metadata or arbitrary YAML does not prove absence of schema-layer dependencies. Zero authored views are non-applicable only with a fully parsed, known model shape plus either a non-destructive dbt diff or immutable evidence that all prior authored views were removed. This is not proof that the tenant has no schema-layer views or content; the other enabled gates remain necessary.

Filename classification uses the shared semantic classifier. Names such as `customer_relationships.view.yaml` remain views; only actual relationship/topic/model sources are excluded from view indexing.

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

Relation matching preserves any known database/schema qualification; it never broadens a qualified relation to unrelated same-named tables. Unqualified heuristic matches remain ambiguous when several distinct relations fit. A model that matches through several supported forms still produces a single finding.

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

## Optional column-to-content impact

Enable this **in the trusted base branch's `.omniflow.yml`**, underneath the existing
`checks.dbt_impact` block. It is off by default:

```yaml
checks:
  dbt_impact:
    enabled: true
    content_impact:
      enabled: true
      sql_dialect: snowflake
      max_searches: 100
      timeout_seconds: 120
```

Keep the existing workflow's `omni-api-key: ${{ secrets.OMNI_API_KEY }}` input.
Use a PAT or organization API key with permission to read the registered model and
the content you intend to cover. The Action runs static analysis without an Omni
credential, followed by a separate credentialed `omniflow dbt content-impact`
step. Fork PRs cannot run this lookup. There is no warehouse query execution,
dbt invocation, branch deployment, document editing, or new blocking policy.
Version-2 environment registrations retain the dedicated validation-environment
credential and verified Git-target requirements; the lookup never creates a branch.

The analysis compares immutable base/head SQL projections, including same-name
expression changes, and follows supported Omni field references into derived fields
and filtered measures. Comments and formatting do not create column changes.
WHERE/JOIN/grouping changes can broaden potential impact. Import CTEs such as
`with orders as (select * from {{ ref('orders') }})` keep column precision: the CTE
is part of the compared context, so changing it broadens impact to every field on
the model. A star in the final projection remains a gap. A constant-argument macro
call inside an aliased output, such as `{{ cents_to_dollars('amount') }} as amount`,
is compared per column; a changed macro file, unaliased macros (which can expand to
several columns), `var()`/`env_var()`, and Jinja blocks remain gaps. Ambiguous
identities, inheritance, or dependency cycles also remain explicit coverage gaps
rather than guessed lineage. Upstream dbt column lineage is not included: when a
changed model is `ref()`'d by other dbt models (read at the base revision), the
report adds `dbt_downstream_models_not_analyzed` so the totals read as a lower bound,
not zero.
For native cross-view names, the optional step uses Omni's returned view-name
mapping only after matching it to the immutable authored files. It does not derive
canonical names by concatenating folder or schema names. Unavailable or ambiguous
identity metadata remains an explicit coverage gap.
The complete authored inventory establishes uniqueness separately for base and
head. A view missing from that mapping stays unresolved: a same-named view in
another schema is never substituted during content lookup. Verified folder moves
remain supported; this is an identity check, not a directory-layout requirement.

Targeted Content Validator field searches find candidate documents. Query and
document-layout metadata distinguish rendered dashboard tiles from workbook-only
tabs and dashboard filters. This observes **published content in the registered
base model**, not a simulated post-deployment warehouse or every branch draft.
Base/head source references remain separate; impact is not proof that a PR breaks
the reported document. User impersonation, permissions, unsupported layouts, and
request/time limits can narrow coverage; incomplete counts are lower bounds.
Saved calculated-field expressions, raw/app queries, and unresolved timeframe
references also retain explicit gaps instead of claiming full query lineage.
A document can contribute to both dashboard and workbook counts when it contains
both displayed tiles and affected saved tabs that are not placed on the dashboard.

The PR receives only one short advisory impact line, with **View affected content**
when this run generated authorized private details and their dedicated upload succeeded.
That link downloads `omniflow-column-impact`, a repository-only artifact; open `column-impact.md` inside
it for a deduplicated content list, owners when returned, navigation links,
changed columns, and dependency paths. The generic **Full report** link remains
the public summary, not the identity-rich detail artifact. Missing authorization,
generation, or upload evidence produces an explanation instead of a stale link.
Public
`.omniflow/public/content-impact.json` and the normal report contain safe counts and
coverage status. Detailed dependency paths and document metadata are emitted only
when the existing live private-repository diagnostic policy authorizes disclosure:

```text
.omniflow/repository/column-impact.json
.omniflow/repository/column-impact.md
```

Update the consumer workflow from `.github/workflow-examples/omniflow.yml` to upload
these new files and pass `CONTENT_IMPACT_READY` from the Action's
`content-impact-details-ready` output into the summary and PR publisher steps.
Include the dedicated `impact_upload` step and its `IMPACT_UPLOAD_OUTCOME`,
`IMPACT_ARTIFACT_ID`, and `IMPACT_ARTIFACT_URL` receipt inputs. Uploading ordinary
repository diagnostics alone never authorizes the affected-content link.
Updating only the Action SHA does not update upload paths or these receipt inputs. Private
artifacts are for repository readers, not shareable support bundles. Disable the
feature with `content_impact.enabled: false`; use `reporting.diagnostics.mode: minimal`
to retain counts while suppressing identity-rich private artifacts.

The advisory step cannot overwrite the earlier validation decision. Its local
receipt binds enrichment to the unchanged report, exact revisions, configuration,
and current Actions run/attempt. An unavailable lookup is never reported as zero
affected dashboards.

### Interpreting incomplete impact coverage

| Report category | Meaning and next step |
| --- | --- |
| Unresolved explicit view mapping | `dbt_explicit_view_mapping_unresolved` means a configured `table_mapping[].omni_view` matched no authored view across the registered model roots in the recorded base/head revision. Check the trusted mapping and any intentional view rename/removal. It is unknown impact, not a confirmed zero; independently resolved impacts remain available. |
| Unresolved field/view identity | Check the source database/schema and published model synchronization. The unmatched field is retained as static evidence but no other view's dashboards are attributed to it. |
| Saved calculation or timeframe lineage | Supported direct references are still useful; inspect calculated expressions in Omni for additional dependencies. These counts are not an exhaustive content inventory. |
| Downstream dbt models not analyzed | `dbt_downstream_models_not_analyzed` means another dbt model `ref()`s the changed one. Content built on those downstream models is not counted; check their Omni views. `dbt_downstream_models_unavailable` means that inventory could not be read. Neither is zero impact. |
| Dynamic macros, wildcard outputs, or compiled-SQL-only changes | The analyzer does not execute dbt or analyze compiled artifacts. Review generated SQL separately; this is not a clean no-impact result. |
| Metadata unavailable | Verify the credential, acting-user permissions, and connectivity for the registered scope, then rerun. No access widening is attempted. |
| Budget or snapshot gap | Narrow the scope, adjust bounded limits where supported, or rerun against stable published metadata. Omitted content is not confirmed unaffected. |

The private report distinguishes **confirmed source dependencies** from broader
**potential impact**, and lists unresolved source locations separately. Neither
category proves a runtime regression. Public reports explain safe reason categories
without copying SQL, names, source paths, or API error text.
For an unresolved explicit mapping, authorized private details identify the configured
view and affected revision. A match in one registered root does not require the same
view in every root. This advisory gap does not add a blocking policy or change the
existing orphan checks; proven comment-only SQL changes still skip impact lookup.

Full saved-calculation/compiled-SQL lineage, upstream staging-to-mart propagation,
and exposure `meta` enrichment remain separate enhancements, not release claims.

## Limitations

State these plainly when planning an adoption.

- **No warehouse execution.** OmniFlow cannot confirm whether a column actually exists today. Static analysis reasons from committed dbt and Omni files; optional enrichment reads content metadata only.
- **Heuristic mode supports a narrow subset.** Unsupported SQL is incomplete and blocks by default; it cannot substitute for warehouse or adapter validation.
- **Custom schema macros need help.** When a dbt model's warehouse relation is not derivable from its name or manifest, add a `table_mapping` entry.
- **Impact is not a regression verdict.** Supported expressions are parsed without executing them. Unsupported syntax remains a coverage gap; review potential-impact findings rather than assuming a hard break.
- **Unqualified matches can be ambiguous.** In heuristic mode a bare model name cannot distinguish same-named tables across schemas. Those findings are marked `ambiguous_relation_match` with the candidate list rather than silently picking one.
- **Mixed pull requests are supported.** Static dbt evidence remains independent of Omni validation, so a comment-only Omni edit cannot hide a destructive warehouse change.
- **A pass is bounded static evidence.** Complete local parsing does not establish deployment acceptance or manifest provenance. In advisory mode, inspect `coverage_complete` and warnings before making a merge decision.

For renames and removals, use expand/contract: deploy the new column or relation while preserving old names, synchronize, migrate Omni and downstream consumers, validate adoption, and only then remove obsolete warehouse names. Simply deploying a destructive dbt rename before changing Omni breaks the existing consumers.

## Related

- [Breaking Change Hold](BREAKING_CHANGE_HOLD.md) covers breaking Omni changes merging ahead of dbt.
- [Post-Deployment dbt Synchronization](DBT_SYNC.md) refreshes Omni after dbt deploys and reruns validation.
