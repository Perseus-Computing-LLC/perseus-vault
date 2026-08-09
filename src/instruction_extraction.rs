//! #889: keystone-suggestion instruction extraction with anchored patterns.
//!
//! Extracts candidate directive/keystone suggestions from `correct` captures
//! using word-boundary-anchored patterns. The anchoring is the whole point:
//! unanchored substring matching inverted meaning in production (#507 lesson)
//! — "whenever" contains "never", so a bare `never` pattern matched inside
//! "whenever needed we can use it" and stored the opposite instruction
//! ("never needed we can use it"). Every trigger is matched with word
//! boundaries on both sides, per locale.
//!
//! Extraction NEVER writes policy: it only produces candidate suggestions
//! that require operator approval before promotion to a keystone.
//!
//! Locale coverage: en first (richest trigger set), then de/ru/it/es minimal
//! sets. Boundary checks use Unicode alphanumeric classification, so Cyrillic
//! (ru) and accented words behave correctly without a regex dependency.

/// Directive triggers per locale: (locale, [(pattern_name, [triggers...])]).
/// Triggers are matched case-insensitively with word boundaries on both sides.
pub const TRIGGERS: &[(&str, &[(&str, &[&str])])] = &[
    (
        "en",
        &[
            ("never", &["never"]),
            ("always", &["always"]),
            ("whenever", &["whenever"]),
            ("do_not", &["do not", "don't", "dont"]),
            ("must_not", &["must not", "mustn't"]),
            ("must", &["must"]),
            ("only", &["only"]),
            ("unless", &["unless"]),
            ("should", &["should"]),
            ("after", &["after"]),
            ("before", &["before"]),
        ],
    ),
    (
        "de",
        &[
            ("nie", &["nie"]),
            ("immer", &["immer"]),
            ("sobald", &["sobald"]),
            ("wenn", &["wenn"]),
            ("darf_nicht", &["darf nicht"]),
            ("muss", &["muss"]),
        ],
    ),
    (
        "ru",
        &[
            ("никогда", &["никогда"]),
            ("всегда", &["всегда"]),
            ("когда", &["когда"]),
            ("нельзя", &["нельзя"]),
            ("обязательно", &["обязательно"]),
        ],
    ),
    (
        "it",
        &[
            ("mai", &["mai"]),
            ("sempre", &["sempre"]),
            ("quando", &["quando"]),
            ("non_devi", &["non devi", "non deve"]),
            ("devi", &["devi"]),
        ],
    ),
    (
        "es",
        &[
            ("nunca", &["nunca"]),
            ("siempre", &["siempre"]),
            ("cuando", &["cuando"]),
            ("no_debes", &["no debes", "no debe"]),
            ("debes", &["debes"]),
        ],
    ),
];

pub const LOCALES: [&str; 5] = ["en", "de", "ru", "it", "es"];

/// Upper bound on suggestions per input text (guard against pathological
/// directive-dense text flooding the queue).
pub const MAX_SUGGESTIONS_PER_TEXT: usize = 8;
/// Upper bound on instruction length (chars) — a directive is a sentence,
/// not a paragraph.
pub const MAX_INSTRUCTION_CHARS: usize = 240;

/// One extracted candidate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Suggestion {
    pub locale: &'static str,
    pub pattern: &'static str,
    pub instruction: String,
}

fn is_word_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// True when `text[i..i+len]` is a word-boundary-anchored match of `trigger`
/// (case-insensitive): the char before and the char after must not be word
/// chars. This is what prevents "never" from matching inside "whenever" and
/// "whenever" from matching inside "never".
fn anchored_match(text: &str, trigger: &str, i: usize) -> bool {
    let end = i + trigger.len();
    if end > text.len() {
        return false;
    }
    if !text[i..end].to_lowercase().eq(trigger) {
        return false;
    }
    let before_ok = i == 0 || !is_word_char(text[..i].chars().next_back().unwrap_or(' '));
    let after_ok = end == text.len() || !is_word_char(text[end..].chars().next().unwrap_or(' '));
    before_ok && after_ok
}

/// The sentence (clause) containing byte range [start, end): split on the
/// standard sentence/clause terminators plus newlines. Falls back to the
/// surrounding window when no terminator exists.
fn containing_clause(text: &str, start: usize, end: usize) -> String {
    let bytes = text.as_bytes();
    let mut s = start;
    while s > 0 {
        let prev = text[..s].chars().next_back().unwrap_or(' ');
        if matches!(prev, '.' | '!' | '?' | ';' | '\n' | '\r') {
            break;
        }
        s -= prev.len_utf8();
    }
    let mut e = end;
    while e < bytes.len() {
        let c = text[e..].chars().next().unwrap_or(' ');
        if matches!(c, '.' | '!' | '?' | ';' | '\n' | '\r') {
            break;
        }
        e += c.len_utf8();
    }
    text[s..e].trim().to_string()
}

