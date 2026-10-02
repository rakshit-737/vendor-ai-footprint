"""Depth planner tests (docs/design.md "Depth policy"; Appendix A 2.5 "Depth policy and stopping rules").

CriticalityResult and VendorProfile objects are built directly: the planner reads only the final tier and the
D factor, and must never depend on how the criticality engine computed them.
"""

from __future__ import annotations

import tomllib
from typing import Any

import pytest
from pydantic import ValidationError

from footprint.depth import (
    DEFAULT_CONFIG_PATH,
    DepthConfig,
    coverage_complete,
    depth_table,
    excluded_sources,
    is_complete,
    load_depth_config,
    plan_depth,
    render_depth_cell,
)
from footprint.models import (
    COMPLETE_STATUSES,
    CoverageEntry,
    CoverageStatus,
    CriticalityResult,
    DepthPlan,
    FactorScore,
    SourceFamily,
    Tier,
    TierOverride,
    VendorProfile,
)

LEG, REG, PRD, JOB = SourceFamily.LEG, SourceFamily.REG, SourceFamily.PRD, SourceFamily.JOB
DNS, IND, HIST, EXEC = SourceFamily.DNS, SourceFamily.IND, SourceFamily.HIST, SourceFamily.EXEC

CLOSING = "Depth followed from the assigned tier rather than from the volume of material the vendor had published."
REGISTRANT_SUFFIX = "(applies only if an SEC registrant; checked by the SEC collector)"
NOT_REGISTRANT = "not an SEC registrant (EDGAR checked)"
D3P_NOTE = (
    "delivery-AI module: embedded third-party tenants and affiliates, platform-maker AI terms, "
    "staff AI-tool signals; escalate early"
)
CRITICAL_FAMILIES_IN_WORDS = (
    "regulatory filings, legal and trust pages (privacy, terms, sub-processors, AI policy), product documentation "
    "and newsroom, the vendor's own job postings, DNS records, independent provider and press corroboration, "
    "and archive history"
)

WEIGHTS = {"O": 7, "D": 6, "P": 5, "R": 4, "V": 3}

# tier -> (label, mandatory families, caps, modes, (fetches, Gemini calls, analyst minutes, saturation window))
EXPECTED: dict[Tier, tuple[str, set[SourceFamily], dict[SourceFamily, int], dict[SourceFamily, str], tuple]] = {
    Tier.CRITICAL: (
        "Full review – tier-driven",
        {LEG, REG, PRD, JOB, DNS, IND, HIST},
        {LEG: 40, REG: 80, PRD: 150, JOB: 120, DNS: 40, IND: 30, HIST: 30, EXEC: 0},
        {LEG: "full", REG: "full", PRD: "full", JOB: "full", DNS: "full", IND: "full", HIST: "full",
         EXEC: "manual"},
        (100, 40, 180, 10),
    ),
    Tier.HIGH: (
        "Standard review – tier-driven",
        {LEG, REG, PRD, JOB, DNS, IND},
        {LEG: 40, REG: 40, PRD: 100, JOB: 60, DNS: 40, IND: 20, HIST: 0, EXEC: 0},
        {LEG: "full", REG: "full", PRD: "full", JOB: "full", DNS: "full", IND: "full", HIST: "on_lead",
         EXEC: "on_lead"},
        (50, 20, 90, 6),
    ),
    Tier.MEDIUM: (
        "Focused review",
        {LEG, REG, PRD, JOB, DNS},
        {LEG: 15, REG: 5, PRD: 20, JOB: 15, DNS: 20, IND: 0, HIST: 0, EXEC: 0},
        {LEG: "privacy_and_subprocessors", REG: "one_query", PRD: "keyword", JOB: "keyword", DNS: "full",
         IND: "not_used", HIST: "not_used", EXEC: "not_used"},
        (0, 6, 30, 4),
    ),
    Tier.LOW: (
        "Screen",
        {LEG, PRD, DNS},
        {LEG: 3, REG: 0, PRD: 1, JOB: 0, DNS: 10, IND: 0, HIST: 0, EXEC: 0},
        {LEG: "screen", REG: "not_used", PRD: "homepage", JOB: "not_used", DNS: "full", IND: "not_used",
         HIST: "not_used", EXEC: "not_used"},
        (0, 2, 15, 3),
    ),
}

