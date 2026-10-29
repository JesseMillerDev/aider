# Frozen comparison baseline

Use [gpt-6.1-sol-subscription-50.json](gpt-6.1-sol-subscription-50.json) as the fixed control
record for evaluating the improved harness. Preserve this record; save treatment results separately.

Experiment: `81472973655c74441877`.
Control revision: `8400741ce7a9d26aecdbe97da423de5f3e80def1`.
Model: `chatgpt/gpt-6.1-sol`, medium reasoning, ChatGPT subscription billing.

| Baseline measure | Value |
|---|---:|
| Tasks completed | 50 |
| Resolved | 32 |
| Unresolved | 18 |
| Pass@1 | **64%** |
| Infrastructure errors | 0 |
| Input tokens | **2,114,323** |
| Output tokens | **40,873** |
| Combined tokens | **2,155,196** |
| Reported cached input tokens | 0 |
| Model calls | 108 |
| Average combined tokens per task | 43,103.92 |
| Average model calls per task | 2.16 |
| Tasks with complete usage measurements | 50 |
| API dollar cost | Unavailable (`null`) |

There are 47 official grading reports and three empty predictions. Empty predictions are
unresolved outcomes; they are not represented as test runs. Input already includes cached
input, and output includes provider-reported reasoning tokens. Cache reads were reported as
zero; this does not establish how the provider implemented caching internally.

The JSON includes every task's ID, repository, base commit, resolution, first-edit timing,
token measurements, failed edits, turn-3 localization recall, peak context sizes, and grading
provenance. It also retains source-record/report hashes, the source-manifest hash, task/dataset
fingerprints, image IDs, worker hash, and effective settings. Raw prompts, credentials and model
response text are not copied into this versioned record. Full local artifacts remain under
`benchmark/results/baseline-50/`.

## Compare the improved version

- Use the identical 50 task IDs and base commits; verify the recorded task/dataset fingerprints.
- Keep GPT-6.1 Sol, medium reasoning, the 25-call limit, and common execution settings fixed.
  Declare treatment flags and source/image revisions explicitly.
- Join task records by `instance_id` for paired token, edit, localization and resolution comparisons.
- Report total input/output/combined-token changes and the number of tasks with measurements.
  Preserve missing or inapplicable values as `null`; never replace them with zero.
- Preserve subscription USD cost as unavailable. Do not estimate subscription spending from
  API token prices.
- Treat this as a historical control. Account for provider/model drift and run timing; rerun or
  interleave a control if stronger causal evidence is required.

The first-edit token measure includes the response containing the first edit attempt. Tasks
without an edit attempt have null first-edit metrics. See the JSON's `metric_definitions` and
[execution notes](../BASELINE_RUN.md) for the remaining measurement conventions.
