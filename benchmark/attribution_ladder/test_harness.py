"""Unit tests for the attribution-ladder harness (#1049).

All tests are offline and deterministic: render shapes, the shape-aware
resolver verdicts, and report verification never touch a model or a binary.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness import (
    SHAPES,
    normalize_item,
    render_item,
    resolver_verdict,
    sha256_json,
    verify_report,
)

HERE = Path(__file__).resolve().parent

OUTLET = {
    "key": "orion-ship-acme",
    "text": "Orion v2.0 ships in June 2026.",
    "source": "Acme Blog",
    "date": "2026-04-01",
}
PLAIN = {
    "key": "orion-ci-retro",
    "text": "The Orion CI pipeline runs nightly at 02:00 UTC.",
    "source": "team retro",
    "date": "2026-02-10",
}
NO_DATE = {k: v for k, v in OUTLET.items() if k != "date"}


class RenderTests(unittest.TestCase):
    def test_shapes_render_expected_fields(self):
        self.assertEqual(render_item(OUTLET, "bare"), "Orion v2.0 ships in June 2026.")
        self.assertEqual(
            render_item(OUTLET, "key_only"),
            "[orion-ship-acme] Orion v2.0 ships in June 2026.",
        )
        self.assertEqual(
            render_item(OUTLET, "key_source"),
            "[orion-ship-acme | Acme Blog] Orion v2.0 ships in June 2026.",
        )
        self.assertEqual(
            render_item(OUTLET, "key_source_time"),
            "[orion-ship-acme | Acme Blog | 2026-04-01] Orion v2.0 ships in June 2026.",
        )

    def test_missing_fields_render_as_question_mark(self):
        self.assertEqual(
            render_item(NO_DATE, "key_source_time"),
            "[orion-ship-acme | Acme Blog | ?] Orion v2.0 ships in June 2026.",
        )


class ResolverTests(unittest.TestCase):
    def test_outlet_question_refuses_without_source_visible(self):
        q = {"kind": "outlet", "resolve": {"source": "Acme Blog"}, "facts": ["June 2026"]}
        self.assertEqual(resolver_verdict(q, [OUTLET], "bare"), "refusal")
        self.assertEqual(resolver_verdict(q, [OUTLET], "key_only"), "refusal")
        self.assertEqual(resolver_verdict(q, [OUTLET], "key_source"), "correct")
        self.assertEqual(resolver_verdict(q, [OUTLET], "key_source_time"), "correct")

    def test_outlet_question_wrong_source_is_refusal(self):
        q = {"kind": "outlet", "resolve": {"source": "Nimbus Docs"}, "facts": ["June 2026"]}
        self.assertEqual(resolver_verdict(q, [OUTLET], "key_source"), "refusal")

    def test_outlet_asof_needs_date(self):
        q = {
            "kind": "outlet_asof",
            "resolve": {"source": "Nimbus Docs", "as_of": "2026-02-01"},
            "facts": ["postgres 14"],
        }
        pg14 = {"key": "orion-pg14", "text": "Orion v1.9 runs postgres 14.",
                "source": "Nimbus Docs", "date": "2026-01-15"}
        pg15 = {"key": "orion-pg15", "text": "Orion v1.9 runs postgres 15.",
                "source": "Nimbus Docs", "date": "2026-03-01"}
        self.assertEqual(resolver_verdict(q, [pg14, pg15], "key_source"), "refusal")
        self.assertEqual(resolver_verdict(q, [pg14, pg15], "key_source_time"), "correct")

    def test_asof_filter_selects_older_fact(self):
        q = {
            "kind": "outlet_asof",
            "resolve": {"source": "Nimbus Docs", "as_of": "2026-02-01"},
            "facts": ["postgres 14"],
        }
        pg14 = {"key": "orion-pg14", "text": "Orion v1.9 runs postgres 14.",
                "source": "Nimbus Docs", "date": "2026-01-15"}
        pg15 = {"key": "orion-pg15", "text": "Orion v1.9 runs postgres 15.",
                "source": "Nimbus Docs", "date": "2026-03-01"}
        self.assertEqual(resolver_verdict(q, [pg14, pg15], "key_source_time"), "correct")
        late = {"kind": "outlet_asof",
                "resolve": {"source": "Nimbus Docs", "as_of": "2026-04-01"},
                "facts": ["postgres 15"]}
        self.assertEqual(resolver_verdict(late, [pg14, pg15], "key_source_time"), "correct")

    def test_plain_question_answers_in_all_shapes(self):
        q = {"kind": "plain", "resolve": {}, "facts": ["02:00"]}
        for shape in SHAPES:
            self.assertEqual(resolver_verdict(q, [PLAIN], shape), "correct", shape)

    def test_absent_question_always_refuses(self):
        q = {"kind": "absent", "resolve": {}, "facts": []}
        for shape in SHAPES:
            self.assertEqual(resolver_verdict(q, [PLAIN], shape), "refusal", shape)

    def test_partial_when_some_facts_missing(self):
        q = {"kind": "plain", "resolve": {}, "facts": ["02:00", "never"]}
        self.assertEqual(resolver_verdict(q, [PLAIN], "bare"), "partial")


class DatasetTests(unittest.TestCase):
    def test_dataset_is_well_formed(self):
        data = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "perseus-vault-attribution-ladder/v1")
        keys = [m["key"] for m in data["store"]]
        self.assertEqual(len(keys), len(set(keys)), "store keys must be unique")
        kinds = {q["kind"] for q in data["queries"]}
        self.assertTrue({"outlet", "outlet_asof", "plain", "absent"} <= kinds)
        for q in data["queries"]:
            if q["kind"] == "outlet":
                self.assertIn("source", q["resolve"])
            if q["kind"] == "outlet_asof":
                self.assertIn("source", q["resolve"])
                self.assertIn("as_of", q["resolve"])

    def test_store_sources_are_present_for_outlet_queries(self):
        data = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
        sources = {m["source"] for m in data["store"]}
        for q in data["queries"]:
            src = q.get("resolve", {}).get("source")
            if src:
                self.assertIn(src, sources, q["q"])


class ReportTests(unittest.TestCase):
    def test_verify_report_recomputes_aggregates(self):
        rows = [
            {"shape": "bare", "verdict": "correct", "retrieval_ok": True, "query": "q1",
             "kind": "plain", "payload_keys": []},
            {"shape": "bare", "verdict": "refusal", "retrieval_ok": True, "query": "q2",
             "kind": "absent", "payload_keys": []},
            {"shape": "key_source_time", "verdict": "correct", "retrieval_ok": True,
             "query": "q3", "kind": "outlet", "payload_keys": []},
        ]
        shapes = {s: {"n": 0, "correct": 0, "refusal": 0, "partial": 0} for s in SHAPES}
        for r in rows:
            a = shapes[r["shape"]]
            a["n"] += 1
            a[r["verdict"]] += 1
        for a in shapes.values():
            a["accuracy"] = a["correct"] / a["n"] if a["n"] else 0.0
            a["refusal_rate"] = a["refusal"] / a["n"] if a["n"] else 0.0
        inputs = {"schema": "perseus-vault-attribution-ladder/v1", "judge": "deterministic",
                  "n_queries": 3, "shapes": list(SHAPES)}
        report = {
            "schema": "perseus-vault-attribution-ladder/v1",
            "inputs": inputs,
            "shapes": shapes,
            "retrieval_ok_rate": 1.0,
            "signature": {"value": sha256_json(inputs), "inputs": inputs},
        }
        self.assertTrue(verify_report(report, rows))
        report["signature"]["value"] = "deadbeef"
        self.assertFalse(verify_report(report, rows))


class NormalizeTests(unittest.TestCase):
    def test_normalize_item_expands_body_fields(self):
        item = {
            "key": "k",
            "body_json": json.dumps({"text": "hello", "source": "Acme", "valid_from": "2026-01-01"}),
        }
        n = normalize_item(item)
        self.assertEqual(n["text"], "hello")
        self.assertEqual(n["source"], "Acme")
        self.assertEqual(n["date"], "2026-01-01")

    def test_normalize_item_tolerates_missing_fields(self):
        n = normalize_item({"key": "k"})
        self.assertEqual(n["text"], "")
        self.assertIsNone(n["source"])
        self.assertIsNone(n["date"])


if __name__ == "__main__":
    unittest.main()