RESERVED: dict[Tier, list[str]] = {
    Tier.CRITICAL: [
        "AI due-diligence questionnaire (FS-ISAC Level 3 equivalent)",
        "contract and AI-clause review",
        "SOC 2 Type II scope and ISO/IEC 42001 validation",
        "AI system and sub-processor inventory request",
    ],
    Tier.HIGH: [
        "targeted AI questionnaire (FS-ISAC Level 2)",
        "review of contract data-use and sub-processor clauses",
        "attestations if AI is confirmed on customer data",
    ],
    Tier.MEDIUM: ["AI items in the next periodic review", "confirm data-use terms"],
    Tier.LOW: ["record and re-screen annually"],
}


# --------------------------------------------------------------------------- builders


def make_result(
    tier: Tier,
    *,
    d_level: int = 2,
    d_anchor: str = "D2",
    vendor_id: str = "V-900",
    overridden_from: Tier | None = None,
) -> CriticalityResult:
    """A synthetic CriticalityResult: only the final tier and the D factor matter to the planner."""
    levels = {"O": 2, "D": d_level, "P": 1, "R": 2, "V": 1}
    factors = {
        code: FactorScore(
            factor=code,
            level=level,
            anchor=d_anchor if code == "D" else f"{code}{level}",
            trigger="",
            source_fields=["data_accessed"],
            weight=WEIGHTS[code],
            points=WEIGHTS[code] * level,
        )
        for code, level in levels.items()
    }
    computed = overridden_from or tier
    override = None
    if overridden_from is not None:
        override = TierOverride(
            vendor_id=vendor_id, tier=tier, reason="Analyst override recorded for this test.", analyst="Tester",
            date="2026-10-02",
        )
    return CriticalityResult(
        vendor_id=vendor_id,
        rubric_version="1.0",
        factors=factors,
        score=sum(f.points for f in factors.values()),
        score_tier=computed,
        floors_fired=[],
        computed_tier=computed,
        tier=tier,
        sensitivity=[],
        perturbation_stable=True,
        override=override,
    )


def make_profile(vendor_id: str = "V-900") -> VendorProfile:
    return VendorProfile(row=12, vendor_id=vendor_id, name="Synthetic Vendor Ltd", website="www.synthetic.example")


def plan_for(tier: Tier, sec_registrant: bool | None = True, **result_kwargs: Any) -> DepthPlan:
    result = make_result(tier, **result_kwargs)
    return plan_depth(result, make_profile(result.vendor_id), sec_registrant=sec_registrant)


def log(plan: DepthPlan, default: CoverageStatus = CoverageStatus.DONE, requests: int = 1,
        **by_family: CoverageStatus) -> list[CoverageEntry]:
    """One Coverage Log entry per mandatory family; keyword arguments override the status of one family."""
    return [
        CoverageEntry(vendor_id=plan.vendor_id, family=f.family, mandatory=True,
                      status=by_family.get(f.family.value, default), requests_used=requests, cap=f.cap)
        for f in plan.families
        if f.mandatory
    ]


def mandatory(plan: DepthPlan) -> set[SourceFamily]:
    return {f.family for f in plan.families if f.mandatory}


def shape(plan: DepthPlan, family: SourceFamily) -> tuple[bool, str, int]:
    fp = plan.family(family)
    assert fp is not None
    return fp.mandatory, fp.mode, fp.cap


def raw_config() -> dict[str, Any]:
    with DEFAULT_CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


# --------------------------------------------------------------------------- config


def test_default_config_defines_every_tier_and_family() -> None:
    config = load_depth_config()
    assert config.version == "1.0"
    assert config.max_cell_chars == 1050
    assert set(config.tiers) == set(Tier)
    for policy in config.tiers.values():
        assert set(policy.families) == set(SourceFamily)


def test_load_from_explicit_path(tmp_path) -> None:
    copy = tmp_path / "depth.toml"
    copy.write_bytes(DEFAULT_CONFIG_PATH.read_bytes())
    assert load_depth_config(copy) == load_depth_config()


def test_config_rejects_tier_missing_a_family() -> None:
    data = raw_config()
    del data["tiers"]["Low"]["families"]["EXEC"]
    with pytest.raises(ValidationError, match="Low"):
        DepthConfig.model_validate(data)


