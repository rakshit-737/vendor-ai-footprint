"""Compose (C10): the student cells L-V and the traceability sheets appended to the workbook.

Contracts: docs/contracts_p3.md §11. Design: Appendix A 2.8 (cell templates P-V), 2.11 (workbook I/O) and the main
plan's "Traceability & workbook I/O". The quality bar is the V-000 example in row 5 of the input workbook.

- ``assign_evidence_ids`` and ``coverage_ids`` give the E-IDs and C-IDs that cells and sheets cite.
- ``build_cells`` renders one vendor's L-V from its ``VendorFindings``. It is pure and deterministic and fits
  ``workbook.DEFAULT_LENGTH_BUDGETS``. A quote is never shortened or reworded (only layout whitespace is shown as one
  space, see ``display_quote``): to fit a budget, whole lower-priority items and negative findings are dropped.
- ``evidence_log_sheet``, ``coverage_log_sheet``, ``evidence_images_sheet`` and ``run_info_sheet`` build the appended
  sheets; ``method_legend_p3`` appends the P3/P4 sections to the Method & Legend sheet.

Style follows V-000: dates DD-MM-YYYY, URLs without the scheme, " — " (U+2014) as the separator, plain sentences.
Column P quotes only verified excerpts and adds no interpretation; columns Q, R and T are plain sentences with no
tag codes or hashes. Every action is Meridian's: the team never contacts a vendor.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

from footprint.criticality import Rubric, render_rationale
from footprint.depth import DepthConfig, load_depth_config, render_depth_cell
from footprint.models import (
    COMPLETE_STATUSES,
    CONTEXT_STRENGTHS,
    ROLE_ORDER,
    STRENGTH_ORDER,
    AssessmentResult,
    CoverageEntry,
    CoverageStatus,
    EvidenceItem,
    SheetSpec,
    SourceFamily,
    StudentCells,
    VendorFindings,
    arp_class,
    tg_for_missing,
)
from footprint.sheets import COVERAGE_HEADERS, COVERAGE_LOG_TITLE, add_section
from footprint.sheets import coverage_log_sheet as _coverage_rows
from footprint.workbook import DEFAULT_LENGTH_BUDGETS

Cell = str | int | float | None

SEP = " — "
"""' — ' (U+2014): the separator of V-000's N5, P5 and R5."""

CLOSING = "Entries are recorded in the Evidence Log sheet with retrieval dates and SHA-256 hashes."
"""Last sentence of column P when no cited item has a screenshot (it never claims screenshots that do not exist)."""

CLOSING_SHOTS = "Entries are recorded in the Evidence Log sheet with retrieval dates and screenshots."
"""Last sentence of column P (design 2.8) when at least one cited item has an excerpt screenshot."""


def closing_sentence(items: Sequence[EvidenceItem]) -> str:
    """CLOSING_SHOTS when a cited (non-Logged) item has a screenshot file, else CLOSING."""
    has = any(i.screenshot_path and i.role != "Logged" for i in items)
    return CLOSING_SHOTS if has else CLOSING

NO_EXCERPT = "No verified excerpt evidences AI use in the service."
"""Opens column P for an Inconclusive or No verdict that has no decisive item to quote."""

EVIDENCE_LOG_TITLE = "Evidence Log"
EVIDENCE_IMAGES_TITLE = "Evidence Images"
RUN_INFO_TITLE = "Run Info"

PRIMARY = "Primary source"
SUPPORTING = "Supporting source"
LIMITING = "Limiting statement"
COUNTER = "Counter-evidence"
CONFLICTING = f"Indicator{SEP}contradicted by a limiting statement"
"""Column P prefix for the Q item of a conflict (rule a) *(interpretation: the design names no prefix for it)*."""
UNCORROBORATED = f"Indicator{SEP}single source, not corroborated"
"""Column P prefix for a Moderate item of an Inconclusive verdict (rule f) *(interpretation)*."""
DNS_SUFFIX = " (DNS TXT record)"

P_PREFIXES: dict[str, str] = {
    "Context - relationship only": f"Indicator{SEP}relationship only",
    "Context - platform supplier": f"Indicator{SEP}platform supplier capability",
    "Context - inferred affiliate": f"Indicator{SEP}inferred affiliate, not confirmed",
    "Marketing only": f"Marketing statement{SEP}not confirmatory",
    "Weak": f"Indicator{SEP}weak",
    "Negative": LIMITING,
}
"""Column P prefix by strength label for an Inconclusive or No verdict (design 2.3). A relationship-only item from
DNS gets DNS_SUFFIX: "Indicator — relationship only (DNS TXT record)"."""

EVIDENCE_LOG_HEADERS: list[str] = [
    "Evidence ID", "Vendor ID", "Role", "Status", "Strength", "Tags", "AI type", "Indicators", "Family",
    "Source type", "Publisher", "Title", "URL", "Retrieved URL", "Published", "Date basis", "Retrieved (UTC)",
    "Excerpt", "Offsets", "Excerpt SHA-256", "Capture SHA-256", "Text SHA-256", "Screenshot", "Screenshot SHA-256",
    "Visible in render", "Providers", "Data mentioned", "Temporal", "Action level", "Method", "LLM model",
    "Prompt SHA-256", "Rule labels", "LLM labels", "Cluster", "Corroborates (E-IDs)", "Review", "Review reason",
    "Reviewer", "Item key",
]
"""The contract's Evidence Log columns plus Status, AI type and Indicators, which show why a row counts (or not)."""
EVIDENCE_LOG_WIDTHS: list[float] = [
    15, 9, 14, 24, 24, 44, 14, 40, 8, 22, 20, 36, 50, 50, 11, 18, 21, 80, 12, 30, 30, 30, 36, 30, 10, 24, 30, 14, 22,
    18, 22, 30, 40, 40, 14, 30, 12, 40, 18, 30,
]
EVIDENCE_IMAGES_HEADERS: list[str] = ["Evidence ID", "Vendor ID", "Screenshot", "SHA-256", "Visible in render", "URL"]
RUN_INFO_HEADERS: list[str] = ["Item", "Value"]

_ORDINALS = ("First", "Second", "Third", "Fourth", "Fifth")
_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                 "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty")
_MAX_IDS = 3          # E-IDs listed per sentence before "and n more"
_YES_ITEMS = 3        # design 2.8: up to 3 Q/K items for Yes
_INCONCLUSIVE_ITEMS = 2
_QUOTE_STRENGTHS = frozenset({"Strong", "Moderate"})
_TERMINAL = ".!?…"
_CLOSERS = "\"')]”’"

# Plain words for tags. Q and T use them so that no tag code reaches a cell.
AI_TYPE_WORDS: dict[str, str] = {
    "predictive_ml": "predictive machine learning",
    "genai_llm": "generative AI (large language models)",
    "agentic": "agentic AI",
    "document_ai": "document AI",
    "conversational": "conversational AI",
    "aiops": "AI for IT operations",
    "unspecified": "AI",
    "not_ai": "automation described as AI",
}
_AI_SHORT: dict[str, str] = {
    "predictive_ml": "machine learning", "genai_llm": "generative AI", "agentic": "agentic AI",
    "document_ai": "document AI", "conversational": "conversational AI", "aiops": "AI for IT operations",
    "unspecified": "AI", "not_ai": "automation described as AI",
}
LOCUS_WORDS: dict[str, str] = {
    "service_feature": "within the service itself",
    "vendor_addon": "in a vendor platform or add-on",
    "delivery_ops": "in the vendor's delivery operations",
    "sdlc": "in the vendor's software development",
    "corporate_internal": "in the vendor's internal corporate functions",
    "platform_supplier": "offered by a platform supplier",
    "affiliate_inferred": "at an inferred affiliate",
    "relationship": "through a provider relationship",
    "commentary": "in commentary",
    "unknown": "in an unspecified part of the business",
}
_ROLE_LOCUS: dict[str, str] = {
    "service_feature": "in the service itself", "vendor_addon": "in a vendor platform or add-on",
    "delivery_ops": "in delivery operations", "sdlc": "in software development",
    "corporate_internal": "for internal corporate use",
}
_ACTION_WORDS: dict[str, str] = {
    "advisory": "producing advisory output only",
    "human_reviewed_decision": "with decisions reviewed by staff",
    "automated_action": "taking automated action",
}
_ACTION_RANK = {"unknown": 0, "none": 1, "advisory": 2, "human_reviewed_decision": 3, "automated_action": 4}
GAP_WORDS: dict[str, str] = {
    "t1": "AI use in the service",
    "t2": "named AI providers",
    "t3": "data-use terms",
    "t4": "human oversight",
    "t5": "an AI governance attestation",
    "t6": "incident or change notification",
}
"""The six transparency checks in plain words (design 2.8)."""
_REGISTER_GAPS: tuple[tuple[str, str], ...] = (
    ("t2", "named AI providers or an AI sub-processor list"),
    ("t5", "an AI policy or AI governance attestation"),
    ("t3", "AI data-use terms"),
)
ESCALATOR_WORDS: dict[str, str] = {
    "X1": "a provider named by a third party but not by the vendor, with exposure of 2 or more",
    "X2": "training or retention without an opt-out, with exposure of 2 or more",
    "X3": "agentic or privileged AI acting on Meridian production without approval",
    "X4": "automated action without per-case review on credit, account access or payment holds",
    "X5": "an AI incident in the last 24 months",
    "X6": "a single foundation-model provider behind a Critical vendor",
}
"""The escalators X1-X6 as the Method & Legend sheet defines them (design 2.8)."""
_ESCALATOR_SHORT: dict[str, str] = {
    "X1": "a provider named only by a third party",
    "X2": "training or retention without an opt-out",
    "X3": "agentic or privileged AI acting on Meridian production without approval",
    "X4": "unreviewed automated action on credit, account access or payment holds",
    "X5": "an AI incident in the last 24 months",
    "X6": "a single foundation-model provider behind a Critical vendor",
}
"""The escalators as column T names them (the exposure condition is already met when a floor applies)."""

