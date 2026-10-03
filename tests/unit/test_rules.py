"""rules.py: the rules tagger (design Appendix A 2.3 and 2.6; docs/contracts_p3.md section 3).

Offline: no network, no Gemini. Synthetic tests use the fictional vendor "Acme Payments Ltd" (V-901) from
tests/fixtures/p3_samples.py. The real-passage tests read the frozen evidence pack (runs/*/, evidence/text/) and skip
when it is absent (it is not committed). They never print evidence text: failures report ids and labels only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
import tomllib
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from footprint import rules
from footprint.models import (
    STRENGTH_ORDER,
    Capture,
    Claim,
    Document,
    EvidenceItem,
    Indicator,
    Passage,
    SourceFamily,
    VerifyResult,
)
from footprint.review import OverrideStore

_p = Path(__file__).resolve().parents[1] / "fixtures" / "p3_samples.py"
_spec = importlib.util.spec_from_file_location("p3_samples", _p)
S = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p3_samples", S)
_spec.loader.exec_module(S)

REPO = Path(__file__).resolve().parents[2]
RUNS = REPO / "runs"
AS_OF = "2026-10-02"
VID = S.VENDOR_ID
PROFILE = S.profile()
PLAN = S.depth(PROFILE)

SEEDS: dict[str, Any] = {
    "vendor_id": VID,
    "name": "Acme Payments",
    "legal_names": ["Acme Payments Ltd"],
    "aliases": ["Acme Payments", "AcmePay"],
    "domains": ["acme.example"],
    "sec_cik": "0000123456",
    "collisions": ["Acme Anvils", "Claude Reumert", "Acme Holdings (biotech, CIK 7777777)"],
    "person_scrub": ["Jane Roe"],
    "service_term": [{"term": "instant payments", "reason": "col E"}, {"term": "Sentinel", "reason": "product"}],
    "family_term": [{"term": "Treasury Services", "reason": "family"}, {"term": "NOC", "reason": "operations"}],
    "affiliate": [{"name": "Acme Labs AI (Acme Labs LLC)", "domain": "acmelabs.example", "status": "inferred"}],
    "platform_supplier": [{"name": "Voltcore (PayEngine)", "domain": "voltcore.example"}],
    "ats": {"platform": "workday", "host": "acme.wd1.myworkdayjobs.com"},
}

PRODUCT_URL = "https://www.acme.example/solutions/payments.html"
PRODUCT_TITLE = "Payments | Acme Payments"
ATS_URL = "https://acme.wd1.myworkdayjobs.com/Acme/job/NOC-Manager_JR1"
SEC_URL = "https://www.sec.gov/Archives/edgar/data/123456/000012345626000010/acme-20251231.htm"


# --------------------------------------------------------------------------- builders


def make_doc(text: str, url: str = PRODUCT_URL, *, title: str = PRODUCT_TITLE, family: SourceFamily = SourceFamily.PRD,
             kind: str = "html", published: str = "", date_basis: str = "",
             retrieved_at: str = "2026-10-01T12:00:00Z") -> tuple[Document, Capture]:
    doc_id = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cap_id = hashlib.sha256(f"raw|{url}|{text}".encode("utf-8")).hexdigest()
    cap = Capture(capture_id=cap_id, vendor_id=VID, family=family, collector="site", url_requested=url,
                  retrieved_at=retrieved_at, status=200)
    doc = Document(doc_id=doc_id, capture_id=cap_id, vendor_id=VID, family=family, url=url, title=title, kind=kind,
                   published=published, date_basis=date_basis)
    return doc, cap


def whole_passage(doc: Document, text: str, start: int = 0, end: int | None = None) -> Passage:
    end = len(text) if end is None else end
    return Passage(passage_id=Passage.make_id(doc.doc_id, start, end), doc_id=doc.doc_id, vendor_id=VID, start=start,
                   end=end, text=text[start:end])


def tag(text: str, url: str = PRODUCT_URL, *, seeds: Mapping[str, Any] | None = None, log: list[str] | None = None,
        **doc_kw: Any) -> EvidenceItem | None:
    doc, cap = make_doc(text, url, **doc_kw)
    return rules.tag_passage(whole_passage(doc, text), doc, cap, dict(seeds if seeds is not None else SEEDS),
                             PROFILE, PLAN, as_of=AS_OF, doc_text=text, log=log)


def job(text: str, **kw: Any) -> EvidenceItem | None:
    base = dict(family=SourceFamily.JOB, kind="json", title="Manager, NOC Operations", published="2026-09-21",
                date_basis="ATS posted date")
    base.update(kw)
    return tag(text, kw.pop("url", ATS_URL), **{k: v for k, v in base.items() if k != "url"})


def third_party(text: str, *, url: str = "https://www.paymentsweekly.example/news/acme",
                title: str = "Payments roundup", **kw: Any) -> EvidenceItem | None:
    return tag(text, url, title=title, family=SourceFamily.IND, published="2026-06-01",
               date_basis="json-ld datePublished", **kw)


def blog(text: str, **kw: Any) -> EvidenceItem | None:
    return tag(text, "https://www.acme.example/blog/ai-trends/", title="AI trends | Acme Payments",
               published="2026-08-01", date_basis="json-ld datePublished", **kw)


def codes(item: EvidenceItem) -> set[str]:
    return {i.code for i in item.indicators}


def check_item_invariants(item: EvidenceItem, doc_text: str, title: str = "") -> None:
    """What every rule item must satisfy, whatever its labels (contract section 3)."""
    assert doc_text[item.start:item.end] == item.excerpt
    assert rules.MIN_EXCERPT <= len(item.excerpt) <= rules.MAX_EXCERPT
    assert item.excerpt == item.excerpt.strip()
    for ind in item.indicators:
        assert ind.span and ind.span in item.excerpt, ind.code
        assert ind.code not in ("G9", "G10")
    for p in item.providers:
        assert p in item.excerpt or p in title
    assert item.method == "rule" and item.role == "Logged"
    assert item.tags.strength == rules.strength_label(
        item.tags.u_class, item.tags.sp, item.tags.rl, item.tags.locus,
        is_q=rules.is_qualifying(item), is_k=rules.is_corroborating(item)) or item.tags.ai_type == "not_ai"
    assert item.rule_labels["sp"] == item.tags.sp and item.rule_labels["rl"] == item.tags.rl
    assert item.rule_labels["temporal"] == item.temporal


# =========================================================================== config


def test_core_lexicon_tables_match_design_2_6() -> None:
    data = tomllib.loads((REPO / "config" / "lexicon.toml").read_text(encoding="utf-8"))
    assert data["core"]["terms"] == [
        "artificial intelligence", "machine learning", "deep learning", "neural", "LLM", "generative AI",
        "agentic", "AI agent", "digital employee", "NLP", "RAG", "model context protocol", "foundation model",
        "fine-tuning", "chatbot", "virtual assistant", "predictive model", "intelligent document processing",
    ]
    assert data["core"]["case_sensitive"] == ["AI", "ML", "LLMs?", "GenAI", "MCP"]
    assert set(data["guards"]) == {"MCP", "Claude", "Gemini", "Copilot", "Devin"}
    sup = {p.lower() for p in data["suppressors"]["phrases"]}
    for phrase in ("intelligent mail barcode", "imb", "intelligent inserting", "automic agents",
                   "automation analytics & intelligence", "oracle 23ai", "business intelligence", "in our dna",
                   "operating model", "user agent", "llms.txt", "recognition", "microsoft certified professional"):
        assert phrase in sup, phrase
    assert data["suppressors"]["conditional"] == ["intelligent tools"]
    for table in ("governance", "limiting", "autonomy", "use_state", "indicators", "definition_test", "loci",
                  "ai_types", "provider", "job", "building", "data_terms", "rule_suppressors"):
        assert table in data, table


def test_load_signals_is_cached_per_path_and_hashed() -> None:
    sig = rules.load_signals()
    assert rules.load_signals() is sig
    assert rules.load_signals(rules.DEFAULT_LEXICON) is sig
    assert len(sig.sha256) == 64 and sig.version == "rules_v1"


def test_sources_register_is_well_formed() -> None:
    reg = rules.load_sources()
    assert rules.load_sources() is reg and len(reg.sha256) == 64
    ids = [t.id for t in reg.types]
    assert len(ids) == len(set(ids))
    for t in reg.types:
        assert t.sr in "ABCDEF" and t.dated_by in ("publication", "retrieval") and t.reason and t.source_type
    # every SR grade of the 2.3 table is used by at least one source type
    assert {t.sr for t in reg.types} >= {"A", "B", "C", "D", "E"}


def test_load_sources_rejects_an_unknown_grade(tmp_path: Path) -> None:
    bad = tmp_path / "sources.toml"
    bad.write_text('version = "x"\n[[type]]\nid = "x"\nsource_type = "X"\nsr = "G"\nfamily = "PRD"\n'
                   'dated_by = "retrieval"\nreason = "r"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="SR"):
        rules.load_sources(bad)


def test_vendor_origin_types_are_mirrors_and_partner_press_rooms() -> None:
    """origin = "vendor" marks pages that carry the vendor's own release (footprint.cluster.independent)."""
    names = rules.vendor_origin_types()
    assert names == rules.load_sources().vendor_origin_types
    assert {"Press release mirror", "Partner press release"} <= names
    assert not names & {"Trade press", "Provider customer story", "Press release", "Product page"}


