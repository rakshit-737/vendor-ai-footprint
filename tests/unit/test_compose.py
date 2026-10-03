"""compose.py (docs/contracts_p3.md §11; design 2.8 cell templates, 2.11 sheets): cells L-V and the appended sheets.

All findings are fabricated (fictional vendor V-901 from tests/fixtures/p3_samples.py). Offline: no network, no Gemini.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from footprint.compose import (
    CLOSING,
    EVIDENCE_IMAGES_TITLE,
    EVIDENCE_LOG_HEADERS,
    EVIDENCE_LOG_TITLE,
    NO_EXCERPT,
    P_PREFIXES,
    RUN_INFO_TITLE,
    SEP,
    assign_evidence_ids,
    build_cells,
    canonical_provider,
    coverage_ids,
    coverage_log_sheet,
    display_quote,
    display_url,
    evidence_images_sheet,
    evidence_log_sheet,
    evidence_status,
    method_legend_p3,
    render_assessed_by,
    run_info_sheet,
)
from footprint.models import (
    STRENGTH_ORDER,
    STUDENT_FIELDS,
    CoverageStatus,
    EvidenceItem,
    Indicator,
    SheetSpec,
    SourceFamily,
    StudentCells,
    VendorFindings,
)
from footprint.sheets import COVERAGE_HEADERS, LEGEND_HEADERS, METHOD_LEGEND_TITLE
from footprint.workbook import DEFAULT_LENGTH_BUDGETS

_p = Path(__file__).resolve().parents[1] / "fixtures" / "p3_samples.py"
_spec = importlib.util.spec_from_file_location("p3_samples", _p)
S = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p3_samples", S)
_spec.loader.exec_module(S)

INPUT = Path(__file__).resolve().parents[2] / "data" / "input" / "Meridian_Vendor_Input.xlsx"
V = S.VENDOR_ID
TEAM = "Team Osprey"
DAY = "2026-10-02"
F = SourceFamily
CS = CoverageStatus

IND_URL = "https://press.example/acme-interview"
IND_EXCERPT = "Acme Payments uses machine learning to flag anomalies on real-time rails, its head of payments said."
DECK_URL = "https://www.acme.example/ir/q1-deck.pdf"
DECK_EXCERPT = "Our assistant platform integrates models from OpenAI and Anthropic for operations staff."
DNS_URL = "https://dns.google/resolve?name=acme.example&type=TXT"
OPENAI_TOKEN = "openai-domain-verification=dv-AbCdEf123456"
COHERE_TOKEN = "cohere-domain-verification=Zx9Yw8Vu7"
BLOG_URL = "https://www.acme.example/blog/ai-ready"
BLOG_EXCERPT = "We are exploring how AI could help our clients in the future."
TRAP_URL = "https://www.acme.example/news/intelligent-tools"
TRAP_EXCERPT = "Our intelligent tools automate statement inserting at scale."
REJECTED_URL = "https://aggregator.example/acme-ai"
REJECTED_EXCERPT = "Acme Payments runs every payment through an AI engine, according to a summary."
COUNTER_URL = "https://print.example/profile-acme"
COUNTER_EXCERPT = "Acme Payments says it has no plans to use AI in statement composition this year."
PRIVACY_URL = "https://www.acme.example/privacy"
LIMITING_EXCERPT = "Acme Payments does not use artificial intelligence to make decisions about payments."
U_TEXT = ("Issue a targeted questionnaire within 15 business days covering sub-processors, training use and incident "
          "notification. Register the vendor on Meridian's AI sub-processor inventory. Raise AI clauses at the next "
          "contract review. Escalate to the Third-Party Risk Committee if the vendor does not confirm.")
FLIP = "External models processing Meridian payment data (exposure 3) would raise the class to Critical."

ALL_GAPS_MISSING = {g: True for g in ("t1", "t2", "t3", "t4", "t5", "t6")}
TAG_CODE = re.compile(r"\b(?:U[1-8]|S[0-3]|R[0-3]|T[0-3]|G(?:1[01]|[1-9])|M[1-7]|X[1-6]|RT[1-7]|IC\s?[1-6])\b"
                      r"|SR:|SP:|RL:|RC:|IC:|locus=")
HASH = re.compile(r"\b[0-9a-f]{64}\b")


# --------------------------------------------------------------------------- builders


def _item(url: str, excerpt: str, **kw) -> EvidenceItem:
    return S.item(url=url, excerpt=excerpt, start=0, **kw)


def _dns(provider: str, record: str, **kw) -> EvidenceItem:
    base = dict(family=F.DNS, source_type="DNS TXT record", publisher="Acme Payments", passage_id="",
                providers=[provider], indicators=[], data_mentioned=[], action_level="unknown", temporal="unclear",
                tags=S.tags(u_class="U4", sp="S1", rl="R2", locus="relationship", ai_type="unspecified",
                            strength="Context - relationship only"))
    base.update(kw)
    return _item(DNS_URL, record, **base)


def _marketing(**kw) -> EvidenceItem:
    base = dict(source_type="Blog post", publisher="Acme Payments", published="2025-11-03",
                date_basis="wordpress date", indicators=[], data_mentioned=[], action_level="unknown",
                temporal="planned", tags=S.tags(u_class="U7", sr="C", sp="S0", rl="R1", locus="unknown",
                                                ai_type="unspecified", strength="Marketing only"))
    base.update(kw)
    return _item(BLOG_URL, BLOG_EXCERPT, **base)


def _trap(**kw) -> EvidenceItem:
    base = dict(source_type="Press release", publisher="Acme Payments", role="Logged", indicators=[],
                data_mentioned=[], tags=S.tags(u_class="U7", sr="C", sp="S1", rl="R1", locus="unknown",
                                               ai_type="not_ai", strength="Marketing only"))
    base.update(kw)
    return _item(TRAP_URL, TRAP_EXCERPT, **base)


def _limiting(**kw) -> EvidenceItem:
    base = dict(family=F.LEG, source_type="Privacy notice", publisher="Acme Payments", indicators=[],
                data_mentioned=[], action_level="none", temporal="in_production", role="Negative",
                tags=S.tags(u_class="U8", sr="A", sp="S2", rl="R3", strength="Negative"))
    base.update(kw)
    return _item(PRIVACY_URL, LIMITING_EXCERPT, **base)


def _cov(family: SourceFamily, status: CoverageStatus, documents: int, ai_passages: int, collector: str = "site",
         mandatory: bool = True) -> object:
    return S.coverage(family=family, status=status, documents=documents, ai_passages=ai_passages,
                      collector=collector, mandatory=mandatory)


def _yes_coverage() -> list:
    return [
        _cov(F.LEG, CS.DONE, 9, 0),                       # C-01: no AI terms on legal pages
        _cov(F.REG, CS.STOPPED, 3, 4, "sec"),             # C-02
        _cov(F.PRD, CS.STOPPED, 40, 12),                  # C-03
        _cov(F.JOB, CS.DONE, 8, 0, "jobs"),               # C-04: no AI duties in postings
        _cov(F.DNS, CS.DONE, 1, 2, "dns"),                # C-05
        _cov(F.IND, CS.DONE, 3, 5, "seeds"),              # C-06
        _cov(F.HIST, CS.DONE, 0, 0, "wayback"),           # C-07: dating only, never a negative finding
    ]


def _by_excerpt(findings: VendorFindings) -> dict[str, EvidenceItem]:
    return {item.excerpt: item for item in findings.evidence}


def _yes(**over) -> VendorFindings:
    """Confirmed (rule b): a first-party R3 Q, an independent SR C K, a second pathway naming providers, a DNS
    token, plus a marketing item, a definition-test trap and an analyst-rejected item that must never be cited."""
    primary = S.item(role="Primary")
    support = _item(IND_URL, IND_EXCERPT, family=F.IND, source_type="Trade press interview",
                    publisher="Payments Weekly", published="2026-05-14", date_basis="json-ld datePublished",
                    role="Supporting", indicators=[], providers=["Google"],
                    tags=S.tags(sr="C", rl="R2", strength="Moderate"))
    named = _item(DECK_URL, DECK_EXCERPT, source_type="Investor presentation", publisher="Acme Payments",
                  published="2026-04-20", date_basis="pdf CreationDate", role="Supporting",
                  providers=["OpenAI", "Anthropic"], data_mentioned=[], action_level="advisory", indicators=[],
                  tags=S.tags(u_class="U3", rl="R2", locus="delivery_ops", ai_type="genai_llm", strength="Moderate"))
    dns = _dns("Cohere", COHERE_TOKEN, role="Context")
    rejected = _item(REJECTED_URL, REJECTED_EXCERPT, review_status="rejected",
                     review_reason="summary by an aggregator, not vendor text", reviewer="RK 2026-10-08")
    items = assign_evidence_ids([primary, support, named, dns, _marketing(role="Logged"), _trap(), rejected], V)
    verdict = S.verdict(keys=None, qualifying=[primary.item_key, named.item_key], corroborating=[support.item_key],
                        decisive=[primary.item_key, support.item_key, named.item_key])
    inputs = S.risk_inputs(e_items=[primary.item_key], k_items=[primary.item_key],
                           gap_items={"t1": [primary.item_key], "t2": [named.item_key]},
                           gaps={"t1": False, "t2": False, "t3": True, "t4": False, "t5": True, "t6": True},
                           gap_reasons={})
    base = dict(evidence=items, coverage=_yes_coverage(), verdict=verdict,
                risk=S.risk_result(inputs=inputs, flip_condition=FLIP), actions=S.action_plan(text=U_TEXT))
    base.update(over)
    return S.findings(**base)


def _inconclusive(**over) -> VendorFindings:
    """Rule f: a DNS token and a marketing statement are decisive; legal pages partly failed, product pages
    blocked by bot protection."""
    dns = _dns("OpenAI", OPENAI_TOKEN, role="Indicator")
    marketing = _marketing(role="Indicator")
    items = assign_evidence_ids([dns, marketing, _trap()], V)
    verdict = S.verdict(keys=None, rule="f", likelihood="roughly even chance", confidence="Low",
                        confidence_reason="only relationship indicators were found and coverage of the mandatory "
                                          "families is incomplete",
                        qualifying=[], corroborating=[], decisive=[dns.item_key, marketing.item_key],
                        coverage_complete=False)
    inputs = S.risk_inputs(e=2, k=2, tp=3, e_assumed=True, k_assumed=True, e_items=[], k_items=[], gap_items={},
                           gaps=dict(ALL_GAPS_MISSING), gap_reasons={})
    risk = S.risk_result(inputs=inputs, arp=14, base_class="Critical", gate_met=False, cap="Medium",
                         pre_cap_class="Medium", final_class="Medium", provisional=True, ceiling_class="Critical",
                         flip_condition="Confirmation that staff use generative AI on Meridian data would raise the "
                                        "class to High.")
    coverage = [
        _cov(F.LEG, CS.ERROR, 0, 0, "seeds"),             # C-01
        _cov(F.LEG, CS.DONE, 0, 0),                       # C-02
        _cov(F.PRD, CS.BLOCKED_BOT, 0, 0),                # C-03
        _cov(F.DNS, CS.DONE, 1, 2, "dns"),                # C-04
        _cov(F.REG, CS.NOT_APPLICABLE, 0, 0, "sec"),      # C-05
        _cov(F.JOB, CS.NOT_APPLICABLE, 0, 0, "jobs"),     # C-06
        _cov(F.IND, CS.DONE, 1, 0, "seeds"),              # C-07
    ]
    base = dict(evidence=items, coverage=coverage, verdict=verdict, risk=risk,
                actions=S.action_plan(text="Send questionnaire items within 15 business days. Reclassify on "
                                           "response."))
    base.update(over)
    return S.findings(**base)


def _no_risk(**kw):
    inputs = S.risk_inputs(e=0, k=0, tp=3, e_items=[], k_items=[], gap_items={}, gaps=dict(ALL_GAPS_MISSING),
                           gap_reasons={})
    base = dict(inputs=inputs, arp=6, base_class="Medium", gate_met=False, cap="None identified",
                pre_cap_class="Medium", final_class="None identified", provisional=False, ceiling_class="",
                flip_condition="Generative AI in the composition tools touching Meridian data would require "
                               "reassessment.")
    base.update(kw)
    return S.risk_result(**base)


def _not_detected(**over) -> VendorFindings:
    """Rule e: complete coverage, one counter-evidence statement of planned use."""
    counter = _item(COUNTER_URL, COUNTER_EXCERPT, family=F.IND, source_type="Trade press profile",
                    publisher="Print World", published="2026-03-02", date_basis="json-ld datePublished",
                    role="Counter-evidence", indicators=[], data_mentioned=[], action_level="unknown",
                    temporal="planned", tags=S.tags(u_class="U7", sr="C", sp="S0", rl="R2", locus="unknown",
                                                    ai_type="unspecified", strength="Marketing only"))
    items = assign_evidence_ids([counter, _trap()], V)
    verdict = S.verdict(keys=None, rule="e", likelihood="unlikely", confidence="Moderate",
                        confidence_reason="the mandatory families were searched completely and nothing qualifying "
                                          "was found",
                        qualifying=[], corroborating=[], decisive=[counter.item_key], coverage_complete=True)
    coverage = [
        _cov(F.LEG, CS.STOPPED, 9, 0),                    # C-01
        _cov(F.PRD, CS.DONE, 8, 8),                       # C-02
        _cov(F.JOB, CS.DONE, 8, 0, "jobs"),               # C-03
        _cov(F.DNS, CS.DONE, 1, 0, "dns"),                # C-04
        _cov(F.REG, CS.NOT_APPLICABLE, 0, 0, "sec"),      # C-05
        _cov(F.IND, CS.DONE, 3, 2, "seeds"),              # C-06
        _cov(F.HIST, CS.DONE, 0, 0, "wayback"),           # C-07
    ]
    base = dict(evidence=items, coverage=coverage, verdict=verdict, risk=_no_risk(),
                actions=S.action_plan(text="Obtain a written no-AI attestation and add an AI change-notification "
                                           "clause at renewal. Re-scan the public footprint annually."))
    base.update(over)
    return S.findings(**base)


def _affirmed_negative() -> VendorFindings:
    limiting = _limiting()
    items = assign_evidence_ids([limiting], V)
    verdict = S.verdict(keys=None, rule="d", likelihood="very unlikely", confidence="Moderate",
                        confidence_reason="one legally accountable source states the limit",
                        qualifying=[], corroborating=[], decisive=[limiting.item_key], coverage_complete=True)
    return S.findings(evidence=items, coverage=_yes_coverage(), verdict=verdict, risk=_no_risk(),
                      actions=S.action_plan(text="Obtain a written no-AI attestation. Re-scan annually."))


def _conflict() -> VendorFindings:
    q = S.item(role="Indicator")
    limiting = _limiting(role="Indicator")
    items = assign_evidence_ids([q, limiting], V)
    verdict = S.verdict(keys=None, rule="a", likelihood="roughly even chance", confidence="Low",
                        confidence_reason="a statement of use is contradicted by a later limiting statement",
                        qualifying=[q.item_key], corroborating=[], decisive=[q.item_key, limiting.item_key],
                        coverage_complete=True)
    inputs = S.risk_inputs(e=2, k=2, tp=3, e_assumed=True, k_assumed=True, e_items=[], k_items=[],
                           gap_items={}, gaps=dict(ALL_GAPS_MISSING), gap_reasons={})
    risk = S.risk_result(inputs=inputs, arp=14, base_class="Critical", gate_met=False, cap="Medium",
                         pre_cap_class="Medium", final_class="Medium", provisional=True, ceiling_class="Critical")
    return S.findings(evidence=items, coverage=_yes_coverage(), verdict=verdict, risk=risk)


def _cells(findings: VendorFindings, team: str | None = TEAM) -> StudentCells:
    return build_cells(findings, team, DAY)


# --------------------------------------------------------------------------- ids


def test_assign_evidence_ids_follows_role_strength_reliability_relevance_recency_order():
    logged = _item("https://a.example/1", "Logged item about machine learning in payments.", role="Logged")
    context = _dns("OpenAI", OPENAI_TOKEN, role="Context")
    support_b = _item("https://a.example/2", "Supporting item with a first-party source and AI.", role="Supporting",
                      tags=S.tags(sr="B", rl="R2", strength="Moderate"))
    support_c = _item("https://a.example/3", "Supporting item with a promotional source and AI.", role="Supporting",
                      tags=S.tags(sr="C", rl="R3", strength="Moderate"))
    support_c_old = _item("https://a.example/4", "Supporting item, older, promotional source and AI.",
                          role="Supporting", tags=S.tags(sr="C", rl="R3", rc="T1", strength="Moderate"))
    primary = S.item(role="Primary")
    out = assign_evidence_ids([logged, context, support_c_old, support_c, support_b, primary], V)
    assert [i.evidence_id for i in out] == [f"{V}-E-{n:04d}" for n in range(1, 7)]
    assert [i.item_key for i in out] == [primary.item_key, support_b.item_key, support_c.item_key,
                                         support_c_old.item_key, context.item_key, logged.item_key]
    assert all(i.evidence_id == "" for i in (logged, primary))           # inputs are not modified


def test_assign_evidence_ids_rejects_other_vendors_and_duplicates():
    with pytest.raises(ValueError, match="V-902"):
        assign_evidence_ids([S.item(vendor_id="V-902")], V)
    with pytest.raises(ValueError, match="duplicate"):
        assign_evidence_ids([S.item(), S.item()], V)
    assert assign_evidence_ids([], V) == []


def test_coverage_ids_number_each_vendor_by_position():
    entries = [S.coverage(), S.coverage(family=F.DNS), S.coverage(vendor_id="V-902"), S.coverage(family=F.LEG)]
    assert coverage_ids(entries) == [f"{V}-C-01", f"{V}-C-02", "V-902-C-01", f"{V}-C-03"]


# --------------------------------------------------------------------------- column V and helpers


@pytest.mark.parametrize("team,expected", [
    ("Team Osprey", "Team Osprey / 02-10-2026"),
    ("Osprey", "Team Osprey / 02-10-2026"),
    ("  team   Osprey ", "team Osprey / 02-10-2026"),
    ("", None),
    (None, None),
])
def test_render_assessed_by(team, expected):
    assert render_assessed_by(team, DAY) == expected


def test_render_assessed_by_needs_an_iso_date():
    with pytest.raises(ValueError, match="ISO date"):
        render_assessed_by(TEAM, "02/10/2026")


@pytest.mark.parametrize("url,shown", [
    ("https://www.acme.example/instant-payments", "www.acme.example/instant-payments"),
    ("http://acme.example/", "acme.example"),
    ("https://acme.example/docs/", "acme.example/docs/"),
    ("https://dns.google/resolve?name=acme.example&type=TXT", "dns.google/resolve?name=acme.example&type=TXT"),
])
def test_display_url_drops_the_scheme(url, shown):
    assert display_url(url) == shown


# --------------------------------------------------------------------------- Yes


def test_yes_simple_columns():
    findings = _yes()
    cells = _cells(findings)
    assert cells.criticality_tier == findings.criticality.tier.value == "Critical"
    assert cells.criticality_rationale.startswith("Critical. ")
    assert cells.assessment_depth.startswith(findings.depth.label)
    assert "Coverage: 7 of 7 mandatory source families completed" in cells.assessment_depth
    assert cells.ai_usage_detected == "Yes"
    assert cells.ai_risk_class == "High"
    assert cells.recommended_action == U_TEXT
    assert cells.assessed_by == "Team Osprey / 02-10-2026"


def test_yes_p_quotes_primary_and_supporting_items_verbatim():
    findings = _yes()
    items = _by_excerpt(findings)
    p = _cells(findings).evidence
    primary, support, named = items[S.EXCERPT], items[IND_EXCERPT], items[DECK_EXCERPT]
    assert primary.evidence_id == f"{V}-E-0001"
    assert p.startswith(f'Primary source{SEP}Product page, Acme Payments, www.acme.example/instant-payments '
                        f'(retrieved 02-10-2026; Evidence Log {primary.evidence_id}): "{S.EXCERPT}"')
    assert (f'Supporting source{SEP}Trade press interview, Payments Weekly, 14-05-2026, press.example/acme-interview '
            f'(retrieved 02-10-2026; Evidence Log {support.evidence_id}): "{IND_EXCERPT}"') in p
    assert (f'Supporting source{SEP}Investor presentation, Acme Payments, 20-04-2026, '
            f'www.acme.example/ir/q1-deck.pdf (retrieved 02-10-2026; Evidence Log {named.evidence_id}): '
            f'"{DECK_EXCERPT}"') in p
    assert p.index(S.EXCERPT) < p.index(IND_EXCERPT) < p.index(DECK_EXCERPT)       # decisive order
    assert p.endswith(CLOSING)
    assert len(p) <= DEFAULT_LENGTH_BUDGETS["evidence"]


def test_yes_p_holds_no_context_marketing_trap_or_rejected_item():
    p = _cells(_yes()).evidence
    for excerpt in (COHERE_TOKEN, BLOG_EXCERPT, TRAP_EXCERPT, REJECTED_EXCERPT):
        assert excerpt not in p
    assert "Indicator" not in p and "Marketing statement" not in p


def test_p_negative_findings_cite_the_coverage_log():
    p = _cells(_yes()).evidence
    findings = re.findall(rf"Negative finding{SEP}[^.]*\.", p)
    assert findings == [
        f"Negative finding{SEP}an AI sub-processor list, an AI policy or AI governance attestation, and AI data-use "
        f"terms not found on nine legal and trust pages, which hold no AI or automated-decision terms (Coverage Log "
        f"{V}-C-01).",
        f"Negative finding{SEP}AI duties or named AI tools not found on eight job postings (Coverage Log {V}-C-04).",
    ]
    assert f"{V}-C-07" not in p                                   # archive history only dates pages


def test_p_never_reports_an_unsearched_family_as_absent():
    p = _cells(_inconclusive()).evidence
    assert f"{V}-C-01" not in p and f"{V}-C-02" not in p          # legal pages: one entry ended in error
    assert f"{V}-C-03" not in p                                   # product pages blocked by bot protection
    assert f"{V}-C-04" not in p                                   # DNS found tokens
    assert f"Negative finding{SEP}an SEC registration not found on EDGAR (Coverage Log {V}-C-05)." in p
    assert (f"Negative finding{SEP}an applicant tracking system not found on the vendor's website "
            f"(Coverage Log {V}-C-06).") in p


def test_yes_q_numbers_pathways_and_labels_relationship_indicators():
    findings = _yes()
    items = _by_excerpt(findings)
    q = _cells(findings).how_ai_used
    e = {k: v.evidence_id for k, v in items.items()}
    assert q.startswith("Two usage pathways are indicated. First, predictive machine learning within the service "
                        'itself, described as "screens every inbound instant payment", applied to inbound instant '
                        "payment, with decisions reviewed by staff; a third-party source names Google "
                        f"({e[S.EXCERPT]}, {e[IND_EXCERPT]}). Second, generative AI (large language models) in the "
                        "vendor's delivery operations, using OpenAI and Anthropic, producing advisory output only "
                        f"({e[DECK_EXCERPT]}).")
    assert (f"Cohere domain-verification tokens in the vendor's DNS records are a relationship indicator, not a "
            f"confirmed sub-processor ({e[COHERE_TOKEN]}).") in q
    assert q.endswith("is undetermined from public material and is carried forward to the vendor questionnaire.")
    assert "retention or training use of Meridian data" in q      # t3 missing
    assert e[BLOG_EXCERPT] not in q and e[TRAP_EXCERPT] not in q and e[REJECTED_EXCERPT] not in q


def test_yes_r_lists_only_providers_the_vendor_names():
    findings = _yes()
    deck = _by_excerpt(findings)[DECK_EXCERPT]
    r = _cells(findings).ai_subprocessors
    assert r.startswith(f"Named by the vendor: OpenAI and Anthropic{SEP}generative AI in delivery operations "
                        f"(Investor presentation; Evidence Log {deck.evidence_id}).")
    assert "Cohere" not in r                                      # DNS token: relationship only
    assert "Google" not in r                                      # named by a third party only
    assert f"No public sub-processor register found (searched the legal and trust pages of acme.example; " \
           f"Coverage Log {V}-C-01)." in r
    assert len(r) <= DEFAULT_LENGTH_BUDGETS["ai_subprocessors"]


def test_r_uses_first_party_sources_when_checks_were_not_assessed():
    findings = _yes()
    inputs = findings.risk.inputs.model_copy(update={"gaps": {}, "gap_items": {}})
    r = _cells(findings.model_copy(update={"risk": findings.risk.model_copy(update={"inputs": inputs})})).ai_subprocessors
    assert r.startswith("Named by the vendor: OpenAI and Anthropic")
    assert "Google" not in r and "Cohere" not in r


def test_r_agrees_with_the_t2_transparency_check():
    findings = _yes()
    gaps = {**findings.risk.inputs.gaps, "t2": True}
    inputs = findings.risk.inputs.model_copy(update={"gaps": gaps, "gap_items": {}})
    risk = findings.risk.model_copy(update={"inputs": inputs})
    r = _cells(findings.model_copy(update={"risk": risk})).ai_subprocessors
    assert r.startswith(f"None named by the vendor{SEP}Inconclusive.")
    assert "OpenAI" not in r


def test_yes_t_sentences_in_order():
    findings = _yes()
    t = _cells(findings).risk_rationale
    primary = _by_excerpt(findings)[S.EXCERPT]
    support = _by_excerpt(findings)[IND_EXCERPT]
    assert t.startswith("Risk class: High. Data involved, per the vendor profile: payment instructions, "
                        "counterparty names and account identifiers")
    likelihood = "It is very likely that the vendor uses AI in the service supporting inbound real-time payments."
    confidence = ("Confidence is moderate because one first-party source and complete coverage of the mandatory "
                  "families.")
    score = ("Score: exposure 2/3, decision impact 2/3, tier 3, transparency gap 1 → 12 of 18 = High (exposure "
             "and decision impact count double).")
    evidence = f"Public evidence shows predictive machine learning within the service itself"
    for sentence in (likelihood, confidence, score, evidence, FLIP):
        assert sentence in t
    assert (t.index(evidence) < t.index(likelihood) < t.index(confidence) < t.index("Operational dependency")
            < t.index(score) < t.index(FLIP))
    assert f"One independent source corroborates it ({support.evidence_id})." in t
    assert f"({primary.evidence_id})" in t
    assert len(t) <= DEFAULT_LENGTH_BUDGETS["risk_rationale"]


# --------------------------------------------------------------------------- Inconclusive


def test_inconclusive_p_prefixes_each_indicator_with_its_label():
    findings = _inconclusive()
    items = _by_excerpt(findings)
    dns, marketing = items[OPENAI_TOKEN], items[BLOG_EXCERPT]
    p = _cells(findings).evidence
    assert p.startswith(f'Indicator{SEP}relationship only (DNS TXT record), Acme Payments, '
                        f'dns.google/resolve?name=acme.example&type=TXT (retrieved 02-10-2026; Evidence Log '
                        f'{dns.evidence_id}): "{OPENAI_TOKEN}". ')
    assert (f'Marketing statement{SEP}not confirmatory, Blog post, Acme Payments, 03-11-2025, '
            f'www.acme.example/blog/ai-ready (retrieved 02-10-2026; Evidence Log {marketing.evidence_id}): '
            f'"{BLOG_EXCERPT}"') in p
    assert TRAP_EXCERPT not in p and p.endswith(CLOSING)


def test_inconclusive_q_r_s_t():
    findings = _inconclusive()
    dns = _by_excerpt(findings)[OPENAI_TOKEN]
    cells = _cells(findings)
    assert cells.ai_usage_detected == "Inconclusive"
    assert cells.how_ai_used.startswith("No usage pathway is evidenced in public material. OpenAI "
                                        "domain-verification tokens in the vendor's DNS records are a relationship "
                                        f"indicator, not a confirmed sub-processor ({dns.evidence_id}).")
    assert "Marketing or forward-looking statements about AI are not confirmatory" in cells.how_ai_used
    assert "AI use within the service itself" in cells.how_ai_used
    assert cells.ai_subprocessors.startswith(f"None named by the vendor{SEP}Inconclusive. The vendor's legal and "
                                             "trust pages could not be fully searched")
    assert cells.ai_subprocessors.endswith("raised with the vendor through Meridian's questionnaire.")
    assert cells.ai_risk_class == "Medium"
    t = cells.risk_rationale
    assert t.startswith("Risk class: Medium (Provisional). ")
    assert "There is a roughly even chance that the vendor uses AI in the service supporting" in t
    assert "Confidence is low because only relationship indicators were found" in t
    assert "exposure 2/3 (assumed), decision impact 2/3 (assumed), tier 3, transparency gap 3 → 14 of 18 = " \
           "Critical" in t
    assert "materiality gate lowers the class to Medium." in t       # long or short wording, as room allows
    assert "Provisional; ceiling Critical if confirmed." in t
    assert len(t) <= DEFAULT_LENGTH_BUDGETS["risk_rationale"]


def test_inconclusive_t_names_incomplete_coverage_when_room():
    findings = _inconclusive(risk=S.risk_result(
        inputs=S.risk_inputs(e=1, k=1, tp=3, tg=0, e_assumed=True, k_assumed=True, e_items=[], k_items=[],
                             gap_items={}, gaps={}, gap_reasons={}),
        arp=7, base_class="Medium", gate_met=False, cap="Medium", pre_cap_class="Medium", final_class="Medium",
        provisional=True, ceiling_class="", flip_condition=""))
    t = _cells(findings).risk_rationale
    assert ("Coverage of the mandatory sources is incomplete: product pages and newsroom blocked by bot "
            "protection and archive history not logged.") in t          # HIST is mandatory at Critical, unlogged
    assert "Provisional pending the vendor's confirmation." in t


def test_conflict_p_shows_both_sides():
    findings = _conflict()
    items = _by_excerpt(findings)
    q, limiting = items[S.EXCERPT], items[LIMITING_EXCERPT]
    cells = _cells(findings)
    p = cells.evidence
    assert p.startswith(f"Indicator{SEP}contradicted by a limiting statement, Product page, Acme Payments, ")
    assert (f'{SEP}Privacy notice, Acme Payments, www.acme.example/privacy (retrieved 02-10-2026; Evidence Log '
            f'{limiting.evidence_id}): "{LIMITING_EXCERPT}"') in p
    assert p.index(S.EXCERPT) < p.index(LIMITING_EXCERPT)
    assert (f"A statement of AI use ({q.evidence_id}) is contradicted by a limiting statement of the same date or "
            f"later ({limiting.evidence_id}).") in cells.risk_rationale
    assert "contradicted by a limiting statement" in cells.how_ai_used


def test_no_decisive_item_opens_p_with_a_plain_sentence():
    findings = _inconclusive()
    verdict = findings.verdict.model_copy(update={"decisive": []})
    p = _cells(findings.model_copy(update={"verdict": verdict})).evidence
    assert p.startswith(NO_EXCERPT + " Negative finding")
    assert OPENAI_TOKEN not in p


# --------------------------------------------------------------------------- No


def test_not_detected_cells():
    findings = _not_detected()
    counter = _by_excerpt(findings)[COUNTER_EXCERPT]
    cells = _cells(findings)
    assert cells.ai_usage_detected == "No"
    assert cells.ai_risk_class == "None identified"
    p = cells.evidence
    assert p.startswith(f'Counter-evidence{SEP}Trade press profile, Print World, 02-03-2026, '
                        f'print.example/profile-acme (retrieved 02-10-2026; Evidence Log {counter.evidence_id}): '
                        f'"{COUNTER_EXCERPT}" Negative finding{SEP}named AI providers or an AI sub-processor list, an '
                        "AI policy or AI governance attestation, and AI data-use terms not found on nine legal and "
                        f"trust pages, which hold no AI or automated-decision terms (Coverage Log {V}-C-01).")
    assert f"AI duties or named AI tools not found on eight job postings (Coverage Log {V}-C-03)." in p
    assert f"AI-provider verification tokens not found on the vendor's DNS records (Coverage Log {V}-C-04)." in p
    assert f"an SEC registration not found on EDGAR (Coverage Log {V}-C-05)." in p
    assert f"{V}-C-02" not in p and f"{V}-C-06" not in p        # families with AI passages
    assert TRAP_EXCERPT not in p
    assert cells.how_ai_used.startswith("No usage pathway is evidenced in public material.")
    assert cells.how_ai_used.endswith("written confirmation is carried forward to the vendor questionnaire.")
    assert cells.ai_subprocessors.startswith(f"None named by the vendor{SEP}Not detected. No public sub-processor "
                                             "register found (searched the legal and trust pages of acme.example; "
                                             f"Coverage Log {V}-C-01).")
    t = cells.risk_rationale
    assert t.startswith("Risk class: None identified. ")
    assert "It is unlikely that the vendor uses AI in the service supporting inbound real-time payments." in t
    assert "The Not detected verdict means no AI risk class applies." in t


def test_affirmed_negative_quotes_the_limiting_statement():
    findings = _affirmed_negative()
    limiting = _by_excerpt(findings)[LIMITING_EXCERPT]
    cells = _cells(findings)
    assert cells.evidence.startswith(f'Limiting statement{SEP}Privacy notice, Acme Payments, '
                                     f'www.acme.example/privacy (retrieved 02-10-2026; Evidence Log '
                                     f'{limiting.evidence_id}): "{LIMITING_EXCERPT}"')
    assert cells.ai_subprocessors.startswith(f"None named by the vendor{SEP}Affirmed negative.")
    assert f"The vendor's own statements limit or exclude AI use ({limiting.evidence_id})." in cells.how_ai_used
    assert "The vendor's own limiting statement covers the service" in cells.risk_rationale
    assert "It is very unlikely that" in cells.risk_rationale


# --------------------------------------------------------------------------- budgets and quotes


def _long_excerpt(n: int, size: int = 590) -> str:
    words = f"Acme Payments sentence {n} says machine learning reviews payments"
    text = (words + " and more ") * (size // (len(words) + 10) + 1)
    return text[: size - 1].rstrip() + "."


def test_p_drops_whole_items_and_never_cuts_a_quote():
    excerpts = [_long_excerpt(n, 450) for n in (1, 2, 3)]
    items = [_item(f"https://www.acme.example/page-{n}", text, role="Primary" if n == 0 else "Supporting")
             for n, text in enumerate(excerpts)]
    items = assign_evidence_ids(items, V)
    keys = [i.item_key for i in sorted(items, key=lambda i: i.evidence_id)]
    findings = _yes(evidence=items, verdict=S.verdict(keys=None, qualifying=keys, corroborating=[], decisive=keys),
                    risk=S.risk_result(inputs=S.risk_inputs(e_items=keys[:1], k_items=keys[:1], gap_items={})))
    p = _cells(findings).evidence
    assert len(p) <= DEFAULT_LENGTH_BUDGETS["evidence"]
    assert p.startswith("Primary source") and p.endswith(CLOSING)
    by_key = {i.item_key: i for i in items}
    assert f'"{by_key[keys[0]].excerpt}"' in p
    for key in keys:
        excerpt = by_key[key].excerpt
        assert (f'"{excerpt}"' in p) == (excerpt[:60] in p)        # a quote is whole or absent
    assert sum(f'"{by_key[k].excerpt}"' in p for k in keys) == 2   # the third item is dropped


def test_p_keeps_the_register_finding_before_extra_family_findings():
    coverage = _yes_coverage() + [_cov(F.EXEC, CS.DONE_MANUAL, 4, 0, "manual")]
    p = _cells(_yes(coverage=coverage)).evidence
    assert f"(Coverage Log {V}-C-01)" in p
    assert len(p) <= DEFAULT_LENGTH_BUDGETS["evidence"]


def _pressure() -> VendorFindings:
    """Many pathways and providers, long profile text, long confidence reason and flip condition."""
    loci = ["service_feature", "vendor_addon", "delivery_ops", "sdlc", "corporate_internal"]
    ai_types = ["predictive_ml", "genai_llm", "agentic", "conversational", "document_ai", "aiops"]
    providers = ["OpenAI", "Anthropic", "Google", "Microsoft", "Amazon Web Services", "Cohere", "Mistral AI",
                 "Meta", "IBM", "Salesforce", "ServiceNow", "Nvidia"]
    items = []
    for n in range(12):
        items.append(_item(f"https://www.acme.example/ai/{n}", _long_excerpt(n, 400),
                           role="Primary" if n == 0 else "Supporting", source_type="Product documentation page",
                           providers=providers[n:n + 2], data_mentioned=[f"data set number {n}", "payment files"],
                           tags=S.tags(locus=loci[n % 5], ai_type=ai_types[n % 6],
                                       strength="Strong" if n == 0 else "Moderate")))
    items = assign_evidence_ids(items, V)
    keys = [i.item_key for i in sorted(items, key=lambda i: i.evidence_id)]
    profile = S.profile(
        data_accessed="Borrower names, addresses, dates of birth, loan account numbers, repayment schedules, arrears "
                      "status, income and expenditure statements, hardship and vulnerability notes (including "
                      "health-related disclosures), and recorded call audio. Payment instructions and account "
                      "identifiers for every customer, with full transaction history and card numbers.",
        data_volume="Approximately 240,000 active loan accounts under servicing and around 660,000 borrower "
                    "interactions per year, of which an estimated 180,000 are recorded voice calls.",
        business_process="Retail lending servicing, arrears management, collections, hardship handling and "
                         "borrower correspondence across every retail channel")
    verdict = S.verdict(keys=None, qualifying=keys, corroborating=keys[1:4], decisive=keys[:3],
                        confidence_reason="two legally accountable sources and one first-party source agree, the "
                                          "coverage of every mandatory family is complete, and the corroborating "
                                          "sources come from independent publishers and different source families "
                                          "with no shared origin cluster, although one of them is promotional")
    inputs = S.risk_inputs(e=3, k=3, e_items=keys[:1], k_items=keys[:1], gap_items={"t2": keys},
                           gaps={"t1": False, "t2": False, "t3": True, "t4": True, "t5": True, "t6": True},
                           gap_reasons={})
    risk = S.risk_result(inputs=inputs, arp=2 * 3 + 2 * 3 + 3 + 2, base_class="Critical", pre_cap_class="Critical",
                         final_class="Critical", escalators_fired=["X3", "X6"],
                         flip_condition="Written confirmation that every model runs in an isolated tenant, that no "
                                        "Meridian borrower data reaches a prompt, that outputs are reviewed case by "
                                        "case and that no provider retains or trains on the data would lower the "
                                        "class to High.")
    actions = S.action_plan(text=" ".join(f"Action sentence number {n} asks Meridian to confirm one more control "
                                          f"with the vendor through the questionnaire." for n in range(40)))
    return S.findings(profile=profile, evidence=items, coverage=_yes_coverage(), verdict=verdict, risk=risk,
                      actions=actions)


def test_every_cell_fits_its_budget_under_pressure():
    cells = _cells(_pressure())
    for field in STUDENT_FIELDS:
        value = getattr(cells, field)
        assert value is not None, field
        assert len(value) <= DEFAULT_LENGTH_BUDGETS[field], (field, len(value))
    t = cells.risk_rationale
    assert t.startswith("Risk class: Critical. ")
    for must in ("It is very likely that the vendor uses AI", "Confidence is moderate because",
                 "Score: exposure 3/3, decision impact 3/3"):
        assert must in t
    assert not t.endswith("...")
    assert "Named by the vendor: " in cells.ai_subprocessors
    q = cells.how_ai_used
    assert q.startswith("Five usage pathways are indicated. First, machine learning, document AI, and AI for IT "
                        "operations within the service itself, ")                 # one pathway per place of use
    assert "Fifth, document AI and conversational AI in the vendor's internal corporate functions" in q
    assert "Amazon (Amazon Web Services; Evidence Log" in cells.ai_subprocessors   # grouped under the maker


def test_u_is_cut_by_whole_sentences_only():
    cells = _cells(_pressure())
    findings = _pressure()
    u = cells.recommended_action
    assert len(u) <= DEFAULT_LENGTH_BUDGETS["recommended_action"]
    assert findings.actions.text.startswith(u) and u.endswith("questionnaire.")


def test_u_falls_back_to_the_playbook_without_text():
    findings = _yes(actions=S.action_plan(text=""))
    u = _cells(findings).recommended_action
    assert u == "Issue a targeted questionnaire within 15 business days covering the open gap blocks. Re-scan the " \
                "public footprint annually."


# --------------------------------------------------------------------------- determinism and style


@pytest.mark.parametrize("make", [_yes, _inconclusive, _not_detected, _affirmed_negative, _conflict, _pressure])
def test_cells_are_deterministic_whatever_the_evidence_order(make):
    findings = make()
    shuffled = findings.model_copy(update={"evidence": list(reversed(findings.evidence))})
    first = _cells(findings)
    assert _cells(findings) == first
    assert _cells(shuffled) == first


@pytest.mark.parametrize("make", [_yes, _inconclusive, _not_detected, _affirmed_negative, _conflict, _pressure])
def test_cells_carry_no_tag_codes_or_hashes(make):
    cells = _cells(make())
    for field in ("evidence", "how_ai_used", "ai_subprocessors", "risk_rationale"):
        text = getattr(cells, field)
        assert not TAG_CODE.search(text), (field, TAG_CODE.search(text))
        assert not HASH.search(text), field
        assert "–" not in text, field                         # the separator is the em dash of V-000
    assert SEP in cells.evidence


def test_a_cited_item_without_an_evidence_id_is_a_contract_break():
    findings = _yes()
    bare = [item.model_copy(update={"evidence_id": ""}) for item in findings.evidence]
    with pytest.raises(ValueError, match="assign_evidence_ids"):
        _cells(findings.model_copy(update={"evidence": bare}))


def test_calibration_example_without_evidence():
    findings = S.findings(evidence=[], coverage=[], verdict=S.verdict(keys=[]),
                          risk=S.risk_result(inputs=S.risk_inputs(e_items=[], k_items=[], gap_items={})))
    cells = _cells(findings)
    assert cells.evidence == CLOSING
    assert cells.how_ai_used.startswith("No usage pathway is evidenced in public material.")
    assert cells.ai_subprocessors.startswith(f"None named by the vendor{SEP}Inconclusive. No sub-processor register "
                                             "search is recorded.")
    assert "Coverage:" not in cells.assessment_depth
    assert cells.risk_rationale.startswith("Risk class: High. ")


def test_p_prefix_table_matches_the_design():
    assert P_PREFIXES == {
        "Context - relationship only": f"Indicator{SEP}relationship only",
        "Context - platform supplier": f"Indicator{SEP}platform supplier capability",
        "Context - inferred affiliate": f"Indicator{SEP}inferred affiliate, not confirmed",
        "Marketing only": f"Marketing statement{SEP}not confirmatory",
        "Weak": f"Indicator{SEP}weak",
        "Negative": "Limiting statement",
    }
    assert SEP == " — "


# --------------------------------------------------------------------------- sheets


def test_evidence_log_has_one_row_per_item_in_e_id_order():
    findings = _yes()
    spec = evidence_log_sheet([findings, _not_detected()])
    assert spec.title == EVIDENCE_LOG_TITLE and spec.headers == EVIDENCE_LOG_HEADERS
    assert len(spec.rows) == len(findings.evidence) + len(_not_detected().evidence)
    assert all(len(row) == len(spec.headers) for row in spec.rows)
    col = {h: n for n, h in enumerate(spec.headers)}
    ids = [row[col["Evidence ID"]] for row in spec.rows[: len(findings.evidence)]]
    assert ids == sorted(ids) and ids[0] == f"{V}-E-0001"
    rows = {row[col["Excerpt"]]: row for row in spec.rows}
    primary = rows[S.EXCERPT]
    item = _by_excerpt(findings)[S.EXCERPT]
    assert primary[col["Tags"]] == "U1 · SR:B · SP:S2 · RL:R3 · RC:T3 · IC:2 · locus=service_feature"
    assert primary[col["Offsets"]] == f"{item.start}-{item.end}"
    assert primary[col["Excerpt SHA-256"]] == item.excerpt_sha256
    assert primary[col["Capture SHA-256"]] == S.CAPTURE_ID and primary[col["Text SHA-256"]] == S.DOC_ID
    assert primary[col["Retrieved (UTC)"]] == "2026-10-02T12:00:00Z"
    assert primary[col["Visible in render"]] == "Not checked"
    assert primary[col["Indicators"]] == "G2: screens every inbound instant payment"
    assert primary[col["Rule labels"]] == "action_level=human_reviewed_decision; sp=S2; temporal=in_production"
    assert primary[col["Item key"]] == item.item_key
    assert primary[col["Status"]] == "citable"
    assert rows[TRAP_EXCERPT][col["Status"]] == "trap: not AI under the definition test"
    assert rows[REJECTED_EXCERPT][col["Status"]] == "rejected by analyst"
    assert rows[REJECTED_EXCERPT][col["Review reason"]] == "summary by an aggregator, not vendor text"


def test_evidence_log_maps_corroboration_to_e_ids():
    findings = _yes()
    items = _by_excerpt(findings)
    support, primary = items[IND_EXCERPT], items[S.EXCERPT]
    evidence = [i.model_copy(update={"corroborates": [primary.item_key]}) if i is support else i
                for i in findings.evidence]
    spec = evidence_log_sheet([findings.model_copy(update={"evidence": evidence})])
    col = {h: n for n, h in enumerate(spec.headers)}
    row = next(r for r in spec.rows if r[col["Excerpt"]] == IND_EXCERPT)
    assert row[col["Corroborates (E-IDs)"]] == primary.evidence_id


def test_evidence_status_for_a_pending_llm_proposal():
    item = S.item(method="llm_proposed_accepted", llm_model="gemini-3.5-flash-lite")
    assert evidence_status(item) == "proposed by the LLM, awaiting analyst review"
    assert evidence_status(item.model_copy(update={"review_status": "accepted"})) == "citable"


def test_coverage_log_adds_the_coverage_id_first():
    findings = [_yes(), _inconclusive()]
    spec = coverage_log_sheet(findings)
    assert spec.title == "Coverage Log" and spec.headers == ["Coverage ID", *COVERAGE_HEADERS]
    assert len(spec.rows) == sum(len(f.coverage) for f in findings)
    assert [row[0] for row in spec.rows[:2]] == [f"{V}-C-01", f"{V}-C-02"]
    assert spec.rows[len(findings[0].coverage)][0] == f"{V}-C-01"          # the next vendor starts again
    assert all(len(row) == len(spec.headers) == len(spec.column_widths) for row in spec.rows)
    assert spec.rows[0][1:5] == [V, "LEG", "Yes", "done"]


def test_evidence_images_lists_items_with_screenshots():
    findings = _yes()
    primary = _by_excerpt(findings)[S.EXCERPT]
    shot = primary.model_copy(update={"screenshot_path": "evidence/shots/ab/abc.png",
                                      "screenshot_sha256": "a" * 64, "visible_in_render": True})
    evidence = [shot if i is primary else i for i in findings.evidence]
    spec = evidence_images_sheet([findings.model_copy(update={"evidence": evidence})])
    assert spec.title == EVIDENCE_IMAGES_TITLE
    assert spec.rows == [[primary.evidence_id, V, "evidence/shots/ab/abc.png", "a" * 64, "Yes", S.URL]]


def test_run_info_rows_leave_out_wall_clock_time():
    manifest = {
        "run_id": "A-20261002-0000abcd", "mode": "replay", "as_of": "2026-10-02", "input_sha256": "0" * 64,
        "created_at": "2026-10-03T09:15:00Z", "code_version": "0.1.0",
        "config_sha256": {"rubric": "1" * 64, "depth": "2" * 64},
        "prompts_sha256": {"extract_v1": "3" * 64},
        "llm": {"withheld": {V: {"count": 2, "rate": 0.25}}, "calls": {V: {"planned": 40, "actual": 3}},
                "models": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"], "switches": []},
        "counts": {V: {"items": 7, "cited": 4}}, "zeta": True,
    }
    example = S.findings(evidence=[], coverage=[], verdict=S.verdict(keys=[]),
                         risk=S.risk_result(inputs=S.risk_inputs(e_items=[], k_items=[], gap_items={})),
                         profile=S.profile(vendor_id="V-000", is_example=True))
    result = S.assessment(manifest=manifest, example=example)
    spec = run_info_sheet(result)
    assert spec.title == RUN_INFO_TITLE and spec.headers == ["Item", "Value"]
    keys = [row[0] for row in spec.rows]
    values = dict((row[0], row[1]) for row in spec.rows)
    assert keys[:5] == ["Run ID", "Mode", "As of", "Input SHA-256", "Vendors"]
    assert values["Run ID"] == result.run_id and values["Vendors"] == V
    assert not any("created_at" in key for key in keys)
    assert "2026-10-03T09:15:00Z" not in [str(v) for v in values.values()]
    assert keys.index("code_version") < keys.index("config_sha256.depth") < keys.index("prompts_sha256.extract_v1")
    assert keys.index("llm.models") < keys.index("llm.switches") < keys.index(f"llm.calls.{V}.actual") \
        < keys.index(f"llm.withheld.{V}.rate")
    assert values["llm.models"] == "gemini-3.5-flash-lite; gemini-3.1-flash-lite"
    assert values[f"llm.withheld.{V}.rate"] == 0.25 and values["zeta"] == "yes"
    assert values["Calibration V-000"] == "Yes (Confirmed); ARP 12 of 18; class High"


def test_method_legend_p3_appends_the_p3_sections():
    spec = SheetSpec(title=METHOD_LEGEND_TITLE, headers=list(LEGEND_HEADERS))
    out = method_legend_p3(spec, questions={"Q10": "Incident notice?", "Q2": "Which providers?"},
                           clauses=[("C1", "training", "Training restriction.")],
                           gap_blocks=[("GAP-SUB", "sub-processors", "Q3; C2 (trigger: t2 missing)")],
                           seeds={V: {"service_term": [{"term": "instant payments", "reason": "col H"}],
                                      "family_term": [{"term": "Payments", "reason": "product family"}],
                                      "expect": "strong"}},
                           query_templates=[("SEC EFTS", '"artificial intelligence" ciks={cik}')])
    assert out is spec
    sections = list(dict.fromkeys(row[0] for row in spec.rows))
    assert sections == ["Evidence tags", "Strength labels (in order)", "Column P prefixes",
                        "Genuine use vs marketing (tests in order)", "Indicators", "AI usage verdict (column O)",
                        "AI risk matrix (columns S and T)", "Questionnaire items", "Contract clauses",
                        "Gap blocks (column U)", "Service-term dictionaries", "Query templates"]
    detail = {(row[0], row[1]): row[2] for row in spec.rows}
    strengths = [row[1] for row in spec.rows if row[0] == "Strength labels (in order)"]
    assert strengths == list(STRENGTH_ORDER)
    assert detail[("AI risk matrix (columns S and T)", "Class bands")] == "Critical 14-18; High 10-13; Medium 6-9; Low 0-5"
    assert detail[("AI risk matrix (columns S and T)", "TG")] == "missing checks 0-1 give 0; 2-3 give 1; 4-5 give 2; 6 give 3"
    assert [row[1] for row in spec.rows if row[0] == "Questionnaire items"] == ["Q2", "Q10"]
    assert detail[("Contract clauses", "C1")] == "training: Training restriction."
    assert detail[("Gap blocks (column U)", "GAP-SUB")] == "sub-processors: Q3; C2 (trigger: t2 missing)"
    assert detail[("Service-term dictionaries", f"{V}: instant payments")] == "exact service (R3); col H"
    assert detail[("Service-term dictionaries", f"{V}: Payments")] == "product family (R2); product family"
    assert detail[("Query templates", "SEC EFTS")] == '"artificial intelligence" ciks={cik}'
    assert all(len(row) == 3 for row in spec.rows)
    assert not any("strong" == row[2] for row in spec.rows)          # a seed's gold label is never read


def test_method_legend_p3_leaves_empty_sections_out():
    spec = method_legend_p3(SheetSpec(title=METHOD_LEGEND_TITLE, headers=list(LEGEND_HEADERS)),
                            questions={}, clauses={}, gap_blocks={})
    sections = {row[0] for row in spec.rows}
    assert not sections & {"Questionnaire items", "Contract clauses", "Gap blocks (column U)", "Query templates",
                           "Service-term dictionaries"}
    assert "AI risk matrix (columns S and T)" in sections


def test_method_legend_p3_reads_the_action_policy_by_default():
    spec = method_legend_p3(SheetSpec(title=METHOD_LEGEND_TITLE, headers=list(LEGEND_HEADERS)))
    codes = {row[0]: [] for row in spec.rows}
    for row in spec.rows:
        codes[row[0]].append(row[1])
    assert codes["Questionnaire items"] == [f"Q{n}" for n in range(1, 16)]
    assert codes["Contract clauses"] == [f"C{n}" for n in range(1, 12)]
    assert codes["Gap blocks (column U)"][0] == "GAP-SUB"


def test_cells_and_sheets_pass_the_workbook_writer(tmp_path):
    from footprint.criticality import load_rubric
    from footprint.depth import load_depth_config
    from footprint.sheets import method_legend_sheet
    from footprint.workbook import check_fidelity, write_workbook

    yes, maybe, no = _yes(), _inconclusive(), _not_detected()
    cells = {"V-001": _cells(yes), "V-003": _cells(no), "V-004": _cells(maybe), "V-005": _cells(_pressure())}
    legend = method_legend_p3(method_legend_sheet(load_rubric(), load_depth_config()))
    sheets = [evidence_log_sheet([yes, maybe, no]), coverage_log_sheet([yes, maybe, no]), legend,
              evidence_images_sheet([yes]), run_info_sheet(S.assessment())]
    out = tmp_path / "out.xlsx"
    report = write_workbook(INPUT, out, cells, sheets, last_modified_by=TEAM)
    assert report.sheets_added == [EVIDENCE_LOG_TITLE, "Coverage Log", METHOD_LEGEND_TITLE, EVIDENCE_IMAGES_TITLE,
                                   RUN_INFO_TITLE]
    assert "P6" in report.written and "V9" in report.written
    assert check_fidelity(INPUT, out) == []


# --------------------------------------------------------------------------- context labels (AutomWorx-like)

AFFILIATE_URL = "https://www.labarum.example/capabilities"
AFFILIATE_EXCERPT = "NEO runs open-source large language models on local GPUs with retrieval over customer environments."
SPD_URL = "https://supplier.example/docs/spd-ai.pdf"
SPD_EXCERPT = "Gemini is the default model on SaaS; customers may bring OpenAI or Claude models instead."
WEAK_URL = "https://www.acme.example/about"
WEAK_EXCERPT = "Acme Payments uses AI to improve its services."


def _context_findings(decisive: str = "affiliate") -> VendorFindings:
    affiliate = _item(AFFILIATE_URL, AFFILIATE_EXCERPT, source_type="Capabilities page", publisher="Labarum AI",
                      role="Indicator", providers=[], indicators=[], data_mentioned=[], action_level="unknown",
                      tags=S.tags(u_class="U2", sr="C", rl="R1", locus="affiliate_inferred", ai_type="genai_llm",
                                  strength="Context - inferred affiliate"))
    platform = _item(SPD_URL, SPD_EXCERPT, family=F.LEG, source_type="Product terms (SPD)", publisher="Supplier Inc",
                     published="2025-10-01", date_basis="pdf CreationDate", role="Context",
                     providers=["Gemini", "OpenAI", "Claude"], indicators=[], data_mentioned=[],
                     tags=S.tags(u_class="U2", sr="A", rl="R1", locus="platform_supplier", ai_type="genai_llm",
                                 strength="Context - platform supplier"))
    weak = _item(WEAK_URL, WEAK_EXCERPT, source_type="Company page", publisher="Acme Payments", role="Indicator",
                 indicators=[], data_mentioned=[], action_level="unknown",
                 tags=S.tags(u_class="U1", sp="S1", rl="R2", locus="unknown", ai_type="unspecified", strength="Weak"))
    marketing = _marketing(role="Indicator")
    items = assign_evidence_ids([affiliate, platform, weak, marketing], V)
    first = {"affiliate": affiliate, "weak": weak}[decisive]
    verdict = S.verdict(keys=None, rule="f", likelihood="unlikely", confidence="Moderate",
                        confidence_reason="complete coverage found only context and marketing statements",
                        qualifying=[], corroborating=[], decisive=[first.item_key, marketing.item_key],
                        coverage_complete=True)
    inputs = S.risk_inputs(e=3, k=3, tp=2, e_assumed=True, k_assumed=True, e_items=[], k_items=[], gap_items={},
                           gaps=dict(ALL_GAPS_MISSING), gap_reasons={})
    risk = S.risk_result(inputs=inputs, arp=17, base_class="Critical", gate_met=False, cap="Medium",
                         pre_cap_class="Medium", final_class="Medium", provisional=True, ceiling_class="Critical",
                         flip_condition="Confirmation that the affiliate's platform delivers Meridian work would "
                                        "raise the class.")
    coverage = [_cov(F.LEG, CS.DONE, 1, 0), _cov(F.REG, CS.DONE, 0, 0, "sec"), _cov(F.IND, CS.DONE, 0, 0, "seeds")]
    return S.findings(evidence=items, coverage=coverage, verdict=verdict, risk=risk)


def test_context_labels_in_p_q_r_and_t():
    findings = _context_findings()
    e = {item.excerpt: item.evidence_id for item in findings.evidence}
    cells = _cells(findings)
    p = cells.evidence
    assert p.startswith(f'Indicator{SEP}inferred affiliate, not confirmed, Capabilities page, Labarum AI, '
                        f'www.labarum.example/capabilities (retrieved 02-10-2026; Evidence Log '
                        f'{e[AFFILIATE_EXCERPT]}): "{AFFILIATE_EXCERPT}" Marketing statement{SEP}not confirmatory, ')
    assert SPD_EXCERPT not in p                                    # context only, not decisive
    q = cells.how_ai_used
    assert ("AI options of Supplier Inc, a platform supplier the vendor uses (Gemini, OpenAI, and Claude), are a "
            f"relationship indicator, not a confirmed sub-processor ({e[SPD_EXCERPT]}).") in q
    assert ("Generative AI (large language models) described by an inferred affiliate is not confirmed as used in "
            f"the service ({e[AFFILIATE_EXCERPT]}).") in q
    assert f"Weak indicators mention AI without tying it to the service ({e[WEAK_EXCERPT]})." in q
    r = cells.ai_subprocessors
    assert r.startswith(f"None named by the vendor{SEP}Inconclusive.")
    assert "Gemini" not in r and "Claude" not in r                 # a platform supplier's terms, not the vendor
    t = cells.risk_rationale
    assert (f"Public evidence is limited to an inferred affiliate's AI capability and marketing statements "
            f"({e[AFFILIATE_EXCERPT]}, {e[BLOG_EXCERPT]}), and none of it confirms AI use in the service.") in t
    assert "It is unlikely that the vendor uses AI" in t


def test_weak_indicator_prefix():
    findings = _context_findings(decisive="weak")
    wid = next(i.evidence_id for i in findings.evidence if i.excerpt == WEAK_EXCERPT)
    p = _cells(findings).evidence
    assert p.startswith(f'Indicator{SEP}weak, Company page, Acme Payments, www.acme.example/about (retrieved '
                        f'02-10-2026; Evidence Log {wid}): "{WEAK_EXCERPT}"')


def test_empty_families_and_a_single_legal_page():
    p = _cells(_context_findings()).evidence
    assert (f"Negative finding{SEP}named AI providers or an AI sub-processor list, an AI policy or AI governance "
            f"attestation, and AI data-use terms not found on one legal and trust page, which holds no AI or "
            f"automated-decision terms (Coverage Log {V}-C-01).") in p
    assert (f"Negative finding{SEP}AI statements not found on an EDGAR full-text search of the vendor's filings "
            f"(Coverage Log {V}-C-02).") in p
    assert f"{V}-C-03" not in p                                    # nothing fetched: nothing to report as absent


def test_sheet_widths_match_headers():
    for spec in (evidence_log_sheet([_yes()]), evidence_images_sheet([_yes()]), run_info_sheet(S.assessment()),
                 coverage_log_sheet([_yes()])):
        assert len(spec.column_widths) == len(spec.headers), spec.title
        assert 1 <= len(spec.title) <= 31


def test_t_keeps_the_evidence_sentence_when_squeezed():
    base = _inconclusive()
    dns = _by_excerpt(base)[OPENAI_TOKEN]
    reason = ("the search of product pages and newsroom (blocked by bot protection), legal and trust pages (ended in "
              "error) and archive history (not logged) was not completed, so the relationship indicators found cannot "
              "be weighed against the vendor's own disclosures")
    flip = ("Confirmation that the AI services indicated only by DNS records (OpenAI) are used on Meridian data would "
            "make the verdict Yes and could raise the class to Critical.")
    findings = base.model_copy(update={"verdict": base.verdict.model_copy(update={"confidence_reason": reason}),
                                       "risk": base.risk.model_copy(update={"flip_condition": flip})})
    t = _cells(findings).risk_rationale
    assert len(t) <= DEFAULT_LENGTH_BUDGETS["risk_rationale"]
    assert t.startswith("Risk class: Medium (Provisional). Data involved, per the vendor profile: ")
    assert dns.evidence_id in t                                      # the evidence is still cited by E-ID
    assert f"Confidence is low because {reason}." in t
    assert "Provisional; ceiling Critical if confirmed." in t and t.endswith(flip)
    assert len(t) > DEFAULT_LENGTH_BUDGETS["risk_rationale"] - 150      # freed room is given back


# --------------------------------------------------------------------------- refinements seen on the live evidence

PDF_EXCERPT = "~70%\nOf restricted party \nscreening for payments  reviewed by AI"
SUB_URL = "https://www.acme.example/legal/subprocessors"
SUB_EXCERPT = "Acme Payments uses OpenAI as a sub-processor to summarise support cases."
REL_URL = "https://www.sec.example/acme-8k.htm"
REL_EXCERPT = "Acme Payments announced a collaboration with OpenAI to reimagine client journeys."
ASSIST_URL = "https://www.acme.example/assist"
ASSIST_EXCERPT = "Acme Payments Assist drafts payment repair suggestions with a large language model."
ALL_GAPS_CLOSED = {g: False for g in ALL_GAPS_MISSING}


def _yes_with(items: list[EvidenceItem], decisive: list[EvidenceItem], **inputs) -> VendorFindings:
    """A Yes verdict over ``items`` (E-IDs assigned) quoting ``decisive``; risk inputs cite the first decisive item."""
    keys = [i.item_key for i in decisive]
    base = dict(e_items=keys[:1], k_items=keys[:1], gap_items={})
    base.update(inputs)
    return _yes(evidence=assign_evidence_ids(items, V),
                verdict=S.verdict(keys=None, qualifying=keys, corroborating=[], decisive=keys),
                risk=S.risk_result(inputs=S.risk_inputs(**base)))


def test_p_shows_layout_whitespace_as_one_space_and_the_log_keeps_the_exact_slice():
    deck = _item(DECK_URL, PDF_EXCERPT, source_type="Investor presentation", publisher="Acme Payments",
                 published="2026-04-16", date_basis="pdf CreationDate", role="Primary", indicators=[],
                 tags=S.tags(u_class="U3", rl="R2", locus="delivery_ops", strength="Moderate"))
    findings = _yes_with([deck], [deck])
    p = _cells(findings).evidence
    assert display_quote(PDF_EXCERPT) == "~70% Of restricted party screening for payments reviewed by AI"
    assert '): "~70% Of restricted party screening for payments reviewed by AI". ' in p    # every character kept
    assert "\n" not in p and "  " not in p
    spec = evidence_log_sheet([findings])
    assert spec.rows[0][spec.headers.index("Excerpt")] == PDF_EXCERPT          # the log keeps the exact slice


def test_a_vendor_legal_page_naming_ai_providers_is_a_register():
    primary = S.item(role="Primary")
    sub = _item(SUB_URL, SUB_EXCERPT, family=F.LEG, source_type="Sub-processor list", publisher="Acme Payments",
                role="Supporting", providers=["OpenAI"], indicators=[], data_mentioned=["support cases"],
                action_level="advisory", tags=S.tags(u_class="U3", sr="A", rl="R2", locus="delivery_ops",
                                                      ai_type="genai_llm", strength="Moderate"))
    gaps = {**ALL_GAPS_CLOSED, "t3": True, "t5": True}
    findings = _yes_with([primary, sub], [primary], gaps=gaps, gap_reasons={}, gap_items={"t2": [sub.item_key]})
    coverage = [_cov(F.LEG, CS.DONE, 9, 1), *_yes_coverage()[1:]]
    cells = _cells(findings.model_copy(update={"coverage": coverage}))
    sub_id = next(i.evidence_id for i in findings.evidence if i.excerpt == SUB_EXCERPT)
    assert cells.ai_subprocessors.startswith(f"Named by the vendor: OpenAI{SEP}generative AI in delivery operations "
                                             f"(Sub-processor list; Evidence Log {sub_id}).")
    assert "register" not in cells.ai_subprocessors
    assert (f"Negative finding{SEP}an AI policy or AI governance attestation and AI data-use terms not found on the "
            f"vendor's legal and trust pages (Coverage Log {V}-C-01).") in cells.evidence


def test_p_and_r_agree_when_providers_are_named_outside_a_register():
    cells = _cells(_yes())                    # t2 is closed by an investor deck; the legal pages hold no register
    assert "an AI sub-processor list" in cells.evidence
    assert "No public sub-processor register found" in cells.ai_subprocessors


def test_q_groups_pathways_by_place_of_use_and_quotes_feature_terms():
    primary = S.item(role="Primary")
    assist = _item(ASSIST_URL, ASSIST_EXCERPT, role="Supporting", data_mentioned=["Payment repairs"],
                   indicators=[Indicator(code="G2", span="drafts payment\nrepair suggestions")],
                   action_level="advisory", tags=S.tags(ai_type="genai_llm", rl="R2", strength="Moderate"))
    findings = _yes_with([primary, assist], [primary, assist])
    e = {i.excerpt: i.evidence_id for i in findings.evidence}
    q = _cells(findings).how_ai_used
    assert q.startswith("One usage pathway is indicated. First, machine learning and generative AI within the "
                        'service itself, described as "screens every inbound instant payment" and "drafts payment '
                        'repair suggestions", applied to inbound instant payment and payment repairs, with decisions '
                        f"reviewed by staff ({e[S.EXCERPT]}, {e[ASSIST_EXCERPT]}).")
    assert q.endswith("The extent of model-provider involvement and retention or training use of Meridian data is "
                      "undetermined from public material and is carried forward to the vendor questionnaire.")


def test_q_always_carries_an_unknown_forward_for_a_yes():
    findings = _yes(risk=S.risk_result(inputs=S.risk_inputs(gaps=dict(ALL_GAPS_CLOSED), gap_reasons={},
                                                            gap_items={})))
    q = _cells(findings).how_ai_used
    assert q.endswith("The extent of Meridian data reaching these AI uses is undetermined from public material and "
                      "is carried forward to the vendor questionnaire.")


def test_named_relationships_in_q_and_r():
    primary = S.item(role="Primary")
    rel = _item(REL_URL, REL_EXCERPT, family=F.REG, source_type="SEC Form 8-K", publisher="Acme Payments",
                published="2026-05-14", date_basis="filing date", role="Context", providers=["OpenAI"],
                indicators=[], data_mentioned=[], action_level="unknown",
                tags=S.tags(u_class="U4", sr="A", sp="S1", rl="R2", locus="relationship", ai_type="unspecified",
                            strength="Context - relationship only"))
    findings = _yes_with([primary, rel], [primary], gap_items={"t2": [rel.item_key]},
                         gaps={**ALL_GAPS_CLOSED, "t3": True}, gap_reasons={})
    rid = next(i.evidence_id for i in findings.evidence if i.excerpt == REL_EXCERPT)
    cells = _cells(findings)
    assert (f"Named AI relationships (OpenAI) are a relationship indicator, not a confirmed sub-processor "
            f"({rid}).") in cells.how_ai_used
    assert cells.ai_subprocessors.startswith(f"Named by the vendor: OpenAI{SEP}named as an AI partner or provider "
                                             f"(SEC Form 8-K; Evidence Log {rid}).")


@pytest.mark.parametrize("name,maker", [
    ("ChatGPT", "OpenAI"), ("Azure OpenAI", "OpenAI"), ("Claude Code", "Anthropic"), ("  Vertex   AI ", "Google"),
    ("GitHub Copilot", "Microsoft"), ("Personetics", "Personetics"), ("Mistral AI", "Mistral"),
])
def test_canonical_provider(name, maker):
    assert canonical_provider(name) == maker


def test_r_groups_model_names_under_their_maker():
    findings = _yes()
    deck = _by_excerpt(findings)[DECK_EXCERPT]
    renamed = deck.model_copy(update={"providers": ["ChatGPT", "Azure OpenAI", "Claude"]})
    evidence = [renamed if i is deck else i for i in findings.evidence]
    r = _cells(findings.model_copy(update={"evidence": evidence})).ai_subprocessors
    assert r.startswith(f"Named by the vendor: OpenAI (ChatGPT, Azure OpenAI) and Anthropic (Claude){SEP}generative "
                        f"AI in delivery operations (Investor presentation; Evidence Log {deck.evidence_id}).")


LONG_REASON = ("two first-party sources agree on the fact of use and every mandatory source family was searched, "
               "but the data flows, the hosting region and the degree of human review are inferred from job "
               "postings and an investor presentation rather than stated in a contract, a trust page or a filing, "
               "so the finding rests on the vendor's own descriptions")


def test_t_gives_the_flip_condition_priority_over_the_gap_and_dependency_sentences():
    base = _yes()
    findings = base.model_copy(update={"verdict": base.verdict.model_copy(update={"confidence_reason": LONG_REASON})})
    t = _cells(findings).risk_rationale
    assert len(t) <= DEFAULT_LENGTH_BUDGETS["risk_rationale"]
    assert t.endswith(FLIP)
    assert "Public evidence shows predictive machine learning within the service itself" in t
    assert "Operational dependency" not in t or "transparency checks" not in t      # one of them gave way


def test_t_keeps_a_short_evidence_sentence_ahead_of_a_very_long_flip():
    base = _yes()
    flip = ("Confirmation of AI use in the service itself, with evidence that a named external model processes "
            "Meridian payment instructions in the service or that AI acts on customers, funds, regulatory outputs or "
            "production without per-case human review, and that no provider retains or trains on the data, would "
            "make the verdict Confirmed and raise the class to Critical.")
    findings = base.model_copy(update={
        "verdict": base.verdict.model_copy(update={"confidence_reason": LONG_REASON}),
        "risk": base.risk.model_copy(update={"flip_condition": flip})})
    t = _cells(findings).risk_rationale
    assert len(t) <= DEFAULT_LENGTH_BUDGETS["risk_rationale"]
    assert "Public evidence shows predictive machine learning within the service itself" in t
    assert flip not in t
    for must in ("Risk class: High.", "It is very likely that", "Confidence is moderate because", "Score: "):
        assert must in t


def test_t_names_escalation_floors_in_plain_words():
    findings = _yes(risk=S.risk_result(inputs=_yes().risk.inputs, escalators_fired=["X1", "X2"]))
    t = _cells(findings).risk_rationale
    assert ("An escalation floor of High applies: a provider named only by a third party and training or retention "
            "without an opt-out.") in t
