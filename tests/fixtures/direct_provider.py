"""Synthetic addon and deny-only media origin. Never serves audio/video bytes."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

EVENTS = []
LOCK = threading.Lock()
MOVIE_ID = "tt990000001"
SHOW_ID = "tt990000002"
BASE = "http://fixture:8080"
PUBLIC_MEDIA = "http://poc-provider.example:8080/media/public.mp4"
PRIVATE_MEDIA = "http://fixture:8080/media/private.mp4"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = unquote(urlsplit(self.path).path)
        if p == "/evidence":
            with LOCK:
                return self.respond(200, {"events": list(EVENTS), "mediaBytesServed": 0})
        with LOCK:
            EVENTS.append({"at": time.time(), "method": self.command, "path": p,
                           "peer": self.client_address[0], "range": self.headers.get("Range"),
                           "userAgent": self.headers.get("User-Agent"), "mediaBytesServed": 0})
        if p.startswith("/media/"):
            return self.respond(451, {"error": "POC_MEDIA_FETCH_FORBIDDEN", "mediaBytesServed": 0})
        if p == "/manifest.json":
            return self.respond(200, {
                "id": "dev.thekush.poc.synthetic", "name": "Tuvora Synthetic POC",
                "version": "1.0.0", "description": "Synthetic metadata; media access denied",
                "types": ["movie", "series"], "resources": ["catalog", "meta", "stream"],
                "catalogs": [{"type": "movie", "id": "poc.movies", "name": "POC Movies", "extra": [{"name": "search"}]},
                             {"type": "series", "id": "poc.series", "name": "POC Series", "extra": [{"name": "search"}]}]})
        movie = {"id": MOVIE_ID, "type": "movie", "name": "Synthetic POC Movie",
                 "description": "Synthetic test fixture", "year": "2026", "runtime": "10 min"}
        show = {"id": SHOW_ID, "type": "series", "name": "Synthetic POC Series",
                "description": "Synthetic test fixture", "videos": [
                    {"id": SHOW_ID + ":1:1", "season": 1, "episode": 1, "name": "Synthetic episode",
                     "released": "2025-01-01T00:00:00.000Z"}]}
        if p.startswith("/catalog/movie/"):
            return self.respond(200, {"metas": [movie]})
        if p.startswith("/catalog/series/"):
            return self.respond(200, {"metas": [show]})
        if p.startswith("/meta/movie/"):
            return self.respond(200, {"meta": movie})
        if p.startswith("/meta/series/"):
            return self.respond(200, {"meta": show})
        if p.startswith("/stream/"):
            streams = [
                {"name": "Synthetic public direct source", "url": PUBLIC_MEDIA,
                 "behaviorHints": {"filename": "POC.Public.1080p.H264.mp4", "videoSize": 1000000}},
                {"name": "Synthetic internal proxy-only source", "url": PRIVATE_MEDIA,
                 "behaviorHints": {"filename": "POC.Private.720p.H264.mp4", "videoSize": 2000000}}]
            if os.environ.get("EXTENDED_FIXTURES") == "true":
                streams.append({"name": "Synthetic header-dependent source", "url": PUBLIC_MEDIA + "?header-required=1",
                                "requestHeaders": {"Authorization": "synthetic-server-only"},
                                "behaviorHints": {"filename": "POC.Headers.480p.H264.mp4", "videoSize": 3000000}})
            return self.respond(200, {"streams": streams})
        if p.startswith("/subtitles/"):
            return self.respond(200, {"subtitles": []})
        self.respond(404, {"error": "fixture_endpoint_not_found"})


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ.get("FIXTURE_BIND_HOST", "0.0.0.0"), int(os.environ.get("FIXTURE_PORT", "8080"))), Handler).serve_forever()
