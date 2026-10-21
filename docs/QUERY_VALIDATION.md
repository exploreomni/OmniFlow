# Reviewed query-result validation (experimental, default off)

Model and content validation can catch invalid references without catching a
valid-but-wrong join. A join can preserve the grand total while moving every row
into a NULL group. Query contracts add **bounded evidence**, not a guarantee that
all business behavior is correct.

This OmniFlow feature is experimental. It is separate from the AI-eval check.
The standard installation does not execute these queries or call the AI API.

## What executes

An engineer approves a fixed structured query and independent expectations in
the trusted base branch's `.omniflow.yml`. The same query runs uncached against
the verified base, PR branch, and base again. A changed reread, insufficient
population, unsupported response or missing provenance is **inconclusive**.
Existing assertion violations are nonblocking when unchanged or improved;
new/worsened violations block only in enforce mode.
Lower- and upper-bound violations are distinct: changing from below a reviewed
range to above it is a new finding, even if the distance from the range is equal
or smaller. Unchanged or improved debt is matched only on the same bound.

The first adapter supports **one-row numeric aggregate probes**, with a hard
two-row result limit to detect accidental grouping/truncation. It supports:

- NULL rate and duplicate-grain rate (`1 - distinct / population`).
- Group-count bounds and aggregate-value bounds.
- Absolute/relative aggregate changes and absolute rate changes.
- Absolute assertions for a topic verified newly added by the PR; it must not
  be an existing topic relabeled as having no baseline. New views used by an
  existing topic use normal regression contracts.

Measures must express the intended grain. Symmetric aggregation can hide fanout;
a passing total alone is not a fanout test. Include an independently reviewed
join-row/unique-key probe, NULL probe and group-count probe as appropriate.
The `aggregate_only` and `independent_oracle` flags record review, not automatic
proof of a measure's semantics.

## Configure deliberately

Put `query_validation` **inside the existing `checks:` mapping**, alongside
`model_validation` and `content_validation`. Do not create a second `checks:` key.
The example below uses fictional measures that you must define and qualify first.

```yaml
checks:
  # Keep your existing model/content/lint settings here.
  query_validation:
    mode: report                 # off (quoted), report, or enforce
    native_qualified: false       # only true after live tenant qualification
    snapshot_attestation: "Reviewed immutable fixture/version evidence reference"
    # timezone: UTC               # only if user-specific timezones are enabled
    max_queries: 12
    timeout_seconds: 120
    max_response_bytes: 1048576
    max_polls: 2
    contracts:
      - id: orders_join_quality
        comparison: regression
        reviewed: true
        aggregate_only: true
        independent_oracle: true
        fixed_window: true
        # Optional: use ONLY after inspecting your tenant's exact JSON response
        # metadata. Omit for canonical field keys with JSON numeric values.
        # result_fields:
        #   orders.join_rows: Joined Rows
        #   orders.distinct_order_ids: Distinct Order IDs
        #   orders.unmatched_store_rows: Unmatched Store Rows
        #   orders.store_group_count: Store Group Count
        # numeric_encoding: number_or_decimal_string
        query:
          table: orders
          join_paths_from_topic_name: orders
          fields:
            - orders.join_rows
            - orders.distinct_order_ids
            - orders.unmatched_store_rows
            - orders.store_group_count
          filters: {}             # use an immutable fixture or reviewed fixed window
        population_field: orders.join_rows
        minimum_population: 100
        assertions:
          - type: unique_grain
            population: orders.join_rows
            distinct: orders.distinct_order_ids
            maximum: 0
          - type: null_rate
            numerator: orders.unmatched_store_rows
            denominator: orders.join_rows
            maximum: 0
          - type: group_count
            field: orders.store_group_count
            minimum: 3
            maximum: 3
```

