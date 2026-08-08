"""Deterministic storage, token, and cost overlays.

These helpers deliberately return measurement data rather than inventing a
provider price. A cost estimate is only emitted when the caller supplies an
explicit price per million tokens.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any


def file_bytes(path: str | Path) -> int:
    return Path(path).stat().st_size


def sqlite_bytes(db_path: str | Path) -> dict[str, int]:
    path = Path(db_path)
    result = {"db_bytes": path.stat().st_size if path.exists() else 0}
    for suffix, key in (("-wal", "wal_bytes"), ("-shm", "shm_bytes")):
        sidecar = Path(f"{path}{suffix}")
        result[key] = sidecar.stat().st_size if sidecar.exists() else 0
    result["total_bytes"] = sum(result.values())
    return result


def token_proxy(text: str) -> int:
    """Stable local token proxy; provider tokenizers belong in provider runs."""
    return max(0, math.ceil(len(text) / 4))


def token_cost(input_tokens: int, output_tokens: int = 0, *, input_usd_per_million: float | None = None, output_usd_per_million: float | None = None) -> float | None:
    if input_usd_per_million is None and output_usd_per_million is None:
        return None
    in_price = 0.0 if input_usd_per_million is None else float(input_usd_per_million)
    out_price = 0.0 if output_usd_per_million is None else float(output_usd_per_million)
    if in_price < 0 or out_price < 0:
        raise ValueError("token prices must be non-negative")
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def database_counts(db_path: str | Path) -> dict[str, int]:
    """Return bounded table counts when the SQLite file is readable."""
    connection = sqlite3.connect(str(db_path))
    try:
        counts: dict[str, int] = {}
        for table in ("entities", "entity_history", "journal", "links"):
            try:
                counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                counts[table] = 0
        return counts
    finally:
        connection.close()


def resource_overlay(*, db_path: str | Path, injected_text: str = "", output_text: str = "", input_usd_per_million: float | None = None, output_usd_per_million: float | None = None) -> dict[str, Any]:
    input_tokens = token_proxy(injected_text)
    output_tokens = token_proxy(output_text)
    storage = sqlite_bytes(db_path)
    counts = database_counts(db_path)
    active = max(0, counts.get("entities", 0))
    return {
        "storage": {**storage, "bytes_per_entity": storage["total_bytes"] / active if active else None},
        "tokens": {"input_proxy": input_tokens, "output_proxy": output_tokens, "injected_chars": len(injected_text), "output_chars": len(output_text)},
        "cost_usd": token_cost(input_tokens, output_tokens, input_usd_per_million=input_usd_per_million, output_usd_per_million=output_usd_per_million),
        "counts": counts,
    }


__all__ = ["database_counts", "file_bytes", "resource_overlay", "sqlite_bytes", "token_cost", "token_proxy"]
