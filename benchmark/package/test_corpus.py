from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from benchmark.package.common.corpus import (
    CERTIFICATION_SCHEMA_VERSION,
    CorpusContractError,
    certify_surfaces,
    materialize_git_tree,
    redact_tree,
    validate_certification_receipt,
    validate_redaction_receipt,
    validate_relative_path,
)


class CorpusCertificationTests(unittest.TestCase):
    def _clean_surfaces(self, root: Path) -> dict[str, object]:
        return {
            "source": root,
            "fixture": {"fixture_id": "synthetic-v1", "cases": ["case-a"]},
            "evidence": {"records": [{"id": "evidence-a", "digest": "a" * 64}]},
            "graph_identity": {"nodes": [{"id": "node-a", "label": "ordinary", "body": "fixture is an ordinary word here"}]},
            "challenge": "A clean challenge with no answer marker.",
        }

    def test_clean_surfaces_produce_a_pass_and_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "src.txt").write_text("clean source", encoding="utf-8")
            receipt = certify_surfaces(self._clean_surfaces(root), manifest_sha256="b" * 64)
            self.assertEqual(receipt["status"], "passed")
            self.assertTrue(receipt["passed"])
            validate_certification_receipt(receipt)
            tampered = dict(receipt)
            tampered["passed"] = False
            with self.assertRaises(CorpusContractError):
                validate_certification_receipt(tampered)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "AGENTS.md").write_text("autoloaded context", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("credential helper", encoding="utf-8")
            (root / "change.patch").write_text("diff --git a/a b/a\n+++ b/a", encoding="utf-8")
            surfaces = self._clean_surfaces(root)
            surfaces["fixture"] = {"fixture_id": "LongMemEval benchmark control arm", "text": "correct answer: leaked"}
            surfaces["graph_identity"] = {
                "nodes": [{"id": "fixture-node", "label": "fixture identity", "body": "fixture remains ordinary"}]
            }
            surfaces["challenge"] = "The benchmark challenge includes the expected answer and solution marker."

            receipt = certify_surfaces(surfaces)

            self.assertEqual(receipt["schema_version"], CERTIFICATION_SCHEMA_VERSION)
            self.assertFalse(receipt["passed"])
            self.assertEqual(
                set(receipt["finding_counts"]),
                {"auto-loaded-context", "suspicious-metadata", "benchmark-awareness", "patch-or-diff", "solution-leak"},
            )
            self.assertNotIn("fixture", receipt["finding_counts"], "graph body fixture must not be a finding by itself")
            public = json.dumps(receipt, sort_keys=True)
            for forbidden in ("autoloaded context", "credential helper", "correct answer", "expected answer", "diff --git"):
                self.assertNotIn(forbidden, public)

    def test_missing_artifact_fails_and_explicit_optout_is_unchecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            surfaces = self._clean_surfaces(root)
            del surfaces["evidence"]
            with self.assertRaises(CorpusContractError):
                certify_surfaces(surfaces)
            receipt = certify_surfaces(
                surfaces,
                environment={"PERSEUS_VAULT_ALLOW_UNCHECKED_CORPUS": "1"},
            )
            self.assertEqual(receipt["status"], "unchecked")
            self.assertFalse(receipt["passed"])
            self.assertTrue(receipt["unchecked_opt_out"])

    def test_redaction_receipt_is_deterministic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "CLAUDE.md").write_text("private context", encoding="utf-8")
            (source / ".cursor").mkdir()
            (source / ".cursor" / "rules").write_text("private rules", encoding="utf-8")
            (source / "src").mkdir()
            (source / "src" / "main.py").write_text("print('clean')", encoding="utf-8")
            one = Path(tmp) / "one"
            two = Path(tmp) / "two"
            first = redact_tree(source, one)
            second = redact_tree(source, two)
            validate_redaction_receipt(first)
            tampered = dict(first)
            tampered["redacted_bytes"] = first["redacted_bytes"] + 1
            with self.assertRaises(CorpusContractError):
                validate_redaction_receipt(tampered)
            self.assertEqual(first, second)
            self.assertEqual([row["path"] for row in first["removed"]], [".cursor/rules", "CLAUDE.md"])
            self.assertFalse((one / "CLAUDE.md").exists())
            self.assertFalse((one / ".cursor").exists())
            self.assertTrue((one / "src" / "main.py").exists())
            third = redact_tree(one, Path(tmp) / "three")
            self.assertEqual(third["removed"], [])
            self.assertEqual(third["redacted_tree_sha256"], first["redacted_tree_sha256"])

    def test_materialize_git_object_is_gitless_and_excludes_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "fixture"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("tracked", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
            (repo / "ignored-build").mkdir()
            (repo / "ignored-build" / "out.txt").write_text("untracked", encoding="utf-8")
            destination = Path(tmp) / "materialized"
            receipt = materialize_git_tree(repo, "HEAD", destination)
            self.assertTrue((destination / "tracked.txt").exists())
            self.assertFalse((destination / ".git").exists())
            self.assertFalse((destination / "ignored-build").exists())
            self.assertEqual(receipt["file_count"], 1)
            self.assertFalse(receipt["raw_inputs_captured"])

    def test_path_validation_is_strict(self):
        self.assertEqual(validate_relative_path("src/main.py"), "src/main.py")
        for value in ("/absolute", "../escape", "a/../b", "a\\b", "C:/drive", ""):
            with self.subTest(value=value):
                with self.assertRaises(CorpusContractError):
                    validate_relative_path(value)


if __name__ == "__main__":
    unittest.main()