# Column P negative findings: family -> (what was not found, singular noun, plural noun) *(interpretation)*.
_FAMILY_NEGATIVES: dict[SourceFamily, tuple[str, str, str]] = {
    SourceFamily.LEG: ("AI or automated-decision terms", "legal and trust page", "legal and trust pages"),
    SourceFamily.REG: ("AI statements", "SEC filing document", "SEC filing documents"),
    SourceFamily.PRD: ("AI statements", "product or newsroom page", "product and newsroom pages"),
    SourceFamily.JOB: ("AI duties or named AI tools", "job posting", "job postings"),
    SourceFamily.IND: ("AI statements about the vendor", "independent source", "independent sources"),
    SourceFamily.EXEC: ("AI statements", "executive channel capture", "executive channel captures"),
}
_EMPTY_FAMILY: dict[SourceFamily, str] = {
    SourceFamily.REG: "AI statements not found on an EDGAR full-text search of the vendor's filings",
    SourceFamily.PRD: "product and newsroom pages not found on the vendor's website",
    SourceFamily.JOB: "AI duties or named AI tools not found on the vendor's job postings",
}
"""Negative findings for a completed family that yielded no document. Other families say nothing when empty."""
_FAMILY_NAMES: dict[SourceFamily, str] = {
    SourceFamily.LEG: "legal and trust pages", SourceFamily.REG: "regulatory filings",
    SourceFamily.PRD: "product pages and newsroom", SourceFamily.JOB: "job postings", SourceFamily.DNS: "DNS records",
    SourceFamily.IND: "independent corroboration", SourceFamily.HIST: "archive history",
    SourceFamily.EXEC: "executive channels",
}
_SUBPROCESSOR = re.compile(r"sub-?processor", re.IGNORECASE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(“])")


# --------------------------------------------------------------------------- IDs


