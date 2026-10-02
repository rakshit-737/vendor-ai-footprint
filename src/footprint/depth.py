"""Depth planner (C3): the criticality tier, adjusted by three modifiers, sets how deep the footprint review goes.

The policy lives in config/depth.toml (design §Depth policy; Appendix A 2.5). A DepthPlan is built from a
CriticalityResult and the vendor's profile only, never from OSINT, so depth follows from the assigned tier rather
than from how much a vendor publishes. Column N (Assessment Depth Applied) is rendered from the plan plus the
Coverage Log. Modifier triggers are coded here; what each modifier changes is configured.
"""

from __future__ import annotations

import tomllib
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from footprint.models import (
    COMPLETE_STATUSES,
    CoverageEntry,
    CoverageStatus,
    CriticalityResult,
    DepthPlan,
    FamilyPlan,
    SourceFamily,
    Tier,
    VendorProfile,
)

DEFAULT_CONFIG_PATH: Path = Path(__file__).resolve().parents[2] / "config" / "depth.toml"

MODIFIER_ORDER: tuple[str, ...] = ("M-D4", "M-D3P", "M-Private")
D3P_ANCHOR = "D3-P"        # rubric anchor: privileged production access or service credentials
NOT_USED = "not_used"
NOT_APPLICABLE = "not_applicable"
MISSING = "missing"        # status_words key for a mandatory family with no Coverage Log entry


# --------------------------------------------------------------------------- config


class FamilyPolicy(BaseModel):
    """One source family at one tier, as configured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mandatory: bool
    mode: str = Field(min_length=1)
    cap: int = Field(ge=0, description="max automated requests for the family (0 = none / manual only)")
    reason: str = Field(min_length=1)


class TierPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, description="column N label")
    phrase: str = Field(min_length=1, description="column N: 'A {tier} tier calls for {phrase}: ...'")
    discretionary_fetches: int = Field(ge=0)
    gemini_calls: int = Field(ge=0)
    analyst_minutes: int = Field(ge=0)
    saturation_window: int = Field(ge=1)
    reserved_for_meridian: list[str] = Field(min_length=1)
    families: dict[SourceFamily, FamilyPolicy]


class ModifierPolicy(BaseModel):
    """What a modifier changes. Its trigger is coded in _triggered_modifiers()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, description="names the modifier in a raised family's reason")
    note: str = Field(min_length=1, description="describes the modifier in column N")
    raise_to_critical: list[SourceFamily] = Field(default_factory=list)
    not_applicable: list[SourceFamily] = Field(default_factory=list)
    not_applicable_reason: str = ""


class ExcludedSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DepthConfig(BaseModel):
    """config/depth.toml after validation: every tier defines every family, and column N has words for each."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    max_cell_chars: int = Field(gt=0)
    closing: str = Field(min_length=1)
    registrant_unknown_suffix: str = Field(min_length=1)
    family_order: list[SourceFamily] = Field(description="order in which column N names the families")
    family_words: dict[SourceFamily, dict[str, str]] = Field(description="family -> mode -> words in column N")
    status_words: dict[str, str] = Field(description="coverage status (or 'missing') -> words, in column N order")
    tiers: dict[Tier, TierPolicy]
    modifiers: dict[str, ModifierPolicy]
    excluded_sources: list[ExcludedSource]

    @model_validator(mode="after")
    def _complete(self) -> DepthConfig:
        problems = _config_problems(self)
        if problems:
            raise ValueError("depth policy is incomplete: " + "; ".join(problems))
        return self


def _config_problems(cfg: DepthConfig) -> list[str]:
    problems: list[str] = []
    if set(cfg.tiers) != set(Tier):
        problems.append(f"tiers must be {', '.join(t.value for t in Tier)}")
    if sorted(cfg.family_order) != sorted(SourceFamily):
        problems.append("family_order must name every source family once")
    statuses = {s.value for s in CoverageStatus} | {MISSING}
    if set(cfg.status_words) != statuses:
        problems.append(f"status_words must cover {', '.join(sorted(statuses))}")
    if set(cfg.modifiers) != set(MODIFIER_ORDER):
        problems.append(f"modifiers must be {', '.join(MODIFIER_ORDER)}")
    for tier, policy in cfg.tiers.items():
        if missing := [f.value for f in SourceFamily if f not in policy.families]:
            problems.append(f"tier {tier.value} lacks families {', '.join(missing)}")
        for fam, fp in policy.families.items():
            if fp.mode == NOT_USED and (fp.mandatory or fp.cap):
                problems.append(f"{tier.value} {fam.value} is not_used, so it cannot be mandatory or capped")
            if fp.mandatory and fp.mode not in cfg.family_words.get(fam, {}):
                problems.append(f"family_words.{fam.value} has no words for mode '{fp.mode}'")
    critical = cfg.tiers.get(Tier.CRITICAL)
    for mod_id, mod in cfg.modifiers.items():
        for fam in mod.raise_to_critical:
            if critical is None or fam not in critical.families or not critical.families[fam].mandatory:
                problems.append(f"{mod_id} raises {fam.value}, which is not mandatory at Critical")
        if mod.not_applicable and not mod.not_applicable_reason:
            problems.append(f"{mod_id} needs a not_applicable_reason")
    return list(dict.fromkeys(problems))  # de-duplicated, in order


def load_depth_config(path: Path | str | None = None) -> DepthConfig:
    """Read and validate a depth policy (default: config/depth.toml)."""
    with Path(path or DEFAULT_CONFIG_PATH).open("rb") as fh:
        return DepthConfig.model_validate(tomllib.load(fh))


# --------------------------------------------------------------------------- planning


def plan_depth(
    result: CriticalityResult,
    profile: VendorProfile,
    sec_registrant: bool | None = None,
    config: DepthConfig | None = None,
) -> DepthPlan:
    """The depth plan for one vendor, from its final tier and D factor (never from OSINT).

    profile must be the row the result was computed from. sec_registrant is True or False once EDGAR has been
    checked; None means unknown at planning time, so REG stays mandatory and the SEC collector logs it
    not_applicable if the vendor does not file.
    """
    if result.vendor_id != profile.vendor_id:
        raise ValueError(f"criticality result {result.vendor_id} does not match profile {profile.vendor_id}")
    cfg = config or load_depth_config()
    policy = cfg.tiers[result.tier]
    families = _base_families(policy)
    modifiers = _triggered_modifiers(result, families, sec_registrant)
    _apply_modifiers(families, modifiers, cfg)
    if sec_registrant is None:
        _mark_registrant_unknown(families, cfg)
    return DepthPlan(
        vendor_id=result.vendor_id,
        tier=result.tier,
        label=policy.label,
        families=list(families.values()),
        modifiers=modifiers,
        discretionary_fetches=policy.discretionary_fetches,
        gemini_calls=policy.gemini_calls,
        analyst_minutes=policy.analyst_minutes,
        saturation_window=policy.saturation_window,
        reserved_for_meridian=list(policy.reserved_for_meridian),
    )


def _base_families(policy: TierPolicy) -> dict[SourceFamily, FamilyPlan]:
    """The tier's families, in SourceFamily order."""
    return {fam: FamilyPlan(family=fam, **policy.families[fam].model_dump()) for fam in SourceFamily}


def _triggered_modifiers(
    result: CriticalityResult, base: dict[SourceFamily, FamilyPlan], sec_registrant: bool | None
) -> list[str]:
    """Ids of the modifiers that apply, in MODIFIER_ORDER."""
    d = result.factors.get("D")
    if d is None:
        raise ValueError(f"criticality result {result.vendor_id} has no D factor")
    fired = {
        "M-D4": d.level == 4 and result.tier != Tier.CRITICAL,  # top data sensitivity below the Critical tier
        "M-D3P": d.anchor == D3P_ANCHOR,  # privileged production access: delivery-AI module
        "M-Private": sec_registrant is False and base[SourceFamily.REG].mandatory,  # no filings to draw on
    }
    return [m for m in MODIFIER_ORDER if fired[m]]


