"""Protocol tests for the LongMemEval official-CoT lane."""
import hashlib
import hmac
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from benchmark import admission_fixture as ADMISSION


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
            out_path = root / "nested" / "reports" / "report.json"
            hypotheses_dir = root / "nested" / "hypotheses"
            journal_path = root / "nested" / "journal" / "progress.jsonl"
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
                "--out", str(out_path), "--outdir", str(hypotheses_dir),
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
            hyp_path = hypotheses_dir / "hypotheses-stateless-gpt-4o-2024-08-06-official-cot.jsonl"
            hypothesis = json.loads(hyp_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(hypothesis["hypothesis"], full_response)

    def test_vault_ingest_uses_admitted_fixture_contract(self):
        instance = {
            "question": "What did I decide?",
            "haystack_session_ids": ["s1"],
            "haystack_dates": ["2024/01/01 (Mon) 00:00"],
            "haystack_sessions": [[{"role": "user", "content": "I decided."}]],
        }
        calls = []

        class FakeServer:
            def call(self, name, args):
                calls.append((name, args))
                if name == "perseus_vault_recall":
                    return {"items": [{"key": "s1"}]}
                return {"ok": True}

        def fake_admitted(client, category, key, body_json, **kwargs):
            calls.append(("admitted_remember", {
                "category": category,
                "key": key,
                "body_json": body_json,
                **kwargs,
            }))
            return {"ok": True, "serveable": True}

        with mock.patch.object(QA, "admitted_remember", side_effect=fake_admitted):
            context, chosen = QA.build_context(
                "perseus-vault", instance, FakeServer(), "q1", 10
            )

        self.assertEqual(chosen, ["s1"])
        self.assertIn("I decided", context)
        admitted = [args for name, args in calls if name == "admitted_remember"]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(admitted[0]["category"], "q1")
        self.assertEqual(admitted[0]["key"], "s1")

    def test_admitted_remember_emits_hash_bound_source_and_valid_time(self):
        calls = []

        class FakeClient:
            def call(self, name, args):
                calls.append((name, args))
                if name == "perseus_vault_journal":
                    return {"id": "jrn-bound"}
                if name == "perseus_vault_remember":
                    return {"ok": True, "serveable": True, "proposed": False}
                return {"ok": True}

        body_json = json.dumps({"note": "café"})
        ADMISSION.admitted_remember(
            FakeClient(), "unicode", "s1", body_json, valid_from_unix_ms=1234
        )
        journal = [args for name, args in calls if name == "perseus_vault_journal"][0]
        remember = [args for name, args in calls if name == "perseus_vault_remember"][0]
        canonical_body = ADMISSION.stable_json(json.loads(body_json))
        digest = hashlib.sha256(canonical_body.encode()).hexdigest()
        evaluated = journal["evaluated"]
        attestation_payload = ADMISSION.stable_json(
            {**evaluated, "requesting_agent_id": ADMISSION.AGENT}
        )
        expected_attestation = hmac.new(
            ADMISSION.HMAC_KEY.encode(), attestation_payload.encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(evaluated["record_digest"], digest)
        self.assertEqual(journal["source_attestation"], expected_attestation)
        self.assertEqual(remember["admission"]["source_event_id"], "jrn-bound")
        self.assertEqual(remember["admission"]["record_digest"], digest)
        self.assertEqual(remember["valid_from_unix_ms"], 1234)

    def test_admitted_remember_rejects_proposed_or_unserveable_result(self):
        for result in (
            {"ok": True, "serveable": True, "proposed": True},
            {"ok": True, "serveable": False, "proposed": False},
        ):
            class FakeClient:
                def call(self, name, args):
                    if name == "perseus_vault_journal":
                        return {"id": "jrn-reject"}
                    if name == "perseus_vault_remember":
                        return result
                    return {"ok": True}

            with self.assertRaises(RuntimeError):
                ADMISSION.admitted_remember(
                    FakeClient(), "category", "key", json.dumps({"note": "x"})
                )

    def test_ku_shared_key_path_passes_valid_time_to_admission(self):
        instance = {
            "question": "What changed?",
            "answer_session_ids": ["s1", "s2"],
            "haystack_session_ids": ["s1", "s2"],
            "haystack_dates": [
                "2024/01/01 (Mon) 00:00",
                "2024/01/02 (Tue) 00:00",
            ],
            "haystack_sessions": [
                [{"role": "user", "content": "The value was one."}],
                [{"role": "user", "content": "The value is two."}],
            ],
        }
        admitted = []

        class FakeServer:
            def call(self, name, args):
                if name == "perseus_vault_recall":
                    return {"items": [{"key": QA.SHARED_FACT_KEY}]}
                return {"ok": True}

        def fake_admitted(client, category, key, body_json, **kwargs):
            admitted.append({"category": category, "key": key, **kwargs})
            return {"ok": True, "serveable": True, "proposed": False}

        with mock.patch.object(QA, "admitted_remember", side_effect=fake_admitted):
            QA.build_context(
                "perseus-vault", instance, FakeServer(), "q1", 10,
                ku_shared=True,
            )
        self.assertEqual(len(admitted), 2)
        self.assertEqual({item["key"] for item in admitted}, {QA.SHARED_FACT_KEY})
        self.assertEqual(
            sorted(item["valid_from_unix_ms"] for item in admitted),
            sorted([QA._date_ms(date) for date in instance["haystack_dates"]]),
        )

    def test_admission_fixture_matches_rust_json_for_unicode_body(self):
        calls = []

        class FakeClient:
            def call(self, name, args):
                calls.append((name, args))
                if name == "perseus_vault_journal":
                    return {"id": "jrn-unicode"}
                if name == "perseus_vault_remember":
                    return {"ok": True, "serveable": True, "proposed": False}
                return {"ok": True}

        ADMISSION.admitted_remember(
            FakeClient(), "unicode", "s1", json.dumps({"note": "café"})
        )
        remember = [args for name, args in calls if name == "perseus_vault_remember"][0]
        self.assertIn("café", remember["body_json"])
        self.assertNotIn("\\u00e9", remember["body_json"])

        with tempfile.TemporaryFile(mode="w+") as journal:
            QA.write_checkpoint(journal, {"question_id": "q1", "correct": True})
            journal.seek(0)
            self.assertEqual(json.loads(journal.readline()), {"question_id": "q1", "correct": True})


if __name__ == "__main__":
    unittest.main()