/// Extract candidate directive suggestions from `text`, scanning every
/// locale's trigger set with word-boundary anchoring. Returns deduplicated
/// candidates, ordered by first match position, capped at
/// `MAX_SUGGESTIONS_PER_TEXT`.
pub fn extract_suggestions(text: &str) -> Vec<Suggestion> {
    let mut out: Vec<Suggestion> = Vec::new();
    for (locale, patterns) in TRIGGERS {
        for (pattern_name, triggers) in *patterns {
            for trigger in *triggers {
                let lower = text.to_lowercase();
                let mut search_from = 0;
                while let Some(rel) = lower[search_from..].find(trigger) {
                    let i = search_from + rel;
                    if anchored_match(text, trigger, i) {
                        let end = i + trigger.len();
                        let clause = containing_clause(text, i, end);
                        if !clause.is_empty() && clause.chars().count() <= MAX_INSTRUCTION_CHARS {
                            let sug = Suggestion {
                                locale,
                                pattern: pattern_name,
                                instruction: clause,
                            };
                            if !out.contains(&sug) {
                                out.push(sug);
                                if out.len() >= MAX_SUGGESTIONS_PER_TEXT {
                                    return out;
                                }
                            }
                        }
                    }
                    search_from = i + trigger.len();
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn whenever_never_inversion_regression() {
        // The production bug (#507): bare `never` matched the "never" inside
        // "whenever" and stored the opposite instruction. Anchored matching
        // must yield ONLY the "whenever" directive.
        let s = extract_suggestions("whenever needed we can use it");
        assert!(!s.is_empty(), "whenever directive must be extracted");
        assert!(
            s.iter().all(|x| x.pattern == "whenever"),
            "only the whenever pattern may match: {s:?}"
        );
        assert!(
            s.iter().all(|x| !x.instruction.starts_with("never")),
            "no inverted 'never' instruction may be stored: {s:?}"
        );
        assert_eq!(s[0].instruction, "whenever needed we can use it");
    }

    #[test]
    fn reverse_inversion_never_does_not_contain_whenever() {
        let s = extract_suggestions("never share credentials in logs");
        assert!(s.iter().any(|x| x.pattern == "never"));
        assert!(
            s.iter().all(|x| x.pattern != "whenever"),
            "whenever must not match inside 'never': {s:?}"
        );
    }

    #[test]
    fn en_triggers_are_anchored() {
        let s = extract_suggestions("Always cite sources. The word whenever matters.");
        assert!(s.iter().any(|x| x.pattern == "always"));
        assert!(s.iter().any(|x| x.pattern == "whenever"));
        // "The" is not a trigger; a bare "ever" is not a trigger either.
        assert!(s.iter().all(|x| x.instruction.len() < 60));
    }

    #[test]
    fn do_not_and_must_not_multiword_triggers() {
        let s = extract_suggestions("Do not use shared keys; must not cross agents.");
        assert!(s.iter().any(|x| x.pattern == "do_not"));
        assert!(s.iter().any(|x| x.pattern == "must_not"));
    }

    #[test]
    fn locale_coverage_de_ru_it_es() {
        let cases = [
            ("de", "Immer die Quelle angeben", "immer"),
            ("de", "Nie Passwörter teilen", "nie"),
            ("ru", "Всегда указывай источник", "всегда"),
            ("ru", "Никогда не делитесь паролями", "никогда"),
            ("it", "Sempre cita le fonti", "sempre"),
            ("it", "Mai condividere password", "mai"),
            ("es", "Siempre cita las fuentes", "siempre"),
            ("es", "Nunca compartas contraseñas", "nunca"),
        ];
        for (locale, text, pattern) in cases {
            let s = extract_suggestions(text);
            assert!(
                s.iter().any(|x| x.locale == locale && x.pattern == pattern),
                "locale {locale}: expected pattern {pattern} in {text:?}, got {s:?}"
            );
        }
    }

    #[test]
    fn false_friends_do_not_cross_locales() {
        // "quando" (it) and "cuando" (es) are distinct words; anchored
        // matching must not let the es trigger fire on the it text.
        let it = extract_suggestions("Quando cade la connessione riprova");
        assert!(it.iter().any(|x| x.locale == "it" && x.pattern == "quando"));
        assert!(
            it.iter().all(|x| x.pattern != "cuando"),
            "es 'cuando' must not match it text: {it:?}"
        );
    }

    #[test]
    fn clause_capture_and_caps() {
        let long = format!("always {}", "x".repeat(300));
        let s = extract_suggestions(&long);
        assert!(s.is_empty(), "over-long clause is rejected");
        let many = "always a. always b. always c. always d. always e. always f. always g. always h. always i.";
        let s = extract_suggestions(many);
        assert!(s.len() <= MAX_SUGGESTIONS_PER_TEXT);
    }
}
