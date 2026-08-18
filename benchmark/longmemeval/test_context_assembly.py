"""Tests for the opt-in answer-facing context assembly arm."""
import importlib.util
import pathlib
import sys
import unittest


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
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

    def call(self, name, args):
        if name == "perseus_vault_recall":
            return {"items": [{"key": sid} for sid in self.ranked]}
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


if __name__ == "__main__":
    unittest.main()
