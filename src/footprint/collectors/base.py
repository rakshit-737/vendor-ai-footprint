"""Collector base: shared context, protocol and helpers (contract: docs/contracts_p2.md).

Collectors never touch the network directly. Every request goes through ``CollectContext.fetch``, which
enforces the per-family request cap from the depth plan before delegating to the injected ``Fetcher``
(LiveFetcher applies ToS register -> robots -> rate limit; ReplayFetcher reads the evidence store).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from footprint.models import (
    Capture,
    CollectorResult,
    CoverageEntry,
    CoverageStatus,
    DepthPlan,
    Document,
    FetchOutcome,
    SourceFamily,
    VendorProfile,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# Hosts whose terms forbid automation outright (manual capture only). Belt-and-braces on top of the ToS register.
MANUAL_ONLY_HOSTS: tuple[str, ...] = ("fiserv.com", "linkedin.com", "lnkd.in")

# Short AI query terms for remote full-text searches (EFTS, WordPress search, ORC keyword).
AI_SEARCH_TERMS: tuple[str, ...] = (
    "artificial intelligence",
    "machine learning",
    "generative AI",
    "LLM",
    "AI",
)

AI_ROLE_TERMS: tuple[str, ...] = (
    "data scientist",
    "data science",
    "ml engineer",
    "machine learning engineer",
    "ai engineer",
    "prompt engineer",
    "automation engineer",
    "intelligent automation",
    "rpa",
    "nlp",
    "computer vision",
)
"""AI specialist roles: a posting that names one is kept as a document even without another AI term."""

DELIVERY_ROLE_TERMS: tuple[str, ...] = AI_ROLE_TERMS + (
    # design 2.5 role keyword list: delivery roles such as NOC, SRE, operations, engineering, data, fraud, support
    "noc",
    "sre",
    "site reliability",
    "operations",
    "engineering",
    "engineer",
    "developer",
    "devops",
    "data",
    "analytics",
    "fraud",
    "support",
    "analyst",
    "architect",
    "administrator",
    "automation",
    "security",
    "technology",
    "implementation",
)
"""Role keywords a posting title must match before its detail page is fetched (design 2.5), besides AI terms and
the vendor's service terms. Seeded postings are always fetched."""

BLOCKED_REASONS: frozenset[str] = frozenset({"blocked_bot", "blocked_robots", "blocked_tou"})
NOT_REQUESTED_REASONS: frozenset[str] = frozenset({"cap_reached", "blocked_tou", "blocked_robots"})
"""Fetcher refusals made before any request (ToS register, per-host ceiling, robots.txt): they use no family
budget (design 2.5: "Robots.txt and terms checks are not counted")."""
REFUSED_STATUSES: frozenset[int] = frozenset({401, 403, 429, 503})


class Fetcher(Protocol):
    def get(
        self, url: str, *, vendor_id: str, family: SourceFamily, collector: str, accept: str | None = None
    ) -> FetchOutcome: ...