def assign_evidence_ids(items: Sequence[EvidenceItem], vendor_id: str) -> list[EvidenceItem]:
    """Number every item of the vendor ``{vendor_id}-E-0001``, ``-E-0002``, ... and return copies in that order.

    The order is ROLE_ORDER, STRENGTH_ORDER, SR, RL (high first), RC (recent first), url, start, item_key, so E-0001
    is the Primary item once ``verdict.assign_roles`` has run. Existing ids are replaced. Raises ValueError for an
    item of another vendor, a duplicate item_key, or more than 9,999 items.
    """
    foreign = sorted({item.vendor_id for item in items if item.vendor_id != vendor_id})
    if foreign:
        raise ValueError(f"items of {', '.join(foreign)} cannot be numbered as {vendor_id}")
    keys = [item.item_key for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate item_key: remove duplicates (V9) before assigning evidence ids")
    if len(items) > 9999:
        raise ValueError("an evidence id has four digits, so at most 9,999 items per vendor")
    ordered = sorted(items, key=_id_order)
    return [item.model_copy(update={"evidence_id": f"{vendor_id}-E-{n:04d}"}) for n, item in enumerate(ordered, 1)]


def _id_order(item: EvidenceItem) -> tuple[Any, ...]:
    tags = item.tags
    return (ROLE_ORDER.index(item.role), STRENGTH_ORDER.index(tags.strength), tags.sr, -tags.rl_level,
            -tags.rc_level, item.url, item.start, item.item_key)


def coverage_ids(coverage: Sequence[CoverageEntry]) -> list[str]:
    """``{vendor_id}-C-01``, ``-C-02``, ... by position; each vendor's entries are numbered separately."""
    counts: dict[str, int] = {}
    ids: list[str] = []
    for entry in coverage:
        counts[entry.vendor_id] = counts.get(entry.vendor_id, 0) + 1
        ids.append(f"{entry.vendor_id}-C-{counts[entry.vendor_id]:02d}")
    return ids


# --------------------------------------------------------------------------- cells


def build_cells(findings: VendorFindings, team: str | None, assessed_on: str, *, rubric: Rubric | None = None,
                depth_config: DepthConfig | None = None) -> StudentCells:
    """Columns L-V for one vendor, within ``workbook.DEFAULT_LENGTH_BUDGETS`` (design 2.8; contracts §11).

    Pure and deterministic: the same findings give the same cells whatever the order of ``findings.evidence``.
    ``assessed_on`` is an ISO date; column V is None when ``team`` is empty. Raises ValueError when a cited item has
    no evidence id (``assign_evidence_ids`` must run first) or ``assessed_on`` is not an ISO date.
    """
    assessed_by = render_assessed_by(team, assessed_on)
    cfg = depth_config or load_depth_config()
    view = _View(findings, cfg)
    return StudentCells(
        criticality_tier=findings.criticality.tier.value,
        criticality_rationale=render_rationale(findings.profile, findings.criticality, rubric),
        assessment_depth=render_depth_cell(findings.depth, findings.coverage or None, config=cfg),
        ai_usage_detected=findings.verdict.column_o,
        evidence=_cell_p(view),
        how_ai_used=_cell_q(view),
        ai_subprocessors=_cell_r(view),
        ai_risk_class=findings.risk.final_class,
        risk_rationale=_cell_t(view),
        recommended_action=_cell_u(findings),
        assessed_by=assessed_by,
    )


def render_assessed_by(team: str | None, assessed_on: str) -> str | None:
    """Column V: ``"Team {name} / {DD-MM-YYYY}"`` ("Team " added once when missing); None without a team."""
    day = _dmy(assessed_on, strict=True)
    name = " ".join((team or "").split())
    if not name:
        return None
    if not name.casefold().startswith("team "):
        name = f"Team {name}"
    return f"{name} / {day}"


class _View:
    """Read-only lookups over one vendor's findings, shared by the cell renderers."""

    def __init__(self, findings: VendorFindings, cfg: DepthConfig) -> None:
        self.f = findings
        self.cfg = cfg
        self.verdict = findings.verdict
        self.risk = findings.risk
        self.inputs = findings.risk.inputs
        self.items = sorted(findings.evidence, key=_eid_order)
        self.citable = [item for item in self.items if item.citable]
        self.cids = coverage_ids(findings.coverage)
        self.by_key = {item.item_key: item for item in findings.evidence}

    @property
    def yes(self) -> bool:
        return self.verdict.column_o == "Yes"

    def decisive(self) -> list[EvidenceItem]:
        """The verdict's decisive items that may be cited, best first."""
        found = (self.by_key.get(key) for key in self.verdict.decisive)
        return [item for item in found if item is not None and item.citable]

    def entries(self, family: SourceFamily) -> list[tuple[CoverageEntry, str]]:
        """(entry, C-ID) for the vendor's mandatory Coverage Log entries of one family, in log order."""
        return [(e, cid) for e, cid in zip(self.f.coverage, self.cids)
                if e.family == family and e.mandatory and e.vendor_id == self.f.vendor_id]

    def pathways(self) -> list[list[EvidenceItem]]:
        """Usage pathways: citable Q/K-grade items (Strong or Moderate, U1-U4), grouped by where the AI sits (locus),
        in E-ID order, so the Primary item's pathway comes first."""
        items = [i for i in self.citable if i.strength in _QUOTE_STRENGTHS and i.tags.u_number <= 4]
        return _group(items, lambda i: i.tags.locus)

    def has_ai_register(self) -> bool:
        """The vendor's own public AI sub-processor register was cited: a sub-processor list, or a legal page of the
        vendor naming AI providers (a platform supplier's terms do not count)."""
        return any(_vendor_source(item) and (_SUBPROCESSOR.search(item.source_type)
                                             or (item.family == SourceFamily.LEG and bool(item.providers)))
                   for item in self.citable)

    def vendor_publisher(self, publisher: str) -> bool:
        """The publisher is the vendor itself (its short name is part of the profile name, or the reverse)."""
        pub = re.sub(r"[^0-9a-z]", "", publisher.casefold())
        name = re.sub(r"[^0-9a-z]", "", self.f.profile.name.casefold())
        label = re.sub(r"[^0-9a-z]", "", self.f.profile.domain.split(".")[0].casefold())
        return bool(pub) and (pub in name or (bool(name) and name in pub) or (len(label) >= 3 and label == pub))

    def incomplete_families(self) -> list[tuple[SourceFamily, str]]:
        """(family, status) for mandatory families of the plan without a complete Coverage Log entry."""
        out: list[tuple[SourceFamily, str]] = []
        for fp in self.f.depth.families:
            if not fp.mandatory:
                continue
            statuses = [e.status for e, _ in self.entries(fp.family)]
            if not any(s in COMPLETE_STATUSES for s in statuses):
                out.append((fp.family, statuses[-1].value if statuses else "missing"))
        order = list(self.cfg.family_order)
        return sorted(out, key=lambda pair: order.index(pair[0]) if pair[0] in order else len(order))


# --------------------------------------------------------------------------- column P


def _cell_p(view: _View) -> str:
    """Column P (design 2.8): verbatim excerpts by verdict, negative findings, then the closing sentence.

    Quotes are never cut. Over budget, whole segments are dropped by priority: for Yes the Primary, the first
    Supporting item, the register finding, the third item, then the family findings; otherwise the first item, the
    register finding, the second item, then the family findings. The closing sentence always stays.
    """
    verdict = view.verdict
    decisive = view.decisive()
    if view.yes:
        quoted = [i for i in decisive if i.strength in _QUOTE_STRENGTHS][:_YES_ITEMS]
        texts = [_cite(item, PRIMARY if n == 0 else SUPPORTING) for n, item in enumerate(quoted)]
    elif verdict.column_o == "Inconclusive":
        texts = [_cite(item, _inconclusive_prefix(item, verdict.conflict)) for item in decisive[:_INCONCLUSIVE_ITEMS]]
    else:
        texts = [_cite(item, LIMITING if item.strength == "Negative" else COUNTER) for item in decisive[:1]]
    register = _register_finding(view)
    families = _family_findings(view)

    segments: list[tuple[int, int, str]] = []          # (priority, render order, text)
    if not texts and not view.yes:
        segments.append((0, 0, NO_EXCERPT))
    item_priority = (0, 1, 3) if view.yes else (0, 2, 3)
    for n, text in enumerate(texts):
        segments.append((item_priority[n], 1 + n, text))
    if register:
        segments.append((2 if view.yes else 1, 10, register))
    for n, text in enumerate(families):
        segments.append((10 + n, 20 + n, text))
    return _fit_segments(segments, closing_sentence(view.items), DEFAULT_LENGTH_BUDGETS["evidence"])


def _inconclusive_prefix(item: EvidenceItem, conflict: bool) -> str:
    strength = item.strength
    if strength in _QUOTE_STRENGTHS:
        return CONFLICTING if conflict else UNCORROBORATED
    prefix = P_PREFIXES[strength]
    if strength == "Context - relationship only" and _is_dns(item):
        prefix += DNS_SUFFIX
    return prefix


def _cite(item: EvidenceItem, prefix: str) -> str:
    """'{prefix} — {type}, {publisher}, {date}, {url} (retrieved {date}; Evidence Log {E-ID}): "{excerpt}"'.

    A prefix that already holds ' — ' is followed by a comma (design 2.3); a source type the prefix already names is
    not repeated; a page dated by retrieval carries no publication date, any other undated source says 'undated'.
    """
    parts: list[str] = []
    if item.source_type and f"({item.source_type})".casefold() not in prefix.casefold():
        parts.append(item.source_type)
    if item.publisher:
        parts.append(item.publisher)
    if item.published:
        parts.append(_dmy(item.published))
    elif item.date_basis != "retrieval":
        parts.append("undated")
    parts.append(display_url(item.url))
    joiner = ", " if SEP in prefix else SEP
    head = prefix + joiner + ", ".join(parts)
    quote = display_quote(item.excerpt)
    return f'{head} (retrieved {_dmy(item.retrieved_at)}; Evidence Log {_eid(item)}): "{quote}"{_full_stop(quote)}'


def display_quote(excerpt: str) -> str:
    """The excerpt as a cell quotes it: every character kept in order, only runs of whitespace (line breaks from PDF
    or HTML layout, doubled spaces) shown as one space, as a browser renders them *(interpretation of "verbatim")*.
    The exact slice, its offsets and its SHA-256 stay in the Evidence Log."""
    return " ".join(excerpt.split())


def _full_stop(excerpt: str) -> str:
    """'.' after the closing quote when the quote itself does not end a sentence (logical punctuation, as V-000)."""
    tail = excerpt.rstrip().rstrip(_CLOSERS)
    return "" if tail and tail[-1] in _TERMINAL else "."


def _register_finding(view: _View) -> str | None:
    """The legal-family negative finding: missing registers, or legal pages with no AI terms, or none found.

    'Negative finding — {missing registers} not found on {where} (Coverage Log ...).' Only when every mandatory
    legal-family entry is complete, so an unsearched page is never reported as absent. The registers are an AI
    sub-processor list (none cited from the legal pages; named AI providers too when t2 is open), an AI policy or
    attestation (t5) and AI data-use terms (t3) *(interpretation of "each missing register")*, so P agrees with R.
    """
    leg = view.entries(SourceFamily.LEG)
    if not leg or not all(e.status in COMPLETE_STATUSES for e, _ in leg):
        return None
    if all(e.status == CoverageStatus.NOT_APPLICABLE for e, _ in leg):
        return None
    refs = ", ".join(cid for _, cid in leg)
    docs = sum(e.documents for e, _ in leg)
    if docs == 0:
        return (f"Negative finding{SEP}legal and trust pages (privacy notice, sub-processor list, AI policy) not "
                f"found on the vendor's website (Coverage Log {refs}).")
    gaps = view.inputs.gaps
    has_list = view.has_ai_register()
    missing: list[str] = []
    for key, words in _REGISTER_GAPS:
        if key == "t2":
            if not has_list:
                missing.append(words if gaps.get("t2") else "an AI sub-processor list")
            elif gaps.get("t2"):
                missing.append("named AI providers")
        elif gaps.get(key):
            missing.append(words)
    pages = f"{_count(docs)} {_plural(docs, 'legal and trust page', 'legal and trust pages')}"
    if sum(e.ai_passages for e, _ in leg) == 0:
        if missing:
            return (f"Negative finding{SEP}{_join(missing)} not found on {pages}, which "
                    f"{_plural(docs, 'holds', 'hold')} no AI or automated-decision terms (Coverage Log {refs}).")
        return f"Negative finding{SEP}AI or automated-decision terms not found on {pages} (Coverage Log {refs})."
    if not missing:
        return None
    return (f"Negative finding{SEP}{_join(missing)} not found on the vendor's legal and trust pages "
            f"(Coverage Log {refs}).")


def _family_findings(view: _View) -> list[str]:
    """'Negative finding — {what} not found on {where} (Coverage Log {C-IDs}).' per mandatory family whose entries
    are all complete and found no AI passage, in column N's family order. The legal family is the register finding;
    archive history only dates pages, so it is never a negative finding."""
    out: list[str] = []
    for family in view.cfg.family_order:
        entries = view.entries(family)
        if not entries or family in (SourceFamily.HIST, SourceFamily.LEG):
            continue
        if not all(e.status in COMPLETE_STATUSES for e, _ in entries) or sum(e.ai_passages for e, _ in entries):
            continue
        refs = ", ".join(cid for _, cid in entries)
        searched = [e for e, _ in entries if e.status != CoverageStatus.NOT_APPLICABLE]
        if not searched:
            if family == SourceFamily.REG:
                out.append(f"Negative finding{SEP}an SEC registration not found on EDGAR (Coverage Log {refs}).")
            elif family == SourceFamily.JOB:
                out.append(f"Negative finding{SEP}an applicant tracking system not found on the vendor's website "
                           f"(Coverage Log {refs}).")
            continue
        if family == SourceFamily.DNS:
            out.append(f"Negative finding{SEP}AI-provider verification tokens not found on the vendor's DNS records "
                       f"(Coverage Log {refs}).")
            continue
        what, one, many = _FAMILY_NEGATIVES[family]
        docs = sum(e.documents for e in searched)
        if docs == 0:
            if family in _EMPTY_FAMILY:
                out.append(f"Negative finding{SEP}{_EMPTY_FAMILY[family]} (Coverage Log {refs}).")
            continue
        out.append(f"Negative finding{SEP}{what} not found on {_count(docs)} {one if docs == 1 else many} "
                   f"(Coverage Log {refs}).")
    return out


def _fit_segments(segments: Sequence[tuple[int, int, str]], closing: str, budget: int) -> str:
    """Keep segments in priority order while they fit (the closing sentence always stays); render in order."""
    chosen: list[tuple[int, str]] = []
    used = len(closing)
    for _, order, text in sorted(segments, key=lambda s: (s[0], s[1])):
        if used + len(text) + 1 <= budget:
            chosen.append((order, text))
            used += len(text) + 1
    return " ".join([text for _, text in sorted(chosen)] + [closing])


# --------------------------------------------------------------------------- column Q


def _cell_q(view: _View) -> str:
    """Column Q (design 2.8): numbered usage pathways with E-IDs, indicators, and the unknowns sent to the
    questionnaire. DNS tokens and platform options are a "relationship indicator, not a confirmed sub-processor"."""
    groups = view.pathways()
    indicators = _indicator_sentences(view)
    closing = _unknowns_sentence(view, groups)
    budget = DEFAULT_LENGTH_BUDGETS["how_ai_used"]

    def render(listed: int, n_indicators: int, detail: int) -> str:
        parts = [_pathway_lead(len(groups), view.yes)]
        parts += [_pathway_sentence(_ORDINALS[n], group, view, detail) for n, group in enumerate(groups[:listed])]
        rest = len(groups) - listed
        if rest:
            parts.append(f"{_count(rest, capital=True)} further {_plural(rest, 'pathway is', 'pathways are')} "
                         "recorded in the Evidence Log.")
        parts += indicators[:n_indicators]
        parts.append(closing)
        return " ".join(parts)

    def candidates() -> Iterator[str]:
        top = min(len(groups), len(_ORDINALS))
        for detail in (_FULL, _NO_FEATURES):
            yield render(top, len(indicators), detail)
        for n_indicators in range(len(indicators), -1, -1):
            yield render(top, n_indicators, _COMPACT)
        for listed in range(top - 1, -1, -1):
            yield render(listed, 0, _COMPACT)

    return _first_fit(candidates(), budget)


_FULL, _NO_FEATURES, _COMPACT = 0, 1, 2
"""Detail levels of a pathway sentence: everything; without the vendor's feature terms; AI, place and action only."""
_MAX_FEATURES = 3


def _pathway_lead(n: int, yes: bool) -> str:
    if n == 0:
        return "No usage pathway is evidenced in public material."
    noun = _plural(n, "usage pathway is", "usage pathways are")
    if yes:
        return f"{_count(n, capital=True)} {noun} indicated."
    noun = _plural(n, "possible usage pathway is", "possible usage pathways are")
    return f"{_count(n, capital=True)} {noun} indicated, not confirmed."


def _pathway_sentence(ordinal: str, group: Sequence[EvidenceItem], view: _View, detail: int = _FULL) -> str:
    """'First, {AI types} {locus}, described as "{feature}", applied to {data}, using {providers}, {action}; a
    third-party source names {providers} ({E-IDs}).' The feature terms are the vendor's own words (the spans of the
    named-feature indicator G2), quoted. Providers only a third party names are attributed to it, never to the
    vendor."""
    words = f"{_ai_words(group)} {LOCUS_WORDS[group[0].tags.locus]}"
    own: list[str] = []
    others: list[str] = []
    if detail == _FULL:
        features = _features(group)
        if features:
            words += ", described as " + _join([f'"{f}"' for f in features])
    if detail < _COMPACT:
        data = _unique(_lower_first(" ".join(d.split())) for item in group for d in item.data_mentioned)
        if data:
            words += f", applied to {_join(data[:3])}"
        own = _unique(p for item in group if _vendor_source(item) for p in item.providers)
        named = {p.casefold() for p in own}
        others = [p for p in _unique(p for item in group for p in item.providers) if p.casefold() not in named]
        if own:
            words += f", using {_join(own[:4])}"
    action = max((item.action_level for item in group), key=_ACTION_RANK.__getitem__)
    if action in _ACTION_WORDS:
        words += f", {_ACTION_WORDS[action]}"
    if all(item.temporal == "pilot_or_beta" for item in group):
        words += ", in pilot or beta"
    if view.verdict.conflict and any(item.item_key in view.verdict.decisive for item in group):
        words += ", contradicted by a limiting statement"
    if others:
        words += f"; a third-party source names {_join(others[:3])}"
    return f"{ordinal}, {words} ({_id_list(group)})."


def _ai_words(group: Sequence[EvidenceItem], *, limit: int = 0) -> str:
    """The kinds of AI in a group, in item order. One kind is named in full ('generative AI (large language
    models)'); several are named briefly ('agentic AI, generative AI and machine learning'), at most ``limit`` of them
    when given. Plain 'AI' is left out when a more specific kind is named."""
    types = _unique(item.tags.ai_type for item in group)
    specific = [t for t in types if t != "unspecified"] or types
    if len(specific) == 1:
        return AI_TYPE_WORDS[specific[0]]
    kinds = _unique(_AI_SHORT[t] for t in specific)
    return _join(kinds[:limit] if limit else kinds)


def _features(group: Sequence[EvidenceItem], limit: int = _MAX_FEATURES) -> list[str]:
    """Up to ``limit`` distinct feature terms the sources use (G2 spans, whitespace collapsed), in item order."""
    spans = (" ".join(ind.span.split()) for item in group for ind in item.indicators if ind.code == "G2")
    return _unique(span for span in spans if len(span) >= 3)[:limit]


def _indicator_sentences(view: _View) -> list[str]:
    """Plain sentences for cited items that are not pathways: relationship, platform supplier and inferred-affiliate
    context, marketing, weak indicators and limiting statements."""
    pathway_keys = {item.item_key for group in view.pathways() for item in group}
    rest = [item for item in view.citable if item.item_key not in pathway_keys]
    context = [item for item in rest if item.strength in CONTEXT_STRENGTHS]
    cited = [item for item in rest if item.strength not in CONTEXT_STRENGTHS and item.role != "Logged"]

    sentences: list[str] = []
    dns = [i for i in context if i.strength == "Context - relationship only" and _is_dns(i)]
    if dns:
        providers = _unique(p for item in dns for p in item.providers)
        who = f"{_join(providers)} domain-verification tokens" if providers else "AI-provider verification tokens"
        sentences.append(f"{who} in the vendor's DNS records are a relationship indicator, not a confirmed "
                         f"sub-processor ({_id_list(dns)}).")
    listing = [i for i in context if i.strength == "Context - relationship only" and not _is_dns(i)]
    if listing:
        providers = _unique(p for item in listing for p in item.providers)
        if providers:
            sentences.append(f"Named AI relationships ({_join(providers[:6])}) are a relationship indicator, not a "
                             f"confirmed sub-processor ({_id_list(listing)}).")
        else:
            sentences.append(f"A stated AI provider relationship is a relationship indicator, not a confirmed "
                             f"sub-processor ({_id_list(listing)}).")
    platform = [i for i in context if i.strength == "Context - platform supplier"]
    if platform:
        providers = _unique(p for item in platform for p in item.providers)
        suppliers = _unique(i.publisher for i in platform if i.publisher and not view.vendor_publisher(i.publisher))
        named = f" ({_join(providers[:5])})" if providers else ""
        if suppliers:
            noun = _plural(len(suppliers), "a platform supplier", "platform suppliers")
            sentences.append(f"AI options of {_join(suppliers[:3])}, {noun} the vendor uses{named}, are a "
                             f"relationship indicator, not a confirmed sub-processor ({_id_list(platform)}).")
        else:
            sentences.append(f"AI options of a platform supplier the vendor uses{named} are a relationship "
                             f"indicator, not a confirmed sub-processor ({_id_list(platform)}).")
    affiliate = [i for i in context if i.strength == "Context - inferred affiliate"]
    if affiliate:
        kinds = _unique(AI_TYPE_WORDS[i.tags.ai_type] for i in affiliate)
        verb = _plural(len(kinds), "is", "are")
        sentences.append(f"{_capital(_join(kinds))} described by an inferred affiliate {verb} not confirmed as used "
                         f"in the service ({_id_list(affiliate)}).")
    negative = [i for i in cited if i.strength == "Negative"]
    if negative:
        sentences.append(f"The vendor's own statements limit or exclude AI use ({_id_list(negative)}).")
    marketing = [i for i in cited if i.strength == "Marketing only"]
    if marketing:
        sentences.append(f"Marketing or forward-looking statements about AI are not confirmatory "
                         f"({_id_list(marketing)}).")
    weak = [i for i in cited if i.strength == "Weak"]
    if weak:
        sentences.append(f"Weak indicators mention AI without tying it to the service ({_id_list(weak)}).")
    return sentences


def _unknowns_sentence(view: _View, groups: Sequence[Sequence[EvidenceItem]]) -> str:
    """'The extent of {unknowns} is undetermined from public material and is carried forward to the vendor
    questionnaire.' Up to three unknowns, from the verdict, assumed risk inputs and missing transparency checks."""
    if view.verdict.column_o == "No":
        return ("Whether AI is used in the service is not evidenced by public material, so written confirmation "
                "is carried forward to the vendor questionnaire.")
    inputs, gaps = view.inputs, view.inputs.gaps
    unknowns: list[str] = []
    if not view.yes:
        unknowns.append("AI use within the service itself")
    if inputs.e_assumed or not view.yes:
        unknowns.append("Meridian data reaching AI models")
    actions = [item.action_level for group in groups for item in group]
    if inputs.k_assumed or gaps.get("t4") or not actions or all(a == "unknown" for a in actions):
        unknowns.append("human review over AI output")
    if gaps.get("t2"):
        unknowns.append("model-provider involvement")
    if gaps.get("t3"):
        unknowns.append("retention or training use of Meridian data")
    # public material never shows Meridian's own data, so a Yes always carries that question forward
    unknowns = _unique(unknowns)[:3] or ["Meridian data reaching these AI uses"]
    return (f"The extent of {_join(unknowns)} is undetermined from public material and is carried forward to the "
            "vendor questionnaire.")


# --------------------------------------------------------------------------- column R


def _cell_r(view: _View) -> str:
    """Column R (design 2.8): only the AI providers the vendor itself names publicly, each with its role and E-IDs;
    otherwise "None named by the vendor — {status}." DNS tokens and third-party mentions never appear here."""
    named = _vendor_named(view)
    register = _register_sentence(view)
    budget = DEFAULT_LENGTH_BUDGETS["ai_subprocessors"]
    if not named:
        status = view.verdict.label if view.verdict.column_o == "No" else "Inconclusive"
        lead = f"None named by the vendor{SEP}{status}."
        tail = ("Provider identity, hosting region and retention terms are raised with the vendor through "
                "Meridian's questionnaire.")
        return _first_fit([" ".join(p for p in (lead, register, tail) if p), f"{lead} {tail}"], budget)
    tail = ("Hosting region, retention and training terms are not stated publicly and are raised with the vendor "
            "through Meridian's questionnaire.")

    def render(listed: int, compact: bool, with_register: bool) -> str:
        clauses = [_provider_clause(names, items, compact) for names, items in named[:listed]]
        text = "Named by the vendor: " + "; ".join(clauses)
        rest = sum(len(names) for names, _ in named[listed:])  # providers left out, each counted once
        if rest:
            text += f"; {_count(rest)} more {_plural(rest, 'provider', 'providers')} in the Evidence Log"
        parts = [text + ".", register if with_register else "", tail]
        return " ".join(p for p in parts if p)

    def candidates() -> Iterator[str]:
        yield render(len(named), False, True)
        yield render(len(named), True, True)
        yield render(len(named), True, False)
        for listed in range(len(named) - 1, 0, -1):
            yield render(listed, True, False)

    return _first_fit(candidates(), budget)


def _vendor_named(view: _View) -> list[tuple[list[tuple[str, list[str]]], list[EvidenceItem]]]:
    """([(provider, [names as written])], items) the vendor itself names, in E-ID order; providers resting on the
    same items share one entry. When the transparency checks were assessed they decide (t2 missing gives none; t2's
    items give the providers), so R and T always agree."""
    gaps, inputs = view.inputs.gaps, view.inputs
    if gaps and gaps.get("t2"):
        return []
    keys = set(inputs.gap_items.get("t2", []))
    items = [i for i in view.citable if i.item_key in keys and i.providers]
    if not items:
        items = [i for i in view.citable if i.providers and _first_party_naming(i)]
    # one entry per provider: a product or model name the risk policy maps to its maker is shown under the maker,
    # for example "OpenAI (ChatGPT, Azure OpenAI)"
    by_provider: dict[str, tuple[str, list[str], list[EvidenceItem]]] = {}
    for item in items:
        for provider in item.providers:
            name = " ".join(provider.split())
            if not name:
                continue
            label = canonical_provider(name)
            _, variants, found = by_provider.setdefault(label.casefold(), (label, [], []))
            if name.casefold() != label.casefold() and name.casefold() not in {v.casefold() for v in variants}:
                variants.append(name)
            if all(f.item_key != item.item_key for f in found):
                found.append(item)
    grouped: dict[tuple[str, ...], tuple[list[tuple[str, list[str]]], list[EvidenceItem]]] = {}
    for label, variants, found in by_provider.values():
        signature = tuple(item.item_key for item in found)
        grouped.setdefault(signature, ([], found))[0].append((label, variants))
    return list(grouped.values())


def canonical_provider(name: str) -> str:
    """The maker a provider or model name belongs to, from the ``[foundation_models]`` aliases of config/risk.toml
    ('ChatGPT' -> 'OpenAI', 'Claude Code' -> 'Anthropic'); any other name as written, whitespace tidied."""
    text = " ".join(name.split())
    for canonical, pattern in _provider_aliases():
        if pattern.search(text):
            return canonical
    return text


@functools.lru_cache(maxsize=1)
def _provider_aliases() -> tuple[tuple[str, re.Pattern[str]], ...]:
    from footprint.risk import load_risk_config  # the provider policy lives with the risk matrix

    models = load_risk_config().foundation_models
    pairs = sorted(((maker, alias) for maker, aliases in models.items() for alias in aliases),
                   key=lambda pair: (-len(pair[1]), pair[1]))
    return tuple((maker, re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE)) for maker, alias in pairs)


