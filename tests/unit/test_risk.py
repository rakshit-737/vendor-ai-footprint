"""Tests for the AI risk engine (footprint.risk + config/risk.toml; design Appendix A 2.8, contracts_p3 section 8).

Profiles and criticality come from the real workbook (never retyped). Evidence items are synthetic: the excerpts
are invented sentences shaped like the scouting notes, except four review regression cases quoted verbatim from the
frozen evidence pack (public SEC filing text and a public podcast page; marked where defined). Fully offline.
"""

from __future__ import annotations

import functools
import hashlib
import random
import re
from pathlib import Path

import pytest

from footprint import risk
from footprint.criticality import score_profile
from footprint.depth import plan_depth
from footprint.models import (
    CriticalityResult,
    EvidenceItem,
    Indicator,
    RiskInputs,
    RiskResult,
    SignalTags,
    SourceFamily,
    Tier,
    UsageVerdict,
    VendorProfile,
)
from footprint.review import OverrideStore, ReviewRecord
from footprint.workbook import read_workbook

REPO = Path(__file__).resolve().parents[2]
WORKBOOK = REPO / "data" / "input" / "Meridian_Vendor_Input.xlsx"
AS_OF = "2026-10-02"
GAPS_ALL_MISSING = {g: True for g in ("t1", "t2", "t3", "t4", "t5", "t6")}
CODES = re.compile(r"\b(?:[EK][0-3]|TP\d|TG\d|X[1-6]|RT[1-7]|t[1-6]|[US]\d|R[0-3]|SR:)\b")


# --------------------------------------------------------------------------- builders


@functools.lru_cache(maxsize=1)
def _profiles() -> dict[str, VendorProfile]:
    data = read_workbook(WORKBOOK)
    return {p.vendor_id: p for p in [data.example, *data.vendors] if p is not None}


def vendor(vid: str) -> tuple[VendorProfile, CriticalityResult, object]:
    p = _profiles()[vid]
    c = score_profile(p)
    return p, c, plan_depth(c, p)


def with_factors(c: CriticalityResult, **levels: tuple[int, str]) -> CriticalityResult:
    """A copy of c with factor levels/anchors replaced, e.g. D=(4, 'D4')."""
    factors = dict(c.factors)
    for code, (level, anchor) in levels.items():
        factors[code] = factors[code].model_copy(update={"level": level, "anchor": anchor})
    return c.model_copy(update={"factors": factors})


def mk(vid: str, excerpt: str, *, family: SourceFamily = SourceFamily.PRD, source_type: str = "Product page",
       publisher: str = "Vendor", action: str = "unknown", providers: tuple[str, ...] | list[str] = (),
       data: tuple[str, ...] | list[str] = (), indicators: tuple[str, ...] = (), published: str = "",
       review: str = "unreviewed", url: str | None = None, **tag_kw: object) -> EvidenceItem:
    tag: dict[str, object] = dict(u_class="U1", sr="B", sp="S2", rl="R3", rc="T3", ic=2, locus="service_feature",
                                  ai_type="unspecified", strength="Strong")
    tag.update(tag_kw)
    digest = hashlib.sha256(excerpt.encode()).hexdigest()[:12]
    return EvidenceItem(
        vendor_id=vid, passage_id=digest[:16].ljust(16, "0"), doc_id="d" * 64, capture_id="c" * 64, family=family,
        source_type=source_type, publisher=publisher, title="Synthetic page",
        url=url or f"https://example.test/{vid}/{digest}", retrieved_at="2026-10-02T12:00:00Z",
        published=published, date_basis="publication" if published else "retrieval", excerpt=excerpt, start=0,
        end=len(excerpt), tags=SignalTags(**tag), indicators=[Indicator(code=c, span=excerpt[:12]) for c in indicators],
        providers=list(providers), data_mentioned=list(data), action_level=action, review_status=review,
    )


def verdict(rule: str, *, qualifying=(), corroborating=(), decisive=(), coverage_complete: bool = True) -> UsageVerdict:
    likelihood = {"a": "roughly even chance", "b": "very likely", "c": "likely", "d": "very unlikely",
                  "e": "unlikely", "f": "unlikely"}[rule]
    return UsageVerdict(
        rule=rule, likelihood=likelihood, confidence="Moderate", confidence_reason="a synthetic test verdict",
        qualifying=[i.item_key for i in qualifying], corroborating=[i.item_key for i in corroborating],
        decisive=[i.item_key for i in decisive], coverage_complete=coverage_complete,
    )


def assess(vid: str, v: UsageVerdict, items: list[EvidenceItem], *, c: CriticalityResult | None = None,
           overrides: OverrideStore | None = None) -> RiskResult:
    p, crit, plan = vendor(vid)
    return risk.assess(v, items, c or crit, p, plan, as_of=AS_OF, overrides=overrides)


def derive(vid: str, v: UsageVerdict, items: list[EvidenceItem], *, c: CriticalityResult | None = None,
           p: VendorProfile | None = None, overrides: OverrideStore | None = None) -> RiskInputs:
    prof, crit, plan = vendor(vid)
    return risk.derive_inputs(v, items, c or crit, p or prof, plan, overrides=overrides)


def inputs(e: int, k: int, tp: int, *, missing: int = 3, e_assumed: bool = False, k_assumed: bool = False,
           e_if_confirmed: int | None = None) -> RiskInputs:
    gaps = {g: n < missing for n, g in enumerate(("t1", "t2", "t3", "t4", "t5", "t6"))}
    return RiskInputs(e=e, k=k, tp=tp, gaps=gaps, e_assumed=e_assumed, k_assumed=k_assumed,
                      e_if_confirmed=e_if_confirmed)


# --------------------------------------------------------------------------- synthetic evidence per vendor
# Shaped like the design's per-vendor calibration notes (Appendix A 5); wording invented for the tests.


def automworx_items() -> list[EvidenceItem]:
    affiliate = mk("V-001", "Our assistant platform answers engineers with a locally hosted language model.",
                   family=SourceFamily.IND, source_type="Affiliate capabilities page", publisher="Labarum AI",
                   u_class="U2", sr="C", sp="S2", rl="R1", locus="affiliate_inferred", ai_type="genai_llm",
                   strength="Context - inferred affiliate")
    marketing = mk("V-001", "Human-led delivery, enabled by AI.", publisher="AutomWorx", u_class="U7", sr="C",
                   sp="S1", rl="R2", locus="unknown", strength="Marketing only")
    platform = mk("V-001", "The default model for the hosted edition is Gemini; a customer may bring its own model.",
                  family=SourceFamily.LEG, source_type="Product terms", publisher="Broadcom", u_class="U2", sr="A",
                  sp="S3", rl="R1", locus="platform_supplier", providers=["Gemini"], indicators=("G1", "G3"),
                  strength="Context - platform supplier")
    return [affiliate, marketing, platform]


def fiserv_items() -> list[EvidenceItem]:
    pilot = mk("V-002", "An agentic pilot now resolves routine internal service tickets, and a person approves "
                        "each closure.", family=SourceFamily.REG, source_type="SEC Form DEFA14A", publisher="Fiserv",
               u_class="U3", sr="A", sp="S2", rl="R2", locus="delivery_ops", ai_type="agentic",
               action="human_reviewed_decision", strength="Moderate")
    filing = mk("V-002", "We are embedding artificial intelligence across our banking solutions.",
                family=SourceFamily.REG, source_type="SEC Form 10-K", publisher="Fiserv", u_class="U2", sr="A",
                sp="S1", rl="R2", locus="vendor_addon", strength="Weak")
    release = mk("V-002", "The company is expanding its collaboration with OpenAI and Microsoft on AI for "
                          "financial institutions.", source_type="Press release", publisher="Fiserv", u_class="U4",
                 sr="C", sp="S2", rl="R2", locus="relationship", providers=["OpenAI", "Microsoft"],
                 strength="Moderate")
    policy = mk("V-002", "Our generative AI policy aligns with the NIST AI RMF.", source_type="Sustainability report",
                publisher="Fiserv", u_class="U6", sr="B", sp="S1", rl="R1", locus="corporate_internal",
                strength="Weak")
    dns = mk("V-002", "anthropic-domain-verification=dv-test-0002", family=SourceFamily.DNS,
             source_type="DNS TXT record", publisher="Fiserv", u_class="U4", sr="B", sp="S1", rl="R2",
             locus="relationship", providers=["Anthropic"], strength="Context - relationship only")
    return [pilot, filing, release, policy, dns]


def fssi_items() -> list[EvidenceItem]:
    platform = mk("V-003", "Experience with document composition platforms that offer generative AI add-ons is a "
                           "plus.", family=SourceFamily.JOB, source_type="Job posting", publisher="FSSI",
                  u_class="U5", sr="B", sp="S1", rl="R1", locus="platform_supplier", ai_type="genai_llm",
                  strength="Context - platform supplier")
    trap = mk("V-003", "Our intelligent inserting line reads barcodes to match every page.", u_class="U7", sr="B",
              sp="S1", rl="R1", locus="unknown", ai_type="not_ai", strength="Marketing only",
              indicators=("M4",))
    return [platform, trap]


