import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchmark.__main__ import check_config, earlier_model_ids
from benchmark.common import read_jsonl
from benchmark.demo import build_suite, demo_models, run_demo
from benchmark.grading import anonymize, export_packet, import_grades
from benchmark.providers import (
    AnthropicProvider, GeminiProvider, OpenAIProvider, PermanentError, Response, ToolCall, TransientError,
)
from benchmark.results import Results, drift_check
from benchmark.publish import build_site, build_site_data, export_public
from benchmark.runner import apply_tool, build_plan, execute, latest_records, run
from benchmark.scoring import extract_answer, score_record, score_run
from benchmark.stats import compare, label_for
from benchmark.tasks import Task, load_suite, readiness, validate

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "tasks" / "examples"
TOOLS = [
    {"name": "list_records", "description": "List", "op": "list"},
    {"name": "write_record", "description": "Write", "op": "write"},
]


def make_task(category="reasoning", **spec):
    prompt = spec.pop("prompt", "Question? ANSWER:")
    return Task(id="t1", category=category, difficulty="easy", prompt=prompt, visibility="public", spec=spec)


def make_record(output="", status="ok", **extra):
    return {"model": "m", "repeat": 0, "output": output, "status": status, "error": "",
            "exhausted_steps": False, "final_state": None, "steps": 0, **extra}


class SuiteTests(unittest.TestCase):
    def test_examples_are_valid_but_not_ready_to_publish(self):
        suite = load_suite(EXAMPLES)
        self.assertEqual(validate(suite), [])
        _, gaps = readiness(suite)
        self.assertTrue(gaps)

    def test_validation_catches_a_missing_answer_format(self):
        suite = load_suite(EXAMPLES)
        suite.tasks[0].spec.pop("test_code")
        self.assertIn("missing 'test_code'", validate(suite)[0])

    def test_private_hash_changes_when_a_task_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            before = build_suite(folder, 4).private_hash
            path = next(Path(folder, "private").glob("*.json"))
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            self.assertNotEqual(load_suite(folder).private_hash, before)


class ScoringTests(unittest.TestCase):
    def test_answer_extraction_uses_the_last_answer_line(self):
        self.assertEqual(extract_answer("ANSWER: 1\nActually\n**ANSWER:** 1,000."), "1,000.")

    def test_answer_normalization(self):
        task = make_task(answers=["1000"])
        self.assertEqual(score_record(task, make_record("**ANSWER:** 1,000."))["score"], 1.0)
        self.assertEqual(score_record(task, make_record("The answer is 1000"))["score"], 0.0)

    def test_fields_score_partial_matches_exactly(self):
        task = make_task("extraction", expected={"a": 1, "b": "x"})
        self.assertEqual(score_record(task, make_record('```json\n{"a": 1, "b": "y"}\n```'))["score"], 0.5)
        self.assertEqual(score_record(task, make_record("nope"))["score"], 0.0)

    def test_document_rejects_invented_citations(self):
        task = make_task("document", document="It reopened\nin 2021.", answers=["2021"])
        good = json.dumps({"answer": "2021", "citations": ["reopened in 2021"]})
        fake = json.dumps({"answer": "2021", "citations": ["closed in 2021"]})
        missing = json.dumps({"answer": "2021", "citations": []})
        self.assertEqual(score_record(task, make_record(good))["score"], 1.0)
        self.assertEqual(score_record(task, make_record(fake))["score"], 0.0)
        self.assertEqual(score_record(task, make_record(missing))["score"], 0.0)

    def test_unanswerable_needs_no_citation(self):
        task = make_task("document", document="Text.", answers=["UNANSWERABLE"])
        output = json.dumps({"answer": "UNANSWERABLE", "citations": []})
        self.assertEqual(score_record(task, make_record(output))["score"], 1.0)

    def test_tool_state_requires_the_exact_state_within_the_limit(self):
        task = make_task("tool_use", tools=TOOLS, initial_state={}, expected_state={"a": "b"}, max_steps=2)
        self.assertEqual(score_record(task, make_record(final_state={"a": "b"}))["score"], 1.0)
        self.assertEqual(score_record(task, make_record(final_state={"a": "b", "c": "d"}))["score"], 0.0)
        self.assertEqual(score_record(task, make_record(final_state={"a": "b"}, exhausted_steps=True))["score"], 0.0)

    def test_refusal_is_a_failure_but_api_errors_are_excluded(self):
        task = make_task(answers=["1"])
        self.assertEqual(score_record(task, make_record(status="refusal"))["score"], 0.0)
        self.assertIsNone(score_record(task, make_record(status="api_error"))["score"])

    def test_code_needs_explicit_permission(self):
        task = make_task("coding", test_code="", reference_solution="")
        self.assertEqual(score_record(task, make_record("```python\nx = 1\n```"))["state"], "needs_code_execution")

    def test_code_tests_can_pass_or_fail_and_cannot_read_api_keys(self):
        test = (
            "import os, unittest\nfrom solution import double\n\nclass T(unittest.TestCase):\n"
            "    def test(self):\n        self.assertEqual(double(2), 4)\n"
            "        self.assertNotIn('OPENAI_API_KEY', os.environ)\n"
        )
        task = make_task("coding", test_code=test, reference_solution="")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}):
            good = score_record(task, make_record("```python\ndef double(x):\n    return x * 2\n```"), allow_code_exec=True)
            bad = score_record(task, make_record("```python\ndef double(x):\n    return x\n```"), allow_code_exec=True)
        self.assertEqual(good["score"], 1.0)
        self.assertEqual(bad["score"], 0.0)


