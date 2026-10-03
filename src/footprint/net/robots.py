"""robots.txt cache (Protego), one fetch per scheme+host per run."""

from __future__ import annotations

import hashlib
from typing import Callable
from urllib.parse import urlsplit

from protego import Protego

UA_TOKEN = "footprint-osint"


class RobotsCache:
    """``fetch_raw(robots_url) -> (status, body)``; status 0 = network error (treated as disallow).

    Rules: 2xx parse (BOM stripped); 4xx allow all (RFC 9309 "unavailable"); 5xx / network error disallow all.
    ``on_fetch(origin, status, body, sha256)`` is called once per origin after the fetch (the LiveFetcher logs it
    and keeps the body in the evidence store, so replay can show which rules applied).
    """

    def __init__(self, fetch_raw: Callable[[str], tuple[int, bytes]],
                 on_fetch: Callable[[str, int, bytes, str], None] | None = None):
        self._fetch_raw = fetch_raw
        self._on_fetch = on_fetch
        self._cache: dict[str, tuple[str, Protego | None, str]] = {}  # origin -> (mode, parser, sha)
        self._status: dict[str, int] = {}
        self._bodies: dict[str, bytes] = {}

    @staticmethod
    def _origin(url: str) -> str:
        s = urlsplit(url)
        return f"{s.scheme}://{s.netloc}".lower()

    def _load(self, url: str) -> tuple[str, Protego | None, str]:
        origin = self._origin(url)
        if origin not in self._cache:
            try:
                status, body = self._fetch_raw(origin + "/robots.txt")
            except Exception:  # noqa: BLE001 - network failure -> conservative
                status, body = 0, b""
            sha = hashlib.sha256(body).hexdigest() if body else ""
            if 200 <= status < 300:
                text = body.decode("utf-8", errors="replace").lstrip("﻿")
                self._cache[origin] = ("parse", Protego.parse(text), sha)
            elif 400 <= status < 500:
                self._cache[origin] = ("allow", None, sha)
            else:
                self._cache[origin] = ("deny", None, sha)
            self._status[origin] = status
            self._bodies[origin] = body if 200 <= status < 300 else b""
            if self._on_fetch is not None:
                try:
                    self._on_fetch(origin, status, body, sha)
                except Exception:  # noqa: BLE001 - logging must never change a robots decision
                    pass
        return self._cache[origin]

    def allowed(self, url: str, ua: str = UA_TOKEN) -> tuple[bool, str]:
        mode, rp, sha = self._load(url)
        if mode == "allow":
            return True, sha
        if mode == "deny":
            return False, sha
        assert rp is not None
        return bool(rp.can_fetch(url, ua)), sha

    def body(self, url: str) -> bytes:
        """The cached robots.txt body of the URL's origin (b"" unless the fetch returned 2xx); fetches it once if
        the origin has not been consulted yet. Lets ``ai.HostAiPolicy`` read Content-Signal lines without a second
        request."""
        self._load(url)
        return self._bodies.get(self._origin(url), b"")

    def status(self, url: str) -> int:
        """HTTP status of the origin's robots.txt fetch (0 = network error, -1 = not fetched yet)."""
        return self._status.get(self._origin(url), -1)

    def crawl_delay(self, url: str, ua: str = UA_TOKEN) -> float | None:
        mode, rp, _ = self._load(url)
        if mode != "parse" or rp is None:
            return None
        d = rp.crawl_delay(ua)
        return float(d) if d is not None else None