def terrapin_items() -> list[EvidenceItem]:
    dns = [mk("V-004", f"{name.lower()}-domain-verification=dv-test-4", family=SourceFamily.DNS,
              source_type="DNS TXT record", publisher="Terrapin", u_class="U4", sr="B", sp="S1", rl="R2",
              locus="relationship", providers=[name], strength="Context - relationship only")
           for name in ("OpenAI", "Anthropic")]
    faq = mk("V-004", "We plan to explore AI-assisted reporting in a future release.", publisher="Terrapin",
             u_class="U7", sr="B", sp="S0", rl="R1", locus="unknown", strength="Marketing only")
    return [*dns, faq]


def bny_items() -> list[EvidenceItem]:
    rtp = mk("V-005", "A single connection to the instant payment network, with AI-enabled anomaly detection.",
             publisher="BNY", u_class="U1", sr="B", sp="S2", rl="R3", locus="service_feature",
             ai_type="predictive_ml", strength="Strong")
    interview = mk("V-005", "The bank applies anomaly detection to real-time rails.", family=SourceFamily.IND,
                   source_type="Trade press interview", publisher="Trade Weekly", u_class="U1", sr="C", sp="S2",
                   rl="R2", locus="service_feature", ai_type="predictive_ml", strength="Moderate")
    paper = mk("V-005", "AI reviews sanctions screening alerts with a human in the loop on every decision.",
               source_type="Whitepaper", publisher="BNY", u_class="U1", sr="C", sp="S2", rl="R2",
               locus="service_feature", ai_type="predictive_ml", action="human_reviewed_decision",
               strength="Moderate")
    deck = mk("V-005", "Our enterprise AI platform integrates models from OpenAI, Google and Anthropic.",
              source_type="Investor presentation", publisher="BNY", u_class="U3", sr="B", sp="S3", rl="R2",
              locus="corporate_internal", ai_type="genai_llm", providers=["OpenAI", "Google", "Anthropic"],
              strength="Moderate")
    principles = mk("V-005", "Our Responsible AI principles govern every model we build.", family=SourceFamily.LEG,
                    source_type="AI policy page", publisher="BNY", u_class="U6", sr="B", sp="S1", rl="R1",
                    locus="corporate_internal", strength="Weak")
    return [rtp, interview, paper, deck, principles]


def tch_items() -> list[EvidenceItem]:
    noc = mk("V-006", "Use AI-powered assistants such as Copilot and ChatGPT during incident response.",
             family=SourceFamily.JOB, source_type="Job posting", publisher="The Clearing House", u_class="U3",
             sr="B", sp="S2", rl="R2", locus="delivery_ops", ai_type="genai_llm", action="advisory",
             providers=["Microsoft Copilot", "ChatGPT"], strength="Moderate")
    sdlc = mk("V-006", "Engineers on the real-time platform build with AI-assisted development tools.",
              family=SourceFamily.JOB, source_type="Job posting", publisher="The Clearing House", u_class="U3",
              sr="B", sp="S2", rl="R2", locus="sdlc", ai_type="genai_llm", strength="Moderate")
    dns = mk("V-006", "openai-domain-verification=dv-test-0006", family=SourceFamily.DNS,
             source_type="DNS TXT record", publisher="The Clearing House", u_class="U4", sr="B", sp="S1", rl="R2",
             locus="relationship", providers=["OpenAI"], strength="Context - relationship only")
    return [noc, sdlc, dns]


# --------------------------------------------------------------------------- config


def test_config_loads_and_agrees_with_the_models():
    cfg = risk.load_risk_config()
    assert cfg is risk.load_risk_config()  # cached per path
    assert cfg.version
    assert tuple(cfg.escalators) == risk.ESCALATOR_CODES == ("X1", "X2", "X3", "X4", "X5", "X6")
    assert tuple(cfg.themes) == risk.THEME_CODES == tuple(f"RT{n}" for n in range(1, 8))
    assert cfg.caps == {"Confirmed": "", "Probable": "High", "Inconclusive": "Medium",
                        "Affirmed negative": "None identified", "Not detected": "None identified"}
    assert cfg.gate.min_e == 2 and cfg.gate.min_k == 2 and cfg.gate.lowered_to == "Medium"
    assert cfg.floor_class == "High"


def test_config_rejects_an_incomplete_policy(tmp_path):
    text = (REPO / "config" / "risk.toml").read_text(encoding="utf-8")
    broken = tmp_path / "risk.toml"
    broken.write_text(text.replace("[escalators.X6]", "[escalators.X7]"), encoding="utf-8")
    with pytest.raises(ValueError, match="X1..X6|escalators"):
        risk.load_risk_config(broken)
    bad_cap = tmp_path / "risk_cap.toml"
    bad_cap.write_text(text.replace('Probable = "High"', 'Probable = "Medium"'), encoding="utf-8")
    with pytest.raises(ValueError, match="cap"):
        risk.load_risk_config(bad_cap)
    bad_regex = tmp_path / "risk_rx.toml"
    bad_regex.write_text(text.replace("'\\btickets?\\b',", "'(unclosed',"), encoding="utf-8")
    with pytest.raises(ValueError, match="pattern"):
        risk.load_risk_config(bad_regex)


def test_module_reads_no_clock():
    source = (REPO / "src" / "footprint" / "risk.py").read_text(encoding="utf-8")
    for forbidden in ("datetime.now", "date.today", "time.time", "utcnow", "import time"):
        assert forbidden not in source


# --------------------------------------------------------------------------- V-000 calibration


def test_v000_calibration_scores_17_critical():
    ri = risk.calibration_inputs()
    assert (ri.e, ri.k, ri.tp, ri.tg) == (3, 3, 3, 2)
    assert not ri.e_assumed and not ri.k_assumed
    rr = risk.calibrate()
    assert rr.arp == 17 and rr.base_class == "Critical" and rr.gate_met
    assert rr.cap == "" and rr.pre_cap_class == "Critical" and rr.final_class == "Critical"
    assert not rr.provisional and rr.ceiling_class == "" and rr.escalators_fired == []
    v = risk.calibration_verdict()
    assert v.label == "Confirmed" and v.column_o == "Yes" and not v.qualifying


def test_v000_inputs_scored_directly():
    gaps = {"t1": False, "t2": True, "t3": True, "t4": True, "t5": False, "t6": False}  # 3 missing -> TG 1
    rr = risk.score(RiskInputs(e=3, k=3, tp=3, gaps=gaps), risk.calibration_verdict(), tier=Tier.CRITICAL)
    assert rr.arp == 16 and rr.final_class == "Critical"


# --------------------------------------------------------------------------- expected outcomes (Appendix 2.8)


def test_expected_automworx_inconclusive_medium_provisional_ceiling_critical():
    items = automworx_items()
    affiliate, marketing, _ = items
    rr = assess("V-001", verdict("f", decisive=[affiliate, marketing]), items)
    i = rr.inputs
    assert (i.e, i.k, i.tp, i.tg) == (3, 3, 2, 3) and i.e_assumed and i.k_assumed
    assert i.e_items == [] and i.k_items == []
    assert rr.arp == 17 and rr.base_class == "Critical" and not rr.gate_met
    assert rr.pre_cap_class == "Medium" and rr.final_class == "Medium"
    assert rr.provisional and rr.cap == "Medium" and rr.ceiling_class == "Critical"
    assert "Labarum AI" in rr.flip_condition and rr.flip_condition.endswith("Critical.")


def test_expected_fiserv_probable_high():
    items = fiserv_items()
    pilot = items[0]
    rr = assess("V-002", verdict("c", qualifying=[pilot], decisive=[pilot]), items)
    i = rr.inputs
    assert (i.e, i.k, i.tp, i.tg) == (2, 2, 3, 1) and not i.e_assumed and not i.k_assumed
    assert i.e_items == [pilot.item_key] and i.k_items == [pilot.item_key]
    assert i.missing_gaps == ["t1", "t3", "t6"]
    assert rr.arp == 12 and rr.base_class == "High" and rr.gate_met
    assert rr.cap == "High" and rr.final_class == "High" and not rr.provisional and rr.ceiling_class == ""
    assert rr.escalators_fired == [] and rr.escalators_logged_not_applied == []
    assert "Confirmed" in rr.flip_condition and rr.flip_condition.endswith("Critical.")


def test_expected_fssi_not_detected_none_identified():
    items = fssi_items()
    rr = assess("V-003", verdict("e"), items)
    i = rr.inputs
    assert (i.e, i.k, i.tp, i.tg) == (0, 0, 2, 3) and not i.e_assumed and not i.k_assumed
    assert rr.cap == "None identified" and rr.final_class == "None identified"
    assert not rr.provisional and rr.ceiling_class == "" and rr.themes == []
    assert "reassessment" in rr.flip_condition and "platforms" in rr.flip_condition


