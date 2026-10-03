"""Tests for column U (footprint.actions + config/actions.toml; design Appendix A 2.8, contracts_p3 section 9).

Risk results are built with footprint.risk.score from the design's expected inputs, so these tests do not depend
on the evidence pipeline. Evidence items are synthetic. Fully offline.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from footprint import actions, risk
from footprint.models import (
    GAP_BLOCK_CODES,
    ActionPlan,
    EvidenceItem,
    Indicator,
    RiskInputs,
    RiskResult,
    SignalTags,
    SourceFamily,
    Tier,
    UsageVerdict,
)
from footprint.workbook import DEFAULT_LENGTH_BUDGETS

REPO = Path(__file__).resolve().parents[2]
CHECKS = ("t1", "t2", "t3", "t4", "t5", "t6")
IMPERATIVES = {"Issue", "Register", "Raise", "Escalate", "Reclassify", "Cover", "Review", "Obtain", "Add", "Take",
               "Re-scan", "Use", "Ask"}
FIRST_PERSON_OR_TEAM = re.compile(r"\b(?:we|our|ours|us|team|osprey)\b|\bI\b", re.IGNORECASE)
SENTENCE = re.compile(r"(?<=[.?!])\s+(?=[A-Z])")


def mk(excerpt: str, *, family: SourceFamily = SourceFamily.PRD, source_type: str = "Product page",
       action: str = "unknown", providers: tuple[str, ...] = (), review: str = "unreviewed", title: str = "Page",
       url: str | None = None, **tag_kw: object) -> EvidenceItem:
    tag: dict[str, object] = dict(u_class="U1", sr="B", sp="S2", rl="R3", rc="T3", ic=2, locus="service_feature",
                                  ai_type="unspecified", strength="Strong")
    tag.update(tag_kw)
    digest = hashlib.sha256(excerpt.encode()).hexdigest()[:12]
    return EvidenceItem(
        vendor_id="V-901", doc_id="d" * 64, capture_id="c" * 64, family=family, source_type=source_type,
        publisher="Acme Payments", title=title, url=url or f"https://acme.example/{digest}",
        retrieved_at="2026-10-02T12:00:00Z", excerpt=excerpt, start=0, end=len(excerpt), tags=SignalTags(**tag),
        providers=list(providers), action_level=action, review_status=review,
        indicators=[Indicator(code="G2", span=excerpt[:10])],
    )


def verdict(rule: str) -> UsageVerdict:
    likelihood = {"a": "roughly even chance", "b": "very likely", "c": "likely", "d": "very unlikely",
                  "e": "unlikely", "f": "unlikely"}[rule]
    return UsageVerdict(rule=rule, likelihood=likelihood, confidence="Moderate",
                        confidence_reason="a synthetic test verdict", coverage_complete=True)


def scored(e: int, k: int, tier: Tier, rule: str, *, missing: tuple[str, ...] = CHECKS, assumed: bool = False,
           escalators: dict[str, bool] | None = None) -> RiskResult:
    gaps = {g: g in missing for g in CHECKS}
    ri = RiskInputs(e=e, k=k, tp=tier.points, gaps=gaps, e_assumed=assumed, k_assumed=assumed)
    return risk.score(ri, verdict(rule), tier=tier, escalators=escalators)


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE.split(text) if s]


def assert_addressed_to_meridian(plan: ActionPlan) -> None:
    assert plan.text and len(plan.text) <= 1480
    assert not FIRST_PERSON_OR_TEAM.search(plan.text), plan.text
    for sentence in sentences(plan.text):
        assert sentence.split()[0] in IMPERATIVES, sentence
        assert sentence.endswith("."), sentence


# --------------------------------------------------------------------------- config


def test_config_loads_and_is_complete():
    cfg = actions.load_actions()
    assert cfg is actions.load_actions()   # cached per path
    assert tuple(cfg.blocks) == GAP_BLOCK_CODES
    assert list(cfg.questions) == [f"Q{n}" for n in range(1, 16)]
    assert list(cfg.clauses) == [f"C{n}" for n in range(1, 12)]
    assert set(cfg.playbooks) == set(actions.PLAYBOOK_KEYS)
    assert cfg.max_chars == DEFAULT_LENGTH_BUDGETS["recommended_action"] == 1480
    assert (cfg.deadlines.critical, cfg.deadlines.high_critical_tier, cfg.deadlines.high) == (15, 15, 30)
    assert (cfg.deadlines.provisional_high_ceiling, cfg.deadlines.provisional) == (15, 30)
    assert (cfg.deadlines.standard_critical_tier, cfg.deadlines.standard) == (15, 30)


@pytest.mark.parametrize("old, new, message", [
    ('questions = ["Q5"]', 'questions = ["Q16"]', "Q16"),
    ("Register the vendor on Meridian's", "We register the vendor on Meridian's", "first person"),
    ('[blocks.MRM]', '[blocks.MODEL]', "blocks"),
    ('max_chars = 1480', 'max_chars = 2000', "max_chars"),
    ('monitoring = "quarterly"', 'monitoring = "weekly"', "monitoring"),
    ("'\\bhosted (?:in|within)\\b',", "'(unclosed',", "pattern"),
])
def test_config_rejects_broken_policies(tmp_path, old, new, message):
    text = (REPO / "config" / "actions.toml").read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "actions.toml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        actions.load_actions(path)


def test_tables_for_the_method_and_legend_sheet():
    questions = actions.questionnaire_table()
    clauses = actions.clause_table()
    blocks = actions.gap_block_table()
    assert [q[0] for q in questions] == [f"Q{n}" for n in range(1, 16)]
    assert [c[0] for c in clauses] == [f"C{n}" for n in range(1, 12)]
    assert [b[0] for b in blocks] == [f"GAP-{code}" for code in GAP_BLOCK_CODES]
    assert all(len(row) == 3 and all(row) for row in questions + clauses)
    assert blocks[0][2].startswith("Q3, Q14; C2")


# --------------------------------------------------------------------------- playbooks by class


def test_critical_class_follows_the_v000_four_sentence_pattern():
    plan = actions.plan_actions(risk.calibrate(), risk.calibration_verdict(), [], Tier.CRITICAL)
    first, second, third, fourth = plan.class_playbook
    assert first.startswith("Issue a targeted questionnaire within 15 business days covering which Meridian data")
    assert second == "Register the vendor on Meridian's AI sub-processor inventory with an unresolved-provider flag."
    assert third == ("Raise AI clauses at the next contract review, addressing training restrictions, sub-processor "
                     "change notification, human oversight thresholds, and rights to review attestations.")
    assert fourth == "Escalate to the Third-Party Risk Committee where confirmation is not received."
    assert plan.gap_blocks == ["SUB", "TRAIN", "LOC", "EXPL", "GOV", "INC"]
    assert plan.questionnaire_items == ["Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q10", "Q11", "Q14", "Q15"]
    assert plan.contract_clauses == ["C1", "C2", "C3", "C4", "C5", "C6", "C9", "C10"]
    assert plan.monitoring.startswith("Re-scan") and "quarterly" in plan.monitoring
    assert plan.text.startswith(" ".join(plan.class_playbook))
    assert "Q10" in plan.text and plan.text.endswith(plan.monitoring)
    assert_addressed_to_meridian(plan)


@pytest.mark.parametrize("tier, days", [(Tier.CRITICAL, 15), (Tier.HIGH, 30), (Tier.MEDIUM, 30)])
def test_high_class_questionnaire_due_by_tier(tier, days):
    rr = scored(3, 3, tier, "c", missing=("t3", "t6"))   # Probable: capped at High whatever the tier
    assert rr.final_class == "High"
    plan = actions.plan_actions(rr, verdict("c"), [], tier)
    assert len(plan.class_playbook) == 4
    assert plan.class_playbook[0].startswith(f"Issue a targeted questionnaire within {days} business days")
    assert plan.class_playbook[1].endswith(", recording the AI providers it names publicly.")   # t2 closed
    assert "every six months" in plan.monitoring
    assert_addressed_to_meridian(plan)


@pytest.mark.parametrize("e, k, tier, ceiling, days", [
    (3, 3, Tier.HIGH, "Critical", 15),     # AutomWorx: 3a.3a.2.3 = 17
    (2, 2, Tier.HIGH, "High", 15),         # Terrapin: 2a.2a.2.3 = 13
    (1, 1, Tier.HIGH, "", 30),             # 1a.1a.2.3 = 9: no ceiling above Medium
])
def test_provisional_playbook_asks_q1_to_q4(e, k, tier, ceiling, days):
    rr = scored(e, k, tier, "f", assumed=True)
    assert rr.provisional and rr.final_class == "Medium" and rr.ceiling_class == ceiling
    plan = actions.plan_actions(rr, verdict("f"), [], tier)
    assert plan.class_playbook[0].startswith(f"Issue questionnaire items Q1 to Q4 within {days} business days")
    assert plan.class_playbook[1].startswith("Reclassify the vendor on the response")
    assert (f"ceiling of {ceiling} if AI use is confirmed" in plan.class_playbook[1]) == bool(ceiling)
    assert plan.questionnaire_items[:4] == ["Q1", "Q2", "Q3", "Q4"]
    assert "model provider identity" not in plan.class_playbook[2]    # SUB is already asked by Q3
    assert plan.class_playbook[2].startswith("Cover training use and retention of Meridian data")
    assert "until the classification is final" in plan.monitoring
    assert_addressed_to_meridian(plan)


def test_medium_or_low_class_without_a_cap_gets_the_standard_playbook():
    rr = scored(1, 1, Tier.MEDIUM, "b", missing=("t2", "t3"))   # 2 + 2 + 1 + 1 = 6, Medium, Confirmed
    assert rr.final_class == "Medium" and not rr.provisional
    plan = actions.plan_actions(rr, verdict("b"), [], Tier.MEDIUM)
    assert plan.class_playbook[0].startswith("Issue a questionnaire within 30 business days covering")
    assert plan.class_playbook[1].startswith("Review AI clauses")
    assert "annually" in plan.monitoring
    assert_addressed_to_meridian(plan)


def test_standard_playbook_for_a_critical_tier_vendor_is_due_in_15_business_days():
    rr = scored(1, 1, Tier.CRITICAL, "c", missing=("t1", "t2", "t3"), assumed=True)   # 1a.1a.3.1 = 8
    assert rr.final_class == "Medium" and not rr.provisional
    plan = actions.plan_actions(rr, verdict("c"), [], Tier.CRITICAL)
    assert plan.class_playbook[0].startswith("Issue a questionnaire within 15 business days covering")
    assert_addressed_to_meridian(plan)


def test_none_identified_for_a_critical_or_high_tier_vendor():
    rr = scored(0, 0, Tier.HIGH, "e")
    assert rr.final_class == "None identified"
    platform = mk("Composition platforms with generative AI add-ons are a plus.", family=SourceFamily.JOB,
                  u_class="U5", sp="S1", rl="R1", locus="platform_supplier", ai_type="genai_llm",
                  strength="Context - platform supplier")
    plan = actions.plan_actions(rr, verdict("e"), [platform], Tier.HIGH)
    assert plan.class_playbook[0] == ("Obtain a written attestation from the vendor that no AI is used in the service "
                                      "or on Meridian data, including the AI features of the platforms it uses.")
    assert plan.class_playbook[1].startswith("Add an AI change-notification clause at renewal")
    assert plan.gap_blocks == [] and plan.questionnaire_items == [] and plan.contract_clauses == ["C9"]
    assert "Use contract clause C9 from the Method & Legend sheet." in plan.text    # singular for one code
    assert "annually" in plan.monitoring
    assert_addressed_to_meridian(plan)
    plain = actions.plan_actions(rr, verdict("e"), [], Tier.HIGH)
    assert plain.class_playbook[0].endswith("no AI is used in the service or on Meridian data.")


def test_none_identified_for_a_medium_or_low_tier_vendor():
    rr = scored(0, 0, Tier.LOW, "d")
    plan = actions.plan_actions(rr, verdict("d"), [], Tier.LOW)
    assert plan.class_playbook == ["Take no AI-specific action beyond monitoring."]
    assert plan.questionnaire_items == [] and plan.contract_clauses == [] and plan.gap_blocks == []
    assert_addressed_to_meridian(plan)


def test_a_conflict_becomes_a_questionnaire_item():
    rr = scored(3, 3, Tier.CRITICAL, "a", assumed=True)
    plan = actions.plan_actions(rr, verdict("a"), [], Tier.CRITICAL)
    assert any(s.startswith("Ask the vendor in the same questionnaire to reconcile") for s in sentences(plan.text))
    assert_addressed_to_meridian(plan)


# --------------------------------------------------------------------------- gap blocks


def _blocks(rr: RiskResult, items: list[EvidenceItem], rule: str = "b") -> list[str]:
    return actions.gap_blocks(rr, verdict(rule), items)


def test_blocks_follow_the_missing_checks():
    assert _blocks(scored(2, 2, Tier.CRITICAL, "b", missing=CHECKS), []) == [
        "SUB", "TRAIN", "LOC", "EXPL", "GOV", "INC"]
    register = mk("Our sub-processor list names each AI provider we use.", family=SourceFamily.LEG,
                  source_type="Sub-processor list", providers=("OpenAI", "Anthropic"))
    region = mk("All AI processing is hosted in the EU.")
    assert _blocks(scored(2, 2, Tier.CRITICAL, "b", missing=()), [register, region]) == []
    assert "SUB" in _blocks(scored(2, 2, Tier.CRITICAL, "b", missing=()), [region])   # t2 closed, no register


@pytest.mark.parametrize("item, block", [
    (mk("An AI agent closes duplicate tickets.", ai_type="agentic"), "AGENT"),
    (mk("The model holds suspicious payments.", action="automated_action"), "AGENT"),
    (mk("A chatbot answers client questions.", ai_type="conversational"), "PI"),
    (mk("A language model drafts client letters.", ai_type="genai_llm"), "PI"),
])
def test_blocks_follow_the_ai_in_the_items(item, block):
    rr = scored(2, 2, Tier.CRITICAL, "b", missing=())
    region = mk("All AI processing is hosted in the EU.")
    register = mk("Sub-processors", family=SourceFamily.LEG, source_type="Sub-processor list")
    assert block in _blocks(rr, [item, region, register])
    rejected = item.model_copy(update={"review_status": "rejected"})
    limiting = item.model_copy(update={"tags": item.tags.model_copy(update={"u_class": "U8"})})
    assert block not in _blocks(rr, [rejected, region, register])
    assert block not in _blocks(rr, [limiting, region, register])


def test_conc_follows_x6_or_a_single_provider():
    rr = scored(2, 2, Tier.CRITICAL, "b", missing=())
    one = mk("The service runs on GPT-4.", providers=("GPT-4", "ChatGPT"))
    two = mk("The service runs on GPT-4 and Claude.", providers=("GPT-4", "Claude"))
    assert "CONC" in _blocks(rr, [one])
    assert "CONC" not in _blocks(rr, [two])
    x6 = scored(2, 2, Tier.CRITICAL, "f", missing=(), escalators={"X6": True})   # logged, not applied
    assert x6.escalators_logged_not_applied == ["X6"] and "CONC" in _blocks(x6, [two], "f")


def test_mrm_needs_a_predictive_model_with_decision_impact_two():
    model = mk("A machine learning model scores every payment.", ai_type="predictive_ml")
    assert "MRM" in _blocks(scored(2, 2, Tier.CRITICAL, "b", missing=()), [model])
    assert "MRM" not in _blocks(scored(2, 1, Tier.CRITICAL, "b", missing=()), [model])


def test_no_blocks_for_none_identified():
    agent = mk("An AI agent closes duplicate tickets.", ai_type="agentic")
    assert _blocks(scored(0, 0, Tier.CRITICAL, "e"), [agent], "e") == []


# --------------------------------------------------------------------------- expected outcomes and budget


@pytest.mark.parametrize("name, e, k, tier, rule, missing, assumed, opening", [
    ("AutomWorx", 3, 3, Tier.HIGH, "f", CHECKS, True, "Issue questionnaire items Q1 to Q4 within 15 business days"),
    ("Fiserv", 2, 2, Tier.CRITICAL, "c", ("t1", "t3", "t6"), False,
     "Issue a targeted questionnaire within 15 business days"),
    ("FSSI", 0, 0, Tier.HIGH, "e", CHECKS, False, "Obtain a written attestation from the vendor"),
    ("Terrapin", 2, 2, Tier.HIGH, "f", CHECKS, True, "Issue questionnaire items Q1 to Q4 within 15 business days"),
    ("BNY", 2, 2, Tier.CRITICAL, "b", ("t3", "t6"), False, "Issue a targeted questionnaire within 15 business days"),
    ("TCH", 2, 1, Tier.CRITICAL, "c", ("t1", "t3", "t4", "t5", "t6"), False,
     "Issue a targeted questionnaire within 15 business days"),
])
def test_expected_outcomes_get_their_playbook(name, e, k, tier, rule, missing, assumed, opening):
    rr = scored(e, k, tier, rule, missing=missing, assumed=assumed)
    plan = actions.plan_actions(rr, verdict(rule), [], tier)
    assert plan.text.startswith(opening), name
    assert_addressed_to_meridian(plan)


def _everything() -> list[EvidenceItem]:
    return [mk("An AI agent with a language model holds payments.", ai_type="agentic", action="automated_action",
               providers=("OpenAI",)),
            mk("A chatbot answers client questions.", ai_type="conversational"),
            mk("A machine learning model scores every payment.", ai_type="predictive_ml")]


@pytest.mark.parametrize("e, k, tier, rule, assumed", [
    (3, 3, Tier.CRITICAL, "b", False), (3, 3, Tier.CRITICAL, "c", False), (3, 3, Tier.CRITICAL, "a", True),
    (1, 2, Tier.MEDIUM, "b", False), (0, 0, Tier.CRITICAL, "e", False), (0, 0, Tier.LOW, "e", False),
])
def test_every_block_at_once_fits_the_column_budget(e, k, tier, rule, assumed):
    rr = scored(e, k, tier, rule, assumed=assumed, escalators={"X6": True})
    plan = actions.plan_actions(rr, verdict(rule), _everything(), tier)
    if rr.final_class != "None identified":
        assert plan.gap_blocks == list(GAP_BLOCK_CODES)
    assert len(plan.text) <= 1480
    assert_addressed_to_meridian(plan)


def test_text_degrades_to_fit_a_smaller_budget(tmp_path):
    text = (REPO / "config" / "actions.toml").read_text(encoding="utf-8")
    rr = scored(3, 3, Tier.CRITICAL, "b", escalators={"X6": True})
    full = actions.plan_actions(rr, verdict("b"), _everything(), Tier.CRITICAL)
    tight = tmp_path / "tight.toml"
    tight.write_text(text.replace("max_chars = 1480", f"max_chars = {len(full.text) - 1}"), encoding="utf-8")
    plan = actions.plan_actions(rr, verdict("b"), _everything(), Tier.CRITICAL, config=actions.load_actions(tight))
    assert len(plan.text) < len(full.text)
    assert plan.questionnaire_items == full.questionnaire_items     # the codes stay in the plan itself
    tiny = tmp_path / "tiny.toml"
    tiny.write_text(text.replace("max_chars = 1480", "max_chars = 200"), encoding="utf-8")
    with pytest.raises(ValueError, match="1480|budget|200"):
        actions.plan_actions(rr, verdict("b"), _everything(), Tier.CRITICAL, config=actions.load_actions(tiny))


def test_plan_is_deterministic_and_order_independent():
    rr = scored(3, 3, Tier.CRITICAL, "b", escalators={"X6": True})
    items = _everything()
    first = actions.plan_actions(rr, verdict("b"), items, Tier.CRITICAL)
    assert actions.plan_actions(rr, verdict("b"), list(reversed(items)), Tier.CRITICAL) == first
    assert isinstance(first, ActionPlan)


def test_codes_are_listed_in_numeric_order():
    rr = scored(3, 3, Tier.CRITICAL, "b", escalators={"X6": True})
    plan = actions.plan_actions(rr, verdict("b"), _everything(), Tier.CRITICAL)
    assert plan.questionnaire_items == sorted(plan.questionnaire_items, key=lambda c: int(c[1:]))
    assert plan.contract_clauses == sorted(plan.contract_clauses, key=lambda c: int(c[1:]))
    assert "Q15" in plan.questionnaire_items and "C11" in plan.contract_clauses
