"""Offline sample builders for the P3/P4 data contracts (docs/contracts_p3.md). No network, no Gemini.

Load it like collectors/_fakes.py:

    _p = Path(__file__).resolve().parents[1] / "fixtures" / "p3_samples.py"
    _spec = importlib.util.spec_from_file_location("p3_samples", _p)
    samples = importlib.util.module_from_spec(_spec)
    sys.modules.setdefault("p3_samples", samples)
    _spec.loader.exec_module(samples)

Every builder takes keyword overrides and returns a valid model. All text is synthetic: the vendor is the fictional
"Acme Payments Ltd" (V-901), so fixtures never quote real vendor evidence.
"""

from __future__ import annotations

from typing import Any

from footprint.criticality import score_profile
from footprint.depth import plan_depth
from footprint.models import (
    ActionPlan,
    AssessmentResult,
    CoverageEntry,
    CoverageStatus,
    CriticalityResult,
    DepthPlan,
    EvidenceItem,
    Indicator,
    RiskInputs,
    RiskResult,
    SignalTags,
    SourceFamily,
    UsageVerdict,
    VendorFindings,
    VendorProfile,
)

VENDOR_ID = "V-901"
AS_OF = "2026-10-02"
INPUT_SHA256 = "0" * 64
DOC_ID = "d" * 64
CAPTURE_ID = "c" * 64
URL = "https://www.acme.example/instant-payments"

DOC_TEXT = (
    "Instant payments\n\n"
    "Acme Payments screens every inbound instant payment with a machine learning model before release. "
    "Alerts are reviewed by an analyst before any payment is held.\n"
)
EXCERPT = "Acme Payments screens every inbound instant payment with a machine learning model before release."
EXCERPT_START = DOC_TEXT.index(EXCERPT)


def profile(**kw: Any) -> VendorProfile:
    base: dict[str, Any] = dict(
        row=12, vendor_id=VENDOR_ID, name="Acme Payments Ltd",
        description="Fictional payments processor used in tests.",
        service="Real-time payment screening for inbound instant payments", category="Payments",
        website="https://www.acme.example", business_process="Inbound real-time payments",
        operational_dependency="Critical",
        data_accessed="Payment instructions, counterparty names and account identifiers",
        data_volume="Approximately 1.2 million customers",
    )
    base.update(kw)
    return VendorProfile(**base)


def criticality(p: VendorProfile | None = None) -> CriticalityResult:
    return score_profile(p or profile())


def depth(p: VendorProfile | None = None, c: CriticalityResult | None = None) -> DepthPlan:
    p = p or profile()
    return plan_depth(c or criticality(p), p)


def tags(**kw: Any) -> SignalTags:
    base: dict[str, Any] = dict(u_class="U1", sr="B", sp="S2", rl="R3", rc="T3", ic=2, locus="service_feature",
                                ai_type="predictive_ml", strength="Strong")
    base.update(kw)
    return SignalTags(**base)


def item(**kw: Any) -> EvidenceItem:
    """A verified rule item quoting EXCERPT from DOC_TEXT (identity hashes are derived by the model)."""
    excerpt = kw.pop("excerpt", EXCERPT)
    start = kw.pop("start", DOC_TEXT.index(excerpt) if excerpt in DOC_TEXT else 0)
    base: dict[str, Any] = dict(
        vendor_id=VENDOR_ID, passage_id="0123456789abcdef", doc_id=DOC_ID, capture_id=CAPTURE_ID,
        family=SourceFamily.PRD, source_type="Product page", publisher="Acme Payments", title="Instant payments",
        url=URL, retrieved_at="2026-10-02T12:00:00Z", date_basis="retrieval",
        excerpt=excerpt, start=start, end=start + len(excerpt), tags=tags(),
        indicators=[Indicator(code="G2", span="screens every inbound instant payment")],
        data_mentioned=["inbound instant payment"], temporal="in_production", action_level="human_reviewed_decision",
        rule_labels={"temporal": "in_production", "action_level": "human_reviewed_decision", "sp": "S2"},
    )
    base.update(kw)
    return EvidenceItem(**base)


