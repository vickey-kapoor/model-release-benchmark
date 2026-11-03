# Plan: a public release-over-release benchmark for GPT, Gemini and Claude

## 1. Goal
Track what changes with each new model release, then publish the results and evidence openly. Each release report should answer:

- What improved, stayed the same or got worse compared with the model's direct predecessor?
- How does the new model compare with the other vendors' current models in the same tier?
- How did cost, speed and consistency change?
- How has each vendor's quality changed over time?

Per-category results are the main findings. A secondary overall index uses fixed, equal category weights and is also reported without the human-graded writing category.

## 2. Scope

| Decision | Choice |
|---|---|
| Coverage | Broad capabilities across six categories |
| Models | Each vendor's flagship and mid-tier models |
| First published history | Current and previous generations, 12 models if all are available |
| Openness | Public methods, scores, raw outputs and most tasks, plus a private holdout |
| Grading | Mostly automated, plus a small blind human panel |
| Publishing | GitHub as the source of record, a results website for trends, and forum posts that link to both |
| Turnaround | Results within 1–2 weeks of a qualifying release |
| Budget | Set from measured pilot costs |

### Release rules
- **What triggers a run:** A generally available API model with a new pinned version ID, in a tracked tier.
- **Previews:** These may be tested, but are labeled and kept out of the trend lines.
- **Predecessors and tiers:** Publish each model's assigned tier and direct predecessor before its run. Record any judgment calls, since vendor lineups don't always map neatly.
- **Unavailable models:** Mark any model that can't be run as unavailable rather than substituting another one.

## 3. Task suite
Target 220 tasks:

| Category | Tasks | Scoring | Repeats |
|---|---|---|---|
| Coding | 40 | Automated tests | 3 |
| Data extraction | 40 | Exact field match | 3 |
| Reasoning and math | 40 | Answer key | 3 |
| Long-document analysis | 40 | Answer key, plus a check that citations are real | 3 |
| Tool use | 40 | Final state and number of steps | 3 |
| Writing | 20 | Blind human rubric | 1 |

- **Public and private tasks:** Publish 75% of the tasks and keep 25% private (55 tasks, spread across all categories).
- **Proof the private tasks didn't change:** Publish a cryptographic hash of the private task file before the first run, so it can be verified if the tasks are revealed later.
- **Task sources:** Use only original or suitably licensed material that is safe to publish, with no private work data. People write the tasks, and any AI assistance is disclosed.
- **Difficulty:** Include easy, medium and hard tasks with room above current scores, so future improvements remain visible.
- **Traps:** Include unanswerable questions in the reasoning and long-document categories to measure made-up answers.
- **Review:** A second person checks every task and answer key before any model sees it.

## 4. Keeping results comparable over time
- **Versioned suite:** Trend lines only compare results from the same suite version. Tasks are not edited in place.
- **Task errors:** If a task is found to be broken, retire it from scoring for every model, rescore the history and log the change.
- **New suite versions:** Add a new version only when scores near the ceiling or tasks are compromised. Keep enough old tasks to bridge the versions, and rerun every model still available.
- **Drift checks:** Run 20 fixed sentinel tasks on tracked models during each release cycle, and at least quarterly. This helps catch silent changes to a model.
- **Test setup changes:** Run the sentinel tasks before and after any change to the test setup, so the change isn't mistaken for a model improvement.
- **Retired models:** Keep their published scores and raw outputs, and mark them as no longer re-runnable.
- **Task contamination:** Compare public and private task scores for each model. A growing gap may mean public tasks have leaked into training. With only 55 private tasks this is a warning sign, not proof.

## 5. Test conditions
- **Prompts:** Use the same prompts and system messages for every model, with no vendor-specific tuning.
- **Settings:** Use the vendor's default API settings and the same generous output limit. Record every setting, because reasoning controls don't map neatly across vendors.
- **Tools:** Turn off web search and other built-in tools, except for the identical mock tools in the tool-use category.
- **Timing:** Shuffle the run order and spread runs across several days and times.
- **Failures:** Retry API and rate-limit errors up to a fixed limit and log them separately. Refusals and malformed outputs count as task failures.
- **Records:** Save every prompt, output, setting, timestamp, token count and cost, except that private task content and outputs stay unpublished.
- **Vendor data settings:** Before sending private tasks, review each vendor's API data retention and training terms, and choose the most restrictive available option.

