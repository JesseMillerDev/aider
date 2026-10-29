# Fifty-task GPT-6.1 Sol baseline

The control runs Aider revision `8400741ce7a9d26aecdbe97da423de5f3e80def1` with the shared
ChatGPT transport shim. LSP graph enrichment, paging and agent navigation are not enabled.
The frozen selection contains 50 SWE-bench Lite tasks from eight repositories, seed 42.
Its task fingerprint is `cb366de5c8f2dfd14dc1aef20043511304be9e1f40b808bc125b1962a547e52a`.

## Current run

Experiment: `81472973655c74441877`.
Artifacts: `benchmark/results/baseline-50/` (ignored by Git).

- `baseline_manifest.json`: task inputs, dataset checksum, control revision, image IDs and settings.
- `generate_status.json`: generated count and generation state, including an exact stop reason.
- `grade_status.json`: grader state; `progress.json`: completed/resolved counts.
- `baseline_report.md`: interim coverage and final Pass@1, withheld until all tasks complete.
- `control_stock_aider/<instance_id>.json`: measurements and official grading provenance.
- `transcripts/<instance_id>/`: per-call request fingerprints, completed responses and token usage.
- `agents/<instance_id>/`: public task, Aider log, telemetry and generated patch.
- `grading/<instance_id>/`: evaluator-only dataset, prediction and raw SWE-bench reports/logs.

The initial model-backed pilot, `matplotlib__matplotlib-25433`, was **resolved** by the official
grader. It required two model calls, 58,239 input tokens and 1,068 output tokens. Its first edit
was attempted on call 2 and oracle-file recall by turn 3 was 1.0. This single pilot is not a
50-task score. Generation and grading then continued as separate background stages.

The model uses `gpt-6.1-sol`, medium reasoning, with at most 25 completed model round trips
per task. Temperature and API dollar costs are unavailable on the subscription route.
Provider token usage is recorded directly; cached input is included in total input.
If a failed request leaves usage unknown, the final token measurements remain unavailable
rather than being imputed as zero.

## Isolation and stock behavior

The agent container is unprivileged, has networking disabled, and has a read-only root filesystem.
Its repository clone is in an ephemeral workspace. It receives a read-only source repository and
a writable artifact directory containing only its own public task and outputs. It receives no
OAuth credentials, Docker socket, gold patch, test patch, expected test names or oracle file labels.

Model inference is performed on the host through a narrow JSON-line stdio protocol. The worker
sends only Aider's model request; the host returns a completed, verified response. Aider itself
still performs repository-map selection, file addition, SEARCH/REPLACE application and lint
reflection. Repository-map budget is 1,024 tokens. Automatic lint is enabled; automatic test/shell
execution and commits are disabled. Official tests run in the separate SWE-bench evaluator.

The evaluator can access the Docker engine and oracle data; its task containers do not receive
the agent's credentials. Grading run IDs include the task and patch fingerprint so changed patches
cannot silently reuse an old report. No patch is recorded explicitly as an empty prediction,
not as a passed test run. Missing reports, crashes and quota/policy failures are not model losses.

## Build and start a new run

From the repository root, with the ChatGPT connection already configured:

```powershell
.venv/Scripts/python.exe -m benchmark.build_agent
docker build --tag better-aider-swebench-grader:4.1.0 benchmark/grader
.venv/Scripts/python.exe -m benchmark.select_subset --limit 50 --output benchmark/subset_50.json
.venv/Scripts/python.exe -m benchmark.run_baseline --limit 1
```

The builder archives the pinned control revision rather than copying the current working Aider
tree. It adds the worker, checks its hash inside the built image, and saves a dependency snapshot.
Use a new `--output` directory if the frozen configuration or images change. Do not rebuild or
retag runtime images during an active run. The manifest retains their immutable IDs.

After the single-task pilot succeeds, continue the two stages in separate terminals:

```powershell
.venv/Scripts/python.exe -u -m benchmark.run_pipeline --phase generate
.venv/Scripts/python.exe -u -m benchmark.run_pipeline --phase grade --watch
```

The second command waits for new patches. Generation is sequential; grading is sequential.
They overlap so a cold task-image download does not prevent model work on another task.
On the development machine these stages were launched as hidden background processes with logs:

- `benchmark/.cache/baseline-generate.log` and `.err.log`
- `benchmark/.cache/baseline-grade.log` and `.err.log`

Check progress without making any inference requests:

```powershell
Get-Content benchmark/results/baseline-50/generate_status.json
Get-Content benchmark/results/baseline-50/progress.json
Get-Content benchmark/.cache/baseline-generate.log -Tail 10
Get-Content benchmark/.cache/baseline-grade.log -Tail 10
```

Do not launch a second generation process for the same run. Phase and per-task locks prevent
duplicate requests. On resume, completed model calls are replayed only when their exact request
fingerprints match; completed patches and official reports are reused. Subscription failures stop
new generation and retain the exact error; they do not trigger an API-key fallback or a guessed
capacity diagnosis. Restart the affected stage after resolving the reported cause.

## Measurement notes

First-edit token usage includes the model response that contains the first edit attempt.
Tasks with no edit attempt retain null first-edit metrics. Failed edit count records turns that
produced malformed edits or lint errors. The turn-3 localization snapshot is taken after the third
response's file handling; tasks finishing earlier use their final file set. L1 peak counts full
source text; L2 is zero for this control because paging is disabled.

The baseline is a historical control run, not an interleaved A/B comparison. A future treatment
comparison must match task/model/settings identities and explicitly account for run timing and
provider behavior. No statistical treatment improvement is claimed from this baseline alone.

Validation before launch: isolated Docker/stdio edit smoke passed; the official first-task grade
resolved the issue; 50 runner/benchmark contract tests passed. The runner tests exercise replay,
request identity, call limits, quota/error handling, cache accounting, missing usage, empty patches,
and withholding Pass@1 when coverage is incomplete.

The combined runner, benchmark and subscription regression suites subsequently passed 95 tests.
