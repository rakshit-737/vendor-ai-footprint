"""Shared data contracts for footprint.

Every stage exchanges these pydantic models. Keep them free of behaviour beyond trivial helpers, so that
modules stay independently testable. Later phases add evidence, verdict and risk models below.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from enum import Enum
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- tiers


class Tier(str, Enum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"

    @property
    def rank(self) -> int:
        """Low=0 .. Critical=3, for max()/comparisons."""
        return TIER_ORDER.index(self)

    @property
    def points(self) -> int:
        """Tier points (TP) used by the AI risk score: Critical 3, High 2, Medium 1, Low 0."""
        return self.rank


TIER_ORDER: list[Tier] = [Tier.LOW, Tier.MEDIUM, Tier.HIGH, Tier.CRITICAL]


def max_tier(*tiers: Tier) -> Tier:
    return max(tiers, key=lambda t: t.rank)


# --------------------------------------------------------------------------- workbook input


class VendorProfile(BaseModel):
    """One vendor row as provided by Optiv (blue-grey columns B-K). Read-only input."""

    model_config = ConfigDict(frozen=True)

    row: int = Field(description="1-based worksheet row number")
    vendor_id: str
    name: str
    description: str = ""
    service: str = ""
    category: str = ""
    website: str = ""
    business_process: str = ""
    operational_dependency: str = ""
    data_accessed: str = ""
    data_volume: str = ""
    is_example: bool = Field(default=False, description="True for the fictional worked example row (V-000)")

    @property
    def domain(self) -> str:
        """Bare host of the public website, e.g. 'fiserv.com' (lower-case, no scheme, no www.)."""
        w = self.website.strip().lower()
        for prefix in ("https://", "http://"):
            if w.startswith(prefix):
                w = w[len(prefix):]
        w = w.split("/")[0]
        return w[4:] if w.startswith("www.") else w


class ValidationIssue(BaseModel):
    code: str = Field(description="E01..E09 (see workbook module docstring)")
    message: str
    severity: Literal["error", "warning"] = "error"
    cell: str = ""


class WorkbookData(BaseModel):
    """Result of reading an uploaded vendor workbook."""

    sheet_name: str
    header_row: int
    column_map: dict[str, str] = Field(description="field name -> column letter, e.g. {'vendor_id': 'B'}")
    vendors: list[VendorProfile] = Field(description="real vendors in sheet order, example row excluded")
    example: VendorProfile | None = Field(default=None, description="worked example row (V-000), if present")
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)


# Student-owned output columns L..V, keyed by field name.
STUDENT_FIELDS: list[str] = [
    "criticality_tier",        # L
    "criticality_rationale",   # M
    "assessment_depth",        # N
    "ai_usage_detected",       # O
    "evidence",                # P
    "how_ai_used",             # Q
    "ai_subprocessors",        # R
    "ai_risk_class",           # S
    "risk_rationale",          # T
    "recommended_action",      # U
    "assessed_by",             # V
]


class StudentCells(BaseModel):
    """Values to write into one vendor's student cells. None = leave the cell untouched."""

    criticality_tier: str | None = None
    criticality_rationale: str | None = None
    assessment_depth: str | None = None
    ai_usage_detected: str | None = None
    evidence: str | None = None
    how_ai_used: str | None = None
    ai_subprocessors: str | None = None
    ai_risk_class: str | None = None
    risk_rationale: str | None = None
    recommended_action: str | None = None
    assessed_by: str | None = None


class SheetSpec(BaseModel):
    """An extra sheet to append to the output workbook (Evidence Log, Coverage Log, ...)."""

    title: str
    headers: list[str]
    rows: list[list[str | int | float | None]] = Field(default_factory=list)
    column_widths: list[float] = Field(default_factory=list, description="optional widths, same order as headers")
    note: str = Field(default="", description="optional one-line note written above the header row")


# --------------------------------------------------------------------------- criticality

FactorCode = Literal["O", "D", "P", "R", "V"]


class FactorScore(BaseModel):
    factor: FactorCode
    level: int = Field(ge=0, le=4)
    anchor: str = Field(description="anchor id from config/rubric.toml, e.g. 'D3-P'")
    trigger: str = Field(description="verbatim phrase from the profile that triggered the anchor ('' if default)")
    source_fields: list[str] = Field(description="profile fields the factor was read from")
    weight: int
    points: int
    note: str = Field(default="", description="e.g. 'two adjacent levels fit; higher level taken'")


class TierOverride(BaseModel):
    """HC1: an analyst changes the computed tier. Always needs a reason."""

    vendor_id: str
    tier: Tier
    reason: str = Field(min_length=10)
    analyst: str
    date: str = Field(description="ISO date YYYY-MM-DD")


class CriticalityResult(BaseModel):
    vendor_id: str
    rubric_version: str
    factors: dict[str, FactorScore] = Field(description="keys O, D, P, R, V")
    score: int = Field(ge=0, le=100)
    score_tier: Tier
    floors_fired: list[str] = Field(description="floor ids, e.g. ['F1', 'F2']")
    computed_tier: Tier = Field(description="max(score tier, floors) before any analyst override")
    tier: Tier = Field(description="final tier (computed_tier, or the override's tier)")
    sensitivity: list[str] = Field(description="plain-language one-step sensitivity statements")
    perturbation_stable: bool = Field(description="tier unchanged under every single +/-1 weight change")
    override: TierOverride | None = None


# --------------------------------------------------------------------------- depth


