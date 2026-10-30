# Milestone measurements

The [idle-only paging treatment](../PAGER_RUN.md#completed-result) has completed with
LSP disabled and no size caps. It resolved 31/50 tasks and did not save tokens.

The [demand-paging correction](../DEMAND_PAGING.md) removes bulk preloading and
adds a measured five-task efficiency gate. Its offline checks are separate from
these task outcomes; no demand-paging model experiment has been run yet.
The [offline verification](demand-paging-preflight.json) records the tested source,
Docker image, whole-prompt estimates and regression checks with zero inference.

Keep the [frozen control](../baselines/README.md) unchanged and record each treatment
separately. These comparisons use the same 50 task IDs and common model/budget settings;
they are historical controls, not interleaved randomized experiments.

| Run | Resolved | Recorded combined tokens | Change versus control |
|---|---:|---:|---:|
| Control | 32/50 (64%) | 2,155,196 | — |
| [LSP-only](lsp-graph-50.json) | 32/50 (64%) | 2,205,170 | +2.32% |
| [Idle-only paging](pager-idle-50.json) | 31/50 (62%) | 3,330,103 accepted-response subtotal; all-attempt total unknown | +54.52% subtotal |

Paging has 47 official reports, three empty predictions and zero unresolved
infrastructure errors. One task improved and two regressed. Its 109 completed
responses used 3,289,196 input and 40,907 output tokens. One failed overload
attempt returned no usage, so the primary snapshot retains null aggregate token
totals. The separate [usage analysis](pager-idle-50-analysis.json) preserves the
known accepted-response subtotal, unknown attempt and per-call source hashes.
Neither an unknown attempt nor subscription dollar cost is treated as zero.

The pager performed 530 preloads and four full-file reloads. Its only TTL
demotion happened after that task's last response; no later request benefited.
L2 context was sent on 46/50 tasks. This arm therefore demonstrated no input
saving from idle demotion. See [the completed paging result](../PAGER_RUN.md#completed-result).

The LSP-only run has 50 complete usage records, 48 official reports, two empty predictions,
and zero infrastructure errors. Two tasks improved and two regressed. The record retains
effective settings, source/image fingerprints, per-task metrics, semantic telemetry and
source-record/report hashes without copying prompts or response text.

Most extra input came from two additional lint/edit correction rounds, partly offset by
a saved round and smaller prompts elsewhere. LSP ranking alone demonstrated no token
saving. The paging measurement above evaluates the separate default-off implementation.
See [LSP_RUN.md](../LSP_RUN.md#completed-result) for the breakdown and limitations.

The LSP run's paired task totals exclude setup overhead: one abandoned request has unknown usage;
a diagnostic call used 10,340 tokens. The JSON preserves this distinction. Subscription
dollar cost and comparable baseline runtime remain unavailable.