def _apply_modifiers(families: dict[SourceFamily, FamilyPlan], modifiers: list[str], cfg: DepthConfig) -> None:
    """Raise families to their Critical mode and cap, and mark families not applicable, as the modifiers say."""
    critical = cfg.tiers[Tier.CRITICAL].families
    raised_by: dict[SourceFamily, list[str]] = {}
    for mod_id in modifiers:
        for fam in cfg.modifiers[mod_id].raise_to_critical:
            raised_by.setdefault(fam, []).append(mod_id)
    for fam, ids in raised_by.items():
        base, top = families[fam], critical[fam]
        raised = base.model_copy(update={"mandatory": True, "mode": top.mode, "cap": max(base.cap, top.cap)})
        if raised != base:  # a family already at Critical depth keeps its tier's reason
            names = " and ".join(f"{i} ({cfg.modifiers[i].title})" for i in ids)
            raised = raised.model_copy(update={"reason": f"Raised to Critical depth by {names}."})
        families[fam] = raised
    for mod_id in modifiers:
        mod = cfg.modifiers[mod_id]
        for fam in mod.not_applicable:
            families[fam] = FamilyPlan(
                family=fam, mandatory=False, mode=NOT_APPLICABLE, cap=0, reason=mod.not_applicable_reason
            )


def _mark_registrant_unknown(families: dict[SourceFamily, FamilyPlan], cfg: DepthConfig) -> None:
    """REG runs only for SEC registrants; while that is unknown, its reason says so."""
    reg = families[SourceFamily.REG]
    if reg.mandatory:
        reason = f"{reg.reason} {cfg.registrant_unknown_suffix}"
        families[SourceFamily.REG] = reg.model_copy(update={"reason": reason})


# --------------------------------------------------------------------------- coverage


def is_complete(status: CoverageStatus | str) -> bool:
    """Complete coverage = done, done_manual, not_applicable or stopped(rule) (models.COMPLETE_STATUSES)."""
    return CoverageStatus(status) in COMPLETE_STATUSES


def coverage_complete(plan: DepthPlan, coverage: Iterable[CoverageEntry]) -> bool:
    """True when every mandatory family of the plan has a Coverage Log entry with a complete status.

    Entries for other vendors are ignored, so the whole run's Coverage Log can be passed.
    """
    entries = _entries_for(plan, coverage)
    return all(_family_status(entries, f.family) in COMPLETE_STATUSES for f in plan.families if f.mandatory)


def _entries_for(plan: DepthPlan, coverage: Iterable[CoverageEntry]) -> list[CoverageEntry]:
    return [e for e in coverage if e.vendor_id == plan.vendor_id]


def _family_status(entries: list[CoverageEntry], family: SourceFamily) -> CoverageStatus | None:
    """The family's last complete status if it has one (e.g. a manual capture after a block), else its last
    status, else None (never logged)."""
    statuses = [e.status for e in entries if e.family == family]
    complete = [s for s in statuses if s in COMPLETE_STATUSES]
    if complete:
        return complete[-1]
    return statuses[-1] if statuses else None


# --------------------------------------------------------------------------- column N


def render_depth_cell(
    plan: DepthPlan,
    coverage: Iterable[CoverageEntry] | None = None,
    config: DepthConfig | None = None,
) -> str:
    """Column N (Assessment Depth Applied) in the style of the V-000 example, within max_cell_chars.

    The coverage sentence appears only when coverage is given; if the cell would run over budget, its per-status
    breakdown is dropped first. Raises ValueError if the cell still does not fit (the policy wording is too long).
    """
    cfg = config or load_depth_config()
    head = f"{plan.label}. {_scope_sentence(plan, cfg)}"
    tail = [_reserved_sentence(plan), cfg.closing]
    if coverage is None:
        candidates = [[head, *tail]]
    else:
        entries = _entries_for(plan, coverage)
        candidates = [
            [head, _coverage_sentence(plan, entries, cfg, detailed), *tail] for detailed in (True, False)
        ]
    texts = [" ".join(part for part in parts if part) for parts in candidates]
    for text in texts:
        if len(text) <= cfg.max_cell_chars:
            return text
    raise ValueError(
        f"column N for {plan.vendor_id} needs {len(texts[-1])} characters, over the budget of "
        f"{cfg.max_cell_chars}; shorten the wording in depth.toml"
    )