_THIRD_PARTY_LOCI = frozenset({"platform_supplier", "affiliate_inferred", "relationship", "commentary"})


def _vendor_source(item: EvidenceItem) -> bool:
    """The vendor's own words: not DNS, independent corroboration, a platform supplier, an inferred affiliate, a
    relationship listing or commentary (as risk's t2 check reads them)."""
    return item.family not in (SourceFamily.DNS, SourceFamily.IND) and item.tags.locus not in _THIRD_PARTY_LOCI


def _first_party_naming(item: EvidenceItem) -> bool:
    """A provider the vendor names itself in an accountable source: SR A or B, and the vendor's own words."""
    return item.tags.sr in ("A", "B") and _vendor_source(item)


def _provider_clause(names: Sequence[tuple[str, list[str]]], items: Sequence[EvidenceItem], compact: bool) -> str:
    """'OpenAI and Anthropic — generative AI in delivery operations (Investor presentation; Evidence Log ...)';
    compact: 'OpenAI (ChatGPT, Azure OpenAI; Evidence Log ...)'."""
    who = _join([f"{label} ({', '.join(variants)})" if variants else label for label, variants in names])
    if compact:
        if len(names) == 1 and names[0][1]:
            label, variants = names[0]
            return f"{label} ({', '.join(variants)}; Evidence Log {_id_list(items)})"
        return f"{who} (Evidence Log {_id_list(items)})"
    first = items[0]
    if first.tags.locus == "relationship":
        role = "named as an AI partner or provider"
    else:
        where = _ROLE_LOCUS.get(first.tags.locus, "")
        role = f"{_AI_SHORT[first.tags.ai_type]} {where}" if where else _AI_SHORT[first.tags.ai_type]
    source = f"{first.source_type}; " if first.source_type else ""
    return f"{who}{SEP}{role} ({source}Evidence Log {_id_list(items)})"


