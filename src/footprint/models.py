"""Shared data contracts for footprint.

Every stage exchanges these pydantic models. Keep them free of behaviour beyond trivial helpers, so that
modules stay independently testable. Later phases add evidence, verdict and risk models below.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
