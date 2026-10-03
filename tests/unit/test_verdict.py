"""Verdict rules for column O (design Appendix A 2.7; docs/contracts_p3.md section 7): truth table and edges.

Offline. Items are built directly with explicit tags, cluster ids, publishers and families, and the depth plan is
built by hand, so these tests depend only on the contracts. The reference predicates below restate the contract
definitions of Q, K and independence; test_predicates_follow_the_contract checks the predicates verdict.py really
uses (rules.is_qualifying, rules.is_corroborating and cluster.independent once those modules exist) against them.
"""

from __future__ import annotations

import importlib.util
import itertools
import random
import re
import sys
from pathlib import Path
from typing import get_args

import pytest

from footprint import verdict as V
from footprint.models import (
    CONTEXT_STRENGTHS,
    ICD203_LIKELIHOOD,
    QUALIFYING_LOCI,
    STRENGTH_ORDER,
    CoverageEntry,
    CoverageStatus,
    DepthPlan,
    EvidenceItem,
    FamilyPlan,
    Indicator,
    Locus,
    SignalTags,
    SourceFamily,
    Tier,
    UsageVerdict,
    VendorFindings,
)

_p = Path(__file__).resolve().parents[1] / "fixtures" / "p3_samples.py"
_spec = importlib.util.spec_from_file_location("p3_samples", _p)
S = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p3_samples", S)
_spec.loader.exec_module(S)

F = SourceFamily
VENDOR = S.VENDOR_ID
OTHER_VENDOR = "V-902"
RETRIEVED = "2026-10-02T12:00:00Z"


# --------------------------------------------------------------------------- contract restated (reference)


def ref_is_q(item: EvidenceItem) -> bool:
    """Q (2.7): verified, SR A/B, SP S2+, RL R2+, RC T1+, class U1-U4, qualifying locus."""
    t = item.tags
    return (item.citable and t.sr in ("A", "B") and t.sp_level >= 2 and t.rl_level >= 2 and t.rc_level >= 1
            and t.u_number <= 4 and t.locus in QUALIFYING_LOCI)


def ref_is_k(item: EvidenceItem) -> bool:
    """The item's own part of K (2.7 + contract interpretation): SR A-C, SP S2+, RL R2+, RC T1+, class U1-U4."""
    t = item.tags
    return (item.citable and t.sr in ("A", "B", "C") and t.sp_level >= 2 and t.rl_level >= 2 and t.rc_level >= 1
            and t.u_number <= 4)


def ref_independent(a: EvidenceItem, b: EvidenceItem) -> bool:
    """Different origin cluster AND (different publisher, case-folded, OR different source family)."""
    return a.cluster_id != b.cluster_id and (a.publisher.casefold() != b.publisher.casefold() or a.family != b.family)


def ref_label(u: str, sr: str, sp: str, rl: str, rc: str, locus: str) -> str:
    """rules.strength_label (2.3 fixed order) with item-local Q and K, as the rules tagger assigns it."""
    level = {"sp": int(sp[1]), "rl": int(rl[1]), "rc": int(rc[1]), "u": int(u[1])}
    common = level["sp"] >= 2 and level["rl"] >= 2 and level["rc"] >= 1 and level["u"] <= 4
    is_q = common and sr in ("A", "B") and locus in QUALIFYING_LOCI
    is_k = common and sr in ("A", "B", "C")
    if u == "U8":
        return "Negative"
    if is_q and rl == "R3":
        return "Strong"
    if is_q or is_k:
        return "Moderate"
    if locus == "relationship":
        return "Context - relationship only"
    if locus == "platform_supplier":
        return "Context - platform supplier"
    if locus == "affiliate_inferred":
        return "Context - inferred affiliate"
    if u == "U7" or sp == "S0":
        return "Marketing only"
    return "Weak"


# --------------------------------------------------------------------------- builders


