import copy
import inspect
import unittest

from benchmark.longmemeval import qa, retrieval_diag
from benchmark.package.common import replay


def _seal(record):
    return {
        **record,
        "record_sha256": replay.sha256_text(replay.stable_json(record)),
    }


def _qa_record():
    return _seal({
        "question_id": "q1",
        "question_type": "single-session-preference",
        "system": "perseus-vault",
        "abstention": False,
        "correct": True,
        "error": None,
        "judge_raw": "yes",
        "ans_usage": None,
        "judge_usage": None,
        "hypothesis": "answer",
        "tokens_est": 10,
        "sessions": 1,
    })


def _preflight():
    identity = {"device": 1, "inode": 2, "ctime_ns": 3, "size": 4}
    return {
        "binary_sha256": "c" * 64,
        "binary_commit": "d" * 40,
        "binary_commit_sha256": replay.sha256_text("d" * 40),
        "database_fresh": True,
        "database_identity": identity,
        "database_id_sha256": replay.sha256_text(replay.stable_json(identity)),
        "response_schema": replay.RECALL_WIRE_SCHEMA_VERSION,
        "response_schema_sha256": replay.sha256_text(replay.RECALL_WIRE_SCHEMA_VERSION),
        "dataset_sha256": "a" * 64,
        "config_sha256": "b" * 64,
    }


class ResumeIntegrityTests(unittest.TestCase):
    def test_qa_resume_rejects_tampered_verdict_and_unknown_fields(self):
        instance = {"question_id": "q1", "question_type": "single-session-preference"}
        record = _qa_record()
        qa.validate_qa_resume_record(
            record,
            instance=instance,
            systems=("perseus-vault",),
            require_preflight=False,
        )

        tampered = copy.deepcopy(record)
        tampered["correct"] = False
        with self.assertRaises(ValueError):
            qa.validate_qa_resume_record(
                tampered,
                instance=instance,
                systems=("perseus-vault",),
                require_preflight=False,
            )

        unknown = copy.deepcopy(record)
        unknown["RAW-QUERY-SENTINEL"] = "must-not-cross"
        unknown.pop("record_sha256")
        unknown = _seal(unknown)
        with self.assertRaises(ValueError):
            qa.validate_qa_resume_record(
                unknown,
                instance=instance,
                systems=("perseus-vault",),
                require_preflight=False,
            )

    def test_qa_resume_rejects_incomplete_preflight_commitment(self):
        instance = {"question_id": "q1", "question_type": "single-session-preference"}
        record = _qa_record()
        record["preflight"] = {"database_fresh": True}
        record.pop("record_sha256")
        record = _seal(record)

        with self.assertRaises(ValueError):
            qa.validate_qa_resume_record(
                record,
                instance=instance,
                systems=("perseus-vault",),
                require_preflight=True,
            )

    def test_qa_resume_binds_the_database_path(self):
        source = inspect.getsource(qa.main)
        resume_start = source.index("for rec in lines[1:]")
        resume_end = source.index("journal_path.parent.mkdir", resume_start)
        resume_block = source[resume_start:resume_end]
        for binding in ('"binary": binary', '"db_path": db', '"repo_root": str(REPO)', '"dataset":', '"config":'):
            self.assertIn(binding, resume_block)

    def test_retrieval_resume_binds_gold_and_ranks_to_current_instance(self):
        instance = {
            "question_id": "q1",
            "question": "Which fact?",
            "question_type": "single-session",
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[{"role": "user", "content": "fact"}]],
            "haystack_dates": ["2023/04/20 (Thu) 00:00"],
            "answer_session_ids": ["s1"],
        }
        envelope, snapshot = retrieval_diag._make_replay_artifact(
            instance,
            "q1",
            [{"key": "s1", "body_json": {"note": "fact"}}],
            1,
            split="s",
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
            preflight=_preflight(),
        )
        record = _seal({
            "question_id": "q1",
            "question_type": "single-session",
            "gold": ["s1"],
            "update_gold": None,
            "ranks": {"s1": 1},
            "wire_status": "complete",
            "n_haystack_sessions": 1,
            "retrieval_replay": envelope,
            "retrieval_snapshot": snapshot,
            "preflight": _preflight(),
        })
        retrieval_diag.validate_retrieval_resume_record(
            record,
            instance=instance,
            depth=1,
            allow_synthetic=True,
        )

        forged = copy.deepcopy(record)
        forged["gold"] = ["forged"]
        forged.pop("record_sha256")
        forged = _seal(forged)
        with self.assertRaises(ValueError):
            retrieval_diag.validate_retrieval_resume_record(forged, instance=instance, depth=1)

        mismatched_preflight = copy.deepcopy(record)
        mismatched_preflight["preflight"]["config_sha256"] = "e" * 64
        mismatched_preflight.pop("record_sha256")
        mismatched_preflight = _seal(mismatched_preflight)
        with self.assertRaises(ValueError):
            retrieval_diag.validate_retrieval_resume_record(
                mismatched_preflight,
                instance=instance,
                depth=1,
                allow_synthetic=True,
            )


if __name__ == "__main__":
    unittest.main()
