"""Terms-of-use register (config/tou.toml): first gate for every automated request."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel

DEFAULT_TOU_PATH = Path(__file__).resolve().parents[3] / "config" / "tou.toml"

# Hosts that are manual-only regardless of what the register file says (CLAUDE.md collection rules).
HARD_MANUAL = ("fiserv.com", "linkedin.com")


class TouEntry(BaseModel):
    match: str
    automation: str = "limited"  # full | limited | none
    ai_processing_allowed: bool = False
    max_rps: float = 1.0
    max_requests_per_run: int = 0
    terms_url: str = ""
    verified: bool = False
    note: str = ""


def _host_of(url_or_host: str) -> str:
    if "://" in url_or_host:
        return (urlsplit(url_or_host).hostname or "").lower()
    return url_or_host.lower().split(":")[0]


class TouRegister:
    """Host -> terms entry lookup with per-run cap and automation checks."""

    def __init__(self, data: Mapping[str, Any]):
        self.version = str(data.get("version", ""))
        self.default = TouEntry(match="*default*", **dict(data.get("default", {})))
        self.entries = [TouEntry(**h) for h in data.get("host", [])]

    def match(self, host: str) -> TouEntry | None:
        """Longest suffix match on the host (``a.b.example.com`` matches ``example.com``)."""
        h = _host_of(host)
        best: TouEntry | None = None
        for e in self.entries:
            m = e.match.lower()
            if h == m or h.endswith("." + m):
                if best is None or len(m) > len(best.match):
                    best = e
        return best

    def entry_for(self, url_or_host: str) -> TouEntry:
        return self.match(url_or_host) or self.default

    def counter_key(self, url_or_host: str) -> str:
        """Key used in per-run request counters (the matched entry, else the bare host)."""
        e = self.match(url_or_host)
        return e.match.lower() if e else _host_of(url_or_host)

    def check(self, url: str, run_counter: Mapping[str, int], *, seeded: bool = False) -> tuple[bool, str]:
        """Is one more automated GET of ``url`` allowed this run?

        ``run_counter`` maps :meth:`counter_key` -> requests already made this run.
        Returns ``(allowed, reason)``; reason is '' when allowed.
        """
        host = _host_of(url)
        if not host:
            return False, "blocked_tou: no host"
        if any(host == m or host.endswith("." + m) for m in HARD_MANUAL):
            return False, f"blocked_tou: {host} is manual capture only"
        e = self.entry_for(url)
        if e.automation == "none":
            return False, f"blocked_tou: automation=none for {e.match}"
        if e.automation == "limited" and not seeded:
            return False, f"blocked_tou: automation=limited for {e.match}, URL not seeded"
        if e.automation not in ("full", "limited"):
            return False, f"blocked_tou: unknown automation {e.automation!r}"
        if e.max_rps <= 0:
            return False, f"blocked_tou: max_rps=0 for {e.match}"
        used = run_counter.get(self.counter_key(url), 0)
        if e.max_requests_per_run and used >= e.max_requests_per_run:
            return False, f"cap_reached: {used}/{e.max_requests_per_run} for {e.match}"
        return True, ""


def load_tou(path: str | Path | None = None) -> TouRegister:
    p = Path(path) if path else DEFAULT_TOU_PATH
    with open(p, "rb") as fh:
        return TouRegister(tomllib.load(fh))
