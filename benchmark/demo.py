"""An offline rehearsal of the whole pipeline, using synthetic tasks and mock models.

Nothing here contacts a vendor, and vendor names are deliberately fictional so
the demo can't be mistaken for results about real models.
"""

import csv
import random
import shutil
from pathlib import Path

from .common import read_json, write_json
from .grading import PACKET_FIELDS, export_packet, import_grades
from .publish import build_site, export_public
from .results import Results, write_report
from .runner import run
from .scoring import score_run
from .tasks import load_suite

REVIEWER = "synthetic"
GENERATIONS = [("previous", "2025-06-16", 0.0), ("current", "2026-05-11", 0.09)]
VENDORS = [("Demo Vendor A", "a", 0.70, 0.0, 1.0), ("Demo Vendor B", "b", 0.66, 4.0, 0.6), ("Demo Vendor C", "c", 0.62, -3.0, 1.5)]
TIERS = [("flagship", 0.06, 12.0, 15.0, 60.0), ("mid", -0.05, 1.0, 4.0, 15.0)]


def demo_models():
    models = []
    for vendor, code, skill, latency_shift, price_scale in VENDORS:
        for tier, tier_skill, latency, price_in, price_out in TIERS:
            previous = None
            for generation, released, gain in GENERATIONS:
                model_id = f"demo-{code}-{tier}-{generation}"
                models.append({
                    "id": model_id,
                    "label": f"{code.upper()} {tier} {generation}",
                    "provider": "mock",
                    "api_model": model_id,
                    "vendor": vendor,
                    "tier": tier,
                    "released": released,
                    "status": "ga",
                    "predecessor": previous,
                    "price_input_per_mtok": price_in * price_scale,
                    "price_output_per_mtok": price_out * price_scale,
                    "mock_skill": skill + tier_skill + gain,
                    "mock_latency": latency + latency_shift,
                })
                previous = model_id
    return models


def _tasks_for(category, count, generator):
    difficulties = ["easy", "medium", "hard"]
    tasks = []
    for i in range(count):
        task = {
            "id": f"{category}-{i:03d}",
            "category": category,
            "difficulty": difficulties[i % 3],
            "reviewed_by": REVIEWER,
            "sentinel": category != "writing" and i < 4,
            **generator(i),
        }
        tasks.append(task)
    return tasks


def _reasoning(i):
    trap = i % 5 == 4
    if trap:
        return {"prompt": f"Record {i} has no listed price. What is its price? End with 'ANSWER: UNANSWERABLE' if it can't be known.", "answers": ["UNANSWERABLE"], "trap": True}
    return {"prompt": f"What is {i} + 17? End with a final line 'ANSWER: <number>'.", "answers": [str(i + 17)]}


def _extraction(i):
    return {"prompt": f"Return JSON with name and age from: 'Person {i} is {20 + i} years old.'", "expected": {"name": f"Person {i}", "age": 20 + i}}


def _document(i):
    document = f"The office in district {i} opened in {1990 + i}. It has {i + 3} rooms."
    if i % 5 == 4:
        return {"prompt": f"Who designed the office in district {i}? Reply as JSON with answer and citations, or answer UNANSWERABLE.", "document": document, "answers": ["UNANSWERABLE"], "trap": True}
    return {"prompt": f"When did the office in district {i} open? Reply as JSON with answer and citations.", "document": document, "answers": [str(1990 + i)], "reference_citation": f"opened in {1990 + i}"}


def _tool_use(i):
    return {
        "prompt": f"Set the status of order {i} to shipped.",
        "tools": [
            {"name": "read_record", "description": "Read a record", "op": "read"},
            {"name": "write_record", "description": "Write a record", "op": "write"},
        ],
        "initial_state": {f"order-{i}": "pending"},
        "expected_state": {f"order-{i}": "shipped"},
        "max_steps": 5,
    }


def _coding(i):
    return {
        "prompt": f"Write a Python function add_{i}(x) that returns x + {i}, in a Python code block.",
        "reference_solution": f"def add_{i}(x):\n    return x + {i}",
        "test_code": f"import unittest\nfrom solution import add_{i}\n\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(add_{i}(2), {2 + i})\n",
    }


def _writing(i):
    return {"prompt": f"Rewrite memo {i} for a new employee.", "rubric": "1 = unusable, 3 = usable with edits, 5 = ready to send"}


def build_suite(path, per_category=40):
    path = Path(path)
    write_json(path / "suite.json", {"version": "demo-synthetic"})
    generators = [("reasoning", _reasoning), ("extraction", _extraction), ("document", _document), ("tool_use", _tool_use), ("coding", _coding)]
    groups = [(c, per_category, g) for c, g in generators] + [("writing", max(1, per_category // 2), _writing)]
    for category, count, generator in groups:
        tasks = _tasks_for(category, count, generator)
        write_json(path / "public" / f"{category}.json", [t for i, t in enumerate(tasks) if i % 4 != 3])
        write_json(path / "private" / f"{category}.json", [t for i, t in enumerate(tasks) if i % 4 == 3])
    return load_suite(path)


def _synthetic_grades(grading_dir, models, a_path, b_path):
    """Pretend two graders scored the packet, close enough to need no adjudication."""
    key = {k["blind_id"]: k for k in read_json(grading_dir / "key.json")}
    skills = {m["id"]: m["mock_skill"] for m in models}
    rng = random.Random(11)
    with (grading_dir / "packet.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    outputs = {a_path: [], b_path: []}
    for row in rows:
        base = max(1, min(5, round(1 + 4 * (skills[key[row["blind_id"]]["model"]] + rng.gauss(0, 0.18)))))
        second = max(1, min(5, base + rng.choice([-1, 0, 0, 1])))
        outputs[a_path].append({**row, "score": base})
        outputs[b_path].append({**row, "score": second})
    for path, graded in outputs.items():
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, PACKET_FIELDS)
            writer.writeheader()
            writer.writerows(graded)


def run_demo(out_dir, per_category=40):
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    suite = build_suite(out_dir / "suite", per_category)
    models = demo_models()
    config = {"max_output_tokens": 4096, "models": models}
    results_dir = out_dir / "results"
    run_dir = results_dir / "baseline"
    run_summary = run(run_dir, config, suite, workers=8)
    # Only the mock's reference solutions and deliberately broken code run here.
    score_summary = score_run(run_dir, suite, allow_code_exec=True)
    grading_dir = run_dir / "grading"
    export_packet(run_dir, suite, grading_dir)
    a_path, b_path = grading_dir / "grader_a.csv", grading_dir / "grader_b.csv"
    _synthetic_grades(grading_dir, models, a_path, b_path)
    grading_summary = import_grades(run_dir, grading_dir / "key.json", a_path, b_path)
    results = Results(results_dir, suite)
    report = write_report(results, "demo-a-flagship-current", out_dir / "reports")
    site = build_site(results, out_dir / "site")
    export = export_public(results_dir, suite, out_dir / "public", allow_synthetic=True)
    return {
        "run": run_summary, "scoring": score_summary, "grading": grading_summary,
        "report": str(report), "site": str(site), "export": str(export),
    }
