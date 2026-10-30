# Full fifty-task combined treatment

This exploratory run enables all implemented context features together:

| Feature | Effective setting |
| --- | --- |
| Semantic LSP ranking | Enabled, 10-second budget |
| Explicit assistant navigation | Enabled, at most three actions per response |
| Whole-file idle L1/L2 paging | Enabled, two completed idle responses |
| Dependency preloading | Disabled by the demand-paging correction |
| File/token size caps | All unset |

Source windows, traceback-weighted ranking and edit-diagnostic reflection have
not been implemented. They are not represented as enabled in this experiment.

The user explicitly requested the full fifty with the implemented features on,
despite the [demand-only smoke](experiments/pager-demand-smoke-5.json) not passing
its strict efficiency gate. That result and its gate remain unchanged. This is
a separate combined arm; it does not relabel that smoke as successful.

## Fixed comparison

The same fifty task IDs, task order, dataset revision, GPT-6.1 Sol subscription
model, medium reasoning, 25-completed-call budget, 1,024 map tokens and official
grader are retained from the frozen control. New source, worker, language-server
runtime and image fingerprints are recorded before inference.

The image overlays the current source on the already pinned Node/Pyright image.
Its installed Python dependencies must match control byte-for-byte. Offline
preflight verifies all flags, shared LSP ownership, native definitions/references,
explicit page/demote transitions and source integrity, without model requests.

The first five tasks are generated and graded once, then included in the fifty.
Their gate checks infrastructure, feature activation, actual semantic ranking,
navigation instructions and complete telemetry. It does **not** select for lower
tokens or a higher score: the full combined measurement was explicitly requested.
Navigation actions and L2 exposure are measured rather than forced.

## Run and monitor

```powershell
.venv/Scripts/python.exe -m benchmark.build_combined_agent
.venv/Scripts/python.exe -m benchmark.run_combined_benchmark --prepare-only
.venv/Scripts/python.exe -u -m benchmark.run_combined_benchmark
```

Output: `benchmark/results/combined-all-50-v1/`.
Host log: `benchmark/.cache/combined-all-50.log` and `.err.log`.

```powershell
Get-Content benchmark/results/combined-all-50-v1/run_status.json
Get-Content benchmark/results/combined-all-50-v1/generate_status.json
Get-Content benchmark/results/combined-all-50-v1/progress.json
Get-Content benchmark/results/combined-all-50-v1/grade.log -Tail 10
```

The worker now forwards the navigation flag and records attempted actions,
invalid protocol replies, shared bridge use and actual instruction exposure.
Auxiliary history-summary requests count against the same model-call budget;
they do not pretend to expose source/interface context or navigation instructions.
Both ordinary file reloads and explicit navigation loads are counted.

Completion validates all patches/reports and saves
`benchmark/experiments/combined-all-50.json`. Control, LSP-only, old paging,
demand-only and smoke records remain separate. Unknown usage is never zero-filled.
Do not edit pinned runner files while the run is active.

## What this sample can show

Most earlier trajectories finished in two calls. Fifty tasks give more observations,
but do not make each session longer. Idle paging may still have little chance to
replace source before a later request. Ranking and navigation could improve
file selection, retries or resolution; they can also add overhead. Measure their
actual use and total tokens instead of assuming that enabled features save tokens.

A longer multi-turn workload would be a separate follow-up for idle-context
savings. These reused tasks and the historical control are not an independent
holdout, and this run alone cannot establish performance on production sessions.