@dataclass
class CollectContext:
    """Everything one collector needs for one vendor.

    ``budget`` counts automated requests already used per family in this run; ``fetch`` refuses with
    ``reason="cap_reached"`` once ``FamilyPlan.cap`` is reached (a family not in the plan has cap 0). A family whose
    plan mode is ``on_lead`` draws on the vendor's discretionary fetch budget instead (``fetch(discretionary=True)``),
    and only when a collector has a lead (design 2.5: "HIST and EXEC on a lead").

    Each URL is requested at most once per vendor run: a repeat ``fetch`` (same URL and Accept) returns the first
    outcome without a request or budget use, so later collectors reuse the seeds' captures. ``blocked`` records URLs
    the run could not read (blocked_bot / blocked_robots / blocked_tou, or a stored 401/403/429/503 answer), which the
    Wayback collector treats as leads. ``as_of`` is the run's as-of date (ISO); recency rules use it, never the clock.
    """

    profile: VendorProfile
    seeds: dict
    plan: DepthPlan
    fetcher: Any
    store: Any
    budget: dict[SourceFamily, int] = field(default_factory=dict)
    as_of: str = ""
    blocked: dict[str, str] = field(default_factory=dict)
    discretionary_used: int = 0
    _seen: dict[tuple[str, str], FetchOutcome] = field(default_factory=dict, repr=False)

    @property
    def vendor_id(self) -> str:
        return self.profile.vendor_id

    def cap(self, family: SourceFamily) -> int:
        fp = self.plan.family(family)
        return fp.cap if fp else 0

    def mandatory(self, family: SourceFamily) -> bool:
        fp = self.plan.family(family)
        return bool(fp and fp.mandatory)

    def on_lead(self, family: SourceFamily) -> bool:
        fp = self.plan.family(family)
        return bool(fp and fp.mode == "on_lead")

    def remaining(self, family: SourceFamily) -> int:
        return max(0, self.cap(family) - self.budget.get(family, 0))

    def discretionary_left(self) -> int:
        return max(0, self.plan.discretionary_fetches - self.discretionary_used)

    def fetch(self, url: str, family: SourceFamily, collector: str, accept: str | None = None, *,
              seeded: bool = False, discretionary: bool = False) -> FetchOutcome:
        """``seeded=True`` for curated seed URLs (lets the ToS register admit 'limited' hosts);
        ``discretionary=True`` spends the vendor's discretionary budget instead of the family cap."""
        key = (url, accept or "")
        if key in self._seen:
            return self._seen[key]
        if discretionary:
            if self.discretionary_left() <= 0:
                return FetchOutcome(ok=False, reason="cap_reached")
            self.discretionary_used += 1
        elif self.remaining(family) <= 0:
            return FetchOutcome(ok=False, reason="cap_reached")
        self.budget[family] = self.budget.get(family, 0) + 1
        kw: dict[str, Any] = {"seeded": True} if seeded else {}
        try:
            out = self.fetcher.get(url, vendor_id=self.vendor_id, family=family, collector=collector,
                                   accept=accept, **kw)
        except Exception:  # noqa: BLE001 - a collector must never crash the run
            out = FetchOutcome(ok=False, reason="network_error")
        if out.reason in NOT_REQUESTED_REASONS:  # refused by the ToS register or robots.txt: no request was made
            self.budget[family] -= 1
            if discretionary:
                self.discretionary_used -= 1
        self._seen[key] = out
        if out.reason in BLOCKED_REASONS:
            self.blocked[url] = out.reason
        elif not out.ok and out.capture is not None and out.capture.status in REFUSED_STATUSES:
            self.blocked[url] = f"http_{out.capture.status}"
        return out

    def is_blocked(self, url: str) -> str:
        """Why ``url`` could not be read in this run ('' if it was not blocked)."""
        return self.blocked.get(url, "")

    def raw(self, outcome: FetchOutcome) -> bytes:
        if outcome.capture is None:
            return b""
        try:
            return self.store.get_raw(outcome.capture.capture_id)
        except Exception:  # noqa: BLE001
            return b""

    def entry(self, family: SourceFamily, status: CoverageStatus, collector: str, **kw: Any) -> CoverageEntry:
        kw.setdefault("cap", self.cap(family))
        return CoverageEntry(
            vendor_id=self.vendor_id, family=family, mandatory=self.mandatory(family), status=status,
            collector=collector, **kw,
        )


@runtime_checkable
class Collector(Protocol):
    name: str
    family: SourceFamily

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool: ...

    def collect(self, ctx: CollectContext) -> CollectorResult: ...


# --------------------------------------------------------------------------- helpers


REASON_STATUS: dict[str, CoverageStatus] = {
    "blocked_robots": CoverageStatus.BLOCKED_ROBOTS,
    "blocked_tou": CoverageStatus.BLOCKED_TOU,
    "blocked_bot": CoverageStatus.BLOCKED_BOT,
    "cap_reached": CoverageStatus.STOPPED,
    "http_error": CoverageStatus.ERROR,
    "network_error": CoverageStatus.ERROR,
    "replay_miss": CoverageStatus.ERROR,
}


