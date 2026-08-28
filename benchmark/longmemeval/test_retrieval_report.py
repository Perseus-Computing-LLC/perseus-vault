import hashlib
import json
from pathlib import Path


REPORT = Path(__file__).with_name("report-currentmain-2026-08-16.json")
EXPECTED_SHA256 = "f1b63198a24b953b629c2bcfe347d67f950dc1a110b31884eed6527b533f005a"
EXPECTED_SIGNATURE = "cb94b4f89dc0da830dc628daa7aa58a70ca257da946b56f37db6c24622cdcc09"


def test_current_main_retrieval_report_is_the_committed_hash_bound_refresh():
    raw = REPORT.read_bytes()
    report = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256
    assert report["signature_sha256"] == EXPECTED_SIGNATURE
    assert report["offline"] is True
    assert report["n_instances"] == 500
    assert report["n_sessions_ingested"] == 23867
    assert report["metrics"]["hybrid"] == {
        "recall@1": 0.832,
        "recall@3": 0.966,
        "recall@5": 0.988,
        "recall@10": 0.998,
        "mrr": 0.8949,
    }


def test_current_main_retrieval_report_has_no_raw_payload_fields():
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    serialized = json.dumps(report, sort_keys=True).lower()
    for marker in ("\"body\"", "\"response\"", "\"credential\"", "\"secret\"", "\"password\"", "\"authorization\"", "\"api_key\""):
        assert marker not in serialized
    assert len(report["per_question"]) == 500
    assert {row["question_type"] for row in report["per_question"]} == {
        "knowledge-update",
        "multi-session",
        "single-session-assistant",
        "single-session-preference",
        "single-session-user",
        "temporal-reasoning",
    }