def test_expected_fssi_incomplete_coverage_becomes_provisional_ceiling_critical():
    items = fssi_items()
    rr = assess("V-003", verdict("f", decisive=[items[0]], coverage_complete=False), items)
    i = rr.inputs
    assert (i.e, i.k, i.tp, i.tg) == (3, 2, 2, 3) and i.e_assumed and i.k_assumed  # 3a.2a.2.3 = 15
    assert rr.arp == 15 and rr.final_class == "Medium" and rr.provisional and rr.ceiling_class == "Critical"


def test_expected_terrapin_inconclusive_medium_provisional_ceiling_high():
    items = terrapin_items()
    rr = assess("V-004", verdict("f", decisive=items[:2]), items)
    i = rr.inputs
    assert (i.e, i.k, i.tp, i.tg) == (2, 2, 2, 3) and i.e_assumed and i.k_assumed
    assert rr.arp == 13 and rr.base_class == "High" and not rr.gate_met
    assert rr.final_class == "Medium" and rr.provisional and rr.ceiling_class == "High"
    assert "DNS records (Anthropic and OpenAI)" in rr.flip_condition and rr.flip_condition.endswith("High.")


def bny_items_with_stated_actions() -> list[EvidenceItem]:
    """BNY with the anomaly-detection pathways stated as advisory (flags for analysts), as the design row assumes."""
    rtp, interview, *rest = bny_items()
    return [rtp.model_copy(update={"action_level": "advisory"}),
            interview.model_copy(update={"action_level": "advisory"}), *rest]


def test_expected_bny_confirmed_high():
    # The design row (2.8: 2.2.3.1 = 12, High) holds once every pathway in the service states its action level.
    items = bny_items_with_stated_actions()
    rtp, interview, paper, _, _ = items
    rr = assess("V-005", verdict("b", qualifying=[rtp], corroborating=[interview],
                                 decisive=[rtp, interview, paper]), items)
    i = rr.inputs
    assert (i.e, i.k, i.tp, i.tg) == (2, 2, 3, 1) and not i.e_assumed and not i.k_assumed
    assert i.k_items == [paper.item_key] and rtp.item_key in i.e_items
    assert i.missing_gaps == ["t3", "t6"]
    assert rr.arp == 12 and rr.gate_met and rr.cap == "" and rr.final_class == "High" and rr.ceiling_class == ""
    flip = rr.flip_condition
    assert "exposure 3 of 3" in flip and "decision impact 3 of 3" in flip and flip.endswith("Critical.")


def test_bny_with_an_unknown_rtp_action_keeps_the_role_assumption():
    # The RTP anomaly detection does not say what its output does: the whitepaper's human review (decision impact 2)
    # is evidence for another pathway and cannot lower the payment-path assumption (3) the unknowns rule makes.
    items = bny_items()
    rtp, interview, paper, _, _ = items
    rr = assess("V-005", verdict("b", qualifying=[rtp], corroborating=[interview],
                                 decisive=[rtp, interview, paper]), items)
    i = rr.inputs
    assert (i.e, i.k, i.tp, i.tg) == (2, 3, 3, 1) and not i.e_assumed and i.k_assumed and i.k_items == []
    assert "role (a role in the payment path)" in i.k_reason and "decision impact 2 of 3" in i.k_reason
    assert rr.arp == 14 and rr.gate_met and rr.final_class == "Critical"   # evidenced exposure 2 meets the gate


def test_expected_tch_probable_high_flips_to_medium():
    items = tch_items()
    noc, sdlc, _ = items
    rr = assess("V-006", verdict("c", qualifying=[noc, sdlc], decisive=[noc, sdlc]), items)
    i = rr.inputs
    assert (i.e, i.k, i.tp, i.tg) == (2, 1, 3, 2) and not i.e_assumed and not i.k_assumed
    assert i.e_items == [noc.item_key] and i.k_items == [noc.item_key]
    assert rr.arp == 11 and rr.final_class == "High" and rr.cap == "High" and rr.escalators_fired == []
    assert "exposure 1 of 3" in rr.flip_condition and rr.flip_condition.endswith("Medium.")


@pytest.mark.parametrize("vid, build, rule", [
    ("V-001", automworx_items, "f"), ("V-002", fiserv_items, "c"), ("V-003", fssi_items, "e"),
    ("V-004", terrapin_items, "f"), ("V-005", bny_items, "b"), ("V-006", tch_items, "c"),
])
def test_cell_wording_carries_no_score_codes(vid, build, rule):
    items = build()
    q = [i for i in items if i.tags.strength in ("Strong", "Moderate")]
    rr = assess(vid, verdict(rule, qualifying=q[:1], decisive=items[:2]), items)
    for text in (rr.flip_condition, rr.inputs.e_reason, rr.inputs.k_reason, *rr.inputs.gap_reasons.values()):
        assert not CODES.search(text), text
    assert rr.inputs.e_reason and rr.inputs.k_reason


# --------------------------------------------------------------------------- exposure (E) by locus


@pytest.mark.parametrize("locus, rl, d, excerpt, data, providers, expected, if_confirmed", [
    ("service_feature", "R3", (4, "D4"), "AI scores every payment in the service.", (), (), 3, None),
    ("vendor_addon", "R3", (3, "D3-P"), "The add-on uses AI to tune job schedules.", (), (), 3, None),
    ("service_feature", "R3", (3, "D3"), "AI scores every payment in the service.", (), (), 2, None),
    ("service_feature", "R3", (2, "D2"), "AI scores every payment in the service.", (), (), 1, None),
    ("service_feature", "R3", (2, "D2"), "The service analyses messages with an external model.",
     ("payment messages",), ("OpenAI",), 3, None),
    ("service_feature", "R3", (2, "D2"), "The service analyses messages with its own model.",
     ("payment messages",), ("BNY Eliza",), 1, None),
    ("service_feature", "R2", (4, "D4"), "AI runs across the product family.", (), (), 2, 3),
    ("delivery_ops", "R3", (3, "D3"), "Support staff use AI to summarise customer data in each request.", (), (),
     3, None),
    ("delivery_ops", "R2", (3, "D3"), "Support staff use AI to summarise customer data in each request.", (), (),
     2, 3),
    ("delivery_ops", "R2", (3, "D3"), "Engineers use AI to triage support tickets.", (), (), 2, None),
    ("delivery_ops", "R2", (3, "D3"), "Engineers use AI writing assistants.", (), (), 1, None),
    ("sdlc", "R2", (4, "D4"), "Developers test AI-generated code against production data.", (), (), 2, None),
    ("sdlc", "R2", (4, "D4"), "Developers write code with AI assistants.", (), (), 1, None),
    ("corporate_internal", "R2", (4, "D4"), "Finance staff draft memos with an AI assistant.", (), (), 0, None),
])
def test_exposure_follows_the_locus_table(locus, rl, d, excerpt, data, providers, expected, if_confirmed):
    p, c, _ = vendor("V-005")
    c = with_factors(c, D=d)
    item = mk("V-005", excerpt, locus=locus, rl=rl, u_class="U3", sr="B", sp="S2", data=data, providers=providers,
              action="advisory")
    ri = derive("V-005", verdict("c", qualifying=[item]), [item], c=c, p=p)
    assert ri.e == expected and not ri.e_assumed
    assert ri.e_items == [item.item_key]
    assert ri.e_if_confirmed == if_confirmed


def test_exposure_is_the_maximum_over_q_and_k_pathways():
    weak = mk("V-005", "Engineers use AI writing assistants.", locus="delivery_ops", rl="R2", u_class="U3")
    strong = mk("V-005", "AI screens payment instructions in the service.", rl="R3")
    k_only = mk("V-005", "Analysts say the bank's AI touches customer data.", family=SourceFamily.IND, sr="C",
                rl="R2", locus="delivery_ops", u_class="U3")
    ri = derive("V-005", verdict("b", qualifying=[strong], corroborating=[k_only]), [weak, strong, k_only])
    assert ri.e == 2 and set(ri.e_items) == {strong.item_key, k_only.item_key}


def test_items_that_do_not_count_never_set_exposure():
    q = mk("V-002", "Engineers use AI writing assistants.", locus="delivery_ops", rl="R2", u_class="U3",
           action="advisory")
    rejected = mk("V-002", "AI decides every core banking posting.", review="rejected")
    trap = mk("V-002", "Rules engine labelled as AI posts every entry.", ai_type="not_ai")
    proposal = mk("V-002", "AI handles every account in the platform.").model_copy(
        update={"method": "llm_proposed_accepted"})
    marketing = mk("V-002", "The smartest AI in banking.", sp="S1", u_class="U7", strength="Marketing only")
    other_vendor = mk("V-005", "AI decides every payment in the service.")
    ri = derive("V-002", verdict("c", qualifying=[q]), [q, rejected, trap, proposal, marketing, other_vendor])
    assert ri.e == 1 and ri.e_items == [q.item_key] and ri.k == 1


