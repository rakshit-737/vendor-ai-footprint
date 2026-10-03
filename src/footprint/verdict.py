"""AI-usage verdict for column O (C8; design Appendix A 2.7; docs/contracts_p3.md section 7).

decide() turns one vendor's evidence items into a UsageVerdict: the first of rules a)-f) that matches, its ICD 203
likelihood, a separate confidence judgement with its reason, and the item keys column P cites (``decisive``).
assign_roles() then gives every item its Evidence Log role. Both are pure and deterministic: no clock and no I/O,
input order never matters, and every list is ranked best first (strength label order, then SR, RL and RC) with
ties broken on the signal class (U1 first), where the AI sits, specificity, the genuine-use indicator and provider
counts, and only then item_key (rank_key).

What counts: only citable items of the plan's vendor (EvidenceItem.citable), that is accepted items and unreviewed
verified items, never rejected items, definition-test traps or LLM proposals still awaiting an analyst. Q is
rules.is_qualifying, K is rules.is_corroborating, independence is cluster.independent and complete coverage is
depth.coverage_complete.

Interpretations (each pinned by tests/unit/test_verdict.py):
- T0 (older than 36 months, or undated) is historical context only. Such an item never decides a rule, never
  blocks "Not detected", never raises the likelihood and is never decisive; a limiting statement needs T1 or newer
  to conflict (rule a) or to affirm a negative (rule d).
- Syndicated copies count once. Items of one origin cluster are never independent, column P never cites two of
  them, and the item lists keep one item per origin cluster.
- An Inconclusive verdict is a "roughly even chance" exactly when some item would block rule e) (SP S1+, RL R2+,
  RC T1+, class U1-U4 or U7). A limiting, governance or capability-building statement does not raise it.
- Confidence High needs a Yes or Affirmed-negative verdict: an Inconclusive verdict rests on indicators that do
  not settle the question, and Not detected rests on coverage alone, so it is Moderate.
- Decisive items are T1 or newer, not industry commentary (RL R0) and not from excluded sources (SR E/F). For an
  Inconclusive verdict, signals attributable to the vendor come first (its own claims, DNS relationships, an
  inferred affiliate); a platform supplier's capability only fills a slot that is left (design section 5, V-001).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from footprint import depth
from footprint.models import (
    CONTEXT_STRENGTHS,
    QUALIFYING_LOCI,
    STRENGTH_ORDER,
    CoverageEntry,
    DepthPlan,
    EvidenceItem,
    SourceFamily,
    UsageVerdict,
)

__all__ = [
    "FAMILY_WORDS",
    "MAX_DECISIVE_INCONCLUSIVE",
    "MAX_DECISIVE_YES",
    "STATUS_WORDS",
    "assign_roles",
    "decide",
    "independent",
    "is_corroborating",
    "is_qualifying",
    "item_date",
    "rank_key",
]

ACCOUNTABLE_SR = frozenset({"A", "B"})        # A legally accountable, B first-party accountable
CORROBORATING_SR = frozenset({"A", "B", "C"})
LOW_GRADE_SR = frozenset({"D", "E", "F"})     # aggregators and copies, user-generated, unknown origin
EXCLUDED_SR = frozenset({"E", "F"})           # excluded sources (2.3): logged, never cited
USE_CLASSES = frozenset({"U1", "U2", "U3", "U4"})
SIGNAL_CLASSES = USE_CLASSES | {"U7"}         # classes that block rule e): AI use, or a claim of it
LIMITING_CLASS = "U8"
SUPPLIER_LOCUS = "platform_supplier"
COMMENTARY_LOCUS = "commentary"
MAX_DECISIVE_YES = 3                          # column P: up to 3 Q/K items for a Yes verdict
MAX_DECISIVE_INCONCLUSIVE = 2                 # column P: up to 2 indicator items for an Inconclusive verdict
LOCUS_RANK: dict[str, int] = {locus: n for n, locus in enumerate((
    "service_feature", "vendor_addon", "delivery_ops", "sdlc", "corporate_internal", "relationship",
    "affiliate_inferred", "platform_supplier", "unknown", "commentary"))}
"""A tie-break of rank_key: the nearer the AI sits to the service Meridian receives, the better (2.8 exposure table)."""

FAMILY_WORDS: dict[SourceFamily, str] = {
    SourceFamily.LEG: "legal and trust pages",
    SourceFamily.REG: "regulatory filings",
    SourceFamily.PRD: "product pages and newsroom",
    SourceFamily.JOB: "job postings",
    SourceFamily.DNS: "DNS records",
    SourceFamily.IND: "independent corroboration",
    SourceFamily.HIST: "archive history",
    SourceFamily.EXEC: "executive channels",
}
"""Source families in plain words, for confidence reasons and the trace (no codes)."""
STATUS_WORDS: dict[str, str] = {
    "pending": "still pending",
    "blocked_robots": "blocked by robots.txt",
    "blocked_tou": "barred by terms of use",
    "blocked_bot": "blocked by bot protection",
    "error": "ended in error",
    "descoped": "descoped",
    "missing": "not logged",
}
"""Incomplete coverage statuses in words ('missing' = no Coverage Log entry), as config/depth.toml words them."""

_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


# --------------------------------------------------------------------------- Q, K and independence
#
# footprint.rules (is_qualifying, is_corroborating) and footprint.cluster (independent) own these predicates
# (docs/contracts_p3.md sections 3 and 6). The mirrors below restate the same contract and are used only while
# those modules do not exist yet (parallel P3 build); tests/unit/test_verdict.py checks whichever predicates are
# in use against the contract definitions.


def _mirror_is_qualifying(item: EvidenceItem) -> bool:
    """Q (2.7): verified, SR A/B, SP S2+, RL R2+, RC T1+, class U1-U4, locus in QUALIFYING_LOCI."""
    t = item.tags
    return (item.citable and t.sr in ACCOUNTABLE_SR and t.sp_level >= 2 and t.rl_level >= 2 and t.rc_level >= 1
            and t.u_class in USE_CLASSES and t.locus in QUALIFYING_LOCI)


def _mirror_is_corroborating(item: EvidenceItem) -> bool:
    """The item's own part of K (2.7): SR A-C, SP S2+, RL R2+, RC T1+, class U1-U4 (independence is separate)."""
    t = item.tags
    return (item.citable and t.sr in CORROBORATING_SR and t.sp_level >= 2 and t.rl_level >= 2 and t.rc_level >= 1
            and t.u_class in USE_CLASSES)


