# Demand paging and the efficiency gate

The previous [paging experiment](PAGER_RUN.md#completed-result) added dependency
interfaces without removing source before later requests. It used 54.52% more
accepted-response tokens and resolved one fewer task. Those records are frozen.

This upgrade removes automatic dependency preloading. Imports never add files to
L2. Files enter L1 through normal Aider file loading or explicit navigation; L2
contains eligible files that were deliberately or automatically demoted from L1.
The pager does not scan the repository to populate interfaces.

```powershell
.venv/Scripts/python.exe -m aider --model chatgpt/gpt-6.1-sol --enable-agent-nav --enable-l2-paging
```

Both flags remain off by default. Navigation alone uses explicit transitions;
adding paging enables idle demotion after two completed idle responses. All
three optional file/token caps remain unset unless explicitly configured.

## Replacement checks

A demotion must lower the locally estimated complete source/interface message
cost, including instructions, acknowledgement, filenames, fences and message
framing. Otherwise the full file stays in L1. An unknown cost cannot establish a
saving. Protected edits and failed validation remain full as before. Summaries
are never truncated to force a saving or fit a cap.

This applies to manual `/demote_l2` too. A short file or an interface with long
docstrings can be larger than its source; keeping it full is the useful outcome.
The `unhelpful_demotions` counter records rejected cost-increasing transitions.

The runtime check covers source/interface messages. Other prompt content and
model decisions still affect total task usage, so this does not promise a
lower-cost solution for every task.

## Whole-prompt verification without inference

`benchmark.pager_preflight` exercises the real Coder message assembly. It proves
that an imported file with a large interface does not alter the active prompt,
then verifies lower complete-message tokens after an eligible file becomes idle.
Source files must remain byte-identical. Completion methods are disabled.

The fixture uses the subscription model's local tokenizer, not provider usage.
The separate six-file TTL fixture uses whitespace word-count units solely to
exercise the uncapped lifecycle; those counters are not model tokens.
The final Docker image runs
the same check with no network access, a read-only filesystem and the unchanged
control Python dependencies.

```powershell
.venv/Scripts/python.exe -m benchmark.pager_preflight
```

## Five tasks before fifty

The updated runner isolates this pager-policy change: navigation and LSP ranking
are disabled in its benchmark arm. Task IDs, order, model, reasoning effort,
25-call limit, map budget and grader match the frozen control. Normal Aider file
loading remains available. Navigation-only and combined arms are separate work.

```powershell
.venv/Scripts/python.exe -m benchmark.build_pager_agent
.venv/Scripts/python.exe -m benchmark.run_pager_benchmark --prepare-only
.venv/Scripts/python.exe -m benchmark.run_pager_benchmark --smoke-only
```

The builder defaults to committed `HEAD`, derives a unique image tag from that
revision, and writes `benchmark/.cache/pager-demand-build.json`. Commit source
changes before building. The new output is `benchmark/results/pager-demand-50-v1/`.
It does not reuse or overwrite the old paging image, build record or results.

The first five tasks must be officially graded, with complete measured usage,
valid context telemetry and zero dependency preloads. Then their **aggregate
input+output tokens must be strictly lower** than those same five control tasks,
and their resolved-task count must not be lower. There is no arbitrary target
percentage. Equal usage, higher usage, unknown usage or a lower resolution count
stops scaling. The decision and measurements are saved in `smoke_gate.json`.
Actual L2 exposure is optional: short tasks may finish before anything is idle.

`--smoke-only` stops after this decision. Running without it may scale to the
remaining 45 tasks only when the gate passes. A repeated invocation rechecks the
gate before scaling. No full fifty-task run is launched by installing this upgrade.

Completed demand-paging results go to `benchmark/experiments/pager-demand-50.json`.
The frozen control, LSP result and old paging result remain separate. Because
these are reused tasks and a historical control, passing five tasks is a cost
screen, not independent evidence of generalization or guaranteed full-run savings.

## Five-task smoke result

The [live five-task smoke](experiments/pager-demand-smoke-5.json), experiment
`400d3df9df553936552a`, resolved **4/5**, matching the same control tasks. It used
**349,669 input + 4,427 output = 354,096 tokens**, versus control's **353,800**:
**296 more tokens (+0.084%)**. Usage is complete for all five tasks, all have
official grading reports, and there are zero infrastructure errors. The strict
efficiency gate is **blocked**; the remaining 45 tasks were not launched.

Both arms made 11 completed calls. There were zero dependency preloads and no
model requests containing L2 summaries. The only demotion occurred after the
final response, so no later request benefited. Removing the bulk preloader
removed its added context, but this sample did not demonstrate token savings.
This is a five-task historical comparison, not a full-run score or holdout.

The initial checker incorrectly assumed baseline result rows were in execution
order; those rows are sorted by task ID. The corrected checker validates the
ordered task list against the frozen selection hash and joins control outcomes
by ID. A gate-only recheck preserved the original manifest, transcripts, patches
and reports, made no inference requests, and saved `gate_recheck.json` alongside
the raw run. That audit records both checker fingerprints. The saved manifest's
original host-code fingerprints were not rewritten. A changed treatment should
use a new output directory rather than modifying this frozen run.
The checker/comparison regression group passed 37 tests, including reordered
baseline rows, rejection of changed frozen task order, and blocked-run behavior.

## Recorded offline verification

The [offline verification record](experiments/demand-paging-preflight.json) pins
source revision `25509b48719d81585aefeffa7123239e8e5c9a5e` and its Docker image.
In that Linux image the complete fixture prompt fell from **5,553 to 1,584 local
tokens** after idle replacement, saving 3,969 tokens. Active prompts were
unchanged and dependency preloads stayed at zero. Python dependencies matched
the frozen control, and the new experiment configuration passed prepare-only
validation. No model requests or SWE-bench tasks were executed.

The focused runtime/harness suite passed **119 tests with one Windows symlink
skip**, including real Pyright. The full basic suite passed **718 tests and 67
subtests**, with six skips and the four previously recorded Windows
editor/color/shell/symlink failures. Static analysis and whitespace checks passed.