def _scope_sentence(plan: DepthPlan, cfg: DepthConfig) -> str:
    """'A {tier} tier calls for {phrase}: {mandatory families}{; modifier notes}.'"""
    words = []
    for fam in cfg.family_order:
        fp = plan.family(fam)
        if fp is not None and fp.mandatory:
            words.append(_family_words(fp, cfg))
    listing = _join_and(words) or "no mandatory source families"
    notes = "".join(f"; {cfg.modifiers[m].note}" for m in plan.modifiers)
    return f"A {plan.tier.value} tier calls for {cfg.tiers[plan.tier].phrase}: {listing}{notes}."


def _family_words(fp: FamilyPlan, cfg: DepthConfig) -> str:
    try:
        return cfg.family_words[fp.family][fp.mode]
    except KeyError:
        raise ValueError(f"depth.toml has no words for {fp.family.value} in mode '{fp.mode}'") from None


def _coverage_sentence(plan: DepthPlan, entries: list[CoverageEntry], cfg: DepthConfig, detailed: bool) -> str:
    """'Coverage: x of y mandatory source families completed (breakdown); n automated requests within caps.'"""
    statuses = [_family_status(entries, f.family) for f in plan.families if f.mandatory]
    completed = sum(s in COMPLETE_STATUSES for s in statuses)
    text = f"Coverage: {completed} of {len(statuses)} mandatory source families completed"
    if detailed and statuses:
        counts = Counter(MISSING if s is None else s.value for s in statuses)
        breakdown = [f"{counts[key]} {words}" for key, words in cfg.status_words.items() if counts[key]]
        text += f" ({', '.join(breakdown)})"
    requests = sum(e.requests_used for e in entries)
    text += f"; {requests} automated {_plural(requests, 'request', 'requests')}"
    over = _families_over_cap(plan, entries)
    if over:
        return text + f", exceeding the cap for {over} source {_plural(over, 'family', 'families')}."
    return text + " within caps."


def _families_over_cap(plan: DepthPlan, entries: list[CoverageEntry]) -> int:
    used: Counter[SourceFamily] = Counter()
    for e in entries:
        used[e.family] += e.requests_used
    return sum(1 for f in plan.families if f.cap and used[f.family] > f.cap)


def _reserved_sentence(plan: DepthPlan) -> str:
    if not plan.reserved_for_meridian:
        return ""
    return f"Reserved for Meridian (non-OSINT): {', '.join(plan.reserved_for_meridian)}."


def _join_and(items: list[str]) -> str:
    """'a', 'a and b', 'a, b, and c' (serial comma, as in the V-000 example)."""
    if len(items) <= 2:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


# --------------------------------------------------------------------------- Method & Legend sheet


def depth_table(config: DepthConfig | None = None) -> list[dict[str, str | int | bool]]:
    """Tier x family grid (Critical first, families in model order) before any vendor modifier, with REG
    described as for a vendor whose SEC registration is not yet known."""
    cfg = config or load_depth_config()
    rows: list[dict[str, str | int | bool]] = []
    for tier in sorted(cfg.tiers, key=lambda t: t.rank, reverse=True):
        policy = cfg.tiers[tier]
        families = _base_families(policy)
        _mark_registrant_unknown(families, cfg)
        for fp in families.values():
            rows.append({
                "tier": tier.value,
                "label": policy.label,
                "family": fp.family.value,
                "mandatory": fp.mandatory,
                "mode": fp.mode,
                "cap": fp.cap,
                "reason": fp.reason,
            })
    return rows


def excluded_sources(config: DepthConfig | None = None) -> list[tuple[str, str]]:
    """(source, reason) pairs for the sources excluded by design, in policy order."""
    cfg = config or load_depth_config()
    return [(x.source, x.reason) for x in cfg.excluded_sources]