def _mirror_independent(a: EvidenceItem, b: EvidenceItem) -> bool:
    """A different origin cluster AND a different publisher (case-folded) or a different source family."""
    return a.cluster_id != b.cluster_id and (a.publisher.casefold() != b.publisher.casefold() or a.family != b.family)


is_qualifying: Callable[[EvidenceItem], bool]
is_corroborating: Callable[[EvidenceItem], bool]
independent: Callable[[EvidenceItem, EvidenceItem], bool]
try:
    from footprint.rules import is_corroborating, is_qualifying
except ModuleNotFoundError as exc:  # pragma: no cover - only before footprint/rules.py exists
    if exc.name != "footprint.rules":
        raise
    is_qualifying, is_corroborating = _mirror_is_qualifying, _mirror_is_corroborating
try:
    from footprint.cluster import independent
except ModuleNotFoundError as exc:  # pragma: no cover - only before footprint/cluster.py (or rules.py) exists
    if exc.name not in ("footprint.cluster", "footprint.rules"):
        raise
    independent = _mirror_independent
try:
    from footprint.cluster import distinct_sources as _distinct_sources
except ModuleNotFoundError:  # pragma: no cover
    def _distinct_sources(items, *, prefer=()):  # type: ignore[no-redef]
        return list(items)


# --------------------------------------------------------------------------- ranking and dates