class SourceFamily(str, Enum):
    LEG = "LEG"    # legal: privacy, terms, DPA, sub-processors, AI policy, trust/security
    REG = "REG"    # regulatory filings (SEC)
    PRD = "PRD"    # product pages, docs, release notes, whitepapers, newsroom/IR
    JOB = "JOB"    # vendor's own applicant tracking system
    DNS = "DNS"    # DNS TXT/SPF/CNAME fingerprints
    IND = "IND"    # independent corroboration: provider stories, partner releases, trade press
    HIST = "HIST"  # Wayback history / archived copies
    EXEC = "EXEC"  # executive channels (manual capture only)


class CoverageStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    DONE_MANUAL = "done_manual"
    NOT_APPLICABLE = "not_applicable"
    STOPPED = "stopped"            # stopped by a rule: cap, saturation, 3 consecutive R1 items
    BLOCKED_ROBOTS = "blocked_robots"
    BLOCKED_TOU = "blocked_tou"
    BLOCKED_BOT = "blocked_bot"
    ERROR = "error"
    DESCOPED = "descoped"


COMPLETE_STATUSES: frozenset[CoverageStatus] = frozenset(
    {CoverageStatus.DONE, CoverageStatus.DONE_MANUAL, CoverageStatus.NOT_APPLICABLE, CoverageStatus.STOPPED}
)


class FamilyPlan(BaseModel):
    family: SourceFamily
    mandatory: bool
    mode: str = Field(description="e.g. 'full', 'keyword', 'homepage', 'one_query', 'on_lead', 'manual'")
    cap: int = Field(description="max automated requests for this family (0 = none / manual only)")
    reason: str = Field(description="why this family is (or is not) mandatory at this tier")


class DepthPlan(BaseModel):
    vendor_id: str
    tier: Tier
    label: str = Field(description="column N label, e.g. 'Full review - tier-driven'")
    families: list[FamilyPlan]
    modifiers: list[str] = Field(default_factory=list, description="e.g. ['M-D4', 'M-Private']")
    discretionary_fetches: int
    gemini_calls: int
    analyst_minutes: int
    saturation_window: int = Field(description="stop after this many actions without a new qualifying cluster")
    reserved_for_meridian: list[str] = Field(description="non-OSINT steps Meridian performs, never the team")

    def family(self, fam: SourceFamily) -> FamilyPlan | None:
        return next((f for f in self.families if f.family == fam), None)


class CoverageEntry(BaseModel):
    """One row of the Coverage Log: what was searched, how, and with what result (negative evidence)."""

    vendor_id: str
    family: SourceFamily
    mandatory: bool
    status: CoverageStatus
    collector: str = ""
    endpoint: str = Field(default="", description="endpoint or query used")
    requests_used: int = 0
    cap: int = 0
    documents: int = 0
    ai_passages: int = 0
    note: str = ""


# --------------------------------------------------------------------------- P2 capture / extraction

RobotsDecision = Literal["allowed", "disallowed", "not_applicable", "manual"]
DocKind = Literal["html", "pdf", "json", "dns", "text"]

CAPTURE_HEADER_KEYS: tuple[str, ...] = ("date", "last-modified", "content-type", "etag", "server")


class Capture(BaseModel):
    """One retrieved (or manually imported) raw artefact in the content-addressed evidence store.

    ``capture_id`` is the sha256 hex of the raw bytes; the blob lives at ``blob_path``
    (repo-relative, e.g. ``evidence/blobs/ab/<sha>.gz``).
    """

    capture_id: str = Field(description="sha256 hex of the raw bytes")
    vendor_id: str
    family: SourceFamily
    collector: str = Field(description="collector name that requested it, or 'manual'")
    url_requested: str
    url_final: str = Field(default="", description="URL after redirects ('' if same / not applicable)")
    status: int = 0
    content_type: str = ""
    headers: dict[str, str] = Field(default_factory=dict, description="subset: date,last-modified,content-type,etag,server")
    retrieved_at: str = Field(description="ISO-8601 UTC timestamp, e.g. 2026-10-02T12:00:00Z")
    size: int = 0
    blob_path: str = Field(default="", description="repo-relative path to gzip blob")
    robots_decision: RobotsDecision = "not_applicable"
    robots_sha256: str = Field(default="", description="sha256 of the robots.txt body consulted ('' if none)")
    tou_match: str = Field(default="", description="host entry in config/tou.toml that governed the request")
    via_wayback: bool = False
    wayback_timestamp: str = Field(default="", description="14-digit Wayback timestamp if via_wayback")
    manual: bool = False
    captured_by: str = Field(default="", description="collector name or 'human:<initials>'")
    screenshot_path: str = ""
    note: str = ""


class Document(BaseModel):
    """Extracted text of a capture. ``doc_id`` equals the sha256 of the extracted text (``text_sha256``)."""

    doc_id: str = Field(description="sha256 hex of extracted text")
    capture_id: str
    vendor_id: str
    family: SourceFamily
    url: str
    title: str = ""
    kind: DocKind
    published: str = Field(default="", description="ISO date YYYY-MM-DD or ''")
    date_basis: str = Field(default="", description="where the date came from, e.g. 'meta article:published_time', 'last-modified'")
    text_path: str = Field(default="", description="repo-relative path to stored text")
    text_len: int = 0
    extractor: str = Field(default="", description="name+version, e.g. 'trafilatura 1.12.2'")
    pages: list[int] = Field(default_factory=list, description="pdf: char offsets where each page starts")


