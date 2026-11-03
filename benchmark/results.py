"""Combine saved runs into per-model metrics, comparisons and release reports."""

import statistics
from pathlib import Path

from .common import read_json, read_jsonl, write_json
from .runner import latest_records
from .stats import ITERATIONS, METRICS, OVERALL, OVERALL_NO_WRITING, compare, summarize_model
from .tasks import CATEGORY_LABELS

METRIC_LABELS = {**CATEGORY_LABELS, OVERALL: "Overall index", OVERALL_NO_WRITING: "Overall, excluding writing"}


class Results:
    """Full (non-sentinel) runs of one suite version. A model's newest run wins."""

    def __init__(self, results_dir, suite, iterations=ITERATIONS):
        self.suite = suite
        self.iterations = iterations
        self.categories = {t.id: t.category for t in suite.tasks}
        self.models = {}
        self.records = {}
        self.scores = {}
        runs = []
        for path in Path(results_dir).glob("*/manifest.json"):
            manifest = read_json(path)
            if manifest["sentinel_only"]:
                continue
            if manifest["suite_public_hash"] != suite.public_hash or manifest["suite_private_hash"] != suite.private_hash:
                continue
            runs.append((manifest["created_at"], path.parent, manifest))
        for _, run_dir, manifest in sorted(runs, key=lambda r: r[0]):
            records = latest_records(run_dir)
            scores = read_jsonl(run_dir / "scores.jsonl") + read_jsonl(run_dir / "human_scores.jsonl")
            for model in manifest["models"]:
                model_id = model["id"]
                model_records = [r for r in records if r["model"] == model_id]
                if model_records:
                    self.models[model_id] = model
                    self.records[model_id] = model_records
                    self.scores[model_id] = [s for s in scores if s["model"] == model_id]

    def task_means(self, model_id, visibility=None):
        values = {}
        for score in self.scores[model_id]:
            if score["state"] == "scored" and score["score"] is not None and score["task_id"] in self.categories:
                values.setdefault(score["task_id"], []).append(score["score"])
        tasks = self.suite.by_id()
        return {
            t: statistics.fmean(v) for t, v in values.items()
            if visibility is None or tasks[t].visibility == visibility
        }

    def operations(self, model_id):
        records = self.records[model_id]
        costs = [r["cost_usd"] for r in records if r.get("cost_usd") is not None]
        latencies = [r["latency_s"] for r in records if r["status"] != "api_error"]
        spreads = {}
        for score in self.scores[model_id]:
            if score["state"] == "scored" and score["score"] is not None:
                spreads.setdefault(score["task_id"], []).append(score["score"])
        repeated = [max(v) - min(v) for v in spreads.values() if len(v) > 1]
        return {
            "runs": len(records),
            "mean_cost_usd": statistics.fmean(costs) if costs else None,
            "median_seconds": statistics.median(latencies) if latencies else None,
            "error_rate": sum(r["status"] == "api_error" for r in records) / len(records),
            "refusal_rate": sum(r["status"] == "refusal" for r in records) / len(records),
            "inconsistent_task_rate": sum(s > 0 for s in repeated) / len(repeated) if repeated else None,
        }

    def public_private_gap(self, model_id):
        public, private = self.task_means(model_id, "public"), self.task_means(model_id, "private")
        if not public or not private:
            return None
        return statistics.fmean(public.values()) - statistics.fmean(private.values())

    def trap_pass_rate(self, model_id):
        traps = {t.id for t in self.suite.tasks if t.trap}
        means = [v for t, v in self.task_means(model_id).items() if t in traps]
        return statistics.fmean(means) if means else None

    def comparison_targets(self, model_id):
        """The direct predecessor, plus the newest same-tier GA model from each other vendor."""
        model = self.models[model_id]
        targets = []
        predecessor = model.get("predecessor")
        if predecessor in self.models:
            targets.append(("predecessor", predecessor))
        rivals = {}
        for other in self.models.values():
            if other["vendor"] == model["vendor"] or other["tier"] != model["tier"] or other["status"] != "ga":
                continue
            if other["released"] > model["released"]:
                continue
            current = rivals.get(other["vendor"])
            if current is None or other["released"] > current["released"]:
                rivals[other["vendor"]] = other
        targets.extend(("rival", m["id"]) for _, m in sorted(rivals.items()))
        return targets

    def comparisons(self, model_id):
        targets = self.comparison_targets(model_id)
        means = self.task_means(model_id)
        output = []
        for kind, other in targets:
            output.append({
                "kind": kind,
                "against": other,
                "metrics": compare(means, self.task_means(other), self.categories, len(targets), self.iterations,
                                   seed=f"{model_id}|{other}"),
            })
        return output

    def summary(self, model_id):
        return summarize_model(self.task_means(model_id), self.categories, seed=model_id)


RIVAL_LABELS = {"Improved": "Ahead", "Regressed": "Behind", "No clear change": "No clear difference"}