def verdict(keys: list[str] | None = None, **kw: Any) -> UsageVerdict:
    keys = keys if keys is not None else [item().item_key]
    base: dict[str, Any] = dict(
        rule="b", likelihood="very likely", confidence="Moderate",
        confidence_reason="one first-party source and complete coverage of the mandatory families",
        qualifying=list(keys), corroborating=[], decisive=list(keys), coverage_complete=True,
    )
    base.update(kw)
    return UsageVerdict(**base)


GAPS_ONE_POINT = {"t1": False, "t2": True, "t3": True, "t4": False, "t5": False, "t6": True}  # 3 missing -> TG 1


def risk_inputs(**kw: Any) -> RiskInputs:
    key = item().item_key
    base: dict[str, Any] = dict(
        e=2, k=2, tp=3, e_reason="AI screens inbound payment instructions (service feature, D3)",
        k_reason="alerts reviewed by an analyst before a payment is held", e_items=[key], k_items=[key],
        gaps=dict(GAPS_ONE_POINT),
        gap_reasons={g: "not publicly disclosed; contractual disclosure unknown"
                     for g, missing in GAPS_ONE_POINT.items() if missing},
    )
    base.update(kw)
    return RiskInputs(**base)


def risk_result(inputs: RiskInputs | None = None, **kw: Any) -> RiskResult:
    inputs = inputs or risk_inputs()
    arp = 2 * inputs.e + 2 * inputs.k + inputs.tp + inputs.tg
    base: dict[str, Any] = dict(
        inputs=inputs, arp=arp, base_class="High", gate_met=True, cap="", pre_cap_class="High",
        final_class="High", provisional=False, ceiling_class="",
        flip_condition="External models processing Meridian payment data (exposure 3) would raise the class to Critical.",
        themes=["RT1", "RT3", "RT7"], steps=["1. score 12 of 18", "2. base class High", "3. gate met (E2 evidenced)",
                                             "4. no escalator", "5. Confirmed: no cap"],
    )
    base.update(kw)
    return RiskResult(**base)


def action_plan(**kw: Any) -> ActionPlan:
    base: dict[str, Any] = dict(
        class_playbook=["Issue a targeted questionnaire within 15 business days covering the open gap blocks."],
        gap_blocks=["SUB", "TRAIN", "INC"], questionnaire_items=["Q1", "Q2", "Q9"], contract_clauses=["C1", "C2"],
        monitoring="Re-scan the public footprint annually.",
        text="Issue a targeted questionnaire within 15 business days covering the open gap blocks.",
    )
    base.update(kw)
    return ActionPlan(**base)


def coverage(**kw: Any) -> CoverageEntry:
    base: dict[str, Any] = dict(vendor_id=VENDOR_ID, family=SourceFamily.PRD, mandatory=True,
                                status=CoverageStatus.DONE, collector="site", documents=3, ai_passages=1)
    base.update(kw)
    return CoverageEntry(**base)


def findings(**kw: Any) -> VendorFindings:
    p = kw.pop("profile", None) or profile()
    c = kw.pop("criticality", None) or criticality(p)
    base: dict[str, Any] = dict(
        profile=p, criticality=c, depth=depth(p, c), coverage=[coverage()], evidence=[item()],
        verdict=verdict(), risk=risk_result(), actions=action_plan(), notes=["synthetic sample"],
    )
    base.update(kw)
    return VendorFindings(**base)


def assessment(**kw: Any) -> AssessmentResult:
    base: dict[str, Any] = dict(run_id="A-20261002-0000abcd", mode="replay", as_of=AS_OF, input_sha256=INPUT_SHA256,
                                vendors=[findings()], example=None, manifest={"counts": {"vendors": 1}})
    base.update(kw)
    return AssessmentResult(**base)
