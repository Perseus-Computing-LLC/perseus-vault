use subtle::ConstantTimeEq;

/// Constant-time comparison of an attacker-supplied token against the expected
/// secret. Prevents a timing side-channel that a byte-by-byte `==` would leak
/// (early-exit on the first mismatching byte lets an attacker recover the secret
/// one byte at a time). The length of the two strings is not itself secret, so
/// leaking it via the short-circuit in `ConstantTimeEq for [u8]` is acceptable.
pub fn constant_time_str_eq(provided: &str, expected: &str) -> bool {
    provided.as_bytes().ct_eq(expected.as_bytes()).into()
}

/// Whether a bind host refers only to the local loopback interface. Used to
/// decide whether exposing an unauthenticated HTTP surface is safe. Treats the
/// unspecified addresses (`0.0.0.0` / `::`) and any concrete non-loopback host
/// as NOT loopback.
pub fn host_is_loopback(host: &str) -> bool {
    // Strip an IPv6 bracket form like "[::1]".
    let h = host.trim().trim_start_matches('[').trim_end_matches(']');
    if h.eq_ignore_ascii_case("localhost") {
        return true;
    }
    match h.parse::<std::net::IpAddr>() {
        Ok(ip) => ip.is_loopback(),
        // A hostname we can't resolve here — treat as non-loopback (be safe).
        Err(_) => false,
    }
}

/// Format a unix timestamp in seconds as an ISO 8601 UTC string.
/// Avoids chrono dependency by hand-rolling a minimal formatter.
/// Only safe for timestamps from 1970 to ~3000 (no leap-second handling).
pub fn format_iso8601(secs: i64) -> String {
    if secs <= 0 {
        return "1970-01-01T00:00:00Z".to_string();
    }
    let days_since_epoch = secs / 86400;
    let secs_of_day = secs % 86400;
    let mut y = 1970i64;
    let mut d = days_since_epoch;
    loop {
        let days_in_year = if (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0) {
            366
        } else {
            365
        };
        if d < days_in_year {
            break;
        }
        d -= days_in_year;
        y += 1;
    }
    let leap = (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0);
    let month_days = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    let mut m = 0usize;
    while m < 12 && d >= month_days[m] {
        d -= month_days[m];
        m += 1;
    }
    let month = m + 1;
    let day = d + 1;
    let h = secs_of_day / 3600;
    let min = (secs_of_day % 3600) / 60;
    let s = secs_of_day % 60;
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        y, month, day, h, min, s
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constant_time_eq_matches_semantics_of_plain_eq() {
        assert!(constant_time_str_eq("s3cret", "s3cret"));
        assert!(!constant_time_str_eq("s3cret", "s3creX"));
        assert!(!constant_time_str_eq("s3cret", "s3cret-longer"));
        assert!(!constant_time_str_eq("", "x"));
        assert!(constant_time_str_eq("", ""));
    }

    #[test]
    fn loopback_detection() {
        assert!(host_is_loopback("127.0.0.1"));
        assert!(host_is_loopback("127.5.6.7"));
        assert!(host_is_loopback("::1"));
        assert!(host_is_loopback("[::1]"));
        assert!(host_is_loopback("localhost"));
        assert!(!host_is_loopback("0.0.0.0"));
        assert!(!host_is_loopback("::"));
        assert!(!host_is_loopback("192.168.1.10"));
        assert!(!host_is_loopback("example.com"));
    }
}
