# Tuvora direct-only POC fork

Pinned starting revision: 5d78becd3b25540b1f8e91d287cc002489d6c33d.

DIRECT_ONLY=true bypasses server probing/liveness requests, permits eligible direct HTTP(S) URLs, blocks proxy/transcode/HLS/extraction/P2P paths and prevents mutable addon settings from overriding the operator policy. Header-dependent and internal sources fail closed. See crates/remux-server/src/direct_only.rs and its tests.

Build Linux amd64 with `docker build --platform linux/amd64 -f docker/Dockerfile.direct-only .`. The POC image forces direct-only mode, runs without root, records and rejects media-tool attempts, includes corresponding source and retains upstream AGPL notices. It is a private pilot image, not a claim of universal Jellyfin-client compatibility.

An independent egress boundary is required. The initial k3s deployment admits only synthetic fixture control traffic and DNS, with no public ingress. Real providers and outside-LAN playback remain separate acceptance checks. A successful synthetic test does not prove real device playback or a complete managed migration service.