# --------------------------------------------------------------------------- decision impact (K)


@pytest.mark.parametrize("action, excerpt, expected", [
    ("automated_action", "AI places holds on suspicious customer payments automatically.", 3),
    ("automated_action", "An AI agent closes duplicate internal tickets on its own.", 2),
    ("human_reviewed_decision", "AI drafts alerts that an analyst reviews.", 2),
    ("advisory", "AI suggests next steps to staff.", 1),
    ("none", "AI is mentioned in the product roadmap.", 0),
])
def test_decision_impact_follows_the_action_level(action, excerpt, expected):
    item = mk("V-005", excerpt, action=action)
    ri = derive("V-005", verdict("b", qualifying=[item]), [item])
    assert ri.k == expected and not ri.k_assumed and ri.k_items == [item.item_key]


def test_unknown_action_level_under_a_yes_verdict_is_assumed_from_the_role():
    item = mk("V-005", "AI-enabled anomaly detection screens the service.", action="unknown")
    ri = derive("V-005", verdict("b", qualifying=[item]), [item])  # BNY: payment path (P3) -> 3
    assert ri.k == 3 and ri.k_assumed and ri.k_items == [] and "role" in ri.k_reason
    advisory = mk("V-005", "AI suggests next steps to staff.", action="advisory")
    ri = derive("V-005", verdict("b", qualifying=[advisory]), [advisory])
    assert ri.k == 1 and not ri.k_assumed and ri.k_items == [advisory.item_key]
    # A stated action level on one pathway never lowers the assumption made for a pathway that states none.
    ri = derive("V-005", verdict("b", qualifying=[item, advisory]), [item, advisory])
    assert ri.k == 3 and ri.k_assumed and ri.k_items == []
    assert ri.k_reason == ("Assumed from the vendor's role (a role in the payment path) because some AI pathways in "
                           "the service or its delivery do not state what the AI's output does; the pathways that do "
                           "show decision impact 1 of 3.")
    reviewed = mk("V-005", "An AI agent places payment holds on its own.", action="automated_action")
    ri = derive("V-005", verdict("b", qualifying=[item, reviewed]), [item, reviewed])
    assert ri.k == 3 and not ri.k_assumed and ri.k_items == [reviewed.item_key]   # evidence reaches the maximum


def test_trade_press_advisory_on_another_process_does_not_lower_decision_impact():
    # BNY regression (V-005-E-0011): the only pathway with a stated action level was trade press on loan-disbursement
    # reconciliation ('AI assists in matching disbursements'); it set an evidenced K1 for a payment-path vendor.
    filings = [mk("V-005", f"Enterprise AI solutions run in operations, part {n}.", family=SourceFamily.REG,
                  source_type="SEC Form 8-K exhibit", sr="A", rl="R2", u_class="U3", locus="delivery_ops",
                  strength="Moderate") for n in (1, 2)]
    press = mk("V-005", "Within liquidity and reconciliation processes, AI assists in matching disbursements.",
               family=SourceFamily.IND, source_type="Trade press", publisher="Trade Weekly", sr="C", rl="R2",
               u_class="U3", locus="delivery_ops", action="advisory", strength="Moderate")
    v = verdict("c", qualifying=filings, corroborating=[press])
    without = derive("V-005", v, filings)
    with_press = derive("V-005", v, [*filings, press])
    assert (without.k, without.k_assumed) == (3, True)
    assert (with_press.k, with_press.k_assumed) == (3, True)


def test_development_and_corporate_pathways_are_not_assumed_to_act_on_the_service():
    # TCH (design 2.8 row 2.1.3.2): a NOC posting states advisory use; AI-assisted development and a corporate tool
    # with no stated action level are not assumed to act on customers, funds or production.
    noc = mk("V-006", "Use AI-powered assistants during incident response.", family=SourceFamily.JOB,
             source_type="Job posting", rl="R2", u_class="U3", locus="delivery_ops", action="advisory")
    dev = mk("V-006", "Engineers build with AI-assisted development tools.", family=SourceFamily.JOB,
             source_type="Job posting", rl="R2", u_class="U3", locus="sdlc")
    corp = mk("V-006", "Finance staff draft memos with an AI assistant.", rl="R2", u_class="U3",
              locus="corporate_internal")
    ri = derive("V-006", verdict("c", qualifying=[noc, dev, corp]), [noc, dev, corp])
    assert (ri.k, ri.k_assumed, ri.k_items) == (1, False, [noc.item_key])
    unknown_place = mk("V-006", "Analysts report the vendor's AI in operations.", family=SourceFamily.IND, sr="C",
                       rl="R2", u_class="U3", locus="unknown", publisher="Analyst")
    ri = derive("V-006", verdict("c", qualifying=[noc, dev], corroborating=[unknown_place]),
                [noc, dev, unknown_place])
    assert (ri.k, ri.k_assumed) == (3, True)   # an unknown place may be the service itself


_ACTIONS = ("unknown", "none", "advisory", "human_reviewed_decision", "automated_action")


@pytest.mark.parametrize("vid", ["V-002", "V-004", "V-005", "V-006"])
def test_adding_an_item_never_lowers_decision_impact(vid):
    """Monotonicity: over pathways in the service, its delivery or an unknown place (any action level, the unknown
    one included) and development or corporate pathways with a stated action level, adding any item to any
    non-empty set never lowers K."""
    pool = [mk(vid, f"AI {locus} pathway acting as {action} on customer payments.", locus=locus, action=action,
               rl="R2", u_class="U3", sr="B")
            for locus in ("service_feature", "delivery_ops", "unknown") for action in _ACTIONS]
    pool += [mk(vid, f"AI {locus} pathway acting as {action} on production.", locus=locus, action=action, rl="R2",
                u_class="U3", sr="B")
             for locus in ("sdlc", "corporate_internal") for action in _ACTIONS[1:]]
    rng = random.Random(1234)
    for _ in range(150):
        base = rng.sample(pool, rng.randint(1, 4))
        extra = rng.choice([i for i in pool if i not in base])
        before = derive(vid, verdict("c", qualifying=base), base)
        after = derive(vid, verdict("c", qualifying=[*base, extra]), [*base, extra])
        assert after.k >= before.k, ([i.excerpt for i in base], extra.excerpt)


def test_unknown_exposure_under_a_yes_verdict_is_assumed_from_data_sensitivity():
    a = mk("V-005", "Analysts report the vendor's AI in industry rankings.", family=SourceFamily.IND, sr="C",
           rl="R2", locus="unknown", u_class="U4")
    b = mk("V-005", "A partner page lists the vendor's AI deployment.", family=SourceFamily.IND, sr="C", rl="R2",
           locus="unknown", u_class="U4", publisher="Partner")
    ri = derive("V-005", verdict("c", corroborating=[a, b]), [a, b])
    assert ri.e == 2 and ri.e_assumed and ri.e_items == []  # BNY D3 -> 2


# --------------------------------------------------------------------------- unknowns rule


@pytest.mark.parametrize("vid, e, k", [
    ("V-001", 3, 3),   # D3-P -> E3; D3-P -> K3
    ("V-002", 3, 3),   # D4 -> E3; P4 -> K3
    ("V-003", 3, 2),   # D4 -> E3; statements and notices are customer-facing outputs -> K2
    ("V-004", 2, 2),   # D3 -> E2; commission calculations (P2) are pay outputs -> K2
    ("V-005", 2, 3),   # D3 -> E2; P3 -> K3
    ("V-006", 2, 3),   # D3 -> E2; P4 -> K3
])
def test_unknowns_rule_for_inconclusive_verdicts(vid, e, k):
    ri = derive(vid, verdict("f"), [])
    assert (ri.e, ri.k) == (e, k) and ri.e_assumed and ri.k_assumed
    assert "Assumed" in ri.e_reason and "Assumed" in ri.k_reason


def test_unknowns_rule_floor_for_a_supporting_role():
    p, c, _ = vendor("V-004")
    c = with_factors(c, D=(2, "D2"), P=(0, "P0"))
    p = p.model_copy(update={"service": "Hosts an internal data warehouse.", "business_process": "Reporting",
                             "category": "Data and Reporting"})
    ri = derive("V-004", verdict("f"), [], c=c, p=p)
    assert (ri.e, ri.k) == (1, 1) and ri.e_assumed and ri.k_assumed


# --------------------------------------------------------------------------- transparency gaps (TG)


