# AI Eval

OmniFlow's AI eval check compares existing prompt sets against `main` and the pull request's Omni branch. It is optional, **disabled by default**, and remains a controlled-alpha capability pending non-production acceptance. Its current comparison policy supports only explicit binary scores (`1` = pass, `0` = fail). Other numeric scores fail with an operational error; OmniFlow does not invent a passing threshold for them.

## Why This Is Opt-In, Not A Default Check

Routine validation uses YAML and validation/metadata endpoints. AI eval starts agentic jobs for configured prompts, consumes service capacity, and depends on the configured judge. Starting a run is an external operation even though it is not a semantic-model write. Review credential permissions, prompt contents, query behavior, AI credit usage, and any warehouse costs in the selected tenant before enabling it. The public endpoint contract does not establish a blanket query-execution or cost guarantee.

## What It Detects

| Situation | Result |
| --- | --- |
| A prompt passes against `main` and fails against the branch | Regression: fails the check |
| A prompt fails against both `main` and the branch | Not a regression: pre-existing failure, reported but does not gate |
| A prompt fails against `main` and passes against the branch | Improvement: reported, never gates |
| No Omni branch is available for this context | Skipped with a note; the check does not fail |
| No configured set belongs to this model | Skipped explicitly for this model |
| Cancelled/failed run, failed prompt job, or missing score after the grace period | Operational failure, always blocks |
| Missing/duplicate prompt rows, malformed results, wrong run identity, or an empty/archived set | Operational failure, always blocks |
| Numeric score other than exactly `0` or `1` | Unsupported scoring contract; operational failure |

`fail_on_regression: false` changes complete accuracy regressions into warnings. It never downgrades operational failures. `security.max_report_samples` only limits sampled opaque prompt IDs, including when set to zero; full regression counts always determine the gate.

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
      - id: 00000000-0000-0000-0000-000000000000  # replace with your own prompt set ID
        label: Core revenue prompts
        model_id: 11111111-1111-1111-1111-111111111111  # optional routing hint, verified against Omni
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Turns the check on. Must be explicitly enabled. |
| `prompt_sets` | `[]` | List of `{id, label, model_id}` entries. Maximum 20; only `id` is required. `label` defaults to `id` and is restricted metadata. Optional `model_id` filters contexts before remote lookup and must match the server's set ownership. |
| `fail_on_regression` | `true` | `true` blocks the merge on any regression; `false` reports the same regressions as warnings only. |
| `poll_interval_seconds` | `10` | Seconds between run-status polls. Bounded 2-30. |
| `timeout_seconds` | `900` | Seconds to wait for both runs (main and branch) to finish before failing with an operational error. Bounded 30-3600. |
| `scoring_grace_seconds` | `180` | Extra seconds to keep polling for scores after a run reaches a terminal status; Omni's scoring lands a few seconds after the run itself completes. Bounded 0-600. |