def ev(name: str, *, u: str = "U1", sr: str = "B", sp: str = "S2", rl: str = "R3", rc: str = "T3",
       locus: str = "service_feature", ai_type: str = "predictive_ml", publisher: str = "Acme Payments",
       family: SourceFamily = F.PRD, source_type: str = "Product page", cluster: str | None = None,
       published: str = "", retrieved: str = RETRIEVED, review: str = "unreviewed", method: str = "rule",
       vendor: str = VENDOR, strength: str | None = None) -> EvidenceItem:
    """One evidence item named `name` (distinct excerpt, URL and, unless given, its own origin cluster)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    tags = SignalTags(u_class=u, sr=sr, sp=sp, rl=rl, rc=rc, ic=2, locus=locus, ai_type=ai_type,
                      strength=strength or ref_label(u, sr, sp, rl, rc, locus))
    return S.item(
        excerpt=f"{name}: Acme Payments screens instant payments with a machine learning model.", start=0,
        url=f"https://www.acme.example/{slug}", vendor_id=vendor, tags=tags, publisher=publisher, family=family,
        source_type=source_type, cluster_id=cluster if cluster is not None else f"cl-{slug}", published=published,
        date_basis="publication" if published else "retrieval", retrieved_at=retrieved, review_status=review,
        method=method,
    )


def q_exact(name: str = "rtp-page", **kw) -> EvidenceItem:
    """Q at R3: first-party product page tying ML to the exact service (Strong)."""
    return ev(name, **kw)


def q_family(name: str = "family-page", **kw) -> EvidenceItem:
    """Q at R2: product-family relevance (Moderate)."""
    kw.setdefault("rl", "R2")
    return ev(name, **kw)


def k_press(name: str = "trade-press", **kw) -> EvidenceItem:
    """K only (SR C): independent trade-press interview on delivery operations."""
    for key, value in dict(u="U3", sr="C", rl="R2", locus="delivery_ops", publisher="Payments Weekly",
                           family=F.IND, source_type="Trade press interview").items():
        kw.setdefault(key, value)
    return ev(name, **kw)


def dns(name: str = "dns-openai", **kw) -> EvidenceItem:
    """DNS verification token: U4, SR B, S1, R2, relationship (Context - relationship only)."""
    for key, value in dict(u="U4", sp="S1", rl="R2", locus="relationship", ai_type="unspecified", family=F.DNS,
                           source_type="DNS TXT record").items():
        kw.setdefault(key, value)
    return ev(name, **kw)


def marketing(name: str = "ai-enabled", **kw) -> EvidenceItem:
    """Marketing claim: U7, S1, R2 (Marketing only); blocks Not detected."""
    for key, value in dict(u="U7", sp="S1", rl="R2", locus="unknown", ai_type="unspecified",
                           source_type="Vendor blog").items():
        kw.setdefault(key, value)
    return ev(name, **kw)


def aspirational(name: str = "plans-ai", **kw) -> EvidenceItem:
    """Statement of planned use: U7, S0 (M2 dominates); counter-evidence, does not block Not detected."""
    for key, value in dict(u="U7", sp="S0", rl="R2", locus="unknown", ai_type="unspecified",
                           source_type="Vendor blog").items():
        kw.setdefault(key, value)
    return ev(name, **kw)


def limiting(name: str = "privacy-notice", **kw) -> EvidenceItem:
    """Limiting statement on the exact service: U8, SR A, R3 (Negative)."""
    for key, value in dict(u="U8", sr="A", sp="S1", rl="R3", family=F.LEG, source_type="Privacy notice").items():
        kw.setdefault(key, value)
    return ev(name, **kw)


MANDATORY = (F.LEG, F.PRD, F.JOB, F.DNS)


def make_plan(mandatory: tuple[SourceFamily, ...] = MANDATORY, vendor: str = VENDOR) -> DepthPlan:
    families = [FamilyPlan(family=f, mandatory=f in mandatory, mode="full" if f in mandatory else "on_lead",
                           cap=10 if f in mandatory else 0, reason="test plan") for f in SourceFamily]
    return DepthPlan(vendor_id=vendor, tier=Tier.HIGH, label="Standard review - tier-driven", families=families,
                     discretionary_fetches=50, gemini_calls=20, analyst_minutes=90, saturation_window=6,
                     reserved_for_meridian=["AI due-diligence questionnaire"])


PLAN = make_plan()


def cov(plan: DepthPlan = PLAN, **status: str) -> list[CoverageEntry]:
    """One Coverage Log entry per mandatory family, 'done' unless overridden (family code -> status)."""
    return [CoverageEntry(vendor_id=plan.vendor_id, family=fp.family, mandatory=True,
                          status=CoverageStatus(status.get(fp.family.value, "done")))
            for fp in plan.families if fp.mandatory]


COMPLETE = cov()


def decide(*items: EvidenceItem, coverage: list[CoverageEntry] | None = None,
           plan: DepthPlan = PLAN) -> UsageVerdict:
    return V.decide(list(items), plan, COMPLETE if coverage is None else coverage)


def keys(*items: EvidenceItem) -> list[str]:
    return [i.item_key for i in items]


def roles(items: list[EvidenceItem], verdict: UsageVerdict) -> dict[str, str]:
    return {i.item_key: i.role for i in V.assign_roles(items, verdict)}


# --------------------------------------------------------------------------- predicates follow the contract


def test_predicates_follow_the_contract():
    base = ev("probe")
    grid = itertools.product(("U1", "U3", "U4", "U5", "U7", "U8"), ("A", "B", "C", "D"), ("S0", "S1", "S2", "S3"),
                             ("R0", "R1", "R2", "R3"), ("T0", "T1", "T3"),
                             ("service_feature", "sdlc", "corporate_internal", "relationship", "platform_supplier"))
    for u, sr, sp, rl, rc, locus in grid:
        tags = SignalTags(u_class=u, sr=sr, sp=sp, rl=rl, rc=rc, ic=2, locus=locus, ai_type="predictive_ml",
                          strength=ref_label(u, sr, sp, rl, rc, locus))
        item = base.model_copy(update={"tags": tags})
        assert bool(V.is_qualifying(item)) == ref_is_q(item), (u, sr, sp, rl, rc, locus)
        assert bool(V.is_corroborating(item)) == ref_is_k(item), (u, sr, sp, rl, rc, locus)


@pytest.mark.parametrize("update", [
    {"review_status": "rejected"},
    {"method": "llm_proposed_accepted"},
    {"tags": S.tags(ai_type="not_ai")},
], ids=["rejected", "pending-proposal", "trap"])
def test_predicates_never_count_items_that_are_not_citable(update):
    item = q_exact().model_copy(update=update)
    assert not item.citable
    assert not V.is_qualifying(item) and not V.is_corroborating(item)


def test_independence_follows_the_contract():
    for (ca, cb), (pa, pb), (fa, fb) in itertools.product(
            [("c1", "c1"), ("c1", "c2")],
            [("Acme Payments", "Acme Payments"), ("Acme Payments", "ACME PAYMENTS"), ("Acme Payments", "Weekly")],
            [(F.PRD, F.PRD), (F.PRD, F.REG)]):
        a = ev("left", cluster=ca, publisher=pa, family=fa)
        b = ev("right", cluster=cb, publisher=pb, family=fb)
        assert bool(V.independent(a, b)) == ref_independent(a, b), (ca, cb, pa, pb, fa, fb)
        assert bool(V.independent(b, a)) == ref_independent(a, b)


# --------------------------------------------------------------------------- rule e/f: coverage


def test_nothing_found_with_complete_coverage_is_not_detected():
    v = decide()
    assert (v.rule, v.label, v.column_o, v.conflict) == ("e", "Not detected", "No", False)
    assert v.likelihood == "unlikely"
    assert v.confidence == "Moderate" and "searched" in v.confidence_reason
    assert v.coverage_complete is True
    assert v.qualifying == v.corroborating == v.decisive == []


@pytest.mark.parametrize("status", ["blocked_robots", "blocked_tou", "blocked_bot", "error", "descoped", "pending"])
def test_incomplete_coverage_blocks_not_detected(status):
    v = decide(coverage=cov(JOB=status))
    assert (v.rule, v.label, v.column_o) == ("f", "Inconclusive", "Inconclusive")
    assert v.coverage_complete is False
    assert v.likelihood == "unlikely"
    assert v.confidence == "Low"
    assert "job postings" in v.confidence_reason and V.STATUS_WORDS[status] in v.confidence_reason
    assert v.decisive == []


def test_a_mandatory_family_without_a_coverage_entry_is_incomplete():
    coverage = [e for e in COMPLETE if e.family != F.DNS]
    v = decide(coverage=coverage)
    assert v.rule == "f" and not v.coverage_complete
    assert "DNS records" in v.confidence_reason and "not logged" in v.confidence_reason


@pytest.mark.parametrize("status", ["done", "done_manual", "not_applicable", "stopped"])
def test_complete_statuses_allow_not_detected(status):
    v = decide(coverage=cov(JOB=status, PRD=status))
    assert v.rule == "e" and v.coverage_complete


def test_a_manual_capture_after_a_block_completes_the_family():
    coverage = cov(JOB="blocked_bot") + [CoverageEntry(vendor_id=VENDOR, family=F.JOB, mandatory=True,
                                                       status=CoverageStatus.DONE_MANUAL)]
    assert decide(coverage=coverage).rule == "e"


def test_a_discretionary_family_does_not_affect_coverage():
    coverage = COMPLETE + [CoverageEntry(vendor_id=VENDOR, family=F.HIST, mandatory=False,
                                         status=CoverageStatus.BLOCKED_ROBOTS)]
    assert decide(coverage=coverage).rule == "e"


def test_coverage_of_another_vendor_is_ignored():
    other = [e.model_copy(update={"vendor_id": OTHER_VENDOR}) for e in COMPLETE]
    assert decide(coverage=other).rule == "f"
    assert decide(coverage=other + COMPLETE).rule == "e"


# --------------------------------------------------------------------------- rule b: Confirmed


def test_q_at_r3_with_independent_k_is_confirmed():
    q, k = q_exact(), k_press()
    v = decide(q, k)
    assert (v.rule, v.label, v.column_o, v.conflict) == ("b", "Confirmed", "Yes", False)
    assert v.likelihood == "very likely"
    assert v.qualifying == keys(q)
    assert v.corroborating == keys(k)
    assert v.decisive == keys(q, k)


def test_same_publisher_in_another_family_is_independent():
    q, filing = q_exact(), q_family("10-k", sr="A", family=F.REG, source_type="SEC Form 10-K")
    v = decide(q, filing)
    assert v.rule == "b"
    assert v.decisive == keys(q, filing)


def test_same_publisher_and_family_is_not_independent():
    q, page = q_exact(), q_family("docs-page")          # both Acme Payments, both PRD, different clusters
    v = decide(q, page)
    assert v.rule == "c" and v.label == "Probable"
    assert v.corroborating == []
    assert v.decisive == keys(q, page)                  # a different statement, cited after independent ones


def test_syndicated_copies_count_once():
    q = q_exact(cluster="cl-release")
    mirror = k_press("partner-mirror", cluster="cl-release", publisher="Partner Wire")   # same origin cluster
    v = decide(q, mirror)
    assert v.rule == "c"                                # the copy is not independent corroboration
    assert v.corroborating == []
    assert v.decisive == keys(q)                        # and column P cites the release once


def test_two_syndicated_copies_are_not_two_corroborations():
    a = k_press("wire-copy-a", cluster="cl-wire", publisher="Wire A")
    b = k_press("wire-copy-b", cluster="cl-wire", publisher="Wire B")
    v = decide(a, b)
    assert v.rule == "f"
    assert len(v.decisive) == 1
    assert v.likelihood == "roughly even chance"


def test_t0_items_are_historical_context_only():
    q = q_exact()
    old_k = k_press(rc="T0")
    assert not ref_is_k(old_k)
    v = decide(q, old_k)
    assert v.rule == "c" and old_k.item_key not in v.decisive + v.corroborating


def test_a_t0_qualifying_statement_does_not_decide_or_block_not_detected():
    old = q_exact("old-page", rc="T0", published="2021-03-01")
    v = decide(old)
    assert v.rule == "e" and v.decisive == []
    assert v.likelihood == "unlikely"
    assert roles([old], v)[old.item_key] == "Logged"
    assert "historical context" in v.trace[-1]


def test_confirmed_relies_on_the_exact_service_q_that_has_independent_support(monkeypatch):
    best = q_exact("best-page", sr="A")                 # ranks first, but nothing independent supports it
    supported = q_exact("other-page", family=F.LEG)
    k = k_press()
    pair = {supported.item_key, k.item_key}
    monkeypatch.setattr(V, "independent", lambda a, b: {a.item_key, b.item_key} == pair)
    v = decide(best, supported, k)
    assert v.rule == "b"
    assert v.decisive[0] == supported.item_key          # the R3 Q the rule relies on is cited first
    assert v.corroborating == keys(k)
    assert v.decisive == keys(supported, k, best)       # then independent items, then the rest
    assert v.qualifying == keys(best, supported)


def test_yes_decisive_cites_up_to_three_items_independent_ones_first():
    q = q_exact()
    sibling = q_family("docs-page")                     # not independent of q (same publisher and family)
    k1 = k_press("press-one")
    k2 = k_press("press-two", publisher="Fintech Daily")
    v = decide(q, sibling, k1, k2)
    assert v.rule == "b"
    assert v.decisive[0] == q.item_key
    assert set(v.decisive[1:]) == {k1.item_key, k2.item_key}
    assert len(v.decisive) == 3
    assert v.qualifying == keys(q, sibling)
    assert set(v.corroborating) == {k1.item_key, k2.item_key}


def test_two_independent_accountable_sources_make_confirmed_almost_certain():
    q = q_exact(sr="A", family=F.LEG, source_type="Product terms")
    filing = q_family("10-k", sr="A", family=F.REG, source_type="SEC Form 10-K")
    v = decide(q, filing)
    assert v.rule == "b" and v.likelihood == "almost certain"
    assert v.confidence == "High"


def test_confirmed_with_one_accountable_source_has_moderate_confidence():
    v = decide(q_exact(), k_press())
    assert v.confidence == "Moderate"
    assert "one" in v.confidence_reason


def test_confirmed_with_incomplete_coverage_has_low_confidence():
    v = decide(q_exact(), q_family("10-k", sr="A", family=F.REG), coverage=cov(DNS="error"))
    assert v.rule == "b" and v.confidence == "Low"
    assert "DNS records" in v.confidence_reason


# --------------------------------------------------------------------------- rule c: Probable


def test_q_at_r2_is_probable():
    q = q_family()
    v = decide(q)
    assert (v.rule, v.label, v.column_o) == ("c", "Probable", "Yes")
    assert v.likelihood == "likely"
    assert v.decisive == keys(q) and v.qualifying == keys(q)


def test_q_at_r3_without_independent_support_is_probable():
    assert decide(q_exact()).rule == "c"


def test_q_at_r2_with_independent_k_is_probable_not_confirmed():
    q, k = q_family(), k_press()
    v = decide(q, k)
    assert v.rule == "c"
    assert v.corroborating == keys(k)
    assert v.decisive == keys(q, k)


def test_two_independent_k_without_q_are_probable():
    a = k_press("press-one")
    b = k_press("press-two", publisher="Fintech Daily")
    v = decide(a, b)
    assert v.rule == "c" and v.column_o == "Yes" and v.likelihood == "likely"
    assert v.qualifying == []
    assert sorted(v.corroborating) == sorted(keys(a, b))
    assert sorted(v.decisive) == sorted(keys(a, b))
    assert v.confidence == "Moderate"                   # two promotional sources, complete coverage


@pytest.mark.parametrize("change", [
    {"sr": "D"}, {"sp": "S1"}, {"rl": "R1"}, {"rc": "T0"}, {"u": "U5"}, {"u": "U7"},
], ids=["aggregator", "s1", "r1", "t0", "u5", "u7"])
def test_k_needs_every_condition(change):
    a = k_press("press-one")
    b = k_press("press-two", publisher="Fintech Daily", **change)
    assert decide(a, b).rule == "f"


@pytest.mark.parametrize("change", [
    {"sr": "C"}, {"sp": "S1"}, {"rl": "R1"}, {"rc": "T0"}, {"u": "U5"}, {"locus": "corporate_internal"},
], ids=["sr-c", "s1", "r1", "t0", "u5", "corporate"])
def test_q_needs_every_condition(change):
    assert not ref_is_q(q_family(**change))
    assert decide(q_family(**change)).rule != "c"


# --------------------------------------------------------------------------- rule a: conflict


def test_later_accountable_limiting_statement_on_the_service_is_a_conflict():
    q = q_exact()
    u8 = limiting(published="2026-10-02")               # same day as the page's retrieval
    v = decide(q, u8, k_press())
    assert (v.rule, v.label, v.column_o, v.conflict) == ("a", "Inconclusive", "Inconclusive", True)
    assert v.decisive == keys(q, u8)
    assert v.confidence == "Low" and "contradict" in v.confidence_reason
    assert v.likelihood == "roughly even chance"
    assert len(v.trace) == 1 and v.trace[0].startswith("a)")


def test_an_older_limiting_statement_is_a_scope_question_not_a_conflict():
    q, k = q_exact(), k_press()
    old = limiting("fact-sheet", published="2025-11-03", source_type="Validation fact sheet")
    v = decide(q, k, old)
    assert v.rule == "b" and not v.conflict
    assert "scope question" in v.trace[0]
    assert roles([q, k, old], v)[old.item_key] == "Negative"


def test_a_conflict_cites_the_claim_and_its_contradiction_even_in_one_cluster():
    q = q_exact(cluster="cl-same")                      # negation barely changes token-set similarity
    u8 = limiting(published="2026-10-02", cluster="cl-same")
    assert decide(q, u8).decisive == keys(q, u8)


@pytest.mark.parametrize("change", [{"rl": "R2"}, {"sr": "C"}, {"rc": "T0"}],
                         ids=["not-exact-service", "not-accountable", "historical"])
def test_only_an_accountable_statement_on_the_exact_service_conflicts(change):
    v = decide(q_exact(), k_press(), limiting(published="2026-10-02", **change))
    assert v.rule == "b"


def test_a_later_product_family_limiting_statement_is_a_scope_question():
    v = decide(q_exact(), k_press(), limiting(published="2026-10-05", rl="R2"))
    assert v.rule == "b" and "scope question" in v.trace[0]


def test_a_rejected_limiting_statement_cannot_conflict():
    v = decide(q_exact(), k_press(), limiting(published="2026-10-02", review="rejected"))
    assert v.rule == "b"


# --------------------------------------------------------------------------- rule d: Affirmed negative


def test_accountable_limiting_statement_without_q_or_k_is_affirmed_negative():
    u8 = limiting()
    v = decide(u8)
    assert (v.rule, v.label, v.column_o) == ("d", "Affirmed negative", "No")
    assert v.likelihood == "very unlikely"
    assert v.decisive == keys(u8)
    assert v.confidence == "Moderate"
    assert roles([u8], v)[u8.item_key] == "Negative"


def test_two_independent_limiting_statements_give_high_confidence():
    a = limiting()
    b = limiting("product-faq", sr="B", rl="R2", family=F.PRD, source_type="Product FAQ")
    v = decide(a, b)
    assert v.rule == "d" and v.confidence == "High"
    assert v.decisive == keys(a, b)


def test_affirmed_negative_does_not_need_complete_coverage():
    v = decide(limiting(), coverage=cov(PRD="blocked_bot"))
    assert v.rule == "d" and v.confidence == "Low"


def test_a_corroborating_signal_blocks_affirmed_negative():
    u8, k = limiting(), k_press()
    v = decide(u8, k)
    assert v.rule == "f"
    assert v.decisive == keys(u8, k)                    # label order: Negative, then Moderate


@pytest.mark.parametrize("change", [{"sr": "C"}, {"rl": "R1"}, {"rc": "T0"}], ids=["sr-c", "r1", "t0"])
def test_affirmed_negative_needs_a_recent_accountable_statement_covering_the_service(change):
    v = decide(limiting(**change))
    assert v.rule == "e"


def test_a_limiting_statement_from_a_promotional_source_is_cited_for_not_detected():
    u8 = limiting("press-quote", sr="C", family=F.IND, publisher="Payments Weekly")
    v = decide(u8)
    assert v.rule == "e" and v.decisive == keys(u8)
    assert roles([u8], v)[u8.item_key] == "Negative"


# --------------------------------------------------------------------------- rule e/f: indicators


def test_dns_only_is_inconclusive():
    openai, anthropic = dns("dns-openai"), dns("dns-anthropic")
    v = decide(openai, anthropic)
    assert (v.rule, v.label, v.column_o) == ("f", "Inconclusive", "Inconclusive")
    assert v.likelihood == "roughly even chance"
    assert v.confidence == "Moderate"
    assert sorted(v.decisive) == sorted(keys(openai, anthropic))
    assert set(roles([openai, anthropic], v).values()) == {"Indicator"}


def test_dns_only_with_incomplete_coverage_has_low_confidence():
    v = decide(dns(), coverage=cov(LEG="blocked_tou"))
    assert v.rule == "f" and v.likelihood == "roughly even chance" and v.confidence == "Low"


def test_marketing_only_is_inconclusive():
    m = marketing()
    v = decide(m)
    assert v.rule == "f" and v.column_o == "Inconclusive"
    assert v.likelihood == "roughly even chance"
    assert v.decisive == keys(m)
    assert roles([m], v)[m.item_key] == "Indicator"


def test_aspirational_wording_is_counter_evidence_for_not_detected():
    s0 = aspirational()
    v = decide(s0)
    assert v.rule == "e" and v.column_o == "No"
    assert v.decisive == keys(s0)
    assert roles([s0], v)[s0.item_key] == "Counter-evidence"


@pytest.mark.parametrize("u", ["U5", "U6", "U8"])
def test_capability_governance_and_limiting_items_do_not_block_not_detected(u):
    v = decide(ev("other-class", u=u, sp="S2", rl="R2", sr="C"))
    assert v.rule == "e"


@pytest.mark.parametrize("change", [{"rl": "R1"}, {"rl": "R0", "locus": "commentary"}, {"rc": "T0"}])
def test_irrelevant_or_old_marketing_does_not_block_not_detected(change):
    assert decide(marketing(**change)).rule == "e"


def test_inferred_affiliate_alone_does_not_block_not_detected():
    affiliate = ev("affiliate", u="U3", sr="C", rl="R1", locus="affiliate_inferred", publisher="Affiliate AI")
    assert decide(affiliate).rule == "e"
    v = decide(affiliate, coverage=cov(PRD="blocked_bot"))
    assert v.rule == "f" and v.decisive == keys(affiliate) and v.likelihood == "unlikely"


def test_inconclusive_likelihood_needs_an_item_that_blocks_not_detected():
    governance = ev("ai-policy", u="U6", sp="S1", rl="R2")
    v = decide(governance, coverage=cov(PRD="blocked_bot"))
    assert v.rule == "f" and v.likelihood == "unlikely"


def test_inconclusive_decisive_items_follow_label_order_then_sr_rl_rc():
    items = [
        marketing("mkt-a-r3", sr="A", rl="R3"),
        dns("dns-token", sr="B"),
        marketing("mkt-c", sr="C"),
        marketing("mkt-b", sr="B"),
    ]
    v = decide(*items)
    assert v.decisive == keys(items[1], items[0])       # Context before Marketing only, then SR A first
    a, b = marketing("same-sr-r3", rl="R3"), marketing("same-sr-r2", rl="R2")
    assert decide(b, a).decisive == keys(a, b)          # same label and SR: RL R3 first
    c, d = marketing("same-rl-t3", rc="T3"), marketing("same-rl-t1", rc="T1")
    assert decide(d, c).decisive == keys(c, d)          # same label, SR and RL: RC T3 first
    e, f = marketing("tie-one"), marketing("tie-two")
    assert decide(f, e).decisive == sorted(keys(e, f))  # full tie: item_key


def test_inconclusive_cites_vendor_signals_before_platform_supplier_context():
    supplier = ev("platform-spd", u="U2", sr="A", sp="S3", rl="R1", locus="platform_supplier",
                  publisher="Platform Maker", family=F.LEG, source_type="Product terms")
    affiliate = ev("affiliate-neo", u="U3", sr="C", rl="R1", locus="affiliate_inferred", publisher="Affiliate AI")
    claim = marketing("human-led-ai-enabled", sr="C")
    assert supplier.strength == "Context - platform supplier"
    assert affiliate.strength == "Context - inferred affiliate"
    v = decide(supplier, affiliate, claim)
    assert v.rule == "f"
    assert v.decisive == keys(affiliate, claim)         # design 5, V-001: affiliate line + marketing line
    assert roles([supplier, affiliate, claim], v) == {
        supplier.item_key: "Context", affiliate.item_key: "Indicator", claim.item_key: "Indicator"}
    v2 = decide(supplier, claim)
    assert v2.decisive == keys(claim, supplier)         # supplier context fills a remaining slot


def test_inconclusive_never_cites_commentary_excluded_sources_or_t0():
    blocker = marketing("blocker")
    commentary = ev("industry-trends", u="U7", sp="S1", rl="R0", locus="commentary")
    forum = marketing("forum-post", sr="E")
    old = dns("old-token", rc="T0")
    v = decide(blocker, commentary, forum, old)
    assert v.rule == "f" and v.decisive == keys(blocker)


def test_inconclusive_cites_one_item_per_origin_cluster():
    a = marketing("release", cluster="cl-pr")
    b = marketing("release-mirror", cluster="cl-pr", publisher="Wire")
    c = marketing("other-claim", sr="C")
    v = decide(a, b, c)
    assert len(v.decisive) == 2 and c.item_key in v.decisive
    assert len({a.item_key, b.item_key} & set(v.decisive)) == 1


@pytest.mark.parametrize("srs, expected", [
    (("D",), "Low"), (("C",), "Low"), (("C", "C"), "Moderate"), (("B",), "Moderate"), (("A", "B"), "Moderate"),
])
def test_inconclusive_confidence_follows_the_cited_sources(srs, expected):
    items = [marketing(f"claim-{n}", sr=sr) for n, sr in enumerate(srs)]
    v = decide(*items)
    assert v.rule == "f" and v.confidence == expected


def test_aggregator_only_evidence_gives_low_confidence():
    v = decide(marketing("aggregator", sr="D"))
    assert v.confidence == "Low" and "aggregat" in v.confidence_reason


# --------------------------------------------------------------------------- what counts


def test_rejected_items_never_count():
    q = q_exact(review="rejected")
    k = k_press()
    v = decide(q, k)
    assert v.rule == "f" and q.item_key not in v.qualifying + v.decisive
    assert roles([q, k], v)[q.item_key] == "Logged"


def test_accepted_and_unreviewed_verified_items_count():
    assert decide(q_exact(review="accepted"), k_press()).rule == "b"
    assert decide(q_exact(review="unreviewed"), k_press()).rule == "b"


def test_llm_proposals_count_only_after_acceptance():
    proposal = q_exact(method="llm_proposed_accepted")
    assert decide(proposal, k_press()).rule == "f"
    accepted = q_exact(method="llm_proposed_accepted", review="accepted")
    assert decide(accepted, k_press()).rule == "b"


def test_definition_test_traps_never_count():
    trap = ev("intelligent-tools", u="U7", sp="S1", rl="R2", ai_type="not_ai", strength="Marketing only")
    v = decide(trap)
    assert v.rule == "e" and v.decisive == []
    assert roles([trap], v)[trap.item_key] == "Logged"


def test_items_of_other_vendors_are_ignored():
    v = decide(q_exact(vendor=OTHER_VENDOR), k_press(vendor=OTHER_VENDOR))
    assert v.rule == "e" and v.qualifying == []


def test_duplicate_item_keys_count_once():
    q = q_exact()
    v = decide(q, q.model_copy())
    assert v.rule == "c" and v.qualifying == keys(q)


# --------------------------------------------------------------------------- truth table


TRUTH_TABLE = [
    # name, items, coverage overrides, rule, column O, likelihood
    ("empty-complete", lambda: [], {}, "e", "No", "unlikely"),
    ("empty-incomplete", lambda: [], {"PRD": "blocked_bot"}, "f", "Inconclusive", "unlikely"),
    ("q-r3+k", lambda: [q_exact(), k_press()], {}, "b", "Yes", "very likely"),
    ("q-r3+k-incomplete", lambda: [q_exact(), k_press()], {"JOB": "error"}, "b", "Yes", "very likely"),
    ("q-r3", lambda: [q_exact()], {}, "c", "Yes", "likely"),
    ("q-r2", lambda: [q_family()], {}, "c", "Yes", "likely"),
    ("2k", lambda: [k_press("a"), k_press("b", publisher="Other")], {}, "c", "Yes", "likely"),
    ("1k", lambda: [k_press()], {}, "f", "Inconclusive", "roughly even chance"),
    ("conflict", lambda: [q_exact(), limiting(published="2026-10-02")], {}, "a", "Inconclusive",
     "roughly even chance"),
    ("u8", lambda: [limiting()], {}, "d", "No", "very unlikely"),
    ("u8-incomplete", lambda: [limiting()], {"DNS": "descoped"}, "d", "No", "very unlikely"),
    ("dns", lambda: [dns()], {}, "f", "Inconclusive", "roughly even chance"),
    ("marketing", lambda: [marketing()], {}, "f", "Inconclusive", "roughly even chance"),
    ("aspirational", lambda: [aspirational()], {}, "e", "No", "unlikely"),
    ("aspirational-incomplete", lambda: [aspirational()], {"LEG": "blocked_robots"}, "f", "Inconclusive",
     "unlikely"),
    ("t0-only", lambda: [q_exact(rc="T0")], {}, "e", "No", "unlikely"),
]


@pytest.mark.parametrize("name, build, status, rule, column_o, likelihood", TRUTH_TABLE,
                         ids=[row[0] for row in TRUTH_TABLE])
def test_truth_table(name, build, status, rule, column_o, likelihood):
    v = decide(*build(), coverage=cov(**status))
    assert (v.rule, v.column_o, v.likelihood) == (rule, column_o, likelihood)
    assert v.label == {"a": "Inconclusive", "b": "Confirmed", "c": "Probable", "d": "Affirmed negative",
                       "e": "Not detected", "f": "Inconclusive"}[rule]
    assert v.likelihood in ICD203_LIKELIHOOD
    assert v.coverage_complete is (not status)
    assert [line[:2] for line in v.trace] == [f"{r})" for r in "abcdef"[: "abcdef".index(rule) + 1]]
    if not status or rule == "a":
        pass
    else:
        assert v.confidence == "Low"


@pytest.mark.parametrize("name, build, status, rule, column_o, likelihood", TRUTH_TABLE,
                         ids=[row[0] for row in TRUTH_TABLE])
def test_confidence_reason_is_a_plain_clause(name, build, status, rule, column_o, likelihood):
    reason = decide(*build(), coverage=cov(**status)).confidence_reason
    assert reason and reason[0].islower() and not reason.endswith(".")
    assert not re.search(r"\b(?:U[1-8]|S[0-3]|R[0-3]|T[0-3]|SR|RL|RC|SP|IC|LEG|REG|PRD|JOB|IND|HIST|EXEC|Q|K)\b",
                         reason), reason


# --------------------------------------------------------------------------- properties over random evidence


def _random_items(rng: random.Random, n: int) -> list[EvidenceItem]:
    builders = [q_exact, q_family, k_press, dns, marketing, aspirational, limiting]
    items = []
    for i in range(n):
        kw: dict = {}
        if rng.random() < 0.3:
            kw["cluster"] = rng.choice(["cl-x", "cl-y"])
        if rng.random() < 0.2:
            kw["rc"] = rng.choice(["T0", "T1", "T2"])
        if rng.random() < 0.1:
            kw["review"] = "rejected"
        if rng.random() < 0.3:
            kw["published"] = rng.choice(["2025-11-03", "2026-10-02", "2026-10-05"])
        if rng.random() < 0.3:
            kw["publisher"] = rng.choice(["Acme Payments", "Payments Weekly", "Wire"])
        if rng.random() < 0.1:
            kw["vendor"] = OTHER_VENDOR
        items.append(rng.choice(builders)(f"item-{i}", **kw))
    return items


def test_verdicts_respect_the_rules_on_random_evidence():
    rng = random.Random(20261002)
    by_key: dict[str, EvidenceItem] = {}
    for _ in range(300):
        items = _random_items(rng, rng.randint(0, 7))
        coverage = cov(**({"PRD": "blocked_bot"} if rng.random() < 0.3 else {}))
        v = decide(*items, coverage=coverage)
        pool = [i for i in items if i.citable and i.vendor_id == VENDOR]
        by_key = {i.item_key: i for i in pool}
        qs = [i for i in pool if ref_is_q(i)]
        ks = [i for i in pool if ref_is_k(i)]
        k_pair = any(ref_independent(a, b) for a in ks for b in ks if a.item_key != b.item_key)
        assert v.conflict is (v.rule == "a")
        if qs:
            assert v.rule in ("a", "b", "c")
        elif k_pair:
            assert v.rule == "c"
        else:
            assert v.rule not in ("b", "c")
        if v.rule == "b":
            anchor = by_key[v.decisive[0]]
            assert ref_is_q(anchor) and anchor.tags.rl == "R3"
            assert any(ref_independent(anchor, k) for k in ks if k.item_key != anchor.item_key)
        if v.rule == "e":
            assert v.coverage_complete
        cited = v.qualifying + v.corroborating + v.decisive
        assert set(cited) <= set(by_key)                # only citable items of the plan's vendor
        assert v.qualifying == [k for k in v.qualifying if ref_is_q(by_key[k])]
        decisive = [by_key[k] for k in v.decisive]
        if v.rule != "a":                               # a claim and its contradiction are both cited, even when
            assert len({i.cluster_id for i in decisive}) == len(decisive)   # similarity put them in one cluster
        assert len(decisive) <= (3 if v.column_o == "Yes" else 2 if v.label == "Inconclusive" else len(pool))
        assert all(i.tags.rc != "T0" for i in decisive if v.rule not in ("a",))
        assert len(v.trace) == "abcdef".index(v.rule) + 1
        shuffled = items[:]
        rng.shuffle(shuffled)
        assert decide(*shuffled, coverage=coverage) == v


# --------------------------------------------------------------------------- determinism and inputs


def test_input_order_never_matters():
    items = [q_exact(), q_family("docs-page"), k_press("press-one"), k_press("press-two", publisher="Daily"),
             dns(), marketing(), limiting("fact-sheet", published="2025-11-03"), aspirational()]
    first = decide(*items).model_dump()
    rng = random.Random(1234)
    for _ in range(10):
        shuffled = items[:]
        rng.shuffle(shuffled)
        assert decide(*shuffled).model_dump() == first


def test_decide_does_not_change_its_inputs():
    items = [q_exact(), k_press()]
    before = [i.model_dump() for i in items]
    coverage = cov()
    decide(*items, coverage=coverage)
    assert [i.model_dump() for i in items] == before


def test_decide_accepts_any_iterables():
    v = V.decide(iter([q_exact(), k_press()]), PLAN, iter(COMPLETE))
    assert v.rule == "b"


def test_decide_works_with_a_planner_depth_plan():
    plan = S.depth()
    coverage = [CoverageEntry(vendor_id=plan.vendor_id, family=fp.family, mandatory=True,
                              status=CoverageStatus.DONE) for fp in plan.families if fp.mandatory]
    assert V.decide([], plan, coverage).rule == "e"
    assert V.decide([], plan, coverage[1:]).rule == "f"


def test_trace_names_each_rule_tested_in_order():
    v = decide(dns())
    assert [line[:2] for line in v.trace] == ["a)", "b)", "c)", "d)", "e)", "f)"]
    assert all(line.strip() == line and line.endswith(".") for line in v.trace)
    assert v.trace[-1].startswith("f) Inconclusive: yes")


# --------------------------------------------------------------------------- roles


def test_roles_for_a_yes_verdict():
    q = q_exact()
    sibling = q_family("docs-page")
    k = k_press()
    copy = k_press("press-copy", cluster=k.cluster_id, publisher="Wire Copy", rc="T2")  # older copy of k
    token = dns()
    old_limit = limiting("fact-sheet", published="2025-11-03")
    rejected = q_exact("rejected-page", review="rejected")
    weak = ev("weak-line", u="U5", sp="S1", rl="R1", locus="unknown")
    items = [weak, rejected, old_limit, token, copy, k, sibling, q]
    v = decide(*items)
    assert v.rule == "b"
    got = V.assign_roles(items, v)
    assert [i.item_key for i in got] == keys(*items)                    # input order
    assert all(a is not b for a, b in zip(got, items))                  # copies
    assert {i.item_key: i.role for i in got} == {
        q.item_key: "Primary", sibling.item_key: "Supporting", k.item_key: "Supporting",
        copy.item_key: "Logged", token.item_key: "Context", old_limit.item_key: "Negative",
        rejected.item_key: "Logged", weak.item_key: "Logged",
    }
    assert [i.role for i in items] == ["Logged"] * len(items)           # inputs unchanged


def test_roles_for_a_conflict():
    q, u8 = q_exact(), limiting(published="2026-10-02")
    assert roles([q, u8], decide(q, u8)) == {q.item_key: "Indicator", u8.item_key: "Negative"}


def test_roles_never_cite_an_item_that_is_no_longer_citable():
    q, k = q_exact(), k_press()
    v = decide(q, k)
    rejected_q = q.model_copy(update={"review_status": "rejected"})
    assert roles([rejected_q, k], v)[q.item_key] == "Logged"


def test_roles_use_only_cited_roles_for_decisive_items():
    for build, status in [(row[1], row[2]) for row in TRUTH_TABLE]:
        items = build()
        v = decide(*items, coverage=cov(**status))
        got = roles(items, v)
        for key in v.decisive:
            assert got[key] != "Logged"
        assert all(got[i.item_key] in ("Context", "Logged") for i in items
                   if i.item_key not in set(v.decisive) | set(v.qualifying) | set(v.corroborating)
                   and i.tags.u_class != "U8")


def test_context_role_needs_a_context_label():
    token = dns()
    assert token.strength in CONTEXT_STRENGTHS
    v = decide(q_exact(), k_press(), token)
    assert roles([token], v)[token.item_key] == "Context"


# --------------------------------------------------------------------------- fits the findings model


def test_verdict_and_roles_fit_vendor_findings():
    q, k, token = q_exact(), k_press(), dns()
    v = decide(q, k, token)
    evidence = V.assign_roles([q, k, token], v)
    inputs = S.risk_inputs(e_items=keys(q), k_items=keys(q))
    findings = S.findings(evidence=evidence, verdict=v, risk=S.risk_result(inputs=inputs))
    assert isinstance(findings, VendorFindings)
    assert [i.role for i in findings.cited()] == ["Primary", "Supporting", "Context"]


def test_rank_key_orders_by_label_then_sr_rl_rc_then_key():
    items = [marketing("m"), dns("d"), q_exact("q"), limiting("u"), k_press("k")]
    ranked = sorted(items, key=V.rank_key)
    assert [i.strength for i in ranked] == ["Negative", "Strong", "Moderate", "Context - relationship only",
                                            "Marketing only"]
    assert [STRENGTH_ORDER.index(i.strength) for i in ranked] == sorted(STRENGTH_ORDER.index(i.strength)
                                                                         for i in items)


def _affiliate(name: str, excerpt: str, *, u: str, key: str, **update) -> EvidenceItem:
    """An inferred-affiliate item (Context label, SR C, R1, T3) with a chosen item_key, in one origin cluster."""
    item = ev(name, u=u, sr="C", sp="S1", rl="R1", rc="T3", locus="affiliate_inferred", publisher="Labarum AI",
              family=F.IND, source_type="Affiliate website", cluster="cl-labarum", ai_type="genai_llm")
    return item.model_copy(update={"excerpt": excerpt, "end": len(excerpt), "item_key": key, **update})


def test_rank_key_breaks_ties_on_what_the_item_says_not_on_its_hash():
    # V-001 regression: every inferred-affiliate item tied on label, SR C, R1 and T3, so column P quoted the item
    # with the lowest hash, a slogan, instead of the NEO platform claim.
    slogan = _affiliate("slogan", "Public-cloud AI services are extraordinary at general-purpose work.", u="U7",
                        key="0" * 64)
    neo = _affiliate("neo", "NEO is the customer-side AI platform that powers Labarum AI engagements.", u="U2",
                     key="f" * 64)
    assert slogan.strength == neo.strength == "Context - inferred affiliate"
    assert sorted([slogan, neo], key=V.rank_key) == [neo, slogan]
    v = decide(slogan, neo, coverage=cov(JOB="blocked_robots"))   # AutomWorx: no ATS, so Inconclusive
    assert v.label == "Inconclusive" and v.decisive == [neo.item_key]   # one item per origin cluster
    # Then where the AI sits, specificity, genuine-use indicators and named providers, before the hash.
    base = dict(u="U3", sr="A", sp="S2", rl="R2", locus="sdlc", family=F.REG, source_type="SEC Form DEFA14A")
    dev = ev("code-generation", **base).model_copy(update={"item_key": "0" * 64})
    desk = ev("service-desk", **{**base, "locus": "delivery_ops"}).model_copy(update={"item_key": "f" * 64})
    assert sorted([dev, desk], key=V.rank_key) == [desk, dev]
    vague = ev("vague", **base).model_copy(update={"item_key": "0" * 64})
    specific = ev("specific", **{**base, "sp": "S3"}).model_copy(update={"item_key": "f" * 64})
    assert sorted([vague, specific], key=V.rank_key) == [specific, vague]
    plain = vague
    named = vague.model_copy(update={"item_key": "f" * 64, "providers": ["GitHub Copilot"],
                                     "indicators": [Indicator(code="G1", span="GitHub Copilot")]})
    assert sorted([plain, named], key=V.rank_key) == [named, plain]


def test_locus_rank_covers_every_locus():
    assert set(V.LOCUS_RANK) == set(get_args(Locus))


def test_item_date_prefers_publication_then_retrieval():
    assert V.item_date(q_exact(published="2025-11-03")) == "2025-11-03"
    assert V.item_date(q_exact()) == "2026-10-02"
    assert V.item_date(q_exact(published="2025-13-45")) == "2026-10-02"   # malformed: retrieval date