_ORIGIN_BASE = ('version = "x"\n[[type]]\nid = "x"\nsource_type = "X"\nsr = "C"\nfamily = "IND"\n'
                'dated_by = "publication"\nreason = "r"\n')


@pytest.mark.parametrize(("extra", "match"), [
    ('origin = "partner"\n', "origin"),
    ('origin = "vendor"\n[[type]]\nid = "y"\nsource_type = "X"\nsr = "C"\nfamily = "IND"\n'
     'dated_by = "publication"\nreason = "r"\n', "with and without"),
])
def test_load_sources_checks_the_origin_field(tmp_path: Path, extra: str, match: str) -> None:
    bad = tmp_path / "sources.toml"
    bad.write_text(_ORIGIN_BASE + extra, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        rules.load_sources(bad)


@pytest.mark.parametrize(("url", "title", "family", "kind", "expected"), [
    (SEC_URL, "acme-20251231", SourceFamily.REG, "html", ("SEC Form 10-K", "A", True)),
    ("https://www.sec.gov/Archives/edgar/data/123456/000012345626000011/d1defa14a.htm", "DEFA14A",
     SourceFamily.REG, "html", ("SEC Form DEFA14A", "A", True)),
    ("https://www.acme.example/privacy-notice", "Privacy", SourceFamily.LEG, "html", ("Privacy notice", "A", True)),
    ("https://www.acme.example/legal/subprocessors", "Sub-processors", SourceFamily.LEG, "html",
     ("Sub-processor list", "A", True)),
    (PRODUCT_URL, PRODUCT_TITLE, SourceFamily.PRD, "html", ("Product page", "B", True)),
    ("https://docs.acme.example/sentinel/api", "Sentinel API", SourceFamily.PRD, "html",
     ("Technical documentation", "B", True)),
    (ATS_URL, "Manager, NOC Operations", SourceFamily.JOB, "json", ("Job posting", "B", True)),
    ("https://dns.google/resolve?name=acme.example&type=TXT", "DNS acme.example", SourceFamily.DNS, "dns",
     ("DNS TXT record", "B", True)),
    ("https://www.acme.example/newsroom/press-releases/acme-sentinel", "Acme launches Sentinel", SourceFamily.PRD,
     "html", ("Press release", "C", True)),
    ("https://www.acme.example/blog/ai-trends/", "AI trends", SourceFamily.PRD, "html", ("Blog post", "C", True)),
    ("https://www.finextra.com/news/acme", "Acme Payments adds AI", SourceFamily.IND, "html",
     ("Trade press", "C", False)),
    ("https://www.voltcore.example/products/payengine", "PayEngine", SourceFamily.IND, "html",
     ("Partner page", "C", False)),
    ("https://acmelabs.example/capabilities/", "Capabilities", SourceFamily.IND, "html",
     ("Affiliate website", "C", False)),
    ("https://finance.yahoo.com/news/acme-payments", "Acme Payments", SourceFamily.IND, "html",
     ("Aggregator copy", "D", False)),
    ("https://www.reddit.com/r/payments/acme", "Acme Payments thread", SourceFamily.IND, "html",
     ("User-generated content", "E", False)),
    ("https://www.federalreserve.gov/newsevents/acme.htm", "Order", SourceFamily.IND, "html",
     ("Regulator publication", "A", False)),
    ("https://mondovisione.com/media-and-resources/news/acme-collaborates", "Acme Collaborates With Microsoft",
     SourceFamily.IND, "html", ("Press release mirror", "C", False)),
    ("https://www.googlecloudpresscorner.com/2025-12-08-Acme-Payments-Selects-Google-Cloud", "Acme selects Google",
     SourceFamily.IND, "html", ("Partner press release", "C", False)),
    ("https://cloud.google.com/customers/acme-payments", "Acme Payments customer story", SourceFamily.IND, "html",
     ("Provider customer story", "C", False)),
])
def test_sources_toml_alone_sets_sr(url: str, title: str, family: SourceFamily, kind: str,
                                    expected: tuple[str, str, bool]) -> None:
    doc, cap = make_doc("text", url, title=title, family=family, kind=kind)
    info = rules.classify_source(doc, cap, SEEDS, PROFILE)
    assert (info.source_type, info.sr, info.first_party) == expected
    if info.first_party:
        assert info.publisher == "Acme Payments"


def test_classify_source_publishers_and_legal_flags() -> None:
    doc, cap = make_doc("text", "https://www.finextra.com/news/acme", family=SourceFamily.IND)
    assert rules.classify_source(doc, cap, SEEDS, PROFILE).publisher == "Finextra"
    doc, cap = make_doc("text", "https://news.example.org/a", family=SourceFamily.IND)
    assert rules.classify_source(doc, cap, SEEDS, PROFILE).publisher == "news.example.org"
    doc, cap = make_doc("text", "https://www.acme.example/privacy-notice", family=SourceFamily.LEG)
    info = rules.classify_source(doc, cap, SEEDS, PROFILE)
    assert info.legal and not info.filing and info.dated_by == "retrieval"
    doc, cap = make_doc("text", SEC_URL, family=SourceFamily.REG)
    info = rules.classify_source(doc, cap, SEEDS, PROFILE)
    assert info.filing and not info.legal and info.dated_by == "publication"


# =========================================================================== traps (design 2.6 and 2.14 list)


TRAPS = {
    "IMb": "Every statement carries an Intelligent Mail barcode (IMb) so the USPS can track it.",
    "IMb alone": "We print the IMb on every envelope before it leaves the plant.",
    "intelligent inserting": "Intelligent inserting matches every insert to the right statement.",
    "intelligent tools": "Acme Payments announced new intelligent tools for statement design.",
    "Automic agents": "Automic agents run the nightly batch on every server.",
    "Analytics & Intelligence": "Automation Analytics & Intelligence dashboards show SLA trends.",
    "23ai": "We upgraded the payments database to Oracle 23ai last quarter.",
    "23ai bare": "The ledger now runs on 23ai with no other changes to the platform.",
    "Claude Reumert": "Claude Reumert, head of strategy, presented the instant payments roadmap.",
    "recognition": "Acme Payments won industry recognition for client service.",
    "contAIner": "Each contAIner image is scanned before it is released.",
    "in our DNA": "Client service excellence is in our DNA.",
    "MCP = Microsoft Certified Professional": "Our engineers hold the MCP certification from Microsoft.",
    "Microsoft Certified Professional (MCP)": "Our support team includes Microsoft Certified Professional (MCP) staff.",
    "business intelligence": "Business intelligence dashboards summarise daily payment volumes.",
    "user agent": "The crawler identifies itself with a custom user agent string.",
    "operating model": "Our operating model keeps support close to every client.",
    "Gemini without Google": "Clients in the Gemini programme receive quarterly statements.",
    "Copilot without Microsoft": "Our analysts use Copilot to summarise alerts for every flagged payment.",
    "Devin without Cognition": "Devin leads the payments migration team in Charlotte.",
}


@pytest.mark.parametrize("name", sorted(TRAPS))
def test_trap_phrases_never_count_as_ai(name: str) -> None:
    log: list[str] = []
    assert tag(TRAPS[name], log=log) is None
    assert log and "no AI term" in log[-1]


def test_llms_txt_documents_are_skipped_whole() -> None:
    log: list[str] = []
    text = "This is an llms.txt file, meant for consumption by LLMs. It lists AI-friendly pages of the site."
    assert tag(text, "https://www.acme.example/llms.txt", log=log) is None
    assert "llms.txt" in log[-1]
    # the Yoast header line is suppressed on any URL
    assert tag("This is an llms.txt file, meant for consumption by LLMs.", "https://www.acme.example/x") is None


def test_guarded_terms_count_with_their_context() -> None:
    claude = tag("Sentinel drafts case notes with Claude from Anthropic for every flagged payment.")
    assert claude is not None and "Claude" in claude.providers and "Anthropic" in claude.providers
    opus = tag("Sentinel drafts case notes with Claude Opus 4 for every flagged payment.")
    assert opus is not None and opus.providers == ["Claude Opus 4"]
    gemini = tag("Sentinel summarises alerts with Google Gemini for every flagged payment.")
    assert gemini is not None and gemini.providers == ["Gemini"]
    copilot = tag("Our engineers use GitHub Copilot to write unit tests for Sentinel.")
    assert copilot is not None and copilot.providers == ["GitHub Copilot"] and copilot.tags.locus == "sdlc"
    devin = tag("Our engineers use Devin from Cognition to migrate legacy code for Sentinel.")
    assert devin is not None and devin.providers == ["Devin"]
    mcp = tag("Sentinel exposes an MCP server for the model context protocol clients of our partners.")
    assert mcp is not None


def test_claude_reumert_beside_an_ai_claim_is_never_a_provider() -> None:
    item = third_party("Claude Reumert said Acme Payments uses AI for fraud detection on instant payments. "
                       "See acme.example for details.", title="Acme Payments interview")
    assert item is not None
    assert item.providers == [] and "G1" not in codes(item)
    assert "Claude" not in json.dumps(item.rule_labels)


def test_mcp_counts_only_with_model_context_protocol_or_another_ai_term() -> None:
    assert tag("Our team lists MCP among its core skills for the year.") is None
    assert tag("The MCP server lets AI agents read Sentinel payment status in real time.") is not None
    text = "Sentinel speaks the Model Context Protocol. Its MCP server is generally available today."
    doc, cap = make_doc(text)
    start = text.index("Its MCP")
    item = rules.tag_passage(whole_passage(doc, text, start), doc, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF,
                             doc_text=text)
    assert item is not None and item.excerpt.startswith("Its MCP")  # the document names the protocol


# =========================================================================== test 1: definition (EU AI Act Art. 3(1))


@pytest.mark.parametrize("text", [
    "Our AI-powered batch scheduling runs four thousand jobs every night.",
    "Acme Payments offers AI workload automation across every core system.",
    "The AI rules engine approves standard payments in seconds for every client.",
    "Our AI-powered smart routing sends every statement to the right printer overnight.",
    "Statements are built with AI templates for every client and every tax form.",
    "Clients see payment volumes on AI dashboards updated every hour of the day.",
    "Our RPA bots, powered by AI, key every remittance into the ledger.",
])
def test_definition_test_failures_become_visible_never_citable_traps(text: str) -> None:
    item = tag(text)
    assert item is not None
    assert item.tags.ai_type == "not_ai" and item.tags.u_class == "U7"
    assert item.strength == "Marketing only" and "M4" in codes(item)
    assert not item.citable and not rules.is_qualifying(item) and not rules.is_corroborating(item)
    assert item.rule_labels["claim_kind"] == "not_ai"


def test_definition_test_passes_when_ml_is_evidenced() -> None:
    item = tag("Our AI-powered batch scheduling uses a machine learning model to predict job run times.")
    assert item is not None and item.tags.ai_type != "not_ai" and "M4" not in codes(item) and item.citable


# =========================================================================== entity guard and test 2 (subject)


def entity(text: str, url: str, *, title: str = "", family: SourceFamily = SourceFamily.IND,
           log: list[str] | None = None) -> bool:
    doc, cap = make_doc(text, url, title=title, family=family)
    return rules.entity_ok(doc, cap, SEEDS, PROFILE, text=text, log=log)


def test_entity_guard_accepts_first_party_ats_dns_filings_and_affiliates() -> None:
    assert entity("anything", PRODUCT_URL, family=SourceFamily.PRD)
    assert entity("anything", ATS_URL, family=SourceFamily.JOB)
    assert entity("anything", "https://dns.google/resolve?name=acme.example&type=TXT", family=SourceFamily.DNS)
    assert entity("anything", SEC_URL, family=SourceFamily.REG)
    assert entity("anything", "https://acmelabs.example/", family=SourceFamily.IND)  # context only
    assert entity("anything", "https://www.voltcore.example/x", family=SourceFamily.IND)  # context only


def test_entity_guard_needs_name_plus_corroboration_on_third_party_pages() -> None:
    url = "https://www.paymentsweekly.example/news/a"
    assert entity("Acme Payments Ltd uses AI for fraud detection.", url)  # legal name
    assert entity("Acme Payments uses AI. Read more at https://www.acme.example/ai.", url)  # name + link
    assert entity("AcmePay uses AI for instant payments.", url)  # alias + its product
    log: list[str] = []
    assert not entity("AcmePay uses AI for marketing emails.", url, log=log)
    assert "not corroborated" in log[-1]
    log = []
    assert not entity("Globex uses AI for marketing emails.", url, log=log)
    assert "does not name the vendor" in log[-1]


def test_entity_guard_rejects_seeded_collisions() -> None:
    log: list[str] = []
    assert not entity("Acme Anvils uses AI to forecast anvil demand.", "https://news.example/a",
                      title="Acme Anvils adopts AI", log=log)
    assert "collision" in log[-1] and "Acme Anvils" in log[-1]
    log = []
    other_filer = "https://www.sec.gov/Archives/edgar/data/7777777/000077777726000001/x10k.htm"
    assert not entity("Acme Payments Ltd uses AI.", other_filer, family=SourceFamily.REG, log=log)
    assert "CIK 7777777" in log[-1]


def test_entity_failure_and_subject_failure_drop_the_passage_with_a_reason() -> None:
    log: list[str] = []
    assert third_party("Acme Anvils uses AI to forecast anvil demand.", title="Acme Anvils adopts AI",
                       log=log) is None
    assert "entity guard" in log[-1]
    # the page names the vendor (entity guard passes) but the AI sentence and the title are about someone else
    text = ("Acme Payments Ltd sponsored the summit this year.\n\n"
            "Globex uses machine learning to approve loans in seconds, its CEO said on Monday.")
    doc, cap = make_doc(text, "https://www.paymentsweekly.example/news/summit", title="Payments summit recap",
                        family=SourceFamily.IND, published="2026-06-01", date_basis="json-ld datePublished")
    log = []
    item = rules.tag_passage(whole_passage(doc, text, text.index("Globex")), doc, cap, SEEDS, PROFILE, PLAN,
                             as_of=AS_OF, doc_text=text, log=log)
    assert item is None and "subject test failed" in log[-1]
    # a third-party sentence passes when the title names the vendor (design 2.3, test 2)
    doc2, cap2 = make_doc(text, "https://www.paymentsweekly.example/news/summit2",
                          title="Acme Payments Ltd and Globex at the summit", family=SourceFamily.IND)
    assert rules.tag_passage(whole_passage(doc2, text, text.index("Globex")), doc2, cap2, SEEDS, PROFILE, PLAN,
                             as_of=AS_OF, doc_text=text) is not None


# =========================================================================== indicators


def test_find_indicators_spans_are_exact_substrings_in_text_order() -> None:
    text = ("Sentinel runs on Azure OpenAI GPT-4o, keeps customer data inside the customer boundary, and cut false "
            "positives by 42% for fraud detection; it will soon add summaries.")
    found = rules.find_indicators(text)
    assert all(i.span in text for i in found)
    positions = [text.find(i.span) for i in found]
    assert positions == sorted(positions)
    by_code = {(i.code, i.span) for i in found}
    assert ("G1", "Azure OpenAI") in by_code and ("G1", "GPT-4o") in by_code
    assert ("G3", "customer data") in by_code and ("G11", "42%") in by_code
    assert ("G2", "fraud detection") in by_code and ("M2", "will") in by_code
    assert len(found) == len(set(by_code))  # one Indicator per (code, span)


def test_find_indicators_adds_g6_and_g7_from_the_source_type_only() -> None:
    text = "We use artificial intelligence to improve our services."
    assert not {"G6", "G7"} & {i.code for i in rules.find_indicators(text)}
    legal = rules.SourceInfo(source_type="Privacy notice", publisher="Acme Payments", sr="A", first_party=True,
                             dated_by="retrieval", legal=True)
    filing = legal.model_copy(update={"source_type": "SEC Form 10-K", "legal": False, "filing": True})
    assert [i for i in rules.find_indicators(text, source=legal) if i.code == "G6"] == [
        Indicator(code="G6", span="artificial intelligence")]
    assert [i for i in rules.find_indicators(text, source=filing) if i.code == "G7"] == [
        Indicator(code="G7", span="artificial intelligence")]


def test_find_indicators_flags_automation_relabelled_as_ai_only_without_ml() -> None:
    assert "M4" in {i.code for i in rules.find_indicators("Our AI scheduling runs every batch overnight.")}
    assert "M4" not in {i.code for i in rules.find_indicators(
        "Our AI scheduling uses a machine learning model to order the batch.")}


@pytest.mark.parametrize(("code", "span", "ok"), [
    ("G1", "OpenAI", True), ("G1", "Claude Opus 4", True), ("G1", "Claude", False), ("G1", "Claude Reumert", False),
    ("G1", "Gemini", False), ("G1", "Google Gemini", True), ("G1", "the weather", False),
    ("G2", "fraud detection", True), ("G2", "the weather", False),
    ("G3", "customer data", True), ("G3", "human in the loop", True), ("G3", "nice offices", False),
    ("G4", "generally available", True), ("G5", "REST API", True), ("G8", "use AI tools", True),
    ("G11", "42%", True), ("G11", "many", False),
    ("M1", "cutting-edge", True), ("M2", "roadmap", True), ("M2", "may", True), ("M2", "today", False),
    ("M3", "the industry", True), ("M4", "AI scheduling", True), ("M5", "industry-leading", True),
    ("M6", "AI", True), ("M7", "logo", True),
    ("G6", "artificial intelligence", False), ("G7", "artificial intelligence", False),
    ("G9", "anything", False), ("G10", "anything", False), ("G2", "  ", False),
])
def test_indicator_ok_lexical_tests(code: str, span: str, ok: bool) -> None:
    assert rules.indicator_ok(code, span) is ok


# =========================================================================== SP, RL, RC, strength


def ind(*codes_: str) -> list[Indicator]:
    return [Indicator(code=c, span=f"span {c}") for c in codes_]


@pytest.mark.parametrize(("codes_", "named", "temporal", "grade"), [
    ((), True, "unclear", "S1"),
    (("M1", "M5"), True, "in_production", "S1"),
    (("M2",), True, "unclear", "S0"),
    (("M3", "M1"), True, "unclear", "S0"),
    (("M2", "G1"), True, "unclear", "S0"),  # G1 alone does not outweigh M2 (not in the dominance list)
    (("M2", "G2"), True, "unclear", "S2"),  # a named feature outweighs a hedge
    (("G2",), True, "planned", "S0"),  # planned always S0
    (("G1", "G2"), True, "in_production", "S3"),
    (("G5", "G3"), False, "unclear", "S3"),
    (("G6", "G3"), False, "unclear", "S3"),
    (("G7", "G2"), False, "unclear", "S3"),
    (("G6",), True, "in_production", "S1"),  # legal artifact alone
    (("G7",), True, "in_production", "S1"),  # filing alone
    (("G2",), True, "in_production", "S2"),
    (("G2",), False, "in_production", "S1"),  # not tied to a named product or function
    (("G4",), True, "unclear", "S2"),
    (("G8",), True, "unclear", "S2"),
    (("G11",), True, "unclear", "S2"),
    (("G2", "M4"), True, "in_production", "S1"),  # automation relabelled as AI
    (("G1",), True, "in_production", "S1"),
    (("G2",), True, "pilot_or_beta", "S2"),  # a pilot is limited deployment, not M2
])
def test_specificity_rules_in_order(codes_: tuple[str, ...], named: bool, temporal: str, grade: str) -> None:
    assert rules.specificity(ind(*codes_), named_target=named, temporal=temporal) == grade


@pytest.mark.parametrize(("text", "locus", "title", "heading", "grade"), [
    ("Analysts discuss AI.", "commentary", "Instant payments", "", "R0"),
    ("PayEngine uses AI for instant payments.", "platform_supplier", "", "", "R1"),
    ("Acme Labs uses AI for instant payments.", "affiliate_inferred", "", "", "R1"),
    ("openai-domain-verification=abc", "relationship", "", "", "R2"),
    ("Sentinel uses AI.", "service_feature", "", "", "R3"),
    ("AI screens Instant Payments.", "vendor_addon", "", "", "R3"),  # case-insensitive
    ("AI screens every payment.", "service_feature", "Instant payments | Acme", "", "R3"),  # page title
    ("AI screens every payment.", "delivery_ops", "", "Instant payments", "R3"),  # heading
    ("AI screens instant payments.", "corporate_internal", "", "", "R1"),  # not a service locus
    ("AI screens instant payments.", "sdlc", "", "", "R2"),
    ("AI supports Treasury Services.", "service_feature", "", "", "R2"),  # family term
    ("AI drafts NOC runbooks.", "corporate_internal", "", "", "R2"),
    ("AI resolves tickets.", "delivery_ops", "", "", "R2"),  # company-wide delivery practice
    ("AI writes code.", "sdlc", "", "", "R2"),
    ("AI writes marketing emails.", "service_feature", "", "", "R1"),
    ("AI screens instant paymentsX.", "service_feature", "", "", "R1"),  # whole words only
])
def test_relevance_is_computed_locally(text: str, locus: str, title: str, heading: str, grade: str) -> None:
    assert rules.relevance(text, SEEDS, locus, title=title, heading=heading) == grade


def test_relevance_masks_the_vendors_own_names() -> None:
    seeds = {**SEEDS, "aliases": ["The Clearing House"], "service_term": [{"term": "clearing"}]}
    assert rules.relevance("The Clearing House uses AI for marketing.", seeds, "service_feature") == "R1"
    assert rules.relevance("AI supports clearing at The Clearing House.", seeds, "service_feature") == "R3"


def test_r3_needs_the_service_term_in_the_claim_sentence_heading_or_title() -> None:
    text = "Acme Payments offers instant payments and wires. Our cash forecasting tool uses machine learning."
    item = tag(text, title="Treasury Services | Acme Payments")
    assert item is not None and item.excerpt.startswith("Our cash forecasting")
    assert item.tags.rl == "R2"  # the family term in the title, not the service term of the other sentence
    item = tag(text, title="Cash tools | Acme Payments")
    assert item is not None and item.tags.rl == "R1"


@pytest.mark.parametrize(("published", "retrieved", "dated_by", "grade"), [
    ("2026-09-30", "2026-10-01T00:00:00Z", "publication", "T3"),
    ("2025-10-02", "2026-10-01T00:00:00Z", "publication", "T3"),  # exactly 12 months
    ("2025-09-03", "2026-10-01T00:00:00Z", "publication", "T3"),  # 12 whole months (and 29 days)
    ("2025-09-02", "2026-10-01T00:00:00Z", "publication", "T2"),  # 13 whole months
    ("2024-10-02", "2026-10-01T00:00:00Z", "publication", "T2"),  # 24
    ("2024-09-02", "2026-10-01T00:00:00Z", "publication", "T1"),  # 25
    ("2023-10-01", "2026-10-01T00:00:00Z", "publication", "T1"),  # 36 whole months
    ("2023-09-02", "2026-10-01T00:00:00Z", "publication", "T0"),  # 37
    ("2017-03-01", "2026-10-01T00:00:00Z", "publication", "T0"),
    ("", "2026-10-01T00:00:00Z", "publication", "T0"),  # no article date: undated, history only (design 2.3)
    ("2017-03-01", "2026-10-01T00:00:00Z", "retrieval", "T3"),  # live pages are dated by retrieval
    ("", "2026-10-01T00:00:00Z", "retrieval", "T3"),
    ("", "", "publication", "T0"),  # undated
    ("2027-01-01", "2026-10-01T00:00:00Z", "publication", "T3"),  # after as_of counts as 0 months
])
def test_recency_against_as_of(published: str, retrieved: str, dated_by: str, grade: str) -> None:
    assert rules.recency(published, retrieved, AS_OF, dated_by=dated_by) == grade


def test_recency_needs_an_iso_as_of() -> None:
    with pytest.raises(ValueError):
        rules.recency("2026-01-01", "", "2 Oct 2026", dated_by="publication")


@pytest.mark.parametrize(("u", "sp", "rl", "locus", "q", "k", "label"), [
    ("U8", "S3", "R3", "service_feature", True, True, "Negative"),
    ("U1", "S2", "R3", "service_feature", True, True, "Strong"),
    ("U1", "S2", "R2", "service_feature", True, False, "Moderate"),
    ("U3", "S2", "R3", "delivery_ops", False, True, "Moderate"),  # K at R3 is still Moderate
    ("U4", "S1", "R2", "relationship", False, False, "Context - relationship only"),
    ("U2", "S2", "R1", "platform_supplier", False, False, "Context - platform supplier"),
    ("U7", "S1", "R1", "affiliate_inferred", False, False, "Context - inferred affiliate"),
    ("U7", "S1", "R3", "service_feature", False, False, "Marketing only"),
    ("U1", "S0", "R3", "service_feature", False, False, "Marketing only"),
    ("U2", "S1", "R1", "service_feature", False, False, "Weak"),
    ("U6", "S1", "R2", "corporate_internal", False, False, "Weak"),
])
def test_strength_label_fixed_order(u: str, sp: str, rl: str, locus: str, q: bool, k: bool, label: str) -> None:
    assert rules.strength_label(u, sp, rl, locus, is_q=q, is_k=k) == label
    assert label in STRENGTH_ORDER


def test_a_dns_token_is_always_context_never_weak() -> None:
    assert rules.strength_label("U4", "S1", "R2", "relationship", is_q=False, is_k=False) == \
        "Context - relationship only"


def test_qualifying_and_corroborating_predicates() -> None:
    q = S.item()
    assert rules.is_qualifying(q) and rules.is_corroborating(q)
    for upd, is_q, is_k in [
        ({"sr": "C"}, False, True),
        ({"sr": "D"}, False, False),
        ({"sp": "S1"}, False, False),
        ({"rl": "R1"}, False, False),
        ({"rc": "T0"}, False, False),
        ({"rc": "T1"}, True, True),
        ({"u_class": "U5"}, False, False),
        ({"u_class": "U4"}, True, True),
        ({"locus": "corporate_internal"}, False, True),
        ({"locus": "commentary"}, False, True),
    ]:
        it = S.item(tags=S.tags(**upd))
        assert (rules.is_qualifying(it), rules.is_corroborating(it)) == (is_q, is_k), upd
    rejected = q.model_copy(update={"review_status": "rejected"})
    assert not rules.is_qualifying(rejected) and not rules.is_corroborating(rejected)
    trap = S.item(tags=S.tags(ai_type="not_ai"))
    assert not rules.is_qualifying(trap) and not rules.is_corroborating(trap)
    proposal = q.model_copy(update={"method": "llm_proposed_accepted"})
    assert not rules.is_qualifying(proposal)


# =========================================================================== tag_passage: synthetic


def test_first_party_service_claim_is_strong_with_exact_offsets() -> None:
    lead = "Instant payments\n\nOUR VALUE\n\n"
    claim = "Sentinel uses a machine learning model to screen every inbound instant payment for fraud detection."
    text = lead + claim + "\n\nContact us today."
    doc, cap = make_doc(text)
    start = len(lead)
    item = rules.tag_passage(whole_passage(doc, text, start), doc, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF,
                             doc_text=text)
    assert item is not None
    check_item_invariants(item, text, doc.title)
    assert item.excerpt == claim and item.start == start  # document offsets
    assert item.tag_string == "U1 · SR:B · SP:S2 · RL:R3 · RC:T3 · IC:2 · locus=service_feature"
    assert item.strength == "Strong" and item.temporal == "in_production" and item.tags.ai_type == "predictive_ml"
    assert (item.source_type, item.publisher, item.family) == ("Product page", "Acme Payments", SourceFamily.PRD)
    assert item.published == "" and item.date_basis == "retrieval"
    assert item.passage_id == whole_passage(doc, text, start).passage_id
    assert item.vendor_id == VID and item.doc_id == doc.doc_id and item.capture_id == cap.capture_id


def test_tag_passage_without_doc_text_keeps_document_offsets() -> None:
    text = "Header line.\n\nSentinel uses machine learning for fraud detection on instant payments."
    doc, cap = make_doc(text)
    start = text.index("Sentinel")
    p = whole_passage(doc, text, start)
    a = rules.tag_passage(p, doc, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF, doc_text=text)
    b = rules.tag_passage(p, doc, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF)
    assert a is not None and b is not None and (a.start, a.end, a.excerpt) == (b.start, b.end, b.excerpt)


def test_tag_passage_rejects_mismatched_inputs() -> None:
    text = "Sentinel uses machine learning for fraud detection on instant payments."
    doc, cap = make_doc(text)
    p = whole_passage(doc, text)
    with pytest.raises(ValueError):
        rules.tag_passage(p, doc, cap, SEEDS, S.profile(vendor_id="V-902"), PLAN, as_of=AS_OF, doc_text=text)
    with pytest.raises(ValueError):
        rules.tag_passage(p, doc, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF, doc_text="x" + text)


def test_excerpt_is_the_smallest_run_of_whole_sentences() -> None:
    text = ("Acme Payments serves 400 banks. Sentinel uses machine learning for fraud detection on instant payments. "
            "Our offices are in Leeds and Austin.")
    item = tag(text)
    assert item is not None and item.excerpt == ("Sentinel uses machine learning for fraud detection on instant "
                                                 "payments.")
    short = tag("Acme Payments serves banks. Sentinel uses AI. Our offices are in Leeds and in Austin.")
    assert short is not None and len(short.excerpt) >= rules.MIN_EXCERPT
    assert short.excerpt in ("Acme Payments serves banks. Sentinel uses AI.", "Sentinel uses AI. Our offices are "
                             "in Leeds and in Austin.")


def test_a_closing_curly_quote_ends_a_sentence() -> None:
    item = tag("Instead of “can it run my jobs?” you start asking “can it govern my AI?” At Acme Payments, we help "
               "teams upgrade their platforms.")
    assert item is not None and item.excerpt.endswith("govern my AI?”")


def test_navigation_link_titles_are_never_claims() -> None:
    log: list[str] = []
    assert tag("Running AI in Production Starts Below the Model Read more > Recent Posts", log=log) is None
    assert "navigation" in log[-1]
    assert tag("Keywords: Treasury Services embedded AI infrastructure instant payments liquidity") is None


def test_modal_hedges_and_plans_are_s0_marketing() -> None:
    hedge = tag("Sentinel can detect fraud on instant payments with AI.")
    assert hedge is not None and hedge.temporal == "planned" and hedge.tags.sp == "S0"
    assert hedge.strength == "Marketing only" and "M2" in codes(hedge)
    plan = tag("Sentinel will add generative AI summaries for instant payments next year.")
    assert plan is not None and plan.tags.sp == "S0" and plan.strength == "Marketing only"


def test_pilot_is_limited_deployment_not_aspiration() -> None:
    item = tag("Acme Payments is piloting an AI model for exception management in Treasury Services.")
    assert item is not None and item.temporal == "pilot_or_beta" and item.tags.sp == "S2"
    assert item.strength == "Moderate"


def test_negative_statement_is_u8() -> None:
    item = tag("Acme Payments does not use AI or machine learning in the instant payments service.")
    assert item is not None and item.tags.u_class == "U8" and item.strength == "Negative"
    assert item.rule_labels["claim_kind"] == "negative_or_limiting"


def test_governance_statement_is_u6() -> None:
    item = tag("Our AI policy follows the NIST AI RMF and every model is reviewed by our AI committee.")
    assert item is not None and item.tags.u_class == "U6"


def test_generic_filing_language_is_s1() -> None:
    text = "We use artificial intelligence to improve our services."
    item = tag(text, SEC_URL, title="acme-20251231", family=SourceFamily.REG, published="2026-02-20",
               date_basis="edgar filing date")
    assert item is not None
    assert item.tags.sr == "A" and item.tags.sp == "S1" and "G7" in codes(item)
    assert item.strength == "Weak" and not rules.is_qualifying(item)
    specific = tag("Sentinel uses machine learning models trained on customer data, with human review of every "
                   "alert.", SEC_URL, title="acme-20251231", family=SourceFamily.REG, published="2026-02-20",
                   date_basis="edgar filing date")
    assert specific is not None and specific.tags.sp == "S3" and specific.strength == "Strong"
    assert specific.action_level == "human_reviewed_decision" and specific.published == "2026-02-20"


def test_provider_relationship_is_u4_context() -> None:
    item = tag("Acme Payments partners with OpenAI in a multi-year strategic alliance.",
               "https://www.acme.example/newsroom/press-releases/acme-openai", title="Acme and OpenAI")
    assert item is not None
    assert (item.tags.u_class, item.tags.locus, item.tags.rl) == ("U4", "relationship", "R2")
    assert item.strength == "Context - relationship only" and item.providers == ["OpenAI"]


def test_reader_advice_and_industry_commentary_are_marketing_r0() -> None:
    for text in ("Banks should use AI to personalise every offer.",
                 "Use AI to track member interactions so staff never ask twice.",
                 "Fraudsters use generative AI to create synthetic identities at scale.",
                 "The 2025 Payments Conference & Expo shone a spotlight on AI in statement workflows."):
        item = blog(text)
        assert item is not None, text
        assert (item.tags.locus, item.tags.rl, item.tags.u_class) == ("commentary", "R0", "U7"), text
        assert item.strength == "Marketing only" and "M3" in codes(item)
        assert item.rule_labels["claim_kind"] == "industry_commentary"


def test_first_person_on_a_vendor_blog_is_not_commentary() -> None:
    item = blog("Our NOC engineers use an AI assistant to summarise incidents and draft runbooks for clients.")
    assert item is not None and item.tags.locus == "delivery_ops" and item.tags.rl == "R2"
    assert item.tags.u_class == "U3" and item.tags.sr == "C" and item.strength == "Moderate"


def test_third_party_trade_press_is_sr_c_and_can_corroborate() -> None:
    item = third_party("Acme Payments uses machine learning for fraud detection on instant payments, the company "
                       "said. More at acme.example.", title="Acme Payments adds machine learning to Sentinel")
    assert item is not None
    assert (item.source_type, item.tags.sr, item.tags.rl) == ("Trade press", "C", "R3")
    assert item.published == "2026-06-01" and item.date_basis == "json-ld datePublished"
    assert rules.is_corroborating(item) and not rules.is_qualifying(item) and item.strength == "Moderate"
    assert item.tags.ic == 4  # uncorroborated C at S2+


def test_platform_supplier_and_inferred_affiliate_are_context_only() -> None:
    supplier = tag("PayEngine uses machine learning for fraud detection on instant payments.",
                   "https://www.voltcore.example/products/payengine", title="PayEngine 5", family=SourceFamily.IND)
    assert supplier is not None and supplier.tags.locus == "platform_supplier" and supplier.tags.rl == "R1"
    assert supplier.strength == "Context - platform supplier" and not rules.is_corroborating(supplier)
    affiliate = tag("Acme Labs runs open-source large language models on local GPU inference for every engagement.",
                    "https://acmelabs.example/capabilities/", title="Acme Labs AI capabilities",
                    family=SourceFamily.IND)
    assert affiliate is not None and affiliate.tags.locus == "affiliate_inferred"
    assert affiliate.strength == "Context - inferred affiliate"


def test_supplier_product_named_only_in_the_title_is_supplier_context() -> None:
    """*(interpretation)* AutomWorx blogs about Automic V26 describe Broadcom's product, not AutomWorx's use."""
    text = "The AI Assistant explains every exception and generates scripts on request."
    item = tag(text, "https://www.acme.example/blog/payengine-5-whats-new/",
               title="Voltcore PayEngine 5: what's new | Acme Payments")
    assert item is not None and item.tags.locus == "platform_supplier"
    assert item.strength == "Context - platform supplier"
    own = tag("Our analysts use an AI assistant that explains every exception on request.",
              "https://www.acme.example/blog/payengine-5-whats-new/",
              title="Voltcore PayEngine 5: what's new | Acme Payments")
    assert own is not None and own.tags.locus != "platform_supplier"  # first person: the vendor's own use


def test_job_posting_duties_are_u3_and_skills_u5() -> None:
    duty = job("Utilize AI-powered tools such as Microsoft Copilot and ChatGPT to enhance documentation and "
               "incident response.")
    assert duty is not None
    assert (duty.tags.u_class, duty.tags.rl, duty.tags.locus) == ("U3", "R2", "delivery_ops")
    assert "G8" in codes(duty) and duty.providers == ["Microsoft Copilot", "ChatGPT"]
    assert (duty.source_type, duty.tags.sr, duty.family) == ("Job posting", "B", SourceFamily.JOB)
    assert duty.rule_labels["claim_kind"] == "ai_hiring" and duty.published == "2026-09-21"
    skill = job("Experience with machine learning frameworks such as PyTorch is preferred.",
                url="https://acme.wd1.myworkdayjobs.com/Acme/job/Engineer_JR2", title="Software Engineer")
    assert skill is not None and skill.tags.u_class == "U5" and "G8" not in codes(skill)
    assert not rules.is_qualifying(skill)


def test_job_postings_are_capped_at_s2_and_will_describes_the_role() -> None:
    item = job("You will use GitHub Copilot to generate code and review customer data flows for the Sentinel API.",
               url="https://acme.wd1.myworkdayjobs.com/Acme/job/Engineer_JR3", title="Software Engineer, Sentinel")
    assert item is not None
    assert {"G1", "G3"} <= codes(item) and item.tags.sp == "S2"  # S3 by the rules, capped at S2
    assert item.temporal != "planned" and "M2" not in codes(item)
    assert item.tags.u_class == "U3" and item.tags.locus == "sdlc"


def test_old_first_person_blog_is_t0_and_never_counts() -> None:
    item = tag("We use machine learning for fraud detection on instant payments.",
               "https://www.acme.example/blog/old-post/", title="Our analytics journey | Acme Payments",
               published="2017-03-01", date_basis="json-ld datePublished")
    assert item is not None and item.tags.rc == "T0" and item.tags.ic == 6
    assert not rules.is_qualifying(item) and not rules.is_corroborating(item)


def test_undated_article_is_t0_not_the_retrieval_date() -> None:
    """Design 2.3 and README: 'T0 is older or undated'. An article, release or filing without a publication date
    is history only; the retrieval date never stands in for it (a review finding: an undated pair gave Yes)."""
    text = "Acme Payments uses a machine learning model for fraud detection on instant payments."
    url = "https://www.acme.example/news/press-releases/acme-sentinel-ml"
    undated = tag(text, url, title="Acme launches Sentinel ML")
    assert undated is not None and undated.source_type == "Press release" and undated.published == ""
    assert undated.tags.rc == "T0" and undated.tags.ic == 6
    assert not rules.is_corroborating(undated) and not rules.is_qualifying(undated)
    dated = tag(text, url, title="Acme launches Sentinel ML", published="2026-06-01",
                date_basis="json-ld datePublished")
    assert dated is not None and dated.tags.rc == "T3" and rules.is_corroborating(dated)
    live = tag(text)  # a product page is dated by retrieval
    assert live is not None and live.tags.rc == "T3"


DEFA14A_URL = "https://www.sec.gov/Archives/edgar/data/123456/000012345626000011/d1defa14a.htm"


def defa14a(text: str) -> EvidenceItem | None:
    return tag(text, DEFA14A_URL, title="DEFA14A", family=SourceFamily.REG, published="2026-05-15",
               date_basis="edgar filing date")


def test_past_tense_deployment_cue_is_in_production() -> None:
    """Design 5, V-002: the Investor Day ticket-resolution statement carries E2. A change already made ('two weeks
    ago, we re-architected ...') is in production, and resolving named tickets is observable behaviour (G2)."""
    item = defa14a("Two weeks ago, we re-architected our client service portal to drive more self-service and "
                   "agentic AI capabilities to resolve our tickets.")
    assert item is not None and item.source_type == "SEC Form DEFA14A"
    assert item.temporal == "in_production" and "G2" in codes(item)
    assert (item.tags.u_class, item.tags.locus, item.tags.rl) == ("U3", "delivery_ops", "R2")
    assert item.tags.sp in ("S2", "S3") and rules.is_qualifying(item) and item.strength == "Moderate"
    # aspirational wording still wins over a past-tense cue: an announcement of plans is planned
    plan = defa14a("Two weeks ago, we announced plans to use agentic AI on our service tickets next year.")
    assert plan is not None and plan.temporal == "planned" and not rules.is_qualifying(plan)


def test_first_party_operations_on_a_named_artefact_are_not_marketing() -> None:
    """A first-person delivery sentence naming tickets or cases is the vendor's own operations: never the U7
    'Marketing only' short-circuit, even with an unclear use state."""
    item = defa14a("And then there is the agentic AI on our service tickets.")
    assert item is not None and item.temporal == "unclear"
    assert item.tags.u_class == "U3" and item.tags.locus == "delivery_ops" and item.strength != "Marketing only"
    # without the first person (or without a named artefact) a generic sentence stays marketing
    generic = defa14a("And then there is agentic AI in customer service.")
    assert generic is not None and generic.tags.u_class == "U7" and generic.strength == "Marketing only"


def test_second_person_is_not_commentary_in_a_first_party_transcript() -> None:
    """'you' in a spoken investor-day transcript is spoken style, not reader advice (V-002-E-0096); on a vendor
    blog 'you' is still reader advice."""
    text = ("Once tickets are created, the tickets are resolved faster because you have the agentic aid to answer "
            "the question.")
    spoken = defa14a(text)
    assert spoken is not None and spoken.tags.locus != "commentary" and spoken.tags.rl != "R0"
    assert "M3" not in codes(spoken)
    advice = blog("You can use agentic AI to answer every question faster.")
    assert advice is not None and advice.tags.locus == "commentary" and "M3" in codes(advice)


class _GoldSeeds(dict):
    """Seeds whose gold labels must never be read (contract section 0)."""

    FORBIDDEN = frozenset({"expect", "relevance", "note", "seed"})

    def __getitem__(self, key: str) -> Any:
        if key in self.FORBIDDEN:
            raise AssertionError(f"rules read the gold label {key!r}")
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.FORBIDDEN:
            raise AssertionError(f"rules read the gold label {key!r}")
        return super().get(key, default)

    def items(self):  # type: ignore[override]
        raise AssertionError("rules iterated over every seed key")

    def values(self):  # type: ignore[override]
        raise AssertionError("rules iterated over every seed key")


def test_rules_never_read_seed_gold_labels_and_are_deterministic() -> None:
    gold = _GoldSeeds({**SEEDS, "expect": "strong", "relevance": "service-specific", "note": "x",
                       "seed": [{"url": PRODUCT_URL, "expect": "strong"}]})
    text = "Sentinel uses a machine learning model to screen every inbound instant payment for fraud detection."
    a = tag(text, seeds=gold)
    b = tag(text)
    assert a is not None and b is not None and a.model_dump() == b.model_dump()


# =========================================================================== tag_document (DNS)


DNS_TEXT = ("DNS records for acme.example (DoH: dns.google, cloudflare-dns.com)\n"
            "TXT\topenai-domain-verification=dv-abc123\n"
            "TXT\tv=spf1 include:_spf.google.com ~all\n"
            "AI_TOKEN\tOpenAI\topenai-domain-verification=dv-abc123\n")


def test_dns_ai_token_is_context_relationship_only() -> None:
    doc, cap = make_doc(DNS_TEXT, "https://dns.google/resolve?name=acme.example&type=TXT", title="DNS acme.example",
                        family=SourceFamily.DNS, kind="dns")
    items = rules.tag_document(doc, DNS_TEXT, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF)
    assert len(items) == 1
    it = items[0]
    assert it.excerpt == "openai-domain-verification=dv-abc123" and DNS_TEXT[it.start:it.end] == it.excerpt
    assert it.start == DNS_TEXT.index("TXT\topenai") + 4  # located on its TXT line
    assert it.tag_string == "U4 · SR:B · SP:S1 · RL:R2 · RC:T3 · IC:2 · locus=relationship"
    assert it.strength == "Context - relationship only" and it.tags.ai_type == "unspecified"
    assert (it.passage_id, it.providers, it.source_type, it.publisher) == ("", ["OpenAI"], "DNS TXT record",
                                                                           "Acme Payments")
    assert it.date_basis == "retrieval" and it.published == ""
    assert not rules.is_qualifying(it) and not rules.is_corroborating(it)


def test_tag_document_ignores_other_kinds_and_tokenless_zones() -> None:
    doc, cap = make_doc("Sentinel uses AI.", family=SourceFamily.PRD)
    assert rules.tag_document(doc, "Sentinel uses AI.", cap, SEEDS, PROFILE, PLAN, as_of=AS_OF) == []
    text = "DNS records for acme.example\nTXT\tv=spf1 ~all\n"
    doc, cap = make_doc(text, "https://dns.google/resolve?name=acme.example&type=TXT", family=SourceFamily.DNS,
                        kind="dns")
    log: list[str] = []
    assert rules.tag_document(doc, text, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF, log=log) == []
    assert "no AI verification token" in log[-1]


# =========================================================================== LLM claims, merge, reviews


CLAIM_TEXT = ("Instant payments\n\nSentinel uses a machine learning model to screen every inbound instant payment for "
              "fraud detection. Alerts are reviewed by an analyst before any payment is held.\n")


def claim(quote: str, **kw: Any) -> Claim:
    base: dict[str, Any] = dict(passage_id="p1", quote=quote, claim_kind="offers_ai_feature", subject="vendor_product",
                                ai_type="predictive_ml", temporal="in_production", action_level="unknown",
                                named_providers=[], data_mentioned=[], indicators=[])
    base.update(kw)
    return Claim(**base)


def verified(text: str, quote: str, **kw: Any) -> VerifyResult:
    start = text.index(quote)
    return VerifyResult(ok=True, start=start, end=start + len(quote), excerpt=quote, **kw)


def claim_item(quote: str, *, c: Claim | None = None, **result_kw: Any) -> EvidenceItem:
    doc, cap = make_doc(CLAIM_TEXT)
    p = whole_passage(doc, CLAIM_TEXT)
    return rules.claim_item(c or claim(quote), verified(CLAIM_TEXT, quote, **result_kw), p, doc, cap, SEEDS, PROFILE,
                            PLAN, as_of=AS_OF, doc_text=CLAIM_TEXT, llm_model="gemini-3.5-flash-lite",
                            prompt_sha256="a" * 64)


def rule_item(text: str = CLAIM_TEXT) -> EvidenceItem:
    doc, cap = make_doc(text)
    item = rules.tag_passage(whole_passage(doc, text), doc, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF, doc_text=text)
    assert item is not None
    return item


def test_claim_item_is_a_tagged_proposal() -> None:
    quote = "Alerts are reviewed by an analyst before any payment is held."
    item = claim_item(quote, c=claim(quote, action_level="human_reviewed_decision", ai_type="predictive_ml"),
                      indicators=[Indicator(code="G3", span="reviewed by an analyst")])
    assert item.method == "llm_proposed_accepted" and item.proposed and not item.citable
    assert item.excerpt == quote and CLAIM_TEXT[item.start:item.end] == quote
    assert item.llm_model == "gemini-3.5-flash-lite" and item.prompt_sha256 == "a" * 64
    assert item.llm_labels["action_level"] == "human_reviewed_decision" and "sp" in item.llm_labels
    assert Indicator(code="G3", span="reviewed by an analyst") in item.indicators
    assert item.rule_labels["rl"] == item.tags.rl  # RL and locus computed locally (V7)


def test_claim_item_drops_source_type_indicators_and_guarded_providers() -> None:
    quote = "Sentinel uses a machine learning model to screen every inbound instant payment for fraud detection."
    item = claim_item(quote, indicators=[Indicator(code="G7", span="machine learning")], providers=["Claude"])
    assert "G7" not in codes(item)  # G6/G7 come from the source type only
    assert "Claude" not in item.providers  # the Claude guard needs Anthropic or a model name


def test_claim_item_needs_a_verified_result() -> None:
    doc, cap = make_doc(CLAIM_TEXT)
    with pytest.raises(ValueError):
        rules.claim_item(claim("x" * 30), VerifyResult(ok=False, failures=["V2"]), whole_passage(doc, CLAIM_TEXT),
                         doc, cap, SEEDS, PROFILE, PLAN, as_of=AS_OF, doc_text=CLAIM_TEXT, llm_model="m",
                         prompt_sha256="a" * 64)


def test_claim_item_not_ai_is_a_trap() -> None:
    quote = "Alerts are reviewed by an analyst before any payment is held."
    item = claim_item(quote, c=claim(quote, claim_kind="not_ai"))
    assert item.tags.ai_type == "not_ai" and item.strength == "Marketing only" and "M4" in codes(item)


def test_merge_claims_folds_agreeing_claims_into_rule_items() -> None:
    rule = rule_item()
    quote = "Sentinel uses a machine learning model to screen every inbound instant payment for fraud detection."
    agree = claim_item(quote, c=claim(quote, temporal=rule.temporal, action_level=rule.action_level))
    agree = agree.model_copy(update={"llm_labels": {**agree.llm_labels, "sp": rule.tags.sp}})
    other_q = "Alerts are reviewed by an analyst before any payment is held."
    lone = claim_item(other_q)
    out = rules.merge_claims([rule], [agree, lone])
    assert [i.item_key for i in out] == sorted([rule.item_key, lone.item_key],
                                               key=lambda k: [i for i in (rule, lone) if i.item_key == k][0].start)
    merged = next(i for i in out if i.item_key == rule.item_key)
    assert merged.method == "rule+llm_agree" and merged.llm_model == "gemini-3.5-flash-lite"
    assert merged.llm_labels["temporal"] == rule.temporal and not merged.label_disagreements
    assert next(i for i in out if i.item_key == lone.item_key).proposed


def test_merge_claims_keeps_the_rule_value_on_disagreement() -> None:
    rule = rule_item()
    quote = "Sentinel uses a machine learning model to screen every inbound instant payment for fraud detection."
    planned = claim_item(quote, c=claim(quote, temporal="planned"))
    out = rules.merge_claims([rule], [planned])
    assert len(out) == 1 and out[0].method == "rule" and "temporal" in out[0].label_disagreements
    assert out[0].temporal == rule.temporal and out[0].tags == rule.tags


def review_store(tmp_path: Path, item: EvidenceItem, value: str, **extra: Any) -> OverrideStore:
    store = OverrideStore(tmp_path / "reviews.jsonl")
    store.add({"kind": "evidence_review", "vendor_id": VID, "key": item.item_key, "value": value,
               "reason": "checked against the captured page", "analyst": "RK", "date": "2026-10-03",
               "extra": extra})
    return store


def test_apply_reviews_rejects_and_relabels(tmp_path: Path) -> None:
    item = rule_item()
    other = S.item()
    out = rules.apply_reviews([item, other], review_store(tmp_path, item, "rejected"))
    assert [i.item_key for i in out] == [item.item_key, other.item_key]
    assert out[0].review_status == "rejected" and not out[0].citable and out[0].reviewer == "RK 2026-10-03"
    assert out[1] == other
    store = review_store(tmp_path / "b", item, "accepted", labels={"rl": "R2", "temporal": "pilot_or_beta"})
    relabelled = rules.apply_reviews([item], store)[0]
    assert relabelled.method == "adjudicated" and relabelled.tags.rl == "R2"
    assert relabelled.temporal == "pilot_or_beta" and relabelled.strength == "Moderate"


def test_apply_reviews_refuses_unknown_labels(tmp_path: Path) -> None:
    item = rule_item()
    with pytest.raises(ValueError):
        rules.apply_reviews([item], review_store(tmp_path, item, "accepted", labels={"sr": "A"}))
    with pytest.raises(ValueError):
        rules.apply_reviews([item], review_store(tmp_path / "b", item, "accepted", labels={"temporal": "soon"}))


def test_accepting_a_proposal_makes_it_citable(tmp_path: Path) -> None:
    quote = "Sentinel uses a machine learning model to screen every inbound instant payment for fraud detection."
    proposal = claim_item(quote)
    assert proposal.proposed and proposal.strength != "Strong"
    accepted = rules.apply_reviews([proposal], review_store(tmp_path, proposal, "accepted"))[0]
    assert accepted.citable and accepted.strength == "Strong"


def test_retag_recomputes_strength_and_keeps_cross_item_ic() -> None:
    item = S.item(tags=S.tags(strength="Weak", ic=3))
    assert rules.retag(item).strength == "Strong" and rules.retag(item).tags.ic == 2
    corroborated = S.item(tags=S.tags(ic=1))
    assert rules.retag(corroborated).tags.ic == 1
    trap = S.item(tags=S.tags(ai_type="not_ai", strength="Weak"))
    assert rules.retag(trap).strength == "Marketing only"


def test_public_signatures_match_the_contract() -> None:
    sig = inspect.signature(rules.tag_passage)
    assert list(sig.parameters)[:6] == ["passage", "document", "capture", "seeds", "profile", "plan"]
    assert {"as_of", "doc_text", "signals", "sources", "log"} <= set(sig.parameters)
    assert list(inspect.signature(rules.entity_ok).parameters) == ["document", "capture", "seeds", "profile", "text",
                                                                   "log"]
    assert list(inspect.signature(rules.strength_label).parameters) == ["u_class", "sp", "rl", "locus", "is_q",
                                                                        "is_k"]
    for name in ("load_signals", "load_sources", "classify_source", "find_indicators", "indicator_ok", "specificity",
                 "relevance", "recency", "is_qualifying", "is_corroborating", "tag_document", "claim_item",
                 "merge_claims", "apply_reviews", "retag"):
        assert name in rules.__all__


# =========================================================================== real passages (frozen evidence pack)


@lru_cache(maxsize=1)
def _real() -> dict[str, Any]:
    if not list(RUNS.glob("V-00*/manifest.json")):
        pytest.skip("no collection runs present")
    from footprint.criticality import score_profile
    from footprint.depth import plan_depth
    from footprint.workbook import read_workbook

    vendors = {v.vendor_id: v for v in read_workbook(REPO / "data" / "input" / "Meridian_Vendor_Input.xlsx").vendors}
    out: dict[str, Any] = {}
    for run in sorted(RUNS.glob("V-00*")):
        vid = run.name[:5]
        man = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        caps = {}
        for line in (run / "captures.jsonl").read_text(encoding="utf-8").splitlines():
            raw = json.loads(line)
            fields = {k: v for k, v in raw.items() if k in Capture.model_fields}
            caps[raw["capture_id"]] = Capture.model_validate(fields)
        passages = [Passage.model_validate_json(x) for x in (run / "passages.jsonl").read_text(encoding="utf-8")
                    .splitlines() if x.strip()]
        profile = vendors[vid]
        out[vid] = dict(profile=profile, plan=plan_depth(score_profile(profile), profile),
                        seeds=tomllib.loads((REPO / "seeds" / f"{vid}.toml").read_text(encoding="utf-8")),
                        docs={d["doc_id"]: Document.model_validate(d) for d in man["documents"]}, caps=caps,
                        passages=passages)
    return out


def _doc_text(doc: Document) -> str:
    path = REPO / doc.text_path
    if not path.exists():
        pytest.skip("evidence text not present")
    return path.read_bytes().decode("utf-8")


def _real_doc(vid: str, url_part: str) -> Document:
    docs = [d for d in _real()[vid]["docs"].values() if url_part in d.url]
    if not docs:
        pytest.skip(f"{vid}: no captured document for {url_part}")
    return sorted(docs, key=lambda d: d.doc_id)[0]


def _real_passage(vid: str, doc: Document, needle: str) -> Passage:
    """The collected passage holding ``needle``; if the collection run has none (a passage-finder gap), the window
    find_passages would build: the sentence with one neighbour on each side, at most 700 characters."""
    for p in _real()[vid]["passages"]:
        if p.doc_id == doc.doc_id and needle in p.text:
            return p
    from footprint.extract import split_sentences

    text = _doc_text(doc)
    pos = text.index(needle)
    sents = split_sentences(text)
    k = next(i for i, (s, e) in enumerate(sents) if s <= pos < e)
    ws, we = sents[k]
    if k > 0 and we - sents[k - 1][0] <= 700:
        ws = sents[k - 1][0]
    if k + 1 < len(sents) and sents[k + 1][1] - ws <= 700:
        we = sents[k + 1][1]
    if we - ws > 700:  # a run-on block (sentences glued without spaces): the glued sentence around the needle
        ws = max(ws, text.rfind(".", ws, pos) + 1)
        stop = text.find(".", pos + len(needle))
        we = we if stop < 0 else min(we, stop + 1)
    return Passage(passage_id=Passage.make_id(doc.doc_id, ws, we), doc_id=doc.doc_id, vendor_id=vid, start=ws,
                   end=we, text=text[ws:we])


def _tag_real(vid: str, doc: Document, passage: Passage, log: list[str] | None = None) -> EvidenceItem | None:
    v = _real()[vid]
    return rules.tag_passage(passage, doc, v["caps"][doc.capture_id], v["seeds"], v["profile"], v["plan"],
                             as_of=AS_OF, doc_text=_doc_text(doc), log=log)


def test_real_bny_rtp_page_is_strong_r3() -> None:
    doc = _real_doc("V-005", "payables/instant-payments.html")
    item = _tag_real("V-005", doc, _real_passage("V-005", doc, "AI-enabled anomaly detection"))
    assert item is not None
    check_item_invariants(item, _doc_text(doc), doc.title)
    assert "AI-enabled anomaly detection" in item.excerpt and "RTP" in item.excerpt
    assert (item.tags.u_class, item.tags.sr, item.tags.rl, item.tags.locus) == ("U1", "B", "R3", "service_feature")
    assert item.tags.sp in ("S2", "S3") and item.strength == "Strong"
    assert item.source_type == "Product page" and item.publisher == "BNY" and rules.is_qualifying(item)


def test_real_fssi_reader_advice_blog_is_marketing_only() -> None:
    doc = _real_doc("V-003", "growth-marketing-and-member-retention-strategies-for-credit-unions")
    items = []
    for p in [p for p in _real()["V-003"]["passages"] if p.doc_id == doc.doc_id]:
        item = _tag_real("V-003", doc, p)
        if item is not None:
            check_item_invariants(item, _doc_text(doc), doc.title)
            items.append(item)
    assert items, "the reader-advice blog produced no logged items"
    assert {i.strength for i in items} == {"Marketing only"}
    assert not any(rules.is_qualifying(i) or rules.is_corroborating(i) for i in items)
    advice = [i for i in items if i.tags.locus == "commentary"]
    assert advice and all(i.tags.rl == "R0" and i.tags.u_class == "U7" for i in advice)


def test_real_terrapin_dns_tokens_are_context_relationship_only() -> None:
    doc = _real_doc("V-004", "dns.google/resolve?name=terrapintech.com")
    v = _real()["V-004"]
    text = _doc_text(doc)
    items = rules.tag_document(doc, text, v["caps"][doc.capture_id], v["seeds"], v["profile"], v["plan"], as_of=AS_OF)
    assert {tuple(i.providers) for i in items} == {("OpenAI",), ("Anthropic",)}
    for it in items:
        assert text[it.start:it.end] == it.excerpt and "domain-verification" in it.excerpt
        assert (it.tags.u_class, it.tags.sp, it.tags.rl, it.tags.locus) == ("U4", "S1", "R2", "relationship")
        assert it.strength == "Context - relationship only" and it.publisher == "Terrapin Technologies"


def test_real_tch_noc_posting_is_u3_r2() -> None:
    doc = _real_doc("V-006", "Manager--NOC-Operations")
    item = _tag_real("V-006", doc, _real_passage("V-006", doc, "leverages technology and AI"))
    assert item is not None
    check_item_invariants(item, _doc_text(doc), doc.title)
    assert (item.tags.u_class, item.tags.rl, item.tags.sr) == ("U3", "R2", "B")
    assert item.source_type == "Job posting" and item.tags.locus == "delivery_ops" and item.tags.sp == "S2"
    assert "G8" in codes(item) and item.strength == "Moderate"


def test_real_tch_noc_posting_named_tools_line_is_u3() -> None:
    doc = _real_doc("V-006", "Manager--NOC-Operations")
    item = _tag_real("V-006", doc, _real_passage("V-006", doc, "Utilize AI-powered tools"))
    assert item is not None and "Microsoft Copilot" in item.providers
    assert (item.tags.u_class, item.tags.rl, item.tags.locus) == ("U3", "R2", "delivery_ops")


def test_real_claude_reumert_never_becomes_anthropic() -> None:
    hits = [(vid, p) for vid, v in _real().items() for p in v["passages"] if "Claude Reumert" in p.text]
    if not hits:
        pytest.skip("no Claude Reumert passage in the evidence pack")
    for vid, p in hits:
        doc = _real()[vid]["docs"][p.doc_id]
        item = _tag_real(vid, doc, p)
        if item is not None and "Anthropic" not in item.excerpt:
            assert not any(x.startswith("Claude") for x in item.providers), p.passage_id


def test_real_sample_respects_the_item_invariants() -> None:
    """Every 15th real passage of every vendor: exact slices, in-excerpt spans, verbatim providers, stable labels."""
    seen = 0
    for vid, v in _real().items():
        for p in v["passages"][::15]:
            doc = v["docs"][p.doc_id]
            log: list[str] = []
            item = _tag_real(vid, doc, p, log)
            if item is None:
                assert log and log[-1].startswith(f"rules {p.passage_id}:"), p.passage_id
                continue
            seen += 1
            check_item_invariants(item, _doc_text(doc), doc.title)
            assert item.vendor_id == vid and item.passage_id == p.passage_id
            assert p.start <= item.start and item.end <= p.end
    assert seen >= 20
