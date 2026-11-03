"""Blind human grading: export shuffled packets, then import two graders' scores."""

import csv
import random
import re
from pathlib import Path

from .common import read_json, read_jsonl, write_json, write_jsonl
from .runner import latest_records

SCALE_MIN, SCALE_MAX = 1, 5
ALLOWED_GAP = 1
IDENTIFIERS = re.compile(r"\b(chatgpt|gpt-?\d[\w.-]*|gpt|openai|claude|anthropic|gemini)\b", re.IGNORECASE)
PACKET_FIELDS = ["blind_id", "rubric", "prompt", "output", "score", "notes"]


def anonymize(text):
    return IDENTIFIERS.sub("[MODEL]", text)


def export_packet(run_dir, suite, out_dir, anchor_run=None, anchors=10, seed=7):
    """Write packet.csv for graders and key.json, which graders must not see."""
    run_dir, out_dir = Path(run_dir), Path(out_dir)
    tasks = suite.by_id()
    scores = read_jsonl(run_dir / "scores.jsonl")
    needed = {(s["model"], s["task_id"], s["repeat"]) for s in scores if s["state"] == "needs_human"}
    items = [(r, None) for r in latest_records(run_dir) if (r["model"], r["task_id"], r["repeat"]) in needed]
    if anchor_run:
        archived = {(s["model"], s["task_id"], s["repeat"]): s["score"] for s in read_jsonl(Path(anchor_run) / "human_scores.jsonl")}
        candidates = [r for r in latest_records(anchor_run) if (r["model"], r["task_id"], r["repeat"]) in archived]
        for record in random.Random(seed).sample(candidates, min(anchors, len(candidates))):
            items.append((record, archived[(record["model"], record["task_id"], record["repeat"])]))
    random.Random(seed).shuffle(items)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = []
    with (out_dir / "packet.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, PACKET_FIELDS)
        writer.writeheader()
        for number, (record, archived_score) in enumerate(items, start=1):
            blind_id = f"B{number:05d}"
            task = tasks[record["task_id"]]
            writer.writerow({
                "blind_id": blind_id,
                "rubric": task.spec["rubric"],
                "prompt": task.prompt,
                "output": anonymize(record["output"]),
                "score": "",
                "notes": "",
            })
            key.append({
                "blind_id": blind_id,
                "model": record["model"],
                "task_id": record["task_id"],
                "repeat": record["repeat"],
                "anchor": archived_score is not None,
                "archived_score": archived_score,
            })
    write_json(out_dir / "key.json", key)
    return len(key)


def _read_grades(path):
    grades = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            value = (row.get("score") or "").strip()
            if value:
                try:
                    number = float(value)
                except ValueError:
                    raise SystemExit(f"{path}: {row['blind_id']} has a non-numeric score {value!r}")
                if not SCALE_MIN <= number <= SCALE_MAX:
                    raise SystemExit(f"{path}: {row['blind_id']} score {value} is outside {SCALE_MIN}-{SCALE_MAX}")
                grades[row["blind_id"]] = number
    return grades


def to_unit(grade):
    return (grade - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)


def import_grades(run_dir, key_path, grader_a, grader_b, adjudicated=None):
    key = read_json(key_path)
    a, b = _read_grades(grader_a), _read_grades(grader_b)
    resolved = _read_grades(adjudicated) if adjudicated else {}
    missing = [k["blind_id"] for k in key if k["blind_id"] not in a or k["blind_id"] not in b]
    if missing:
        raise SystemExit(f"{len(missing)} outputs lack two grades, starting with {missing[0]}.")
    disputes = [
        k["blind_id"] for k in key
        if abs(a[k["blind_id"]] - b[k["blind_id"]]) > ALLOWED_GAP and k["blind_id"] not in resolved
    ]
    if disputes:
        raise SystemExit(f"{len(disputes)} outputs need an adjudicated score: {', '.join(disputes[:10])}")
    rows, anchor_gaps = [], []
    for entry in key:
        blind_id = entry["blind_id"]
        final = resolved.get(blind_id, (a[blind_id] + b[blind_id]) / 2)
        if entry["anchor"]:
            anchor_gaps.append(to_unit(final) - entry["archived_score"])
            continue
        rows.append({
            "model": entry["model"],
            "task_id": entry["task_id"],
            "repeat": entry["repeat"],
            "score": to_unit(final),
            "state": "scored",
            "detail": f"grades {a[blind_id]:g} and {b[blind_id]:g}, final {final:g}",
        })
    write_jsonl(Path(run_dir) / "human_scores.jsonl", rows)
    total = len(key)
    summary = {
        "graded": len(rows),
        "exact_agreement": sum(a[k["blind_id"]] == b[k["blind_id"]] for k in key) / total if total else None,
        "within_one_point": sum(abs(a[k["blind_id"]] - b[k["blind_id"]]) <= 1 for k in key) / total if total else None,
        "adjudicated": len(resolved),
        "anchors": len(anchor_gaps),
        "anchor_mean_shift": sum(anchor_gaps) / len(anchor_gaps) if anchor_gaps else None,
    }
    write_json(Path(run_dir) / "grading_summary.json", summary)
    return summary