class Passage(BaseModel):
    """A lexicon-hit window of a document; ``text == document_text[start:end]`` exactly."""

    passage_id: str = Field(description="first 16 hex of sha256(f'{doc_id}|{start}|{end}')")
    doc_id: str
    vendor_id: str
    start: int
    end: int
    text: str
    hits: list[str] = Field(default_factory=list, description="lexicon terms found")

    @staticmethod
    def make_id(doc_id: str, start: int, end: int) -> str:
        import hashlib

        return hashlib.sha256(f"{doc_id}|{start}|{end}".encode()).hexdigest()[:16]


class CollectorResult(BaseModel):
    """What one collector returns for one vendor."""

    captures: list[Capture] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    leads: list[str] = Field(default_factory=list, description="URLs discovered but not fetched")
    notes: list[str] = Field(default_factory=list)


FetchReason = Literal[
    "", "ok", "blocked_robots", "blocked_tou", "blocked_bot", "http_error", "cap_reached", "replay_miss", "network_error"
]


class FetchOutcome(BaseModel):
    """Result of Fetcher.get(). ``capture`` is set whenever bytes were stored (also for http_error bodies)."""

    ok: bool
    capture: Capture | None = None
    status: int = 0
    reason: str = Field(default="", description="'' when ok, else e.g. blocked_robots, blocked_tou, blocked_bot, http_error, cap_reached, replay_miss, network_error")


# =========================================================================== P3/P4: signals, verdict, risk
#
# Interfaces and behaviour: docs/contracts_p3.md. Design: Appendix A 2.3 (tags, strength labels), 2.6 (claim schema,
# V1-V9), 2.7 (verdict rules), 2.8 (risk matrix, cell templates, actions), 2.11 (workbook I/O). Additive only.
# Internal models forbid unknown fields, so a misspelt keyword fails loudly. The Gemini output schema (Indicator,
# Claim, ClaimBatch) stays lenient: model output is parsed first and trusted only after footprint.verify passes it.

TAG_SEPARATOR = " · "

IndicatorCode = Literal[
    "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11",
    "M1", "M2", "M3", "M4", "M5", "M6", "M7",
]
INDICATOR_CODES: tuple[str, ...] = get_args(IndicatorCode)
GENUINE_INDICATORS: tuple[str, ...] = tuple(c for c in INDICATOR_CODES if c.startswith("G"))
MARKETING_INDICATORS: tuple[str, ...] = tuple(c for c in INDICATOR_CODES if c.startswith("M"))
UNDEFINED_INDICATORS: frozenset[str] = frozenset({"G9", "G10"})
"""Inside the schema's G1..G11 range but not defined in Appendix 2.3: verify (V4) drops them."""
SOURCE_TYPE_INDICATORS: frozenset[str] = frozenset({"G6", "G7"})
"""Legal artifact / filing: set by the rules from the source type only; verify (V4) drops LLM-proposed ones."""


class Indicator(BaseModel):
    """One genuine-use (G1..G11) or marketing (M1..M7) indicator hit, with the words that show it (design 2.3).

    ``span`` is copied character for character from the claim quote or the evidence excerpt (checked by V4).
    """

    model_config = ConfigDict(json_schema_extra={"description": "An indicator found in the quote."})

    code: IndicatorCode = Field(description="G1..G11 genuine-use or M1..M7 marketing indicator code")
    span: str = Field(description="the words of the quote that show the indicator, copied exactly")


UClass = Literal["U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8"]
SourceReliability = Literal["A", "B", "C", "D", "E", "F"]
SpecificityGrade = Literal["S0", "S1", "S2", "S3"]
RelevanceGrade = Literal["R0", "R1", "R2", "R3"]
RecencyGrade = Literal["T0", "T1", "T2", "T3"]
Locus = Literal[
    "service_feature", "vendor_addon", "delivery_ops", "sdlc", "corporate_internal", "platform_supplier",
    "affiliate_inferred", "relationship", "commentary", "unknown",
]
AiType = Literal[
    "predictive_ml", "genai_llm", "agentic", "document_ai", "conversational", "aiops", "unspecified", "not_ai",
]
Strength = Literal[
    "Negative", "Strong", "Moderate", "Context - relationship only", "Context - platform supplier",
    "Context - inferred affiliate", "Marketing only", "Weak",
]
STRENGTH_ORDER: tuple[str, ...] = get_args(Strength)
"""The fixed label order of design 2.3 (first matching rule wins); also the first key when ranking items."""
CONTEXT_STRENGTHS: frozenset[str] = frozenset(s for s in STRENGTH_ORDER if s.startswith("Context - "))
"""'Context - not scored' labels: shown in Q, T and the Evidence Log, never counted as Q or K."""
QUALIFYING_LOCI: frozenset[str] = frozenset({"service_feature", "vendor_addon", "delivery_ops", "sdlc"})
"""Loci a qualifying signal (Q) may have (design 2.7)."""