def test_each_check_closes_only_with_citable_evidence():
    closers = [
        mk("V-005", "AI screens every instant payment in the service.", sr="B", rl="R3"),                       # t1
        mk("V-005", "Our assistant runs on models from Anthropic.", rl="R2", providers=["Anthropic"]),          # t2
        mk("V-005", "Customer data is never used to train AI models.", family=SourceFamily.LEG,
           source_type="Privacy notice", rl="R2", u_class="U8", strength="Negative", indicators=("G3",)),      # t3
        mk("V-005", "Each AI alert is approved by an analyst.", rl="R2"),                                       # t4
        mk("V-005", "We hold ISO/IEC 42001 certification.", rl="R1", u_class="U6", strength="Weak"),          # t5
        mk("V-005", "We will notify clients of material changes to AI features.", rl="R2"),                    # t6
    ]
    ri = derive("V-005", verdict("b", qualifying=closers[:1]), closers)
    assert ri.missing_gaps == [] and ri.tg == 0 and ri.gap_reasons == {}
    assert ri.gap_items["t1"] == [closers[0].item_key] and ri.gap_items["t6"] == [closers[5].item_key]
    rejected = [i.model_copy(update={"review_status": "rejected"}) for i in closers]
    ri = derive("V-005", verdict("b"), rejected)
    assert ri.missing_gaps == list(risk.GAP_KEYS) and ri.tg == 3
    assert set(ri.gap_reasons.values()) == {"not publicly disclosed; contractual disclosure unknown"}


def test_gap_checks_ignore_dns_tokens_third_parties_and_platform_suppliers():
    items = [
        mk("V-004", "openai-domain-verification=dv-test", family=SourceFamily.DNS, source_type="DNS TXT record",
           u_class="U4", sp="S1", rl="R2", locus="relationship", providers=["OpenAI"],
           strength="Context - relationship only"),
        mk("V-004", "The partner names the vendor as an OpenAI customer.", family=SourceFamily.IND, sr="C",
           rl="R2", providers=["OpenAI"], u_class="U4", locus="relationship", publisher="Partner"),
        mk("V-004", "Prompts are never retained by the default model.", family=SourceFamily.LEG,
           locus="platform_supplier", rl="R1", indicators=("G3",), providers=["Gemini"],
           strength="Context - platform supplier"),
        mk("V-004", "We do not use OpenAI in client work.", rl="R2", u_class="U8", providers=["OpenAI"],
           strength="Negative"),
        mk("V-004", "AI may support reporting in the product family.", sr="B", rl="R2", sp="S1"),  # R2: not t1
        mk("V-004", "Trade press says AI runs in the exact service.", family=SourceFamily.IND, sr="C", rl="R3"),
    ]
    ri = derive("V-004", verdict("f"), items)
    assert ri.missing_gaps == list(risk.GAP_KEYS)


def test_checks_three_to_six_need_a_disclosure_not_marketing_or_a_job_posting():
    items = [
        mk("V-005", "Operate AI systems with appropriate human oversight of each decision.", family=SourceFamily.LEG,
           source_type="AI policy page", rl="R1", u_class="U6", strength="Weak", indicators=("G3",)),     # t4, t5
        mk("V-005", "Human led, AI supercharged.", rl="R2", sp="S1", u_class="U7", strength="Marketing only",
           action="human_reviewed_decision"),
        mk("V-005", "Align the team with our AI policy and notify clients of incidents; staff review AI output.",
           family=SourceFamily.JOB, source_type="Job posting", rl="R2", u_class="U6", strength="Weak"),
    ]
    ri = derive("V-005", verdict("f"), items)
    assert ri.missing_gaps == ["t1", "t2", "t3", "t6"]       # G3 on human review only is not a data-use term
    assert ri.gap_items == {"t4": [items[0].item_key], "t5": [items[0].item_key]}
    terms = mk("V-005", "Client inputs are not retained after the session ends.", family=SourceFamily.LEG,
               source_type="AI terms", rl="R1", u_class="U8", strength="Negative", indicators=("G3",))
    assert "t3" not in derive("V-005", verdict("f"), [*items, terms]).missing_gaps


# Regression cases quoted from the frozen evidence pack (public SEC filings and a public podcast page), as the review
# found them closing checks. Neither third-party pages nor descriptions of a law or a risk are vendor disclosures.
PODCAST_CHAPTERS = ("- Oliver Freigang, CEO of qashqade — The 6 x 6lock Podcast\nChapters:\n0:00 Cold Open: The "
                    "Curling Reveal\n0:42 Intro\n1:48 Kristefor's Curling Life\n10:18 Terrapin Technologies: The "
                    "60-Second Pitch\n12:57 Hidden Data Problems at Wealth Management Firms\n16:54 The Brittle "
                    "Spreadsheet Trap\n20:42 AI, Vibe Coding & Why Governance Matters\n26:26 Garbage In, Garbage Out: "
                    "AI Doesn't Fix Messy Data\n28:14 How Terrapin Normalizes & Cleans Your Data\n31:38 Who Is the "
                    "Right Firm for Terrapin?")
AI_ACT_10K = ("The AI Act requires categorization of artificial intelligence systems into four risk levels "
              "depending on their potential to harm individuals or society (unacceptable risk, high risk, limited "
              "risk, and minimal risk) and imposes obligations depending on the risk categorization, which may "
              "include data governance, documentation and recordkeeping, human oversight, testing, cybersecurity, "
              "disclosure, regulatory notification or reporting, or training.")
AI_ACT_PUBLISHED = ("EU Artificial Intelligence Act\nOn July 12, 2024, the EU’s Regulation on Artificial "
                    "Intelligence (“AI Act”) was published in the Official Journal of the EU and came into force on "
                    "Aug. 1, 2024.")
FLS_DISCLAIMER = ("Statements about our artificial intelligence initiatives, including the timing, implementation, "
                  "efficacy and expected benefits, are subject to various factors, including third-party and vendor "
                  "dependencies, the availability, usability and quality of data, evolving legal, regulatory and "
                  "supervisory expectations, employee and client adoption, and our ability to deploy, monitor and "
                  "scale such capabilities with appropriate governance and in an effective control environment.")


def test_a_third_party_podcast_page_closes_no_check():
    pod = mk("V-004", PODCAST_CHAPTERS, family=SourceFamily.IND, source_type="Podcast page",
             publisher="Modern Financial Advisor podcast (pod.co)", u_class="U6", sr="C", sp="S1", rl="R2",
             locus="sdlc", strength="Weak")
    ri = derive("V-004", verdict("f"), [pod])
    assert ri.missing_gaps == list(risk.GAP_KEYS) and ri.tg == 3 and ri.gap_items == {}
    rr = assess("V-004", verdict("f", decisive=[pod]), [pod])     # Terrapin: 2a.2a.2.3 = 13 (design 2.8)
    assert rr.arp == 13 and rr.final_class == "Medium" and rr.ceiling_class == "High"


@pytest.mark.parametrize("excerpt, u_class, action", [
    (AI_ACT_10K, "U3", "human_reviewed_decision"),     # closed t4 (human oversight)
    (AI_ACT_PUBLISHED, "U6", "unknown"),               # closed t5 as a U6 item
    (FLS_DISCLAIMER, "U6", "unknown"),                 # closed t5 as a U6 item
])
def test_regulatory_and_forward_looking_boilerplate_closes_no_check(excerpt, u_class, action):
    item = mk("V-005", excerpt, family=SourceFamily.REG, source_type="SEC Form 10-K", publisher="BNY", sr="A",
              sp="S1", rl="R2", u_class=u_class, locus="corporate_internal", action=action, strength="Weak")
    ri = derive("V-005", verdict("f"), [item])
    assert ri.missing_gaps == list(risk.GAP_KEYS), ri.gap_items


def test_governance_needs_an_attestation_not_a_governance_tag():
    vague = mk("V-002", "What we are building is a safe, governed way to scaled AI adoption.", sr="A", rl="R1",
               family=SourceFamily.REG, source_type="SEC Form DEFA14A", u_class="U6", strength="Weak")
    assert "t5" in derive("V-002", verdict("f"), [vague]).missing_gaps
    for attestation in (mk("V-002", "Our generative AI policy aligns with the NIST AI RMF.", rl="R1", u_class="U6",
                           source_type="Corporate report", strength="Weak"),
                        mk("V-002", "Staff may use approved tools only with client consent.", rl="R1", u_class="U6",
                           family=SourceFamily.LEG, source_type="AI policy", strength="Weak")):
        assert "t5" not in derive("V-002", verdict("f"), [attestation]).missing_gaps


def test_first_party_disclosures_still_close_checks_outside_legal_pages():
    # The vendor's own whitepaper (SR C, design 5: BNY's human-in-the-loop paper gives t4) closes t4; the same
    # sentence carried by trade press or an aggregator copy does not.
    text = "AI reviews sanctions screening alerts with a human in the loop on every decision."
    own = mk("V-005", text, source_type="Whitepaper", publisher="BNY", sr="C", rl="R2", u_class="U1",
             strength="Moderate")
    press = own.model_copy(update={"family": SourceFamily.IND, "source_type": "Trade press"})
    copy = own.model_copy(update={"tags": own.tags.model_copy(update={"sr": "D"})})
    assert "t4" not in derive("V-005", verdict("f"), [own]).missing_gaps
    assert "t4" in derive("V-005", verdict("f"), [press]).missing_gaps
    assert "t4" in derive("V-005", verdict("f"), [copy]).missing_gaps


