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
import stat
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE = "model-release-benchmark-sandbox"
ROOT = Path(__file__).resolve().parent.parent
# CPUs given to the container. Scoring runs one worker per CPU: coding tests have a
# wall-clock timeout, so extra workers sharing a CPU can make correct solutions time out.
CPUS = 1
MAX_SCORES_BYTES = 64 * 1024 * 1024


def docker(*args, capture=False):
    return subprocess.run(["docker", *args], check=False, text=True, capture_output=capture)


def ensure_image(rebuild):
    exists = docker("image", "inspect", IMAGE, capture=True).returncode == 0
    if exists and not rebuild:
        return
    if docker("build", "--tag", IMAGE, str(ROOT)).returncode != 0:
        raise SystemExit("docker build failed")


def sandbox_command(suite, work_run, cpus=CPUS, allow_suite_change=False):
    command = [
        "run", "--rm",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=256m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", "1g",
        "--cpus", str(cpus),
        "--pids-limit", "256",
        "--volume", f"{suite}:/suite:ro",
        "--volume", f"{work_run}:/run-dir",
    ]
    if hasattr(os, "getuid"):
        # On Linux, write as the host user so the copied-back file stays editable.
        command += ["--user", f"{os.getuid()}:{os.getgid()}"]
    command += [IMAGE, "score", "--suite", "/suite", "--run-dir", "/run-dir", "--allow-code-exec",
                "--workers", str(cpus)]
    if allow_suite_change:
        command.append("--allow-suite-change")
    return command


def read_sandbox_scores(path):
    """Read scores.jsonl written in the sandbox, refusing anything but a regular file.

    Model-written code can write to the run directory, so scores.jsonl may be a
    symlink to a host path. Following it would copy a host file into the saved scores.
    """
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise SystemExit("sandboxed scoring wrote no scores.jsonl; the original run directory is unchanged")
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("scores.jsonl from the sandbox isn't a regular file; refusing to copy it")
    # O_NOFOLLOW closes the gap between the check above and the open, where it exists.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise SystemExit(f"can't open scores.jsonl from the sandbox safely: {error}")
    with os.fdopen(fd, "rb") as f:
        opened = os.fstat(f.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise SystemExit("scores.jsonl from the sandbox changed while it was being read; refusing to copy it")
        data = f.read(MAX_SCORES_BYTES + 1)
    if len(data) > MAX_SCORES_BYTES:
        raise SystemExit("scores.jsonl from the sandbox is implausibly large; refusing to copy it")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--rebuild", action="store_true", help="rebuild the image first")
    parser.add_argument("--allow-suite-change", action="store_true",
                        help="score even if the suite's hashes differ from the run's manifest.json")
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
        command = sandbox_command(suite, work_run, allow_suite_change=args.allow_suite_change)
        if docker(*command).returncode != 0:
            raise SystemExit("sandboxed scoring failed; the original run directory is unchanged")
        data = read_sandbox_scores(work_run / "scores.jsonl")
    (run_dir / "scores.jsonl").write_bytes(data)
    print(f"Wrote {run_dir / 'scores.jsonl'}")


if __name__ == "__main__":
    sys.exit(main())