def rank_key(item: EvidenceItem) -> tuple[int, str, int, int, int, int, int, int, int, str]:
    """Best first: strength label order (2.3), then SR (A first), RL (R3 first), RC (T3 first).

    Ties then go to what the item says rather than to its hash: the signal class (U1 first, so a statement of AI use,
    U1-U4, outranks a marketing claim, U7), where the AI sits (the service, then its delivery, then development, as
    in the exposure table of 2.8), the specificity (S3 first), the number of distinct genuine-use indicators (G1-G11)
    and of named providers (more first), and only then item_key. Column P therefore quotes the affiliate's platform
    claim ('NEO is the customer-side AI platform ...') before its slogans.
    """
    t = item.tags
    genuine = len({ind.code for ind in item.indicators if ind.code.startswith("G")})
    return (STRENGTH_ORDER.index(t.strength), t.sr, -t.rl_level, -t.rc_level, t.u_number, LOCUS_RANK[t.locus],
            -t.sp_level, -genuine, -len(set(item.providers)), item.item_key)


def item_date(item: EvidenceItem) -> str:
    """The day an item speaks for: its publication date, else its retrieval date (ISO YYYY-MM-DD; '' if neither)."""
    for value in (item.published, item.retrieved_at):
        day = value[:10]
        if _ISO_DAY.fullmatch(day):
            try:
                dt.date.fromisoformat(day)
            except ValueError:
                continue
            return day
    return ""


def _cluster(item: EvidenceItem) -> str:
    """Origin cluster; an item that was never clustered is its own origin."""
    return item.cluster_id or item.item_key


def _distinct(candidates: Iterable[EvidenceItem], limit: int,
              chosen: Sequence[EvidenceItem] = ()) -> list[EvidenceItem]:
    """`chosen`, then candidates in order while under `limit`, skipping any origin cluster already cited."""
    out = list(chosen)
    seen = {_cluster(i) for i in out}
    for item in candidates:
        if len(out) >= limit:
            break
        if _cluster(item) not in seen:
            out.append(item)
            seen.add(_cluster(item))
    return out


def _per_cluster(items: Sequence[EvidenceItem], prefer: Sequence[EvidenceItem] = ()) -> list[EvidenceItem]:
    """One item per origin cluster (syndicated copies count once): a preferred member, else the best ranked."""
    wanted = {i.item_key for i in items}
    best: dict[str, EvidenceItem] = {}
    for item in [*(p for p in prefer if p.item_key in wanted), *sorted(items, key=rank_key)]:
        best.setdefault(_cluster(item), item)
    return sorted(best.values(), key=rank_key)


def _sources(items: Sequence[EvidenceItem], prefer: Sequence[EvidenceItem] = ()) -> list[EvidenceItem]:
    """One item per independent source (cluster.distinct_sources over one item per origin cluster), best first."""
    return sorted(_distinct_sources(_per_cluster(items, prefer=prefer), prefer=prefer), key=rank_key)