def test_config_rejects_mandatory_mode_without_words() -> None:
    data = raw_config()
    del data["family_words"]["PRD"]["homepage"]
    with pytest.raises(ValidationError, match="homepage"):
        DepthConfig.model_validate(data)


def test_config_rejects_unknown_keys() -> None:
    data = raw_config()
    data["tiers"]["High"]["families"]["LEG"]["capp"] = 3
    with pytest.raises(ValidationError):
        DepthConfig.model_validate(data)


def test_config_rejects_missing_modifier() -> None:
    data = raw_config()
    del data["modifiers"]["M-D3P"]
    with pytest.raises(ValidationError, match="M-D3P"):
        DepthConfig.model_validate(data)


# --------------------------------------------------------------------------- plans per tier


@pytest.mark.parametrize("tier", list(Tier))
def test_plan_for_each_tier(tier: Tier) -> None:
    label, required, caps, modes, budgets = EXPECTED[tier]
    plan = plan_for(tier)
    assert plan.tier is tier
    assert plan.label == label
    assert [f.family for f in plan.families] == list(SourceFamily)
    assert mandatory(plan) == required
    assert {f.family: f.cap for f in plan.families} == caps
    assert {f.family: f.mode for f in plan.families} == modes
    assert (plan.discretionary_fetches, plan.gemini_calls, plan.analyst_minutes, plan.saturation_window) == budgets
    assert plan.reserved_for_meridian == RESERVED[tier]
    assert plan.modifiers == []
    assert all(f.reason.strip() for f in plan.families)


def test_labels_use_an_en_dash() -> None:
    assert "–" in plan_for(Tier.CRITICAL).label
    assert "–" in plan_for(Tier.HIGH).label


def test_reasons_explain_why_families_thin_out() -> None:
    medium, low = plan_for(Tier.MEDIUM), plan_for(Tier.LOW)
    assert all(p.family(LEG).reason.startswith("Never dropped") for p in (medium, low))
    assert all(p.family(DNS).reason.startswith("Never dropped") for p in (medium, low))
    assert medium.family(IND).reason.startswith("None below High")
    assert medium.family(REG).reason.startswith("One full-text query at Medium")
    assert low.family(REG).reason.startswith("None at Low")
    assert plan_for(Tier.HIGH).family(HIST).reason.startswith("On a lead at High")


def test_plan_follows_the_final_tier_after_an_analyst_override() -> None:
    plan = plan_for(Tier.CRITICAL, overridden_from=Tier.HIGH)
    assert plan.tier is Tier.CRITICAL
    assert plan.label == "Full review – tier-driven"


def test_plan_refuses_a_result_for_another_vendor() -> None:
    with pytest.raises(ValueError, match="V-001"):
        plan_depth(make_result(Tier.HIGH, vendor_id="V-001"), make_profile("V-002"))


def test_plan_refuses_a_result_without_a_d_factor() -> None:
    result = make_result(Tier.HIGH)
    result = result.model_copy(update={"factors": {k: v for k, v in result.factors.items() if k != "D"}})
    with pytest.raises(ValueError, match="D factor"):
        plan_depth(result, make_profile())


def test_plan_is_deterministic() -> None:
    assert plan_for(Tier.HIGH, d_level=4, d_anchor="D4") == plan_for(Tier.HIGH, d_level=4, d_anchor="D4")


# --------------------------------------------------------------------------- modifiers


def test_m_d4_fssi_like_high_vendor_runs_leg_and_hist_at_critical_caps() -> None:
    plan = plan_for(Tier.HIGH, d_level=4, d_anchor="D4", vendor_id="V-003")
    assert plan.modifiers == ["M-D4"]
    assert shape(plan, LEG) == (True, "full", 40)
    assert shape(plan, HIST) == (True, "full", 30)
    assert "M-D4" in plan.family(HIST).reason
    assert shape(plan, IND) == (True, "full", 20)  # untouched
    assert mandatory(plan) == {LEG, REG, PRD, JOB, DNS, IND, HIST}


def test_m_d4_does_not_apply_at_critical() -> None:
    plan = plan_for(Tier.CRITICAL, d_level=4, d_anchor="D4", vendor_id="V-002")  # Fiserv-like
    assert plan.modifiers == []


