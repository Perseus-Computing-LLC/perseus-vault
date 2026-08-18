import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _canonical_tools():
    source = (ROOT / "src/mcp.rs").read_text(encoding="utf-8")
    start = source.index("fn tool_registry_base")
    match = re.search(r'r###"(\[.*?\])"###', source[start:], re.DOTALL)
    assert match, "canonical Rust tool registry literal not found"
    return {tool["name"]: tool for tool in json.loads(match.group(1))}


def _installer_tools():
    source = (ROOT / "integrations/hermes/install-perseus-vault.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'SCHEMAS = json\.loads\(r"""(.*?)"""\)', source, re.DOTALL)
    assert match, "installer static schema literal not found"
    return {tool["name"]: tool for tool in json.loads(match.group(1))}


def _server_card_tools():
    card = json.loads(
        (ROOT / ".well-known/mcp/server-card.json").read_text(encoding="utf-8")
    )
    return {tool["name"]: tool for tool in card["tools"]}


def test_admission_schema_copies_match_canonical_registry():
    canonical = _canonical_tools()
    installer = _installer_tools()
    server_card = _server_card_tools()
    # These are intentionally smaller allowlists, but every advertised tool
    # must be an exact copy of the canonical runtime registry entry.
    for label, advertised in (("installer", installer), ("server-card", server_card)):
        assert set(advertised) <= set(canonical), f"{label} advertises unknown tools"
        for name, tool in advertised.items():
            assert tool == canonical[name], f"{label} schema drift: {name}"

    required = {
        "perseus_vault_remember",
        "perseus_vault_journal",
        "perseus_vault_admission_decide",
    }
    for name in required:
        assert name in canonical
        assert name in installer, f"installer does not advertise {name}"
        assert name in server_card, f"server card does not advertise {name}"

    remember = canonical["perseus_vault_remember"]
    assert "admission" in remember["inputSchema"]["properties"]
    assert "outcome_class" in remember["outputSchema"]["properties"]
    journal = canonical["perseus_vault_journal"]
    assert "source_attestation" in journal["inputSchema"]["properties"]
    decide = canonical["perseus_vault_admission_decide"]
    assert decide["inputSchema"]["required"] == [
        "category",
        "key",
        "workspace_hash",
        "requesting_agent_id",
        "decision",
        "reason",
    ]
