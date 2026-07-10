//! #520: opt-in in-session memory capture — distill a transcript / insight
//! payload into durable memory entities the moment a problem is solved,
//! instead of waiting for a scheduled harvest to notice.
//!
//! This module is the **distiller**: pure, deterministic text → candidate
//! notes, with zero network / zero LLM by default (the same local-first bar
//! as `extraction.rs`, which handles sentence-level fact/preference items;
//! this handles note-level session takeaways). An optional LLM path exists
//! at the tool layer (`tools::handle_capture` with `llm: true`) and falls
//! back here on any failure — the rule-based distiller is the floor, not a
//! degraded mode.
//!
//! Pipeline: [`split_candidates`] (headed sections / paragraphs / JSONL) →
//! [`classify`] (root-cause / pitfall / decision / pattern / takeaway via
//! cheap keyword markers, failure markers aligned with the #521 deja-vu
//! guard) → [`summary_line`] + [`key_for`] (stable slug key) → capped
//! [`DistillReport`]. Writing the notes (with trigram near-dup merging ON —
//! that is the anti-flood control) happens in `tools::handle_capture`.

use serde::Serialize;

/// Hard cap on entities written per capture invocation (anti-flood, #520).
/// Callers can lower it per call; they cannot raise it.
pub const MAX_CAPTURE_NOTES: usize = 20;

/// Candidates shorter than this (in chars, after trimming) are discarded as
/// non-durable chatter ("ok", "done", "thanks"). Precision over recall.
const MIN_CANDIDATE_CHARS: usize = 16;

/// Max length of the extracted summary line (chars).
const MAX_SUMMARY_CHARS: usize = 160;

/// Max length of the slugified key (chars).
const MAX_KEY_CHARS: usize = 64;

/// A single distilled, durable note ready to be remembered.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CaptureNote {
    /// One of [`CAPTURE_ENTITY_TYPES`].
    pub entity_type: String,
    /// Stable slug key derived from the summary — the same solved problem
    /// re-captured with the same headline updates in place instead of
    /// creating a sibling row.
    pub key: String,
    /// One-line summary (the key basis and the recall headline).
    pub summary: String,
    /// The full candidate note text.
    pub content: String,
}

/// The distiller's output: capped notes plus accounting for what was dropped.
#[derive(Debug, Clone, Serialize)]
pub struct DistillReport {
    pub notes: Vec<CaptureNote>,
    /// Candidates that survived the minimum-length filter, before capping.
    pub candidates: usize,
    /// Candidates dropped by the per-invocation cap (logged, not silent).
    pub dropped: usize,
}

/// The closed set of entity types a capture may write. LLM output is
/// validated against this list (anything else degrades to "takeaway") —
/// model output is untrusted data, same rule as `dream`'s insight types.
pub const CAPTURE_ENTITY_TYPES: [&str; 5] =
    ["root-cause", "pitfall", "decision", "pattern", "takeaway"];

// ─── Classification markers ─────────────────────────────────────
//
// All lowercase substring markers, matched against the lowercased note.
// Priority: root-cause > pitfall > decision > pattern > takeaway — a note
// that names a failure AND its cause is a root-cause; a failure without a
// cause is a pitfall.

/// Markers that a note explains WHY something failed (diagnosis, not just
/// symptom). Checked before the failure markers.
const ROOT_CAUSE_MARKERS: &[&str] = &[
    "root cause",
    "root-cause",
    "caused by",
    " because ",
    "turned out",
    "the fix was",
    "the culprit",
    "traced to",
    "due to",
];

/// Markers that a note DESCRIBES A FAILURE. Kept aligned with the #521
/// deja-vu guard's `db::FAILURE_MARKERS` (same substring semantics: "fail"
/// covers failed/failure/failing; "bug" deliberately excluded because
/// "debug" false-positives on routine payloads) so a captured pitfall is
/// findable by `mimir_check_failure_pattern`. The root-cause-only markers
/// live in [`ROOT_CAUSE_MARKERS`] and win first.
const FAILURE_MARKERS: &[&str] = &[
    "fail",
    "error",
    "pitfall",
    "broke",
    "mistake",
    "wrong",
    "regression",
    "doesn't work",
    "does not work",
    "didn't work",
    "did not work",
    "incident",
    "postmortem",
];