class SignalTags(BaseModel):
    """The tag card of one evidence item (design 2.3).

    u_class U1 (AI in the exact service) .. U8 (negative or limiting statement); sr A (legally accountable) ..
    F (unknown origin), from config/sources.toml only; sp S0 (M2/M3 dominate) .. S3, from the indicators;
    rl R0 (commentary) .. R3 (exact service), computed locally; rc T0 (older than 36 months or undated) ..
    T3 (12 months or less before as_of); ic 1 (independently corroborated) .. 6 (cannot be judged).
    ``strength`` is footprint.rules.strength_label of the other tags.
    """

    model_config = ConfigDict(extra="forbid")

    u_class: UClass
    sr: SourceReliability
    sp: SpecificityGrade
    rl: RelevanceGrade
    rc: RecencyGrade
    ic: int = Field(ge=1, le=6, description="corroboration 1..6; see docs/contracts_p3.md for each value")
    locus: Locus
    ai_type: AiType
    strength: Strength

    def tag_string(self) -> str:
        """E.g. 'U3 · SR:B · SP:S2 · RL:R2 · RC:T3 · IC:2 · locus=delivery_ops' (Evidence Log, Method & Legend)."""
        return TAG_SEPARATOR.join([
            self.u_class, f"SR:{self.sr}", f"SP:{self.sp}", f"RL:{self.rl}", f"RC:{self.rc}", f"IC:{self.ic}",
            f"locus={self.locus}",
        ])

    @property
    def u_number(self) -> int:
        """1..8 for U1..U8."""
        return int(self.u_class[1:])

    @property
    def sp_level(self) -> int:
        """0..3 for S0..S3."""
        return int(self.sp[1:])

    @property
    def rl_level(self) -> int:
        """0..3 for R0..R3."""
        return int(self.rl[1:])

    @property
    def rc_level(self) -> int:
        """0..3 for T0..T3."""
        return int(self.rc[1:])


ClaimKind = Literal[
    "uses_ai", "offers_ai_feature", "ai_partnership", "names_ai_provider", "ai_hiring", "ai_governance",
    "generic_ai_marketing", "negative_or_limiting", "industry_commentary", "not_ai",
]
ClaimSubject = Literal[
    "vendor_product", "vendor_operations", "vendor_staff_tools", "third_party_product", "other_or_industry",
]
ClaimAiType = Literal["predictive_ml", "genai_llm", "agentic", "document_ai", "conversational", "aiops", "unspecified"]
Temporal = Literal["in_production", "pilot_or_beta", "planned", "unclear"]
ActionLevel = Literal["none", "advisory", "human_reviewed_decision", "automated_action", "unknown"]


class Claim(BaseModel):
    """One claim Gemini extracted from a public passage: the extract_v1 response item (design 2.6), unverified.

    Every field is required, so structured output always carries it; unknown keys are ignored. Nothing here is
    trusted until footprint.verify passes it (V1-V6), and the final labels always come from the rules (V7, V8).
    The JSON-schema description (sent to the model) is the short text in json_schema_extra, not this docstring.
    """

    model_config = ConfigDict(json_schema_extra={"description": "One statement about AI found in an input item."})

    passage_id: str = Field(description="id of the input item the quote was copied from, exactly as given")
    quote: str = Field(description="verbatim, 1-3 sentences: one continuous span copied character for character "
                                   "from that item, no ellipses")
    claim_kind: ClaimKind
    subject: ClaimSubject
    ai_type: ClaimAiType
    temporal: Temporal
    action_level: ActionLevel
    named_providers: list[str] = Field(description="AI providers, models or products named in the quote (names only)")
    data_mentioned: list[str] = Field(description="kinds of data the quote says the AI uses, in the quote's words")
    indicators: list[Indicator] = Field(description="G/M indicator hits; every span copied from the quote")


class ClaimBatch(BaseModel):
    """Top-level extract_v1 response: the claims found in one batch of passages (empty when none is relevant)."""

    model_config = ConfigDict(json_schema_extra={"description": "Claims found in the input items; empty if none."})

    claims: list[Claim]


VerifyCode = Literal["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9"]
BLOCKING_VERIFY_CODES: frozenset[str] = frozenset({"V1", "V2", "V3", "V6", "V9"})
"""Rejects the claim: unknown passage, quote not in the source, ellipsis or length, entity guard, duplicate."""
REPAIR_VERIFY_CODES: frozenset[str] = frozenset({"V4", "V5"})
"""Drops the offending indicator spans or provider names; the verified quote itself survives."""


class VerifyResult(BaseModel):
    """Outcome of footprint.verify.verify_claim for one Claim (gates V1-V9, design 2.6).

    ``failures`` lists every gate that failed, unique and in gate order; ``ok`` is True exactly when none of them
    is blocking. ``start``/``end`` are offsets of the exact source slice in the document text and ``excerpt`` is
    that slice (never the model's quote). ``indicators`` and ``providers`` are the claim's indicators and provider
    names that survived V4 and V5.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    failures: list[VerifyCode] = Field(default_factory=list)
    start: int | None = None
    end: int | None = None
    excerpt: str = ""
    indicators: list[Indicator] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list, description="one plain line per failure, e.g. 'V5: dropped X'")

    @field_validator("failures")
    @classmethod
    def _gate_order(cls, value: list[str]) -> list[str]:
        return sorted(set(value), key=lambda code: int(code[1:]))

    @model_validator(mode="after")
    def _consistent(self) -> VerifyResult:
        if self.ok == any(f in BLOCKING_VERIFY_CODES for f in self.failures):
            raise ValueError("ok must be True exactly when no blocking gate (V1, V2, V3, V6, V9) failed")
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be given together")
        if self.start is None or self.end is None:
            if self.excerpt:
                raise ValueError("an excerpt needs its offsets")
            if self.ok:
                raise ValueError("a passing claim needs its located source excerpt (V2)")
        elif self.start < 0 or self.end - self.start != len(self.excerpt):
            raise ValueError("excerpt must be the source slice [start:end]")
        return self


EvidenceMethod = Literal["rule", "rule+llm_agree", "llm_proposed_accepted", "adjudicated"]
EvidenceRole = Literal["Primary", "Supporting", "Indicator", "Counter-evidence", "Negative", "Context", "Logged"]
ReviewStatus = Literal["unreviewed", "accepted", "rejected"]
ROLE_ORDER: tuple[str, ...] = get_args(EvidenceRole)
CITED_ROLES: frozenset[str] = frozenset(ROLE_ORDER) - {"Logged"}
V8_LABEL_KEYS: tuple[str, ...] = ("temporal", "action_level", "sp")
"""Labels the rules decide and Gemini only proposes (V8); a disagreement goes to the HC2 review queue."""

_EVIDENCE_ID = re.compile(r"(?P<vendor>.+)-E-\d{4}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def sha256_text(text: str) -> str:
    """sha256 hex of UTF-8 text (excerpt hashes, item keys)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _derived(name: str, given: str, expected: str) -> str:
    """The derived value of an identity field; a value that was given must already equal it."""
    if given and given != expected:
        raise ValueError(f"{name} does not match the value derived from the item's own fields")
    return expected


