"""Paired bootstrap comparisons with the release labels set out in the protocol.

Tasks are the unit of sampling, so repeats are averaged within a task first.
The overall index averages the categories equally, and is also reported without
the human-graded writing category. Corrected intervals use Bonferroni.
"""

import random
import statistics

from .tasks import CATEGORIES

ITERATIONS = 10_000
ALPHA = 0.05
HUMAN_GRADED = {"writing"}
OVERALL = "overall"
OVERALL_NO_WRITING = "overall_excluding_writing"
METRICS = CATEGORIES + [OVERALL, OVERALL_NO_WRITING]


def _bootstrap_means(values, iterations, rng):
    size = len(values)
    return [sum(rng.choices(values, k=size)) / size for _ in range(iterations)]


def _percentile_interval(sorted_values, alpha):
    last = len(sorted_values) - 1
    return sorted_values[round(alpha / 2 * last)], sorted_values[round((1 - alpha / 2) * last)]


def _combine(per_category, names):
    """Average categories equally, or return None if any category has no data."""
    if any(name not in per_category for name in names):
        return None
    arrays = [per_category[name] for name in names]
    return [sum(values) / len(arrays) for values in zip(*arrays)]


def _estimates(by_category):
    estimates = {c: statistics.fmean(v) for c, v in by_category.items()}
    for metric, names in ((OVERALL, CATEGORIES), (OVERALL_NO_WRITING, [c for c in CATEGORIES if c not in HUMAN_GRADED])):
        if all(n in estimates for n in names):
            estimates[metric] = statistics.fmean(estimates[n] for n in names)
    return estimates


def _distributions(by_category, iterations, rng):
    samples = {c: _bootstrap_means(v, iterations, rng) for c, v in by_category.items()}
    automated = [c for c in CATEGORIES if c not in HUMAN_GRADED]
    combined = {OVERALL: _combine(samples, CATEGORIES), OVERALL_NO_WRITING: _combine(samples, automated)}
    samples.update({k: v for k, v in combined.items() if v is not None})
    return samples


def group_by_category(task_values, task_categories):
    grouped = {}
    for task_id, value in sorted(task_values.items()):
        grouped.setdefault(task_categories[task_id], []).append(value)
    return grouped


def summarize_model(task_means, task_categories, iterations=2_000, seed="model"):
    """Each model's score per metric with a 95% interval, on a 0-1 scale."""
    grouped = group_by_category(task_means, task_categories)
    estimates = _estimates(grouped)
    samples = _distributions(grouped, iterations, random.Random(seed))
    result = {}
    for metric, estimate in estimates.items():
        low, high = _percentile_interval(sorted(samples[metric]), ALPHA)
        count = len(grouped[metric]) if metric in grouped else None
        result[metric] = {"score": estimate, "low": low, "high": high, "tasks": count}
    return result


def label_for(corrected_low, corrected_high):
    if corrected_low > 0:
        return "Improved"
    if corrected_high < 0:
        return "Regressed"
    return "No clear change"


def compare(means_new, means_old, task_categories, comparisons_in_family=1, iterations=ITERATIONS, seed="compare"):
    """Compare two models on the tasks both completed. Positive favors `means_new`."""
    shared = set(means_new) & set(means_old)
    differences = {t: means_new[t] - means_old[t] for t in shared}
    grouped = group_by_category(differences, task_categories)
    estimates = _estimates(grouped)
    samples = _distributions(grouped, iterations, random.Random(seed))
    corrected_alpha = ALPHA / (comparisons_in_family * len(METRICS))
    result = {}
    for metric, estimate in estimates.items():
        ordered = sorted(samples[metric])
        low, high = _percentile_interval(ordered, ALPHA)
        corrected_low, corrected_high = _percentile_interval(ordered, corrected_alpha)
        result[metric] = {
            "difference": estimate,
            "low": low,
            "high": high,
            "corrected_low": corrected_low,
            "corrected_high": corrected_high,
            "label": label_for(corrected_low, corrected_high),
            "tasks": len(grouped[metric]) if metric in grouped else sum(len(v) for v in grouped.values()),
        }
    return result
