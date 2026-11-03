# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A release-over-release benchmark for GPT, Gemini and Claude models. Pure Python 3.11+ with **no third-party dependencies** (HTTP goes through `urllib`). Keep it that way. The protocol the code implements, including task targets, statistics and publishing rules, is in `BENCHMARK_PLAN.md`. Read it before you change scoring, stats or what gets published. `README.md` walks through a real run step by step.

## Commands

```
python -m benchmark demo                       # full offline pipeline with mock models -> build/demo (site at build/demo/site/index.html)
python -m unittest discover tests              # all tests
python -m unittest tests.test_benchmark.ScoringTests.test_answer_normalization   # single test
python -m benchmark validate --suite tasks/examples
python -m benchmark run ... --dry-run          # show the planned run count without calling any API
```

There's no linter or build step. Real runs need `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`, plus `--budget-usd`. Don't start a paid run unless the user asks for one.

## Pipeline and architecture

The CLI (`benchmark/__main__.py`) runs a fixed pipeline. Each stage reads files the previous stage wrote into a **run directory**:

`validate` → `run` (writes `manifest.json` and `records.jsonl`) → `score` (`scores.jsonl`) → `grade-export`/`grade-import` (`human_scores.jsonl`) → `report` / `export` / `site` (these read every run under a `results/` directory)

- **`tasks.py`**: a suite is `<suite>/suite.json` plus `public/*.json` and `private/*.json`, where each file is a JSON array of tasks. The category sets the scoring method (`SCORING_BY_CATEGORY`) and the repeats (3, or 1 for writing). Fields specific to a category stay in `Task.spec`. Public and private directories are each hashed, and those hashes identify a suite version everywhere downstream.
- **`runner.py`**: `build_plan` makes a seeded, shuffled list of `(model, task, repeat)` items. Runs are **resumable** and append to `records.jsonl`. `init_run` refuses to resume if the hashes, seed, models or token limit differ from the manifest. `latest_records` keeps the newest record per item, so a retry replaces a failure. API failures become `api_error` records rather than exceptions. Tool-use tasks run a simulated key-value tool loop (`apply_tool`) limited by `max_steps`.
- **`providers.py`**: there's one class per vendor (OpenAI, Anthropic, Gemini), plus `MockProvider`, which is deterministic and used by tests and the demo. Providers raise `TransientError` (retried with backoff) or `PermanentError`.
- **`scoring.py`**: one scorer per scoring type. `api_error` is excluded (score `None`), and `refusal` scores 0. Coding tasks only run with `--allow-code-exec`. That runs model code in a subprocess with a stripped environment and a timeout, but **it isn't a sandbox**. Don't weaken the stripping of API keys from that environment.
- **`stats.py`**: a paired bootstrap that samples tasks, after averaging repeats within each task. Categories are weighted equally in `overall`, and there's also `overall_excluding_writing`. A Bonferroni-corrected interval gives the labels Improved, Regressed and No clear change.
- **`results.py`**: `Results` loads every non-sentinel run whose suite hashes match. When a model appears in several runs, the newest run wins. A model's predecessor may come from an earlier sibling run. `drift_check` compares sentinel-only runs.
- **`grading.py`**: writes blind CSV packets for writing tasks, anonymizing outputs and keeping the model mapping in `key.json`. Grades use a 1–5 scale. Gaps of more than 1 point need an adjudicated file.
- **`publish.py`**: `export` and `site` must **never contain private task content**. Private tasks appear only as aggregate scores and their hash. Results from mock models are refused by `export` unless allowed, and are marked with a banner on the site. Tests in `EndToEndTests` enforce this.

## Invariants to preserve

- Private tasks (`tasks/**/private/`, except the examples), `results/`, `build/` and `config/models.json` are gitignored. Raw run directories contain private tasks and grading keys.
- Hashing ignores CRLF/LF differences, so the published hash stays the same on Windows.
- `check_config` in `__main__.py` rejects `REPLACE` placeholders, predecessors it doesn't know about, and paid models without list prices.
