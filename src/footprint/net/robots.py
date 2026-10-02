"""robots.txt cache (Protego), one fetch per scheme+host per run."""

from __future__ import annotations

import hashlib
from typing import Callable
from urllib.parse import urlsplit

from protego import Protego

UA_TOKEN = "footprint-osint"


class RobotsCache:
    """``fetch_raw(robots_url) -> (status, body)``; status 0 = network error (treated as disallow).

    Rules: 2xx parse (BOM stripped); 4xx allow all; 5xx / network error disallow all.
    """

    def __init__(self, fetch_raw: Callable[[str], tuple[int, bytes]]):
        self._fetch_raw = fetch_raw
        self._cache: dict[str, tuple[str, Protego | None, str]] = {}  # origin -> (mode, parser, sha)

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
        return self._cache[origin]

    def allowed(self, url: str, ua: str = UA_TOKEN) -> tuple[bool, str]:
        mode, rp, sha = self._load(url)
        if mode == "allow":
            return True, sha
        if mode == "deny":
            return False, sha
        assert rp is not None
        return bool(rp.can_fetch(url, ua)), sha

    def crawl_delay(self, url: str, ua: str = UA_TOKEN) -> float | None:
        mode, rp, _ = self._load(url)
        if mode != "parse" or rp is None:
            return None
        d = rp.crawl_delay(ua)
        return float(d) if d is not None else None