def _register_sentence(view: _View) -> str:
    """Whether a public sub-processor register was found, with where it was searched."""
    leg = view.entries(SourceFamily.LEG)
    if view.has_ai_register():
        return ""
    if not leg:
        return "No sub-processor register search is recorded."
    refs = ", ".join(cid for _, cid in leg)
    if all(e.status in COMPLETE_STATUSES for e, _ in leg):
        domain = view.f.profile.domain
        where = f"the legal and trust pages of {domain}" if domain else "the vendor's legal and trust pages"
        return f"No public sub-processor register found (searched {where}; Coverage Log {refs})."
    return (f"The vendor's legal and trust pages could not be fully searched (Coverage Log {refs}), so a public "
            "sub-processor register is not ruled out.")


# --------------------------------------------------------------------------- column T


def _cell_t(view: _View) -> str:
    """Column T (design 2.8): plain sentences, no codes, in the order class; data; AI evidence; ICD 203 likelihood,
    then confidence; dependency; transparency gaps; score in words; cap, ceiling and flip.

    Each sentence has variants from richest to dropped. Over budget, a fixed sequence of steps shortens or drops
    them (volume, reliability note, gap list, evidence detail, dependency detail, data detail, the score's
    parenthesis, the long gate wording, the gap sentence, the dependency, ...); the flip condition, then the
    evidence (down to its E-IDs) and data sentences go last, and the class, likelihood, confidence and score always
    stay. A short evidence sentence outranks the flip condition. Once the text fits, room is
    given back in a fixed order: a short evidence sentence, the flip condition, the gap count, then the richer
    wording of each sentence.
    """
    budget = DEFAULT_LENGTH_BUDGETS["risk_rationale"]
    slots: dict[str, list[str]] = {
        "class": [_class_sentence(view)],
        "data": _data_variants(view),
        "evidence": _evidence_variants(view),
        "likelihood": [_likelihood_sentence(view)],
        "confidence": [_confidence_sentence(view)],
        "reliability": [_reliability_sentence(view), ""],
        "dependency": _dependency_variants(view),
        "gaps": _gap_variants(view),
        "score": _score_variants(view),
        "adjust": _adjust_variants(view),
    }
    steps = [("data", 1), ("reliability", 1), ("gaps", 1), ("evidence", 1), ("dependency", 1), ("data", 2),
             ("score", 1), ("adjust", 1), ("gaps", 2), ("evidence", 2), ("data", 3), ("dependency", 2),
             ("adjust", 2), ("evidence", 3), ("evidence", 4), ("data", 4)]
    # restored in this order, each up to the given variant: a short evidence sentence, the flip condition, the gap
    # count, the dependency, then the richer wording (the dependency and the gaps also show in columns M and R)
    upgrades = (("evidence", 2), ("adjust", 0), ("gaps", 1), ("dependency", 1), ("evidence", 0), ("data", 0),
                ("dependency", 0), ("gaps", 0), ("score", 0), ("reliability", 0))
    chosen = {slot: 0 for slot in slots}

    def render() -> str:
        return " ".join(s for s in (slots[k][min(chosen[k], len(slots[k]) - 1)] for k in slots) if s)

    for slot, variant in steps:
        if len(render()) <= budget:
            break
        chosen[slot] = max(chosen[slot], variant)
    if len(render()) <= budget:
        for slot, best in upgrades:
            current = chosen[slot]
            for variant in range(best, current):
                chosen[slot] = variant
                if len(render()) <= budget:
                    break
            else:
                chosen[slot] = current
    return _first_fit([render()], budget)


def _class_sentence(view: _View) -> str:
    """'Risk class: High.', with '(Provisional)' when the Inconclusive cap applies."""
    risk = view.risk
    return f"Risk class: {risk.final_class}" + (" (Provisional)." if risk.provisional else ".")


def _data_variants(view: _View) -> list[str]:
    """Data involved in the profile's words: with the annual volume, without it, shortened twice, dropped."""
    profile = view.f.profile
    full = _profile_data(profile.data_accessed)
    if not full:
        text = "Data involved: not stated in the vendor profile."
        return [text, text, text, text, ""]
    volume = " ".join(profile.data_volume.split()).rstrip(".")
    lead = "Data involved, per the vendor profile: "
    with_volume = (f"{lead}{full} (annual volume: {_lower_first(volume)})."
                   if volume and volume.casefold() not in ("not applicable", "n/a", "none") else f"{lead}{full}.")
    return [with_volume, f"{lead}{full}.", f"{lead}{_shorten_list(full, 90)}.", f"{lead}{_shorten_list(full, 50)}.",
            ""]


def _evidence_variants(view: _View) -> list[str]:
    """The AI evidence and what it touches, with E-IDs: full, compact, minimal, the E-IDs alone, dropped."""
    verdict = view.verdict
    if view.yes:
        groups = view.pathways()
        if not groups:
            return [""]
        first = groups[0]
        ai = f"{_ai_words(first, limit=2)} {LOCUS_WORDS[first[0].tags.locus]}"
        data = _unique(_lower_first(" ".join(d.split())) for item in first for d in item.data_mentioned)
        touches = f", applied to {_join(data[:3])}" if data else ""
        also = ""
        if len(groups) > 1:
            second = groups[1]
            also = (f" It also indicates {_ai_words(second, limit=2)} {LOCUS_WORDS[second[0].tags.locus]} "
                    f"({_id_list(second)}).")
        corroborating_keys = {k for k in verdict.corroborating if k in view.by_key and view.by_key[k].citable}
        shown = [i for i in first if i.item_key not in corroborating_keys] or first
        corroborating = [view.by_key[k] for k in sorted(corroborating_keys)
                         if view.by_key[k].item_key not in {i.item_key for i in shown}]
        corroboration = (f" {_count(len(corroborating), capital=True)} independent "
                         f"{_plural(len(corroborating), 'source corroborates', 'sources corroborate')} it "
                         f"({_id_list(corroborating)})." if corroborating else "")
        full = f"Public evidence shows {ai}{touches} ({_id_list(shown)}).{corroboration}{also}"
        compact = f"Public evidence shows {ai} ({_id_list(shown)}).{corroboration}"
        minimal = f"Public evidence shows {ai} ({_id_list(shown)})."
        return [full, compact, minimal, f"Evidence: {_id_list(shown)}.", ""]
    decisive = view.decisive()
    if verdict.conflict and len(decisive) >= 2:
        text = (f"A statement of AI use ({_eid(decisive[0])}) is contradicted by a limiting statement of the same "
                f"date or later ({_eid(decisive[1])}).")
        return [text, text, text, f"Conflicting evidence: {_id_list(decisive)}.", ""]
    if verdict.column_o == "No":
        if verdict.label == "Affirmed negative" and decisive:
            text = (f"The vendor's own limiting statement covers the service ({_id_list(decisive)}), and no "
                    "qualifying or corroborating evidence of AI use was found.")
            tiny = f"Limiting statement: {_id_list(decisive)}."
        else:
            counter = f"; counter-evidence is recorded ({_id_list(decisive)})" if decisive else ""
            text = f"The mandatory sources were searched without finding AI evidence tied to the service{counter}."
            tiny = f"Counter-evidence: {_id_list(decisive)}." if decisive else ""
        return [text, text, text, tiny, ""]
    incomplete = view.incomplete_families()
    coverage = ""
    if incomplete:
        words = [f"{_FAMILY_NAMES[fam]} {view.cfg.status_words.get(status, status)}" for fam, status in incomplete]
        coverage = f" Coverage of the mandatory sources is incomplete: {_join(words)}."
    if decisive:
        kinds = _join(_unique(_kind_words(item) for item in decisive))
        base = (f"Public evidence is limited to {kinds} ({_id_list(decisive)}), and none of it confirms AI use in the "
                "service.")
        tiny = f"Indicators: {_id_list(decisive)}."
    else:
        base = tiny = "No public evidence ties AI to the service."
    return [base + coverage, base + coverage, base, tiny, ""]


def _kind_words(item: EvidenceItem) -> str:
    strength = item.strength
    if strength == "Context - relationship only":
        return "AI-provider DNS verification tokens" if _is_dns(item) else "a provider relationship listing"
    return {
        "Context - platform supplier": "a platform supplier's AI options",
        "Context - inferred affiliate": "an inferred affiliate's AI capability",
        "Marketing only": "marketing statements",
        "Weak": "weak indicators",
        "Negative": "a limiting statement",
    }.get(strength, "a single uncorroborated source")


def _likelihood_sentence(view: _View) -> str:
    """ICD 203: 'It is {likelihood} that the vendor uses AI in {service}.'"""
    likelihood = view.verdict.likelihood
    claim = f"the vendor uses AI in {_service_phrase(view)}"
    if likelihood == "roughly even chance":
        return f"There is a roughly even chance that {claim}."
    if likelihood == "almost no chance":
        return f"There is almost no chance that {claim}."
    return f"It is {likelihood} that {claim}."


def _confidence_sentence(view: _View) -> str:
    verdict = view.verdict
    reason = " ".join(verdict.confidence_reason.split()).rstrip(".")
    reason = re.sub(r"^because\s+", "", reason, flags=re.IGNORECASE)
    level = verdict.confidence.lower()
    return f"Confidence is {level} because {reason}." if reason else f"Confidence is {level}."


def _reliability_sentence(view: _View) -> str:
    """Design 2.8: where it applies, the evidence is reliable on the fact of use while data flows are inferred."""
    inputs = view.inputs
    if view.yes and view.pathways() and (inputs.e_assumed or inputs.gaps.get("t3")):
        return "The evidence is reliable on the fact of use, while data flows are inferred."
    return ""


def _dependency_variants(view: _View) -> list[str]:
    """Dependency in the profile's words: with the business process, without it, dropped."""
    profile, tier = view.f.profile, view.f.criticality.tier.value
    dependency = " ".join(profile.operational_dependency.split()).lower()
    process = _lower_first(" ".join(profile.business_process.split()).rstrip("."))
    if not dependency:
        text = f"The criticality tier is {tier}."
        return [text, text, ""]
    compact = f"Operational dependency is {dependency}, and the criticality tier is {tier}."
    full = (f"Operational dependency on the vendor for {process} is {dependency}, and the criticality tier is {tier}."
            if process else compact)
    return [full, compact, ""]