/// Markers of a committed choice between alternatives.
const DECISION_MARKERS: &[&str] = &[
    "decided",
    "decision",
    "chose",
    "we will",
    "going with",
    "opted",
    "instead of",
    "standing rule",
    "agreed to",
];

/// Markers of a reusable recipe / convention.
const PATTERN_MARKERS: &[&str] = &[
    "pattern",
    "whenever",
    "recipe",
    "workflow",
    "convention",
    "rule of thumb",
    "lesson",
    "always ",
    "works: ",
];

/// Classify a candidate note into one of [`CAPTURE_ENTITY_TYPES`].
pub fn classify(text: &str) -> &'static str {
    let lower = text.to_lowercase();
    if ROOT_CAUSE_MARKERS.iter().any(|m| lower.contains(m)) {
        return "root-cause";
    }
    if FAILURE_MARKERS.iter().any(|m| lower.contains(m)) {
        return "pitfall";
    }
    if DECISION_MARKERS.iter().any(|m| lower.contains(m)) {
        return "decision";
    }
    if PATTERN_MARKERS.iter().any(|m| lower.contains(m)) {
        return "pattern";
    }
    "takeaway"
}

/// Truncate to at most `max` chars (not bytes — safe on any UTF-8).
fn clip_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_string()
    } else {
        s.chars().take(max).collect()
    }
}

/// First non-empty line of the note, stripped of markdown lead-in
/// (`#`/`-`/`*`/`>` and whitespace), clipped to [`MAX_SUMMARY_CHARS`].
pub fn summary_line(text: &str) -> String {
    for line in text.lines() {
        let stripped = line
            .trim_start_matches(|c: char| {
                c == '#' || c == '-' || c == '*' || c == '>' || c.is_whitespace()
            })
            .trim();
        if !stripped.is_empty() {
            return clip_chars(stripped, MAX_SUMMARY_CHARS);
        }
    }
    String::new()
}

