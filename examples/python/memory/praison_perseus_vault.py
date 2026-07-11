#!/usr/bin/env python3
"""PraisonAI + Perseus Vault: persistent memory across runs (#551).

Demonstrates the PerseusVaultAdapter as a MemoryProtocol backend. Run it twice
against the same --db path: the second run recalls what the first stored,
proving memory survives process restarts.

Usage
-----
    pip install ./integrations/client ./integrations/praison
    # first run stores + recalls
    python examples/python/memory/praison_perseus_vault.py --db ./agent.db
    # second run recalls only (memory persisted)
    python examples/python/memory/praison_perseus_vault.py --db ./agent.db --recall-only

Requires the `perseus-vault` binary on PATH (single static binary, no deps):
    curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash
"""

from __future__ import annotations

import argparse

from perseus_vault_praison import PerseusVaultAdapter


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="./agent.db", help="Perseus Vault SQLite DB path")
    ap.add_argument("--binary", default=None, help="Path to perseus-vault binary")
    ap.add_argument("--recall-only", action="store_true",
                    help="Skip the store step; only recall (use on the 2nd run)")
    args = ap.parse_args()

    memory = PerseusVaultAdapter(binary=args.binary, db_path=args.db)

    if not args.recall_only:
        print("Storing memories...")
        memory.store_long_term(
            "The user prefers metric units and 24-hour time.",
            {"source": "preferences", "confidence": "high"},
        )
        memory.store_long_term(
            "Project 'atlas' uses Rust + SQLite and ships as a single static binary.",
            {"source": "project-notes"},
        )
        memory.store_short_term("Currently debugging the retry backoff loop.")
        print("  stored 2 long-term + 1 short-term memory\n")

    print("Recalling 'what units does the user like?' ->")
    for hit in memory.search_long_term("units time preference", limit=3):
        print(f"  [{hit['type']}] {hit['text']}")

    print("\nContext block for 'atlas project stack':")
    print("  " + (memory.get_context("atlas project stack", limit=2) or "(empty)"))

    total = memory.get_all_memories()
    print(f"\nTotal memories in the vault: {len(total)}")

    memory.close()


if __name__ == "__main__":
    main()
