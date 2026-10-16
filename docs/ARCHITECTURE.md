# OmniFlow Architecture And Operating Guide

> **Historical v0.4.0 reference — not current setup or security guidance.**
> Preserved from the ATX repository during synchronization with ExploreOmni.
> The implementation has since changed, including trusted policy, validation
> comparisons, diagnostic privacy, and AI safeguards. Use the current
> [installation guide](INSTALLATION.md), [configuration reference](CONFIGURATION.md),
> [security model](SECURITY_MODEL.md), [diagnostic privacy guide](DIAGNOSTIC_PRIVACY.md),
> and [testing guide](TESTING.md) for current behavior and acceptance requirements.

**Version:** 0.4.0 (controlled alpha)
**Audience:** Omni product management and engineering
**Repositories:** [atx-omni/OmniFlow](https://github.com/atx-omni/OmniFlow), [exploreomni/OmniFlow](https://github.com/exploreomni/OmniFlow)

---

## 1. Executive Summary

OmniFlow is an open-source CI/CD companion for Omni semantic-layer development. It runs as a GitHub Action in a customer's repository, validates Omni model and content changes before merge, and can synchronize Omni after a production dbt deployment.

This document covers the full architecture and, in detail, the two policies added to solve a specific customer problem: **safely collapsing a dbt + Omni monorepo from two branches down to one.**

### The Customer Problem

A customer runs a monorepo (dbt + Omni + Hightouch) with two branches: `main` for dbt and `omni-main` as Omni's Git base branch, plus a merge-forward workflow. It is safe but clunky, and Omni's history diverges from the repository's.

They cannot simply merge the branches because of a timing gap. Omni promotes authored model YAML **the moment a pull request merges**, via its Git-integration webhook. When a change renames or drops a warehouse column, Omni starts referencing the new schema before dbt has deployed it, and production content breaks in the window between merge and deployment.

### What We Built

Two complementary policies, both optional and disabled by default:

| Policy | Direction covered |
|---|---|
| **Breaking Change Hold** | A breaking **Omni** change merging ahead of its dbt deployment |
| **dbt Impact Analysis** | A **dbt** change orphaning a reference the Omni model still holds |

### The Central Constraint

**OmniFlow cannot intercept Omni's webhook.** No API consumer can. Promotion on merge is Omni platform behavior.

What OmniFlow does instead is prevent the **unsafe merge**. By the time anything reaches the base branch, either the change is additive (safe to promote immediately) or the dbt deployment has already landed. This is enforced at the GitHub branch-protection layer through a required check.

An administrative merge bypass still produces the gap. A complete guarantee would require an Omni-side promotion gate, which is not currently in Omni's public API. That is the "interrupt mechanism" this customer has been asking product for, and it remains the correct long-term answer.

---

## 2. System Architecture

### 2.1 Component Overview

```mermaid
flowchart TB
    subgraph triggers [Triggers]
        OmniPR[Omni creates PR via Git integration]
        DevPR[Developer opens dbt or app PR]
        Deploy[Push to protected base branch]
    end

    subgraph gha [GitHub Actions]
        Action[action.yml composite action]
    end

    subgraph cli [OmniFlow CLI]
        Route[omniflow route]
        Run[omniflow run]
        Sync[omniflow dbt sync]
        Doctor[omniflow doctor]
    end

    subgraph core [Core Engine]
        Discovery[discovery.py trusted routing]
        Config[config.py policy]
        Client[omni_client.py API]
        Diff[diff/ semantic graph and diff]
    end

    subgraph checks [Checks and Policies]
        ModelVal[validators/model.py]
        ContentVal[validators/content.py]
        Lint[validators/yaml_lint.py]
        Contracts[contracts.py downstream]
        Exposures[exposures.py dbt lineage]
        Hold[breaking_hold.py]
        Impact[dbt_impact.py]
    end

    subgraph out [Output]
        Reports[reporting/ json md sarif junit]
        Artifacts[artifacts.py public and restricted]
        Annotations[github/annotations.py]
    end

    OmniPR --> Action
    DevPR --> Action
    Deploy --> Action
    Action --> Route
    Action --> Run
    Action --> Sync
    Route --> Discovery
    Run --> Discovery
    Sync --> Discovery
    Discovery --> Config
    Run --> Client
    Sync --> Client
    Client --> Diff
    Diff --> ModelVal
    Diff --> ContentVal
    Diff --> Lint
    Diff --> Contracts
    Diff --> Exposures
    Diff --> Hold
    Run --> Impact
    ModelVal --> Reports
    ContentVal --> Reports
    Contracts --> Reports
    Hold --> Reports
    Impact --> Reports
    Reports --> Artifacts
    Reports --> Annotations
```

### 2.2 Module Inventory

| Module | Responsibility |
|---|---|
| `cli.py` | Argparse entry point and orchestration for every subcommand |
| `config.py` | `.omniflow.yml` parsing, strict schema validation, env overrides |
| `discovery.py` | Trusted routing from `.omni/flow.json`, changed-file inventory, PR marker |
| `omni_client.py` | Omni REST client: pagination, retry, bounded responses, redaction |
| `diff/semantic_graph.py` | Builds a graph of models, views, topics, fields, relationships |
| `diff/diff_engine.py` | Classifies changes and assigns risk (`info` → `breaking`) |
| `diff/yaml_loader.py` | Bounded, secure YAML loading |
| `validators/model.py` | Omni model validation API results and policy |
| `validators/content.py` | Content Validator results, history, new-only policy |
| `validators/yaml_lint.py` | Semantic lint rules |
| `contracts.py` | Maps breaking changes to referencing dashboards and queries |
| `exposures.py` | Optional dbt exposure lineage enrichment |
| `dbt_sync.py` | Post-deployment schema refresh with job polling |
| **`breaking_hold.py`** | **Breaking Change Hold policy** |
| **`dbt_impact.py`** | **dbt Impact Analysis cross-reference** |
| **`dbt_manifest.py`** | **dbt manifest parsing for precise column truth** |
| **`dbt_sql_diff.py`** | **Conservative SQL heuristic column extraction** |
| `security.py` | Redaction, path validation, secret rejection |
| `trust.py` | Reads config only from trusted base-branch sources |
| `artifacts.py` | Public and restricted artifact separation |
| `reporting/` | JSON, Markdown, SARIF, JUnit renderers |
| `repair/` | Unreleased AI repair scaffold, disabled |

### 2.3 Trust Model

```mermaid
flowchart LR
    subgraph untrusted [Untrusted]
        PRCode[Pull request head code]
        PRBody[Pull request description]
    end
    subgraph trusted [Trusted base branch]
        Flow[.omni/flow.json]
        Policy[.omniflow.yml]
        Workflow[.github/workflows]
    end
    subgraph secret [Secrets]
        Key[OMNI_API_KEY]
        SyncKey[OMNIFLOW_SYNC_API_KEY]
        StateToken[OMNIFLOW_SYNC_STATE_TOKEN]
    end

    PRBody -->|"filename list only, via API"| RouteStep[Credential-free preflight]
    Flow --> RouteStep
    Policy --> RouteStep
    RouteStep -->|"only if Omni change detected"| RunStep[Validation process]
    Key --> RunStep
    PRCode -.->|"never checked out or executed"| Blocked[Blocked by design]
```

Design rules enforced in code:

- The workflow uses `pull_request_target` but **never checks out or executes PR head code**
- A credential-free `omniflow route` preflight decides whether the secret is needed at all
- `base_url`, `model_id`, and policy come only from the protected base branch
- A PR marker may select among registered models; it **cannot** supply `base_url`
- Fork PRs never receive the Omni secret; fork PRs touching Omni files fail closed
- Omni-like files outside registered model paths fail closed

### 2.4 Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success, or an intentional skip |
| `1` | Validation or policy gate failed |
| `2` | Configuration or discovery error |
| `3` | Authentication or authorization error |
| `4` | Omni API error |
| `5` | Security policy violation |
| `6` | Internal error |

---

## 3. Policy Design

### 3.1 Breaking Change Hold

Evaluates only changes the semantic diff marks `breaking`: `field_deleted`, `field_renamed`, `field_type_changed`, `relationship_deleted`, `relationship_cardinality_changed`.

Two detection modes:

```mermaid
flowchart TD
    Start[PR validated] --> Diff[Semantic diff computed]
    Diff --> Breaking{Any breaking change?}
    Breaking -->|No| Pass[Pass]
    Breaking -->|Yes| SamePR{dbt files in this PR?}
    SamePR -->|Yes| BlockA["BLOCK: split required"]
    SamePR -->|No| SyncState{OMNIFLOW_LAST_SYNC_SHA set?}
    SyncState -->|No| PassB["Pass, pending check skipped"]
    SyncState -->|Yes| GitDiff["git diff sha...HEAD"]
    GitDiff --> Reachable{Commit reachable?}
    Reachable -->|No| WarnPass["Warn loudly, pass"]
    Reachable -->|Yes| DbtCommits{dbt-path commits newer?}
    DbtCommits -->|No| Pass
    DbtCommits -->|Yes| BlockB["BLOCK: deployment pending"]
```

Config:

```yaml
deployment:
  breaking_change_hold:
    enabled: true
    action: fail            # or warn
    dbt_paths: [models, seeds, snapshots, macros]
    pending_label: omniflow/awaiting-deploy
```

**Sync state mechanism.** After `omniflow dbt sync` succeeds, the deployment workflow records `GITHUB_SHA` as the repository variable `OMNIFLOW_LAST_SYNC_SHA`. Pull-request validation reads it as an action input and compares dbt-path commits against it. Writing a repository variable requires more than the default `GITHUB_TOKEN`, so a fine-grained `OMNIFLOW_SYNC_STATE_TOKEN` is used.

**Fail-open on weak evidence.** If the SHA is unset or unreachable (shallow checkout), OmniFlow prints a loud stderr warning and evaluates same-PR detection only. It does not block a merge on incomplete history, and it does not silently pretend to be protecting the branch.

### 3.2 dbt Impact Analysis

Runs on pull requests that OmniFlow would otherwise **skip**: dbt-path changes with no Omni model changes.

```mermaid
flowchart TD
    Start[PR opened] --> OmniFiles{Omni model files changed?}
    OmniFiles -->|Yes| Normal["Normal validation plus hold"]
    OmniFiles -->|No| DbtFiles{dbt-path files changed?}
    DbtFiles -->|No| Skip[Skip, exit 0]
    DbtFiles -->|Yes| Enabled{dbt_impact enabled?}
    Enabled -->|No| Skip
    Enabled -->|Yes| Mode{Manifest committed?}
    Mode -->|Yes| Manifest["Diff base and head manifest.json"]
    Mode -->|No| Heuristic["Parse model SQL select aliases"]
    Manifest --> Index[Index Omni YAML by file path]
    Heuristic --> Index
    Index --> Cross[Cross-reference removed columns against Omni fields]
    Cross --> Found{Orphaned references?}
    Found -->|No| Pass[Pass]
    Found -->|Yes| Block["BLOCK: orphaned Omni reference"]
```

Config:

```yaml
checks:
  dbt_impact:
    enabled: true
    manifest_path: target/manifest.json
    fail_on_orphaned_references: true
    omni_yaml_paths: [omni]        # defaults to .omni/flow.json model paths
    table_mapping:                 # only for custom schema macros
      - dbt_model: orders_v2
        sql_table_name: analytics.marts.orders
```

**Runs without an Omni credential.** This is pure static analysis over committed files, which is why it can occupy the previously-skipped path. No Omni API call, no warehouse query, no dbt invocation.

**Two modes.** Manifest mode uses `nodes[].columns` and `nodes[].relation_name` for exact truth. Heuristic mode parses the final top-level `SELECT` for aliases, stripping Jinja and comments, and deliberately reports nothing for `SELECT *` or unparseable SQL rather than inventing findings.

**View indexing by file path.** Discovered during real-repo testing: `build_graph` keys views by name, so three `dim_product.view.yaml` files across different schemas collapsed into one entry (37 files produced 11 views), hiding two views from the check. `dbt_impact.py` therefore builds its own index keyed by file path. `build_graph` was left unchanged because the diff engine depends on its behavior.

**Ambiguity is reported, not guessed.** When an unqualified relation name matches several distinct Omni relations, the finding carries `ambiguous_relation_match: true` and `candidate_relations`, and lists every candidate's orphaned fields.

---

## 4. Process Flows By Example

Each flow below is a distinct path a real pull request can take.

### Example 1: Omni-Only Repository

No dbt in the repository. Baseline OmniFlow behavior.

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant Omni
    participant GH as GitHub
    participant OF as OmniFlow

    Dev->>Omni: Edit view in a branch
    Dev->>Omni: Click "Create pull request"
    Omni->>GH: Create PR
    GH->>OF: pull_request_target triggers workflow
    OF->>OF: Route preflight, credential-free
    OF->>Omni: Pull base and branch YAML
    OF->>Omni: Validate model
    OF->>Omni: Run Content Validator
    OF->>OF: Semantic diff, lint, contracts
    OF->>GH: Redacted PR comment plus annotations
    Note over GH: Branch protection gates merge
    Dev->>GH: Approve and merge
    GH->>Omni: Webhook promotes branch
```

**Blocks on:** model validation errors, Content Validator issues under new-only policy, a renamed or deleted field referenced by published content.

### Example 2: Two-Branch Monorepo (Current Customer State)

`main` for dbt, `omni-main` for Omni.

```mermaid
flowchart LR
    subgraph omnipath [Omni path]
        A1[Omni branch] --> A2[PR to omni-main]
        A2 --> A3[OmniFlow validates]
        A3 --> A4[Merge]
        A4 --> A5[Omni promotes]
    end
    subgraph dbtpath [dbt path]
        B1[dbt branch] --> B2[PR to main]
        B2 --> B3[dbt CI]
        B3 --> B4[Merge]
        B4 --> B5[dbt deploys]
        B5 --> B6[omniflow dbt sync]
        B6 --> B7[Omni schema refreshed]
    end
    A5 -.->|"manual merge-forward, clunky"| B1
```

Safe, but the merge-forward step is manual and Omni's history diverges from the repository's. This is what the customer wants to eliminate.

### Example 3: Single Branch, Additive Change

The common case. No policy intervention.

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant OF as OmniFlow
    participant GH as GitHub
    participant Omni
    participant WH as Warehouse

    Dev->>GH: PR adds dbt column plus Omni field
    GH->>OF: Validate
    OF->>OF: Diff risk = info, not breaking
    OF->>OF: Hold does not fire
    OF-->>GH: Pass, exit 0
    Dev->>GH: Merge
    GH->>Omni: Webhook promotes YAML
    Note over Omni,WH: Old schema still valid, nothing breaks
    GH->>WH: dbt deploys new column
    GH->>OF: omniflow dbt sync
    OF->>Omni: Schema refresh plus revalidation
```

### Example 4: Single Branch, Breaking Change In One PR

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant OF as OmniFlow
    participant GH as GitHub

    Dev->>GH: PR renames column in dbt AND Omni view
    GH->>OF: Validate
    OF->>OF: Diff risk = breaking (field_renamed)
    OF->>OF: Changed files match dbt_paths
    OF-->>GH: BLOCK exit 1, rule = same_pull_request
    OF->>GH: Apply label omniflow/awaiting-deploy
    Note over Dev: Message instructs: dbt first, Omni second
    Dev->>GH: Split into PR A (dbt) and PR B (Omni)
```

**Why blocking is correct:** merging this promotes Omni YAML referencing `customer_key` while the warehouse still has `customer_id`. Every dashboard on that field fails until dbt deploys.

### Example 5: Breaking Change, Wrong Order (Pending Deployment)

PR A (dbt) has merged and awaits deployment. PR B (Omni) opens.

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant OF as OmniFlow
    participant WH as Warehouse
    participant Omni

    Note over GH: PR A (dbt rename) already merged, not deployed
    GH->>OF: PR B (Omni rename) opens, validate
    OF->>OF: Diff risk = breaking
    OF->>OF: No dbt files in PR B, same-PR rule quiet
    OF->>OF: Read OMNIFLOW_LAST_SYNC_SHA
    OF->>OF: git diff sha...HEAD finds PR A dbt commit
    OF-->>GH: BLOCK, rule = pending_dbt_deployment
    OF->>GH: Label omniflow/awaiting-deploy

    GH->>WH: Production dbt deploys
    GH->>OF: omniflow dbt sync
    OF->>Omni: Schema refresh, poll to terminal state
    OF-->>GH: Sync success
    GH->>GH: Record OMNIFLOW_LAST_SYNC_SHA
    GH->>GH: Remove label, enable auto-merge on PR B
    GH->>OF: Re-run required check on PR B
    OF->>OF: No pending dbt, hold clears
    OF->>Omni: Contract validation passes
    OF-->>GH: Pass
    GH->>Omni: Merge, webhook promotes against ready warehouse
```

### Example 6: Two dbt-Only PRs (Impact Analysis)

Neither PR touches Omni. Previously both merged and broke production on deploy.

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant GH as GitHub
    participant OF as OmniFlow
    participant Repo as Committed files

    Dev->>GH: PR renames revenue to total_revenue in dbt
    GH->>OF: Route: no Omni files changed
    Note over OF: Previously: skip, exit 0
    OF->>OF: dbt_impact enabled and dbt paths matched
    OF->>Repo: Read committed Omni YAML, no API call
    OF->>Repo: Diff manifest or parse model SQL
    OF->>OF: Omni view still defines revenue field
    OF-->>GH: BLOCK exit 1, orphaned Omni reference
    Note over Dev: Names the column, view, and file
```

**Ambiguity variant.** When several schemas hold the same table name and the model reference is unqualified:

```mermaid
flowchart TD
    Token["Unqualified token: dim_product"] --> Match[Match Omni relations]
    Match --> R1[coffee_training.analytics_marts.dim_product]
    Match --> R2[coffee_training.dbt_austin_marts.dim_product]
    Match --> R3[omni_dbt_marts.dim_product]
    R1 --> Flag
    R2 --> Flag
    R3 --> Flag
    Flag["ambiguous_relation_match = true, all candidates listed"] --> Resolve["Resolve with a manifest or table_mapping"]
```

### Example 7: Fork Pull Request

```mermaid
flowchart TD
    Fork[Fork PR opens] --> Detect{Head repo equals base repo?}
    Detect -->|Yes| Full[Full validation with secret]
    Detect -->|No| NoSecret[Secret withheld]
    NoSecret --> Touches{Touches Omni model files?}
    Touches -->|No| Skip[Skip cleanly, exit 0]
    Touches -->|Yes| FailClosed["Fail closed, exit 2"]
    FailClosed --> Move[Maintainer moves change to a same-repo branch]
```

### Example 8: Post-Deployment Synchronization

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant WH as Warehouse
    participant OF as OmniFlow
    participant Omni

    GH->>WH: Reviewed production dbt command
    Note over GH: OmniFlow runs only if dbt succeeded
    GH->>OF: omniflow dbt sync --auto
    OF->>OF: Reject PR events and non-base branches
    OF->>Omni: Resolve connection_id per model
    OF->>OF: Verify every affected shared model is registered
    OF->>Omni: Snapshot pre-refresh YAML
    OF->>Omni: POST refresh, one per connection
    loop Poll to terminal state
        OF->>Omni: GET job status
    end
    OF->>Omni: Rerun all enabled checks
    OF->>GH: Evidence artifacts, exit code
```

---

## 5. Decision Matrix

| Situation | Hold | Impact | Result |
|---|---|---|---|
| Additive Omni change | quiet | n/a | Pass |
| Additive Omni plus dbt, same PR | quiet | n/a | Pass |
| Breaking Omni, no downstream references | quiet | n/a | Warning, pass |
| Breaking Omni referenced by a dashboard | quiet | n/a | Fail, contract violation |
| Breaking Omni plus dbt, same PR | **fires** | n/a | Fail, split required |
| Breaking Omni, dbt deployment pending | **fires** | n/a | Fail, held until sync |
| Breaking Omni, dbt current | quiet | n/a | Pass |
| dbt-only, removes referenced column | n/a | **fires** | Fail, orphaned reference |
| dbt-only, removes unreferenced column | n/a | quiet | Pass |
| dbt-only, adds a column | n/a | quiet | Pass |
| dbt-only, deletes a referenced model | n/a | **fires** | Fail, orphaned view |
| Fork PR, no Omni files | n/a | n/a | Skip, exit 0 |
| Fork PR with Omni files | n/a | n/a | Fail closed, exit 2 |

---

## 6. Installation

### 6.1 Prerequisites

- Omni model already using Git integration and Branch Mode against the target repository
- Omni pull-request webhook configured
- GitHub plan supporting branch protection on the repository
- A dedicated least-privilege Omni service user and personal access token

### 6.2 Files

| File | Location | Purpose |
|---|---|---|
| `.omni/flow.json` | Protected base branch | Non-secret model identity: `base_url`, `model_id`, `model_path`, `base_branch` |
| `.omniflow.yml` | Protected base branch | Optional policy |
| `.github/workflows/omniflow.yml` | Protected base branch | Validation workflow, action pinned to a reviewed SHA |
| `.github/workflows/omniflow-dbt-sync.yml` | Protected base branch | Deployment workflow with sync and release |

### 6.3 Secrets And Variables

| Name | Type | Used by | Scope |
|---|---|---|---|
| `OMNI_API_KEY` | Secret | Validation | Read model, validate, Content Validator, list branches |
| `OMNIFLOW_SYNC_API_KEY` | Environment secret | dbt sync | Modeler, or Connection Admin for multi-model connections |
| `OMNIFLOW_SYNC_STATE_TOKEN` | Secret | Sync state recording | Repository variables, read and write, single repo |
| `OMNIFLOW_LAST_SYNC_SHA` | Variable | Pending detection | Written by the deployment workflow |

### 6.4 Full Single-Branch Policy

```yaml
contracts:
  enabled: true
  fail_on:
    deleted_referenced_fields: true
    renamed_referenced_fields: true
    referenced_field_type_changes: true
    referenced_join_cardinality_changes: true
    coverage_gaps: true

checks:
  content_validation:
    enabled: true
    fail_on_new_only: true
  model_validation:
    enabled: true
  semantic_lint:
    enabled: true
  dbt_impact:
    enabled: true
    manifest_path: target/manifest.json
    fail_on_orphaned_references: true

deployment:
  dbt_sync:
    enabled: true
    refresh_mode: hard
    post_sync_validation: true
  breaking_change_hold:
    enabled: true
    action: fail
    dbt_paths: [models, seeds, snapshots, macros]
    pending_label: omniflow/awaiting-deploy

security:
  redaction_level: standard
  retain_restricted_artifacts: false
```

### 6.5 Checkout Requirement

Pending-deployment detection compares a recorded commit against `HEAD`, so that commit must be reachable. Set `fetch-depth: 0` on the validation checkout when the hold is enabled. With a shallow checkout OmniFlow warns and evaluates same-PR detection only.

### 6.6 Recommended Rollout

1. Install validation only; leave both new policies disabled. Confirm a live Omni PR validates.
2. Make the OmniFlow check required in branch protection.
3. Enable `dbt_sync` against a non-production connection. Confirm refresh and revalidation.
4. Enable `breaking_change_hold` with `action: warn`. Observe for a week.
5. Enable `dbt_impact` with `fail_on_orphaned_references: false`. Tune `dbt_paths` and add `table_mapping` entries as needed.
6. Promote both to blocking once the false-positive rate is acceptable.
7. Add the release workflow so held PRs auto-merge after sync.

---

## 7. Evidence And Privacy

Every run writes root and redacted public summaries:

```text
.omniflow/
  report.json
  report.md
  report.sarif
  junit.xml
  evidence.json
  dbt-sync.json              # dbt synchronization runs
  dbt-impact.json            # impact analysis runs
  artifact-manifest.json
  public/
  restricted/<model_id>/
```

- Public reports exclude API keys, raw payloads, email addresses, document URLs, and folder paths
- `security.redaction_level: strict` additionally removes content names, query names, owners, labels, and free-text messages
- Restricted artifacts are deleted by default and are never uploaded by the example workflows
- Raw response output cannot be enabled in CI policy; `--unsafe-raw-output` exists only on a local debugging command
- No module persists warehouse rows, query results, or compiled SQL

---

## 8. Verification Status

### 8.1 Automated

| Suite | Result |
|---|---|
| Unit tests | 281 passing |
| Alpha simulations | 21 passing |
| `ruff` | Clean |
| `bandit` | Clean |
| Coverage | 79% overall; `dbt_impact` 81%, `dbt_manifest` 82%, `dbt_sql_diff` 93%, `breaking_hold` 93% |

### 8.2 Real-Repository Validation

Tested against [atx-omni/coffee-omni-training](https://github.com/atx-omni/coffee-omni-training), a genuine dbt plus Omni monorepo:

| Test | Outcome |
|---|---|
| Rename `standard_unit_price` in `dim_product.sql` | Blocked, exact column, view, and file named |
| Drop a referenced column | Blocked |
| Add a column | Passed with an informative note |
| Delete a dbt model | Blocked, orphaned view rule |
| Breaking Omni view plus dbt model, same PR | Held, same-PR rule |
| Additive Omni plus dbt | Passed |
| Qualified relation match | Precise, single schema, not flagged ambiguous |
| Unqualified match across three schemas | All candidates reported, flagged ambiguous |

That testing surfaced the view-name collision defect described in section 3.2. It would have shipped silently otherwise.

### 8.3 Not Yet Verified — Required Before Customer Reliance

- The **live release loop**: recording `OMNIFLOW_LAST_SYNC_SHA` via fine-grained token, label apply and remove, `gh pr merge --auto` completing under real branch protection
- Whether Omni's **model validation API** flags a field whose SQL references a column absent from the warehouse
- **Pending-deployment detection** against a real `fetch-depth: 0` checkout with a genuinely recorded sync SHA
- A customer's dbt project shape mapping cleanly to `dbt_paths` and producing a usable manifest

The recommendation is to run this gate on `coffee-omni-training` first: it has a real Omni model, real Git integration, and is non-production.

---

## 9. Known Limitations

### 9.1 Architectural

| Limitation | Consequence |
|---|---|
| Cannot intercept Omni's webhook | An admin merge bypass still produces the gap |
| Never queries the warehouse | Cannot confirm whether a column exists today |
| Does not run dbt | Relies on committed artifacts or file diffs |
| Evaluates PRs independently | No concept of a coordinated PR pair |

### 9.2 Detection

| Limitation | Mitigation |
|---|---|
| Hold detection is path-based, not semantic | Narrow `dbt_paths`; unrelated `models/` edits alongside breaking Omni changes are still held |
| Heuristic mode cannot resolve Jinja, macros, `ref()`, `source()` | Commit a dbt manifest |
| Custom schema macros break name-based mapping | Add `table_mapping` entries |
| Word-boundary column matching can over-report | Review `orphaned_fields` before assuming a break |
| Unqualified relations are ambiguous across schemas | Reported explicitly; resolve with a manifest |
| Pending detection needs history and a recorded SHA | `fetch-depth: 0` plus `OMNIFLOW_SYNC_STATE_TOKEN` |

### 9.3 Known Interaction: Coordinated Rename Deadlock

The two policies are individually correct but can conflict on a coordinated rename:

```mermaid
flowchart TD
    Goal["Goal: rename customer_id to customer_key in dbt and Omni"] --> Split[Split into two PRs]
    Split --> PRA["PR A: dbt only"]
    Split --> PRB["PR B: Omni only"]
    PRA --> ImpactCheck[Impact check runs]
    ImpactCheck --> BlockA["BLOCKED: Omni still references customer_id"]
    PRB --> HoldCheck[Hold check runs]
    HoldCheck --> BlockB["BLOCKED: dbt deployment pending"]
    BlockA --> Deadlock["Neither PR can merge"]
    BlockB --> Deadlock
```

**Current workarounds:**

- Run `dbt_impact` with `fail_on_orphaned_references: false` during coordinated migrations
- Run `breaking_change_hold` with `action: warn` and rely on reviewer discipline for the ordering
- Merge the dbt PR with an explicit admin override, deploy, sync, then merge the Omni PR normally

**Proposed resolution (not built).** A coordinated-change marker, for example a shared label or a PR-body reference, that tells OmniFlow two PRs are a pair. The impact check would then suppress an orphan finding when a paired Omni PR resolving that exact column is open, and the hold would release the Omni PR as soon as its paired dbt PR deploys. This is the natural next feature and should be scoped only if the customer hits it in practice.

---

## 10. Product Recommendations

Ordered by leverage.

1. **Omni promotion gate API.** The only complete fix for the merge-to-promotion window. A way to hold or defer shared-model promotion until released would let any CI tool close the gap fully, and it is what this customer originally asked for. **High** value.

2. **Machine-readable Omni PR metadata.** Omni's PR description guarantees a branch-content link, but no documented stable payload carries the host and model ID. OmniFlow therefore requires committed `.omni/flow.json` bootstrap metadata and deliberately refuses to send a token to a host parsed from PR text. A documented payload would remove that setup step. **Medium** value.

3. **Document whether model validation resolves against live warehouse schema.** This determines whether a dangling column reference is already caught for free. It changes how much detection logic any CI tool needs. **Medium** value, low cost.

4. **Documented Modeling Agent mutation API.** The AI repair scaffold in the repository remains disabled because the documented AI Jobs API is query-oriented and does not expose branch editing. **Low** urgency.

---

## 11. References

### OmniFlow Documentation

- [Installation](https://github.com/atx-omni/OmniFlow/blob/main/docs/INSTALLATION.md)
- [Configuration reference](https://github.com/atx-omni/OmniFlow/blob/main/docs/CONFIGURATION.md)
- [Breaking Change Hold](https://github.com/atx-omni/OmniFlow/blob/main/docs/BREAKING_CHANGE_HOLD.md)
- [dbt Impact Analysis](https://github.com/atx-omni/OmniFlow/blob/main/docs/DBT_IMPACT.md)
- [Post-Deployment dbt Synchronization](https://github.com/atx-omni/OmniFlow/blob/main/docs/DBT_SYNC.md)
- [Testing matrix](https://github.com/atx-omni/OmniFlow/blob/main/docs/TESTING.md)
- [Security model](https://github.com/atx-omni/OmniFlow/blob/main/docs/SECURITY_MODEL.md)
- [Troubleshooting](https://github.com/atx-omni/OmniFlow/blob/main/docs/TROUBLESHOOTING.md)

### Omni Official

- [Branch Mode](https://docs.omni.co/content/develop/branch-mode)
- [Git integration settings](https://docs.omni.co/integrations/git/settings)
- [Git integration best practices](https://docs.omni.co/integrations/git/best-practices)
- [Model YAML API](https://docs.omni.co/api/models/get-model-yaml)
- [Model validation API](https://docs.omni.co/api/models/validate-model)
- [Content Validator API](https://docs.omni.co/api/content-validator/validate-content)
- [Refresh schema API](https://docs.omni.co/api/models/refresh-schema)
- [Job status API](https://docs.omni.co/api/jobs/get-job-status)
- [dbt exposures API](https://docs.omni.co/api/dbt/get-dbt-exposures)
- [Schema refresh behavior](https://docs.omni.co/modeling/develop/schema-refreshes)

### GitHub

- [Secure use of Actions](https://docs.github.com/en/actions/reference/security/secure-use)