def test_m_d4_at_medium_raises_leg_mode_and_cap() -> None:
    plan = plan_for(Tier.MEDIUM, d_level=4, d_anchor="D4")
    assert plan.modifiers == ["M-D4"]
    assert shape(plan, LEG) == (True, "full", 40)
    assert shape(plan, HIST) == (True, "full", 30)
    assert shape(plan, REG) == (True, "one_query", 5)
    assert "M-D4" in plan.family(LEG).reason


def test_m_d3p_automworx_like_vendor_makes_ind_mandatory_at_critical_cap() -> None:
    plan = plan_for(Tier.HIGH, d_level=3, d_anchor="D3-P", vendor_id="V-001")
    assert plan.modifiers == ["M-D3P"]
    assert shape(plan, IND) == (True, "full", 30)
    assert "M-D3P" in plan.family(IND).reason
    assert D3P_NOTE in render_depth_cell(plan)


def test_m_d3p_applies_at_critical_and_keeps_its_note() -> None:
    plan = plan_for(Tier.CRITICAL, d_level=3, d_anchor="D3-P")
    assert plan.modifiers == ["M-D3P"]
    assert shape(plan, IND) == (True, "full", 30)
    assert D3P_NOTE in render_depth_cell(plan)


def test_m_d3p_at_medium_adds_ind() -> None:
    plan = plan_for(Tier.MEDIUM, d_level=3, d_anchor="D3-P")  # e.g. after an analyst override down to Medium
    assert shape(plan, IND) == (True, "full", 30)


@pytest.mark.parametrize("tier", [Tier.CRITICAL, Tier.HIGH, Tier.MEDIUM])
def test_m_private_marks_reg_not_applicable_and_deepens_leg_and_hist(tier: Tier) -> None:
    plan = plan_for(tier, sec_registrant=False)
    assert plan.modifiers == ["M-Private"]
    reg = plan.family(REG)
    assert (reg.mandatory, reg.mode, reg.cap, reg.reason) == (False, "not_applicable", 0, NOT_REGISTRANT)
    assert shape(plan, LEG) == (True, "full", 40)
    assert shape(plan, HIST) == (True, "full", 30)
    assert REG not in mandatory(plan)


def test_m_private_does_not_apply_at_low_where_reg_is_not_used() -> None:
    plan = plan_for(Tier.LOW, sec_registrant=False, d_level=1, d_anchor="D1")
    assert plan.modifiers == []
    assert shape(plan, REG) == (False, "not_used", 0)
    assert shape(plan, LEG) == (True, "screen", 3)


@pytest.mark.parametrize("tier", [Tier.CRITICAL, Tier.HIGH, Tier.MEDIUM])
def test_unknown_registrant_keeps_reg_mandatory_with_suffix(tier: Tier) -> None:
    plan = plan_for(tier, sec_registrant=None)
    reg = plan.family(REG)
    assert reg.mandatory
    assert reg.reason.endswith(" " + REGISTRANT_SUFFIX)
    assert "M-Private" not in plan.modifiers


def test_known_registrant_reason_has_no_suffix() -> None:
    assert REGISTRANT_SUFFIX not in plan_for(Tier.HIGH, sec_registrant=True).family(REG).reason


def test_unknown_registrant_adds_no_suffix_where_reg_is_not_used() -> None:
    assert REGISTRANT_SUFFIX not in plan_for(Tier.LOW, sec_registrant=None).family(REG).reason


def test_fssi_like_private_d4_vendor_lists_modifiers_in_order() -> None:
    plan = plan_for(Tier.HIGH, sec_registrant=False, d_level=4, d_anchor="D4")
    assert plan.modifiers == ["M-D4", "M-Private"]
    hist = plan.family(HIST).reason
    assert "M-D4" in hist and "M-Private" in hist
    assert mandatory(plan) == {LEG, PRD, JOB, DNS, IND, HIST}


def test_automworx_like_private_d3p_vendor_lists_modifiers_in_order() -> None:
    plan = plan_for(Tier.HIGH, sec_registrant=False, d_level=3, d_anchor="D3-P")
    assert plan.modifiers == ["M-D3P", "M-Private"]
    assert shape(plan, IND) == (True, "full", 30)
    assert shape(plan, HIST) == (True, "full", 30)