The check uses `OMNI_API_KEY`; do not assume an existing routine-validation credential automatically has eval permission. The [prompt-set API](https://docs.omni.co/api/ai-eval/get-an-eval-prompt-set) documents at least Querier model access. Preflight fetches prompt-set ownership and the exact prompt inventory before creating runs. Only sets belonging to the current model run against its branch. Every returned run is checked against the requested run ID, model, set, and branch. For repositories spanning Omni instances, provide `model_id` so unrelated contexts do not attempt lookup on the wrong instance.

The [start-run API](https://docs.omni.co/api/ai-eval/start-an-eval-run) describes a per-user active-run limit. OmniFlow starts at most one baseline/branch pair at a time and evaluates sets and model contexts sequentially. Concurrent PR jobs using the same service identity can still compete for that limit: serialize eval-enabled workflows with a shared GitHub concurrency group and `cancel-in-progress: false`. GitHub concurrency groups coordinate only within one repository; users shared across repositories need external scheduling or separate approved identities. Capacity errors fail operationally; run creation is never retried automatically.

## Evidence

Each run writes two artifacts:

```text
.omniflow/
  report.json          # aggregate report; ai_eval appears in check_reports
  report.md            # includes an "AI Eval" section per model
  restricted/<model>/
    ai-eval-detail.json  # restricted: full per-prompt rows, conversation IDs, timing
    ai-eval-runs.json    # restricted: durable run IDs, last status, cleanup/recovery state
```

`ai-eval-detail.json` is written only to the restricted artifact path and is never included in `public/`. It carries the same kind of data-bearing fields (timing, conversation ID) that `get_ai_job_status` already discards for AI Repair, for the same reason: it can reflect customer prompt and answer content.

The public report (`report.json`, `report.md`, `report.sarif`, `junit.xml`) carries only:

- Per prompt set aggregate accuracy (main vs. branch)
- Complete regression counts and a bounded list (`security.max_report_samples`) of opaque hashed prompt IDs
- Fixed operational reason codes and a cleanup-required flag

Prompt text, configured labels, judge errors, conversation identifiers, and response details never enter any public format, even with standard redaction. The restricted detail maps opaque prompt IDs to full prompts. Restricted artifacts follow the default deletion policy; explicitly enable approved restricted retention when recovery evidence must survive the run, and keep uploads restricted to authorized operators.

## Failure Recovery

Known run IDs are written before polling or starting the next run. On handled errors and timeouts, OmniFlow reads each known unfinished run, validates its identity, requests [cancellation](https://docs.omni.co/api/ai-eval/cancel-an-eval-run), then rereads to confirm a terminal state. Cancellation archives the run according to the API; it does not undo consumed work. A lost cancellation response is reconciled once rather than blindly retried. Identity mismatches or unconfirmed cleanup require operator review.

A failed creation without a returned ID is ambiguous: there may be a remote run that OmniFlow cannot identify safely. The journal records that outcome and the public report flags cleanup as required. Operators must reconcile it in Omni before rerunning. A forcibly killed runner cannot execute cleanup. Where restricted retention is approved, use its journal to reconcile known runs; otherwise inspect the service identity's recent runs in Omni. Do not blindly rerun or cancel unrelated runs owned by the same user.

## Limitations

- **A pass does not mean the prompt sets are comprehensive.** AI eval only evaluates the prompt sets you configure; it says nothing about prompts you have not written.
- **No new prompt sets are created here.** Add or edit prompt sets in Omni directly; this check only runs the ones it's told about.
- **Binary scoring is a narrow limitation.** The [run response](https://docs.omni.co/api/ai-eval/get-an-eval-run) documents numeric scores, including fractional examples, without defining a binary passing threshold. Fractional, non-finite, string, boolean, or missing scores cannot establish a pass. Confirm the tenant's scoring contract before enabling this check; fractional scoring needs a separately reviewed policy extension.
- **Prompt coverage is exact.** Duplicate prompt text is rejected because run-result IDs are distinct from prompt-set IDs and the documented comparison join uses prompt text. Edits to a prompt set during a pair of runs, missing rows, and duplicated results cannot establish a complete comparison.
- **Wall-clock time scales with prompt set count.** `timeout_seconds` applies per pair, with bounded additional API request and cleanup time. No cross-repository scheduler or automatic retry exists.
- **Pass/fail comes from a judge's scoring, not deterministic validation.** Treat a regression as a signal to review, the same way you would a flaky test.

## Acceptance Before Enablement

Offline CLI/artifact tests cover complete regressions, zero sampling, failed/cancelled/incomplete outcomes, unsupported scores, model routing, duplicate coverage, privacy, partial creation, and timeout cleanup. They do not prove tenant support. Keep eval disabled until a named non-production environment confirms prompt-set and run identity, the binary scoring policy, a known regression, permission/quota behavior, cancellation/recovery, and the final public/restricted artifact boundary. Record tenant/API version, immutable code revision, approved prompts, and observed results without publishing customer data.

## Related

- [Post-Deployment dbt Synchronization](DBT_SYNC.md) is the other opt-in check that talks to a live system rather than only committed files.
- [Security Model](SECURITY_MODEL.md) covers what data this check does and does not persist.