`report` never blocks on this optional check, but incomplete evidence remains
advisory, not a pass. `enforce` requires `native_qualified: true` and a stable-data
attestation; it blocks on regression or incomplete evidence. Other safety checks
remain independent. Omit the section or use `mode: "off"` to disable it completely.
There is no automatic retry of a query-execution POST.

Optional `environment_connection_id`, `user_id` and `timezone` apply identically
to both sides. By default timezone is omitted, inheriting the connection setting.
An explicit IANA timezone requires the connection's user-specific timezone
support; a rejected override remains an error, with no silent fallback. These
parameters and operator attestations do not independently prove the effective
server-side context. Verify that context during qualification.
The execution context injects model and branch; contracts cannot
override them. Verify identical access/filter scope. User impersonation requires
an organization key with appropriate access; do not use it with a PAT. Do not
put credentials, patient identifiers or sensitive filter literals in policy.
Environment-variable substitution is rejected in query policy.

Some native JSON exports use display labels and numeric strings even with
`formatResults: false`. If qualification observes that response, declare an exact
`result_fields` mapping from **every** requested field to its returned key, and
explicitly choose `numeric_encoding: number_or_decimal_string`. Mappings must be
one-to-one; missing/extra/duplicate labels remain inconclusive. No aliases are
guessed. The default is identity mapping and `numeric_encoding: json_number`.
Only bounded plain finite decimal strings are accepted in the opt-in mode, never
currency/comma formatting, booleans, NULLs or non-finite values. Comparisons use
decimal arithmetic; raw values are never emitted in reports.

## Optional AI proposal — local preparation only

```sh
omniflow query propose --config .omniflow.yml \
  --base-url https://your-company.omniapp.co --model-id YOUR_MODEL_ID \
  --topic orders --prompt "Propose aggregate probes for join quality" \
  --output .omniflow/restricted/query-proposal.json
```

Supply `OMNI_API_KEY` securely through your local environment. This command is
rejected in GitHub Actions. It calls the documented AI generate-query endpoint
with `runQuery: false`, `workbookUrl: false` and `queryAllViews: false`. It saves one
private, unapproved proposal; it does **not** execute a query or edit policy.
Existing output files are never overwritten. Treat its field names and filters
as confidential. Review/translate it into the restricted contract schema and
independent assertions; unsupported AI output is not silently dropped.

## Qualification and limits

1. Use an isolated non-production branch and stable fixture; preserve originals.
2. Run known-good probes in report mode. Verify exact numeric field names,
   population, identity, environment, timezone and filters.
3. Intentionally introduce a wrong join, duplicate grain and extra NULLs. Confirm
   each relevant assertion detects it even when the total remains unchanged.
4. Verify unchanged baseline debt stays nonblocking; test empty results,
   changed data and denied access as inconclusive.
5. Repair the fixture, verify recovery, then approve enforce mode in trusted policy.

Rereading base detects observed drift; it does not create a transactional data
snapshot. A fixed time window may still be mutable. Attest only independently
stable data. Query limits/client deadlines are not warehouse cost limits or
cancellation guarantees; use warehouse-side resource controls. Model/content
health checks remain available when query checks are disabled.

Budgets apply per model/check invocation, including rereads, not across an entire
multi-model workflow. The same configured contracts apply to every selected
model: use a policy/target scope where each contract is valid and qualified, and
account for the multiplied warehouse cost.

The adapter accepts complete synchronous JSON records. It can reconcile known
asynchronous job IDs within bounds, but Arrow-only results stay inconclusive;
it never launches a duplicate query to recover missing results. Raw rows,
aggregate values, prompts and contract IDs are not included in public reports.
Reports identify the numbered contract/assertion, safe failure category and
corrective action. Match the numbers to the trusted policy used for that run.

Official API contracts: [Run query](https://docs.omni.co/api/queries/run-query),
[Wait for results](https://docs.omni.co/api/queries/wait-for-query-results),
[Generate query](https://docs.omni.co/api/ai/generate-a-query),
[Authentication](https://docs.omni.co/api/authentication).
