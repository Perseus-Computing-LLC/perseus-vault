"""Protocol tests for the LongMemEval official-CoT lane."""
import importlib.util
import json
import pathlib
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("longmemeval_qa", HERE / "qa.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load qa.py test module")
QA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QA)


class OfficialCoTProtocolTests(unittest.TestCase):
    def test_cot_prompt_matches_official_generation_template(self):
        rendered = QA.ANSWER_PROMPT_COT.format(
            context="CTX",
            question_date="2024-01-02",
            question="QUESTION",
        )
        expected = (
            "I will give you several history chats between you and a user. Please answer "
            "the question based on the relevant chat history. Answer the question step by step: "
            "first extract all the relevant information, and then reason over the information to "
            "get the answer.\n\n\n"
            "History Chats:\n\nCTX\n\n"
            "Current Date: 2024-01-02\n"
            "Question: QUESTION\n"
            "Answer (step by step):"
        )
        self.assertEqual(rendered, expected)

    def test_cot_judge_hypothesis_preserves_complete_response(self):
        raw = "Relevant fact.\n\nReasoning over the fact.\n\nAnswer: final answer."
        self.assertEqual(QA.hypothesis_for_judge(raw, cot=True), raw)

    def test_checkpoint_writer_persists_json_record(self):
        with tempfile.TemporaryFile(mode="w+") as journal:
            QA.write_checkpoint(journal, {"question_id": "q1", "correct": True})
            journal.seek(0)
            self.assertEqual(json.loads(journal.readline()), {"question_id": "q1", "correct": True})


if __name__ == "__main__":
    unittest.main()