class EvidenceItem(BaseModel):
    """One verified evidence excerpt and its tags: one row of the Evidence Log (design 2.3, 2.6, Traceability).

    ``item_key`` = sha256(f"{url_final}|{excerpt_sha256}") is the stable id that verdicts, risk inputs, reviews and
    screenshots use. ``evidence_id`` ('{vendor_id}-E-0001') is assigned once the run's item set is final
    (footprint.compose.assign_evidence_ids) and is what the cells cite. The identity fields excerpt_sha256,
    url_final, item_key, capture_sha256 and text_sha256 are derived when omitted and checked when given.
    ``model_copy(update=...)`` skips validation: use it only for tags, labels, role, review, cluster and ids,
    never for the excerpt, its offsets or the URLs.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(default="", description="'' until assign_evidence_ids, then '{vendor_id}-E-0001'")
    item_key: str = Field(default="", description="sha256(url_final|excerpt_sha256); derived when omitted")
    vendor_id: str
    passage_id: str = Field(default="", description="source passage ('' for document-level items such as DNS tokens)")
    doc_id: str = Field(description="Document.doc_id (sha256 of the extracted text)")
    capture_id: str = Field(description="Capture.capture_id (sha256 of the raw bytes)")
    family: SourceFamily
    source_type: str = Field(description="source type as column P names it, e.g. 'Product page', 'SEC Form 10-K'")
    publisher: str = Field(description="who published the source, e.g. the vendor or a trade-press title")
    title: str = ""
    url: str = Field(description="cited URL (Document.url; the original URL for a Wayback copy)")
    url_final: str = Field(default="", description="URL actually retrieved (Capture.url_final or url_requested; "
                                                   "the replay URL for a Wayback copy); defaults to url")
    published: str = Field(default="", description="ISO date YYYY-MM-DD or ''")
    date_basis: str = Field(default="", description="where the date came from, e.g. 'json-ld datePublished', 'retrieval'")
    retrieved_at: str = Field(description="Capture.retrieved_at, ISO-8601 UTC")
    excerpt: str = Field(description="exact source slice document_text[start:end], never paraphrased")
    start: int = Field(ge=0, description="offset of the excerpt in the document text")
    end: int = Field(ge=0)
    excerpt_sha256: str = ""
    capture_sha256: str = Field(default="", description="sha256 of the raw bytes (= capture_id)")
    text_sha256: str = Field(default="", description="sha256 of the extracted text (= doc_id)")
    screenshot_path: str = ""
    screenshot_sha256: str = ""
    visible_in_render: bool | None = Field(default=None, description="excerpt seen in the rendered page; None = not checked")
    tags: SignalTags
    indicators: list[Indicator] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list, description="verified AI provider or model names (V5)")
    data_mentioned: list[str] = Field(default_factory=list)
    temporal: Temporal = "unclear"
    action_level: ActionLevel = "unknown"
    method: EvidenceMethod = "rule"
    llm_model: str = ""
    prompt_sha256: str = ""
    rule_labels: dict[str, str] = Field(default_factory=dict, description="labels the rules assigned (V7, V8)")
    llm_labels: dict[str, str] = Field(default_factory=dict, description="labels Gemini proposed, shown beside them")
    cluster_id: str = Field(default="", description="origin cluster (footprint.cluster)")
    corroborates: list[str] = Field(default_factory=list, description="item keys of the items this one corroborates")
    role: EvidenceRole = "Logged"
    review_status: ReviewStatus = "unreviewed"
    review_reason: str = ""
    reviewer: str = Field(default="", description="analyst and date of the HC2 decision, e.g. 'RK 2026-10-08'")

    @staticmethod
    def make_key(url_final: str, excerpt_sha256: str) -> str:
        """item_key for an excerpt (sha256 hex of 'url_final|excerpt_sha256')."""
        return sha256_text(f"{url_final}|{excerpt_sha256}")

    @model_validator(mode="after")
    def _identity(self) -> EvidenceItem:
        if not self.excerpt:
            raise ValueError("excerpt must not be empty")
        if self.end - self.start != len(self.excerpt):
            raise ValueError("excerpt must be the source slice [start:end] (end - start == len(excerpt))")
        self.excerpt_sha256 = _derived("excerpt_sha256", self.excerpt_sha256, sha256_text(self.excerpt))
        if not self.url_final:
            self.url_final = self.url
        self.item_key = _derived("item_key", self.item_key, self.make_key(self.url_final, self.excerpt_sha256))
        self.capture_sha256 = _derived("capture_sha256", self.capture_sha256, self.capture_id)
        self.text_sha256 = _derived("text_sha256", self.text_sha256, self.doc_id)
        if self.evidence_id:
            match = _EVIDENCE_ID.fullmatch(self.evidence_id)
            if match is None or match["vendor"] != self.vendor_id:
                raise ValueError(f"evidence_id must look like '{self.vendor_id}-E-0001'")
        return self

    @property
    def strength(self) -> str:
        return self.tags.strength

    @property
    def tag_string(self) -> str:
        """The tag card as one string (SignalTags.tag_string)."""
        return self.tags.tag_string()

    @property
    def proposed(self) -> bool:
        """An LLM-only claim (no rule item covers it) that an analyst has not accepted yet."""
        return self.method == "llm_proposed_accepted" and self.review_status != "accepted"

    @property
    def citable(self) -> bool:
        """May count toward verdict, risk and cells: not rejected, not a definition-test trap, not a pending proposal."""
        return self.review_status != "rejected" and self.tags.ai_type != "not_ai" and not self.proposed

    @property
    def label_disagreements(self) -> list[str]:
        """V8 labels on which the rules and Gemini disagree (the rule value stands until an analyst adjudicates)."""
        return [k for k in V8_LABEL_KEYS
                if k in self.rule_labels and k in self.llm_labels and self.rule_labels[k] != self.llm_labels[k]]


VerdictRule = Literal["a", "b", "c", "d", "e", "f"]
VerdictLabel = Literal["Confirmed", "Probable", "Affirmed negative", "Not detected", "Inconclusive"]
ColumnO = Literal["Yes", "No", "Inconclusive"]
Likelihood = Literal[
    "almost no chance", "very unlikely", "unlikely", "roughly even chance", "likely", "very likely", "almost certain",
]
ConfidenceLevel = Literal["High", "Moderate", "Low"]
ICD203_LIKELIHOOD: tuple[str, ...] = get_args(Likelihood)
RULE_LABEL: dict[str, str] = {
    "a": "Inconclusive", "b": "Confirmed", "c": "Probable", "d": "Affirmed negative", "e": "Not detected",
    "f": "Inconclusive",
}
LABEL_COLUMN_O: dict[str, str] = {
    "Confirmed": "Yes", "Probable": "Yes", "Affirmed negative": "No", "Not detected": "No",
    "Inconclusive": "Inconclusive",
}


class UsageVerdict(BaseModel):
    """Column O for one vendor (design 2.7): the first rule a-f that matched, its label and its ICD 203 wording.

    ``label``, ``column_o`` and ``conflict`` follow from ``rule`` (derived when omitted, checked when given).
    The item lists hold item keys, never E-IDs: ``qualifying`` = Q items, ``corroborating`` = independent K items,
    ``decisive`` = the items column P cites, best first.
    """

    model_config = ConfigDict(extra="forbid")

    rule: VerdictRule
    label: VerdictLabel
    column_o: ColumnO
    conflict: bool
    likelihood: Likelihood
    confidence: ConfidenceLevel
    confidence_reason: str
    qualifying: list[str] = Field(default_factory=list)
    corroborating: list[str] = Field(default_factory=list)
    decisive: list[str] = Field(default_factory=list)
    coverage_complete: bool
    trace: list[str] = Field(default_factory=list, description="plain-language why trace, one line per rule tested")

    @model_validator(mode="before")
    @classmethod
    def _fill_from_rule(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("rule") in RULE_LABEL:
            data = dict(data)
            data.setdefault("label", RULE_LABEL[data["rule"]])
            if data["label"] in LABEL_COLUMN_O:
                data.setdefault("column_o", LABEL_COLUMN_O[data["label"]])
            data.setdefault("conflict", data["rule"] == "a")
        return data

    @model_validator(mode="after")
    def _consistent(self) -> UsageVerdict:
        if self.label != RULE_LABEL[self.rule]:
            raise ValueError(f"rule {self.rule}) gives the label {RULE_LABEL[self.rule]!r}")
        if self.column_o != LABEL_COLUMN_O[self.label]:
            raise ValueError(f"{self.label} is written as {LABEL_COLUMN_O[self.label]!r} in column O")
        if self.conflict != (self.rule == "a"):
            raise ValueError("conflict is True exactly for rule a)")
        return self


GapKey = Literal["t1", "t2", "t3", "t4", "t5", "t6"]
GAP_KEYS: tuple[str, ...] = get_args(GapKey)
RiskClass = Literal["Critical", "High", "Medium", "Low"]
FinalRiskClass = Literal["Critical", "High", "Medium", "Low", "None identified"]
RISK_CLASS_ORDER: tuple[str, ...] = ("Low", "Medium", "High", "Critical")
VerdictCap = Literal["", "High", "Medium", "None identified"]
CeilingClass = Literal["", "Critical", "High", "Medium", "Low"]
EscalatorCode = Literal["X1", "X2", "X3", "X4", "X5", "X6"]
RiskTheme = Literal["RT1", "RT2", "RT3", "RT4", "RT5", "RT6", "RT7"]
_TG_POINTS: tuple[int, ...] = (0, 0, 1, 1, 2, 2, 3)


def tg_for_missing(missing: int) -> int:
    """TG for the number of missing transparency checks: 0-1 -> 0, 2-3 -> 1, 4-5 -> 2, all 6 -> 3."""
    if not 0 <= missing < len(_TG_POINTS):
        raise ValueError(f"missing checks must be 0..{len(GAP_KEYS)}, not {missing}")
    return _TG_POINTS[missing]


def arp_class(arp: int) -> str:
    """Base class for an ARP score: Critical 14-18, High 10-13, Medium 6-9, Low 0-5."""
    if not 0 <= arp <= 18:
        raise ValueError(f"ARP must be 0..18, not {arp}")
    return "Critical" if arp >= 14 else "High" if arp >= 10 else "Medium" if arp >= 6 else "Low"


class RiskInputs(BaseModel):
    """Inputs of ARP = 2E + 2K + TP + TG (design 2.8). Each input cites item keys or is marked assumed.

    ``gaps`` maps every transparency check t1..t6 to True when it is missing (all six keys, or empty when not
    assessed); ``tg`` follows from it (derived when omitted, checked when given). Assumed inputs (the unknowns rule)
    never satisfy the materiality gate. ``e_if_confirmed`` is the E an R2 item would give if confirmed for the exact
    service; it feeds only the ceiling and the flip condition.
    """

    model_config = ConfigDict(extra="forbid")

    e: int = Field(ge=0, le=3, description="exposure of Meridian data to AI")
    k: int = Field(ge=0, le=3, description="decision impact")
    tp: int = Field(ge=0, le=3, description="tier points: Critical 3, High 2, Medium 1, Low 0")
    tg: int = Field(ge=0, le=3, description="transparency gap points from the six checks")
    e_assumed: bool = False
    k_assumed: bool = False
    e_reason: str = ""
    k_reason: str = ""
    gaps: dict[GapKey, bool] = Field(default_factory=dict, description="check -> True when missing")
    gap_reasons: dict[GapKey, str] = Field(default_factory=dict)
    e_items: list[str] = Field(default_factory=list, description="item keys E rests on")
    k_items: list[str] = Field(default_factory=list, description="item keys K rests on")
    gap_items: dict[GapKey, list[str]] = Field(default_factory=dict, description="item keys that closed each check")
    e_if_confirmed: int | None = Field(default=None, ge=0, le=3)

    @model_validator(mode="before")
    @classmethod
    def _fill_tg(cls, data: Any) -> Any:
        if isinstance(data, dict) and "tg" not in data:
            gaps = data.get("gaps")
            if isinstance(gaps, dict) and set(gaps) == set(GAP_KEYS):
                data = {**data, "tg": tg_for_missing(sum(1 for missing in gaps.values() if missing))}
        return data

    @model_validator(mode="after")
    def _consistent(self) -> RiskInputs:
        if self.gaps:
            if set(self.gaps) != set(GAP_KEYS):
                raise ValueError("gaps must cover all six checks t1..t6, or be empty when not assessed")
            if self.tg != tg_for_missing(len(self.missing_gaps)):
                raise ValueError(f"{len(self.missing_gaps)} missing checks give TG {tg_for_missing(len(self.missing_gaps))}")
        return self

    @property
    def missing_gaps(self) -> list[str]:
        """The missing checks in t1..t6 order."""
        return [g for g in GAP_KEYS if self.gaps.get(g)]


class RiskResult(BaseModel):
    """The ordered AI-risk steps for one vendor (design 2.8): score, base class, materiality gate, escalator
    floors, then the verdict cap, always last; plus the ceiling 'if confirmed' and the flip condition.

    ``cap`` is the verdict cap in force: '' (Confirmed), 'High' (Probable), 'Medium' (Inconclusive, so
    ``provisional``) or 'None identified' (a No verdict). ``pre_cap_class`` is the class after steps 1-4.
    ``ceiling_class`` is the class after steps 1-4 with assumed inputs treated as confirmed ('' when it does not
    differ from the final class). ``steps`` is the ordered, plain-language log behind the UI's why trace.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: RiskInputs
    arp: int = Field(ge=0, le=18)
    base_class: RiskClass
    gate_met: bool
    escalators_fired: list[EscalatorCode] = Field(default_factory=list)
    escalators_logged_not_applied: list[EscalatorCode] = Field(default_factory=list)
    cap: VerdictCap = ""
    pre_cap_class: RiskClass | None = None
    final_class: FinalRiskClass
    provisional: bool = False
    ceiling_class: CeilingClass = ""
    flip_condition: str = ""
    themes: list[RiskTheme] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> RiskResult:
        i = self.inputs
        if self.arp != 2 * i.e + 2 * i.k + i.tp + i.tg:
            raise ValueError(f"ARP must be 2E + 2K + TP + TG = {2 * i.e + 2 * i.k + i.tp + i.tg}")
        if self.base_class != arp_class(self.arp):
            raise ValueError(f"an ARP of {self.arp} gives the base class {arp_class(self.arp)}")
        if self.provisional != (self.cap == "Medium"):
            raise ValueError("provisional exactly when the Inconclusive cap (Medium) is in force")
        if (self.cap == "None identified") != (self.final_class == "None identified"):
            raise ValueError("'None identified' is the final class exactly when a No verdict caps it")
        if self.cap in ("High", "Medium") and _class_rank(self.final_class) > _class_rank(self.cap):
            raise ValueError(f"the final class may not exceed the {self.cap} cap")
        if self.pre_cap_class is not None and self.cap != "None identified":
            expected = self.pre_cap_class
            if self.cap and _class_rank(expected) > _class_rank(self.cap):
                expected = self.cap
            if self.final_class != expected:
                raise ValueError(f"the cap turns {self.pre_cap_class} into {expected}, not {self.final_class}")
        if set(self.escalators_fired) & set(self.escalators_logged_not_applied):
            raise ValueError("an escalator is either applied or logged as not applied")
        return self