def _result_label(kind, label):
    return RIVAL_LABELS[label] if kind == "rival" else label


def _points(value):
    return f"{value * 100:+.1f}"


def _interval(low, high):
    return f"{low * 100:+.1f} to {high * 100:+.1f}"


def write_report(results, model_id, out_dir):
    model = results.models[model_id]
    summary = results.summary(model_id)
    comparisons = results.comparisons(model_id)
    operations = results.operations(model_id)
    gap = results.public_private_gap(model_id)
    trap = results.trap_pass_rate(model_id)
    out_dir = Path(out_dir)
    lines = [
        f"# Release report: {model['label']}",
        "",
        f"- **Model ID:** `{model['api_model']}`",
        f"- **Vendor and tier:** {model['vendor']}, {model['tier']}",
        f"- **Released:** {model['released']} ({model['status']})",
        f"- **Suite:** {results.suite.version}",
        "",
        "Scores run from 0 to 100. Differences are in points, and the labels follow the corrected interval.",
        "",
        "## Scores",
        "",
        "| Measure | Score | 95% interval | Tasks |",
        "|---|---|---|---|",
    ]
    for metric in METRICS:
        if metric in summary:
            s = summary[metric]
            lines.append(
                f"| {METRIC_LABELS[metric]} | {s['score'] * 100:.1f} | {s['low'] * 100:.1f} to {s['high'] * 100:.1f} "
                f"| {s['tasks'] if s['tasks'] is not None else ''} |"
            )
    for comparison in comparisons:
        other = results.models[comparison["against"]]
        title = "Against its predecessor" if comparison["kind"] == "predecessor" else "Against a current rival"
        lines += [
            "",
            f"## {title}: {other['label']}",
            "",
            "| Measure | Difference | 95% interval | Corrected interval | Result |",
            "|---|---|---|---|---|",
        ]
        for metric in METRICS:
            if metric in comparison["metrics"]:
                c = comparison["metrics"][metric]
                lines.append(
                    f"| {METRIC_LABELS[metric]} | {_points(c['difference'])} | {_interval(c['low'], c['high'])} "
                    f"| {_interval(c['corrected_low'], c['corrected_high'])} "
                    f"| {_result_label(comparison['kind'], c['label'])} |"
                )
    lines += [
        "",
        "## Operations",
        "",
        f"- **Runs:** {operations['runs']}",
        f"- **Mean cost per run:** {_money(operations['mean_cost_usd'])}, at the list prices recorded in the run manifest",
        f"- **Median total time:** {_seconds(operations['median_seconds'])}",
        f"- **API error rate:** {operations['error_rate']:.1%}",
        f"- **Refusal rate:** {operations['refusal_rate']:.1%}",
        f"- **Tasks with mixed results across repeats:** {_rate(operations['inconsistent_task_rate'])}",
        f"- **Trap tasks passed:** {_rate(trap)}",
        f"- **Public minus private task score:** {_points(gap) + ' points' if gap is not None else 'not available'}",
        "- **Time to first token:** not measured by this harness version",
        "",
        "## Limitations",
        "",
        "Results apply only to this model ID, suite version and test period. A \"No clear change\" label means the "
        "benchmark could not detect a difference, not that the models are equal. A growing gap between public and "
        "private tasks is a warning sign of task contamination, not proof.",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{model_id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(out_dir / f"{model_id}.json", {
        "model": model, "scores": summary, "comparisons": comparisons, "operations": operations,
        "public_minus_private": gap, "trap_pass_rate": trap,
    })
    return out_dir / f"{model_id}.md"


def _money(value):
    return f"${value:.4f}" if value is not None else "not available"


def _seconds(value):
    return f"{value:.1f}s" if value is not None else "not available"


def _rate(value):
    return f"{value:.1%}" if value is not None else "not available"


def drift_check(baseline_dir, current_dir, iterations=ITERATIONS):
    """Compare two sentinel runs, to tell silent model or setup changes from real releases."""

    def means(run_dir):
        values = {}
        for score in read_jsonl(Path(run_dir) / "scores.jsonl"):
            if score["state"] == "scored" and score["score"] is not None:
                values.setdefault((score["model"], score["task_id"]), []).append(score["score"])
        grouped = {}
        for (model, task), scores in values.items():
            grouped.setdefault(model, {})[task] = statistics.fmean(scores)
        return grouped

    baseline, current = means(baseline_dir), means(current_dir)
    shared = sorted(set(baseline) & set(current))
    output = {}
    for model in shared:
        pooled = {t: "all" for t in set(current[model]) & set(baseline[model])}
        if not pooled:
            continue
        result = compare(current[model], baseline[model], pooled, len(shared), iterations, seed=f"drift|{model}")["all"]
        output[model] = {**result, "label": "Possible drift" if result["label"] != "No clear change" else "No clear change"}
    return output
