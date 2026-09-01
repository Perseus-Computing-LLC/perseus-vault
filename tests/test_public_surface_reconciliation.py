"""Public benchmark copy stays aligned with the committed retrieval evidence."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_longmemeval_public_surfaces_use_the_offline_retrieval_lane():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claims = (ROOT / "CLAIMS-AUDIT.md").read_text(encoding="utf-8")
    deprecated = (ROOT / "benchmarks" / "LONG_MEM_EVAL.md").read_text(encoding="utf-8")

    for text in (readme, claims, deprecated):
        assert "session-level recall" in text.lower()
        assert "83.2%" in text
        assert "98.8%" in text
        assert "99.8%" in text
        assert "0.8949" in text
        assert "judge-free" in text.lower()
        assert "offline" in text.lower()

    assert "73.8%" not in readme
    assert "official harness" not in readme.lower()
    assert "end-to-end qa accuracy" in readme.lower()
    assert "content-hashed" in deprecated.lower()
    assert "signed" not in deprecated.lower()


def test_longmemeval_claims_name_the_committed_report():
    claims = (ROOT / "CLAIMS-AUDIT.md").read_text(encoding="utf-8")
    assert "report-currentmain-2026-08-16.json" in claims
    assert "23,867" in claims
    assert "500" in claims


def test_generated_benchmark_page_excludes_retired_qa_claims():
    page = (ROOT / "benchmarks-index.html").read_text(encoding="utf-8")

    assert "99.8%" in page
    for retired in ("73.8%", "80.9%", "81.4%", "79.0%", "official-CoT"):
        assert retired not in page


def test_evaluator_guide_matches_canonical_lean_status_and_ledger_runtime():
    guide = (ROOT / "docs" / "EVALUATOR_GUIDE.md").read_text(encoding="utf-8")

    assert "mkdir -p /tmp/verify_vault" in guide
    assert "`perseus_vault_workspace_status`" in guide
    assert "`perseus_vault_status`" not in guide
    assert "stdlib `http.server`" in guide
    assert "FastAPI" not in guide
