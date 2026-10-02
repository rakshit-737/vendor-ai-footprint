"""Offline tests for net.fetcher against local http.server threads (127.0.0.1 only)."""

from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from footprint.models import Capture, SourceFamily
from footprint.net.fetcher import (
    DEFAULT_UA,
    LiveFetcher,
    ReplayFetcher,
    is_bot_wall,
    is_save_page_now,
    retry_delay,
)
from footprint.net.ratelimit import RateLimiter
from footprint.net.tou import TouRegister


class FakeStore:
    def __init__(self):
        self.caps: list[Capture] = []
        self.raw: dict[str, bytes] = {}

    def put_raw(self, data: bytes, **meta) -> Capture:
        sha = hashlib.sha256(data).hexdigest()
        cap = Capture(capture_id=sha, size=len(data), blob_path=f"evidence/blobs/{sha[:2]}/{sha}.gz", **meta)
        self.caps.append(cap)
        self.raw[sha] = data
        return cap

    def find_by_url(self, url):
        hits = [c for c in self.caps if c.url_requested == url]
        return hits[-1] if hits else None


def make_server(routes: dict):
    """routes: path -> callable(handler) -> (status, headers, body) ; records hits."""
    hits: list[tuple[str, str]] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append((self.path, self.headers.get("User-Agent", "")))
            fn = routes.get(self.path)
            status, headers, body = fn(self) if fn else (404, {}, b"nf")
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv, base, hits


@pytest.fixture
def serve():
    servers = []

    def _mk(routes):
        srv, base, hits = make_server(routes)
        servers.append(srv)
        return base, hits

    yield _mk
    for s in servers:
        s.shutdown()
        s.server_close()


def ok(body=b"<html>hello AI</html>", ct="text/html; charset=utf-8", extra=None):
    return lambda h: (200, {"Content-Type": ct, **(extra or {})}, body)


ROBOTS_OPEN = ok(b"User-agent: *\nAllow: /\n", "text/plain")


def reg(automation="full", cap=0, rps=1000.0):
    return TouRegister({
        "default": {"automation": "limited", "max_rps": 1000.0, "max_requests_per_run": 3},
        "host": [
            {"match": "127.0.0.1", "automation": automation, "max_rps": rps, "max_requests_per_run": cap},
            {"match": "linkedin.com", "automation": "full", "max_rps": 1.0},
        ],
    })


def fetcher(tou, store=None, sleeps=None, **kw):
    sl = sleeps if sleeps is not None else []
    return LiveFetcher(store or FakeStore(), tou, ratelimit=RateLimiter(sleep=lambda s: None),
                       sleep=sl.append, **kw)


KW = dict(vendor_id="V-001", family=SourceFamily.PRD, collector="test")


def test_happy_path_stores_capture_with_metadata(serve):
    base, hits = serve({"/robots.txt": ROBOTS_OPEN, "/p": ok(extra={"ETag": "x1", "X-Other": "y"})})
    store = FakeStore()
    out = fetcher(reg(), store).get(base + "/p", **KW)
    assert out.ok and out.status == 200 and out.reason == ""
    c = out.capture
    assert c.url_requested == base + "/p" and c.url_final == ""
    assert c.robots_decision == "allowed" and len(c.robots_sha256) == 64
    assert c.tou_match == "127.0.0.1" and c.captured_by == "test"
    assert c.headers.get("etag") == "x1" or c.headers.get("ETag") == "x1"
    assert "X-Other" not in c.headers and "x-other" not in c.headers
    assert store.raw[c.capture_id] == b"<html>hello AI</html>"
    assert all(ua == DEFAULT_UA for _, ua in hits)


def test_robots_disallow_blocks_without_get(serve):
    base, hits = serve({"/robots.txt": ok(b"User-agent: footprint-osint\nDisallow: /private\n", "text/plain"),
                        "/private/x": ok()})
    out = fetcher(reg()).get(base + "/private/x", **KW)
    assert not out.ok and out.reason == "blocked_robots"
    assert [p for p, _ in hits] == ["/robots.txt"]


def test_robots_fetched_once_per_host(serve):
    base, hits = serve({"/robots.txt": ROBOTS_OPEN, "/a": ok(), "/b": ok(b"b")})
    f = fetcher(reg())
    f.get(base + "/a", **KW)
    f.get(base + "/b", **KW)
    assert [p for p, _ in hits].count("/robots.txt") == 1


def test_redirect_to_other_origin_rechecks_robots(serve):
    other, other_hits = serve({"/robots.txt": ok(b"User-agent: *\nDisallow: /\n", "text/plain"), "/t": ok()})
    base, _ = serve({"/robots.txt": ROBOTS_OPEN, "/r": lambda h: (302, {"Location": other + "/t"}, b"")})
    out = fetcher(reg()).get(base + "/r", **KW)
    assert out.reason == "blocked_robots"
    assert "/t" not in [p for p, _ in other_hits]


def test_redirect_to_tou_blocked_host_is_not_followed(serve):
    base, _ = serve({"/robots.txt": ROBOTS_OPEN,
                     "/r": lambda h: (301, {"Location": "https://www.linkedin.com/company/x"}, b"")})
    out = fetcher(reg()).get(base + "/r", **KW)
    assert not out.ok and out.reason == "blocked_tou"


