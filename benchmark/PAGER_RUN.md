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

## Completed result

All fifty tasks finished grading: **31/50 resolved (62%)**, versus **32/50 (64%)**
for the control. There are 47 official reports, three empty predictions and zero
unresolved infrastructure errors. One task improved (`psf__requests-3362`); two
regressed (`django__django-12308`, `pytest-dev__pytest-5103`).

The [treatment snapshot](experiments/pager-idle-50.json) preserves all fifty result
records, effective settings, source/image fingerprints and patch/report hashes.
The [accepted-response usage analysis](experiments/pager-idle-50-analysis.json)
records **3,289,196 input + 40,907 output = 3,330,103 tokens**, across **109 completed
model calls**. This accepted-response subtotal is **54.52% higher** than the
control's 2,155,196 tokens. Reported cached input is zero.

This is not an all-attempt token total. A request for `django__django-11815`
returned `server_is_overloaded` with no usage, after nineteen tasks had generated.
Generation resumed after a cooldown, preserving completed tasks and the original
error. All fifty accepted-response usage records are known, but only 49 tasks
have complete usage for every attempt. The primary snapshot therefore keeps
aggregate input/output/combined usage null. The failed attempt's additional
usage remains unknown; it is not replaced with zero.

Paging recorded **530 preloads, four full-file reloads and one TTL demotion**;
46 tasks actually sent L2 context. The sole demotion occurred after the final
response of `astropy__astropy-7746`, with no subsequent model request. It could
not reduce that task's input. Dependency interfaces added context, while idle
demotion provided no measured input savings in these short trajectories. This
arm did not demonstrate a resolution or token-efficiency improvement.

After all grades completed, resuming the unchanged orchestrator saved the final
comparison without changing any transcript fingerprints. The frozen control
and LSP snapshots were verified byte-for-byte unchanged. Raw provider errors,
retry evidence, transcripts and grading reports remain in the ignored results
directory. The JSON analysis includes per-call usage and source hashes, without
copying prompts or response text.

These are historical comparisons. Provider drift and inference variability
remain possible confounders, and the run does not separate preloading from idle
demotion effects. No fresh holdout or navigation treatment has been evaluated.

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

This section describes the recorded preloading treatment and its runner revision
`892a1cba0`. The current runner uses the separate demand-paging policy, image and
output directory described in [the upgrade guide](DEMAND_PAGING.md). Use the
recorded runner revision when reproducing the historical arm; current defaults
do not overwrite or resume this experiment.

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
