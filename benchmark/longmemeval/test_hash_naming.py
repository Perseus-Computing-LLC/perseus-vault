"""QA report hash terminology stays canonical at the producer boundary."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QA = ROOT / "benchmark" / "longmemeval" / "qa.py"
CPST = ROOT / "benchmark" / "longmemeval" / "cpst.py"


def test_qa_producer_emits_content_hashes_and_cpst_reads_legacy_reports():
    qa = QA.read_text(encoding="utf-8")
    cpst = CPST.read_text(encoding="utf-8")

    assert "content-hashed" in qa
    assert '"content_hash_sha256": signature' in qa
    assert "signed report" not in qa.lower()
    assert 'report.get("content_hash_sha256")' in cpst
    assert 'report.get("signature_sha256")' in cpst
