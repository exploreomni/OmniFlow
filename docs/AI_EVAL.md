# AI Eval

OmniFlow's AI eval check runs each configured Omni AI eval prompt set against `main` and against the pull request's Omni branch, then fails the check on any prompt that passed on `main` but failed on the branch. It is optional and **disabled by default**.

## Why This Is Opt-In, Not A Default Check

Every other OmniFlow check is static: it reads committed YAML, calls read-only validation and metadata endpoints, and never asks Omni AI to answer a question. AI eval is different on both axes that matter for a CI gate:

- **It executes warehouse queries.** Each eval run has Omni AI answer real prompts against the branch's semantic model, which can run real SQL against the connected warehouse.
- **It spends money.** Every prompt incurs LLM cost for the answer and for the judge that scores it, reported per run as `main_cost_usd` / `branch_cost_usd`.

Enabling `checks.ai_eval` means every pull request that touches the configured model runs this cost and query load automatically. Treat it the same way as [post-deployment dbt sync](DBT_SYNC.md) or [AI Repair](AI_REPAIR.md): review the prompt sets, the expected per-PR spend, and the warehouse load before turning it on as a required check.

## What It Detects

| Situation | Result |
| --- | --- |
| A prompt passes against `main` and fails against the branch | Regression: fails the check |
| A prompt fails against both `main` and the branch | Not a regression: pre-existing failure, reported but does not gate |
| A prompt fails against `main` and passes against the branch | Improvement: reported, never gates |
| No Omni branch is available for this context | Skipped with a note; the check does not fail |

Cost changes are always reported and never gate. Only a genuine accuracy regression fails the check.

## Prerequisite

The prompt sets you list in policy must already exist in Omni. This check evaluates existing prompt sets; it does not create them.

## Enable The Policy

```yaml
checks:
  ai_eval:
    enabled: true
    fail_on_regression: true
    poll_interval_seconds: 10
    timeout_seconds: 900
    scoring_grace_seconds: 180
    prompt_sets:
      - id: 9ff94a07-4081-4ef6-9e6a-e542424cb3bf
        label: Core revenue prompts
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turns the check on. Must be explicitly enabled. |
| `prompt_sets` | `[]` | List of `{id, label}` entries, one per Omni AI eval prompt set to run. Maximum 20. `label` defaults to `id` when omitted. |
| `fail_on_regression` | `true` | `true` blocks the merge on any regression; `false` reports the same regressions as warnings only. |
| `poll_interval_seconds` | `10` | Seconds between run-status polls. Bounded 2-30. |
| `timeout_seconds` | `900` | Seconds to wait for both runs (main and branch) to finish before failing with an operational error. Bounded 30-3600. |
| `scoring_grace_seconds` | `180` | Extra seconds to keep polling for scores after a run reaches a terminal status; Omni's scoring lands a few seconds after the run itself completes. Bounded 0-600. |

The check runs once per Omni model context, alongside model and content validation, using the same `OMNI_API_KEY` already required for routine validation. Starting an eval run and reading its results are not model writes, so no dedicated token is required; the pull-request-validation credential already covers it.

Omni allows at most two AI eval runs in progress organization-wide, so prompt sets are evaluated one at a time (`main` then the branch, per set), never all at once. A repository with several prompt sets configured should expect the check's wall-clock time to grow linearly with the number of sets.

## Evidence

Each run writes two artifacts:

```text
.omniflow/
  report.json          # aggregate report; ai_eval appears in check_reports
  report.md            # includes an "AI Eval" section per model
  <model>/
    ai-eval-detail.json  # restricted: full per-prompt rows, cost breakdown, conversation IDs, timing
```

`ai-eval-detail.json` is written only to the restricted artifact path and is never included in `public/`. It carries the same kind of data-bearing fields (per-prompt cost, timing, conversation ID) that `get_ai_job_status` already discards for AI Repair, for the same reason: it can reflect customer prompt and answer content.

The public report (`report.json`, `report.md`, `report.sarif`, `junit.xml`) carries only:

- Per prompt set aggregate accuracy (main vs. branch) and total LLM spend
- A bounded list (`security.max_report_samples`) of the prompts that regressed, each truncated to 300 characters, with the judge's error reason when Omni provided one

This keeps the reviewer-facing summary useful (which prompts regressed, and by how much accuracy and spend moved) without publishing full per-prompt cost breakdowns or conversation identifiers.

## Limitations

- **This check queries the warehouse and spends money.** See the section above before enabling it as a required check.
- **A pass does not mean the prompt sets are comprehensive.** AI eval only evaluates the prompt sets you configure; it says nothing about prompts you have not written.
- **No new prompt sets are created here.** Add or edit prompt sets in Omni directly; this check only runs the ones it's told about.
- **The two-run limit means wall-clock time scales with prompt set count.** Keep `timeout_seconds` generous if you configure several prompt sets.

## Related

- [Post-Deployment dbt Synchronization](DBT_SYNC.md) is the other opt-in check that talks to a live system rather than only committed files.
- [Security Model](SECURITY_MODEL.md) covers OmniFlow's broader no-query-execution posture and why this check is the documented exception.
