"""Analyze complete paired results; missing or failed infrastructure is never a loss."""

import argparse
import math
from pathlib import Path
from statistics import mean, median

from benchmark.ab_support import ARMS, METRICS, SCHEMA_VERSION, read_json, validate_manifest


def load_pairs(manifest, results_dir):
    validate_manifest(manifest)
    expected = {task["instance_id"]: task for task in manifest["tasks"]}
    arms = {}
    for arm in ARMS:
        results = {}
        for path in sorted((Path(results_dir) / arm).glob("*.json")):
            row = read_json(path)
            instance_id = row.get("instance_id")
            if instance_id not in expected or instance_id in results:
                raise ValueError(f"Unknown or duplicate instance in {path}")
            if (row.get("schema_version") != SCHEMA_VERSION
                    or row.get("experiment_id") != manifest["experiment_id"]
                    or row.get("arm") != arm
                    or row.get("repo") != expected[instance_id]["repo"]):
                raise ValueError(f"Result provenance mismatch: {path}")
            if row.get("status") != "completed" or type(row.get("resolved")) is not bool:
                raise ValueError(f"Incomplete or ungraded result: {path}")
            grading = row.get("grading", {})
            if (grading.get("engine") != "swebench"
                    or grading.get("run_id") != f"{manifest['experiment_id']}-{arm}"):
                raise ValueError(f"Missing official grading provenance: {path}")
            for metric in METRICS:
                value = row.get(metric)
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value) or value < 0
                ):
                    raise ValueError(f"Invalid {metric}: {path}")
                if (value is not None and metric not in
                        ("total_cost_usd", "localization_recall_at_turn_3") and int(value) != value):
                    raise ValueError(f"Noninteger counter {metric}: {path}")
            recall = row.get("localization_recall_at_turn_3")
            if recall is not None and recall > 1:
                raise ValueError(f"Recall outside [0, 1]: {path}")
            cached, total = row.get("cached_input_tokens"), row.get("total_input_tokens")
            if cached is not None and total is not None and cached > total:
                raise ValueError(f"Cached tokens exceed input tokens: {path}")
            # Cache reads are a subset of total input tokens, not an additional counter.
            row["cache_hit_ratio"] = cached / total if cached is not None and total else None
            results[instance_id] = row
        missing = set(expected) - set(results)
        if missing:
            raise ValueError(f"Missing {len(missing)} {arm} results; examples: {sorted(missing)[:3]}")
        arms[arm] = results
    return [(arms[ARMS[0]][key], arms[ARMS[1]][key]) for key in sorted(expected)]


def quantile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def describe(values):
    return f"{mean(values):.4g} / {median(values):.4g} / {quantile(values, .75) - quantile(values, .25):.4g}"


def wilcoxon_p(control, treatment):
    differences = [b - a for a, b in zip(control, treatment)]
    if not any(differences):
        return "1"
    try:
        from scipy.stats import wilcoxon
    except ImportError:
        return "unavailable (install scipy)"
    return f"{wilcoxon(differences, alternative='two-sided').pvalue:.6g}"


def mcnemar_p(pairs):
    lost = sum(a["resolved"] and not b["resolved"] for a, b in pairs)
    gained = sum(not a["resolved"] and b["resolved"] for a, b in pairs)
    n = lost + gained
    pvalue = min(1.0, 2 * sum(math.comb(n, k) for k in range(min(lost, gained) + 1)) / 2**n)
    return pvalue, lost, gained


def metric_pairs(pairs, metric):
    available = [(a[metric], b[metric]) for a, b in pairs
                 if a.get(metric) is not None and b.get(metric) is not None]
    return [a for a, _ in available], [b for _, b in available]


def reduction(pairs, metric, aggregate):
    control, treatment = metric_pairs(pairs, metric)
    if len(control) != len(pairs) or not aggregate(control):
        return None
    return 1 - aggregate(treatment) / aggregate(control)


def report(manifest, pairs):
    lines = [
        "# Paired A/B comparison",
        "",
        f"Experiment: `{manifest['experiment_id']}`; model: `{manifest['model']}`.",
        f"Complete pairs: {len(pairs)}. Each arm summary is mean / median / IQR.",
        "Delta is mean(treatment - control); relative delta divides by the paired control mean.",
        "Missing telemetry is unavailable, never imputed as zero. Smaller is not always better.",
        "",
        "| Metric | Pairs | Control | Treatment | Paired delta | Relative delta |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for metric in ("resolved", *METRICS, "cache_hit_ratio"):
        control, treatment = metric_pairs(pairs, metric)
        if not control:
            lines.append(f"| {metric} | 0 | unavailable | unavailable | — | — |")
            continue
        delta = mean(b - a for a, b in zip(control, treatment))
        relative = f"{100 * delta / mean(control):+.2f}%" if mean(control) else "undefined"
        lines.append(f"| {metric} | {len(control)} | {describe(control)} | {describe(treatment)}"
                     f" | {delta:+.4g} | {relative} |")
    lines.extend(["", "## Paired tests", "", "Two-sided exploratory tests; no multiple-testing correction."])
    for metric in ("tokens_to_first_edit", "total_cost_usd", "total_turns"):
        control, treatment = metric_pairs(pairs, metric)
        pvalue = wilcoxon_p(control, treatment) if control else "unavailable"
        lines.append(f"- Wilcoxon {metric}: p={pvalue} (n={len(control)}).")
    pvalue, lost, gained = mcnemar_p(pairs)
    lines.append(f"- Exact McNemar: p={pvalue:.6g}; control-only={lost}, treatment-only={gained}.")
    first_edit = reduction(pairs, "tokens_to_first_edit", median)
    failed_edits = reduction(pairs, "failed_edit_count", mean)
    control, treatment = metric_pairs(pairs, "localization_recall_at_turn_3")
    recall_delta = mean(treatment) - mean(control) if len(control) == len(pairs) else None
    resolution_ok = sum(b["resolved"] for _, b in pairs) >= sum(a["resolved"] for a, _ in pairs)
    outcomes = [
        ("Median tokens to first edit reduced at least 40%", first_edit, .4),
        ("Mean failed edits reduced at least 25%", failed_edits, .25),
        ("Recall improved at least 15 percentage points with nondecreasing Pass@1", recall_delta, .15),
    ]
    lines.extend(["", "## Engineering hypotheses", "", "These thresholds describe this sample; they do not establish statistical significance."])
    for index, (label, value, threshold) in enumerate(outcomes):
        outcome = "NOT EVALUABLE" if value is None else (
            "PASS" if value >= threshold - 1e-12 and (index != 2 or resolution_ok) else "FAIL"
        )
        measured = "unavailable or zero baseline" if value is None else f"{value * 100:.2f}%"
        if index == 2 and value is not None:
            measured = f"{value * 100:+.2f} percentage points; Pass@1 nondecreasing={resolution_ok}"
        lines.append(f"- **{outcome}**: {label} ({measured}).")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=Path("benchmark/results"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = read_json(args.manifest or args.results_dir / "manifest.json")
        pairs = load_pairs(manifest, args.results_dir)
        output = args.output or args.results_dir / "ab_comparison_report.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report(manifest, pairs), encoding="utf-8")
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(f"Analyzed {len(pairs)} complete pairs: {output}")


if __name__ == "__main__":
    main()
