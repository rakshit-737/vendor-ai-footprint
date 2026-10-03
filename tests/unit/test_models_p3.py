"""P3/P4 data contracts (docs/contracts_p3.md): tags, claims, verification, evidence, verdict, risk, actions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from footprint import models as m
from footprint.models import (
    ActionPlan,
    AssessmentResult,
    Claim,
    ClaimBatch,
    EvidenceItem,
    Indicator,
    RiskInputs,
    RiskResult,
    SignalTags,
    StudentCells,
    UsageVerdict,
    VendorFindings,
    VerifyResult,
)

_p = Path(__file__).resolve().parents[1] / "fixtures" / "p3_samples.py"
_spec = importlib.util.spec_from_file_location("p3_samples", _p)
S = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p3_samples", S)
_spec.loader.exec_module(S)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- indicators and tags


def test_indicator_codes_follow_the_schema_range():
    assert m.INDICATOR_CODES == tuple(f"G{i}" for i in range(1, 12)) + tuple(f"M{i}" for i in range(1, 8))
    for code in m.INDICATOR_CODES:
        assert Indicator(code=code, span="x").code == code
    for bad in ("G0", "G12", "M8", "g1", "S1", ""):
        with pytest.raises(ValidationError):
            Indicator(code=bad, span="x")
    assert m.UNDEFINED_INDICATORS == {"G9", "G10"} and m.SOURCE_TYPE_INDICATORS == {"G6", "G7"}


def test_tag_string_format():
    t = S.tags(u_class="U3", sr="B", sp="S2", rl="R2", rc="T3", ic=2, locus="delivery_ops")
    assert isinstance(t, SignalTags)
    assert t.tag_string() == "U3 · SR:B · SP:S2 · RL:R2 · RC:T3 · IC:2 · locus=delivery_ops"
    assert (t.u_number, t.sp_level, t.rl_level, t.rc_level) == (3, 2, 2, 3)


@pytest.mark.parametrize("field,value", [
    ("u_class", "U9"), ("sr", "G"), ("sp", "S4"), ("rl", "R4"), ("rc", "T4"), ("ic", 0), ("ic", 7),
    ("locus", "service"), ("ai_type", "llm"), ("strength", "Context – relationship only"), ("strength", "strong"),
])
def test_signal_tags_reject_values_outside_the_legend(field, value):
    with pytest.raises(ValidationError):
        S.tags(**{field: value})


def test_signal_tags_forbid_unknown_fields():
    with pytest.raises(ValidationError):
        S.tags(relevance="R3")


def test_label_and_role_orders():
    assert m.STRENGTH_ORDER == ("Negative", "Strong", "Moderate", "Context - relationship only",
                                "Context - platform supplier", "Context - inferred affiliate", "Marketing only", "Weak")
    assert m.CONTEXT_STRENGTHS == {"Context - relationship only", "Context - platform supplier",
                                   "Context - inferred affiliate"}
    assert m.ROLE_ORDER == ("Primary", "Supporting", "Indicator", "Counter-evidence", "Negative", "Context", "Logged")
    assert m.CITED_ROLES == set(m.ROLE_ORDER) - {"Logged"}


# --------------------------------------------------------------------------- Gemini claim schema


def test_claim_schema_is_the_appendix_schema():
    schema = ClaimBatch.model_json_schema()
    claim = schema["$defs"]["Claim"]
    fields = ["passage_id", "quote", "claim_kind", "subject", "ai_type", "temporal", "action_level",
              "named_providers", "data_mentioned", "indicators"]
    assert list(claim["properties"]) == fields and sorted(claim["required"]) == sorted(fields)
    enums = {k: v.get("enum") for k, v in claim["properties"].items()}
    assert enums["claim_kind"] == ["uses_ai", "offers_ai_feature", "ai_partnership", "names_ai_provider", "ai_hiring",
                                   "ai_governance", "generic_ai_marketing", "negative_or_limiting",
                                   "industry_commentary", "not_ai"]
    assert enums["subject"] == ["vendor_product", "vendor_operations", "vendor_staff_tools", "third_party_product",
                                "other_or_industry"]
    assert enums["ai_type"] == ["predictive_ml", "genai_llm", "agentic", "document_ai", "conversational", "aiops",
                                "unspecified"]
    assert enums["temporal"] == ["in_production", "pilot_or_beta", "planned", "unclear"]
    assert enums["action_level"] == ["none", "advisory", "human_reviewed_decision", "automated_action", "unknown"]
    ind = schema["$defs"]["Indicator"]
    assert ind["properties"]["code"]["enum"] == list(m.INDICATOR_CODES) and sorted(ind["required"]) == ["code", "span"]
    assert schema["required"] == ["claims"]
    assert "additionalProperties" not in claim  # lenient on purpose: Gemini output is parsed, then verified


def test_claim_schema_sent_to_gemini_carries_no_internal_wording():
    text = json.dumps(ClaimBatch.model_json_schema()).lower()
    for word in ("meridian", "footprint", "verify", "tier", "verdict", "osprey"):
        assert word not in text, word


def test_claim_batch_parses_model_output():
    raw = json.dumps({"claims": [{
        "passage_id": "0123456789abcdef", "quote": S.EXCERPT, "claim_kind": "uses_ai", "subject": "vendor_product",
        "ai_type": "predictive_ml", "temporal": "in_production", "action_level": "advisory",
        "named_providers": [], "data_mentioned": ["inbound instant payment"],
        "indicators": [{"code": "G2", "span": "screens every inbound instant payment"}],
        "confidence": 0.9,  # unknown keys are ignored, never trusted
    }]})
    batch = ClaimBatch.model_validate_json(raw)
    assert batch.claims[0].quote == S.EXCERPT and batch.claims[0].indicators[0].code == "G2"
    assert not hasattr(batch.claims[0], "confidence")


def test_claim_rejects_values_outside_the_schema():
    good = dict(passage_id="p", quote="q", claim_kind="uses_ai", subject="vendor_product", ai_type="genai_llm",
                temporal="planned", action_level="none", named_providers=[], data_mentioned=[], indicators=[])
    assert Claim(**good).temporal == "planned"
    for field, bad in (("claim_kind", "uses_ml"), ("ai_type", "not_ai"), ("temporal", "live")):
        with pytest.raises(ValidationError):
            Claim(**{**good, field: bad})
    with pytest.raises(ValidationError):
        Claim(**{k: v for k, v in good.items() if k != "indicators"})  # every field is required


# --------------------------------------------------------------------------- verification result


def test_verify_result_ok_means_no_blocking_failure():
    located = dict(start=10, end=10 + len(S.EXCERPT), excerpt=S.EXCERPT)
    assert VerifyResult(ok=True, failures=[], **located).ok
    repaired = VerifyResult(ok=True, failures=["V5", "V4", "V4"], **located)
    assert repaired.failures == ["V4", "V5"]  # unique, gate order
    assert VerifyResult(ok=False, failures=["V2"]).start is None
    assert m.BLOCKING_VERIFY_CODES == {"V1", "V2", "V3", "V6", "V9"} and m.REPAIR_VERIFY_CODES == {"V4", "V5"}
    with pytest.raises(ValidationError):
        VerifyResult(ok=True, failures=["V3"], **located)
    with pytest.raises(ValidationError):
        VerifyResult(ok=False, failures=[], **located)
    with pytest.raises(ValidationError):
        VerifyResult(ok=False, failures=["V10"])


def test_verify_result_offsets_match_the_excerpt():
    with pytest.raises(ValidationError):
        VerifyResult(ok=True, failures=[])  # a passing claim needs its located excerpt (V2)
    with pytest.raises(ValidationError):
        VerifyResult(ok=True, failures=[], start=0, end=5, excerpt="four")
    with pytest.raises(ValidationError):
        VerifyResult(ok=False, failures=["V2"], start=None, end=None, excerpt="orphan text")


# --------------------------------------------------------------------------- evidence items


def test_evidence_item_derives_its_identity():
    it = S.item()
    assert it.excerpt == S.DOC_TEXT[it.start:it.end]
    assert it.excerpt_sha256 == _sha(S.EXCERPT)
    assert it.url_final == S.URL  # same as url when there was no redirect
    assert it.item_key == _sha(f"{S.URL}|{_sha(S.EXCERPT)}") == EvidenceItem.make_key(S.URL, _sha(S.EXCERPT))
    assert it.capture_sha256 == S.CAPTURE_ID and it.text_sha256 == S.DOC_ID
    assert it.evidence_id == "" and it.role == "Logged" and it.method == "rule" and it.review_status == "unreviewed"
    wayback = S.item(url_final="https://web.archive.org/web/20260101000000id_/" + S.URL)
    assert wayback.item_key != it.item_key


@pytest.mark.parametrize("override", [
    {"excerpt_sha256": "0" * 64},
    {"item_key": "0" * 64},
    {"capture_sha256": "e" * 64},
    {"text_sha256": "e" * 64},
    {"end": S.EXCERPT_START + len(S.EXCERPT) + 1},
    {"excerpt": "", "start": 0, "end": 0},
    {"start": -1, "end": len(S.EXCERPT) - 1},
    {"evidence_id": "V-902-E-0001"},
    {"evidence_id": "V-901-E-1"},
    {"unknown_field": 1},
])
def test_evidence_item_rejects_inconsistent_identity(override):
    with pytest.raises(ValidationError):
        S.item(**override)


def test_evidence_item_json_round_trip():
    it = S.item(evidence_id="V-901-E-0001", role="Primary", cluster_id="ab12cd34ef56")
    data = json.loads(it.model_dump_json())
    assert data["family"] == "PRD" and data["tags"]["strength"] == "Strong"
    assert EvidenceItem.model_validate_json(it.model_dump_json()) == it
    assert it.tag_string == it.tags.tag_string() and it.strength == "Strong"


def test_citable_and_proposed_items():
    assert S.item().citable and not S.item().proposed
    assert not S.item(review_status="rejected").citable
    pending = S.item(method="llm_proposed_accepted", llm_model="gemini-3.5-flash-lite")
    assert pending.proposed and not pending.citable
    accepted = S.item(method="llm_proposed_accepted", review_status="accepted", reviewer="RK 2026-10-08")
    assert accepted.citable and not accepted.proposed
    trap = S.item(tags=S.tags(ai_type="not_ai", u_class="U7", sp="S1", strength="Marketing only"))
    assert not trap.citable  # definition-test traps stay visible in the Evidence Log, never counted


def test_label_disagreements_cover_the_v8_labels_only():
    it = S.item(rule_labels={"temporal": "in_production", "action_level": "advisory", "sp": "S2"},
                llm_labels={"temporal": "planned", "action_level": "advisory", "sp": "S2", "claim_kind": "uses_ai"})
    assert it.label_disagreements == ["temporal"]
    assert m.V8_LABEL_KEYS == ("temporal", "action_level", "sp")
    assert S.item().label_disagreements == []


# --------------------------------------------------------------------------- verdict


@pytest.mark.parametrize("rule,label,column_o", [
    ("a", "Inconclusive", "Inconclusive"), ("b", "Confirmed", "Yes"), ("c", "Probable", "Yes"),
    ("d", "Affirmed negative", "No"), ("e", "Not detected", "No"), ("f", "Inconclusive", "Inconclusive"),
])
def test_usage_verdict_derives_label_and_column_o_from_the_rule(rule, label, column_o):
    v = UsageVerdict(rule=rule, likelihood="unlikely", confidence="Low", confidence_reason="test", coverage_complete=False)
    assert (v.label, v.column_o, v.conflict) == (label, column_o, rule == "a")
    assert m.RULE_LABEL[rule] == label and m.LABEL_COLUMN_O[label] == column_o


@pytest.mark.parametrize("override", [
    {"label": "Probable"}, {"column_o": "No"}, {"conflict": True}, {"likelihood": "probable"},
    {"confidence": "Medium"}, {"rule": "g"},
])
def test_usage_verdict_rejects_inconsistent_values(override):
    with pytest.raises(ValidationError):
        S.verdict(**override)


def test_icd203_likelihood_scale():
    assert m.ICD203_LIKELIHOOD == ("almost no chance", "very unlikely", "unlikely", "roughly even chance", "likely",
                                   "very likely", "almost certain")


# --------------------------------------------------------------------------- risk inputs and result


def test_transparency_gap_points_and_risk_bands():
    assert [m.tg_for_missing(n) for n in range(7)] == [0, 0, 1, 1, 2, 2, 3]
    assert [m.arp_class(a) for a in (0, 5, 6, 9, 10, 13, 14, 18)] == [
        "Low", "Low", "Medium", "Medium", "High", "High", "Critical", "Critical"]
    with pytest.raises(ValueError):
        m.arp_class(19)
    with pytest.raises(ValueError):
        m.tg_for_missing(7)


def test_risk_inputs_derive_tg_from_the_six_checks():
    ri = S.risk_inputs()
    assert ri.tg == 1 and ri.missing_gaps == ["t2", "t3", "t6"]
    assert RiskInputs(e=1, k=1, tp=0, tg=2).gaps == {}  # gaps not assessed: tg stands as given
    with pytest.raises(ValidationError):
        S.risk_inputs(tg=2)  # inconsistent with three missing checks
    with pytest.raises(ValidationError):
        S.risk_inputs(gaps={"t1": True, "t2": True})  # all six checks or none
    with pytest.raises(ValidationError):
        S.risk_inputs(gaps={**S.GAPS_ONE_POINT, "t7": True})
    for field, bad in (("e", 4), ("k", -1), ("tp", 4), ("e_if_confirmed", 4)):
        with pytest.raises(ValidationError):
            S.risk_inputs(**{field: bad})


def test_v000_calibration_scores_17_critical():
    gaps = {"t1": False, "t2": True, "t3": True, "t4": True, "t5": True, "t6": False}  # 4 missing -> TG 2
    ri = RiskInputs(e=3, k=3, tp=3, gaps=gaps, e_reason="E3 evidenced", k_reason="K3 evidenced")
    assert ri.tg == 2
    rr = RiskResult(inputs=ri, arp=17, base_class="Critical", gate_met=True, pre_cap_class="Critical",
                    final_class="Critical")
    assert rr.final_class == "Critical" and not rr.provisional and rr.cap == ""


@pytest.mark.parametrize("override", [
    {"arp": 13},                                                  # 2E + 2K + TP + TG = 12
    {"base_class": "Medium"},                                     # 12 is High
    {"cap": "Medium", "provisional": False, "final_class": "Medium"},  # Inconclusive cap means Provisional
    {"cap": "", "provisional": True},
    {"cap": "High", "final_class": "Critical"},                   # final class above the cap
    {"cap": "None identified", "final_class": "Low"},
    {"final_class": "None identified"},                           # only a No verdict gives None identified
    {"escalators_fired": ["X1"], "escalators_logged_not_applied": ["X1"]},
    {"themes": ["RT8"]},
    {"ceiling_class": "Critical if confirmed"},
])
def test_risk_result_rejects_inconsistent_steps(override):
    with pytest.raises(ValidationError):
        S.risk_result(**override)


def test_provisional_and_none_identified_results():
    ri = RiskInputs(e=3, k=3, tp=2, tg=3, e_assumed=True, k_assumed=True)
    prov = RiskResult(inputs=ri, arp=17, base_class="Critical", gate_met=False, cap="Medium", pre_cap_class="Medium",
                      final_class="Medium", provisional=True, ceiling_class="Critical",
                      flip_condition="Confirmation that the affiliate platform delivers Meridian work.")
    assert prov.provisional and prov.ceiling_class == "Critical"
    none = RiskResult(inputs=RiskInputs(e=0, k=0, tp=2, tg=1), arp=3, base_class="Low", gate_met=False,
                      cap="None identified", pre_cap_class="Low", final_class="None identified")
    assert none.final_class == "None identified"


# --------------------------------------------------------------------------- actions


def test_action_plan_codes():
    assert S.action_plan().questionnaire_items == ["Q1", "Q2", "Q9"]
    assert m.GAP_BLOCK_CODES == ("SUB", "TRAIN", "LOC", "EXPL", "AGENT", "PI", "GOV", "INC", "CONC", "MRM")
    for field, bad in (("questionnaire_items", ["Q16"]), ("contract_clauses", ["C12"]), ("gap_blocks", ["GAP-SUB"])):
        with pytest.raises(ValidationError):
            S.action_plan(**{field: bad})


# --------------------------------------------------------------------------- findings and assessment


def test_vendor_findings_bundle_one_vendor():
    f = S.findings()
    assert f.vendor_id == "V-901" and f.cells == StudentCells()
    key = f.evidence[0].item_key
    assert f.item(key) is f.evidence[0] and f.item("nope") is None
    assert f.cited() == []
    cited = S.findings(evidence=[S.item(evidence_id="V-901-E-0001", role="Primary")])
    assert cited.item("V-901-E-0001") is cited.evidence[0] and cited.cited() == cited.evidence


@pytest.mark.parametrize("override", [
    lambda: {"evidence": [S.item(vendor_id="V-902", passage_id="x")]},          # another vendor's item
    lambda: {"evidence": [S.item(), S.item()]},                                 # duplicate item key (V9)
    lambda: {"evidence": [S.item(evidence_id="V-901-E-0001"),
                          S.item(excerpt="Alerts are reviewed by an analyst before any payment is held.",
                                 evidence_id="V-901-E-0001")]},                 # duplicate E-ID
    lambda: {"verdict": S.verdict(keys=["f" * 64])},                            # verdict cites an unknown item
    lambda: {"risk": S.risk_result(S.risk_inputs(e_items=["f" * 64]))},        # risk input cites an unknown item
    lambda: {"coverage": [S.coverage(vendor_id="V-902")]},
    lambda: {"evidence": [S.item(corroborates=["f" * 64])]},
])
def test_vendor_findings_reject_dangling_or_foreign_references(override):
    with pytest.raises(ValidationError):
        S.findings(**override())


def test_assessment_result_round_trip_and_helpers():
    res = S.assessment()
    again = AssessmentResult.model_validate_json(res.model_dump_json())
    assert again == res
    assert res.vendor("V-901") is res.vendors[0] and res.vendor("V-999") is None
    assert res.cells() == {"V-901": StudentCells()}
    assert isinstance(res.vendors[0], VendorFindings)


@pytest.mark.parametrize("override", [
    {"mode": "live"}, {"as_of": "02-10-2026"}, {"input_sha256": "abc"},
    {"vendors": "twice"},
])
def test_assessment_result_rejects_bad_run_metadata(override):
    if override.get("vendors") == "twice":
        override = {"vendors": [S.findings(), S.findings()]}
    with pytest.raises(ValidationError):
        S.assessment(**override)


def test_action_plan_and_findings_forbid_unknown_fields():
    with pytest.raises(ValidationError):
        ActionPlan(text="x", deadline_days=15)
    with pytest.raises(ValidationError):
        S.findings(summary="x")


# --------------------------------------------------------------------------- P1/P2 contracts unchanged


def test_existing_contracts_unchanged():
    assert m.STUDENT_FIELDS == ["criticality_tier", "criticality_rationale", "assessment_depth", "ai_usage_detected",
                                "evidence", "how_ai_used", "ai_subprocessors", "ai_risk_class", "risk_rationale",
                                "recommended_action", "assessed_by"]
    assert list(m.CoverageEntry.model_fields) == ["vendor_id", "family", "mandatory", "status", "collector", "endpoint",
                                                  "requests_used", "cap", "documents", "ai_passages", "note"]
    assert list(m.Passage.model_fields) == ["passage_id", "doc_id", "vendor_id", "start", "end", "text", "hits"]
    assert m.Passage.make_id("d", 1, 5) == hashlib.sha256(b"d|1|5").hexdigest()[:16]
    assert list(m.StudentCells.model_fields) == m.STUDENT_FIELDS