def _class_rank(cls: str) -> int:
    return RISK_CLASS_ORDER.index(cls)


GapBlock = Literal["SUB", "TRAIN", "LOC", "EXPL", "AGENT", "PI", "GOV", "INC", "CONC", "MRM"]
GAP_BLOCK_CODES: tuple[str, ...] = get_args(GapBlock)
QuestionCode = Literal[
    "Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10", "Q11", "Q12", "Q13", "Q14", "Q15",
]
ClauseCode = Literal["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11"]


class ActionPlan(BaseModel):
    """Column U for one vendor (design 2.8): the playbook for the risk class plus gap blocks, mapped to
    questionnaire items Q1..Q15 and contract clauses C1..C11 (config/actions.toml). Every action is addressed to
    Meridian; the team never contacts a vendor.
    """

    model_config = ConfigDict(extra="forbid")

    class_playbook: list[str] = Field(default_factory=list, description="playbook sentences for the class")
    gap_blocks: list[GapBlock] = Field(default_factory=list)
    questionnaire_items: list[QuestionCode] = Field(default_factory=list)
    contract_clauses: list[ClauseCode] = Field(default_factory=list)
    monitoring: str = ""
    text: str = Field(default="", description="rendered column U, within its length budget")


class VendorFindings(BaseModel):
    """Everything the assessment found for one vendor: L-N inputs, coverage, evidence, verdict, risk, actions and
    the composed cells. Every item key the verdict, the risk inputs or an item cites must be in ``evidence``.

    ``cells`` starts empty (all None) because footprint.compose.build_cells reads the findings; the pipeline then
    stores its result with ``model_copy(update={"cells": ...})``.
    """

    model_config = ConfigDict(extra="forbid")

    profile: VendorProfile
    criticality: CriticalityResult
    depth: DepthPlan
    coverage: list[CoverageEntry] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    verdict: UsageVerdict
    risk: RiskResult
    actions: ActionPlan
    cells: StudentCells = Field(default_factory=StudentCells)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_vendor(self) -> VendorFindings:
        vid = self.profile.vendor_id
        if self.criticality.vendor_id != vid or self.depth.vendor_id != vid:
            raise ValueError(f"criticality and depth must belong to {vid}")
        if any(e.vendor_id != vid for e in self.coverage) or any(i.vendor_id != vid for i in self.evidence):
            raise ValueError(f"coverage and evidence must belong to {vid}")
        keys = [i.item_key for i in self.evidence]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate item_key: duplicates are removed (V9) before findings are built")
        ids = [i.evidence_id for i in self.evidence if i.evidence_id]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate evidence_id")
        ri = self.risk.inputs
        cited = [*self.verdict.qualifying, *self.verdict.corroborating, *self.verdict.decisive, *ri.e_items,
                 *ri.k_items, *(k for ks in ri.gap_items.values() for k in ks),
                 *(k for i in self.evidence for k in i.corroborates)]
        dangling = sorted(set(cited) - set(keys))
        if dangling:
            raise ValueError(f"references to items not in evidence: {', '.join(k[:12] for k in dangling[:5])}")
        return self

    @property
    def vendor_id(self) -> str:
        return self.profile.vendor_id

    def item(self, ref: str) -> EvidenceItem | None:
        """The evidence item with this item_key or evidence_id, else None."""
        return next((i for i in self.evidence if ref and ref in (i.item_key, i.evidence_id)), None)

    def cited(self) -> list[EvidenceItem]:
        """Items with a cited role (anything but 'Logged'), in evidence order."""
        return [i for i in self.evidence if i.role in CITED_ROLES]


