import hashlib
import hmac
import json
import unittest

from benchmark.hostile_memory_gauntlet.gauntlet.models import MemoryHit, MemoryRecord
from benchmark.hostile_memory_gauntlet.gauntlet.perseus_mcp import admission_source_attestation_digest, decode_tool_result, entity_key, item_to_hit


class PerseusMCPBoundaryTests(unittest.TestCase):
    def test_structured_content_is_preferred(self):
        payload = {"result": {"structuredContent": {"items": [{"id": "x"}]}}}
        self.assertEqual(decode_tool_result(payload), {"items": [{"id": "x"}]})

    def test_text_wrapped_json_is_normalized(self):
        payload = {"result": {"content": [{"type": "text", "text": '{"items": []}'}]}}
        self.assertEqual(decode_tool_result(payload), {"items": []})

    def test_recall_item_normalization_preserves_provenance_and_scope(self):
        item = {
            "id": "vault-id",
            "category": "gauntlet",
            "key": "record-1",
            "workspace_hash": "team-a",
            "status": "active",
            "gauntlet_record_id": "record-1",
            "gauntlet_memory_key": "profile.alice.role",
            "text": "Alice is an analyst.",
            "source_ref": "source://record-1",
            "source_digest": "a" * 64,
            "actor": "alice",
            "trust": "authoritative",
            "valid_from": 1,
        }
        hit = item_to_hit(item)
        self.assertIsInstance(hit, MemoryHit)
        self.assertEqual(hit.record_id, "record-1")
        self.assertEqual(hit.scope, "team-a")
        self.assertEqual(hit.source_ref, "source://record-1")
        self.assertEqual(hit.record_digest, "a" * 64)

    def test_missing_source_binding_is_not_filled_with_a_fake_digest(self):
        item = {
            "id": "vault-id", "category": "gauntlet", "key": "record-1",
            "workspace_hash": "team-a", "gauntlet_record_id": "record-1",
            "gauntlet_memory_key": "profile.alice.role", "text": "Alice is an analyst.",
        }
        hit = item_to_hit(item)
        self.assertEqual(hit.record_digest, "")
        self.assertEqual(hit.source_ref, "")

    def test_source_attestation_matches_vault_journal_hmac_wire_value(self):
        evaluated = {
            "record_digest": "a" * 64,
            "source_identity": "gauntlet:r1",
            "workspace_hash": "team-a",
            "actor_kind": "connector",
            "actor_identity": "agent-a",
        }
        payload = json.dumps({
            **evaluated, "requesting_agent_id": "agent-a",
        }, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        hmac_hex = hmac.new(b"test-key", payload.encode(), hashlib.sha256).hexdigest()
        expected = hmac_hex
        self.assertEqual(admission_source_attestation_digest("test-key", evaluated, "agent-a"), expected)

    def test_versions_share_one_vault_entity_key_within_scope(self):
        first = MemoryRecord("r1", "profile.alice.role", "team-a", "Alice is an analyst.", "s1", "a" * 64, "alice", "authoritative", 1, 1)
        second = MemoryRecord("r2", "profile.alice.role", "team-a", "Alice is an architect.", "s2", "b" * 64, "alice", "authoritative", 5, 5)
        foreign = MemoryRecord("r3", "profile.alice.role", "team-b", "Alice is a doctor.", "s3", "c" * 64, "alice", "authoritative", 1, 1)
        self.assertEqual(entity_key(first), entity_key(second))
        self.assertNotEqual(entity_key(first), entity_key(foreign))


if __name__ == "__main__":
    unittest.main()
