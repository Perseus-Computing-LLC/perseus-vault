"""Tests for the opt-in answer-facing context assembly arm."""
import importlib.util
import inspect
import pathlib
import sys
import unittest


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import context_assembly as assembly  # noqa: E402
from context_assembly import assemble_ranked_snippets  # noqa: E402


def load_qa():
    spec = importlib.util.spec_from_file_location("longmemeval_qa_assembly", HERE / "qa.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load qa.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeServer:
    def __init__(self, ranked):
        self.ranked = ranked
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        if name == "perseus_vault_recall":
            return {"items": [{"key": sid} for sid in self.ranked]}
        if name == "perseus_vault_journal":
            return {"id": "jrn-test"}
        if name == "perseus_vault_remember":
            return {"ok": True, "serveable": True, "proposed": False}
        return {}


class RankedSnippetAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.inst = {
            "question": "What did I decide about the blue bicycle?",
            "haystack_session_ids": ["s1", "s2", "s3"],
            "haystack_dates": [
                "2023/01/01 (Sun) 00:00",
                "2023/01/02 (Mon) 00:00",
                "2023/01/03 (Tue) 00:00",
            ],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "We discussed unrelated travel plans."},
                    {"role": "assistant", "content": "The itinerary is saved."},
                    {"role": "user", "content": "I decided to buy the blue bicycle."},
                    {"role": "assistant", "content": "The blue bicycle decision is recorded."},
                ],
                [
                    {"role": "user", "content": "The red bicycle was too expensive."},
                    {"role": "assistant", "content": "You did not choose the red bicycle."},
                ],
                [
                    {"role": "user", "content": "A blue bicycle was mentioned in passing."},
                    {"role": "assistant", "content": "Noted."},
                ],
            ],
        }

    def test_prefers_query_bearing_pair_within_ranked_session(self):
        context, selected, telemetry = assemble_ranked_snippets(
            self.inst, ["s1", "s2", "s3"], budget_tokens=1000, max_windows_per_session=1
        )
        self.assertEqual(selected, ["s1", "s2", "s3"])
        self.assertIn("I decided to buy the blue bicycle", context)
        self.assertNotIn("unrelated travel plans", context)
        self.assertEqual(telemetry["selected_windows"], 3)

    def test_respects_budget_and_session_window_cap(self):
        context, selected, telemetry = assemble_ranked_snippets(
            self.inst, ["s1", "s2", "s3"], budget_tokens=12, max_windows_per_session=1
        )
        self.assertLessEqual(telemetry["estimated_tokens"], 12)
        self.assertLessEqual(telemetry["selected_windows"], 3)
        self.assertEqual(len(selected), len(set(selected)))
        self.assertTrue(context)

    def test_rendered_context_including_headers_and_guidance_respects_budget(self):
        context, _selected, _telemetry = assemble_ranked_snippets(
            self.inst,
            ["s1", "s2", "s3"],
            budget_tokens=100,
            max_windows_per_session=2,
            guidance="evidence-structured",
        )
        self.assertLessEqual((len(context) + 3) // 4, 100)

    def test_unknown_and_duplicate_ranked_ids_are_ignored(self):
        _context, selected, telemetry = assemble_ranked_snippets(
            self.inst, ["missing", "s2", "s2", "s1"], budget_tokens=1000
        )
        self.assertEqual(selected, ["s2", "s1"])
        self.assertEqual(telemetry["candidate_sessions"], 2)

    def test_qa_wires_ranked_snippets_as_an_opt_in_arm(self):
        qa = load_qa()
        context, chosen = qa.build_context(
            "perseus-vault",
            self.inst,
            _FakeServer(["s1", "s2", "s3"]),
            "q",
            10,
            context_assembly="ranked-snippets",
            assembly_k=20,
            context_budget=1000,
            assembly_windows=1,
        )
        self.assertEqual(chosen, ["s1", "s2", "s3"])
        self.assertIn("blue bicycle", context)

    def test_default_qa_path_remains_full_session_context(self):
        qa = load_qa()
        context, chosen = qa.build_context(
            "perseus-vault", self.inst, _FakeServer(["s1", "s2", "s3"]), "q", 2
        )
        self.assertEqual(chosen, ["s1", "s2"])
        self.assertIn("unrelated travel plans", context)
        self.assertIn("blue bicycle decision is recorded", context)

    def test_qa_wires_preference_guidance_as_a_separate_arm(self):
        qa = load_qa()
        self.inst["question_type"] = "single-session-preference"
        context, chosen = qa.build_context(
            "perseus-vault",
            self.inst,
            _FakeServer(["s1", "s2", "s3"]),
            "q",
            10,
            context_assembly="ranked-snippets",
            context_guidance="preference",
            assembly_k=20,
            context_budget=1000,
            assembly_windows=1,
        )
        self.assertEqual(chosen, ["s1", "s2", "s3"])
        self.assertIn("Preference evidence guide", context)

    def test_preference_guidance_is_explicitly_opt_in(self):
        self.inst["question_type"] = "single-session-preference"
        plain, plain_selected, plain_telemetry = assemble_ranked_snippets(
            self.inst, ["s1", "s2", "s3"], guidance="none"
        )
        guided, guided_selected, guided_telemetry = assemble_ranked_snippets(
            self.inst, ["s1", "s2", "s3"], guidance="preference"
        )
        self.assertNotIn("Preference evidence guide", plain)
        self.assertIn("Preference evidence guide", guided)
        self.assertIn("direct user statements", guided)
        self.assertEqual(plain_selected, guided_selected)
        self.assertLessEqual(guided_telemetry["estimated_tokens"], 1000)
        self.assertEqual(plain_telemetry["guidance_applied"], "none")
        self.assertEqual(guided_telemetry["guidance_applied"], "preference")

    def test_structured_preference_arm_labels_user_evidence(self):
        context, selected, telemetry = assemble_ranked_snippets(
            self.inst,
            ["s1", "s2", "s3"],
            budget_tokens=1000,
            max_windows_per_session=2,
            guidance="preference-structured",
        )
        self.assertEqual(selected, ["s1", "s2", "s3"])
        self.assertIn("User-stated evidence", context)
        self.assertIn("Assistant context (not user evidence)", context)
        self.assertIn("I decided to buy the blue bicycle", context)
        self.assertEqual(telemetry["guidance_applied"], "preference-structured")
        self.assertGreaterEqual(telemetry["preference_evidence_windows"], 1)

    def test_structured_preference_retains_existing_guidance_contract(self):
        context, _selected, _telemetry = assemble_ranked_snippets(
            self.inst,
            ["s1", "s2", "s3"],
            budget_tokens=2000,
            max_windows_per_session=2,
            guidance="preference-structured",
        )
        self.assertIn("direct user statements", context)
        self.assertIn("general preference", context)
        self.assertIn("assistant suggestions or generic examples", context)

    def test_structured_prefers_durable_preference_over_generic_planning(self):
        inst = {
            "question": "What should I do about my project and emails?",
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[
                {"role": "user", "content": "I have a project due and need to answer emails."},
                {"role": "assistant", "content": "Set aside time for the project."},
                {"role": "user", "content": "I prefer winding down by 9:30 pm and enjoyed meditation."},
                {"role": "assistant", "content": "A calm evening routine may help."},
            ]],
        }
        context, _selected, telemetry = assemble_ranked_snippets(
            inst, ["s1"], budget_tokens=1000, max_windows_per_session=2,
            guidance="preference-structured",
        )
        self.assertIn("9:30 pm", context)
        self.assertEqual(telemetry["preference_evidence_windows"], 1)

    def test_evidence_structured_arm_adds_fact_bearing_user_evidence(self):
        inst = {
            "question": "How many workshops did I attend?",
            "haystack_session_ids": ["s1"],
            "haystack_dates": ["2023/05/01 (Mon) 00:00"],
            "haystack_sessions": [[
                {"role": "user", "content": "Can you suggest some workshop ideas?"},
                {"role": "assistant", "content": "Here are several workshop ideas."},
                {"role": "user", "content": "I attended 3 workshops on April 10 and April 12."},
                {"role": "assistant", "content": "That is useful event history."},
            ]],
        }
        context, selected, telemetry = assemble_ranked_snippets(
            inst, ["s1"], budget_tokens=1000, max_windows_per_session=1,
            guidance="evidence-structured",
        )
        self.assertEqual(selected, ["s1"])
        self.assertIn("Cross-session evidence guide", context)
        self.assertIn("3 workshops", context)
        self.assertIn("User-stated evidence", context)
        self.assertEqual(telemetry["guidance_applied"], "evidence-structured")
        self.assertGreaterEqual(telemetry["evidence_windows"], 1)

    def test_qa_wires_structured_preference_as_a_distinct_arm(self):
        qa = load_qa()
        self.inst["question_type"] = "single-session-preference"
        context, chosen = qa.build_context(
            "perseus-vault",
            self.inst,
            _FakeServer(["s1", "s2", "s3"]),
            "q",
            10,
            context_assembly="ranked-snippets",
            context_guidance="preference-structured",
            assembly_k=20,
            context_budget=1000,
            assembly_windows=2,
        )
        self.assertEqual(chosen, ["s1", "s2", "s3"])
        self.assertIn("Structured preference evidence guide", context)
        self.assertIn("User-stated evidence", context)

    def test_qa_wires_evidence_structured_as_a_distinct_arm(self):
        qa = load_qa()
        self.inst["question_type"] = "temporal-reasoning"
        context, chosen = qa.build_context(
            "perseus-vault",
            self.inst,
            _FakeServer(["s1", "s2", "s3"]),
            "q",
            10,
            context_assembly="ranked-snippets",
            context_guidance="evidence-structured",
            assembly_k=20,
            context_budget=1000,
            assembly_windows=2,
        )
        self.assertEqual(chosen, ["s1", "s2", "s3"])
        self.assertIn("Cross-session evidence guide", context)
        self.assertIn("User-stated evidence", context)


    def test_evidence_ledger_selector_has_no_gold_field_surface(self):
        source = inspect.getsource(assembly.assemble_evidence_ledger)
        for forbidden in ("answer", "gold", "update_gold"):
            self.assertNotIn(forbidden, source.lower())

        context, selected, telemetry = assembly.assemble_evidence_ledger(
            "How many workshops did I attend?",
            ["s1"],
            [[
                {"role": "user", "content": "I attended 3 workshops in April."},
                {"role": "assistant", "content": "That is useful event history."},
            ]],
            ["2023/04/20 (Thu) 00:00"],
            ["s1"],
            budget_tokens=500,
        )
        self.assertEqual(selected, ["s1"])
        self.assertIn("3 workshops", context)
        self.assertEqual(telemetry["forbidden_fields_read"], 0)

    def test_evidence_ledger_is_deterministic_compact_and_provenance_labeled(self):
        args = (
            "What did I decide about the bicycle?",
            ["s1", "s2", "s3"],
            [
                [
                    {"role": "user", "content": "I decided to buy the blue bicycle."},
                    {"role": "assistant", "content": "The bicycle decision is recorded."},
                ],
                [
                    {"role": "user", "content": "I decided to buy the blue bicycle."},
                    {"role": "assistant", "content": "The bicycle decision is recorded."},
                ],
                [
                    {"role": "user", "content": "I bought 2 bicycles, not 3."},
                    {"role": "assistant", "content": "Keep the receipts."},
                ],
            ],
            [
                "2023/01/01 (Sun) 00:00",
                "2023/01/02 (Mon) 00:00",
                "2023/01/03 (Tue) 00:00",
            ],
            ["s2", "s1", "s2", "missing", "s3"],
        )
        first = assembly.assemble_evidence_ledger(*args, budget_tokens=600)
        second = assembly.assemble_evidence_ledger(*args, budget_tokens=600)
        self.assertEqual(first, second)
        context, selected, telemetry = first
        self.assertEqual(selected, ["s2", "s1", "s3"])
        self.assertLessEqual(telemetry["estimated_tokens"], 600)
        self.assertIn("session=s2", context)
        self.assertIn("date=2023/01/02", context)
        self.assertIn("turn=1", context)
        self.assertIn("[user]", context)
        self.assertIn("[assistant-context]", context)
        self.assertEqual(context.count("I decided to buy the blue bicycle."), 1)
        self.assertIn("sources=s2", context)
        self.assertGreaterEqual(telemetry["deduplicated_items"], 1)

    def test_evidence_ledger_keeps_distinct_numeric_versions(self):
        context, _selected, _telemetry = assembly.assemble_evidence_ledger(
            "How many bicycles did I buy?",
            ["s1"],
            [[
                {"role": "user", "content": "I bought 2 bicycles."},
                {"role": "user", "content": "Actually, I bought 3 bicycles."},
            ]],
            ["2023/01/01 (Sun) 00:00"],
            ["s1"],
            budget_tokens=500,
        )
        self.assertIn("bought 2 bicycles", context)
        self.assertIn("bought 3 bicycles", context)

    def test_evidence_ledger_rejects_budget_above_hard_cap(self):
        with self.assertRaises(ValueError):
            assembly.assemble_evidence_ledger(
                "q", ["s1"], [[{"role": "user", "content": "fact"}]], [None], ["s1"],
                budget_tokens=16001,
            )

    def test_evidence_ledger_qa_path_does_not_read_gold_only_fields(self):
        qa = load_qa()

        class GoldRejectingDict(dict):
            def get(self, key, default=None):
                if key in {"answer", "answer_session_ids", "update_gold"}:
                    raise AssertionError(f"gold-only field read: {key}")
                return super().get(key, default)

        inst = GoldRejectingDict(self.inst)
        context, chosen = qa.build_context(
            "perseus-vault", inst, _FakeServer(["s1", "s2", "s3"]), "q", 10,
            context_assembly="evidence-ledger", assembly_k=20,
            ledger_budget=300,
        )
        self.assertEqual(chosen, ["s1", "s2", "s3"])
        self.assertIn("Evidence ledger", context)

    def test_qa_wires_evidence_ledger_as_opt_in_arm(self):
        qa = load_qa()
        server = _FakeServer(["s1", "s2", "s3"])
        context, chosen = qa.build_context(
            "perseus-vault",
            self.inst,
            server,
            "q",
            10,
            context_assembly="evidence-ledger",
            assembly_k=20,
            context_budget=1000,
            assembly_windows=2,
            ledger_budget=300,
        )
        self.assertEqual(chosen, ["s1", "s2", "s3"])
        recall = [args for name, args in server.calls if name == "perseus_vault_recall"]
        self.assertEqual(recall[-1]["limit"], 20)
        self.assertIn("Evidence ledger", context)
        self.assertLessEqual(len(context) // 4, 300)


if __name__ == "__main__":
    unittest.main()
