"""Run tasks against models and save an auditable, resumable record of every run."""

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .common import read_json, read_jsonl, write_json
from .providers import PermanentError, TransientError, get_provider

MAX_RETRIES = 5
DEFAULT_SEED = 20261102
COMPLETE_STATUSES = {"ok", "refusal"}


def apply_tool(tools, state, name, args):
    tool = next((t for t in tools if t["name"] == name), None)
    if tool is None:
        return f"ERROR: unknown tool {name}"
    if tool["op"] == "list":
        return json.dumps(sorted(state))
    key = str(args.get("key", ""))
    if tool["op"] == "read":
        return json.dumps(state[key]) if key in state else "NOT_FOUND"
    state[key] = str(args.get("value", ""))
    return "OK"


def call_with_retries(provider, model, system, messages, tools, max_tokens, task, sleep, retries):
    attempt = 0
    while True:
        attempt += 1
        started = time.perf_counter()
        try:
            response = provider.call(model, system, messages, tools, max_tokens, task=task)
            elapsed = time.perf_counter() - started
            if response.simulated_latency is not None:
                elapsed = response.simulated_latency
            return response, elapsed, attempt
        except TransientError as error:
            if attempt > retries:
                raise
            delay = error.retry_after if error.retry_after is not None else min(60, 2**attempt) + random.random()
            sleep(delay)


def execute(task, model, repeat, provider, max_tokens, sleep=time.sleep, retries=MAX_RETRIES):
    """Run one task once. API failures become records rather than exceptions."""
    model = {**model, "_repeat": repeat}
    tools = task.spec.get("tools") if task.scoring == "tool_state" else None
    state = dict(task.spec.get("initial_state", {})) if tools else None
    max_steps = task.spec.get("max_steps", 0)
    record = {
        "model": model["id"],
        "task_id": task.id,
        "repeat": repeat,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "output": "",
        "error": "",
        "finish_reason": "",
        "served_model": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_s": 0.0,
        "attempts": 0,
        "steps": 0,
        "tool_calls": [],
        "exhausted_steps": False,
        "final_state": None,
    }
    try:
        messages = provider.start(task.full_prompt(), task.system)
        while True:
            response, elapsed, attempts = call_with_retries(
                provider, model, task.system, messages, tools, max_tokens, task, sleep, retries
            )
            record["latency_s"] += elapsed
            record["attempts"] += attempts
            record["input_tokens"] += response.input_tokens
            record["output_tokens"] += response.output_tokens
            record["finish_reason"] = response.finish_reason
            record["served_model"] = response.served_model or record["served_model"]
            record["output"] = response.text
            if response.refusal:
                record["status"] = "refusal"
                break
            if not tools or not response.tool_calls:
                break
            if record["steps"] + len(response.tool_calls) > max_steps:
                record["exhausted_steps"] = True
                break
            results = []
            for call in response.tool_calls:
                result = apply_tool(tools, state, call.name, call.args)
                record["tool_calls"].append({"name": call.name, "args": call.args, "result": result})
                record["steps"] += 1
                results.append((call, result))
            provider.extend(messages, response, results)
    except (TransientError, PermanentError) as error:
        record["status"] = "api_error"
        record["error"] = str(error)
    record["final_state"] = state
    price_in, price_out = model.get("price_input_per_mtok"), model.get("price_output_per_mtok")
    record["cost_usd"] = (
        (record["input_tokens"] * price_in + record["output_tokens"] * price_out) / 1_000_000
        if price_in is not None and price_out is not None
        else None
    )
    return record


def build_plan(models, tasks, seed):
    """A fixed shuffled order, so a run split across days still follows one schedule."""
    items = [(m["id"], t.id, r) for m in models for t in tasks for r in range(t.repeats)]
    items.sort()
    random.Random(seed).shuffle(items)
    return items


def init_run(run_dir, config, suite, seed, sentinel_only):
    run_dir = Path(run_dir)
    manifest = {
        "harness_version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suite_version": suite.version,
        "suite_public_hash": suite.public_hash,
        "suite_private_hash": suite.private_hash,
        "seed": seed,
        "sentinel_only": sentinel_only,
        "max_output_tokens": config["max_output_tokens"],
        "models": config["models"],
    }
    path = run_dir / "manifest.json"
    if path.exists():
        existing = read_json(path)
        for key in ("suite_public_hash", "suite_private_hash", "seed", "sentinel_only", "max_output_tokens", "models"):
            if existing[key] != manifest[key]:
                raise SystemExit(f"Refusing to resume {run_dir}: {key} differs from the original run.")
        return existing
    write_json(path, manifest)
    return manifest


def run(run_dir, config, suite, seed=DEFAULT_SEED, sentinel_only=False, limit=None, workers=4,
        budget_usd=None, retry_failed=False, providers=None, sleep=time.sleep):
    run_dir = Path(run_dir)
    init_run(run_dir, config, suite, seed, sentinel_only)
    tasks = [t for t in suite.tasks if t.sentinel or not sentinel_only]
    models = {m["id"]: m for m in config["models"]}
    tasks_by_id = {t.id: t for t in tasks}
    records_path = run_dir / "records.jsonl"
    existing = read_jsonl(records_path)
    finished = {
        (r["model"], r["task_id"], r["repeat"])
        for r in existing
        if r["status"] in COMPLETE_STATUSES or not retry_failed
    }
    spent = sum(r.get("cost_usd") or 0 for r in existing)
    pending = [i for i in build_plan(config["models"], tasks, seed) if i not in finished]
    if limit is not None:
        pending = pending[:limit]
    providers = providers or {name: get_provider(name) for name in {m["provider"] for m in models.values()}}
    lock = threading.Lock()
    summary = {"completed": 0, "api_errors": 0, "skipped_for_budget": 0, "spent_usd": spent}

    def work(item):
        model_id, task_id, repeat = item
        with lock:
            if budget_usd is not None and summary["spent_usd"] >= budget_usd:
                summary["skipped_for_budget"] += 1
                return
        model = models[model_id]
        record = execute(
            tasks_by_id[task_id], model, repeat, providers[model["provider"]], config["max_output_tokens"], sleep=sleep
        )
        with lock, records_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            summary["completed"] += 1
            summary["api_errors"] += record["status"] == "api_error"
            summary["spent_usd"] += record.get("cost_usd") or 0

    records_path.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, pending))
    summary["remaining"] = max(0, len(build_plan(config["models"], tasks, seed)) - len(finished) - summary["completed"])
    return summary


def latest_records(run_dir):
    """Keep the newest record per run item, so a successful retry replaces its failure."""
    latest = {}
    for record in read_jsonl(Path(run_dir) / "records.jsonl"):
        latest[(record["model"], record["task_id"], record["repeat"])] = record
    return list(latest.values())