# --------------------------------------------------------------------------- overrides


def _record(store: OverrideStore, vid: str, key: str, value: str, *, evidence: list[str] | None = None,
            analyst: str = "RK", date: str = "2026-10-08", reason: str = "confirmed on the vendor call notes") -> None:
    store.add(ReviewRecord(kind="risk_input_override", vendor_id=vid, key=key, value=value, reason=reason,
                           analyst=analyst, date=date, extra={"evidence": evidence} if evidence else {}))


def test_overrides_replace_values_and_reasons(tmp_path):
    items = terrapin_items()
    store = OverrideStore(tmp_path / "overrides.jsonl")
    _record(store, "V-004", "e", "1", reason="first attempt at the exposure value")
    _record(store, "V-004", "e", "3", evidence=[items[0].item_key])           # last record wins
    _record(store, "V-004", "k", "1")                                          # no evidence: still assumed
    _record(store, "V-004", "t2", "closed", evidence=[items[0].item_key, "f" * 64])
    _record(store, "V-004", "t5", "closed")
    _record(store, "V-005", "e", "0")                                          # another vendor
    ri = derive("V-004", verdict("f"), items, overrides=store)
    assert ri.e == 3 and not ri.e_assumed and ri.e_items == [items[0].item_key] and ri.e_if_confirmed is None
    assert ri.e_reason == "analyst RK 2026-10-08: confirmed on the vendor call notes"
    assert ri.k == 1 and ri.k_assumed and ri.k_items == []
    assert ri.gaps["t2"] is False and ri.gap_items["t2"] == [items[0].item_key]  # unknown key dropped
    assert ri.gaps["t5"] is False and "t5" not in ri.gap_items
    assert ri.tg == 2 and ri.gap_reasons["t2"].startswith("analyst RK 2026-10-08: ")
    _record(store, "V-004", "t2", "missing")
    assert derive("V-004", verdict("f"), items, overrides=store).gaps["t2"] is True


@pytest.mark.parametrize("key, value", [("e", "4"), ("k", "high"), ("t1", "open"), ("tp", "1")])
def test_malformed_overrides_are_refused(tmp_path, key, value):
    store = OverrideStore(tmp_path / "overrides.jsonl")
    _record(store, "V-004", key, value)
    if key == "tp":  # not an overridable input: ignored
        assert derive("V-004", verdict("f"), [], overrides=store).tp == 2
        return
    with pytest.raises(ValueError):
        derive("V-004", verdict("f"), [], overrides=store)


def test_derive_inputs_refuses_mismatched_vendors():
    p, c, plan = vendor("V-004")
    _, c5, _ = vendor("V-005")
    with pytest.raises(ValueError):
        risk.derive_inputs(verdict("f"), [], c5, p, plan)


# --------------------------------------------------------------------------- escalators


def _escalators(vid, v, items, ri, *, c=None):
    p, crit, _ = vendor(vid)
    return risk.evaluate_escalators(v, items, ri, c or crit, as_of=AS_OF, profile=p)


def test_x1_provider_named_only_by_a_third_party():
    q = mk("V-005", "AI screens payment instructions in the service.", providers=["OpenAI"])
    press = mk("V-005", "The vendor's screening runs on Claude, the provider says.", family=SourceFamily.IND,
               sr="C", rl="R2", publisher="Provider blog", providers=["Claude"])
    v = verdict("b", qualifying=[q])
    assert _escalators("V-005", v, [q, press], inputs(2, 2, 3))["X1"] is True
    assert _escalators("V-005", v, [q, press], inputs(1, 2, 3))["X1"] is False
    assert _escalators("V-005", v, [q, press], inputs(3, 2, 3, e_assumed=True))["X1"] is False
    named = q.model_copy(update={"providers": ["OpenAI", "Anthropic"]})
    assert _escalators("V-005", v, [named, press], inputs(2, 2, 3))["X1"] is False
    old = press.model_copy(update={"tags": press.tags.model_copy(update={"rc": "T0"})})
    assert _escalators("V-005", v, [q, old], inputs(2, 2, 3))["X1"] is False   # T0: historical context only


def test_x1_counts_the_maker_and_the_vendors_own_releases_as_vendor_named():
    # BNY regression: a joint release on the provider's press site names 'Gemini Enterprise', and BNY's own 8-K
    # says it integrates models from OpenAI, Google and Anthropic (its provider list missed Google).
    q = mk("V-005", "AI screens payment instructions in the service.", providers=["OpenAI"])
    joint = mk("V-005", "BNY Collaborates with Google Cloud to Advance its Eliza AI Platform with Gemini Enterprise",
               family=SourceFamily.IND, source_type="Provider customer story", publisher="Google Cloud", sr="C",
               sp="S1", rl="R2", u_class="U4", locus="relationship", providers=["Gemini Enterprise"],
               strength="Context - relationship only")
    joint = joint.model_copy(update={"title": joint.excerpt})
    story = joint.model_copy(update={"title": "How a global bank scales its AI platform with Gemini Enterprise"})
    deck = mk("V-005", "Launched Eliza, BNY's AI platform; integrates models from e.g., OpenAI, Google, Anthropic.",
              family=SourceFamily.REG, source_type="SEC Form 8-K exhibit", sr="A", rl="R1", u_class="U2",
              providers=["OpenAI", "Anthropic"], strength="Weak")
    v = verdict("b", qualifying=[q])
    assert _escalators("V-005", v, [q, story], inputs(2, 2, 3))["X1"] is True    # Google named only by Google
    assert _escalators("V-005", v, [q, joint], inputs(2, 2, 3))["X1"] is False   # BNY's own announcement
    assert _escalators("V-005", v, [q, story, deck], inputs(2, 2, 3))["X1"] is False   # BNY names Google itself
    # Fiserv: a Mondo Visione mirror of Fiserv's own release is the vendor's voice, not a third party's.
    pilot = mk("V-002", "Agents resolve tickets with AI.", locus="delivery_ops", rl="R2", u_class="U3")
    mirror = mk("V-002", "GitHub Copilot has been deployed to more than 8,000 software engineers across Fiserv.",
                family=SourceFamily.IND, source_type="Press release mirror", publisher="Mondo Visione", sr="C",
                rl="R2", u_class="U3", locus="sdlc", providers=["GitHub Copilot"], strength="Moderate")
    mirror = mirror.model_copy(update={"title": "Fiserv Collaborates With Microsoft To Accelerate AI-Driven "
                                                "Innovation"})
    assert _escalators("V-002", verdict("c", qualifying=[pilot]), [pilot, mirror], inputs(2, 2, 3))["X1"] is False
    possessive = mirror.model_copy(update={"title": "Fiserv's AI push draws on Microsoft, analysts say"})
    assert _escalators("V-002", verdict("c", qualifying=[pilot]), [pilot, possessive], inputs(2, 2, 3))["X1"] is True


def test_x2_training_without_an_opt_out():
    trains = mk("V-005", "Client prompts are used to train our AI models.")
    opt_out = mk("V-005", "Client prompts are used to train our AI models unless the client opts out.")
    never = mk("V-005", "Client prompts are never used to train AI models.")
    v = verdict("b", qualifying=[trains])
    assert _escalators("V-005", v, [trains], inputs(2, 1, 3))["X2"] is True
    assert _escalators("V-005", v, [opt_out], inputs(2, 1, 3))["X2"] is False
    assert _escalators("V-005", v, [never], inputs(2, 1, 3))["X2"] is False
    assert _escalators("V-005", v, [trains], inputs(1, 1, 3))["X2"] is False
    for text in ("We may use your content to improve our models.", "Prompts are retained for 30 days.",
                 "We train our models on customer data."):
        assert _escalators("V-005", v, [mk("V-005", text)], inputs(2, 1, 3))["X2"] is True, text
    generic = [mk("V-005", "Engineers scale GPU infrastructure supporting model training and inference."),
               mk("V-005", "The inference server puts trained AI models to work."),
               mk("V-005", "The model was trained on public data."),
               mk("V-005", "Client data must never be used for model training without consent, an analyst writes.",
                  family=SourceFamily.IND, sr="C", locus="commentary"),
               mk("V-005", "The default model retains prompts for 30 days.", family=SourceFamily.LEG,
                  locus="platform_supplier", strength="Context - platform supplier")]
    assert _escalators("V-005", v, generic, inputs(2, 1, 3))["X2"] is False


def test_x3_agentic_ai_acting_on_production_without_approval():
    agent = mk("V-001", "Our AI agent applies configuration changes to production on its own.", ai_type="agentic",
               action="automated_action", locus="delivery_ops", rl="R2", u_class="U3")
    advisory = agent.model_copy(update={"action_level": "advisory"})
    v = verdict("c", qualifying=[agent])
    assert _escalators("V-001", v, [agent], inputs(2, 2, 2))["X3"] is True
    assert _escalators("V-001", v, [advisory], inputs(2, 2, 2))["X3"] is False