def test_all_three_modifiers_keep_the_fixed_order() -> None:
    # Synthetic: level 4 with the D3-P anchor fires both D modifiers, which no real rubric output does.
    plan = plan_for(Tier.HIGH, sec_registrant=False, d_level=4, d_anchor="D3-P")
    assert plan.modifiers == ["M-D4", "M-D3P", "M-Private"]


def test_family_already_at_critical_depth_keeps_its_tier_reason() -> None:
    base, raised = plan_for(Tier.HIGH), plan_for(Tier.HIGH, d_level=4, d_anchor="D4")
    assert raised.family(LEG) == base.family(LEG)  # High LEG is already full / 40


# --------------------------------------------------------------------------- coverage


@pytest.mark.parametrize("status", list(CoverageStatus))
def test_is_complete_uses_the_shared_complete_set(status: CoverageStatus) -> None:
    assert is_complete(status) is (status in COMPLETE_STATUSES)
    assert is_complete(status.value) is (status in COMPLETE_STATUSES)


@pytest.mark.parametrize("status", sorted(COMPLETE_STATUSES, key=lambda s: s.value))
def test_coverage_complete_when_every_mandatory_family_is_complete(status: CoverageStatus) -> None:
    plan = plan_for(Tier.CRITICAL)
    assert coverage_complete(plan, log(plan, status))


@pytest.mark.parametrize("status", [s for s in CoverageStatus if s not in COMPLETE_STATUSES])
def test_coverage_incomplete_when_one_mandatory_family_is_incomplete(status: CoverageStatus) -> None:
    plan = plan_for(Tier.CRITICAL)
    assert not coverage_complete(plan, log(plan, PRD=status))


def test_coverage_incomplete_when_a_mandatory_family_has_no_entry() -> None:
    plan = plan_for(Tier.HIGH)
    assert not coverage_complete(plan, [e for e in log(plan) if e.family is not JOB])


def test_coverage_ignores_optional_families_and_other_vendors() -> None:
    plan = plan_for(Tier.HIGH)
    noise = [
        CoverageEntry(vendor_id=plan.vendor_id, family=HIST, mandatory=False, status=CoverageStatus.ERROR),
        CoverageEntry(vendor_id="V-999", family=LEG, mandatory=True, status=CoverageStatus.BLOCKED_BOT),
    ]
    assert coverage_complete(plan, log(plan) + noise)
    other_vendor_only = [e.model_copy(update={"vendor_id": "V-999"}) for e in log(plan)]
    assert not coverage_complete(plan, other_vendor_only)


def test_family_counts_as_complete_when_any_of_its_entries_is_complete() -> None:
    plan = plan_for(Tier.HIGH)
    manual = CoverageEntry(vendor_id=plan.vendor_id, family=PRD, mandatory=True, status=CoverageStatus.DONE_MANUAL)
    blocked = log(plan, PRD=CoverageStatus.BLOCKED_BOT)
    assert coverage_complete(plan, blocked + [manual])  # e.g. a manual capture after the bot wall
    assert coverage_complete(plan, [manual] + blocked)  # entry order does not matter
    assert render_depth_cell(plan, blocked + [manual]) == render_depth_cell(plan, [manual] + blocked)
    assert "6 of 6 mandatory source families completed" in render_depth_cell(plan, [manual] + blocked)


def test_not_applicable_reg_is_not_required_for_a_private_vendor() -> None:
    plan = plan_for(Tier.HIGH, sec_registrant=False)
    assert coverage_complete(plan, log(plan))
    assert REG not in {e.family for e in log(plan)}


# --------------------------------------------------------------------------- column N