AssessmentMode = Literal["replay", "live_rules", "live_ai"]


class AssessmentResult(BaseModel):
    """What footprint.pipeline.run_assessment returns: one VendorFindings per real vendor (sheet order), the V-000
    calibration example (never written to the inventory) and the run manifest. JSON-serialisable end to end.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    mode: AssessmentMode
    as_of: str = Field(description="ISO date YYYY-MM-DD all recency and dating decisions use")
    input_sha256: str = Field(description="sha256 hex of the input workbook bytes")
    vendors: list[VendorFindings] = Field(default_factory=list)
    example: VendorFindings | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> AssessmentResult:
        try:
            dt.date.fromisoformat(self.as_of)
        except ValueError:
            raise ValueError("as_of must be an ISO date YYYY-MM-DD") from None
        if not _ISO_DAY.fullmatch(self.as_of):
            raise ValueError("as_of must be an ISO date YYYY-MM-DD")
        if not _HEX64.fullmatch(self.input_sha256):
            raise ValueError("input_sha256 must be 64 lower-case hex characters")
        ids = [f.vendor_id for f in self.vendors]
        if len(set(ids)) != len(ids):
            raise ValueError("each vendor appears once")
        if self.example is not None and self.example.vendor_id in ids:
            raise ValueError("the calibration example is not one of the vendors")
        return self

    def vendor(self, vendor_id: str) -> VendorFindings | None:
        return next((f for f in self.vendors if f.vendor_id == vendor_id), None)

    def cells(self) -> dict[str, StudentCells]:
        """vendor_id -> cells, the mapping footprint.workbook.write_workbook takes."""
        return {f.vendor_id: f.cells for f in self.vendors}
