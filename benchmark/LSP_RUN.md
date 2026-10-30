# Milestone 1: LSP-only fifty-task treatment

This run compares revision `3432f71f0f398013c00d87ac6d0c7359891b57f9` with the frozen
[historical control](baselines/README.md). Paging, navigation, trace weighting and diagnostic
reflection remain disabled. The first five tasks are a smoke gate and are included once in
the fifty-task result; their resolution score is not used to decide whether to continue.

Experiment: `bacd0701013355dd2b09`.
Artifacts: `benchmark/results/lsp-graph-50-v2/` (ignored by Git).
Host log: `benchmark/.cache/lsp-run-v2.log`; stderr: `benchmark/.cache/lsp-run-v2.err.log`.

## Frozen configuration

- Same 50 public tasks, dataset revision/checksum, GPT-6.1 Sol model, medium reasoning,
  25-call limit, 1,024 map-token budget, lint settings and official grader as the control.
- The treatment image overlays the pinned Aider revision on the original control image,
  retaining its installed Python dependencies. A digest-pinned Node image supplies Node
  22.20.0; Pyright is pinned to 1.1.414 and works without runtime network access.
- Treatment flags: `enable_lsp_graph=true`, `lsp_timeout=2.0`. Runtime source, worker,
  Dockerfile, host runner files, image IDs and language-server versions are fingerprinted
  in `run_manifest.json`. Resume refuses changed configurations and inference requests.
- Existing isolation is preserved: the agent has no network, OAuth credentials, Docker
  socket, gold patch, expected tests or oracle localization labels. Host inference and
  evaluator-only grading remain separate.

## Gate and measurements

The no-inference preflight builds a real requests task's map inside the final image. It
resolved 11 sites and changed 58 edge evaluations in approximately two seconds. The missing
TypeScript server for ancillary JavaScript files is recorded as an optional-language fallback;
Python server failures stop the smoke gate.

The automated five-task gate requires complete generation, official grading (or an explicitly
empty prediction), complete provider usage, active semantic telemetry, usable Python servers,
resolved sites and actual changed graph edges. Missing optional-language servers and bounded
query timeouts remain visible in the record. A gate failure stops before the remaining tasks.

The worker records every semantic preparation's counters and duration, aggregate boosted,
dampened and unchanged edge evaluations, started/unavailable language servers, bridge RPC
queries and timeout/fallback counters. These are repeated map/site evaluations, not unique
source locations. The budget-exhaustion counter counts events rather than distinct maps.
Task elapsed time is measured inside the agent; the baseline has no comparable elapsed-time
metric, so a treatment-only runtime cannot establish a speed improvement.

## Build, launch and monitor

```powershell
.venv/Scripts/python.exe -m benchmark.build_lsp_agent
.venv/Scripts/python.exe -m benchmark.run_lsp_benchmark --preflight
.venv/Scripts/python.exe -u -m benchmark.run_lsp_benchmark
```

The last command runs five generation tasks, grades them, checks `smoke_gate.json`, then
overlaps sequential generation with sequential grading for the remaining tasks. It resumes
completed work rather than sending those requests again. Do not launch a second orchestrator,
edit pinned host code, or rebuild/retag the treatment image while a run is active.

```powershell
Get-Content benchmark/results/lsp-graph-50-v2/run_status.json
Get-Content benchmark/results/lsp-graph-50-v2/generate_status.json
Get-Content benchmark/results/lsp-graph-50-v2/progress.json
Get-Content benchmark/.cache/lsp-run-v2.log -Tail 12
Get-Content benchmark/results/lsp-graph-50-v2/grade.log -Tail 12
```

When all 50 tasks are graded, the orchestrator writes `comparison_report.md` and a sanitized
`treatment_snapshot.json`, and records the snapshot under `benchmark/experiments/lsp-graph-50.json`.
The comparison joins task identities, checks prediction/report provenance, reports token totals
and paired metric differences, and retains unavailable measurements as null. The original
baseline record is never rewritten. Pass@1 is withheld while coverage is incomplete.

This is a historical comparison, not a randomized/interleaved experiment. Provider/model
drift and timing-dependent LSP evidence can affect results. Exploratory statistical tests do
not remove these limitations; fresh holdout validation remains a later step.

## Verification before inference

The combined runner, comparison, benchmark, subscription and LSP group passed 143 tests;
Flake8 and Git whitespace checks passed. The final container preflight made zero inference
requests and verified actual Pyright activity.

## Transport correction and setup overhead

The first attempt (`e26d3eeab5cca181b865`, `lsp-graph-50-v1`) stopped on its first request
before any task completed. The service emitted completed text and message seals but an
empty terminal output snapshot. The decoder treated that valid compact snapshot as a
text mismatch. One diagnostic call reproduced the issue and its captured event trace
reproduced it offline. The fix verifies per-item/content deltas against both
`response.output_text.done` and completed `response.output_item.done` messages before
accepting an empty snapshot; conflicting, unsealed or unfinished streams still fail.
See the [official streaming guide](https://developers.openai.com/api/docs/guides/streaming-responses).

The host transport source is fingerprinted in the new experiment. No model, request-body,
reasoning or task configuration changed. The abandoned attempt's usage is unknown. The
diagnostic call used 10,252 input and 88 output tokens (10,340 total); both calls are setup
overhead, excluded from the clean fifty-task paired totals and recorded in `setup_usage.json`.
The overall setup-plus-run token use therefore has an unknown component. Failed-attempt
artifacts remain in their original directory; no completed results were discarded.