class FakeProvider:
    """Replays scripted responses, and can raise errors along the way."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def start(self, prompt, system):
        return []

    def call(self, model, system, messages, tools, max_tokens, task=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def extend(self, messages, response, results):
        pass


class RunnerTests(unittest.TestCase):
    model = {"id": "m", "provider": "fake", "price_input_per_mtok": 1.0, "price_output_per_mtok": 2.0}

    def test_retries_then_succeeds_and_accumulates_cost(self):
        provider = FakeProvider([TransientError("busy"), Response(text="ANSWER: 1", input_tokens=1000, output_tokens=500)])
        record = execute(make_task(answers=["1"]), self.model, 0, provider, 100, sleep=lambda s: None)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["attempts"], 2)
        self.assertAlmostEqual(record["cost_usd"], 0.002)

    def test_retry_limit_records_an_api_error(self):
        provider = FakeProvider([TransientError("busy")] * 3)
        record = execute(make_task(answers=["1"]), self.model, 0, provider, 100, sleep=lambda s: None, retries=2)
        self.assertEqual(record["status"], "api_error")
        self.assertEqual(provider.calls, 3)

    def test_permanent_errors_are_not_retried(self):
        provider = FakeProvider([PermanentError("bad key")])
        record = execute(make_task(answers=["1"]), self.model, 0, provider, 100, sleep=lambda s: None)
        self.assertEqual(record["status"], "api_error")
        self.assertEqual(provider.calls, 1)

    def test_tool_loop_changes_state_and_enforces_the_step_limit(self):
        task = make_task("tool_use", tools=TOOLS, initial_state={}, expected_state={"a": "b"}, max_steps=1)
        call = ToolCall("1", "write_record", {"key": "a", "value": "b"})
        record = execute(task, self.model, 0, FakeProvider([Response(tool_calls=[call]), Response(text="done")]), 100)
        self.assertEqual(record["final_state"], {"a": "b"})
        self.assertEqual(record["steps"], 1)
        looping = FakeProvider([Response(tool_calls=[call]), Response(tool_calls=[call])])
        self.assertTrue(execute(task, self.model, 0, looping, 100)["exhausted_steps"])

    def test_unknown_tools_return_an_error_to_the_model(self):
        self.assertIn("ERROR", apply_tool(TOOLS, {}, "delete_everything", {}))

    def test_plan_is_reproducible_and_complete(self):
        tasks = [make_task(answers=["1"])]
        plan = build_plan([{"id": "a"}, {"id": "b"}], tasks, 1)
        self.assertEqual(plan, build_plan([{"id": "a"}, {"id": "b"}], tasks, 1))
        self.assertEqual(len(plan), 6)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.suite = build_suite(self.root / "suite", 4)
        self.models = demo_models()[:1]
        self.config = {"max_output_tokens": 100, "models": self.models}

    def test_runs_resume_without_repeating_work(self):
        run_dir = self.root / "run"
        first = run(run_dir, self.config, self.suite, limit=5, workers=1)
        self.assertEqual(first["completed"], 5)
        second = run(run_dir, self.config, self.suite, workers=2)
        self.assertEqual(second["remaining"], 0)
        keys = [(r["model"], r["task_id"], r["repeat"]) for r in read_jsonl(run_dir / "records.jsonl")]
        self.assertEqual(len(keys), len(set(keys)))

    def test_budget_stops_new_runs(self):
        summary = run(self.root / "run", self.config, self.suite, workers=1, budget_usd=0.01)
        self.assertGreater(summary["skipped_for_budget"], 0)
        self.assertGreater(summary["remaining"], 0)

    def test_resume_refuses_changed_models(self):
        run_dir = self.root / "run"
        run(run_dir, self.config, self.suite, limit=1, workers=1)
        changed = {"max_output_tokens": 100, "models": [{**self.models[0], "price_output_per_mtok": 999}]}
        with self.assertRaises(SystemExit):
            run(run_dir, changed, self.suite, limit=1, workers=1)

    def test_retry_failed_replaces_api_errors(self):
        class Flaky:
            def __init__(self, fail):
                self.fail = fail

            def start(self, prompt, system):
                return []

            def call(self, model, system, messages, tools, max_tokens, task=None):
                if self.fail:
                    raise PermanentError("down")
                return Response(text="ok", input_tokens=1, output_tokens=1)

            def extend(self, messages, response, results):
                pass

        run_dir = self.root / "run"
        run(run_dir, self.config, self.suite, limit=1, workers=1, providers={"mock": Flaky(True)})
        self.assertEqual(latest_records(run_dir)[0]["status"], "api_error")
        run(run_dir, self.config, self.suite, limit=1, workers=1, retry_failed=True, providers={"mock": Flaky(False)})
        self.assertEqual(latest_records(run_dir)[0]["status"], "ok")


class ProviderTests(unittest.TestCase):
    model = {"api_model": "pinned-id"}
    tools = TOOLS

    def test_openai_request_and_tool_round_trip(self):
        sent = {}

        def post(url, headers, payload):
            sent.update(url=url, headers=headers, payload=payload)
            return {
                "model": "served",
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "function": {"name": "write_record", "arguments": '{"key": "a", "value": "b"}'}}]}}],
            }

        provider = OpenAIProvider(post)
        messages = provider.start("hello", "sys")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            response = provider.call(self.model, "sys", messages, self.tools, 50)
        self.assertEqual(sent["payload"]["max_completion_tokens"], 50)
        self.assertEqual(sent["headers"]["Authorization"], "Bearer k")
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(response.tool_calls[0].args, {"key": "a", "value": "b"})
        self.assertEqual((response.input_tokens, response.output_tokens), (3, 4))
        provider.extend(messages, response, [(response.tool_calls[0], "OK")])
        self.assertEqual(messages[-1], {"role": "tool", "tool_call_id": "c1", "content": "OK"})

    def test_openai_refusal(self):
        post = lambda u, h, p: {"choices": [{"finish_reason": "stop", "message": {"content": None, "refusal": "No."}}]}
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            self.assertTrue(OpenAIProvider(post).call(self.model, "", [], None, 5).refusal)

    def test_anthropic_request_and_tool_round_trip(self):
        sent = {}

        def post(url, headers, payload):
            sent.update(headers=headers, payload=payload)
            return {
                "model": "served", "stop_reason": "tool_use", "usage": {"input_tokens": 3, "output_tokens": 4},
                "content": [{"type": "text", "text": "ok"},
                            {"type": "tool_use", "id": "c1", "name": "write_record", "input": {"key": "a", "value": "b"}}],
            }

        provider = AnthropicProvider(post)
        messages = provider.start("hello", "sys")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            response = provider.call(self.model, "sys", messages, self.tools, 50)
        self.assertEqual(sent["payload"]["system"], "sys")
        self.assertEqual(sent["payload"]["max_tokens"], 50)
        self.assertEqual(sent["headers"]["x-api-key"], "k")
        self.assertIn("input_schema", sent["payload"]["tools"][0])
        self.assertEqual(response.text, "ok")
        provider.extend(messages, response, [(response.tool_calls[0], "OK")])
        self.assertEqual(messages[-1]["content"][0]["tool_use_id"], "c1")

    def test_gemini_keeps_native_parts_and_counts_thinking_tokens(self):
        sent = {}
        part = {"functionCall": {"name": "write_record", "args": {"key": "a", "value": "b"}}, "thoughtSignature": "sig"}

        def post(url, headers, payload):
            sent.update(url=url, headers=headers, payload=payload)
            return {
                "modelVersion": "served",
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "thoughtsTokenCount": 5},
                "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": "hidden", "thought": True}, part]}}],
            }

        provider = GeminiProvider(post)
        messages = provider.start("hello", "sys")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
            response = provider.call(self.model, "sys", messages, self.tools, 50)
        self.assertNotIn("secret", sent["url"])
        self.assertIn("pinned-id:generateContent", sent["url"])
        self.assertNotIn("parameters", sent["payload"]["tools"][0]["functionDeclarations"][0])
        self.assertEqual(response.text, "")
        self.assertEqual(response.output_tokens, 9)
        provider.extend(messages, response, [(response.tool_calls[0], "OK")])
        self.assertEqual(messages[-2]["parts"][1]["thoughtSignature"], "sig")
        self.assertEqual(messages[-1]["parts"][0]["functionResponse"]["name"], "write_record")

    def test_gemini_blocked_prompt_is_a_refusal(self):
        post = lambda u, h, p: {"promptFeedback": {"blockReason": "SAFETY"}}
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
            self.assertTrue(GeminiProvider(post).call(self.model, "", [], None, 5).refusal)

    def test_missing_key_is_a_permanent_error(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(PermanentError):
            OpenAIProvider(lambda *a: {}).call(self.model, "", [], None, 5)


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.categories = {f"t{i}": "reasoning" for i in range(60)}

    def test_labels(self):
        self.assertEqual(label_for(0.01, 0.2), "Improved")
        self.assertEqual(label_for(-0.2, -0.01), "Regressed")
        self.assertEqual(label_for(-0.1, 0.1), "No clear change")

    def test_large_consistent_gain_is_improved_and_reversal_is_regressed(self):
        low = {t: 0.2 + (i % 3) * 0.1 for i, t in enumerate(self.categories)}
        high = {t: v + 0.4 + (i % 2) * 0.1 for i, (t, v) in enumerate(low.items())}
        up = compare(high, low, self.categories, iterations=2000)["reasoning"]
        down = compare(low, high, self.categories, iterations=2000)["reasoning"]
        self.assertEqual(up["label"], "Improved")
        self.assertEqual(down["label"], "Regressed")
        self.assertLess(up["low"], up["difference"])

    def test_identical_models_show_no_clear_change(self):
        values = {t: (i % 2) * 1.0 for i, t in enumerate(self.categories)}
        self.assertEqual(compare(values, dict(values), self.categories, iterations=500)["reasoning"]["label"], "No clear change")

    def test_noise_is_not_labeled_as_a_change(self):
        new = {t: float(i % 2) for i, t in enumerate(self.categories)}
        old = {t: float((i + 1) % 2) for i, t in enumerate(self.categories)}
        self.assertEqual(compare(new, old, self.categories, iterations=2000)["reasoning"]["label"], "No clear change")

    def test_correction_widens_the_interval(self):
        new = {t: float(i % 2) for i, t in enumerate(self.categories)}
        old = {t: 0.5 for t in self.categories}
        one = compare(new, old, self.categories, 1, iterations=4000)["reasoning"]
        many = compare(new, old, self.categories, 8, iterations=4000)["reasoning"]
        self.assertGreater(many["corrected_high"] - many["corrected_low"], one["high"] - one["low"])

    def test_overall_weights_categories_equally(self):
        categories = {"a1": "reasoning", "a2": "reasoning", "a3": "reasoning", "b1": "writing"}
        for name in ("coding", "extraction", "document", "tool_use"):
            categories[name] = name
        means = {t: 0.0 for t in categories}
        means["b1"] = 1.0
        result = compare(means, {t: 0.0 for t in categories}, categories, iterations=50)
        self.assertAlmostEqual(result["overall"]["difference"], 1 / 6)
        self.assertAlmostEqual(result["overall_excluding_writing"]["difference"], 0.0)


class GradingTests(unittest.TestCase):
    def test_identifiers_are_removed(self):
        self.assertEqual(anonymize("As Claude, GPT-5 and Gemini agree"), "As [MODEL], [MODEL] and [MODEL] agree")

    def _graded_run(self, folder):
        root = Path(folder)
        suite = build_suite(root / "suite", 2)
        run_dir = root / "run"
        run(run_dir, {"max_output_tokens": 10, "models": demo_models()[:1]}, suite, workers=1)
        score_run(run_dir, suite)
        count = export_packet(run_dir, suite, root / "grading")
        self.assertGreater(count, 0)
        return root, run_dir

    def _write(self, source, target, scores):
        import csv
        with open(source, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        with open(target, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow({**row, "score": scores(row["blind_id"])})

    def test_packet_hides_the_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root, _ = self._graded_run(folder)
            self.assertNotIn("demo-a", (root / "grading" / "packet.csv").read_text(encoding="utf-8-sig"))
            self.assertIn("demo-a", (root / "grading" / "key.json").read_text(encoding="utf-8"))

    def test_import_scales_scores_and_reports_agreement(self):
        with tempfile.TemporaryDirectory() as folder:
            root, run_dir = self._graded_run(folder)
            packet = root / "grading" / "packet.csv"
            self._write(packet, root / "a.csv", lambda b: 5)
            self._write(packet, root / "b.csv", lambda b: 4)
            summary = import_grades(run_dir, root / "grading" / "key.json", root / "a.csv", root / "b.csv")
            self.assertEqual(summary["exact_agreement"], 0)
            self.assertEqual(summary["within_one_point"], 1)
            self.assertAlmostEqual(read_jsonl(run_dir / "human_scores.jsonl")[0]["score"], 0.875)

    def test_large_disagreements_need_adjudication(self):
        with tempfile.TemporaryDirectory() as folder:
            root, run_dir = self._graded_run(folder)
            packet = root / "grading" / "packet.csv"
            self._write(packet, root / "a.csv", lambda b: 5)
            self._write(packet, root / "b.csv", lambda b: 1)
            with self.assertRaises(SystemExit):
                import_grades(run_dir, root / "grading" / "key.json", root / "a.csv", root / "b.csv")
            self._write(packet, root / "c.csv", lambda b: 3)
            import_grades(run_dir, root / "grading" / "key.json", root / "a.csv", root / "b.csv", root / "c.csv")
            self.assertEqual(read_jsonl(run_dir / "human_scores.jsonl")[0]["score"], 0.5)

    def test_out_of_range_grades_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, run_dir = self._graded_run(folder)
            packet = root / "grading" / "packet.csv"
            self._write(packet, root / "a.csv", lambda b: 9)
            self._write(packet, root / "b.csv", lambda b: 4)
            with self.assertRaises(SystemExit):
                import_grades(run_dir, root / "grading" / "key.json", root / "a.csv", root / "b.csv")


class ConfigTests(unittest.TestCase):
    def test_template_placeholders_are_rejected(self):
        config = json.loads((ROOT / "config" / "models.template.json").read_text(encoding="utf-8"))
        self.assertEqual(len(check_config(config)), len(config["models"]))

    def test_paid_models_need_prices(self):
        model = {"id": "x", "provider": "openai", "released": "2026-01-01", "price_input_per_mtok": None, "price_output_per_mtok": 1}
        self.assertIn("prices", check_config({"models": [model]})[0])


class NewReleaseTests(unittest.TestCase):
    def test_new_release_run_compares_with_a_predecessor_from_an_earlier_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            suite = build_suite(root / "suite", 4)
            previous, current = demo_models()[:2]
            self.assertEqual(current["predecessor"], previous["id"])
            run(root / "results" / "first", {"max_output_tokens": 10, "models": [previous]}, suite, workers=1)
            score_run(root / "results" / "first", suite, allow_code_exec=True)

            second = {"max_output_tokens": 10, "models": [current]}
            self.assertTrue(check_config(second))
            earlier = earlier_model_ids(root / "results" / "second")
            self.assertEqual(check_config(second, earlier), [])

            run(root / "results" / "second", second, suite, workers=1)
            score_run(root / "results" / "second", suite, allow_code_exec=True)
            results = Results(root / "results", suite, iterations=100)
            self.assertEqual(results.comparison_targets(current["id"])[0], ("predecessor", previous["id"]))
            self.assertIn("reasoning", results.comparisons(current["id"])[0]["metrics"])

    def test_runs_of_a_different_suite_version_are_not_mixed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            suite = build_suite(root / "suite", 4)
            run(root / "results" / "first", {"max_output_tokens": 10, "models": demo_models()[:1]}, suite, workers=1)
            changed = build_suite(root / "other", 8)
            self.assertEqual(Results(root / "results", changed, iterations=100).models, {})


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.out = Path(cls.folder.name) / "demo"
        cls.summary = run_demo(cls.out, per_category=8)
        cls.suite = load_suite(cls.out / "suite")

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_all_runs_complete_and_every_output_gets_a_score(self):
        self.assertEqual(self.summary["run"]["remaining"], 0)
        self.assertNotIn("needs_code_execution", self.summary["scoring"])
        self.assertGreater(self.summary["grading"]["graded"], 0)

    def test_public_export_contains_no_private_task_content(self):
        private = [t for t in self.suite.tasks if t.visibility == "private"]
        self.assertTrue(private)
        exported = "\n".join(
            p.read_text(encoding="utf-8") for p in (self.out / "public").rglob("*") if p.is_file()
        )
        for task in private:
            self.assertNotIn(f'"{task.id}"', exported)
            self.assertNotIn(task.prompt, exported)
        self.assertIn(self.suite.private_hash, exported)

    def test_site_data_contains_no_private_task_content(self):
        site = (self.out / "site" / "index.html").read_text(encoding="utf-8")
        for task in self.suite.tasks:
            if task.visibility == "private":
                self.assertNotIn(task.prompt, site)
        self.assertNotIn("/*__DATA__*/", site)

    def test_export_refuses_synthetic_results_by_default(self):
        with self.assertRaises(SystemExit):
            export_public(self.out / "results", self.suite, self.out / "blocked")

    def test_site_flags_synthetic_data_and_escapes_labels(self):
        results = Results(self.out / "results", self.suite, iterations=100)
        self.assertTrue(build_site_data(results)["synthetic"])
        model_id = next(iter(results.models))
        results.models[model_id] = {**results.models[model_id], "label": "</script><script>alert(1)</script>"}
        page = build_site(results, self.out / "escaped") .read_text(encoding="utf-8")
        self.assertNotIn("</script><script>alert(1)", page)

    def test_comparisons_cover_the_predecessor_and_both_rivals(self):
        results = Results(self.out / "results", self.suite, iterations=100)
        kinds = [kind for kind, _ in results.comparison_targets("demo-a-flagship-current")]
        self.assertEqual(kinds, ["predecessor", "rival", "rival"])

    def test_rivals_released_later_are_not_used(self):
        results = Results(self.out / "results", self.suite, iterations=100)
        rivals = [m for kind, m in results.comparison_targets("demo-a-flagship-previous") if kind == "rival"]
        self.assertTrue(all("previous" in m for m in rivals))

    def test_drift_check_finds_no_drift_between_identical_runs(self):
        run_dir = self.out / "results" / "baseline"
        drift = drift_check(run_dir, run_dir, iterations=200)
        self.assertTrue(drift)
        self.assertTrue(all(v["difference"] == 0 for v in drift.values()))
        self.assertTrue(all(v["label"] == "No clear change" for v in drift.values()))


if __name__ == "__main__":
    unittest.main()