def test_redirect_followed_records_final_url(serve):
    base, _ = serve({"/robots.txt": ROBOTS_OPEN, "/r": lambda h: (302, {"Location": "/final"}, b""),
                     "/final": ok(b"final")})
    out = fetcher(reg()).get(base + "/r", **KW)
    assert out.ok and out.capture.url_final == base + "/final"


def test_429_retry_honours_retry_after(serve):
    n = {"c": 0}

    def flaky(h):
        n["c"] += 1
        return (429, {"Retry-After": "7"}, b"slow") if n["c"] < 3 else (200, {"Content-Type": "text/plain"}, b"ok")

    base, _ = serve({"/robots.txt": ROBOTS_OPEN, "/p": flaky})
    sleeps: list[float] = []
    out = fetcher(reg(), sleeps=sleeps).get(base + "/p", **KW)
    assert out.ok and n["c"] == 3 and sleeps == [7.0, 7.0]


def test_5xx_gives_up_after_max_retries(serve):
    n = {"c": 0}

    def down(h):
        n["c"] += 1
        return (502, {}, b"bad gateway")

    base, _ = serve({"/robots.txt": ROBOTS_OPEN, "/p": down})
    sleeps: list[float] = []
    out = fetcher(reg(), sleeps=sleeps).get(base + "/p", **KW)
    assert not out.ok and out.reason == "http_error" and out.status == 502
    assert n["c"] == 4 and sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("status,headers,body", [
    (403, {"Server": "cloudflare"}, b"<title>Just a moment...</title>"),
    (503, {"cf-mitigated": "challenge"}, b""),
    (403, {"Server": "AkamaiGHost"}, b"Access Denied. Reference #18.abc"),
])
def test_bot_wall_detected_no_retry(serve, status, headers, body):
    n = {"c": 0}

    def wall(h):
        n["c"] += 1
        return status, headers, body

    base, _ = serve({"/robots.txt": ROBOTS_OPEN, "/p": wall})
    store = FakeStore()
    out = fetcher(reg(), store).get(base + "/p", **KW)
    assert out.reason == "blocked_bot" and n["c"] == 1 and store.caps == []


def test_plain_404_is_http_error_and_stored(serve):
    base, _ = serve({"/robots.txt": ROBOTS_OPEN})
    store = FakeStore()
    out = fetcher(reg(), store).get(base + "/missing", **KW)
    assert out.reason == "http_error" and out.status == 404 and out.capture is not None


def test_per_run_cap(serve):
    base, hits = serve({"/robots.txt": ROBOTS_OPEN, "/a": ok(), "/b": ok(b"b"), "/c": ok(b"c")})
    f = fetcher(reg(cap=2))
    assert f.get(base + "/a", **KW).ok
    assert f.get(base + "/b", **KW).ok
    out = f.get(base + "/c", **KW)
    assert out.reason == "cap_reached"
    assert "/c" not in [p for p, _ in hits]


def test_limited_requires_seeded(serve):
    base, hits = serve({"/robots.txt": ROBOTS_OPEN, "/a": ok()})
    f = fetcher(reg(automation="limited"))
    assert f.get(base + "/a", **KW).reason == "blocked_tou"
    assert hits == []
    assert f.get(base + "/a", seeded=True, **KW).ok


def test_automation_none_blocks(serve):
    base, hits = serve({"/robots.txt": ROBOTS_OPEN, "/a": ok()})
    assert fetcher(reg(automation="none")).get(base + "/a", **KW).reason == "blocked_tou"
    assert hits == []


def test_hard_manual_hosts_and_spn_never_requested():
    f = fetcher(reg())
    for u in ("https://www.fiserv.com/en.html", "https://www.linkedin.com/company/x",
              "https://web.archive.org/save/https://example.com"):
        assert f.get(u, seeded=True, **KW).reason == "blocked_tou"
    assert f.request_counts == {}
    assert is_save_page_now("https://web.archive.org/save/x")
    assert not is_save_page_now("https://web.archive.org/web/2024id_/x")


def test_sec_user_agent():
    f = fetcher(reg(), sec_contact="team@example.edu")
    assert f.ua_for("https://www.sec.gov/x") == "footprint-osint/1.0 team@example.edu"
    assert f.ua_for("https://efts.sec.gov/x") == "footprint-osint/1.0 team@example.edu"
    assert f.ua_for("https://example.com/") == DEFAULT_UA
    with pytest.raises(ValueError):
        fetcher(reg()).ua_for("https://data.sec.gov/x")


def test_retry_delay_parsing():
    assert retry_delay("3", 0) == 3.0
    assert retry_delay(None, 2) == 4.0
    assert retry_delay("9999", 0) == 120.0
    assert retry_delay("garbage", 1) == 2.0


def test_is_bot_wall_ignores_plain_403():
    assert not is_bot_wall(403, {"Server": "nginx"}, b"Forbidden")
    assert not is_bot_wall(200, {"Server": "cloudflare"}, b"cf-chl")


def test_replay_fetcher_hit_and_miss():
    store = FakeStore()
    store.put_raw(b"x", vendor_id="V-001", family=SourceFamily.PRD, collector="t",
                  url_requested="https://a.example/p", status=200, retrieved_at="2026-10-02T00:00:00Z")
    rf = ReplayFetcher(store)
    hit = rf.get("https://a.example/p", **KW)
    assert hit.ok and hit.capture.capture_id == hashlib.sha256(b"x").hexdigest()
    miss = rf.get("https://a.example/other", **KW)
    assert miss == miss.__class__(ok=False, reason="replay_miss")