def _gap_variants(view: _View) -> list[str]:
    gaps = view.inputs.gaps
    if not gaps:
        return [""]
    missing = view.inputs.missing_gaps
    if not missing:
        text = "All six transparency checks are publicly disclosed."
        return [text, text, ""]
    n = len(missing)
    if n == len(GAP_WORDS):
        text = "None of the six transparency checks is publicly disclosed; contractual disclosure is unknown."
        return [text, text, ""]
    head = f"{_count(n, capital=True)} of six transparency checks {_plural(n, 'is', 'are')} not publicly disclosed"
    listing = _join([GAP_WORDS[g] for g in missing])
    return [f"{head} ({listing}); contractual disclosure is unknown.",
            f"{head}; contractual disclosure is unknown.", ""]


def _score_variants(view: _View) -> list[str]:
    """'Score: exposure 2/3, decision impact 2/3, tier 3, transparency gap 1 → 12 of 18 = High (...)', then without
    the parenthesis."""
    risk, inputs = view.risk, view.inputs
    e = f"exposure {inputs.e}/3" + (" (assumed)" if inputs.e_assumed else "")
    k = f"decision impact {inputs.k}/3" + (" (assumed)" if inputs.k_assumed else "")
    score = f"Score: {e}, {k}, tier {inputs.tp}, transparency gap {inputs.tg} → {risk.arp} of 18 = {risk.base_class}"
    return [f"{score} (exposure and decision impact count double).", f"{score}."]


def _adjust_variants(view: _View) -> list[str]:
    """The materiality gate, escalator floors, the verdict cap, the ceiling and the flip condition: in full, with
    the short gate wording, and without the flip condition."""
    risk = view.risk
    gate_lowers = risk.base_class in ("High", "Critical") and not risk.gate_met
    rest: list[str] = []
    if risk.escalators_fired:
        floors = _join([_ESCALATOR_SHORT[x] for x in risk.escalators_fired])
        rest.append(f"An escalation floor of High applies: {floors}.")
    pre_cap = risk.pre_cap_class or risk.base_class
    if risk.cap == "High" and pre_cap == "Critical":
        rest.append("The Probable verdict caps the class at High.")
    if risk.cap == "None identified":
        rest.append(f"The {view.verdict.label} verdict means no AI risk class applies.")
    if risk.provisional:
        rest.append(f"Provisional; ceiling {risk.ceiling_class} if confirmed." if risk.ceiling_class
                    else "Provisional pending the vendor's confirmation.")
    elif risk.ceiling_class:
        rest.append(f"Ceiling {risk.ceiling_class} if confirmed.")
    flip = " ".join(risk.flip_condition.split())
    flip = (flip if flip[-1] in _TERMINAL else flip + ".") if flip else ""
    long_gate = ("Neither exposure nor decision impact is evidenced at 2 or more, so the materiality gate lowers the "
                 "class to Medium." if gate_lowers else "")
    short_gate = "The materiality gate lowers the class to Medium." if gate_lowers else ""

    def join(*parts: str) -> str:
        return " ".join(p for p in parts if p)

    return [join(long_gate, *rest, flip), join(short_gate, *rest, flip), join(short_gate, *rest)]


def _service_phrase(view: _View) -> str:
    process = _lower_first(" ".join(view.f.profile.business_process.split()).rstrip("."))
    return f"the service supporting {process}" if process else "the service it provides to Meridian"


def _profile_data(text: str) -> str:
    """The profile's data column as one phrase: sentences joined with '; ', each lower-cased at the start."""
    flat = " ".join(text.split())
    sentences = [s.strip().rstrip(".;") for s in re.split(r"(?<=[.;])\s+", flat) if s.strip(" .;")]
    return "; ".join(_lower_first(s) for s in sentences)


_MORE = ", among others"


def _shorten_list(phrase: str, limit: int) -> str:
    """Shorten a list phrase to ``limit`` characters by whole items ('; ' or ', ' separated): 'a, b, among others'."""
    if len(phrase) <= limit:
        return phrase
    pieces = re.split(r"(; |, )", phrase)          # item, separator, item, ...
    room = limit - len(_MORE)
    kept = pieces[0]
    if len(kept) > room:
        return kept[: max(room, 1)].rsplit(" ", 1)[0].rstrip(" ,;") + _MORE
    for n in range(1, len(pieces) - 1, 2):
        longer = kept + pieces[n] + pieces[n + 1]
        if len(longer) > room:
            break
        kept = longer
    return kept + _MORE


# --------------------------------------------------------------------------- column U


def _cell_u(findings: VendorFindings) -> str | None:
    """Column U: ``actions.text`` (or the playbook and monitoring when no text was rendered), cut to the budget by
    whole trailing sentences only."""
    actions = findings.actions
    text = " ".join(actions.text.split())
    if not text:
        text = " ".join(" ".join(s.split()) for s in [*actions.class_playbook, actions.monitoring] if s.strip())
    if not text:
        return None
    budget = DEFAULT_LENGTH_BUDGETS["recommended_action"]
    sentences = _SENTENCE_END.split(text)
    while len(sentences) > 1 and len(" ".join(sentences)) > budget:
        sentences.pop()
    return _first_fit([" ".join(sentences)], budget)


# --------------------------------------------------------------------------- sheets


def evidence_log_sheet(findings_list: Sequence[VendorFindings]) -> SheetSpec:
    """Evidence Log: one row per item considered, including rejected items and definition-test traps. Vendors in
    order, each vendor's items by E-ID; every column needed to trace a cited excerpt back to its capture."""
    rows: list[list[Cell]] = []
    for findings in findings_list:
        eids = {item.item_key: item.evidence_id for item in findings.evidence if item.evidence_id}
        rows += [_log_row(item, eids) for item in sorted(findings.evidence, key=_eid_order)]
    return SheetSpec(
        title=EVIDENCE_LOG_TITLE, headers=list(EVIDENCE_LOG_HEADERS), rows=rows,
        column_widths=list(EVIDENCE_LOG_WIDTHS),
        note="One row per evidence item considered, including rejected items and definition-test traps; the "
             "cells cite the Evidence ID. Excerpts are exact slices of the captured text (offsets and SHA-256 given).",
    )


def _log_row(item: EvidenceItem, eids: Mapping[str, str]) -> list[Cell]:
    corroborates = sorted(eids.get(key, key[:12]) for key in item.corroborates)
    return [
        item.evidence_id, item.vendor_id, item.role, evidence_status(item), item.strength, item.tag_string,
        item.tags.ai_type, "; ".join(f"{i.code}: {i.span}" for i in item.indicators), item.family.value,
        item.source_type, item.publisher, item.title, item.url, item.url_final, item.published, item.date_basis,
        item.retrieved_at, item.excerpt, f"{item.start}-{item.end}", item.excerpt_sha256, item.capture_sha256,
        item.text_sha256, item.screenshot_path, item.screenshot_sha256, _visible(item.visible_in_render),
        "; ".join(item.providers), "; ".join(item.data_mentioned), item.temporal, item.action_level, item.method,
        item.llm_model, item.prompt_sha256, _labels(item.rule_labels), _labels(item.llm_labels), item.cluster_id,
        ", ".join(corroborates), item.review_status, item.review_reason, item.reviewer, item.item_key,
    ]


def evidence_status(item: EvidenceItem) -> str:
    """Whether a row counts, in words: citable, or why not."""
    if item.tags.ai_type == "not_ai":
        return "trap: not AI under the definition test"
    if item.review_status == "rejected":
        return "rejected by analyst"
    if item.proposed:
        return "proposed by the LLM, awaiting analyst review"
    return "citable"


def coverage_log_sheet(findings_list: Sequence[VendorFindings]) -> SheetSpec:
    """Coverage Log (``sheets.COVERAGE_HEADERS`` with a Coverage ID column first), vendors in order."""
    rows: list[list[Cell]] = []
    for findings in findings_list:
        base = _coverage_rows(findings.coverage)
        rows += [[cid, *row] for cid, row in zip(coverage_ids(findings.coverage), base.rows)]
    base = _coverage_rows([])
    return SheetSpec(title=COVERAGE_LOG_TITLE, headers=["Coverage ID", *COVERAGE_HEADERS], rows=rows,
                     column_widths=[13.0, *base.column_widths], note=base.note)


def evidence_images_sheet(findings_list: Sequence[VendorFindings]) -> SheetSpec:
    """Evidence Images: the excerpt-anchored screenshot of each item that has one, by reference and SHA-256 (the
    design's no-images form until ``workbook.write_workbook`` can embed images)."""
    rows: list[list[Cell]] = []
    for findings in findings_list:
        for item in sorted(findings.evidence, key=_eid_order):
            if item.screenshot_path:
                rows.append([item.evidence_id, item.vendor_id, item.screenshot_path, item.screenshot_sha256,
                             _visible(item.visible_in_render), item.url])
    return SheetSpec(title=EVIDENCE_IMAGES_TITLE, headers=list(EVIDENCE_IMAGES_HEADERS), rows=rows,
                     column_widths=[15, 9, 60, 66, 10, 60],
                     note="Screenshots are kept in the evidence pack and listed by path and SHA-256; the Evidence "
                          "ID links each one to its Evidence Log row.")


_RUN_INFO_SKIP = frozenset({"run_id", "mode", "as_of", "input_sha256", "created_at"})
_RUN_INFO_ORDER = ("code_version", "config_sha256", "prompts_sha256", "llm", "collection_runs", "counts")
_LLM_ORDER = ("models", "switches", "calls", "withheld")


