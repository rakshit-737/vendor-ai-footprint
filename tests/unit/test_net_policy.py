"""Offline tests for net.tou, net.robots, net.ratelimit."""

from __future__ import annotations

import pytest

from footprint.net.ratelimit import RateLimiter
from footprint.net.robots import RobotsCache
from footprint.net.tou import load_tou

# ------------------------------------------------------------------ tou


@pytest.fixture(scope="module")
def tou():
    return load_tou()


def test_match_suffix_and_longest(tou):
    assert tou.match("www.sec.gov").match == "sec.gov"
    assert tou.match("efts.sec.gov").match == "sec.gov"
    assert tou.match("fiserv.wd5.myworkdayjobs.com").match == "fiserv.wd5.myworkdayjobs.com"
    assert tou.match("notsec.gov") is None
    assert tou.match("unknown.example") is None


def test_manual_only_hosts(tou):
    for u in ("https://www.fiserv.com/x", "https://careers.fiserv.com/", "https://www.linkedin.com/in/x"):
        ok, why = tou.check(u, {}, seeded=True)
        assert not ok and why.startswith("blocked_tou")


def test_full_host_allowed(tou):
    assert tou.check("https://www.sec.gov/cgi-bin/browse-edgar", {}) == (True, "")


def test_limited_and_default(tou):
    assert not tou.check("https://labarum.ai/", {})[0]
    assert tou.check("https://labarum.ai/", {}, seeded=True)[0]
    assert not tou.check("https://unknown.example/", {})[0]
    assert tou.check("https://unknown.example/", {}, seeded=True)[0]
    key = tou.counter_key("https://unknown.example/")
    ok, why = tou.check("https://unknown.example/", {key: 3}, seeded=True)
    assert not ok and why.startswith("cap_reached")


def test_cap_counter_key_shared_across_subdomains(tou):
    assert tou.counter_key("https://www.labarum.ai/a") == tou.counter_key("https://labarum.ai/b") == "labarum.ai"
    ok, why = tou.check("https://www.labarum.ai/", {"labarum.ai": 15}, seeded=True)
    assert not ok and why.startswith("cap_reached")


# ------------------------------------------------------------------ robots


def cache_with(status, body):
    calls = []

    def fetch(u):
        calls.append(u)
        return status, body

    return RobotsCache(fetch), calls


def test_robots_bom_stripped_and_ua_token():
    rc, _ = cache_with(200, "﻿User-agent: footprint-osint\nDisallow: /x\n".encode("utf-8"))
    assert rc.allowed("https://h.example/x/1")[0] is False
    assert rc.allowed("https://h.example/y")[0] is True


def test_robots_4xx_allow_5xx_disallow_error_disallow():
    assert RobotsCache(lambda u: (404, b""))
    assert cache_with(404, b"")[0].allowed("https://a.example/p")[0] is True
    assert cache_with(503, b"")[0].allowed("https://a.example/p")[0] is False

    def boom(u):
        raise OSError("down")

    assert RobotsCache(boom).allowed("https://a.example/p")[0] is False


def test_robots_cached_per_origin_and_sha():
    body = b"User-agent: *\nCrawl-delay: 4\nAllow: /\n"
    rc, calls = cache_with(200, body)
    ok1, sha = rc.allowed("https://a.example/1")
    rc.allowed("https://a.example/2")
    rc.allowed("https://b.example/1")
    assert calls == ["https://a.example/robots.txt", "https://b.example/robots.txt"]
    assert ok1 and len(sha) == 64
    assert rc.crawl_delay("https://a.example/1") == 4.0


# ------------------------------------------------------------------ ratelimit


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_ratelimit_spacing_per_host():
    c = Clock()
    rl = RateLimiter(clock=c, sleep=c.sleep)
    assert rl.wait("a", 2.0) == 0.0
    assert rl.wait("a", 2.0) == pytest.approx(0.5)
    assert rl.wait("b", 2.0) == 0.0
    c.t += 10
    assert rl.wait("a", 2.0) == 0.0


def test_ratelimit_crawl_delay_wins():
    c = Clock()
    rl = RateLimiter(clock=c, sleep=c.sleep)
    rl.wait("a", 5.0, crawl_delay=3)
    assert rl.wait("a", 5.0, crawl_delay=3) == pytest.approx(3.0)
    assert RateLimiter.interval(5.0, None) == pytest.approx(0.2)
