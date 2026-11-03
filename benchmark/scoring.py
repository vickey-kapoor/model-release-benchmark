"""Score saved outputs. Scoring is separate from running, so results can be rescored."""

import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import write_jsonl
from .runner import latest_records

CODE_TIMEOUT_S = 30


def normalize(text):
    text = str(text).replace("*", "").replace("`", "").strip().lower()
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.rstrip(".!")


def extract_answer(output):
    matches = re.findall(r"^[\s*_#>-]*ANSWER[\s*_]*:[\s*_]*(.+?)\s*$", output, re.IGNORECASE | re.MULTILINE)
    return matches[-1] if matches else None


def parse_json_output(output):
    text = output.strip()
    fenced = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced[-1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def score_answer_key(task, record):
    answer = extract_answer(record["output"])
    if answer is None:
        return 0.0, "no ANSWER line"
    correct = normalize(answer) in {normalize(a) for a in task.spec["answers"]}
    return float(correct), f"answered {answer!r}"


def score_fields(task, record):
    parsed = parse_json_output(record["output"])
    if parsed is None:
        return 0.0, "output is not a JSON object"
    expected = task.spec["expected"]
    matched = sum(parsed.get(key) == value for key, value in expected.items())
    return matched / len(expected), f"{matched} of {len(expected)} fields match"


def score_document(task, record):
    parsed = parse_json_output(record["output"])
    if parsed is None:
        return 0.0, "output is not a JSON object"
    answer = normalize(parsed.get("answer", ""))
    if answer not in {normalize(a) for a in task.spec["answers"]}:
        return 0.0, "wrong answer"
    if answer == "unanswerable":
        return 1.0, "correctly marked unanswerable"
    citations = parsed.get("citations")
    if not isinstance(citations, list) or not citations:
        return 0.0, "right answer with no citation"
    document = " ".join(task.spec["document"].split())
    fake = [c for c in citations if " ".join(str(c).split()) not in document]
    if fake:
        return 0.0, f"{len(fake)} citation(s) are not in the document"
    return 1.0, "right answer with real citations"


def score_tool_state(task, record):
    if record["exhausted_steps"]:
        return 0.0, f"exceeded the {task.spec['max_steps']} step limit"
    if record["final_state"] != task.spec["expected_state"]:
        return 0.0, f"wrong final state after {record['steps']} steps"
    return 1.0, f"correct final state in {record['steps']} steps"


def extract_code(output):
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", output, re.DOTALL)
    return blocks[-1] if blocks else None


def score_tests(task, record):
    """Run model-written code. This is not a sandbox, so callers must opt in and isolate it."""
    code = extract_code(record["output"])
    if code is None:
        return 0.0, "no code block"
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "solution.py").write_text(code, encoding="utf-8")
        Path(folder, "test_solution.py").write_text(task.spec["test_code"], encoding="utf-8")
        # API keys and other secrets must not reach model-written code.
        env = {k: os.environ[k] for k in ("SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "test_solution"],
                cwd=folder, env=env, capture_output=True, timeout=CODE_TIMEOUT_S, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return 0.0, f"tests timed out after {CODE_TIMEOUT_S}s"
    return float(result.returncode == 0), "tests passed" if result.returncode == 0 else "tests failed"


SCORERS = {
    "answer_key": score_answer_key,
    "fields": score_fields,
    "document": score_document,
    "tool_state": score_tool_state,
    "tests": score_tests,
}


def score_record(task, record, allow_code_exec=False):
    base = {"model": record["model"], "task_id": task.id, "repeat": record["repeat"]}
    if record["status"] == "api_error":
        return {**base, "score": None, "state": "api_error", "detail": record["error"][:200]}
    if record["status"] == "refusal":
        return {**base, "score": 0.0, "state": "scored", "detail": "refusal"}
    if task.scoring == "human":
        return {**base, "score": None, "state": "needs_human", "detail": ""}
    if task.scoring == "tests" and not allow_code_exec:
        return {**base, "score": None, "state": "needs_code_execution", "detail": ""}
    score, detail = SCORERS[task.scoring](task, record)
    return {**base, "score": score, "state": "scored", "detail": detail}


def score_run(run_dir, suite, allow_code_exec=False, workers=8):
    tasks = suite.by_id()
    records = [r for r in latest_records(run_dir) if r["task_id"] in tasks]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        scores = list(pool.map(lambda r: score_record(tasks[r["task_id"]], r, allow_code_exec), records))
    write_jsonl(Path(run_dir) / "scores.jsonl", scores)
    summary = {}
    for score in scores:
        summary[score["state"]] = summary.get(score["state"], 0) + 1
    return summary