def run_info_sheet(result: AssessmentResult) -> SheetSpec:
    """Run Info: what produced this workbook, as key/value rows from the run and its manifest. The wall-clock
    ``created_at`` is left out so that the same run exports byte-identical workbooks."""
    rows: list[list[Cell]] = [
        ["Run ID", result.run_id], ["Mode", result.mode], ["As of", result.as_of],
        ["Input SHA-256", result.input_sha256], ["Vendors", ", ".join(f.vendor_id for f in result.vendors)],
    ]
    manifest = result.manifest
    keys = [k for k in _RUN_INFO_ORDER if k in manifest]
    keys += sorted(k for k in manifest if k not in _RUN_INFO_ORDER and k not in _RUN_INFO_SKIP)
    for key in keys:
        value = manifest[key]
        if key == "llm" and isinstance(value, Mapping):
            sub = [k for k in _LLM_ORDER if k in value] + sorted(k for k in value if k not in _LLM_ORDER)
            for name in sub:
                rows += [[k, v] for k, v in _flatten(f"llm.{name}", value[name])]
        else:
            rows += [[k, v] for k, v in _flatten(key, value)]
    example = result.example
    if example is not None:
        rows.append([f"Calibration {example.vendor_id}",
                     f"{example.verdict.column_o} ({example.verdict.label}); ARP {example.risk.arp} of 18; "
                     f"class {example.risk.final_class}"])
    return SheetSpec(title=RUN_INFO_TITLE, headers=list(RUN_INFO_HEADERS), rows=rows, column_widths=[45, 100],
                     note="The run that produced this workbook: input, configuration and prompt hashes, models and "
                          "counts. Replay of the same run reproduces the same cells.")


def _flatten(prefix: str, value: Any) -> Iterator[tuple[str, Cell]]:
    if isinstance(value, Mapping):
        if not value:
            yield prefix, ""
        for key in sorted(value, key=str):
            yield from _flatten(f"{prefix}.{key}", value[key])
    elif isinstance(value, (list, tuple)):
        yield prefix, "; ".join(_scalar_text(v) for v in value)
    elif isinstance(value, bool):
        yield prefix, "yes" if value else "no"
    elif value is None:
        yield prefix, ""
    elif isinstance(value, (int, float)):
        yield prefix, value
    else:
        yield prefix, str(value)


def _scalar_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


# --------------------------------------------------------------------------- Method & Legend (P3/P4 sections)

_TAG_LEGEND: tuple[tuple[str, str], ...] = (
    ("Tag string", "U · SR · SP · RL · RC · IC · locus, for example U3 · SR:B · SP:S2 · RL:R2 · RC:T3 · IC:2 · "
                   "locus=delivery_ops"),
    ("U1", "AI in the exact service"),
    ("U2", "AI in the vendor's platform or an add-on"),
    ("U3", "AI in operations that touch the service: support, network operations, software delivery"),
    ("U4", "a named AI provider or relationship, including a DNS verification token"),
    ("U5", "building AI capability, for example a posting that only asks for AI skills"),
    ("U6", "AI governance: NIST AI RMF, ISO/IEC 42001 or an AI policy"),
    ("U7", "a marketing claim"),
    ("U8", "a negative or limiting statement"),
    ("SR A", "legally accountable: SEC filings and exhibits, regulator publications, privacy notices, DPAs, "
             "sub-processor lists, contract and product terms"),
    ("SR B", "first-party accountable: vendor product pages, technical docs, release notes, trust pages, investor "
             "decks, the vendor's own job postings, named-executive statements, DNS records, verified repositories"),
    ("SR C", "promotional or second-party: unattributed blogs and whitepapers, press releases and their mirrors, "
             "provider customer stories, partner pages, trade press, third-party podcasts"),
    ("SR D", "aggregators and syndicated copies"),
    ("SR E-F", "user-generated content; content of unknown origin, including AI-generated blogs (excluded)"),
    ("SP S3", "a named model, provider, developer artifact, legal artifact or filing (G1, G5, G6, G7) together "
              "with a named feature or a data-flow statement (G2, G3)"),
    ("SP S2", "a named feature, GA release note, developer artifact, operational job duty or quantified outcome "
              "(G2, G4, G5, G8, G11) tied to a named product or function, with no relabelled automation (M4); job "
              "postings are capped at S2"),
    ("SP S1", "marketing indicators only (M1, M5, M6, M7), or a legal or filing source without a feature or "
              "data flow; generic legal or filing language"),
    ("SP S0", "aspirational, planned or commentary wording dominates (M2, M3)"),
    ("RL R3", "an exact-service term in the claim sentence, its heading or the page title, for AI in the service, "
              "an add-on or delivery operations"),
    ("RL R2", "a product-family term, a company-wide delivery practice, or a relationship proven by DNS"),
    ("RL R1", "another business line, an inferred affiliate or a platform supplier"),
    ("RL R0", "commentary"),
    ("RC T3", "12 months or less before the assessment date"),
    ("RC T2", "24 months or less"),
    ("RC T1", "36 months or less"),
    ("RC T0", "older or undated; historical context only. Articles, releases and filings are dated by "
              "publication, live product, policy and trust pages by retrieval"),
    ("IC 1", "corroborated by an independent corroborating signal"),
    ("IC 2", "consistent but uncorroborated, SR A or B"),
    ("IC 3", "S0 or S1 only"),
    ("IC 4", "uncorroborated SR C or D at S2 or above (Admiralty 'doubtful')"),
    ("IC 5", "contradicted by an A/B limiting statement at R3 dated the same day or later"),
    ("IC 6", "cannot be judged: undated or older than 36 months, or SR E or F"),
    *((f"locus={locus}", f"AI {words}") for locus, words in LOCUS_WORDS.items()),
)

_STRENGTH_LEGEND: tuple[tuple[str, str], ...] = (
    ("Negative", "a negative or limiting statement (U8)"),
    ("Strong", "a qualifying signal at exact-service relevance (R3)"),
    ("Moderate", "a qualifying signal at product-family relevance (R2), or a corroborating signal"),
    ("Context - relationship only", "a DNS verification token or a provider listing; not scored"),
    ("Context - platform supplier", "a platform supplier's AI capability; not scored"),
    ("Context - inferred affiliate", "an affiliate that is inferred but not confirmed; not scored"),
    ("Marketing only", "a marketing claim (U7) or aspirational wording (S0)"),
    ("Weak", "anything else verified (S1 or R1)"),
)

_TESTS_LEGEND: tuple[tuple[str, str], ...] = (
    ("1. Definition", "EU AI Act Art. 3(1): workload automation, batch scheduling, RPA, rules engines, templated "
                      "composition, Intelligent Mail barcodes and BI dashboards are not AI unless an ML or LLM "
                      "component is evidenced; such passages are kept as traps and never cited"),
    ("2. Subject", "the sentence names the vendor, an alias or a product, or uses the first person; a third-party "
                   "article must name the vendor in the sentence or the title"),
    ("3. Use state", "deployed cues (live, launched, generally available, uses, reviewed by AI, a percentage) "
                     "rather than planned or exploratory wording; a pilot counts as limited deployment"),
    ("4. Specificity", "genuine-use (G) against marketing (M) indicators give the specificity grade S0-S3"),
)

_INDICATOR_LEGEND: tuple[tuple[str, str], ...] = (
    ("G1", "a named model or provider"),
    ("G2", "a named feature with observable behaviour"),
    ("G3", "a statement about data flow, retention, training or human review"),
    ("G4", "a release note for a generally available feature"),
    ("G5", "a developer artifact"),
    ("G6", "a legal artifact (set from the source type only)"),
    ("G7", "a filing that ties AI to products (set from the source type only)"),
    ("G8", "a job posting with operational AI duties"),
    ("G9, G10", "not defined; dropped by verification"),
    ("G11", "a quantified outcome"),
    ("M1", "buzzword"),
    ("M2", "aspirational or capability wording: will, plan, exploring, roadmap; may, might, could and can in "
           "statements about what a product can do"),
    ("M3", "commentary about other companies or the industry"),
    ("M4", "automation relabelled as AI"),
    ("M5", "superlative"),
    ("M6", "no product named"),
    ("M7", "a partnership shown only by logo"),
)

_VERDICT_LEGEND: tuple[tuple[str, str], ...] = (
    ("Qualifying signal (Q)", "verified, SR A or B, SP S2 or higher, RL R2 or higher, RC T1 or newer, class U1-U4, "
                              "and AI in the service, an add-on, delivery operations or software development"),
    ("Corroborating signal (K)", "SR A-C, SP S2 or higher, RL R2 or higher, RC T1 or newer, class U1-U4, from an "
                                 "independent origin cluster"),
    ("Independent", "a different origin cluster (token-set similarity below 90 and different titles) and a "
                    "different publisher or source family; partner mirrors of one release form one cluster; a third-party "
                    "copy of the vendor's own words (press-release mirror) counts as the vendor and is independent "
                    "only of a third-party item of another publisher; one source counts once"),
    ("a) Conflict", "a Q contradicted for the same service by an A/B limiting statement dated the same day or "
                    "later: Inconclusive, raised as a questionnaire item"),
    ("b) Confirmed", "a Q at R3 plus an independent K: Yes"),
    ("c) Probable", "any Q, or two independent K: Yes"),
    ("d) Affirmed negative", "an A/B limiting statement covers the service and there is no Q or K: No"),
    ("e) Not detected", "every mandatory family complete and no item at S1+, R2+, T1+ in classes U1-U4 or U7: No"),
    ("f) Inconclusive", "anything else: marketing, capability or relationship only, an inferred affiliate, or "
                        "incomplete coverage"),
    ("ICD 203 likelihood", "Confirmed: very likely (almost certain with two independent SR A sources); Probable: "
                           "likely; Inconclusive: roughly even chance with an S1+ item at R2+, else unlikely; Not "
                           "detected: unlikely; Affirmed negative: very unlikely"),
    ("ICD 203 confidence", "High: two or more consistent A/B sources and complete coverage. Moderate: one A/B "
                           "source, two or more C sources, or complete coverage with nothing found. Low: a "
                           "conflict, incomplete coverage of a mandatory family, or D-grade sources only"),
)

