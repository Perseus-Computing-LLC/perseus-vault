from __future__ import annotations

from abc import ABC, abstractmethod

from .models import AdmissionReceipt, MemoryRecord, MutationReceipt, RetrievalResult


class HostileMemoryProvider(ABC):
    """Minimal provider boundary exercised by the Gauntlet."""

    name = "provider"
    contract = "perseus-hostile-memory-gauntlet/provider/v1"

    @abstractmethod
    def reset(self) -> None:
        """Return to an empty isolated case state."""

    @abstractmethod
    def ingest(self, record: MemoryRecord) -> AdmissionReceipt:
        """Admit, quarantine, or reject one source-bound record."""

    @abstractmethod
    def forget(self, scope: str, record_id: str) -> MutationReceipt:
        """Archive one record while preserving a tombstone."""

    @abstractmethod
    def retrieve(self, query: str, scope: str, as_of: int, limit: int) -> RetrievalResult:
        """Return bounded evidence or an explicit abstention."""

    def public_metadata(self) -> dict[str, object]:
        """Return non-sensitive execution identity for the public report."""
        return {"real_producer": False, "offline": True, "network_calls": 0}
