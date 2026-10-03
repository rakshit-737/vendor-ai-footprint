"""Fetchers: LiveFetcher (policy-gated GET) and ReplayFetcher (evidence store only).

Gate order on every hop: ToS register -> robots.txt (Protego, UA token ``footprint-osint``)
-> per-host rate limit -> GET. GET only; never Wayback Save Page Now; no bot evasion.

A host that refuses the declared User-Agent (robots.txt itself answers 401/403 and so does the page, or three
refusals in a row) is reported as ``blocked_bot`` and not requested again in the run: the client identity is never
changed to get past a refusal. Every gate decision is appended to ``LiveFetcher.events`` (the run's fetch log).
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
TIMEOUT_S: tuple[float, float] = (10.0, 60.0)
"""(connect, read) seconds. Some public sites take ~30 s before the first byte (icba.org on 2 Oct 2026)."""
MAX_RETRIES = 3
NETWORK_RETRIES = 1
"""Extra attempts after a timeout or connection error (with back-off), before giving up as network_error."""
ROBOTS_RETRIES = 2
"""Extra attempts for a robots.txt fetch that timed out, dropped or answered 429/5xx."""
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_S = 120.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REFUSAL_STATUSES = frozenset({401, 403})
REFUSAL_LIMIT = 3
"""Consecutive 401/403 answers from one host after which it counts as refusing this client."""
HOST_REFUSED_NOTE = "host_refused"
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
    """Policy-gated live GETs. ``store`` follows the EvidenceStore contract (``put_raw``; ``put_blob`` optional,
    used to keep robots.txt bodies).

    Per-run per-host request counters live on the instance (``request_counts``, keyed by
    ``TouRegister.counter_key``); use one instance per run. ``events`` is the fetch log (one dict per gate
    decision and per robots.txt fetch); ``drain_events()`` hands it over and clears it.
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
        timeout: tuple[float, float] | float = TIMEOUT_S,
    ):
        self.store = store
        self.tou = tou
        self.ratelimit = ratelimit or RateLimiter()
        self.user_agent = user_agent
        self.sec_contact = sec_contact
        self.session = session or requests.Session()
        self._sleep = sleep
        self.timeout = timeout
        self.request_counts: dict[str, int] = {}
        self.refusals: dict[str, int] = {}
        self.refused_hosts: dict[str, str] = {}
        self.events: list[dict[str, Any]] = []
        self._ctx: dict[str, Any] = {}
        self.robots = robots or RobotsCache(self._fetch_robots, on_fetch=self._robots_fetched)

    # ------------------------------------------------------------------ helpers
    def ua_for(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if host == SEC_HOST_SUFFIX or host.endswith("." + SEC_HOST_SUFFIX):
            if not self.sec_contact:
                raise ValueError("FOOTPRINT_SEC_CONTACT required for SEC requests")
            return f"footprint-osint/1.0 {self.sec_contact}"
        return self.user_agent

    def _fetch_robots(self, robots_url: str) -> tuple[int, bytes]:
        """GET robots.txt. A timeout, connection error, 429 or 5xx is retried (with back-off, Retry-After honoured)
        before the conservative "disallow everything" applies: on 3 Oct 2026 one dropped connection to
        www.sec.gov/robots.txt barred every SEC request of a run."""
        e = self.tou.entry_for(robots_url)
        host = (urlsplit(robots_url).hostname or "").lower()
        last_exc: Exception | None = None
        status, body = 0, b""
        for attempt in range(ROBOTS_RETRIES + 1):
            if attempt:
                self._log("robots_txt", "retry", robots_url, status=status, why=type(last_exc).__name__ if last_exc else "")
            self.ratelimit.wait(host, e.max_rps if e.max_rps > 0 else 1.0)
            try:
                r = self.session.get(robots_url, headers={"User-Agent": self.ua_for(robots_url)}, timeout=self.timeout,
                                     allow_redirects=True)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc, status, body = exc, 0, b""
                if attempt < ROBOTS_RETRIES:
                    self._sleep(retry_delay(None, attempt + 1))
                continue
            status, body, last_exc = r.status_code, r.content, None
            if status in RETRY_STATUSES and attempt < ROBOTS_RETRIES:
                self._sleep(retry_delay(r.headers.get("Retry-After"), attempt + 1))
                continue
            return status, body
        if last_exc is not None:
            raise last_exc
        return status, body

    def _robots_fetched(self, origin: str, status: int, body: bytes, sha: str) -> None:
        put_blob = getattr(self.store, "put_blob", None)
        if body and callable(put_blob):
            put_blob(body)
        self._log("robots_txt", "fetched", origin + "/robots.txt", status=status, robots_sha256=sha)

    def _log(self, gate: str, decision: str, url: str, **extra: Any) -> None:
        ev = {"ts": _now_iso(), "gate": gate, "decision": decision, "url": url,
              "host": (urlsplit(url).hostname or "").lower(), **self._ctx, **extra}
        self.events.append(ev)

    def drain_events(self) -> list[dict[str, Any]]:
        out, self.events = self.events, []
        return out

    def _count(self, url: str) -> None:
        k = self.tou.counter_key(url)
        self.request_counts[k] = self.request_counts.get(k, 0) + 1

    @staticmethod
    def _fail(reason: str, status: int = 0, capture: Any = None) -> FetchOutcome:
        return FetchOutcome(ok=False, capture=capture, status=status, reason=reason)

    def _gate(self, url: str, seeded: bool) -> tuple[str, str]:
        """('', '') if allowed by scheme + ToS register, else (FetchOutcome reason, why)."""
        if urlsplit(url).scheme not in ("http", "https") or is_save_page_now(url):
            return "blocked_tou", "scheme or Save Page Now"
        ok, why = self.tou.check(url, self.request_counts, seeded=seeded)
        if ok:
            return "", ""
        return ("cap_reached" if why.startswith("cap_reached") else "blocked_tou"), why

    # ------------------------------------------------------------------ main
    def get(
        self, url: str, *, vendor_id: str, family: SourceFamily, collector: str,
        accept: str | None = None, seeded: bool = False,
    ) -> FetchOutcome:
        """GET ``url`` through all gates. ``seeded=True`` marks a URL explicitly listed in seeds
        (required for hosts with automation='limited'). Redirect hops are re-gated per host."""
        self._ctx = {"vendor_id": vendor_id, "family": SourceFamily(family).value, "collector": collector,
                     "seeded": seeded}
        try:
            return self._get(url, vendor_id=vendor_id, family=family, collector=collector, accept=accept,
                             seeded=seeded)
        finally:
            self._ctx = {}

    def _get(self, url: str, *, vendor_id: str, family: SourceFamily, collector: str, accept: str | None,
             seeded: bool) -> FetchOutcome:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            reason, why = self._gate(current, seeded)
            if reason:
                self._log("tou", reason, current, why=why)
                return self._fail(reason)
            host = (urlsplit(current).hostname or "").lower()
            if host in self.refused_hosts:
                self._log("circuit", "blocked_bot", current, why=self.refused_hosts[host])
                return self._fail("blocked_bot")
            entry = self.tou.entry_for(current)
            try:
                allowed, robots_sha = self.robots.allowed(current, UA_TOKEN)
                delay = self.robots.crawl_delay(current, UA_TOKEN) if hasattr(self.robots, "crawl_delay") else None
            except Exception:  # noqa: BLE001
                self._log("robots", "network_error", current)
                return self._fail("network_error")
            if not allowed:
                self._log("robots", "blocked_robots", current, robots_sha256=robots_sha)
                return self._fail("blocked_robots")
            headers = {"User-Agent": self.ua_for(current)}
            if accept:
                headers["Accept"] = accept

            resp: requests.Response | None = None
            net_tries = 0
            attempt = 0
            while True:
                if attempt or net_tries:
                    reason, why = self._gate(current, seeded)
                    if reason:
                        self._log("tou", reason, current, why=why)
                        return self._fail(reason, status=resp.status_code if resp is not None else 0)
                self.ratelimit.wait(host, entry.max_rps, delay)
                self._count(current)
                try:
                    resp = self.session.get(current, headers=headers, timeout=self.timeout, allow_redirects=False)
                except (requests.Timeout, requests.ConnectionError) as exc:
                    if net_tries < NETWORK_RETRIES:
                        net_tries += 1
                        self._log("fetch", "retry_network", current, why=type(exc).__name__)
                        self._sleep(retry_delay(None, net_tries))
                        continue
                    self._log("fetch", "network_error", current, why=type(exc).__name__)
                    return self._fail("network_error")
                except requests.RequestException as exc:
                    self._log("fetch", "network_error", current, why=type(exc).__name__)
                    return self._fail("network_error")
                if is_bot_wall(resp.status_code, resp.headers, resp.content):
                    self._log("fetch", "blocked_bot", current, status=resp.status_code, why="bot wall marker")
                    return self._fail("blocked_bot", status=resp.status_code)
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                    self._sleep(retry_delay(resp.headers.get("Retry-After"), attempt))
                    attempt += 1
                    continue
                break
            assert resp is not None
            if resp.status_code in REFUSAL_STATUSES:
                self.refusals[host] = self.refusals.get(host, 0) + 1
                robots_status = self.robots.status(current) if hasattr(self.robots, "status") else -1
                if robots_status in REFUSAL_STATUSES or self.refusals[host] >= REFUSAL_LIMIT:
                    why = (f"{HOST_REFUSED_NOTE}: robots.txt HTTP {robots_status} and page HTTP {resp.status_code} "
                           f"for the declared User-Agent" if robots_status in REFUSAL_STATUSES else
                           f"{HOST_REFUSED_NOTE}: {self.refusals[host]} consecutive HTTP 401/403 answers")
                    self.refused_hosts[host] = why
                    cap = self._store(url, current, resp, entry.match, robots_sha, vendor_id=vendor_id,
                                      family=family, collector=collector, note=why)
                    self._log("fetch", "blocked_bot", current, status=resp.status_code, why=why,
                              capture_id=cap.capture_id)
                    return self._fail("blocked_bot", status=resp.status_code, capture=cap)
            else:
                self.refusals[host] = 0
            if resp.is_redirect and resp.headers.get("Location"):
                nxt = urljoin(current, resp.headers["Location"])
                self._log("fetch", "redirect", current, status=resp.status_code, location=nxt)
                current = nxt
                continue
            return self._finish(url, current, resp, entry.match, robots_sha,
                                vendor_id=vendor_id, family=family, collector=collector)
        self._log("fetch", "http_error", current, why="too many redirects")
        return self._fail("http_error")

    def _store(self, url, final, resp, tou_match, robots_sha, *, vendor_id, family, collector, note=""):
        hdrs = {k: resp.headers[k] for k in CAPTURE_HEADER_KEYS if k in resp.headers}
        s = urlsplit(final)
        m = _WAYBACK_RE.match(s.path)
        via_wb = bool(m) and (s.hostname or "").lower().endswith("archive.org")
        return self.store.put_raw(
            resp.content,
            vendor_id=vendor_id, family=family, collector=collector,
            url_requested=url, url_final=final if final != url else "",
            status=resp.status_code, content_type=resp.headers.get("content-type", ""),
            headers=hdrs, retrieved_at=_now_iso(),
            robots_decision="allowed", robots_sha256=robots_sha, tou_match=tou_match,
            via_wayback=via_wb, wayback_timestamp=m.group(1) if via_wb and m else "",
            manual=False, captured_by=collector, note=note,
        )

    def _finish(self, url, final, resp, tou_match, robots_sha, *, vendor_id, family, collector) -> FetchOutcome:
        cap = self._store(url, final, resp, tou_match, robots_sha, vendor_id=vendor_id, family=family,
                          collector=collector)
        if 200 <= resp.status_code < 300:
            self._log("fetch", "ok", final, status=resp.status_code, tou_match=tou_match, robots_sha256=robots_sha,
                      capture_id=cap.capture_id)
            return FetchOutcome(ok=True, capture=cap, status=resp.status_code, reason="")
        self._log("fetch", "http_error", final, status=resp.status_code, tou_match=tou_match,
                  capture_id=cap.capture_id)
        return FetchOutcome(ok=False, capture=cap, status=resp.status_code, reason="http_error")


class ReplayFetcher:
    """Serves the latest stored capture for a URL; never touches the network.

    A stored refusal (note ``host_refused``) replays as ``blocked_bot``, as it was reported live.
    """

    def __init__(self, store: Any):
        self.store = store
        self.events: list[dict[str, Any]] = []

    def drain_events(self) -> list[dict[str, Any]]:
        out, self.events = self.events, []
        return out

    def get(
        self, url: str, *, vendor_id: str, family: SourceFamily, collector: str,
        accept: str | None = None, seeded: bool = False,
    ) -> FetchOutcome:
        cap = self.store.find_by_url(url)
        base = {"gate": "replay", "url": url, "vendor_id": vendor_id, "family": SourceFamily(family).value,
                "collector": collector}
        if cap is None:
            self.events.append({**base, "decision": "replay_miss"})
            return FetchOutcome(ok=False, reason="replay_miss")
        ok = 200 <= cap.status < 300 or (cap.status == 0 and cap.manual)
        if not ok and cap.status in REFUSAL_STATUSES and cap.note.startswith(HOST_REFUSED_NOTE):
            reason = "blocked_bot"
        else:
            reason = "" if ok else "http_error"
        self.events.append({**base, "decision": reason or "ok", "status": cap.status, "capture_id": cap.capture_id})
        return FetchOutcome(ok=ok, capture=cap, status=cap.status, reason=reason)