_RISK_INPUTS: tuple[tuple[str, str], ...] = (
    ("E3", "needs an exact-service (R3) item. Service or add-on: data sensitivity 4 or privileged production "
           "access (D3-P), or a named external model processes the data. Delivery operations: the excerpt states "
           "that customer data or credentials are processed by AI"),
    ("E2", "Service or add-on: non-public personal information (D3), or an R2 item that would give E3 if confirmed "
           "(capped at E2; the E3 feeds only the ceiling). Delivery operations: AI works on artefacts that carry "
           "customer data (tickets, cases, incidents, exceptions, transactions, production logs, communications). "
           "Software development: production data is named"),
    ("E1", "any other AI in the service, an add-on, delivery operations or software development"),
    ("E0", "corporate-internal AI only"),
    ("K3", "action without per-case human review on customers, funds, regulatory outputs or production"),
    ("K2", "action with human review or approval gates, or autonomous action on operational items that do not "
           "affect customers"),
    ("K1", "advisory output only"),
    ("K0", "none"),
    ("TP", "tier points: Critical 3, High 2, Medium 1, Low 0"),
)
_RISK_RULES: tuple[tuple[str, str], ...] = (
    ("Transparency checks", "; ".join(f"{key}: {words}" for key, words in GAP_WORDS.items())
     + "; only verified items close a check, and each gap reads 'not publicly disclosed; contractual disclosure "
       "unknown'"),
    ("Unknowns rule", "for an Inconclusive verdict, or an input a Yes verdict leaves unknown: E from data "
                      "sensitivity, K from the vendor's role, marked assumed; assumed values never meet the "
                      "materiality gate"),
    ("Materiality gate", "High or above needs an evidenced (not assumed) E of 2 or more or K of 2 or more; otherwise "
                         "the class is lowered to Medium"),
    *((x, f"{words}: a floor of High, for Confirmed or Probable with the gate met; otherwise logged, not applied")
      for x, words in ESCALATOR_WORDS.items()),
    ("Verdict cap", "applied last: Probable at most High; Inconclusive at most Medium (Provisional); a No verdict "
                    "gives None identified"),
    ("Ceiling", "the class before the cap with assumed inputs treated as confirmed, written 'ceiling ... if "
                "confirmed'"),
    ("Themes", "RT1 data exposure; RT2 undisclosed sub-processors and concentration; RT3 decision transparency; RT4 "
               "excessive agency; RT5 injection and leakage; RT6 output integrity; RT7 predictive-model risk "
               "(NIST AI 600-1, OWASP LLM Top 10)"),
)


LegendTable = Mapping[str, str] | Iterable[Sequence[str]]
"""code -> text, or rows (code, topic, text) as footprint.actions.questionnaire_table() returns them."""


def method_legend_p3(spec: SheetSpec, *, questions: LegendTable | None = None, clauses: LegendTable | None = None,
                     gap_blocks: LegendTable | None = None, seeds: Mapping[str, Mapping[str, Any]] | None = None,
                     query_templates: Sequence[tuple[str, str]] | None = None) -> SheetSpec:
    """Append the P3/P4 sections to a Method & Legend sheet and return the same spec (via ``sheets.add_section``).

    Always: tag legend, strength labels and the column P prefixes, the four genuine-vs-marketing tests, the G and M
    indicators, verdict rules a-f with the ICD 203 wording, and the risk matrix (bands and TG points are computed
    from ``models.arp_class`` and ``models.tg_for_missing``). Questionnaire items Q1-Q15, contract clauses C1-C11 and
    the gap blocks come from config/actions.toml through ``footprint.actions`` unless given (pass ``{}`` to leave a
    section out). When given: the service-term dictionaries with their reasons (``seeds`` maps vendor id -> seed
    dict; only service_term and family_term are read) and the discovery query templates.
    """
    if questions is None or clauses is None or gap_blocks is None:
        from footprint import actions  # lazy: the action policy is only needed for this sheet

        questions = actions.questionnaire_table() if questions is None else questions
        clauses = actions.clause_table() if clauses is None else clauses
        gap_blocks = actions.gap_block_table() if gap_blocks is None else gap_blocks
    add_section(spec, "Evidence tags", _TAG_LEGEND)
    add_section(spec, "Strength labels (in order)", _STRENGTH_LEGEND)
    prefixes = [
        ("Yes", f"{PRIMARY}{SEP}type, publisher, date, URL (retrieved date; Evidence Log E-ID): \"verbatim\"; "
                f"then up to two items as {SUPPORTING}"),
        *((label, prefix + (f"{DNS_SUFFIX} for a DNS token" if label == "Context - relationship only" else ""))
          for label, prefix in P_PREFIXES.items()),
        ("Qualifying item in a conflict", CONFLICTING),
        ("Moderate item under Inconclusive", UNCORROBORATED),
        ("No verdict", f"{LIMITING} or {COUNTER}, then the negative findings"),
        ("Quotes", "exact excerpts, never shortened; runs of spaces and line breaks show as one space, and the "
                   "Evidence Log keeps the exact slice with its offsets and SHA-256"),
        ("Every verdict", f"Negative finding{SEP}what was not found, where (Coverage Log C-ID); then: {CLOSING_SHOTS} (or, "
                          f"when no cited item has a screenshot: {CLOSING})"),
    ]
    add_section(spec, "Column P prefixes", prefixes)
    add_section(spec, "Genuine use vs marketing (tests in order)", _TESTS_LEGEND)
    add_section(spec, "Indicators", _INDICATOR_LEGEND)
    add_section(spec, "AI usage verdict (column O)", _VERDICT_LEGEND)
    add_section(spec, "AI risk matrix (columns S and T)", [
        ("Score", "ARP = 2 x exposure (E) + 2 x decision impact (K) + tier points (TP) + transparency gap (TG), "
                  "0-18; adjustments in a fixed order: score, base class, materiality gate, escalator floors, "
                  "verdict cap"),
        ("Class bands", _bands()),
        *_RISK_INPUTS,
        ("TG", _tg_points()),
        *_RISK_RULES,
    ])
    for title, table, ordered in (("Questionnaire items", questions, True), ("Contract clauses", clauses, True),
                                  ("Gap blocks (column U)", gap_blocks, False)):
        rows = _table_rows(table)
        if rows:
            add_section(spec, title, sorted(rows, key=lambda kv: _code_number(kv[0])) if ordered else rows)
    if seeds:
        add_section(spec, "Service-term dictionaries", list(_service_terms(seeds)))
    if query_templates:
        add_section(spec, "Query templates", list(query_templates))
    return spec


def _bands() -> str:
    """'Critical 14-18; High 10-13; Medium 6-9; Low 0-5', computed from models.arp_class."""
    spans: dict[str, list[int]] = {}
    for arp in range(19):
        spans.setdefault(arp_class(arp), []).append(arp)
    return "; ".join(f"{cls} {min(v)}-{max(v)}" for cls, v in sorted(spans.items(), key=lambda kv: -max(kv[1])))


def _tg_points() -> str:
    """'0-1 missing checks: 0; 2-3: 1; 4-5: 2; 6: 3', computed from models.tg_for_missing."""
    spans: dict[int, list[int]] = {}
    for missing in range(7):
        spans.setdefault(tg_for_missing(missing), []).append(missing)
    parts = [f"{min(v)}-{max(v)}" if min(v) != max(v) else f"{min(v)}" for v in spans.values()]
    return "missing checks " + "; ".join(f"{span} give {tg}" for span, tg in zip(parts, spans))


def _service_terms(seeds: Mapping[str, Mapping[str, Any]]) -> Iterator[tuple[str, str]]:
    for vendor_id in sorted(seeds):
        seed = seeds[vendor_id]
        for kind, grade in (("service_term", "exact service (R3)"), ("family_term", "product family (R2)")):
            for entry in seed.get(kind, []) or []:
                term = str(entry.get("term", "")).strip()
                if term:
                    reason = str(entry.get("reason", "")).strip()
                    yield f"{vendor_id}: {term}", f"{grade}" + (f"; {reason}" if reason else "")


def _table_rows(table: LegendTable) -> list[tuple[str, str]]:
    """(code, detail) rows from a code -> text mapping or (code, topic, text) rows ('topic: text')."""
    if isinstance(table, Mapping):
        return [(str(code), str(text)) for code, text in table.items()]
    rows: list[tuple[str, str]] = []
    for row in table:
        code, *rest = [str(cell) for cell in row]
        rows.append((code, ": ".join(part for part in rest if part)))
    return rows


def _code_number(code: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", code)
    return (int(digits) if digits else 0, code)


# --------------------------------------------------------------------------- helpers


def display_url(url: str) -> str:
    """A URL as the cells show it: without the scheme, and without a lone trailing slash after the host."""
    text = _SCHEME.sub("", url.strip())
    return text[:-1] if text.endswith("/") and text.count("/") == 1 else text


def _dmy(value: str, *, strict: bool = False) -> str:
    """ISO date or timestamp -> DD-MM-YYYY. A value that is not a date is returned unchanged (strict: ValueError)."""
    try:
        return dt.date.fromisoformat(value.strip()[:10]).strftime("%d-%m-%Y")
    except ValueError:
        if strict:
            raise ValueError(f"expected an ISO date YYYY-MM-DD, not {value!r}") from None
        return value


def _eid(item: EvidenceItem) -> str:
    if not item.evidence_id:
        raise ValueError(f"item {item.item_key[:12]} has no evidence_id; run compose.assign_evidence_ids first")
    return item.evidence_id


def _eid_order(item: EvidenceItem) -> tuple[bool, str, str]:
    return (not item.evidence_id, item.evidence_id, item.item_key)


def _id_list(items: Iterable[EvidenceItem], limit: int = _MAX_IDS) -> str:
    ids = sorted({_eid(item) for item in items})
    if len(ids) <= limit:
        return ", ".join(ids)
    return f"{', '.join(ids[:limit])} and {len(ids) - limit} more"


def _group(items: Sequence[EvidenceItem], key: Callable[[EvidenceItem], Any]) -> list[list[EvidenceItem]]:
    """Group items by key, in order of first appearance."""
    groups: dict[Any, list[EvidenceItem]] = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return list(groups.values())


def _is_dns(item: EvidenceItem) -> bool:
    return item.family == SourceFamily.DNS or item.source_type.casefold().startswith("dns")


def _unique(values: Iterable[str]) -> list[str]:
    """Distinct non-empty values (case-insensitive, whitespace collapsed), first spelling kept, in order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


def _join(items: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b, and c' (serial comma, as in column N)."""
    items = list(items)
    if len(items) <= 2:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _count(n: int, *, capital: bool = False) -> str:
    word = _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)
    return _capital(word) if capital else word


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _lower_first(text: str) -> str:
    """Lower-case a leading capitalised word ('Payment instructions'), never an acronym ('ACH', 'RTP')."""
    if not text:
        return text
    word = text.split(" ", 1)[0]
    return text[0].lower() + text[1:] if len(word) > 1 and word[0].isupper() and word[1:].islower() else text


def _labels(labels: Mapping[str, str]) -> str:
    return "; ".join(f"{key}={labels[key]}" for key in sorted(labels))


def _visible(value: bool | None) -> str:
    return "Not checked" if value is None else "Yes" if value else "No"


def _first_fit(candidates: Iterable[str], budget: int) -> str:
    """The first candidate within the budget; otherwise the last one, cut at a word boundary (never a quote)."""
    last = ""
    for text in candidates:
        if len(text) <= budget:
            return text
        last = text
    cut = last[: max(budget - 3, 0)].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "..."
