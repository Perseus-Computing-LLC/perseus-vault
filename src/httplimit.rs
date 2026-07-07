//! DoS-resistance layers for the HTTP surfaces (MCP transport + web dashboard).
//!
//! Two protections, both env-tunable and applied by `apply_http_limits`:
//!   * an explicit request-body size cap (so a huge POST can't force an
//!     unbounded in-memory buffer before `remember`'s own field caps apply), and
//!   * a global token-bucket rate limit (so a request flood can't saturate the
//!     blocking thread pool — each MCP call can run synchronous LLM round-trips).
//!
//! The rate limit is intentionally GLOBAL, not per-IP: the vault is a
//! single-tenant local-first service, and per-client fairness is the job of a
//! fronting reverse proxy. See docs/transport.md.

use axum::{
    extract::{DefaultBodyLimit, Request},
    http::{header, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    Router,
};
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// Default max request body (bytes). The largest legitimate payload is an
/// `ingest`/`remember` body; `remember` caps its own body at 4 MiB (#434), so
/// 8 MiB leaves headroom for the JSON-RPC envelope while still bounding memory.
const DEFAULT_MAX_BODY_BYTES: usize = 8 * 1024 * 1024;
/// Default sustained request rate (requests/second). Generous for interactive
/// agent use; a flood well above this is what we shed.
const DEFAULT_RATE_PER_SEC: f64 = 50.0;
/// Default burst allowance (bucket capacity).
const DEFAULT_BURST: f64 = 100.0;

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(default)
}

fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v >= 0.0)
        .unwrap_or(default)
}

/// A simple monotonic-clock token bucket. `allow()` refills based on elapsed
/// time, then spends one token if available.
struct TokenBucket {
    tokens: f64,
    last: Instant,
    rate: f64,
    burst: f64,
}

impl TokenBucket {
    fn new(rate: f64, burst: f64) -> Self {
        Self {
            tokens: burst,
            last: Instant::now(),
            rate,
            burst,
        }
    }

    fn allow(&mut self) -> bool {
        let now = Instant::now();
        let elapsed = now.duration_since(self.last).as_secs_f64();
        self.last = now;
        self.tokens = (self.tokens + elapsed * self.rate).min(self.burst);
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

fn too_many_requests() -> Response {
    let mut resp = (
        StatusCode::TOO_MANY_REQUESTS,
        r#"{"error":"rate_limited","message":"Too many requests"}"#,
    )
        .into_response();
    resp.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("application/json"),
    );
    // A fixed hint; the bucket refills continuously so this is advisory.
    resp.headers_mut()
        .insert(header::RETRY_AFTER, header::HeaderValue::from_static("1"));
    resp
}

/// Wrap `router` with the body-size cap and (unless disabled) the global rate
/// limit. Env knobs:
///   * `MIMIR_MAX_HTTP_BODY_BYTES` (default 8 MiB)
///   * `MIMIR_HTTP_RATE_PER_SEC`   (default 50; set 0 to disable rate limiting)
///   * `MIMIR_HTTP_RATE_BURST`     (default 100)
///
/// Layer order matters: the rate limit is applied LAST so it sits OUTERMOST and
/// sheds a flood cheaply, before the body is buffered or routed.
pub fn apply_http_limits(router: Router) -> Router {
    let max_body = env_usize("MIMIR_MAX_HTTP_BODY_BYTES", DEFAULT_MAX_BODY_BYTES);
    let router = router.layer(DefaultBodyLimit::max(max_body));

    let rate = env_f64("MIMIR_HTTP_RATE_PER_SEC", DEFAULT_RATE_PER_SEC);
    if rate <= 0.0 {
        return router; // rate limiting disabled
    }
    let burst = env_f64("MIMIR_HTTP_RATE_BURST", DEFAULT_BURST).max(1.0);
    let bucket = Arc::new(Mutex::new(TokenBucket::new(rate, burst)));

    router.layer(middleware::from_fn(move |req: Request, next: Next| {
        let bucket = Arc::clone(&bucket);
        async move {
            let allowed = bucket
                .lock()
                .map(|mut b| b.allow())
                // A poisoned lock shouldn't hard-fail the surface; fail open.
                .unwrap_or(true);
            if allowed {
                next.run(req).await
            } else {
                too_many_requests()
            }
        }
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_bucket_sheds_when_empty_and_refills_over_time() {
        // rate=100/s, burst=2: two immediate allows, then shed.
        let mut b = TokenBucket::new(100.0, 2.0);
        assert!(b.allow());
        assert!(b.allow());
        assert!(!b.allow(), "third request in the same instant must be shed");

        // Simulate elapsed time by rewinding `last` ~20ms (100/s -> ~2 tokens).
        b.last -= std::time::Duration::from_millis(20);
        assert!(b.allow(), "bucket must refill as time passes");
    }

    #[test]
    fn burst_capacity_is_capped() {
        // Even after a long idle, tokens never exceed burst.
        let mut b = TokenBucket::new(1000.0, 3.0);
        b.last -= std::time::Duration::from_secs(60);
        assert!(b.allow());
        assert!(b.allow());
        assert!(b.allow());
        assert!(!b.allow(), "cannot exceed burst capacity of 3");
    }
}
