# Model release benchmark

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![Status: pre-release, no published results](https://img.shields.io/badge/status-pre--release-orange)](BENCHMARK_PLAN.md)

Tracks what changes with each GPT, Gemini and Claude release, against its predecessor and other vendors' current models. The protocol is in [BENCHMARK_PLAN.md](BENCHMARK_PLAN.md).

It needs Python 3.11 or later and has no other dependencies.

## Try it offline
This runs the full pipeline with mock models and synthetic tasks. It makes no API calls and costs nothing.

```
python -m benchmark demo
python -m unittest discover tests
```

Open `build/demo/site/index.html` to see the site. Demo scores are fake, use fictional vendors and carry a banner on the site.

## Run a benchmark

1. **Write the suite.** Create `tasks/suite/suite.json` with a version, and add tasks under `public/` and `private/`. See `tasks/examples` for each category's format. Every task needs a `reviewed_by` value, and 20 tasks should set `"sentinel": true`.
2. **Check it and publish the private hash.** Do this before the first run:
   ```
   python -m benchmark validate --suite tasks/suite
   ```
3. **Configure models.** Copy `config/models.template.json` to `config/models.json`, then add one entry per model with its pinned ID, release date, predecessor and list prices.
4. **Set API keys.** Set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`.
5. **Check the plan, then run.** Real API runs require a budget. Runs can stop and resume, so use `--limit` to spread one run across several days and times:
   ```
   python -m benchmark run --suite tasks/suite --config config/models.json --run-dir results/baseline --dry-run
   python -m benchmark run --suite tasks/suite --config config/models.json --run-dir results/baseline --budget-usd 25 --limit 500
   ```
   Use `--pilot` for a run on an incomplete suite that won't be published.
6. **Score the outputs.** `--allow-code-exec` runs model-written code. The harness removes API keys from that code's environment and sets a timeout, but it isn't a sandbox, so only use this flag inside a disposable container or VM with no network access.
   ```
   python -m benchmark score --suite tasks/suite --run-dir results/baseline --allow-code-exec
   ```
7. **Grade the writing outputs.** Give each grader a copy of `packet.csv`, and keep `key.json` away from them. Scores use a 1–5 scale. Gaps of more than one point need a third, adjudicated file.
   ```
   python -m benchmark grade-export --suite tasks/suite --run-dir results/baseline --out results/baseline/grading
   python -m benchmark grade-import --run-dir results/baseline --key results/baseline/grading/key.json --grader-a a.csv --grader-b b.csv
   ```
8. **Publish.** Publish only the output of `export`, `report` and `site`. The raw `results/` folder contains private tasks.
   ```
   python -m benchmark report --suite tasks/suite --results results --model MODEL_ID --out public/reports
   python -m benchmark export --suite tasks/suite --results results --out public/data
   python -m benchmark site --suite tasks/suite --results results --out public/site
   ```

## For each new release
Add the model to a new config with a new run directory, and run it through steps 5–8. On each release cycle, run `--sentinel-only` on tracked models in a separate directory, then compare it with an earlier sentinel run:

```
python -m benchmark drift --baseline results/sentinel-old --current results/sentinel-new
```

For later writing batches, pass `--anchor-run` to `grade-export` to mix blind archived outputs into the packet. This shows whether graders' standards are shifting.