def _union(*groups: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    unique = {i.item_key: i for group in groups for i in group}
    return sorted(unique.values(), key=rank_key)


def _independent_pair(items: Sequence[EvidenceItem]) -> bool:
    return any(independent(a, b) for n, a in enumerate(items) for b in items[n + 1:])


# --------------------------------------------------------------------------- item tests


def _blocks_not_detected(item: EvidenceItem) -> bool:
    """Rule e) fails while an item has SP S1+, RL R2+, RC T1+ and class U1-U4 or U7."""
    t = item.tags
    return t.sp_level >= 1 and t.rl_level >= 2 and t.rc_level >= 1 and t.u_class in SIGNAL_CLASSES


def _may_decide(item: EvidenceItem) -> bool:
    """May column P cite the item as decisive: T1 or newer, not commentary, not an excluded source."""
    t = item.tags
    return t.rc_level >= 1 and t.rl_level >= 1 and t.locus != COMMENTARY_LOCUS and t.sr not in EXCLUDED_SR


def _limiting(item: EvidenceItem, *, min_rl: int) -> bool:
    """An accountable (SR A/B) limiting statement, T1 or newer, at RL `min_rl` or higher."""
    t = item.tags
    return t.u_class == LIMITING_CLASS and t.sr in ACCOUNTABLE_SR and t.rl_level >= min_rl and t.rc_level >= 1


def _describe(item: EvidenceItem) -> str:
    what = ", ".join(part for part in (item.source_type, item.publisher) if part) or "an item"
    return f"{what} [{item.item_key[:12]}]"


def _count(n: int, noun: str) -> str:
    words = _NUMBER_WORDS[n] if n < len(_NUMBER_WORDS) else str(n)
    return f"{words} {noun}{'' if n == 1 else 's'}"


def _join(parts: Sequence[str]) -> str:
    if len(parts) < 2:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------- coverage


def _incomplete_families(plan: DepthPlan, coverage: Sequence[CoverageEntry]) -> list[str]:
    """Each mandatory family without a complete status, in words: 'job postings (blocked by robots.txt)'."""
    entries = [e for e in coverage if e.vendor_id == plan.vendor_id]
    out: list[str] = []
    for fp in plan.families:
        if not fp.mandatory:
            continue
        statuses = [e.status for e in entries if e.family == fp.family]
        if any(depth.is_complete(s) for s in statuses):
            continue
        status = statuses[-1].value if statuses else "missing"
        out.append(f"{FAMILY_WORDS.get(fp.family, fp.family.value)} ({STATUS_WORDS.get(status, status)})")
    return out


def _coverage_gap(gaps: Sequence[str]) -> str:
    if not gaps:
        return "coverage of the mandatory source families was not completed"
    return f"the search of {_join(gaps)} was not completed"


# --------------------------------------------------------------------------- the rules


@dataclass
class _Match:
    rule: str
    decisive: list[EvidenceItem]
    corroborating: list[EvidenceItem] = field(default_factory=list)
    basis: list[EvidenceItem] = field(default_factory=list)  # the items the confidence judgement rests on


@dataclass
class _Evidence:
    pool: list[EvidenceItem]        # citable items of the vendor, one per item_key, best first
    qs: list[EvidenceItem]          # Q
    ks: list[EvidenceItem]          # K candidates (independence is checked per rule)
    signals: list[EvidenceItem]     # items that block rule e)
    complete: bool
    gaps: list[str]


def _rule_a(ev: _Evidence, trace: list[str]) -> _Match | None:
    """Conflict: a Q, plus an accountable limiting statement on the exact service dated the same day or later."""
    limits = [u for u in ev.pool if _limiting(u, min_rl=3)]
    for q in ev.qs:
        later = [u for u in limits if item_date(u) >= item_date(q)]
        if later:
            u = later[0]
            trace.append(f"a) Conflict: yes. {_sentence(_describe(q))} ({item_date(q)}) is contradicted for the exact "
                         f"service by {_describe(u)} ({item_date(u)}); the conflict goes to the questionnaire.")
            support = [k for k in ev.ks if k.item_key != q.item_key and independent(q, k)]
            return _Match("a", decisive=[q, u], corroborating=support)
    scope = [u for u in ev.pool if _limiting(u, min_rl=2)] if ev.qs else []
    if scope:  # every one of them predates the evidence of use, or does not name the exact service
        listed = "; ".join(f"{_describe(u)} ({item_date(u)})" for u in scope)
        trace.append(f"a) Conflict: no. {_sentence(_count(len(scope), 'limiting statement'))} that predate"
                     f"{'s' if len(scope) == 1 else ''} the evidence of use or do{'es' if len(scope) == 1 else ''} not "
                     f"name the exact service raise{'s' if len(scope) == 1 else ''} a scope question, not a conflict: "
                     f"{listed}.")
    else:
        trace.append("a) Conflict: no. No first-party or legally accountable limiting statement on the exact service "
                     "is dated on or after a qualifying signal.")
    return None


def _rule_b(ev: _Evidence, trace: list[str]) -> _Match | None:
    """Confirmed: a Q at R3 plus a K that is a different item and independent of it."""
    exact = [q for q in ev.qs if q.tags.rl == "R3"]
    for q in exact:
        support = [k for k in ev.ks if k.item_key != q.item_key and independent(q, k)]
        if support:
            n = len(_sources(support))
            trace.append(f"b) Confirmed: yes. {_sentence(_describe(q))} ties AI to the exact service and "
                         f"{_count(n, 'independent source')} corroborate{'s' if n == 1 else ''} it, first "
                         f"{_describe(support[0])}.")
            return _Match("b", decisive=_yes_decisive(q, ev), corroborating=support, basis=_union(ev.qs, ev.ks))
    if exact:
        n = len(_per_cluster(exact))
        trace.append(f"b) Confirmed: no. {_sentence(_count(n, 'qualifying signal'))} tie{'s' if n == 1 else ''} AI to "
                     "the exact service, but none has independent corroboration.")
    else:
        trace.append("b) Confirmed: no. No qualifying signal ties AI to the exact service.")
    return None


def _rule_c(ev: _Evidence, trace: list[str]) -> _Match | None:
    """Probable: any Q, or two K that are independent of each other."""
    if ev.qs:
        anchor = ev.qs[0]
        support = [k for k in ev.ks if k.item_key != anchor.item_key and independent(anchor, k)]
        trace.append(f"c) Probable: yes. {_sentence(_count(len(_per_cluster(ev.qs)), 'qualifying signal'))}, the "
                     f"strongest {_describe(anchor)}.")
        return _Match("c", decisive=_yes_decisive(anchor, ev), corroborating=support, basis=_union(ev.qs, ev.ks))
    paired = [k for k in ev.ks if any(j.item_key != k.item_key and independent(k, j) for j in ev.ks)]
    if paired:
        anchor = paired[0]
        partner = next(j for j in ev.ks if j.item_key != anchor.item_key and independent(anchor, j))
        trace.append(f"c) Probable: yes. No qualifying signal, but {_describe(anchor)} and {_describe(partner)} are "
                     "independent corroborating signals.")
        return _Match("c", decisive=_yes_decisive(anchor, ev), corroborating=paired, basis=list(ev.ks))
    if ev.ks:
        lone = _count(len(_per_cluster(ev.ks)), "corroborating signal")
        trace.append(f"c) Probable: no. No qualifying signal, and {lone} without an independent second one.")
    else:
        trace.append("c) Probable: no. No qualifying signal and no corroborating signal.")
    return None


def _yes_decisive(anchor: EvidenceItem, ev: _Evidence) -> list[EvidenceItem]:
    """The anchor, then up to two more Q or K items, independent ones first, one per origin cluster."""
    others = [i for i in _union(ev.qs, ev.ks) if i.item_key != anchor.item_key]
    ordered = [i for i in others if independent(anchor, i)] + [i for i in others if not independent(anchor, i)]
    return _distinct(ordered, MAX_DECISIVE_YES, chosen=[anchor])


def _rule_d(ev: _Evidence, trace: list[str]) -> _Match | None:
    """Affirmed negative: an accountable limiting statement covering the service (RL R2+), with no Q and no K."""
    limits = [u for u in ev.pool if _limiting(u, min_rl=2)]
    if limits and not ev.qs and not ev.ks:
        trace.append(f"d) Affirmed negative: yes. {_sentence(_describe(limits[0]))} limits AI use for the service, "
                     "and there is no qualifying or corroborating signal.")
        decisive = _distinct(limits, len(limits))
        return _Match("d", decisive=decisive, basis=decisive)
    if limits:
        signal = (ev.qs or ev.ks)[0]
        trace.append(f"d) Affirmed negative: no. {_sentence(_describe(limits[0]))} limits AI use, but "
                     f"{_describe(signal)} still points to it.")
    else:
        trace.append("d) Affirmed negative: no. No recent first-party or legally accountable limiting statement "
                     "covers the service.")
    return None


def _rule_e(ev: _Evidence, trace: list[str]) -> _Match | None:
    """Not detected: complete coverage, and no item shows (claimed) AI use at RL R2+ (SP S1+, RC T1+)."""
    reasons = []
    if not ev.complete:
        reasons.append(_coverage_gap(ev.gaps))
    if ev.signals:
        reasons.append(f"{_count(len(ev.signals), 'item')} indicate{'s' if len(ev.signals) == 1 else ''} possible AI "
                       f"use, the strongest {_describe(ev.signals[0])}")
    if reasons:
        trace.append(f"e) Not detected: no. {_sentence('; '.join(reasons))}.")
        return None
    counter = [i for i in ev.pool if _may_decide(i) and i.tags.locus != SUPPLIER_LOCUS
               and (i.tags.u_class == LIMITING_CLASS or i.tags.sp == "S0")]
    decisive = counter[:1]
    found = f" Strongest counter-evidence: {_describe(decisive[0])}." if decisive else ""
    trace.append("e) Not detected: yes. Every mandatory source family was searched and no item shows AI use in the "
                 f"service, its product family or its delivery.{found}")
    return _Match("e", decisive=decisive)


def _rule_f(ev: _Evidence, trace: list[str]) -> _Match:
    """Inconclusive: marketing only, capability only, relationship only, an inferred affiliate, incomplete coverage."""
    eligible = [i for i in ev.pool if _may_decide(i)]
    vendor_side = [i for i in eligible if i.tags.locus != SUPPLIER_LOCUS]
    supplier = [i for i in eligible if i.tags.locus == SUPPLIER_LOCUS]
    decisive = _distinct(vendor_side + supplier, MAX_DECISIVE_INCONCLUSIVE)
    if decisive:
        why = f"The evidence stops at indicators: {'; '.join(_describe(i) for i in decisive)}."
    else:
        why = "No item settles the question."
    if not ev.complete:
        why += f" {_sentence(_coverage_gap(ev.gaps))}."
    trace.append(f"f) Inconclusive: yes. {why}")
    return _Match("f", decisive=decisive, basis=decisive)


_RULES: tuple[Callable[[_Evidence, list[str]], _Match | None], ...] = (_rule_a, _rule_b, _rule_c, _rule_d, _rule_e)


# --------------------------------------------------------------------------- wording


def _likelihood(match: _Match, ev: _Evidence) -> str:
    """ICD 203 likelihood (2.7)."""
    if match.rule == "b":
        accountable = [i for i in _union(ev.qs, ev.ks) if i.tags.sr == "A"]
        return "almost certain" if _independent_pair(accountable) else "very likely"
    if match.rule == "c":
        return "likely"
    if match.rule == "d":
        return "very unlikely"
    if match.rule == "e":
        return "unlikely"
    return "roughly even chance" if ev.signals else "unlikely"


def _confidence(match: _Match, ev: _Evidence) -> tuple[str, str]:
    """ICD 203 confidence (2.7) and the clause completing 'Confidence is {level} because ...'."""
    if match.rule == "a":
        return "Low", ("a first-party or legally accountable statement dated on or after the evidence of use "
                       "contradicts it")
    if not ev.complete:
        return "Low", _coverage_gap(ev.gaps)
    basis = match.basis
    if basis and all(i.tags.sr in LOW_GRADE_SR for i in basis):
        return "Low", "only aggregated, user-generated or unattributed sources were found"
    accountable = [i for i in basis if i.tags.sr in ACCOUNTABLE_SR]
    settles = match.rule in ("b", "c", "d")
    if settles and _independent_pair(accountable):
        return "High", ("independent first-party or legally accountable sources agree and every mandatory source "
                        "family was searched")
    if accountable:
        if settles:
            return "Moderate", "only one independent first-party or legally accountable source supports the finding"
        return "Moderate", "a first-party or legally accountable source was found, but it does not settle the question"
    if sum(1 for i in basis if i.tags.sr == "C") >= 2:
        return "Moderate", "the finding rests on two or more promotional or second-party sources"
    if match.rule == "e":
        return "Moderate", "every mandatory source family was searched and no evidence of AI use was found"
    if basis:
        return "Low", "the finding rests on a single promotional or second-party source"
    return "Low", "no first-party or legally accountable source bears on the question"


def _historical_note(ev: _Evidence) -> str:
    old = sum(1 for i in ev.pool if i.tags.rc_level == 0)
    if not old:
        return ""
    return (f" {_sentence(_count(old, 'item'))} older than 36 months or undated count{'s' if old == 1 else ''} as "
            "historical context only.")


# --------------------------------------------------------------------------- public API


def decide(items: Iterable[EvidenceItem], plan: DepthPlan, coverage: Iterable[CoverageEntry]) -> UsageVerdict:
    """Column O for the plan's vendor (design 2.7): rules a)-f) in order, the first match wins.

    Items and coverage entries of other vendors are ignored, so a whole run's lists may be passed. Nothing is
    modified. Every list in the verdict holds item keys, best first, one item per origin cluster (except that a
    conflict always cites both the claim and the statement contradicting it).
    """
    coverage = list(coverage)
    seen: set[str] = set()
    pool: list[EvidenceItem] = []
    for item in sorted((i for i in items if i.vendor_id == plan.vendor_id and i.citable), key=rank_key):
        if item.item_key not in seen:  # one item per key (V9 normally guarantees it)
            seen.add(item.item_key)
            pool.append(item)
    ev = _Evidence(
        pool=pool,
        qs=[i for i in pool if is_qualifying(i)],
        ks=[i for i in pool if is_corroborating(i)],
        signals=[i for i in pool if _blocks_not_detected(i)],
        complete=depth.coverage_complete(plan, coverage),
        gaps=_incomplete_families(plan, coverage),
    )
    trace: list[str] = []
    match = next((m for rule in _RULES if (m := rule(ev, trace)) is not None), None) or _rule_f(ev, trace)
    trace[-1] += _historical_note(ev)
    confidence, reason = _confidence(match, ev)
    return UsageVerdict(
        rule=match.rule,
        likelihood=_likelihood(match, ev),
        confidence=confidence,
        confidence_reason=reason,
        qualifying=[i.item_key for i in _per_cluster(ev.qs, prefer=match.decisive)],
        corroborating=[i.item_key for i in _sources(match.corroborating, prefer=match.decisive)],
        decisive=[i.item_key for i in match.decisive],
        coverage_complete=ev.complete,
        trace=trace,
    )


def assign_roles(items: Sequence[EvidenceItem], verdict: UsageVerdict) -> list[EvidenceItem]:
    """Copies of the items, in input order, with their Evidence Log role (docs/contracts_p3.md section 1, role).

    Precedence: an item that is not citable is Logged; a limiting statement (U8) is Negative; then Primary (the
    first decisive item of a Yes verdict) and Supporting (the other decisive, Q and K items of a Yes verdict);
    Indicator (decisive items of an Inconclusive verdict); Counter-evidence (decisive items of a No verdict);
    Context (any other item with a Context label); everything else is Logged.
    """
    decisive = list(verdict.decisive)
    if verdict.column_o == "Yes":
        primary = decisive[0] if decisive else ""
        supporting = set(decisive[1:]) | set(verdict.qualifying) | set(verdict.corroborating)
        supporting.discard(primary)
        decisive_role = ""
    else:
        primary, supporting = "", set()
        decisive_role = "Indicator" if verdict.column_o == "Inconclusive" else "Counter-evidence"
    decisive_keys = set(decisive)

    def role(item: EvidenceItem) -> str:
        if not item.citable:
            return "Logged"
        if item.tags.u_class == LIMITING_CLASS:
            return "Negative"
        if item.item_key == primary:
            return "Primary"
        if item.item_key in supporting:
            return "Supporting"
        if decisive_role and item.item_key in decisive_keys:
            return decisive_role
        if item.strength in CONTEXT_STRENGTHS:
            return "Context"
        return "Logged"

    return [item.model_copy(update={"role": role(item)}) for item in items]
