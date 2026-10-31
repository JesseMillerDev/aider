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

## Completed result

Experiment `966dbc21cb9c109f1970` completed all fifty tasks. The frozen result is
[combined-all-50.json](experiments/combined-all-50.json); the independently checked
usage, paired outcomes and recovery audit are in
[combined-all-50-analysis.json](experiments/combined-all-50-analysis.json).

| Metric | Frozen baseline | All implemented features |
| --- | ---: | ---: |
| Resolved | 32/50 (64%) | 34/50 (68%) |
| Input tokens | 2,114,323 | 2,193,358 |
| Output tokens | 40,873 | 39,605 |
| Total tokens | 2,155,196 | 2,232,963 |
| Completed model calls | 108 | 111 |

The treatment solved four tasks the baseline missed and missed two the baseline
solved: a net gain of two solves, with **77,767 more tokens (+3.61%)**. This run did
not demonstrate token savings. One historical sample with all features combined
does not establish a reliable quality gain or identify which feature caused it.

Navigation was used on three tasks: two `nav_def` and five `page_l1` attempts,
across four responses, with no invalid navigation responses. There were no
dependency preloads or auxiliary model calls. Thirty-eight tasks finished in two
calls, ten in three, one in four and one in one.

Only `astropy__astropy-7746` demoted a file, after its third and final response.
There was no later request: **zero of fifty tasks sent any L2 context**. A summary
existing in the final local state saved no request tokens. The added navigation
instructions, different selected context and three additional calls can add
usage; this combined experiment does not isolate their individual contributions.
A longer workload with repeated requests after files become idle is needed to
measure paging savings under the chosen policy.

Verification checked all 111 saved request fingerprints and provider usage
records, all fifty prediction hashes, 49 official grading reports and one empty
prediction. Usage is complete for every task, with no provider-error records or
unresolved grading infrastructure errors. The frozen baseline and pinned host
sources remained unchanged.

Two workers (`matplotlib__matplotlib-24149` and `astropy__astropy-14182`) completed
their patches before Pyright teardown timed out. Offline recovery restored the
original read-only map/date checkpoints, replayed five saved replies with all
request fingerprints matching, and reproduced the exact original patches.
Recovery made **zero new model calls**. Their snapshot rows retain recovery
provenance; original total worker runtime was unavailable and is omitted rather
than replaced with replay timing. Subsequent generation used a host guard that
accepts a terminal report only when its patch hash and the subsequent cleanup
trace match; no additional task required that recovery. Finalization preserved
all provider transcripts byte for byte.
