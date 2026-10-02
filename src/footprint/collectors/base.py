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

DELIVERY_ROLE_TERMS: tuple[str, ...] = (
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


class Fetcher(Protocol):
    def get(
        self, url: str, *, vendor_id: str, family: SourceFamily, collector: str, accept: str | None = None
    ) -> FetchOutcome: ...


@dataclass
class CollectContext:
    """Everything one collector needs for one vendor.

    ``budget`` counts automated requests already used per family in this run; ``fetch`` refuses with
    ``reason="cap_reached"`` once ``FamilyPlan.cap`` is reached (a family not in the plan has cap 0).
    """

    profile: VendorProfile
    seeds: dict
    plan: DepthPlan
    fetcher: Any
    store: Any
    budget: dict[SourceFamily, int] = field(default_factory=dict)

    @property
    def vendor_id(self) -> str:
        return self.profile.vendor_id

    def cap(self, family: SourceFamily) -> int:
        fp = self.plan.family(family)
        return fp.cap if fp else 0

    def mandatory(self, family: SourceFamily) -> bool:
        fp = self.plan.family(family)
        return bool(fp and fp.mandatory)

    def remaining(self, family: SourceFamily) -> int:
        return max(0, self.cap(family) - self.budget.get(family, 0))

    def fetch(self, url: str, family: SourceFamily, collector: str, accept: str | None = None, *,
              seeded: bool = False) -> FetchOutcome:
        """``seeded=True`` for curated seed URLs (lets the ToS register admit 'limited' hosts)."""
        if self.remaining(family) <= 0:
            return FetchOutcome(ok=False, reason="cap_reached")
        self.budget[family] = self.budget.get(family, 0) + 1
        kw: dict[str, Any] = {"seeded": True} if seeded else {}
        try:
            return self.fetcher.get(url, vendor_id=self.vendor_id, family=family, collector=collector, accept=accept,
                                    **kw)
        except Exception:  # noqa: BLE001 - a collector must never crash the run
            return FetchOutcome(ok=False, reason="network_error")

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


def is_manual_only(url: str) -> bool:
    h = host_of(url)
    return any(h == m or h.endswith("." + m) for m in MANUAL_ONLY_HOSTS)


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