def test_x4_k3_on_credit_account_access_or_payment_holds():
    hold = mk("V-005", "The model places payment holds on flagged customer transfers automatically.",
              action="automated_action")
    v = verdict("b", qualifying=[hold])
    assert _escalators("V-005", v, [hold], inputs(2, 3, 3))["X4"] is True
    assert _escalators("V-005", v, [hold], inputs(2, 2, 3))["X4"] is False
    routing = mk("V-005", "The model routes customer payments to the fastest rail automatically.",
                 action="automated_action")
    assert _escalators("V-005", verdict("b", qualifying=[routing]), [routing], inputs(2, 3, 3))["X4"] is False


@pytest.mark.parametrize("published, excerpt, expected", [
    ("2025-06-01", "BNY disclosed an AI incident that exposed client prompts.", True),
    ("2024-08-01", "BNY disclosed an AI incident that exposed client prompts.", False),            # 26 months
    ("2026-12-01", "BNY disclosed an AI incident that exposed client prompts.", True),             # after as_of
    ("2026-05-01", "An AI data breach at BNY could harm its business if it occurred.", False),     # hypothetical
    ("2026-05-01", "In May BNY reported a security incident in its AI chatbot.", True),
    ("2026-05-01", "BNY disclosed a data breach at a branch office.", False),                      # not AI
    ("2026-05-01", "BNY expanded its AI platform. It also disclosed a data breach at a branch.", False),  # apart
    ("2026-05-01", "Globex disclosed an AI incident that exposed client prompts.", False),         # not the vendor
])
def test_x5_recent_ai_incident(published, excerpt, expected):
    item = mk("V-005", excerpt, published=published, family=SourceFamily.IND, sr="C", rl="R2")
    assert _escalators("V-005", verdict("b"), [item], inputs(2, 2, 3))["X5"] is expected


def test_x5_ignores_commentary_about_another_company():
    # Review regression: one trade-press commentary item about another firm's fine raised a Medium vendor to High.
    globex = mk("V-005", "Globex Corp was fined $5 million after its AI chatbot exposed customer records.",
                family=SourceFamily.IND, source_type="Trade press", publisher="Trade Weekly", published="2026-06-01",
                u_class="U7", sr="C", sp="S0", rl="R0", locus="commentary", strength="Marketing only",
                indicators=("M3",))
    assert _escalators("V-005", verdict("b"), [globex], inputs(2, 2, 3))["X5"] is False
    retagged = globex.model_copy(update={"tags": globex.tags.model_copy(update={"locus": "service_feature"})})
    assert _escalators("V-005", verdict("b"), [retagged], inputs(2, 2, 3))["X5"] is False    # M3: another company
    unnamed = retagged.model_copy(update={"indicators": []})
    assert _escalators("V-005", verdict("b"), [unnamed], inputs(2, 2, 3))["X5"] is False     # BNY is not named
    own = mk("V-005", "We identified an unauthorized access incident in our AI chatbot in March.",
             family=SourceFamily.REG, source_type="SEC Form 8-K", sr="A", published="2026-04-01")
    assert _escalators("V-005", verdict("b"), [own], inputs(2, 2, 3))["X5"] is True          # its own filing
    q = mk("V-005", "AI scores every customer survey in the service.")
    p, c, plan = vendor("V-005")
    rr = risk.assess(verdict("b", qualifying=[q]), [q, globex], c, p, plan, as_of=AS_OF)
    assert rr.escalators_fired == [] and rr.escalators_logged_not_applied == []


def test_x6_single_foundation_model_provider_behind_a_critical_vendor():
    one = mk("V-005", "The service screens payments with GPT-4 from OpenAI.", providers=["OpenAI", "GPT-4"])
    two = one.model_copy(update={"providers": ["OpenAI", "Claude"]})
    v = verdict("b", qualifying=[one])
    assert _escalators("V-005", v, [one], inputs(2, 2, 3))["X6"] is True        # BNY tier Critical
    assert _escalators("V-005", v, [two], inputs(2, 2, 3))["X6"] is False
    _, c4, _ = vendor("V-004")
    high = one.model_copy(update={"vendor_id": "V-004"})
    assert _escalators("V-004", verdict("b", qualifying=[high]), [high], inputs(2, 2, 2), c=c4)["X6"] is False
    dns_only = mk("V-005", "openai-domain-verification=x", family=SourceFamily.DNS, sp="S1", rl="R2",
                  locus="relationship", u_class="U4", providers=["OpenAI"], strength="Context - relationship only")
    assert _escalators("V-005", v, [dns_only], inputs(2, 2, 3))["X6"] is False  # not a Q or K pathway


def test_x6_counts_the_providers_the_vendor_names_outside_the_pathways():
    # Fiserv regression: its only pathway provider is GitHub Copilot (Microsoft), but its own DEFA14A names OpenAI.
    copilot = mk("V-002", "Engineers write and test code with GitHub Copilot.", locus="sdlc", rl="R2", u_class="U3",
                 providers=["GitHub Copilot"], strength="Moderate")
    openai = mk("V-002", "We announced a strategic collaboration with OpenAI this morning.", family=SourceFamily.REG,
                source_type="SEC Form DEFA14A", sr="A", sp="S1", rl="R2", u_class="U4", locus="relationship",
                providers=["OpenAI"], strength="Context - relationship only")
    v = verdict("c", qualifying=[copilot])
    assert _escalators("V-002", v, [copilot], inputs(1, 3, 3))["X6"] is True
    assert _escalators("V-002", v, [copilot, openai], inputs(1, 3, 3))["X6"] is False
    rr = assess("V-002", v, [copilot, openai])
    assert rr.escalators_fired == [] and rr.escalators_logged_not_applied == []


def test_vendor_name_spellings():
    names = {vid: risk._name_keys(vendor(vid)[0]) for vid in ("V-002", "V-003", "V-005", "V-006")}
    assert "fiserv" in names["V-002"]
    assert {"fssi", "financialstatementservices"} <= set(names["V-003"])
    assert "bny" in names["V-005"]
    assert "theclearinghousepayments" in names["V-006"]
    p6 = vendor("V-006")[0]
    assert risk._opens_with_vendor("The Clearing House Payments announces an AI pilot", p6)
    p5 = vendor("V-005")[0]
    assert risk._opens_with_vendor("“BNY Collaborates with Google Cloud”", p5)
    assert not risk._opens_with_vendor("BNY’s AI push, explained", p5)
    assert not risk._opens_with_vendor("BNYX launches a fund", p5)
    assert not risk._opens_with_vendor("Banks and BNY", p5)


def test_escalators_return_all_six_codes():
    out = _escalators("V-005", verdict("b"), [], inputs(2, 2, 3))
    assert list(out) == list(risk.ESCALATOR_CODES) and not any(out.values())


# --------------------------------------------------------------------------- the fixed order of steps


def test_gate_lowers_high_or_critical_without_evidenced_inputs():
    rr = risk.score(inputs(3, 3, 3, missing=6, e_assumed=True, k_assumed=True), verdict("b"), tier=Tier.CRITICAL)
    assert rr.base_class == "Critical" and not rr.gate_met and rr.final_class == "Medium"
    rr = risk.score(inputs(3, 1, 3, missing=6, e_assumed=True), verdict("b"), tier=Tier.CRITICAL)
    assert not rr.gate_met and rr.final_class == "Medium"     # evidenced K1 is below the gate
    rr = risk.score(inputs(3, 2, 3, missing=6, e_assumed=True), verdict("b"), tier=Tier.CRITICAL)
    assert rr.gate_met and rr.final_class == "Critical"       # evidenced K2 meets it
    rr = risk.score(inputs(1, 1, 1, missing=0), verdict("b"), tier=Tier.MEDIUM)
    assert not rr.gate_met and rr.arp == 5 and rr.base_class == rr.final_class == "Low"


def test_escalator_floors_apply_only_to_yes_verdicts_with_the_gate_met():
    esc = {"X5": True}
    rr = risk.score(inputs(2, 1, 1, missing=0), verdict("b"), tier=Tier.MEDIUM, escalators=esc)
    assert rr.arp == 7 and rr.base_class == "Medium" and rr.gate_met
    assert rr.escalators_fired == ["X5"] and rr.pre_cap_class == "High" and rr.final_class == "High"
    rr = risk.score(inputs(2, 1, 1, missing=0), verdict("c"), tier=Tier.MEDIUM, escalators=esc)
    assert rr.escalators_fired == ["X5"] and rr.final_class == "High"          # Probable cap is High
    rr = risk.score(inputs(2, 1, 1, missing=0, e_assumed=True), verdict("b"), tier=Tier.MEDIUM, escalators=esc)
    assert rr.escalators_logged_not_applied == ["X5"] and rr.final_class == "Medium"   # gate not met
    rr = risk.score(inputs(2, 1, 1, missing=0), verdict("f"), tier=Tier.MEDIUM, escalators=esc)
    assert rr.escalators_logged_not_applied == ["X5"] and rr.escalators_fired == []
    assert rr.final_class == "Medium" and rr.provisional
    rr = risk.score(inputs(0, 0, 1, missing=0), verdict("e"), tier=Tier.MEDIUM, escalators=esc)
    assert rr.escalators_logged_not_applied == ["X5"] and rr.final_class == "None identified"


