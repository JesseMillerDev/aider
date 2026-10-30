# Milestone measurements

Keep the [frozen control](../baselines/README.md) unchanged and record each treatment
separately. These comparisons use the same 50 task IDs and common model/budget settings;
they are historical controls, not interleaved randomized experiments.

| Run | Resolved | Combined tokens | Change versus control |
|---|---:|---:|---:|
| Control | 32/50 (64%) | 2,155,196 | — |
| [LSP-only](lsp-graph-50.json) | 32/50 (64%) | 2,205,170 | +2.32% |

The LSP-only run has 50 complete usage records, 48 official reports, two empty predictions,
and zero infrastructure errors. Two tasks improved and two regressed. The record retains
effective settings, source/image fingerprints, per-task metrics, semantic telemetry and
source-record/report hashes without copying prompts or response text.

Most extra input came from two additional lint/edit correction rounds, partly offset by
a saved round and smaller prompts elsewhere. LSP ranking alone demonstrated no token
saving. L1/L2 context paging and compression have not been implemented or evaluated yet.
See [LSP_RUN.md](../LSP_RUN.md#completed-result) for the breakdown and limitations.

Paired task totals exclude setup overhead: one abandoned request has unknown usage;
a diagnostic call used 10,340 tokens. The JSON preserves this distinction. Subscription
dollar cost and comparable baseline runtime remain unavailable.
