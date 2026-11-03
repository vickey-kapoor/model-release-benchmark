"""Build the results website and the public data export.

Neither output may contain private task content. Private tasks appear only as
aggregate scores and the hash published before the first run.
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .common import read_json, read_jsonl, write_json, write_jsonl
from .results import METRIC_LABELS, Results
from .runner import latest_records
from .stats import METRICS

TEMPLATE = Path(__file__).parent / "site_template.html"


def is_synthetic(results):
    return any(m["provider"] == "mock" for m in results.models.values())


def build_site_data(results, repo_url=""):
    models = []
    for model_id, model in sorted(results.models.items(), key=lambda item: (item[1]["released"], item[0])):
        comparisons = []
        for comparison in results.comparisons(model_id):
            comparisons.append({
                "kind": comparison["kind"],
                "against": results.models[comparison["against"]]["label"],
                "metrics": comparison["metrics"],
            })
        models.append({
            "id": model_id,
            "label": model["label"],
            "api_model": model["api_model"],
            "vendor": model["vendor"],
            "tier": model["tier"],
            "released": model["released"],
            "status": model["status"],
            "scores": results.summary(model_id),
            "operations": results.operations(model_id),
            "comparisons": comparisons,
        })
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "suite_version": results.suite.version,
        "private_hash": results.suite.private_hash,
        "synthetic": is_synthetic(results),
        "repo_url": repo_url,
        "metrics": [{"id": m, "label": METRIC_LABELS[m]} for m in METRICS],
        "vendors": sorted({m["vendor"] for m in results.models.values()}),
        "models": models,
    }


def build_site(results, out_dir, repo_url=""):
    data = build_site_data(results, repo_url)
    # Escaping "<" keeps model labels from closing the embedding script tag.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    write_json(out_dir / "data.json", data)
    return out_dir / "index.html"


def export_public(results_dir, suite, out_dir, allow_synthetic=False):
    """Copy what can be published: public tasks, their records, and all run metadata."""
    results_dir, out_dir = Path(results_dir), Path(out_dir)
    results = Results(results_dir, suite, iterations=200)
    if is_synthetic(results) and not allow_synthetic:
        raise SystemExit("These results include mock models, so they can't be exported for publication.")
    public_ids = {t.id for t in suite.tasks if t.visibility == "public"}
    if out_dir.exists():
        shutil.rmtree(out_dir)
    tasks_out = out_dir / "tasks"
    tasks_out.mkdir(parents=True)
    shutil.copy(suite.path / "suite.json", tasks_out / "suite.json")
    if (suite.path / "public").exists():
        shutil.copytree(suite.path / "public", tasks_out / "public")
    write_json(out_dir / "private_tasks_commitment.json", {
        "suite_version": suite.version,
        "sha256": suite.private_hash,
        "private_tasks": len(suite.tasks) - len(public_ids),
    })
    for manifest_path in results_dir.glob("*/manifest.json"):
        run_dir = manifest_path.parent
        target = out_dir / "runs" / run_dir.name
        target.mkdir(parents=True)
        shutil.copy(manifest_path, target / "manifest.json")
        write_jsonl(target / "records.jsonl", [r for r in latest_records(run_dir) if r["task_id"] in public_ids])
        for name in ("scores.jsonl", "human_scores.jsonl"):
            write_jsonl(target / name, [s for s in read_jsonl(run_dir / name) if s["task_id"] in public_ids])
        if (run_dir / "grading_summary.json").exists():
            write_json(target / "grading_summary.json", read_json(run_dir / "grading_summary.json"))
    write_json(out_dir / "private_task_scores.json", {
        model_id: _private_aggregate(results, model_id) for model_id in results.models
    })
    return out_dir


def _private_aggregate(results, model_id):
    means = results.task_means(model_id, "private")
    return {
        "tasks": len(means),
        "mean_score": sum(means.values()) / len(means) if means else None,
        "public_minus_private": results.public_private_gap(model_id),
    }