def test_the_verdict_cap_comes_last():
    rr = risk.score(inputs(3, 3, 3, missing=6), verdict("c"), tier=Tier.CRITICAL, escalators={"X4": True})
    assert rr.base_class == "Critical" and rr.pre_cap_class == "Critical" and rr.escalators_fired == ["X4"]
    assert rr.cap == "High" and rr.final_class == "High" and rr.ceiling_class == "Critical"
    assert "Probable cap" in rr.flip_condition and rr.flip_condition.endswith("Critical.")
    rr = risk.score(inputs(3, 3, 3, missing=6), verdict("a"), tier=Tier.CRITICAL)
    assert rr.cap == "Medium" and rr.final_class == "Medium" and rr.provisional
    rr = risk.score(inputs(0, 0, 3, missing=6), verdict("d"), tier=Tier.CRITICAL)
    assert rr.final_class == "None identified" and rr.ceiling_class == ""


def test_inconclusive_low_scores_stay_low_and_provisional():
    rr = risk.score(inputs(1, 1, 0, missing=1, e_assumed=True, k_assumed=True), verdict("f"), tier=Tier.LOW)
    assert rr.arp == 4 and rr.final_class == "Low" and rr.provisional and rr.ceiling_class == ""
    assert rr.flip_condition == ""


def test_steps_log_the_fixed_order():
    rr = risk.score(inputs(2, 2, 3, missing=3), verdict("b"), tier=Tier.CRITICAL)
    heads = [s.split(":")[0] for s in rr.steps]
    assert heads == ["1. Score", "2. Base class", "3. Materiality gate", "4. Escalators", "5. Verdict cap",
                     "6. Ceiling if confirmed", "7. Flip condition"]
    assert "12 of 18" in rr.steps[0] and "High" in rr.steps[1]


def test_steps_read_as_plain_sentences():
    rr = risk.score(inputs(2, 1, 1, missing=0), verdict("b"), tier=Tier.MEDIUM, escalators={"X5": True})
    assert rr.steps[3] == "4. Escalators: X5 (recent AI incident) sets a High floor, so Medium rises to High."
    rr = risk.score(inputs(3, 3, 3, missing=6), verdict("c"), tier=Tier.CRITICAL)
    assert rr.steps[4] == "5. Verdict cap: Probable caps the class at High, so Critical becomes High."
    rr = risk.score(inputs(3, 3, 2, missing=6, e_assumed=True, k_assumed=True), verdict("f"), tier=Tier.HIGH)
    assert rr.steps[2].startswith("3. Materiality gate: not met (")
    assert rr.steps[2].endswith("so Critical is lowered to Medium.")
    assert rr.steps[4] == ("5. Verdict cap: Inconclusive caps the class at Medium and marks it Provisional; Medium is "
                           "within the cap.")
    assert rr.steps[5] == ("6. Ceiling if confirmed: Critical (assumed inputs treated as evidenced: exposure 3, "
                           "decision impact 3; 17 of 18).")
    rr = risk.score(inputs(2, 1, 1, missing=0), verdict("f"), tier=Tier.MEDIUM, escalators={"X1": True, "X5": True})
    assert rr.steps[3] == ("4. Escalators: X1 (provider named only by a third party) and X5 (recent AI incident) are "
                           "logged, not applied, because the verdict is Inconclusive.")


def test_score_refuses_inconsistent_calls():
    with pytest.raises(ValueError, match="tier"):
        risk.score(inputs(2, 2, 3), verdict("b"), tier=Tier.HIGH)
    with pytest.raises(ValueError, match="escalator"):
        risk.score(inputs(2, 2, 3), verdict("b"), tier=Tier.CRITICAL, escalators={"X9": True})


# --------------------------------------------------------------------------- ceiling and flip


def test_ceiling_uses_the_if_confirmed_exposure_of_an_r2_item():
    p, c, plan = vendor("V-002")
    family = mk("V-002", "AI runs across the core banking product family.", rl="R2", sr="A",
                family=SourceFamily.REG, action="human_reviewed_decision")
    v = verdict("c", qualifying=[family])
    rr = risk.assess(v, [family], c, p, plan, as_of=AS_OF)
    assert rr.inputs.e == 2 and rr.inputs.e_if_confirmed == 3
    assert rr.final_class == "High" and rr.ceiling_class == "Critical"
    assert "Confirmed" in rr.flip_condition


def test_ceiling_counts_only_evidenced_escalators():
    ri = inputs(2, 0, 1, missing=0, e_assumed=True, k_assumed=True)
    rr = risk.score(ri, verdict("f"), tier=Tier.MEDIUM, escalators={"X3": True})
    assert rr.arp == 5 and rr.escalators_logged_not_applied == ["X3"] and rr.final_class == "Low"
    assert rr.ceiling_class == "High"            # if confirmed, the gate is met and the escalator's floor applies
    assert risk.score(ri, verdict("f"), tier=Tier.MEDIUM).ceiling_class == ""


def test_flip_for_a_confirmed_vendor_already_at_critical_points_down():
    rr = risk.calibrate()
    assert rr.flip_condition.startswith("Evidence that the AI is isolated") and rr.flip_condition.endswith("High.")


def test_flip_for_a_probable_vendor_at_its_cap_names_the_confirmation():
    items = bny_items_with_stated_actions()
    rtp, _, paper, _, _ = items
    rr = assess("V-005", verdict("c", qualifying=[rtp], decisive=[rtp, paper]), items)
    assert (rr.inputs.e, rr.inputs.k, rr.arp) == (2, 2, 12) and rr.pre_cap_class == rr.final_class == "High"
    flip = rr.flip_condition   # exposure 3 or decision impact 3 alone stays under the Probable cap
    assert flip.startswith("Confirmation of AI use in the service itself, with evidence that a named external model")
    assert "(exposure 3 of 3) or that AI acts on customers" in flip and "(decision impact 3 of 3), would" in flip
    assert flip.endswith("would make the verdict Confirmed and raise the class to Critical.")
    assert not CODES.search(flip)
    rr = risk.score(inputs(3, 2, 3, missing=2), verdict("c"), tier=Tier.CRITICAL)   # 14 Critical, capped
    assert "lift the Probable cap" in rr.flip_condition
    rr = risk.score(inputs(3, 2, 0, missing=0), verdict("c"), tier=Tier.LOW)        # 10 High, Probable
    assert rr.final_class == "High" and rr.flip_condition.startswith("Evidence that the AI is isolated")


def test_flip_names_decision_impact_when_only_k_can_move_the_class():
    item = mk("V-005", "Developers write code with AI assistants.", locus="sdlc", rl="R2", u_class="U3",
              action="advisory")
    rr = assess("V-005", verdict("b", qualifying=[item]), [item])
    assert (rr.inputs.e, rr.inputs.k) == (1, 1)
    assert "decision impact 3 of 3" in rr.flip_condition and "exposure 3" not in rr.flip_condition


# --------------------------------------------------------------------------- themes and determinism


def test_themes_follow_inputs_and_ai_types():
    item = mk("V-005", "An AI agent drafts customer letters with a language model.", ai_type="genai_llm",
              action="automated_action")
    rr = assess("V-005", verdict("b", qualifying=[item]), [item])
    assert {"RT1", "RT3", "RT4", "RT5", "RT6"} <= set(rr.themes) and "RT7" not in rr.themes
    assert rr.themes == sorted(rr.themes, key=lambda t: int(t[2:]))
    ml = mk("V-005", "A machine learning model scores the service's payments.", ai_type="predictive_ml",
            action="advisory")
    assert "RT7" in assess("V-005", verdict("b", qualifying=[ml]), [ml]).themes
    assert assess("V-003", verdict("e"), fssi_items()).themes == []


def test_assess_is_deterministic_and_order_independent():
    items = bny_items()
    v = verdict("b", qualifying=items[:1], corroborating=items[1:2], decisive=items[:3])
    first = assess("V-005", v, items).model_dump()
    assert assess("V-005", v, list(reversed(items))).model_dump() == first
    assert assess("V-005", v, items).model_dump() == first


def test_provider_names_are_canonical():
    items = [mk("V-005", "x" * 30, providers=["ChatGPT", "GPT-4o", "Microsoft Copilot"]),
             mk("V-005", "y" * 30, providers=["Claude 3.5 Sonnet", "ServiceNow AI"])]
    assert risk.provider_names(items) == ["Anthropic", "Microsoft", "OpenAI", "ServiceNow AI"]
    assert risk.foundation_model_providers(items) == ["Anthropic", "Microsoft", "OpenAI"]
