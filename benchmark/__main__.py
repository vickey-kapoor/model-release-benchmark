"""Command-line interface: python -m benchmark <command>."""

import argparse
import json
import re
import sys
from pathlib import Path

from .common import read_json
from .demo import run_demo
from .grading import export_packet, import_grades
from .publish import build_site, export_public
from .results import Results, drift_check, write_report
from .runner import DEFAULT_SEED, build_plan, run
from .scoring import score_run
from .tasks import load_suite, readiness, validate


def load_checked_suite(path):
    suite = load_suite(path)
    problems = validate(suite)
    if problems:
        raise SystemExit("Suite problems:\n  " + "\n  ".join(problems))
    return suite


def earlier_model_ids(run_dir):
    """Model ids from sibling runs, since a release's predecessor was usually run before it."""
    ids = set()
    for path in Path(run_dir).resolve().parent.glob("*/manifest.json"):
        ids.update(m["id"] for m in read_json(path)["models"])
    return ids


def check_config(config, earlier_ids=()):
    """Return problems that would make a run wrong, costly or impossible to compare."""
    problems = []
    ids = [m["id"] for m in config["models"]]
    if len(set(ids)) != len(ids):
        problems.append("model ids must be unique")
    known_ids = set(ids) | set(earlier_ids)
    for model in config["models"]:
        name = model["id"]
        if "REPLACE" in json.dumps(model):
            problems.append(f"{name}: placeholder values remain")
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", model.get("released", "")):
            problems.append(f"{name}: released must be YYYY-MM-DD")
        if model.get("predecessor") and model["predecessor"] not in known_ids:
            problems.append(f"{name}: predecessor {model['predecessor']} is not in this config or an earlier run")
        if model["provider"] != "mock" and (model.get("price_input_per_mtok") is None or model.get("price_output_per_mtok") is None):
            problems.append(f"{name}: list prices are required so spending can be tracked")
    return problems


def cmd_validate(args):
    suite = load_checked_suite(args.suite)
    summary, gaps = readiness(suite)
    print(json.dumps(summary, indent=2))
    print(f"Public hash:  {suite.public_hash}\nPrivate hash: {suite.private_hash}")
    if gaps:
        print("Not ready for a published run:\n  " + "\n  ".join(gaps))
    else:
        print("Ready: the suite meets the protocol's targets.")


def cmd_run(args):
    suite = load_checked_suite(args.suite)
    config = read_json(args.config)
    problems = check_config(config, earlier_model_ids(args.run_dir))
    if problems:
        raise SystemExit("Config problems:\n  " + "\n  ".join(problems))
    tasks = [t for t in suite.tasks if t.sentinel or not args.sentinel_only]
    planned = len(build_plan(config["models"], tasks, args.seed))
    print(f"{planned} planned runs across {len(config['models'])} models and {len(tasks)} tasks.")
    if args.dry_run:
        return
    uses_paid_api = any(m["provider"] != "mock" for m in config["models"])
    if uses_paid_api:
        if args.budget_usd is None:
            raise SystemExit("Real API runs cost money, so --budget-usd is required.")
        _, gaps = readiness(suite)
        if gaps and not args.pilot:
            raise SystemExit("The suite isn't ready:\n  " + "\n  ".join(gaps) + "\nUse --pilot for a run that won't be published.")
    summary = run(args.run_dir, config, suite, seed=args.seed, sentinel_only=args.sentinel_only, limit=args.limit,
                  workers=args.workers, budget_usd=args.budget_usd, retry_failed=args.retry_failed)
    print(json.dumps(summary, indent=2))


def check_suite_matches_run(suite, run_dir, allow_change=False):
    """Refuse to score a run with a suite other than the one it was made with, unless allowed."""
    manifest_path = Path(run_dir) / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"no manifest.json in {run_dir}; is this a run directory?")
    manifest = read_json(manifest_path)
    changed = [name for name in ("public", "private")
               if manifest[f"suite_{name}_hash"] != getattr(suite, f"{name}_hash")]
    if not changed:
        return
    message = (f"the suite's {' and '.join(changed)} hash doesn't match this run's manifest.json, "
               "so it isn't the suite the run was made with")
    if not allow_change:
        raise SystemExit(message + ". Score with the original suite, or pass --allow-suite-change "
                         "to score against this one on purpose (for example after fixing an answer key).")
    print(f"Warning: {message}. Scoring against it anyway because of --allow-suite-change.", file=sys.stderr)