def status_for(reason: str) -> CoverageStatus:
    return REASON_STATUS.get(reason, CoverageStatus.ERROR)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


_REGISTER: list[Any] = []


def _register() -> Any:
    """config/tou.toml, loaded once (None if unreadable: the hard-coded list still applies)."""
    if not _REGISTER:
        try:
            from footprint.net.tou import load_tou

            _REGISTER.append(load_tou())
        except Exception:  # noqa: BLE001
            _REGISTER.append(None)
    return _REGISTER[0]


def is_manual_only(url: str) -> bool:
    """Hosts whose terms bar automation: the hard-coded list, plus every ToS-register entry with
    ``automation = "none"`` (so collectors log them as manual/terms-barred instead of requesting them)."""
    h = host_of(url)
    if any(h == m or h.endswith("." + m) for m in MANUAL_ONLY_HOSTS):
        return True
    reg = _register()
    if reg is None or not h:
        return False
    e = reg.match(h)
    return bool(e is not None and e.automation == "none")


def to_document(ctx: CollectContext, outcome: FetchOutcome, family: SourceFamily | None = None) -> Document | None:
    """Extract a Document from a successful fetch via ``footprint.extract`` (None if unavailable/failed)."""
    if not outcome.ok or outcome.capture is None:
        return None
    try:
        from footprint.extract import extract_document  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        doc = extract_document(outcome.capture, ctx.raw(outcome), ctx.store)
    except Exception:  # noqa: BLE001
        return None
    if family is not None and doc.family != family:
        doc = doc.model_copy(update={"family": family})
    return doc


def make_text_document(
    ctx: CollectContext, capture: Capture, text: str, *, kind: str, title: str = "", extractor: str
) -> Document:
    """Build a Document from text the collector rendered itself (DNS records, job JSON summaries)."""
    sha, path = ctx.store.put_text(text)
    return Document(
        doc_id=sha, capture_id=capture.capture_id, vendor_id=ctx.vendor_id, family=capture.family,
        url=capture.url_requested, title=title, kind=kind, text_path=str(path), text_len=len(text),
        extractor=extractor,
    )


def load_lexicon(path: Path | str | None = None) -> dict:
    p = Path(path) if path else REPO_ROOT / "config" / "lexicon.toml"
    try:
        with open(p, "rb") as fh:
            return tomllib.load(fh)
    except OSError:
        return {"core": {"terms": list(AI_SEARCH_TERMS[:3]), "case_sensitive": ["AI", "ML", "LLMs?", "GenAI"]}}


def ai_regex(extra_terms: list[str] | tuple[str, ...] = (), lexicon: dict | None = None) -> re.Pattern[str]:
    """One regex matching core AI terms (case-insensitive phrases + case-sensitive tokens) and extra terms."""
    lex = lexicon if lexicon is not None else load_lexicon()
    core = lex.get("core", {})
    ci = [re.escape(t) for t in list(core.get("terms", [])) + list(extra_terms) if t]
    cs = list(core.get("case_sensitive", []))
    parts = []
    if ci:
        parts.append(r"(?i:\b(?:" + "|".join(sorted(ci, key=len, reverse=True)) + r")\b)")
    if cs:
        parts.append(r"\b(?:" + "|".join(cs) + r")\b")
    return re.compile("|".join(parts) or r"(?!x)x")


def seed_terms(seeds: dict, key: str) -> list[str]:
    out = []
    for item in seeds.get(key, []) or []:
        t = item.get("term") if isinstance(item, dict) else item
        if t:
            out.append(str(t))
    return out


def vendor_domains(profile: VendorProfile, seeds: dict) -> list[str]:
    doms = [d.lower().removeprefix("www.") for d in seeds.get("domains", []) or [] if d]
    if profile.domain and profile.domain not in doms:
        doms.insert(0, profile.domain)
    return doms
