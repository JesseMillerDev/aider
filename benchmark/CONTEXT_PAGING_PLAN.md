# Context paging fork: evaluation and implementation plan

Upstream: `Aider-AI/aider`, commit `5dc9490bb35f9729ef2c95d00a19ccd30c26339c`.
Fork: <https://github.com/JesseMillerDev/aider>.

## Assessment of prompt.md

The proposed experiment is worth building. Its strongest idea is measuring localization and
time to first edit alongside resolution and cost. Implementing all features before establishing
measurement would make improvements difficult to attribute. This first increment therefore
freezes the experiment contract before changing Aider's behavior.

Several requirements need adjustment:

1. The prompt says four milestones but specifies five. Tests belong in each implementation
   increment, with a final integration milestone.
2. Selecting tasks using gold patch complexity changes the population being measured. The
   default subset is repository-balanced without inspecting patches. A separately declared
   challenge subset can be added later; it must not be presented as a random Lite sample.
3. Agent inputs must exclude `patch`, `test_patch`, expected tests, and oracle file labels.
   The selector uses an allowlist. Localization labels are written to a separate evaluator-only
   location. Repository source remains at the instance's `base_commit`.
4. Stock Aider has no `--no-enable-lsp-graph` or `--no-enable-l2-paging` flags. The control
   must execute the pinned upstream revision with its existing flags. Later, compare the fork
   with features disabled as an additional compatibility check.
5. An LSP timeout, empty response, unsupported method, and verified other target are distinct
   outcomes. Only a positively resolved conflicting target warrants a collision penalty.
6. A two-second timeout per symbol can multiply into minutes. Bound the entire cold start and
   each map's semantic enrichment phase. Make partial results usable and measure fallback rate.
7. A cache keyed only by filename, mtime, symbol and line becomes stale when dependencies or
   server configuration change. Include content/server generation and workspace invalidation.
8. Windowed L1 source cannot be passed to existing whole-file edit paths without adaptation.
   Start with whole files. Add windows only after proving edits preserve source outside them.
9. No known lint error does not establish passing tests. Track unknown/clean/failed states;
   do not demote files involved in unresolved diagnostics, test failures, or pending edits.
10. Traceback weighting is a localization heuristic, not spectrum-based fault localization
    without coverage spectra. Fixed graph weights do not guarantee a fivefold PageRank change.
    Test graph behavior on controlled fixtures instead of imposing an arbitrary global ratio.
11. Stub size reduction depends on the source. Measure preservation and reduction on representative
    fixtures; report the real ratio on `repomap.py` rather than making 65% a correctness rule.
12. A bundled treatment can measure overall impact but cannot attribute it to individual features.
    Follow it with graph-only, pager-only, navigation-only, and combined ablations.
13. Use the official SWE-bench grader. A successful process exit is not evidence that a patch
    resolved an issue. Missing reports, container failures, and timeouts need explicit statuses.
14. Exact model IDs, package versions, dataset revision, source fingerprint, prompt settings,
    and grading run IDs belong in the experiment record. Temperature zero is not a guarantee
    of identical provider responses. Threshold checks are descriptive, not significance tests.

## Implemented in this increment

- `select_subset.py`: immutable Hugging Face dataset revision, source checksum, seeded balanced
  selection independent of input order, public task allowlist, and separate optional oracle labels.
- `eval_harness_ab.py`: deterministic paired manifest, model/budget parity, distinct worktree paths
  and grading IDs, source fingerprint including dirty and untracked code, overwrite protection.
  **This is a planner, not an execution loop.** Treatment features are marked unimplemented.
- `analyze_results.py`: strict complete-pair/provenance checks, unavailable telemetry preserved as
  unavailable, mean/median/IQR and paired differences, two-sided Wilcoxon, exact McNemar, and the
  three engineering threshold checks. Statistical tests are exploratory without multiplicity correction.
- `grade_smoke.py` and `grader/`: evaluator-only gold-patch smoke runner using a Linux image,
  digest-pinned Python base and locked SWE-bench 4.1.0 dependencies. Retains actual reports/logs,
  checks report contents rather than process exit alone, and uses fresh grading IDs.
- `tests/basic/test_benchmark_ab.py`: selection, leakage, pairing, provenance, telemetry validation,
  missing data, zero baselines, and statistical edge cases. Tests use synthetic results in temporary
  directories, never persisted as benchmark outcomes.

## Local reproduction

