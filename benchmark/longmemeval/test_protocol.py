"""Protocol tests for the LongMemEval official-CoT lane."""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("longmemeval_qa", HERE / "qa.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load qa.py test module")
QA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QA)


class OfficialCoTProtocolTests(unittest.TestCase):
    def test_cot_template_is_verbatim_official_positional_template(self):
        expected = (
            "I will give you several history chats between you and a user. Please answer "
            "the question based on the relevant chat history. Answer the question step by step: "
            "first extract all the relevant information, and then reason over the information to "
            "get the answer.\n\n\n"
            "History Chats:\n\n{}\n\n"
            "Current Date: {}\n"
            "Question: {}\n"
            "Answer (step by step):"
        )
        self.assertEqual(QA.ANSWER_PROMPT_COT, expected)

    def test_cot_prompt_matches_official_generation_template(self):
        rendered = QA.ANSWER_PROMPT_COT.format(
            "CTX",
            "2024-01-02",
            "QUESTION",
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

    def test_cot_hypothesis_artifact_is_separate_from_plain_lane(self):
        cot_name = QA.hypothesis_artifact_name(
            "perseus-vault", "gpt-4o-2024-08-06", cot=True
        )
        plain_name = QA.hypothesis_artifact_name(
            "perseus-vault", "gpt-4o-2024-08-06", cot=False
        )
        self.assertEqual(
            cot_name,
            "hypotheses-perseus-vault-gpt-4o-2024-08-06-official-cot.jsonl",
        )
        self.assertEqual(
            plain_name,
            "hypotheses-perseus-vault-gpt-4o-2024-08-06-plain.jsonl",
        )
        self.assertNotEqual(cot_name, plain_name)

    def test_main_sends_complete_cot_response_to_judge_and_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            data_path = root / "fixture.json"
            out_path = root / "report.json"
            journal_path = root / "progress.jsonl"
            data_path.write_text(json.dumps([{
                "question_id": "q1",
                "question_type": "single-session-user",
                "question": "What is the answer?",
                "answer": "final answer",
                "question_date": "2024/01/02 (Tue) 00:00",
                "haystack_session_ids": ["s1"],
                "haystack_dates": ["2024/01/01 (Mon) 00:00"],
                "haystack_sessions": [[]],
            }]), encoding="utf-8")
            full_response = (
                "Relevant fact.\n\nReasoning over the fact.\n\n"
                "Answer: final answer."
            )
            responses = [
                (full_response, {"prompt_tokens": 10, "completion_tokens": 8}),
                ("yes", {"prompt_tokens": 20, "completion_tokens": 1}),
            ]
            seen_prompts = []

            def fake_call_llm(*_args, **kwargs):
                prompt = kwargs["prompt"] if "prompt" in kwargs else _args[3]
                seen_prompts.append(prompt)
                return responses.pop(0)

            argv = [
                "qa.py", "--data", str(data_path), "--systems", "stateless",
                "--cot", "--tpm", "0", "--max-retries", "1",
                "--out", str(out_path), "--outdir", str(root),
                "--journal", str(journal_path),
            ]
            with mock.patch.object(QA, "get_api_key", return_value="test-key"), \
                    mock.patch.object(QA, "call_llm", side_effect=fake_call_llm), \
                    mock.patch.object(
                        QA,
                        "extract_cot_answer",
                        side_effect=AssertionError("official-CoT tail extraction is forbidden"),
                    ), \
                    mock.patch.object(sys, "argv", argv):
                self.assertEqual(QA.main(), 0)

            self.assertEqual(len(seen_prompts), 2)
            self.assertIn(full_response, seen_prompts[1])
            report = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(report["hypothesis_mode"], "complete-response")
            hyp_path = root / "hypotheses-stateless-gpt-4o-2024-08-06-official-cot.jsonl"
            hypothesis = json.loads(hyp_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(hypothesis["hypothesis"], full_response)

    def test_checkpoint_writer_persists_json_record(self):
        with tempfile.TemporaryFile(mode="w+") as journal:
            QA.write_checkpoint(journal, {"question_id": "q1", "correct": True})
            journal.seek(0)
            self.assertEqual(json.loads(journal.readline()), {"question_id": "q1", "correct": True})


if __name__ == "__main__":
    unittest.main()
