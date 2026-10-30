"""Compare complete LSP-only or paging-only treatments with the immutable control."""

import argparse
import hashlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from benchmark.ab_support import digest, read_json
from benchmark.analyze_results import report
from benchmark.grade_smoke import read_resolution
from benchmark.run_baseline import ROOT, atomic_json
from benchmark.run_lsp_benchmark import ARM, BASELINE


def compare(output):
    output = Path(output)
    manifest = read_json(output / "run_manifest.json")
    paging = manifest.get("kind") == "paging_only_treatment"
    demand = paging and manifest.get("paging_policy") == "demand"
    if paging and manifest.get("paging_policy") not in (None, "demand"):
        raise ValueError("Unknown paging treatment policy")
    arm = manifest.get("arm", ARM)
    baseline = read_json(BASELINE)
    if hashlib.sha256(BASELINE.read_bytes()).hexdigest() != manifest["baseline_sha256"]:
        raise ValueError("Frozen baseline changed during the treatment")
    if digest(manifest["tasks"]) != baseline["configuration"]["tasks_sha256"]:
        raise ValueError("Treatment task identity differs from baseline")
    control = {row["instance_id"]: row for row in baseline["instances"]}
    pairs, records = [], []
    semantic, bridge, pager_totals = Counter(), Counter(), Counter()
    for task in manifest["tasks"]:
        key = task["instance_id"]
        path = output / arm / (key + ".json")
        row = read_json(path)
        if (
            row.get("status") != "completed"
            or type(row.get("resolved")) is not bool
            or row.get("experiment_id") != manifest["experiment_id"]
            or row.get("arm") != arm
            or row.get("repo") != task["repo"]
            or control[key]["base_commit"] != task["base_commit"]
        ):
            raise ValueError(f"Incomplete or mismatched treatment result: {key}")
        grading = row["grading"]
        patch = output / "agents" / key / "prediction.patch"
        if hashlib.sha256(patch.read_bytes()).hexdigest() != grading["patch_sha256"]:
            raise ValueError(f"Graded prediction changed: {key}")
        report_hash = None
        if grading.get("status") == "graded":
            report_path = output / grading["report"]
            if read_resolution(report_path, key) != row["resolved"]:
                raise ValueError(f"Official resolution differs: {key}")
            report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        elif (
            grading.get("status") != "empty_prediction"
            or patch.read_bytes().strip()
            or row["resolved"]
        ):
            raise ValueError(f"Missing official grading provenance: {key}")
        pairs.append((control[key], row))
        if paging:
            if not row.get("pager", {}).get("enabled") or row.get("lsp"):
                raise ValueError(f"Paging-only treatment was not active: {key}")
            if demand and row["pager"]["stats"].get("preloads", 0):
                raise ValueError(f"Demand paging unexpectedly preloaded dependencies: {key}")
            pager_totals.update(
                {
                    name: row["pager"]["stats"].get(name, 0)
                    for name in (
                        "demotions",
                        "preloads",
                        "promotions",
                        "full_file_reloads",
                        "budget_overflows",
                        "unhelpful_demotions",
                    )
                }
            )
        else:
            semantic.update(row["lsp"]["totals"])
            bridge.update(row["lsp"]["bridge"])
        records.append(
            {
                **row,
                "base_commit": task["base_commit"],
                "grading": {**grading, "report_sha256": report_hash},
                "source_record_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if len(pairs) != 50 or set(control) != {task["instance_id"] for task in manifest["tasks"]}:
        raise ValueError("Comparison requires exactly the original 50 complete pairs")

    usage_complete = all(row["usage_complete"] for row in records)

    def total(metric):
        return sum(row[metric] for row in records) if usage_complete else None

    inputs, outputs = total("total_input_tokens"), total("total_output_tokens")
    resolved = sum(row["resolved"] for row in records)
    combined = inputs + outputs if usage_complete else None
    summary = {
        "tasks": len(records),
        "resolved": resolved,
        "unresolved": 50 - resolved,
        "pass_at_1": resolved / 50,
        "infrastructure_errors": 0,
        "official_test_reports": sum(row["grading"]["status"] == "graded" for row in records),
        "empty_predictions": sum(row["grading"]["status"] == "empty_prediction" for row in records),
        "usage_complete_tasks": sum(row["usage_complete"] for row in records),
        "total_input_tokens": inputs,
        "total_output_tokens": outputs,
        "combined_tokens": combined,
        "cached_input_tokens": total("cached_input_tokens"),
        "total_turns": sum(row["total_turns"] for row in records),
        "total_cost_usd": None,
        **(
            {
                "pager_totals": dict(pager_totals),
                "tasks_with_demotions": sum(
                    row["pager"]["stats"].get("demotions", 0) > 0 for row in records
                ),
                "tasks_sent_l2_context": sum(
                    any(request["l2_files"] for request in row["pager"]["request_contexts"])
                    for row in records
                ),
            }
            if paging
            else {"lsp_totals": dict(semantic), "lsp_bridge": dict(bridge)}
        ),
    }
    paired_report = report(manifest, pairs).replace(
        "# Paired A/B comparison",
        (
            (
                "# Demand paging versus historical control"
                if demand
                else "# Idle-only paging versus historical control"
            )
            if paging
            else "# LSP-only treatment versus historical control"
        ),
        1,
    )
    additions = [
        "",
        "## Totals and interpretation",
        "",
        f"Control: {baseline['summary']['resolved']}/50 resolved; {baseline['summary']['combined_tokens']:,} combined tokens.",
        f"Treatment: {resolved}/50 resolved; {combined if combined is not None else 'unavailable'} combined tokens.",
        f"LSP: {semantic['resolved_sites']} resolved sites, {bridge['queries']} RPC queries, "
        f"{semantic['boosted_edges']} boosted and {semantic['dampened_edges']} dampened edge evaluations.",
        f"Unknown queried sites: {semantic['unknown_sites']}; time/query budget exhaustion events: {semantic['budget_exhausted']}.",
        "Edge/site counters include repeated maps and are not unique source locations.",
        "Runtime is recorded for treatment tasks; the frozen baseline lacks comparable total-runtime telemetry.",
        "The first five tasks are included once in the 50-task result; their smoke gate tests infrastructure, not score.",
        "This is a historical comparison, not randomized/interleaved. Model drift, LSP timing and inference variability may affect differences.",
        "Exploratory p-values do not remove those confounders. No fresh holdout tasks have been evaluated.",
    ]
    if paging:
        additions[5:8] = [
            f"Paging: {pager_totals['demotions']} demotions, {pager_totals['preloads']} preloads, "
            f"{pager_totals['full_file_reloads']} full-file reloads; {summary['tasks_with_demotions']}/50 tasks had a demotion.",
            f"L2 interfaces were sent on {summary['tasks_sent_l2_context']}/50 tasks. LSP was disabled; all size caps were unset.",
            "Pager counters include repeated operations; short tasks can finish before the idle TTL expires.",
        ]
    if demand:
        additions.append(
            "Demand paging disabled dependency preloading. Its five-task gate required strictly "
            "fewer measured input+output tokens and no lower resolved-task count than control. "
            "The five tasks are reused, so this selection is not independent evidence."
        )
        additions = [
            line.replace(
                "their smoke gate tests infrastructure, not score.",
                "their efficiency gate is described below.",
            )
            for line in additions
        ]
    if combined is not None:
        additions.insert(
            5,
            f"Combined-token change: {100 * (combined / baseline['summary']['combined_tokens'] - 1):+.2f}%.",
        )
    (output / "comparison_report.md").write_text(
        paired_report + "\n".join(additions) + "\n", encoding="utf-8"
    )
    snapshot = {
        "schema_version": 1,
        "record_type": "milestone_treatment",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "experiment_id": manifest["experiment_id"],
        "model": manifest["model"],
        "billing_mode": "chatgpt_subscription",
        "configuration": manifest,
        "summary": summary,
        "instances": records,
        "comparison": {
            "control_experiment_id": baseline["experiment_id"],
            "baseline_sha256": manifest["baseline_sha256"],
            "kind": "historical_control",
        },
    }
    setup_path = output / "setup_usage.json"
    if setup_path.exists():
        snapshot["excluded_setup_usage"] = read_json(setup_path)
        with (output / "comparison_report.md").open("a", encoding="utf-8") as setup_report:
            setup_report.write(
                "\nSetup overhead is excluded from the paired task totals: the abandoned first "
                "attempt has unknown token usage; a separate diagnostic call used 10,340 tokens. "
                "See setup_usage.json. Overall setup-plus-run usage therefore has an unknown component.\n"
            )
    # Public task text is already retained by the frozen dataset identity; omit it here.
    snapshot["configuration"] = {key: value for key, value in manifest.items() if key != "tasks"}
    atomic_json(output / "treatment_snapshot.json", snapshot)
    target = (
        ROOT
        / "benchmark/experiments"
        / (
            "pager-demand-50.json"
            if demand
            else "pager-idle-50.json" if paging else "lsp-graph-50.json"
        )
    )
    if target.exists() and read_json(target)["experiment_id"] != manifest["experiment_id"]:
        raise ValueError("Refusing to replace a different recorded treatment")
    atomic_json(target, snapshot)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("benchmark/results/lsp-graph-50-v2"))
    print(compare(parser.parse_args(argv).output))


if __name__ == "__main__":
    main()