From the repository root in PowerShell:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e . -r requirements/requirements-benchmark.txt
.venv/Scripts/python.exe -m pytest tests/basic/test_benchmark_ab.py -q

.venv/Scripts/python.exe -m benchmark.select_subset --limit 5 --output benchmark/subset_smoke.json --labels-output benchmark/oracle/smoke_labels.json
.venv/Scripts/python.exe -m benchmark.eval_harness_ab --instances benchmark/subset_smoke.json --model PROVIDER/EXACT_MODEL_ID --output benchmark/results/manifest.json
```

Replace the model placeholder with the intended provider model before freezing a real experiment.
The planner makes no model requests. Use a new output directory for a different experiment.
The pinned dataset revision is `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`, test split.
Without `--input`, the selector downloads its Parquet snapshot into the ignored cache directory.
With `--input`, selection works offline on a JSON array, JSONL, or Parquet file.

Generate the larger balanced subset with `--limit 50`; the default seed is 42.
Generated datasets, plans, results, environment snapshots, and oracle labels are ignored by Git.
Do not put the oracle directory in an agent worktree or mount it in its container.

## Result contract

The future execution adapter writes `results/<arm>/<instance_id>.json`. Required identity fields:

```json
{
  "schema_version": 1,
  "experiment_id": "the manifest experiment_id",
  "instance_id": "the selected instance_id",
  "repo": "the selected repo",
  "arm": "control_stock_aider",
  "status": "completed",
  "resolved": false,
  "grading": {
    "engine": "swebench",
    "run_id": "<experiment_id>-control_stock_aider"
  }
}
```

This is a schema example, not a benchmark result. `resolved` comes from the instance's official
report. The analyzer validates identity and schema; it cannot authenticate a caller's claimed
grader. The execution adapter must retain the prediction, patch hash, raw grader report, and logs.
It must not synthesize a failed resolution when grading cannot run.

Telemetry keys are listed in `ab_support.METRICS`. Missing measurements are `null` or absent.
Only a real measurement can be zero. Cache reads count inside `total_input_tokens`;
`cache_hit_ratio` is derived as cached / total input. Recall is a fraction in [0, 1].
First-edit metrics include the response containing the first edit attempt. Tasks with no edit
have `null` first-edit metrics and need a separate no-edit/censoring analysis in the runner milestone.
The analyzer does not evaluate the first-edit hypothesis unless every pair has that measurement.
Unknown metrics do not silently pass engineering thresholds.

```powershell
.venv/Scripts/python.exe -m benchmark.analyze_results --results-dir benchmark/results
```

This command deliberately fails until every planned task has a completed, graded result in both
arms. It must not produce an apparently successful comparison from an empty or partial directory.

## Official grading smoke test on Windows

Docker Desktop must be running Linux containers. After downloading the dataset with the selector:

```powershell
.venv/Scripts/python.exe -m benchmark.grade_smoke
```

This builds `better-aider-swebench-grader:4.1.0` and grades the gold patch for
`pytest-dev__pytest-7220`. It makes no model calls. The first run downloads the runner and task
images. Use `--skip-build` to reuse the runner image. A fresh ignored evaluator directory contains
the one-task oracle dataset, image ID, source checksum, raw official report, logs, and summary.
Exit status zero requires an official report with `resolved: true`.

SWE-bench 5.0.2 expected an `image` dataset column absent from this original Lite snapshot.
SWE-bench 4.1.0 understood the legacy schema but imported Unix `resource`, so it could not run
natively on Windows. The dedicated Linux runner resolves both issues without altering dataset
content. It mounts the Docker engine socket to create official task containers. Keep this evaluator
runner separate from any model/agent container, which must not receive that socket or oracle data.

## Next increment: execution and telemetry

1. Add an isolated runner and an Aider adapter with provider calls mocked for integration tests.
   Use a fresh task checkout per arm at `base_commit`, explicit environment configuration,
   disabled analytics and cache-warming pings, and no gold metadata visible to the agent.
2. Instrument actual model round trips, attempted/applied edits, token accounting, lint/test failures,
   and context membership. Hard-stop turn counts before the next request. Reserve output tokens
   against the remaining cost budget and record any unavoidable provider accounting overshoot.
3. Export official prediction JSONL (`instance_id`, `model_name_or_path`, `model_patch`), grade
   through the official harness, import actual reports, and preserve infrastructure failures.
   Include prediction content in a fresh grading attempt ID before retrying with a changed patch;
   never reuse a cached report for a different prediction. Current plan IDs distinguish experiments
   and arms, but do not yet identify patch attempts because no patches are produced.
4. First run a gold-patch infrastructure smoke test, then one control task, then a five-task pair.
   Log model price source and all effective settings. Scale to 50 only after the smoke results
   demonstrate grading, budget enforcement, and telemetry completeness.

Official references:

- <https://www.swebench.com/SWE-bench/guides/evaluation/>
- <https://www.swebench.com/SWE-bench/reference/harness/>
- <https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite>

## Initial local verification

The unmodified upstream basic suite on Windows/Python 3.12.14 completed with 468 passed,
4 failed, 5 skipped, and 67 subtests passed. Failures:

- `test_editor.py::test_pipe_editor_with_fake_editor`: editor argument capture was empty.
- `test_io.py::TestInputOutput::test_color_initialization`: expected color was unset.
- `test_run_cmd.py::test_run_cmd_echo`: Windows shell output differed from the expected string.
- `test_utils.py::test_safe_abs_path_symlink_loop`: Windows error 1314, symlink privilege unavailable.

No Aider runtime source was modified in this increment. Preserve these baseline failures when
assessing later changes; they are not evidence that the benchmark foundation regressed the runtime.
Docker Engine 28.4.0 reported Linux/x86_64 and a disposable `hello-world` container succeeded.

The new benchmark contract suite passed 35 tests. An earlier combined run of the unchanged
RepoMap suite and the then-current 27 benchmark tests passed 73 tests. Flake8 and Git whitespace
checks passed. The official gold-patch smoke graded one instance with **1 resolved, 0 unresolved,
0 errors, 0 unstopped containers, 0 unremoved images**. Its raw report is at
`benchmark/oracle/gold-smoke/logs/run_evaluation/gold-smoke-linux-20261029-1/gold/pytest-dev__pytest-7220/report.json`.
The built grader image ID was
`sha256:5695af350998c795fa05573456f7a4e00cbe13e65e98e4f77b570d0ed5835d77`.
This is an infrastructure check using a known gold patch, **not a model score or A/B result**.

## Milestone 1 diff plan: opt-in semantic graph

The inspected upstream graph stores `references[tag.name]` as filenames, then aggregates them with
`Counter` before building a `networkx.MultiDiGraph`. LSP checks require individual source positions,
which are lost at that aggregation step. `format_messages()` delegates assembly to
`format_chat_chunks()`; ordering work belongs in that assembly path. Slash commands are dispatched
by `Commands.run()` and are not an existing autonomous model tool protocol. The linter already has
language-specific fallbacks that should remain operational.

1. **`aider/lsp_bridge.py`**: lazy server lifecycle, framed JSON-RPC, request correlation,
   notification handling, bounded requests, initialize/shutdown, versioned didOpen/didChange,
   URI normalization, UTF-16 position conversion, structured known/unknown resolution outcomes,
   and diagnostic generations. Use a background worker behind the synchronous RepoMap boundary.
   Do not block the CLI event loop on an unbounded server read.
2. **`aider/args.py` / configuration wiring**: default-off `--enable-lsp-graph` and positive
   `--lsp-timeout`. Pass one workspace bridge into `Coder` and `RepoMap`; avoid starting servers
   or changing prompt bytes when disabled. Shut down subprocesses at the owner lifecycle boundary.
3. **`aider/repomap.py`**: preserve per-reference Tree-sitter positions in a parallel site index
   without breaking existing tag consumers/cache serialization. Aggregate verified site contributions
   with 2.5 weighting, conflicting known-target contributions with 0.05, and unknown contributions
   with stock weight. Preserve upstream name heuristics and PageRank behavior otherwise.
4. **Caching**: scope entries by workspace/server generation and source contents; invalidate on
   relevant workspace changes. Limit semantic query count/time per map. Fall back on unsupported,
   missing, still-indexing, crashed, malformed, or timed-out servers with observable counters.
5. **Tests**: fake stdio server for fragmented headers/bodies, asynchronous notifications,
   cancellation, crash, timeout and Unicode positions; graph collision fixtures for all three
   resolution states; default-off map/CLI compatibility; bounded startup and process cleanup.

Then implement whole-file paging behind a separate default-off flag, navigation and trace weighting,
and diagnostic reflection. Measure each increment against the frozen control before combining them.