def cmd_score(args):
    suite = load_checked_suite(args.suite)
    check_suite_matches_run(suite, args.run_dir, allow_change=args.allow_suite_change)
    summary = score_run(args.run_dir, suite, allow_code_exec=args.allow_code_exec, workers=args.workers)
    print(json.dumps(summary, indent=2))


def cmd_grade_export(args):
    count = export_packet(args.run_dir, load_checked_suite(args.suite), args.out, args.anchor_run, args.anchors)
    print(f"Wrote {count} outputs to {Path(args.out) / 'packet.csv'}. Keep key.json away from graders.")


def cmd_grade_import(args):
    print(json.dumps(import_grades(args.run_dir, args.key, args.grader_a, args.grader_b, args.adjudicated), indent=2))


def cmd_report(args):
    results = Results(args.results, load_checked_suite(args.suite))
    if args.model not in results.models:
        raise SystemExit(f"No results for {args.model}. Available: {', '.join(sorted(results.models))}")
    print(write_report(results, args.model, args.out))


def cmd_drift(args):
    print(json.dumps(drift_check(args.baseline, args.current), indent=2))


def cmd_site(args):
    print(build_site(Results(args.results, load_checked_suite(args.suite)), args.out, args.repo_url))


def cmd_export(args):
    print(export_public(args.results, load_checked_suite(args.suite), args.out))


def cmd_demo(args):
    print(json.dumps(run_demo(args.out, args.per_category), indent=2))


def build_parser():
    parser = argparse.ArgumentParser(prog="benchmark", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    sub = command("validate", cmd_validate, "check a suite, show its hashes and readiness")
    sub.add_argument("--suite", required=True)

    sub = command("run", cmd_run, "run a suite against the configured models")
    sub.add_argument("--suite", required=True)
    sub.add_argument("--config", required=True)
    sub.add_argument("--run-dir", required=True)
    sub.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sub.add_argument("--limit", type=int, help="stop after this many runs; resume later with the same command")
    sub.add_argument("--workers", type=int, default=4)
    sub.add_argument("--budget-usd", type=float, help="stop starting new runs once this much is spent")
    sub.add_argument("--sentinel-only", action="store_true")
    sub.add_argument("--retry-failed", action="store_true", help="run items again if they ended in an API error")
    sub.add_argument("--pilot", action="store_true", help="allow an incomplete suite, for runs that won't be published")
    sub.add_argument("--dry-run", action="store_true", help="show the planned run count and exit")

    sub = command("score", cmd_score, "score saved outputs")
    sub.add_argument("--suite", required=True)
    sub.add_argument("--run-dir", required=True)
    sub.add_argument("--allow-code-exec", action="store_true",
                     help="run model-written code; only do this inside a disposable container or VM")
    sub.add_argument("--workers", type=int, default=8,
                     help="records scored at once; keep it at or below the CPUs available to coding tests")
    sub.add_argument("--allow-suite-change", action="store_true",
                     help="score even if the suite's hashes differ from the run's manifest.json")

    sub = command("grade-export", cmd_grade_export, "create a blind grading packet")
    sub.add_argument("--suite", required=True)
    sub.add_argument("--run-dir", required=True)
    sub.add_argument("--out", required=True)
    sub.add_argument("--anchor-run", help="earlier graded run to draw blind anchor outputs from")
    sub.add_argument("--anchors", type=int, default=10)

    sub = command("grade-import", cmd_grade_import, "import two graders' scores")
    sub.add_argument("--run-dir", required=True)
    sub.add_argument("--key", required=True)
    sub.add_argument("--grader-a", required=True)
    sub.add_argument("--grader-b", required=True)
    sub.add_argument("--adjudicated")

    sub = command("report", cmd_report, "write a release report for one model")
    sub.add_argument("--suite", required=True)
    sub.add_argument("--results", required=True)
    sub.add_argument("--model", required=True)
    sub.add_argument("--out", required=True)

    sub = command("drift", cmd_drift, "compare two sentinel runs")
    sub.add_argument("--baseline", required=True)
    sub.add_argument("--current", required=True)

    sub = command("site", cmd_site, "build the results website")
    sub.add_argument("--suite", required=True)
    sub.add_argument("--results", required=True)
    sub.add_argument("--out", required=True)
    sub.add_argument("--repo-url", default="")

    sub = command("export", cmd_export, "create the public data export")
    sub.add_argument("--suite", required=True)
    sub.add_argument("--results", required=True)
    sub.add_argument("--out", required=True)

    sub = command("demo", cmd_demo, "rehearse everything offline with mock models")
    sub.add_argument("--out", default="build/demo")
    sub.add_argument("--per-category", type=int, default=40)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
