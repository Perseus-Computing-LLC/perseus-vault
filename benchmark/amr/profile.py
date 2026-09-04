"""Provider-free AMR 0.1 export and verification profile for Vault claim cards."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

AMR_VERSION = "0.1"
PROFILE_NAME = "perseus-vault-amr-0.1"
MAX_EXTENSION_BYTES = 65_536
MAX_RECORD_BYTES = 131_072
SUPPORTED_EPISTEMIC = {"fact", "inference", "open_question", "unverified"}
_SUPPORTED_HASHES = {"sha256": 64, "md5": 32}
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


class AMRValidationError(ValueError):
    """A malformed or unsafe AMR/Vault boundary value."""

    def __init__(self, message: str, reason: str = "invalid_record") -> None:
        super().__init__(message)
        self.reason = reason


def _fail(message: str, reason: str = "invalid_record") -> None:
    raise AMRValidationError(message, reason)


def _text(value: Any, field: str, *, allow_empty: bool = False, limit: int = 16_384) -> str:
    if not isinstance(value, str) or "\x00" in value or "\r" in value or "\n" in value:
        _fail(f"{field} must be bounded text")
    result = value if allow_empty else value.strip()
    if not allow_empty and not result:
        _fail(f"{field} must be non-empty text")
    if len(result) > limit:
        _fail(f"{field} exceeds its bound")
    return result


def _ref(value: Any, field: str) -> str:
    result = _text(value, field, limit=512)
    if result != value:
        _fail(f"{field} must not contain leading or trailing whitespace", "malformed_ref")
    if result.startswith("/") or ".." in result or _REF_RE.fullmatch(result) is None:
        _fail(f"{field} is a malformed ref", "malformed_ref")
    return result


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("value is not canonical JSON", "noncanonical_json")
        raise AssertionError from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def hash_algorithm(value: Any) -> str:
    """Return the AMR compatibility algorithm after strict hash parsing."""
    return _parse_hash(value, "quote_hash")[0]


def normalize_quote(value: str) -> str:
    """Apply the AMR 0.1 punctuation and whitespace normalization."""
    if not isinstance(value, str):
        _fail("quote must be text", "malformed_quote")
    folded = value.translate(str.maketrans({
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
    }))
    return re.sub(r"\s+", " ", folded).strip()


def _parse_hash(value: Any, field: str) -> tuple[str, str]:
    value = _text(value, field, limit=160)
    algorithm = ""
    if ":" in value:
        algorithm, digest = value.split(":", 1)
        if algorithm not in _SUPPORTED_HASHES:
            _fail(f"{field} uses unsupported hash algorithm {algorithm}", "unsupported_hash_algorithm")
    else:
        if len(value) == 32:
            algorithm = "md5"
        elif len(value) == 64:
            algorithm = "sha256"
        else:
            _fail(f"{field} has an unrecognized bare digest length", "unsupported_hash_algorithm")
        digest = value
    expected_length = _SUPPORTED_HASHES[algorithm]
    if len(digest) != expected_length or _HEX_RE.fullmatch(digest) is None:
        _fail(f"{field} has an invalid {algorithm} digest", "malformed_hash")
    return algorithm, digest


def _quote_hash(quote: str, algorithm: str = "sha256") -> str:
    if algorithm not in _SUPPORTED_HASHES:
        _fail(f"unsupported quote hash algorithm {algorithm}", "unsupported_hash_algorithm")
    digest = hashlib.new(algorithm, normalize_quote(quote).encode("utf-8")).hexdigest()
    return f"{algorithm}:{digest}"


def derive_claim_id(record_ref: str, claim_text: str) -> str:
    return hashlib.sha256(f"{record_ref}:{normalize_quote(claim_text)}".encode("utf-8")).hexdigest()[:16]


def _exact_keys(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object")
    unknown = set(value) - allowed
    if unknown:
        _fail(f"{field} has unsupported fields: {sorted(unknown)}", "unsupported_field")


def _validate_source(source: Any, index: int) -> None:
    if not isinstance(source, Mapping):
        _fail(f"sources[{index}] must be an object")
    _exact_keys(source, {"ref", "quote", "quote_hash"}, f"sources[{index}]")
    _ref(source.get("ref"), f"sources[{index}].ref")
    quote = _text(source.get("quote"), f"sources[{index}].quote", limit=65_536)
    if not normalize_quote(quote):
        _fail(f"sources[{index}].quote must not be empty", "malformed_quote")
    if "quote_hash" in source and source["quote_hash"] is not None:
        _parse_hash(source["quote_hash"], f"sources[{index}].quote_hash")


def _validate_claim(claim: Any, index: int) -> None:
    if not isinstance(claim, Mapping):
        _fail(f"claims[{index}] must be an object")
    _exact_keys(claim, {"claim_id", "text", "source_id", "span", "anchor_id"}, f"claims[{index}]")
    if "claim_id" in claim and claim["claim_id"] is not None:
        _text(claim["claim_id"], f"claims[{index}].claim_id", limit=256)
    _text(claim.get("text"), f"claims[{index}].text", limit=65_536)
    if "source_id" not in claim:
        _fail(f"claims[{index}] is missing source_id", "missing_required_field")
    _ref(claim.get("source_id"), f"claims[{index}].source_id")
    span = claim.get("span")
    if not isinstance(span, Mapping):
        _fail(f"claims[{index}].span must be an object", "missing_required_field")
    _exact_keys(span, {"quote", "quote_hash"}, f"claims[{index}].span")
    quote = _text(span.get("quote"), f"claims[{index}].span.quote", limit=65_536)
    if not normalize_quote(quote):
        _fail(f"claims[{index}].span.quote must not be empty", "malformed_quote")
    if "quote_hash" in span and span["quote_hash"] is not None:
        _parse_hash(span["quote_hash"], f"claims[{index}].span.quote_hash")
    if "anchor_id" in claim and claim["anchor_id"] is not None:
        _text(claim["anchor_id"], f"claims[{index}].anchor_id", limit=512)


def validate_record(record: Mapping[str, Any]) -> None:
    """Validate the AMR record envelope without repairing any values."""
    if not isinstance(record, Mapping):
        _fail("AMR record must be an object")
    allowed = {"auditable_memory", "ref", "epistemic", "confidence", "sources", "claims", "backed_by", "contradicts", "extensions", "loss_report"}
    _exact_keys(record, allowed, "record")
    if record.get("auditable_memory") != AMR_VERSION:
        _fail("unrecognized auditable_memory version", "unsupported_version")
    _ref(record.get("ref"), "ref")
    if "epistemic" in record and record["epistemic"] is not None:
        if not isinstance(record["epistemic"], str) or record["epistemic"] not in SUPPORTED_EPISTEMIC:
            _fail("epistemic value not in closed vocabulary", "unsupported_epistemic")
    if "confidence" in record:
        confidence = record["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            _fail("confidence out of range", "invalid_confidence")
    for field in ("backed_by", "contradicts"):
        if field not in record:
            continue
        values = record[field]
        if not isinstance(values, list):
            _fail(f"{field} must be a list")
        seen: set[str] = set()
        for index, value in enumerate(values):
            parsed = _ref(value, f"{field}[{index}]")
            if parsed in seen:
                _fail(f"{field} contains duplicate refs", "duplicate_ref")
            seen.add(parsed)
    if "sources" in record:
        if not isinstance(record["sources"], list):
            _fail("sources must be a list")
        for index, source in enumerate(record["sources"]):
            _validate_source(source, index)
    if "claims" in record:
        if not isinstance(record["claims"], list):
            _fail("claims must be a list")
        seen_claim_bindings: set[tuple[str, str, str, str | None]] = set()
        for index, claim in enumerate(record["claims"]):
            _validate_claim(claim, index)
            if isinstance(claim, Mapping) and claim.get("claim_id") is not None:
                binding = (claim["claim_id"], claim["source_id"], claim["span"]["quote"], claim.get("anchor_id"))
                if binding in seen_claim_bindings:
                    _fail(f"claims[{index}] duplicates a claim binding", "duplicate_claim_binding")
                seen_claim_bindings.add(binding)
    if "extensions" in record:
        if not isinstance(record["extensions"], Mapping):
            _fail("extensions must be an object")
        _validate_extension_safety(record["extensions"], "extensions")
    if "loss_report" in record:
        loss = record["loss_report"]
        if not isinstance(loss, Mapping) or set(loss) != {"lost_fields", "lossless"}:
            _fail("loss_report must declare lost_fields and lossless", "malformed_loss_report")
        if not isinstance(loss["lost_fields"], list) or any(not isinstance(item, str) or not item for item in loss["lost_fields"]):
            _fail("loss_report.lost_fields must be a text list", "malformed_loss_report")
        if not isinstance(loss["lossless"], bool):
            _fail("loss_report.lossless must be boolean", "malformed_loss_report")
        if loss["lossless"] != (len(loss["lost_fields"]) == 0):
            _fail("loss_report.lossless disagrees with lost_fields", "malformed_loss_report")
    canonical = _canonical_bytes(record)
    if len(canonical) > MAX_RECORD_BYTES:
        _fail("AMR record exceeds its bounded JSON size", "size_bound")


def validate_cited_record(record: Mapping[str, Any]) -> None:
    """Apply the stricter Level 3 cited-record gate to an AMR record."""
    validate_record(record)
    if not record.get("sources") or not record.get("claims"):
        _fail("citation evidence requires at least one source and claim", "missing_citations")


def _validate_extension_safety(value: Any, field: str) -> None:
    forbidden = {
        "body", "body_json", "prompt", "raw_prompt", "answer", "raw_answer", "gold_answer", "provider_response",
        "customer_data", "question", "question_id", "question_type", "answer_session_ids", "evaluator_metadata",
        "hidden_label", "password", "passphrase", "secret", "api_key", "credential", "authorization", "token",
        "private_key", "private_key_pem", "signing_key", "secret_key", "client_secret", "access_key",
        "access_token", "refresh_token", "bearer_token", "encryption_key", "inferred_links",
        "model", "provider", "judge", "dataset", "split",
    }
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            if _is_forbidden_key(key, forbidden):
                _fail(f"{field}.{key} is not permitted in an AMR extension", "sensitive_field")
            _validate_extension_safety(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_extension_safety(child, f"{field}[{index}]")


_CARD_FIELDS = {
    "entity_id", "claim", "claim_id", "claim_card_version", "category", "key", "entity_type", "provenance_class", "epistemic_state", "epistemic",
    "confidence", "certainty", "verified", "support_count", "times", "valid_time", "transaction_time", "scope", "workspace_hash",
    "authority", "agent_id", "visibility", "state", "lifecycle", "evidence", "evidence_entity_ids", "source_spans", "links", "revocation", "tombstone", "quarantine", "quarantined", "revoked", "archived", "superseded_by", "supersedes", "lossy_required_fields", "lossy_fields",
}
_SENSITIVE_CARD_KEYS = {
    "body", "body_json", "prompt", "answer", "gold_answer", "provider_response", "customer_data",
    "token", "credential", "secret", "password", "passphrase", "private_key", "private_key_pem",
    "signing_key", "secret_key", "client_secret", "access_key", "access_token", "refresh_token",
    "bearer_token", "encryption_key",
}
_FORBIDDEN_CARD_KEYS = _SENSITIVE_CARD_KEYS | {
    "raw_prompt", "raw_answer", "question", "question_id", "question_type", "answer_session_ids",
    "evaluator_metadata", "hidden_label", "api_key", "authorization", "model", "provider", "judge", "dataset", "split",
    "password", "passphrase", "private_key", "private_key_pem", "signing_key", "secret_key", "client_secret",
    "access_key", "access_token", "refresh_token", "bearer_token", "encryption_key",
}
_EPISTEMIC_MAP = {
    "fact": "fact", "asserted": "fact", "observed": "fact",
    "inference": "inference", "inferred": "inference",
    "open_question": "open_question", "unverified": "unverified", "candidate": "unverified", "draft": "unverified",
}
_BACKING_RELATIONSHIPS = {"backed_by", "evidence_for", "derived_from", "promoted_to"}
_CONTRADICTION_RELATIONSHIPS = {"contradicts", "contradiction"}
_SOURCE_SPAN_FIELDS = {"source_ref", "ref", "quote", "quote_hash", "anchor_id"}
_SPAN_ITEM_FIELDS = {"source_span", "span", "source_ref", "ref", "quote", "quote_hash", "anchor_id", "claim_id", "claim_text"}
_EVIDENCE_ITEM_FIELDS = _SPAN_ITEM_FIELDS | {"entity_id", "target_id", "target_ref", "relationship"}
_LINK_ITEM_FIELDS = {"entity_id", "target_id", "target_ref", "relationship"}
_FORBIDDEN_KEY_MARKERS = (
    "password", "passphrase", "private_key", "privatekey", "secret", "credential", "authorization",
    "api_key", "apikey", "access_token", "refresh_token", "bearer_token", "accesskey", "encryption_key",
    "benchmark", "provider", "model", "judge", "dataset", "split", "evaluator", "question", "answer_session",
)


def _normalized_key(value: Any) -> str:
    return str(value).lower().replace("-", "_")


def _is_forbidden_key(value: Any, forbidden: set[str]) -> bool:
    normalized = _normalized_key(value)
    exact = {item.replace("-", "_") for item in forbidden}
    return normalized in exact or any(marker in normalized for marker in _FORBIDDEN_KEY_MARKERS) or normalized.endswith(("_token", "_secret", "_credential", "_password", "_passphrase", "_private_key", "_signing_key", "_access_key", "_encryption_key"))


def _json_safe_copy(value: Any, field: str) -> Any:
    try:
        copied = copy.deepcopy(value)
        if len(_canonical_bytes(copied)) > MAX_EXTENSION_BYTES:
            _fail(f"{field} exceeds its bounded JSON size", "size_bound")
        _validate_extension_safety(copied, field)
        return copied
    except AMRValidationError:
        raise
    except Exception as exc:
        _fail(f"{field} is not JSON-safe", "noncanonical_json")
        raise AssertionError from exc


def _card_times(card: Mapping[str, Any]) -> dict[str, Any]:
    raw_times = card.get("times", {})
    if raw_times is None:
        raw_times = {}
    valid_input = card.get("valid_time", {})
    transaction_input = card.get("transaction_time", {})
    if valid_input is None:
        valid_input = {}
    if transaction_input is None:
        transaction_input = {}
    if not isinstance(raw_times, Mapping) or not isinstance(valid_input, Mapping) or not isinstance(transaction_input, Mapping):
        _fail("times, valid_time, and transaction_time must be objects", "malformed_time")
    def choose(group_name: str, key: str, alias: Mapping[str, Any]) -> Any:
        if key in raw_times and key in alias and raw_times[key] != alias[key]:
            _fail(f"conflicting {group_name} values for {key}", "malformed_time")
        if key in raw_times:
            return raw_times[key]
        return alias.get(key)

    valid = {
        "valid_from_unix_ms": choose("valid_time", "valid_from_unix_ms", valid_input),
        "valid_to_unix_ms": choose("valid_time", "valid_to_unix_ms", valid_input),
    }
    transaction = {
        "recorded_at_unix_ms": choose("transaction_time", "recorded_at_unix_ms", transaction_input),
        "invalidated_at_unix_ms": choose("transaction_time", "invalidated_at_unix_ms", transaction_input),
    }
    for group_name, group in (("valid_time", valid), ("transaction_time", transaction)):
        for key, value in group.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                _fail(f"{group_name}.{key} must be an integer or null", "malformed_time")
    known = {
        "times": {"valid_from_unix_ms", "valid_to_unix_ms", "recorded_at_unix_ms", "invalidated_at_unix_ms"},
        "valid_time": {"valid_from_unix_ms", "valid_to_unix_ms"},
        "transaction_time": {"recorded_at_unix_ms", "invalidated_at_unix_ms"},
    }
    unmapped: dict[str, Any] = {}
    for group_name, group in (("times", raw_times), ("valid_time", valid_input), ("transaction_time", transaction_input)):
        extra = {key: value for key, value in group.items() if key not in known[group_name]}
        if extra:
            unmapped[group_name] = _json_safe_copy(extra, f"{group_name}.unmapped")
    result: dict[str, Any] = {"valid_time": valid, "transaction_time": transaction}
    if unmapped:
        result["unmapped_fields"] = unmapped
    return result


def _span_from(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    if "source_span" in value and "span" in value:
        _fail(f"{field} cannot declare both source_span and span", "malformed_source_span")
    nested_name = "source_span" if "source_span" in value else "span" if "span" in value else None
    if nested_name is not None:
        span = value[nested_name]
        competing = {key for key in ("source_ref", "ref", "quote", "quote_hash") if key in value}
        if competing:
            _fail(f"{field} has competing nested and direct span fields", "malformed_source_span")
        if "anchor_id" in value and isinstance(span, Mapping) and "anchor_id" in span and value["anchor_id"] != span["anchor_id"]:
            _fail(f"{field} has conflicting anchor_id aliases", "malformed_source_span")
    else:
        span = {key: value[key] for key in _SOURCE_SPAN_FIELDS if key in value}
    if not isinstance(span, Mapping):
        _fail(f"{field} source span must be an object", "missing_required_field")
    _exact_keys(span, _SOURCE_SPAN_FIELDS, f"{field}.span")
    if "source_ref" in span and "ref" in span and span["source_ref"] != span["ref"]:
        _fail(f"{field} source span has conflicting source ref aliases", "malformed_source_span")
    source_ref = span.get("source_ref", span.get("ref"))
    if source_ref is None:
        _fail(f"{field} source span is missing ref", "missing_required_field")
    quote = span.get("quote")
    quote = _text(quote, f"{field}.quote", limit=65_536)
    if not normalize_quote(quote):
        _fail(f"{field}.quote must not be empty", "malformed_quote")
    source_ref = _ref(source_ref, f"{field}.source_ref")
    supplied_hash = span.get("quote_hash")
    if supplied_hash is None:
        quote_hash = _quote_hash(quote)
    else:
        algorithm, digest = _parse_hash(supplied_hash, f"{field}.quote_hash")
        if algorithm != "sha256":
            _fail(f"{field}.quote_hash must use sha256 for export", "unsupported_hash_algorithm")
        expected = _quote_hash(quote, algorithm)
        if digest != expected.split(":", 1)[1]:
            _fail(f"{field}.quote_hash does not match quote", "anchor_tampered")
        quote_hash = f"{algorithm}:{digest}"
    result = {"ref": source_ref, "quote": quote, "quote_hash": quote_hash}
    anchor_id = span.get("anchor_id", value.get("anchor_id"))
    if anchor_id is not None:
        result["anchor_id"] = _text(anchor_id, f"{field}.anchor_id", limit=512)
    return result


def _link(value: Any, field: str) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object", "malformed_link")
    relationship = _text(value.get("relationship"), f"{field}.relationship", limit=64)
    candidates = [(name, value[name]) for name in ("target_id", "target_ref", "entity_id") if name in value and value[name] is not None]
    if not candidates:
        _fail(f"{field} is missing a target", "malformed_link")
    if any(candidate != candidates[0][1] for _, candidate in candidates[1:]):
        _fail(f"{field} has conflicting target aliases", "malformed_link")
    target = _ref(candidates[0][1], f"{field}.target_id")
    if relationship not in _BACKING_RELATIONSHIPS | _CONTRADICTION_RELATIONSHIPS:
        _fail(f"{field} has unsupported typed relationship {relationship}", "unsupported_relationship")
    return relationship, target


def export_claim_card(card: Mapping[str, Any]) -> dict[str, Any]:
    """Export a sanitized Vault claim-card projection as AMR 0.1."""
    if not isinstance(card, Mapping):
        _fail("claim card must be an object")
    _reject_forbidden_card_fields(card, "claim card")
    unknown = sorted(set(card) - _CARD_FIELDS)
    lossy = card.get("lossy_required_fields", card.get("lossy_fields", []))
    if lossy is None:
        lossy = []
    if not isinstance(lossy, list) or any(not isinstance(item, str) or not item for item in lossy):
        _fail("lossy fields must be a text list", "malformed_loss_report")
    if lossy:
        _fail(f"required Vault fields would be lossy: {sorted(lossy)}", "lossy_required_field")
    ref = _ref(card.get("entity_id"), "entity_id")
    claim_text = _text(card.get("claim"), "claim", limit=65_536)
    card_claim_id = card.get("claim_id")
    if card_claim_id is not None:
        card_claim_id = _text(card_claim_id, "claim_id", limit=256)
    raw_epistemic_state = card.get("epistemic_state")
    raw_epistemic_alias = card.get("epistemic")
    if raw_epistemic_state is not None and raw_epistemic_alias is not None:
        if _text(raw_epistemic_state, "epistemic_state", limit=64) != _text(raw_epistemic_alias, "epistemic", limit=64):
            _fail("epistemic_state conflicts with epistemic", "unsupported_epistemic")
    raw_epistemic = raw_epistemic_state if raw_epistemic_state is not None else raw_epistemic_alias
    mapped_epistemic = None
    if raw_epistemic is not None:
        raw_epistemic = _text(raw_epistemic, "epistemic_state", limit=64)
        mapped_epistemic = _EPISTEMIC_MAP.get(raw_epistemic)
        if mapped_epistemic is None:
            _fail("epistemic value is unsupported by the AMR mapping", "unsupported_epistemic")
    record: dict[str, Any] = {"auditable_memory": AMR_VERSION, "ref": ref}
    if mapped_epistemic is not None:
        record["epistemic"] = mapped_epistemic
    confidence_value = card.get("confidence")
    certainty_value = card.get("certainty")
    if confidence_value is not None and certainty_value is not None and confidence_value != certainty_value:
        _fail("confidence conflicts with certainty", "invalid_confidence")
    confidence = confidence_value if confidence_value is not None else certainty_value
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            _fail("confidence out of range", "invalid_confidence")
        record["confidence"] = confidence

    spans: list[tuple[str, str, dict[str, Any], str, str]] = []
    raw_spans = card.get("source_spans", [])
    if raw_spans is None:
        raw_spans = []
    if not isinstance(raw_spans, list):
        _fail("source_spans must be a list", "malformed_source_span")
    for index, item in enumerate(raw_spans):
        if not isinstance(item, Mapping):
            _fail(f"source_spans[{index}] must be an object", "malformed_source_span")
        _exact_keys(item, _SPAN_ITEM_FIELDS, f"source_spans[{index}]")
        span = _span_from(item, f"source_spans[{index}]")
        text = _text(item.get("claim_text", claim_text), f"source_spans[{index}].claim_text", limit=65_536)
        claim_id = item.get("claim_id", card.get("claim_id")) or derive_claim_id(ref, text)
        claim_id = _text(claim_id, f"source_spans[{index}].claim_id", limit=256)
        spans.append((span["ref"], span["quote"], span, claim_id, text))
    evidence = card.get("evidence", [])
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        _fail("evidence must be a list", "malformed_evidence")
    typed_links: set[tuple[str, str]] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            _fail(f"evidence[{index}] must be an object", "malformed_evidence")
        _exact_keys(item, _EVIDENCE_ITEM_FIELDS, f"evidence[{index}]")
        relationship, target = _link(item, f"evidence[{index}]")
        typed_links.add((relationship, target))
        if any(key in item for key in ("source_span", "span")):
            span = _span_from(item, f"evidence[{index}]")
            text = _text(item.get("claim_text", claim_text), f"evidence[{index}].claim_text", limit=65_536)
            claim_id = item.get("claim_id", card.get("claim_id")) or derive_claim_id(ref, text)
            spans.append((span["ref"], span["quote"], span, _text(claim_id, f"evidence[{index}].claim_id", limit=256), text))
    links = card.get("links", [])
    if links is None:
        links = []
    if not isinstance(links, list):
        _fail("links must be a list", "malformed_link")
    for index, item in enumerate(links):
        _exact_keys(item, _LINK_ITEM_FIELDS, f"links[{index}]")
        if isinstance(item, Mapping) and "entity_id" in item:
            _ref(item["entity_id"], f"links[{index}].entity_id")
        relationship, target = _link(item, f"links[{index}]")
        typed_links.add((relationship, target))
    if "inferred_links" in card and card["inferred_links"]:
        _fail("inferred relations cannot be serialized as AMR typed links", "inferred_relationship")
    sorted_links = sorted(typed_links)
    typed_link_objects = [{"relationship": relationship, "target_id": target} for relationship, target in sorted_links]
    backing = sorted({target for relationship, target in sorted_links if relationship in _BACKING_RELATIONSHIPS})
    contradictions = sorted({target for relationship, target in sorted_links if relationship in _CONTRADICTION_RELATIONSHIPS})
    if backing:
        record["backed_by"] = backing
    if contradictions:
        record["contradicts"] = contradictions

    source_values: dict[tuple[str, str, str], dict[str, str]] = {}
    claims: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for source_ref, quote, span, claim_id, span_claim_text in spans:
        source_key = (source_ref, quote, span["quote_hash"])
        source_values[source_key] = {"ref": source_ref, "quote": quote, "quote_hash": span["quote_hash"]}
        claim_record: dict[str, Any] = {
            "claim_id": claim_id,
            "text": span_claim_text,
            "source_id": source_ref,
            "span": {"quote": quote, "quote_hash": span["quote_hash"]},
        }
        if "anchor_id" in span:
            claim_record["anchor_id"] = span["anchor_id"]
        claim_key = (claim_id, source_ref, quote, span.get("anchor_id", ""))
        if claim_key in claims:
            _fail("conflicting or duplicate claim binding", "duplicate_claim_binding")
        claims[claim_key] = claim_record
    if source_values:
        record["sources"] = [source_values[key] for key in sorted(source_values)]
        record["claims"] = [claims[key] for key in sorted(claims)]

    times = _card_times(card)
    state = card.get("state", {})
    if state is None:
        state = {}
    if not isinstance(state, Mapping):
        _fail("state must be an object", "malformed_state")
    state = dict(state)
    for inherited_field in ("superseded_by", "supersedes", "tombstone", "quarantine", "quarantined", "revoked", "revocation", "archived"):
        if inherited_field not in card:
            continue
        if inherited_field in state and state[inherited_field] != card[inherited_field]:
            _fail(f"state.{inherited_field} conflicts with the top-level value", "malformed_state")
        state.setdefault(inherited_field, card[inherited_field])
    state_known_fields = {
        "superseded", "superseded_by", "supersedes", "quarantined", "quarantine", "revoked", "revocation", "tombstone", "archived",
    }
    state_unmapped = {key: value for key, value in state.items() if key not in state_known_fields}
    state_extension = {
        "superseded": state.get("superseded", False),
        "superseded_by": state.get("superseded_by"),
        "supersedes": state.get("supersedes"),
        "quarantined": state.get("quarantined", False),
        "quarantine": state.get("quarantine", False),
        "revoked": state.get("revoked", False),
        "revocation": state.get("revocation"),
        "tombstone": state.get("tombstone", False),
        "archived": state.get("archived", False),
    }
    for name, value in state_extension.items():
        if name in {"revocation", "quarantine"}:
            if value is not None and not isinstance(value, (bool, Mapping)):
                _fail(f"state.{name} must be boolean, object, or null", "malformed_state")
            if isinstance(value, Mapping):
                state_extension[name] = _json_safe_copy(value, f"state.{name}")
            continue
        if isinstance(value, bool) or value is None:
            continue
        if name in {"superseded_by", "supersedes"}:
            if isinstance(value, list):
                for index, item in enumerate(value):
                    _ref(item, f"state.{name}[{index}]")
            else:
                _ref(value, f"state.{name}")
        else:
            _fail(f"state.{name} must be boolean or null", "malformed_state")
    if state_unmapped:
        state_extension["unmapped_fields"] = _json_safe_copy(state_unmapped, "state.unmapped_fields")
    authority = card.get("authority")
    if authority is None and (card.get("agent_id") is not None or card.get("visibility") is not None):
        authority = {}
        if card.get("agent_id") is not None:
            authority["agent_id"] = _text(card["agent_id"], "agent_id", limit=256)
        if card.get("visibility") is not None:
            authority["visibility"] = _text(card["visibility"], "visibility", limit=128)
    if authority is not None and not isinstance(authority, (Mapping, str)):
        _fail("authority must be an object, text, or null", "malformed_authority")
    if isinstance(authority, str):
        if card.get("agent_id") is not None or card.get("visibility") is not None:
            _fail("authority text conflicts with agent_id or visibility", "malformed_authority")
        authority = _text(authority, "authority", limit=512)
    elif isinstance(authority, Mapping):
        authority = dict(_json_safe_copy(authority, "authority"))
        for field_name, field_limit in (("agent_id", 256), ("visibility", 128)):
            if card.get(field_name) is None:
                continue
            field_value = _text(card[field_name], field_name, limit=field_limit)
            if field_name in authority and authority[field_name] != field_value:
                _fail(f"authority.{field_name} conflicts with the top-level value", "malformed_authority")
            authority[field_name] = field_value
    explicit_evidence_ids = card.get("evidence_entity_ids", [])
    if explicit_evidence_ids is None:
        explicit_evidence_ids = []
    if not isinstance(explicit_evidence_ids, list):
        _fail("evidence_entity_ids must be a list", "malformed_evidence")
    evidence_entity_ids = []
    for index, entity_id in enumerate(explicit_evidence_ids):
        evidence_entity_ids.append(_ref(entity_id, f"evidence_entity_ids[{index}]") )
    for index, item in enumerate(evidence):
        if "entity_id" in item:
            evidence_entity_ids.append(_ref(item["entity_id"], f"evidence[{index}].entity_id"))
    verified = card.get("verified")
    if verified is not None and not isinstance(verified, bool):
        _fail("verified must be boolean or null", "malformed_claim_card")
    support_count = card.get("support_count")
    if support_count is not None and (isinstance(support_count, bool) or not isinstance(support_count, int) or support_count < 0):
        _fail("support_count must be a non-negative integer or null", "malformed_claim_card")
    scope = card.get("scope")
    workspace_hash = card.get("workspace_hash")
    if scope is not None and workspace_hash is not None and scope != workspace_hash:
        _fail("scope conflicts with workspace_hash", "malformed_scope")
    if scope is None:
        scope = workspace_hash
    if scope is not None:
        scope = _text(scope, "scope", limit=512)
    vault_extension = {
        "profile": PROFILE_NAME,
        "claim": claim_text,
        "claim_id": card_claim_id,
        "claim_card_version": card.get("claim_card_version"),
        "category": card.get("category"),
        "key": card.get("key"),
        "entity_type": card.get("entity_type"),
        "provenance_class": card.get("provenance_class"),
        "epistemic_state": raw_epistemic,
        "certainty": card.get("certainty"),
        "verified": verified,
        "support_count": support_count,
        "times": times,
        "scope": scope,
        "authority": authority,
        "revocation": _json_safe_copy(card.get("revocation"), "revocation") if card.get("revocation") is not None else None,
        "state": state_extension,
        "lifecycle": _json_safe_copy(card.get("lifecycle"), "lifecycle") if card.get("lifecycle") is not None else None,
        "typed_links": typed_link_objects,
        "evidence_entity_ids": sorted(evidence_entity_ids),
    }
    if vault_extension["scope"] is not None:
        vault_extension["scope"] = _text(vault_extension["scope"], "scope", limit=512)
    record["extensions"] = {"vault": vault_extension}
    record["loss_report"] = {"lost_fields": unknown, "lossless": not unknown}
    validate_record(record)
    return record


def _reject_forbidden_card_fields(value: Any, field: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            normalized = _normalized_key(key)
            if normalized == "inferred_links":
                _fail(f"{field}.{key} cannot be serialized as an AMR typed link", "inferred_relationship")
            if _is_forbidden_key(key, _FORBIDDEN_CARD_KEYS):
                _fail(f"{field}.{key} is not exportable", "sensitive_field")
            _reject_forbidden_card_fields(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_card_fields(child, f"{field}[{index}]")


def _verification_status(source: Mapping[str, Any], source_text: str | None, field: str) -> dict[str, Any]:
    quote = source["quote"]
    hash_value = source.get("quote_hash")
    partial = hash_value is None
    algorithm = digest = None
    if hash_value is not None:
        algorithm, digest = _parse_hash(hash_value, f"{field}.quote_hash")
        expected = _quote_hash(quote, algorithm).split(":", 1)[1]
        if digest != expected:
            return {"ref": source["ref"], "status": "anchor_tampered", "partial": False}
    if source_text is None:
        return {"ref": source["ref"], "status": "source_missing", "partial": partial}
    present = normalize_quote(quote) in normalize_quote(source_text)
    return {"ref": source["ref"], "status": "ok" if present else "source_drifted", "partial": partial}


def verify_record(record: Mapping[str, Any], sources: Mapping[str, str]) -> dict[str, Any]:
    """Verify AMR citations with distinct integrity, drift, and missing outcomes."""
    validate_record(record)
    if not record.get("sources"):
        _fail("citation evidence is missing sources", "missing_citations")
    if not isinstance(sources, Mapping) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in sources.items()):
        _fail("source resolver must map text refs to text", "malformed_source_resolver")
    citations = []
    seen_anchors: set[tuple[str, str, Any]] = set()
    for index, source in enumerate(record.get("sources", [])):
        anchor_key = (source["ref"], source["quote"], source.get("quote_hash"))
        seen_anchors.add(anchor_key)
        source_ref = source["ref"]
        citations.append(_verification_status(source, sources.get(source_ref), f"sources[{index}]"))
    for index, claim in enumerate(record.get("claims", [])):
        span = claim["span"]
        anchor = {"ref": claim["source_id"], "quote": span["quote"]}
        if "quote_hash" in span:
            anchor["quote_hash"] = span["quote_hash"]
        anchor_key = (anchor["ref"], anchor["quote"], anchor.get("quote_hash"))
        if anchor_key in seen_anchors:
            continue
        seen_anchors.add(anchor_key)
        citations.append(_verification_status(anchor, sources.get(anchor["ref"]), f"claims[{index}].span"))
    statuses = {item["status"] for item in citations}
    if "anchor_tampered" in statuses:
        status = "anchor_tampered"
    elif "source_missing" in statuses:
        status = "source_missing"
    elif "source_drifted" in statuses:
        status = "source_drifted"
    else:
        status = "ok"
    return {"status": status, "partial": any(item["partial"] for item in citations), "citations": citations}


def import_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an imported AMR record without granting authority or promotion."""
    validate_record(record)
    return {
        "record": copy.deepcopy(dict(record)),
        "authority": {"authoritative": False, "promotion_required": True},
        "status": "imported_non_authoritative",
    }


class InMemoryAMRStore:
    """Provider-free store used only by conformance fixtures and tests."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def put(self, record: Mapping[str, Any]) -> None:
        validate_record(record)
        ref = record["ref"]
        existing = self._records.get(ref)
        candidate = copy.deepcopy(dict(record))
        if existing is not None and canonical_sha256(existing) != canonical_sha256(candidate):
            _fail(f"record {ref} already exists with different content", "conflicting_record")
        self._records[ref] = candidate

    def get(self, ref: str) -> dict[str, Any]:
        ref = _ref(ref, "ref")
        if ref not in self._records:
            _fail(f"record {ref} is missing", "source_missing")
        return copy.deepcopy(self._records[ref])

    def refs(self) -> list[str]:
        return sorted(self._records)

    def query_links(self, ref: str, relationship: str) -> list[str]:
        record = self.get(ref)
        if relationship == "backed_by":
            return list(record.get("backed_by", []))
        if relationship == "contradicts":
            return list(record.get("contradicts", []))
        _fail(f"unsupported query relationship {relationship}", "unsupported_relationship")
        return []
