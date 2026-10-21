//! Operator-enforced metadata/redirect-only mode. No media I/O is permitted.
use crate::stream::{StreamDescriptor, is_internal_host};

/// Inspect the descriptor only: never probe, HEAD, GET or resolve a media URL.
pub(crate) fn redirect_url(descriptor: &StreamDescriptor) -> anyhow::Result<&str> {
    let StreamDescriptor::Http {
        url,
        request_headers,
        response_headers,
    } = descriptor
    else {
        anyhow::bail!("direct-only mode requires a public HTTP(S) source");
    };
    if !request_headers.is_empty() || !response_headers.is_empty() {
        anyhow::bail!("direct-only mode does not support server-applied media headers");
    }
    anyhow::ensure!(
        !url.chars()
            .any(|c| c.is_control() || c.is_whitespace())
            && http::HeaderValue::try_from(url.as_str()).is_ok(),
        "invalid redirect URL"
    );
    let parsed = url::Url::parse(url)?;
    anyhow::ensure!(
        matches!(parsed.scheme(), "http" | "https"),
        "unsupported media URL scheme"
    );
    anyhow::ensure!(
        parsed
            .username()
            .is_empty()
            && parsed
                .password()
                .is_none(),
        "URL credentials are unsupported"
    );
    anyhow::ensure!(
        parsed
            .host_str()
            .is_some_and(|host| !is_internal_host(host)),
        "internal media origins require a relay"
    );
    Ok(url)
}

/// The pilot supports Stremio's JSON control API, plus TMDB metadata. Other
/// presets may scan files, resolve torrents, or run server-side media tools.
pub(crate) fn addon_allowed(kind: &str) -> bool {
    matches!(kind, "stremio" | "tmdb")
}

/// Guard alternate media routes before handlers can start processes or read
/// files. Normal stream endpoints have a separate redirect-or-reject guard.
pub(crate) fn blocked_path(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    let parts: Vec<_> = lower
        .split('/')
        .filter(|s| !s.is_empty())
        .collect();
    match parts.as_slice() {
        ["stream", ..] | ["audio", ..] => true,
        ["videos", _, "additionalparts"] => false, // JSON metadata only.
        ["videos", _, leaf] if *leaf == "stream" || leaf.starts_with("stream.") => {
            false
        }
        ["videos", ..] => true,
        ["livetv", "liverecordings", ..] => true,
        _ => false,
    }
}

pub(crate) async fn guard(
    axum::extract::State(enabled): axum::extract::State<bool>,
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    use axum::response::IntoResponse;
    if enabled
        && blocked_path(
            request
                .uri()
                .path(),
        )
    {
        return (http::StatusCode::FORBIDDEN, axum::Json(serde_json::json!({
            "error": "direct_only_mode", "message": "This operation requires server-side media access"
        }))).into_response();
    }
    next.run(request)
        .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direct_only_redirect_policy() {
        assert_eq!(
            redirect_url(&StreamDescriptor::http(
                "https://cdn.example.com/movie.mp4?token=synthetic"
            ))
            .unwrap(),
            "https://cdn.example.com/movie.mp4?token=synthetic"
        );
        for url in [
            "http://127.0.0.1/video",
            "http://addon/video",
            "http://10.0.0.1/video",
            "http://[::1]/video",
            "http://host.internal/video",
            "ftp://example.com/video",
            "https://user:password@example.com/video",
            "not-a-url",
            "https://example.com/video\r\nX-Injected: true",
        ] {
            assert!(redirect_url(&StreamDescriptor::http(url)).is_err());
        }
        let mut with_headers = StreamDescriptor::http("https://cdn.example.com/video");
        if let StreamDescriptor::Http {
            request_headers, ..
        } = &mut with_headers
        {
            request_headers.insert("Authorization".into(), "synthetic".into());
        }
        assert!(redirect_url(&with_headers).is_err());
        assert!(
            redirect_url(&StreamDescriptor::Local("/tmp/file.mp4".into())).is_err()
        );
        assert!(
            redirect_url(&StreamDescriptor::Rtsp {
                url: "rtsp://example.com/video".into()
            })
            .is_err()
        );
    }

    #[test]
    fn direct_only_blocks_alternate_media_routes() {
        for path in [
            "/stream/id",
            "/videos/id/master.m3u8",
            "/videos/no-streams",
            "/videos/id/hls/session/segment.ts",
            "/videos/id/source/subtitles/1/stream.srt",
            "/audio/id/stream",
            "/livetv/liverecordings/id/stream",
        ] {
            assert!(blocked_path(path), "{path}");
        }
        for path in [
            "/items/id/playbackinfo",
            "/users/me",
            "/videos/id/stream.mp4",
            "/videos/id/stream",
            "/items/id/download",
            "/videos/id/additionalparts",
        ] {
            assert!(!blocked_path(path), "{path}");
        }
    }

    #[test]
    fn direct_only_limits_presets_to_control_apis() {
        assert!(addon_allowed("stremio"));
        assert!(addon_allowed("tmdb"));
        for kind in ["opendal", "ytdlp", "torznab", "iptv", "unknown"] {
            assert!(!addon_allowed(kind));
        }
    }
}
