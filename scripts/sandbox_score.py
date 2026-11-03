"""Score a run with --allow-code-exec inside a locked-down Docker container.

Model-written code never runs on the host. The container has no network, a
read-only filesystem, no API keys, no Linux capabilities and capped memory,
CPU and processes. It works on a temporary copy of the run directory, and only
scores.jsonl is copied back, so the original records can't be changed.

    python scripts/sandbox_score.py --suite tasks/examples --run-dir results/pilot-3

Requires Docker. The image is built from the Dockerfile on first use; pass
--rebuild after changing the benchmark code.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE = "model-release-benchmark-sandbox"
ROOT = Path(__file__).resolve().parent.parent


def docker(*args, capture=False):
    return subprocess.run(["docker", *args], check=False, text=True, capture_output=capture)


def ensure_image(rebuild):
    exists = docker("image", "inspect", IMAGE, capture=True).returncode == 0
    if exists and not rebuild:
        return
    if docker("build", "--tag", IMAGE, str(ROOT)).returncode != 0:
        raise SystemExit("docker build failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--rebuild", action="store_true", help="rebuild the image first")
    args = parser.parse_args()

    suite = Path(args.suite).resolve()
    run_dir = Path(args.run_dir).resolve()
    for path in (suite, run_dir):
        if not path.is_dir():
            raise SystemExit(f"not a directory: {path}")
    if shutil.which("docker") is None:
        raise SystemExit("docker not found; install Docker Desktop and start it")
    if docker("info", capture=True).returncode != 0:
        raise SystemExit("the Docker engine isn't running; start Docker Desktop and try again")

    ensure_image(args.rebuild)

    with tempfile.TemporaryDirectory(prefix="sandbox-score-") as work:
        work_run = Path(work) / "run"
        shutil.copytree(run_dir, work_run)
        command = [
            "run", "--rm",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", "1g",
            "--cpus", "1",
            "--pids-limit", "256",
            "--volume", f"{suite}:/suite:ro",
            "--volume", f"{work_run}:/run-dir",
        ]
        if hasattr(os, "getuid"):
            # On Linux, write as the host user so the copied-back file stays editable.
            command +=["--user", f"{os.getuid()}:{os.getgid()}"]
        command += [IMAGE, "score", "--suite", "/suite", "--run-dir", "/run-dir", "--allow-code-exec"]
        if docker(*command).returncode != 0:
            raise SystemExit("sandboxed scoring failed; the original run directory is unchanged")
        shutil.copy2(work_run / "scores.jsonl", run_dir / "scores.jsonl")
    print(f"Wrote {run_dir / 'scores.jsonl'}")


if __name__ == "__main__":
    sys.exit(main())