def test_render_critical_cell_with_coverage_golden() -> None:
    plan = plan_for(Tier.CRITICAL)
    requests = {LEG: 12, REG: 30, PRD: 0, JOB: 45, DNS: 4, IND: 30, HIST: 10}
    status = {PRD: CoverageStatus.DONE_MANUAL, IND: CoverageStatus.STOPPED}
    coverage = [
        CoverageEntry(vendor_id=plan.vendor_id, family=fam, mandatory=True,
                      status=status.get(fam, CoverageStatus.DONE), requests_used=n, cap=plan.family(fam).cap)
        for fam, n in requests.items()
    ]
    assert render_depth_cell(plan, coverage) == (
        "Full review – tier-driven. A Critical tier calls for the deepest level of work: "
        f"{CRITICAL_FAMILIES_IN_WORDS}. "
        "Coverage: 7 of 7 mandatory source families completed (5 done, 1 done by manual capture, "
        "1 stopped at a cap or stop rule); 131 automated requests within caps. "
        "Reserved for Meridian (non-OSINT): AI due-diligence questionnaire (FS-ISAC Level 3 equivalent), "
        "contract and AI-clause review, SOC 2 Type II scope and ISO/IEC 42001 validation, "
        "AI system and sub-processor inventory request. "
        f"{CLOSING}"
    )


def test_render_without_coverage_has_no_coverage_line() -> None:
    text = render_depth_cell(plan_for(Tier.CRITICAL))
    assert text.startswith(f"Full review – tier-driven. A Critical tier calls for the deepest level of work: "
                           f"{CRITICAL_FAMILIES_IN_WORDS}. Reserved for Meridian (non-OSINT): ")
    assert "Coverage:" not in text


@pytest.mark.parametrize("tier", list(Tier))
def test_render_starts_with_label_mentions_reserved_steps_and_closes(tier: Tier) -> None:
    plan = plan_for(tier)
    text = render_depth_cell(plan)
    assert text.startswith(f"{EXPECTED[tier][0]}. A {tier.value} tier calls for ")
    assert "Reserved for Meridian (non-OSINT): " + ", ".join(RESERVED[tier]) + "." in text
    assert text.endswith(CLOSING)


def test_render_names_only_mandatory_families_at_low() -> None:
    text = render_depth_cell(plan_for(Tier.LOW))
    assert ("Screen. A Low tier calls for a screening level of work: the privacy notice and sub-processor or "
            "trust page, the homepage, and DNS records.") in text
    assert "regulatory" not in text


def test_render_reports_incomplete_and_missing_families_and_cap_overrun() -> None:
    plan = plan_for(Tier.CRITICAL)
    coverage = [
        e.model_copy(update={"requests_used": 151}) if e.family is PRD else e  # PRD cap is 150
        for e in log(plan, requests=10, LEG=CoverageStatus.BLOCKED_BOT)
        if e.family is not HIST
    ]
    text = render_depth_cell(plan, coverage)
    assert ("Coverage: 5 of 7 mandatory source families completed (5 done, 1 blocked by bot protection, "
            "1 not logged); 201 automated requests, exceeding the cap for 1 source family.") in text


def test_render_describes_modifiers_in_words() -> None:
    plan = plan_for(Tier.HIGH, sec_registrant=False, d_level=4, d_anchor="D4", vendor_id="V-003")
    text = render_depth_cell(plan, log(plan, requests=8))
    assert ("and archive history; legal and archive review at Critical depth, including AI features of named "
            "platforms, for data of the highest sensitivity; regulatory filings not applicable (not an SEC "
            "registrant; EDGAR checked), offset by deeper legal and archive review. Coverage: 6 of 6 mandatory "
            "source families completed (6 done); 48 automated requests within caps.") in text
    assert text.startswith("Standard review – tier-driven. A High tier calls for a standard level of work: "
                           "legal and trust pages (privacy, terms, sub-processors, AI policy), ")


def test_render_is_deterministic() -> None:
    plan = plan_for(Tier.HIGH, sec_registrant=False, d_level=3, d_anchor="D3-P")
    coverage = log(plan, requests=3, JOB=CoverageStatus.ERROR)
    assert render_depth_cell(plan, coverage) == render_depth_cell(plan, list(reversed(coverage)))
    assert render_depth_cell(plan, coverage) == render_depth_cell(plan_for(Tier.HIGH, sec_registrant=False,
                                                                           d_level=3, d_anchor="D3-P"), coverage)


