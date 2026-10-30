# Idle-only paging: fifty-task treatment

Experiment: `55587c4cef4936fd31b6`.
Artifacts: `benchmark/results/pager-idle-50-v1/` (ignored by Git).
Host log: `benchmark/.cache/pager-idle-run.log`; stderr: `benchmark/.cache/pager-idle-run.err.log`.

This run measures paging separately from LSP. It uses the same fifty public tasks, task
order, dataset checksum, GPT-6.1 Sol model, medium reasoning, 25-call limit, 1,024 map-token
budget and official grader as the [frozen control](baselines/README.md).

Runtime revision: `40f82be433b5b5a8adefa4e720e37cccaa1bc636`.
Features: `enable_l2_paging=true`, `enable_lsp_graph=false`, `l1_ttl_turns=2`.
All three size caps (`max_l1_tokens`, `max_l1_files`, `max_l2_tokens`) are explicitly null.
The image overlays this pinned source on the original control image and verifies identical
installed Python dependencies. No Node or language servers are added. Source, worker,
preflight, Dockerfile, host runner and image fingerprints are saved in `run_manifest.json`.
The control and completed LSP-only result records remain unchanged.

## Gate and isolation

The offline image preflight makes zero inference requests. It keeps six files totaling
more than 16,000 word-count units in L1 for two responses, then verifies six TTL demotions
into L2 on the third response and byte-identical source files. These fixture units are
not model-token measurements or benchmark outcomes.

The first five frozen tasks are generated and officially graded before scaling. The gate
requires complete usage, matching idle-only settings, completed-response/request telemetry,
disjoint L1/L2 membership, no LSP activity, and actual L2 exposure in model requests.
Resolution score and a minimum demotion count are deliberately not gate criteria. Short
tasks can finish before the idle TTL; their lack of demotions is a result to measure.
The five smoke tasks are included exactly once in the final fifty-task comparison.

The worker retains the existing unprivileged, read-only, offline container isolation.
Only the host performs inference using the authorized ChatGPT subscription. The worker
receives no credentials, Docker socket, gold patch, expected tests or oracle labels.
Official grading remains a separate evaluator-only stage. Subscription failures stop
new requests and retain the actual error; there is no API-key or alternate-model fallback.

## Telemetry

Alongside baseline-compatible metrics, the worker records actual interface-block token
counts, pager settings, cumulative demotion/preload/promotion/reload counts, per-response
events, and L1/L2 membership for completed model requests. Full-file reloads are measured
at the agent's file-addition path. Counters describe repeated operations, not unique files.

L2 peak context includes interface instructions and filename blocks. L1 peak measurements
retain the control's full-source counting convention. Actual provider input/output tokens
remain the primary cost measure. Request exposure is recorded separately from summaries
created after a final response, so unused L2 content is not mistaken for model exposure.

## Reproduce and monitor

```powershell
.venv/Scripts/python.exe -m benchmark.build_pager_agent
.venv/Scripts/python.exe -m benchmark.run_pager_benchmark --prepare-only
.venv/Scripts/python.exe -u -m benchmark.run_pager_benchmark
```

The orchestrator handles the five-task gate, then overlaps sequential generation with
sequential grading for the remainder. Resume reuses completed work and verifies saved
request fingerprints. Do not launch a duplicate orchestrator, edit pinned host code, or
rebuild/retag its image while a run is active.

```powershell
Get-Content benchmark/results/pager-idle-50-v1/run_status.json
Get-Content benchmark/results/pager-idle-50-v1/generate_status.json
Get-Content benchmark/results/pager-idle-50-v1/progress.json
Get-Content benchmark/.cache/pager-idle-run.log -Tail 12
Get-Content benchmark/results/pager-idle-50-v1/grade.log -Tail 12
```

After fifty complete graded results, the orchestrator validates source records, patches
and official reports, writes `comparison_report.md` and `treatment_snapshot.json`, and
saves `benchmark/experiments/pager-idle-50.json`. It never overwrites the control or LSP
snapshot. Pass@1 is withheld while coverage is incomplete. Empty predictions are unresolved
without inventing a test report; infrastructure failures remain explicit.

This is a historical control comparison. Provider drift and inference variability remain
confounders, and the reused fifty tasks are not a fresh holdout. A measured compression
ratio alone does not establish fewer overall tokens after preloading and reload calls.

Validation before launch: 166 runner, gate, telemetry, comparison, paging and subscription
tests passed; Flake8 and Git whitespace checks passed. The offline image preflight passed,
and the public task order exactly matches the frozen control.
