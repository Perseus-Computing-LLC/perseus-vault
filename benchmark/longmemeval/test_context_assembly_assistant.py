"""RED tests for the opt-in assistant-recall continuity projection."""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from context_assembly import assemble_assistant_recall_ledger  # noqa: E402


class _FakeServer:
    def __init__(self, ranked):
        self.ranked = ranked

    def call(self, name, args):
        if name == "perseus_vault_recall":
            return {"items": [{"key": sid} for sid in self.ranked]}
        if name == "perseus_vault_journal":
            return {"id": "jrn-test"}
        if name == "perseus_vault_remember":
            return {"ok": True, "serveable": True, "proposed": False}
        return {}


class AssistantRecallContinuityTests(unittest.TestCase):
    def test_retains_complete_answer_turn_and_provenance(self):
        inst = (
            "What was the seventh job in the list?",
            ["answer", "distractor"],
            [
                [
                    {"role": "user", "content": "Brainstorm work from home jobs for seniors."},
                    {"role": "assistant", "content": (
                        "1. Virtual customer service representative. "
                        "2. Telehealth professional. 3. Remote bookkeeper. "
                        "4. Virtual tutor. 5. Freelance writer. 6. Survey taker. "
                        "7. Transcriptionist. 8. Social media manager."
                    )},
                ],
                [
                    {"role": "user", "content": "Unrelated travel planning."},
                    {"role": "assistant", "content": "The itinerary is saved."},
                ],
            ],
            ["2023/01/01 (Sun) 00:00", "2023/01/02 (Mon) 00:00"],
            ["answer", "distractor"],
        )
        context, selected, telemetry = assemble_assistant_recall_ledger(
            *inst, budget_tokens=500
        )
        self.assertEqual(selected, ["answer", "distractor"])
        self.assertIn("7. Transcriptionist", context)
        self.assertIn("[assistant-context]", context)
        self.assertIn("session=answer", context)
        self.assertIn("turn=2", context)
        self.assertLessEqual(telemetry["estimated_tokens"], 500)
        self.assertEqual(telemetry["provider_calls"], 0)

    def test_preserves_turn_order_and_is_deterministic(self):
        args = (
            "What did I decide about the blue bicycle?",
            ["s1"],
            [[
                {"role": "user", "content": "We discussed a blue bicycle."},
                {"role": "assistant", "content": "I recorded the blue bicycle decision."},
                {"role": "user", "content": "Keep this decision."},
                {"role": "assistant", "content": "The decision remains saved."},
            ]],
            ["2023/01/01 (Sun) 00:00"],
            ["s1"],
        )
        first = assemble_assistant_recall_ledger(*args, budget_tokens=500)
        second = assemble_assistant_recall_ledger(*args, budget_tokens=500)
        self.assertEqual(first, second)
        context = first[0]
        self.assertLess(context.index("turn=1"), context.index("turn=2"))
        self.assertLess(context.index("turn=2"), context.index("turn=3"))
        self.assertLess(context.index("turn=3"), context.index("turn=4"))

    def test_tiny_budget_never_exceeds_the_declared_bound(self):
        context, _selected, telemetry = assemble_assistant_recall_ledger(
            "q",
            ["s1"],
            [[
                {"role": "user", "content": "a long user turn"},
                {"role": "assistant", "content": "a long assistant turn"},
            ]],
            [None],
            ["s1"],
            budget_tokens=1,
        )
        self.assertLessEqual((len(context) + 3) // 4, 1)
        self.assertLessEqual(telemetry["estimated_tokens"], 1)

    def test_qa_wires_assistant_recall_as_an_explicit_opt_in_arm(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("longmemeval_qa_assistant", HERE / "qa.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load qa.py")
        qa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qa)
        inst = {
            "question": "What was the seventh job in the list?",
            "question_type": "single-session-assistant",
            "haystack_session_ids": ["answer"],
            "haystack_dates": ["2023/01/01 (Sun) 00:00"],
            "haystack_sessions": [[
                {"role": "user", "content": "Brainstorm work from home jobs for seniors."},
                {"role": "assistant", "content": "7. Transcriptionist."},
            ]],
        }
        context, chosen = qa.build_context(
            "perseus-vault", inst, _FakeServer(["answer"]), "q", 10,
            context_assembly="assistant-recall", assembly_k=20,
            ledger_budget=500,
        )
        self.assertEqual(chosen, ["answer"])
        self.assertIn("7. Transcriptionist", context)
        self.assertIn("Assistant recall ledger", context)


if __name__ == "__main__":
    unittest.main()