/// FNV-1a over the input — a tiny stable hash for fallback keys. No crypto
/// claim; only used to make a non-sluggable summary produce a stable key.
fn fnv1a(s: &str) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in s.as_bytes() {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

/// Slugify a summary into a stable ASCII key: lowercase, alphanumeric runs
/// joined by single `-`, clipped to [`MAX_KEY_CHARS`]. A summary with no
/// ASCII-alphanumeric content (emoji-only, CJK, …) falls back to a stable
/// `note-<hash>` key so it still round-trips deterministically.
pub fn key_for(summary: &str) -> String {
    let mut slug = String::with_capacity(summary.len());
    let mut last_dash = true; // suppress a leading dash
    for c in summary.chars() {
        if c.is_ascii_alphanumeric() {
            slug.push(c.to_ascii_lowercase());
            last_dash = false;
        } else if !last_dash {
            slug.push('-');
            last_dash = true;
        }
        if slug.len() >= MAX_KEY_CHARS {
            break;
        }
    }
    let slug = slug.trim_matches('-').to_string();
    if slug.is_empty() {
        format!("note-{:016x}", fnv1a(summary))
    } else {
        slug
    }
}

/// True when every non-empty line parses as a JSON object — the JSONL shape
/// hook payloads and transcript exports commonly use.
fn looks_like_jsonl(payload: &str) -> bool {
    let mut saw_any = false;
    for line in payload.lines() {
        let t = line.trim();
        if t.is_empty() {
            continue;
        }
        saw_any = true;
        if !t.starts_with('{')
            || serde_json::from_str::<serde_json::Map<String, serde_json::Value>>(t).is_err()
        {
            return false;
        }
    }
    saw_any
}

/// Pull the note text out of one JSONL record: the first present non-empty
/// string among the conventional content fields, else the compact record
/// itself (still classifiable — markers survive JSON encoding).
fn jsonl_note_text(record: &serde_json::Map<String, serde_json::Value>) -> String {
    const CONTENT_FIELDS: &[&str] =
        &["content", "text", "insight", "lesson", "summary", "message"];
    for field in CONTENT_FIELDS {
        if let Some(serde_json::Value::String(s)) = record.get(*field) {
            let t = s.trim();
            if !t.is_empty() {
                return t.to_string();
            }
        }
    }
    serde_json::Value::Object(record.clone()).to_string()
}

/// Split a payload into candidate note texts.
///
/// Three shapes, auto-detected:
/// 1. **JSONL** — every non-empty line is a JSON object → one candidate per
///    record (conventional content field, else the compact record).
/// 2. **Headed markdown** — any `#`-heading lines present → one candidate
///    per headed section (heading + body until the next heading); a
///    non-empty preamble before the first heading is its own candidate.
/// 3. **Plain text** — candidates are blank-line-separated paragraphs.
///
/// Candidates shorter than [`MIN_CANDIDATE_CHARS`] are discarded.
pub fn split_candidates(payload: &str) -> Vec<String> {
    let trimmed = payload.trim();
    if trimmed.is_empty() {
        return Vec::new();
    }

    let raw: Vec<String> = if looks_like_jsonl(trimmed) {
        trimmed
            .lines()
            .filter_map(|l| {
                let t = l.trim();
                if t.is_empty() {
                    return None;
                }
                serde_json::from_str::<serde_json::Map<String, serde_json::Value>>(t)
                    .ok()
                    .map(|rec| jsonl_note_text(&rec))
            })
            .collect()
    } else if trimmed.lines().any(|l| l.trim_start().starts_with('#')) {
        // Headed sections: heading line + everything until the next heading.
        let mut sections: Vec<String> = Vec::new();
        let mut current = String::new();
        for line in trimmed.lines() {
            if line.trim_start().starts_with('#') {
                if !current.trim().is_empty() {
                    sections.push(current.trim().to_string());
                }
                current = String::new();
            }
            current.push_str(line);
            current.push('\n');
        }
        if !current.trim().is_empty() {
            sections.push(current.trim().to_string());
        }
        sections
    } else {
        // Blank-line-separated paragraphs.
        trimmed
            .split("\n\n")
            .map(|p| p.trim().to_string())
            .filter(|p| !p.is_empty())
            .collect()
    };

    raw.into_iter()
        .filter(|c| c.chars().count() >= MIN_CANDIDATE_CHARS)
        .collect()
}

/// The rule-based distiller: payload → classified, keyed, capped notes.
/// Deterministic and fully local (no LLM, no network, no DB access).
/// In-batch repeats of the same key keep the first occurrence (the DB-side
/// trigram dedup handles near-duplicates across invocations).
pub fn distill(payload: &str, max_notes: usize) -> DistillReport {
    let cap = max_notes.clamp(1, MAX_CAPTURE_NOTES);
    let candidates = split_candidates(payload);
    let total = candidates.len();

    let mut notes: Vec<CaptureNote> = Vec::new();
    for candidate in candidates {
        let summary = summary_line(&candidate);
        if summary.is_empty() {
            continue;
        }
        let key = key_for(&summary);
        if notes.iter().any(|n| n.key == key) {
            continue; // in-batch duplicate headline: first wins
        }
        notes.push(CaptureNote {
            entity_type: classify(&candidate).to_string(),
            key,
            summary,
            content: candidate,
        });
    }

    let kept = notes.len().min(cap);
    let dropped = notes.len() - kept;
    notes.truncate(kept);
    DistillReport {
        notes,
        candidates: total,
        dropped,
    }
}

// ─── Optional LLM distillation (#520 `--llm`) ────────────────────
//
// The prompt/parse pair for the opt-in LLM path. The transport call itself
// lives on `Database` (`llm_generate`, gated on `llm_config.enabled` with
// the #528 MIMIR_LLM_TIMEOUT_SECS timeout); `tools::handle_capture` wires
// prompt → call → parse and falls back to [`distill`] on ANY failure.

/// Build the distillation prompt. Strict-JSON contract, same style as
/// `synthesize`'s lesson extraction.
pub fn llm_prompt(payload: &str) -> String {
    format!(
        r#"You are a memory distillation system for an AI agent. Given a session transcript or insight payload, extract the few durable notes worth remembering across sessions.

CRITICAL INSTRUCTIONS:
- Extract at most {max} notes; fewer is better. Only include notes that will still matter in a future session.
- Each note's "type" MUST be one of: "root-cause" (why something failed), "pitfall" (a failure to avoid), "decision" (a committed choice), "pattern" (a reusable recipe/convention), "takeaway" (anything else durable).
- "summary" is one line (max 160 chars); "content" is the full self-contained note.
- Return ONLY valid JSON. No markdown, no commentary.

Payload:
{payload}

Return a JSON object: {{"notes": [{{"type": "...", "summary": "...", "content": "..."}}]}}
If nothing is worth remembering, return: {{"notes": []}}"#,
        max = MAX_CAPTURE_NOTES,
        payload = payload
    )
}

/// Parse the LLM's distillation output. Tolerates a ```json fence; anything
/// else non-conforming returns `None` so the caller falls back to the
/// rule-based path. Unknown types degrade to "takeaway" (LLM output is
/// untrusted); empty content falls back to the summary.
pub fn parse_llm_notes(raw: &str, max_notes: usize) -> Option<DistillReport> {
    let cap = max_notes.clamp(1, MAX_CAPTURE_NOTES);
    let mut text = raw.trim();
    if let Some(stripped) = text.strip_prefix("```json").or_else(|| text.strip_prefix("```")) {
        text = stripped.trim_start();
    }
    if let Some(stripped) = text.strip_suffix("```") {
        text = stripped.trim_end();
    }
    let parsed: serde_json::Value = serde_json::from_str(text).ok()?;
    let arr = parsed.get("notes")?.as_array()?;

    let mut notes: Vec<CaptureNote> = Vec::new();
    for item in arr {
        let summary = clip_chars(
            item.get("summary").and_then(|v| v.as_str()).unwrap_or("").trim(),
            MAX_SUMMARY_CHARS,
        );
        if summary.is_empty() {
            continue;
        }
        let content = item
            .get("content")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|c| !c.is_empty())
            .unwrap_or(&summary)
            .to_string();
        let raw_type = item.get("type").and_then(|v| v.as_str()).unwrap_or("");
        let entity_type = if CAPTURE_ENTITY_TYPES.contains(&raw_type) {
            raw_type.to_string()
        } else {
            "takeaway".to_string()
        };
        let key = key_for(&summary);
        if notes.iter().any(|n| n.key == key) {
            continue;
        }
        notes.push(CaptureNote {
            entity_type,
            key,
            summary,
            content,
        });
    }

    let total = notes.len();
    let kept = total.min(cap);
    let dropped = total - kept;
    notes.truncate(kept);
    Some(DistillReport {
        notes,
        candidates: total,
        dropped,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_headed_markdown_sections_with_preamble() {
        let payload = "Session context: fixed the deploy pipeline today.\n\n\
                       # Root cause of the deploy failure\nThe deploy failed because the schema version was never bumped.\n\n\
                       # Decision on toolchain\nWe decided to standardize on the MSVC toolchain for Windows builds.";
        let sections = split_candidates(payload);
        assert_eq!(sections.len(), 3, "{sections:?}");
        assert!(sections[0].starts_with("Session context"));
        assert!(sections[1].starts_with("# Root cause"));
        assert!(sections[2].starts_with("# Decision"));
    }

    #[test]
    fn splits_plain_paragraphs_and_drops_chatter() {
        let payload = "ok\n\nThe cargo build only works with the MSVC toolchain on Windows.\n\n\
                       done\n\nAlways trim PATH before invoking vcvars to avoid the 8191-char overflow.";
        let sections = split_candidates(payload);
        // "ok" and "done" are below the minimum-length filter.
        assert_eq!(sections.len(), 2, "{sections:?}");
    }

    #[test]
    fn splits_jsonl_records_via_content_fields() {
        let payload = r#"{"content": "The retry loop failed because the token expired mid-flight."}
{"text": "Decided to cache the token with a 5-minute refresh margin."}
{"kind": "misc", "note_id": 7, "detail": "record with no conventional field but plenty of length"}"#;
        let sections = split_candidates(payload);
        assert_eq!(sections.len(), 3, "{sections:?}");
        assert!(sections[0].contains("token expired"));
        assert!(sections[1].contains("refresh margin"));
        // Fallback: the compact record itself.
        assert!(sections[2].starts_with('{') && sections[2].contains("note_id"));
    }

    #[test]
    fn classifies_all_five_types() {
        assert_eq!(
            classify("The deploy failed; root cause was the unbumped schema version."),
            "root-cause"
        );
        assert_eq!(classify("The migration failed on the FK constraint."), "pitfall");
        assert_eq!(classify("We decided to ship the fallback path first."), "decision");
        assert_eq!(
            classify("Rule of thumb: run the smoke suite before every release."),
            "pattern"
        );
        assert_eq!(classify("The vault holds about 1,300 entities now."), "takeaway");
    }

    #[test]
    fn root_cause_wins_over_pitfall() {
        // A failure WITH a diagnosis is a root-cause, not a pitfall.
        let text = "The build broke; turned out the linker needed vcvars in PATH.";
        assert_eq!(classify(text), "root-cause");
    }

    #[test]
    fn failure_markers_align_with_the_deja_vu_guard() {
        // Alignment pin (#521): every capture failure marker must be one the
        // deja-vu guard (`db::FAILURE_MARKERS`) also recognizes, so a
        // captured pitfall is findable by mimir_check_failure_pattern. The
        // guard's list may be a superset (e.g. "root cause"/"root-cause"
        // live in ROOT_CAUSE_MARKERS here, which classify() checks first).
        for m in FAILURE_MARKERS {
            assert!(
                crate::db::FAILURE_MARKERS.contains(m),
                "capture failure marker {m:?} missing from db::FAILURE_MARKERS — \
                 keep the two lists aligned so captured pitfalls stay findable \
                 by the deja-vu guard"
            );
        }
    }

    #[test]
    fn summary_and_key_are_stable_and_bounded() {
        let text = "## The Fix: bump SCHEMA_VERSION on every new ensure_column!\nDetails follow.";
        let summary = summary_line(text);
        assert_eq!(summary, "The Fix: bump SCHEMA_VERSION on every new ensure_column!");
        let key = key_for(&summary);
        assert_eq!(key, "the-fix-bump-schema-version-on-every-new-ensure-column");
        // Deterministic.
        assert_eq!(key, key_for(&summary_line(text)));
        // Bounded + ASCII even for long/unicode summaries.
        let long_key = key_for(&"é🎉 ".repeat(200));
        assert!(long_key.starts_with("note-"), "{long_key}");
        assert!(key_for(&"x".repeat(500)).len() <= MAX_KEY_CHARS);
    }

    #[test]
    fn distill_caps_notes_and_reports_dropped() {
        let payload = (0..30)
            .map(|i| format!("Durable takeaway number {i} about the capture system."))
            .collect::<Vec<_>>()
            .join("\n\n");
        let report = distill(&payload, 50); // asks above the hard cap
        assert_eq!(report.candidates, 30);
        assert_eq!(report.notes.len(), MAX_CAPTURE_NOTES);
        assert_eq!(report.dropped, 10);

        // A caller can lower the cap, never raise it.
        let report = distill(&payload, 5);
        assert_eq!(report.notes.len(), 5);
        assert_eq!(report.dropped, 25);
    }

    #[test]
    fn distill_skips_in_batch_duplicate_keys() {
        let payload = "The build failed on the FK constraint again today.\n\n\
                       The build failed on the FK constraint again today.";
        let report = distill(&payload, 20);
        assert_eq!(report.notes.len(), 1, "{report:?}");
    }

    #[test]
    fn distill_is_deterministic() {
        let payload = "# Root cause\nThe deploy failed because of the stale cache.\n\n\
                       # Next step\nAlways invalidate the cache before deploying.";
        let a = distill(payload, 20);
        let b = distill(payload, 20);
        assert_eq!(a.notes, b.notes);
    }

    #[test]
    fn parse_llm_notes_happy_path_fences_and_junk() {
        let raw = r#"```json
{"notes": [
  {"type": "root-cause", "summary": "Token expiry broke retries", "content": "The retry loop failed because the token expired mid-flight."},
  {"type": "made-up-type", "summary": "Something else durable", "content": "Body."},
  {"type": "decision", "summary": ""}
]}
```"#;
        let report = parse_llm_notes(raw, 20).expect("fenced JSON must parse");
        assert_eq!(report.notes.len(), 2, "{report:?}"); // empty summary skipped
        assert_eq!(report.notes[0].entity_type, "root-cause");
        // Unknown type degrades to takeaway (untrusted LLM output).
        assert_eq!(report.notes[1].entity_type, "takeaway");

        // Junk → None (caller falls back to the rule-based distiller).
        assert!(parse_llm_notes("I could not find any notes, sorry!", 20).is_none());
        assert!(parse_llm_notes("{\"lessons\": []}", 20).is_none());
        // Missing content falls back to the summary.
        let report =
            parse_llm_notes(r#"{"notes": [{"type": "takeaway", "summary": "Just this"}]}"#, 20)
                .unwrap();
        assert_eq!(report.notes[0].content, "Just this");
    }
}