def test_render_drops_status_breakdown_before_exceeding_the_budget() -> None:
    config = load_depth_config()
    plan = plan_for(Tier.CRITICAL)
    coverage = log(plan, requests=5, LEG=CoverageStatus.BLOCKED_TOU)
    detailed = render_depth_cell(plan, coverage, config)
    assert "(6 done, 1 barred by terms of use)" in detailed
    tight = config.model_copy(update={"max_cell_chars": len(detailed) - 1})
    compact = render_depth_cell(plan, coverage, tight)
    assert "Coverage: 6 of 7 mandatory source families completed; 35 automated requests within caps." in compact
    assert compact.endswith(CLOSING)
    with pytest.raises(ValueError, match="1050|budget"):
        render_depth_cell(plan, coverage, config.model_copy(update={"max_cell_chars": 200}))


def _worst_case_log(plan: DepthPlan) -> list[CoverageEntry]:
    """Every mandatory family with a different status and an over-cap request count."""
    statuses = list(CoverageStatus)
    required = [f for f in plan.families if f.mandatory]
    return [
        CoverageEntry(vendor_id=plan.vendor_id, family=f.family, mandatory=True,
                      status=statuses[i % len(statuses)], requests_used=99_999, cap=f.cap)
        for i, f in enumerate(required)
    ]


# Every D factor the rubric can emit, as far as the planner is concerned. D3-P is a level-3 anchor, so M-D4 and
# M-D3P never fire together (the synthetic level-4 D3-P result is used only for the modifier-order test).
REACHABLE_D = [(2, "D2"), (3, "D3"), (3, "D3-P"), (4, "D4")]


@pytest.mark.parametrize("sec_registrant", [True, False, None])
@pytest.mark.parametrize(("d_level", "d_anchor"), REACHABLE_D)
@pytest.mark.parametrize("tier", list(Tier))
def test_render_fits_column_budget_for_every_plan_shape(
    tier: Tier, d_level: int, d_anchor: str, sec_registrant: bool | None
) -> None:
    plan = plan_for(tier, sec_registrant=sec_registrant, d_level=d_level, d_anchor=d_anchor)
    for coverage in (None, log(plan), _worst_case_log(plan)):
        text = render_depth_cell(plan, coverage)
        assert len(text) <= 1050
        assert text.startswith(plan.label + ". ")
        assert text.endswith(CLOSING)
        assert ("Coverage: " in text) is (coverage is not None)
        assert "Reserved for Meridian (non-OSINT): " in text


# --------------------------------------------------------------------------- Method & Legend tables


def test_excluded_sources_carry_reasons() -> None:
    rows = excluded_sources(load_depth_config())
    assert len(rows) == 7
    names = " | ".join(name for name, _ in rows)
    for expected in ("ddgs", "Google News RSS", "Gemini grounding", "LinkedIn", "Glassdoor", "Save Page Now",
                     "Patents", "GDELT", "OpenAlex", "Hacker News", "NDA"):
        assert expected in names
    assert all(isinstance(reason, str) and reason.strip() for _, reason in rows)


def test_depth_table_is_a_tier_by_family_grid() -> None:
    config = load_depth_config()
    rows = depth_table(config)
    assert len(rows) == len(Tier) * len(SourceFamily)
    assert [r["tier"] for r in rows[:: len(SourceFamily)]] == ["Critical", "High", "Medium", "Low"]
    assert [r["family"] for r in rows[: len(SourceFamily)]] == [f.value for f in SourceFamily]
    assert all(set(r) == {"tier", "label", "family", "mandatory", "mode", "cap", "reason"} for r in rows)
    cell = {(r["tier"], r["family"]): r for r in rows}
    assert cell[("Critical", "PRD")]["cap"] == 150
    assert cell[("Medium", "REG")]["mode"] == "one_query"
    low_reg = {k: v for k, v in cell[("Low", "REG")].items() if k != "reason"}
    assert low_reg == {"tier": "Low", "label": "Screen", "family": "REG", "mandatory": False, "mode": "not_used",
                       "cap": 0}
    assert cell[("High", "REG")]["reason"].endswith(REGISTRANT_SUFFIX)
    for tier in Tier:  # the grid matches what the planner produces for an unknown registrant
        plan = plan_for(tier, sec_registrant=None)
        for f in plan.families:
            row = cell[(tier.value, f.family.value)]
            assert (row["mandatory"], row["mode"], row["cap"], row["reason"]) == (
                f.mandatory, f.mode, f.cap, f.reason
            )
