"""Fetchers: LiveFetcher (policy-gated GET) and ReplayFetcher (evidence store only).

Gate order on every hop: ToS register -> robots.txt (Protego, UA token ``footprint-osint``)
-> per-host rate limit -> GET. GET only; never Wayback Save Page Now; no bot evasion.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urljoin, urlsplit

import requests

from footprint.models import CAPTURE_HEADER_KEYS, FetchOutcome, SourceFamily
from footprint.net.ratelimit import RateLimiter
from footprint.net.robots import UA_TOKEN, RobotsCache
from footprint.net.tou import TouRegister

DEFAULT_UA = "footprint-osint/1.0 (+confidential TPRM research)"
TIMEOUT_S = 20
MAX_RETRIES = 3
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_S = 120.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SEC_HOST_SUFFIX = "sec.gov"

_BOT_MARKERS = (
    "cf-chl", "challenge-platform", "cf_chl_opt", "just a moment...", "attention required! | cloudflare",
    "checking your browser", "_abck", "ak_bmsc", "akamai", "errors.edgesuite.net",
    "captcha", "perimeterx", "px-captcha", "incapsula", "ddos-guard",
)
_BOT_SERVERS = ("cloudflare", "akamaighost", "akamai")
_WAYBACK_RE = re.compile(r"^/web/(\d{14})")


class Fetcher(Protocol):
    def get(
        self, url: str, *, vendor_id: str, family: SourceFamily, collector: str,
        accept: str | None = None, seeded: bool = False,
    ) -> FetchOutcome: ...


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_bot_wall(status: int, headers: Any, body: bytes) -> bool:
    """403/503 carrying an Akamai/Cloudflare-style challenge marker (header or body)."""
    if status not in (403, 503):
        return False
    h = {str(k).lower(): str(v).lower() for k, v in dict(headers or {}).items()}
    if h.get("cf-mitigated") == "challenge" or any(b in h.get("server", "") for b in _BOT_SERVERS):
        return True
    snippet = body[:65536].decode("utf-8", errors="ignore").lower()
    if "access denied" in snippet and "reference #" in snippet:  # Akamai edge denial page
        return True
    return any(m in snippet for m in _BOT_MARKERS)


def is_save_page_now(url: str) -> bool:
    s = urlsplit(url)
    host = (s.hostname or "").lower()
    return (host == "archive.org" or host.endswith(".archive.org")) and s.path.lower().startswith("/save")


def retry_delay(value: str | None, attempt: int) -> float:
    """Seconds to wait: Retry-After (seconds or HTTP date, capped) else 2**attempt."""
    if value:
        try:
            return min(MAX_RETRY_AFTER_S, max(0.0, float(value)))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(value)
                return min(MAX_RETRY_AFTER_S, max(0.0, (dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:  # noqa: BLE001
                pass
    return float(2**attempt)


class LiveFetcher:
    """Policy-gated live GETs. ``store`` follows the EvidenceStore contract (``put_raw``).

    Per-run per-host request counters live on the instance (``request_counts``, keyed by
    ``TouRegister.counter_key``); use one instance per run.
    """

    def __init__(
        self,
        store: Any,
        tou: TouRegister,
        robots: RobotsCache | None = None,
        ratelimit: RateLimiter | None = None,
        user_agent: str = DEFAULT_UA,
        sec_contact: str = "",
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.tou = tou
        self.ratelimit = ratelimit or RateLimiter()
        self.user_agent = user_agent
        self.sec_contact = sec_contact
        self.session = session or requests.Session()
        self._sleep = sleep
        self.request_counts: dict[str, int] = {}
        self.robots = robots or RobotsCache(self._fetch_robots)

    # ------------------------------------------------------------------ helpers
    def ua_for(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if host == SEC_HOST_SUFFIX or host.endswith("." + SEC_HOST_SUFFIX):
            if not self.sec_contact:
                raise ValueError("FOOTPRINT_SEC_CONTACT required for SEC requests")
            return f"footprint-osint/1.0 {self.sec_contact}"
        return self.user_agent

    def _fetch_robots(self, robots_url: str) -> tuple[int, bytes]:
        e = self.tou.entry_for(robots_url)
        host = (urlsplit(robots_url).hostname or "").lower()
        self.ratelimit.wait(host, e.max_rps if e.max_rps > 0 else 1.0)
        r = self.session.get(
            robots_url, headers={"User-Agent": self.ua_for(robots_url)}, timeout=TIMEOUT_S, allow_redirects=True
        )
        return r.status_code, r.content

    def _count(self, url: str) -> None:
        k = self.tou.counter_key(url)
        self.request_counts[k] = self.request_counts.get(k, 0) + 1

    @staticmethod
    def _fail(reason: str, status: int = 0, capture: Any = None) -> FetchOutcome:
        return FetchOutcome(ok=False, capture=capture, status=status, reason=reason)

    def _gate(self, url: str, seeded: bool) -> str:
        """'' if allowed by scheme + ToS register, else a FetchOutcome reason."""
        if urlsplit(url).scheme not in ("http", "https") or is_save_page_now(url):
            return "blocked_tou"
        ok, why = self.tou.check(url, self.request_counts, seeded=seeded)
        if ok:
            return ""
        return "cap_reached" if why.startswith("cap_reached") else "blocked_tou"

    # ------------------------------------------------------------------ main
    def get(
        self, url: str, *, vendor_id: str, family: SourceFamily, collector: str,
        accept: str | None = None, seeded: bool = False,
    ) -> FetchOutcome:
        """GET ``url`` through all gates. ``seeded=True`` marks a URL explicitly listed in seeds
        (required for hosts with automation='limited'). Redirect hops are re-gated per host."""
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            reason = self._gate(current, seeded)
            if reason:
                return self._fail(reason)
            entry = self.tou.entry_for(current)
            try:
                allowed, robots_sha = self.robots.allowed(current, UA_TOKEN)
                delay = self.robots.crawl_delay(current, UA_TOKEN) if hasattr(self.robots, "crawl_delay") else None
            except Exception:  # noqa: BLE001
                return self._fail("network_error")
            if not allowed:
                return self._fail("blocked_robots")
            host = (urlsplit(current).hostname or "").lower()
            headers = {"User-Agent": self.ua_for(current)}
            if accept:
                headers["Accept"] = accept

            resp: requests.Response | None = None
            for attempt in range(MAX_RETRIES + 1):
                if attempt:
                    reason = self._gate(current, seeded)
                    if reason:
                        return self._fail(reason, status=resp.status_code if resp is not None else 0)
                self.ratelimit.wait(host, entry.max_rps, delay)
                self._count(current)
                try:
                    resp = self.session.get(current, headers=headers, timeout=TIMEOUT_S, allow_redirects=False)
                except requests.RequestException:
                    return self._fail("network_error")
                if is_bot_wall(resp.status_code, resp.headers, resp.content):
                    return self._fail("blocked_bot", status=resp.status_code)
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                    self._sleep(retry_delay(resp.headers.get("Retry-After"), attempt))
                    continue
                break
            assert resp is not None
            if resp.is_redirect and resp.headers.get("Location"):
                current = urljoin(current, resp.headers["Location"])
                continue
            return self._finish(url, current, resp, entry.match, robots_sha,
                                vendor_id=vendor_id, family=family, collector=collector)
        return self._fail("http_error")

    def _finish(self, url, final, resp, tou_match, robots_sha, *, vendor_id, family, collector) -> FetchOutcome:
        hdrs = {k: resp.headers[k] for k in CAPTURE_HEADER_KEYS if k in resp.headers}
        s = urlsplit(final)
        m = _WAYBACK_RE.match(s.path)
        via_wb = bool(m) and (s.hostname or "").lower().endswith("archive.org")
        cap = self.store.put_raw(
            resp.content,
            vendor_id=vendor_id, family=family, collector=collector,
            url_requested=url, url_final=final if final != url else "",
            status=resp.status_code, content_type=resp.headers.get("content-type", ""),
            headers=hdrs, retrieved_at=_now_iso(),
            robots_decision="allowed", robots_sha256=robots_sha, tou_match=tou_match,
            via_wayback=via_wb, wayback_timestamp=m.group(1) if via_wb and m else "",
            manual=False, captured_by=collector,
        )
        if 200 <= resp.status_code < 300:
            return FetchOutcome(ok=True, capture=cap, status=resp.status_code, reason="")
        return FetchOutcome(ok=False, capture=cap, status=resp.status_code, reason="http_error")


class ReplayFetcher:
    """Serves the latest stored capture for a URL; never touches the network."""

    def __init__(self, store: Any):
        self.store = store

    def get(
        self, url: str, *, vendor_id: str, family: SourceFamily, collector: str,
        accept: str | None = None, seeded: bool = False,
    ) -> FetchOutcome:
        cap = self.store.find_by_url(url)
        if cap is None:
            return FetchOutcome(ok=False, reason="replay_miss")
        ok = 200 <= cap.status < 300 or (cap.status == 0 and cap.manual)
        return FetchOutcome(ok=ok, capture=cap, status=cap.status, reason="" if ok else "http_error")