## 6. Scoring and analysis
- **Grading panel:** Use 2–3 named graders. Two people blind-grade each writing output, then resolve disagreements and report how often they agreed.
- **Grading over time:** Mix about 10 archived outputs into each new batch, blind, to check whether graders' standards are shifting.
- **AI graders:** Do not use AI models for published scores. This avoids any model judging itself or a competitor.
- **Operations:** Report cost per task at the list price on the run date, time to first token, total time, error rate, refusal rate and variation across repeats.
- **Main comparisons:** Set these in advance: each new model against its direct predecessor, and against other vendors' current same-tier models.
- **Statistics:** Compare models on the same tasks, report 95% confidence intervals, and correct for multiple comparisons.
- **Result labels:** Use **Improved** or **Regressed** only when the corrected interval excludes zero. Otherwise, use **No clear change**, including for small gains.
- **Precision:** Assuming two models get different results on 20% of tasks:

  | Comparison | Tasks | Approximate margin of error |
  |---|---|---|
  | Overall | 220 | ±6 points |
  | Category | 40 | ±14 points |
  | Writing | 20 | ±20 points |

  Adjacent model releases may differ by less than these margins, so reports should say when a change is too small to detect. These figures are a rough guide, and the actual intervals are calculated from the results.

## 7. Launch plan
These are estimates for part-time work:

| Phase | Output | Time |
|---|---|---|
| 1. Define | Protocol, release rules, categories and rubrics | 3–5 days |
| 2. Build tasks | Reviewed suite and private task hash | 3–5 weeks |
| 3. Pilot | 10–15 tasks per model, measured costs and fixes to unclear tasks | 1 week |
| 4. Set budget and freeze | Final task count, locked protocol and suite version 1.0 | 1–2 days |
| 5. Baseline run | About 7,440 runs across 12 models, plus retries | 1 week |
| 6. Grade | Automated scores and about 240 writing outputs graded by two people | 1 week |
| 7. Independent check | A second person reproduces a sample of scores from the raw outputs | 3–5 days |
| 8. Publish | GitHub release, website and launch post | 2–3 days |

That comes to about 8–12 weeks in total.

**Using the pilot to set the budget:** Budget for both the baseline and an expected number of releases per year. If costs are too high, cut repeats first, then expand task count more slowly, because task count has the biggest effect on precision. The minimum is 120 tasks (20 per category), which gives margins of about ±8 points overall and ±20 points per category.

## 8. Process for each new release
A new model needs about 620 runs, plus sentinel tasks.

| Target | Step |
|---|---|
| Day 0–1 | Confirm the release qualifies, then publish its pinned ID, tier and predecessor |
| Day 1–2 | Run the sentinel tasks on tracked models and resolve any test setup issues |
| Day 2–6 | Run the full suite across several days and times |
| Day 6–9 | Complete automated scoring and blind human grading |
| Day 9–11 | Have a second person check the results and failures |
| Day 11–14 | Publish the release report, data and website update |

Do not publish partial results as final. If an early look is released, label it preliminary and limit it to automated scores.

## 9. What gets published
- **GitHub:** Protocol, public tasks and answer keys, rubrics, raw outputs for public tasks, scores, suite versions and a changelog.
- **Website:** Vendor trend lines by release date, predecessor comparisons, cross-vendor comparisons and quality vs. cost and speed charts.
- **Each release report:** Key improvements and regressions, confidence intervals, limitations and examples of successes and failures.
- **Forum posts:** A short summary with key caveats that links to one permanent results page.

## 10. Credibility and public risks
- **Independence:** Pay at list price through standard API accounts and don't accept vendor credits or sponsorship. Disclose funding, affiliations and any changes to them.
- **AI assistance:** Disclose that this plan was drafted with Claude, one of the vendors' models being tested, along with any later AI assistance on the benchmark.
- **Corrections:** Provide a public way to report errors, fix confirmed mistakes promptly and keep a dated record. Don't silently overwrite published results.
- **Vendor feedback:** Vendors can report errors through the same public process, but they get no early access to results or approval rights.
- **Pre-publication review:** Before launch, check each vendor's terms on publishing benchmarks and API outputs, and get legal advice if the terms are unclear.
- **Claims:** Stick to measured results under stated test conditions, with exact model IDs and dates. Don't claim that one model is best in general.
- **Bias:** Publish how tasks were chosen, avoid tuning them on any one model and log every change to the suite.
- **Sustainability:** Publish a maintenance policy, including what happens if releases are skipped or the project ends.

## 11. Definition of done
**For launch:**
- Anyone can trace a published public-task score to its raw output and rerun it from the protocol.
- The private task hash was published before the first run.
- A second person has reproduced a sample of scores.
- Vendor terms, disclosures and the corrections process are in place.

**For each release:**
- Results are published within 14 days, or the delay is explained publicly.
- Improvements, regressions and undetectable changes are reported using the rules set in advance.
- The trend lines, data and changelog are all updated together.
