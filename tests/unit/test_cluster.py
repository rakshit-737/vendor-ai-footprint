"""cluster.py: duplicates (V9), origin clusters, independence and corroboration links (design Appendix A 2.6-2.7;
docs/contracts_p3.md section 6).

Offline. footprint.rules is hidden by default (sys.modules entry None), so the contract's fallback predicates are
pinned here; tests that need the rules hooks install a fake module. All text is synthetic (Acme, V-901).
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from footprint import cluster as C
from footprint.models import EvidenceItem, SourceFamily

_p = Path(__file__).resolve().parents[1] / "fixtures" / "p3_samples.py"
_spec = importlib.util.spec_from_file_location("p3_samples", _p)
S = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p3_samples", S)
_spec.loader.exec_module(S)

RELEASE = "Acme Payments today announced Sentinel, a machine learning service that screens instant payments."
MIRROR = ("Acme Payments today announced Sentinel, a machine-learning service that screens instant payments in "
          "real time.")
SUPPORT = "Our support desk uses a generative AI assistant to draft first replies to customer tickets."
BATCH = "Acme screens every outbound card payment with a rules engine before settlement each night."
FILLER = "Acme uses machine learning to screen instant payments in production. " * 10


@pytest.fixture(autouse=True)
def _rules_hidden(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    if not request.node.name.startswith("test_real_rules"):
        monkeypatch.setitem(sys.modules, "footprint.rules", None)


def fake_rules(monkeypatch: pytest.MonkeyPatch, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType("footprint.rules")
    for name, value in attrs.items():
        setattr(mod, name, value)
    monkeypatch.setitem(sys.modules, "footprint.rules", mod)
    return mod


def doc_for(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def it(excerpt: str, *, url: str = "https://www.acme.example/a", start: int = 0, doc_id: str | None = None,
       **kw: Any) -> EvidenceItem:
    """An evidence item; tag_<name>=... overrides one SignalTags field (default: a Strong U1 SR B item)."""
    tag_kw = {k[4:]: kw.pop(k) for k in list(kw) if k.startswith("tag_")}
    base: dict[str, Any] = dict(excerpt=excerpt, start=start, url=url, doc_id=doc_id or doc_for(url))
    if tag_kw:
        base["tags"] = S.tags(**tag_kw)
    base.update(kw)
    return S.item(**base)


def span(start: int, end: int, *, page: str = "a", doc: str = "1" * 64, **kw: Any) -> EvidenceItem:
    """An item over FILLER[start:end] in one shared document; ``page`` varies the URL (and so the item key)."""
    return it(FILLER[start:end], start=start, doc_id=doc, url=f"https://www.acme.example/{page}", **kw)


# --------------------------------------------------------------------------- dedupe (V9)


def test_dedupe_keeps_one_item_per_key_and_logs_v9():
    a, b = it(RELEASE), it(RELEASE)
    assert a.item_key == b.item_key
    log: list[str] = []
    assert C.dedupe([a, b], log=log) == [a]
    assert log == [f"V9:{a.item_key}"]
    assert C.dedupe([]) == []


def test_dedupe_overlapping_spans_keep_the_strongest_and_preserve_order():
    weak = span(0, 40, page="w", tag_strength="Weak")
    lone = span(100, 140, page="x")
    strong = span(20, 70, page="s", tag_strength="Strong")
    log: list[str] = []
    assert C.dedupe([weak, lone, strong], log=log) == [lone, strong]
    assert log == [f"V9:{weak.item_key}"]
    negative = span(30, 60, page="n", tag_strength="Negative", tag_u_class="U8")
    assert C.dedupe([strong, negative]) == [negative]  # STRENGTH_ORDER puts Negative first


@pytest.mark.parametrize(("better", "worse"), [
    ("rule+llm_agree", "adjudicated"), ("adjudicated", "rule"), ("rule", "llm_proposed_accepted"),
])
def test_dedupe_breaks_strength_ties_on_method(better: str, worse: str):
    good = span(0, 40, page="g", method=better)
    bad = span(10, 50, page="b", method=worse)
    assert C.dedupe([bad, good]) == [good]


def test_dedupe_then_prefers_the_longer_excerpt_then_the_smaller_key():
    short, long_ = span(0, 40, page="s"), span(5, 60, page="l")
    assert C.dedupe([short, long_]) == [long_]
    one, two = span(0, 40, page="p"), span(0, 40, page="q")
    best = min(one, two, key=lambda i: i.item_key)
    assert C.dedupe([one, two]) == [best] and C.dedupe([two, one]) == [best]


def test_dedupe_greedy_over_a_chain_of_overlaps():
    a = span(0, 30, page="a", tag_strength="Weak")
    b = span(20, 50, page="b", tag_strength="Strong")
    c = span(40, 70, page="c", tag_strength="Weak")
    assert C.dedupe([a, b, c]) == [b]
    a2 = span(0, 30, page="a", tag_strength="Strong")
    b2 = span(20, 50, page="b", tag_strength="Moderate")
    assert C.dedupe([a2, b2, c]) == [a2, c]


def test_dedupe_keeps_citable_evidence_over_traps_rejections_and_proposals():
    weak = span(0, 40, page="w", tag_strength="Weak")
    trap = span(10, 50, page="t", tag_strength="Marketing only", tag_ai_type="not_ai", tag_u_class="U7")
    rejected = span(20, 60, page="r", tag_strength="Strong", review_status="rejected")
    proposal = span(5, 45, page="p", tag_strength="Strong", method="llm_proposed_accepted")
    log: list[str] = []
    assert C.dedupe([trap, rejected, proposal, weak], log=log) == [weak]
    assert log == [f"V9:{x.item_key}" for x in (trap, rejected, proposal)]
    accepted = proposal.model_copy(update={"review_status": "accepted"})
    assert C.dedupe([weak, accepted]) == [accepted]  # an accepted proposal is citable: strength decides again
    assert C.dedupe([trap, rejected]) == [rejected]  # neither is citable: strength decides


def test_dedupe_leaves_adjacent_spans_other_documents_and_other_vendors_alone():
    left, right = span(0, 30, page="l"), span(30, 60, page="r")
    elsewhere = span(10, 40, page="e", doc="2" * 64)
    other_vendor = span(0, 30, page="v", vendor_id="V-902")
    items = [left, right, elsewhere, other_vendor]
    assert C.dedupe(items) == items


# --------------------------------------------------------------------------- same_origin


def test_same_origin_by_excerpt_similarity_covers_partner_mirrors():
    vendor = it(RELEASE, url="https://www.acme.example/news/sentinel", publisher="Acme Payments")
    mirror = it(MIRROR, url="https://wire.example/acme-sentinel", publisher="Example Wire",
                family=SourceFamily.IND, title="Acme Payments launches Sentinel | Example Wire")
    assert C.same_origin(vendor, mirror) and C.same_origin(mirror, vendor)
    assert not C.same_origin(vendor, it(SUPPORT, url="https://www.acme.example/support"))
    assert not C.same_origin(vendor, it(BATCH, url="https://www.acme.example/batch"))


def test_same_origin_by_title_needs_four_words():
    a = it(SUPPORT, url="https://a.example/x", title="Acme launches \u201cSentinel\u201d screening")
    b = it(BATCH, url="https://b.example/y", title="  acme launches \"sentinel\"   screening.")
    assert C.same_origin(a, b)
    short_a = it(SUPPORT, url="https://a.example/x", title="Acme Sentinel launch")
    short_b = it(BATCH, url="https://b.example/y", title="Acme Sentinel launch")
    assert not C.same_origin(short_a, short_b)
    assert not C.same_origin(it(SUPPORT, url="https://a.example/x", title=""),
                             it(BATCH, url="https://b.example/y", title=""))


def test_same_origin_title_counts_words_not_separators():
    a = it(SUPPORT, url="https://a.example/x", title="Analyst Program | Acme")
    b = it(BATCH, url="https://b.example/y", title="Analyst Program | Acme")
    assert not C.same_origin(a, b)  # three words; the "|" is not one
    assert C.title_keys("Analyst Program | Acme") == frozenset()
    assert C.title_keys("") == frozenset() and C.title_keys(" | - ") == frozenset()


def test_same_origin_title_parts_match_a_headline_under_another_site_suffix():
    headline = "Acme Launches Sentinel: Machine Learning for Instant Payments"
    vendor = it(SUPPORT, url="https://www.acme.example/news/sentinel", title=f"{headline} – Acme Newsroom")
    wire = it(BATCH, url="https://wire.example/acme", publisher="Example Wire", family=SourceFamily.IND,
              title=f"{headline.upper()} | Example Wire")
    blog = it(FILLER[:80], url="https://blog.example/acme", publisher="Payments Weekly", family=SourceFamily.IND,
              title=f"Payments Weekly — {headline}")
    assert C.same_origin(vendor, wire) and C.same_origin(wire, blog) and C.same_origin(vendor, blog)
    assert "acme launches sentinel machine learning for instant payments" in C.title_keys(vendor.title)
    # a shared site suffix of fewer than four words does not make two pages one origin
    other = it(BATCH, url="https://www.acme.example/news/other", title="Acme Opens Dublin Office – Acme Newsroom")
    assert C.title_keys(other.title).isdisjoint(C.title_keys(vendor.title))
    assert not C.same_origin(vendor, other)
    # a colon is part of a headline, not a separator
    assert "machine learning for instant payments" not in C.title_keys(headline)
    for sep in (" | ", " - ", " -- ", " – ", " — ", " · ", " • ", " :: "):
        assert "acme opens dublin office" in C.title_keys(f"Acme Opens Dublin Office{sep}Acme News"), sep
    assert "acme opens dublin office" not in C.title_keys("Acme Opens Dublin Office-Acme News")  # no spaces


def test_same_origin_same_document_or_same_page():
    doc = "9" * 64
    assert C.same_origin(it(SUPPORT, url="https://a.example/x", doc_id=doc),
                         it(BATCH, url="https://b.example/y", doc_id=doc))
    assert C.same_origin(it(SUPPORT, url="https://www.acme.example/news/sentinel/"),
                         it(BATCH, url="http://acme.example/news/sentinel#top"))
    assert not C.same_origin(it(SUPPORT, url="https://acme.example/news/sentinel"),
                             it(BATCH, url="https://acme.example/news/sentinel-2"))


def test_items_of_different_vendors_are_never_the_same_origin():
    assert not C.same_origin(it(RELEASE), it(RELEASE, vendor_id="V-902"))


# --------------------------------------------------------------------------- assign_clusters


def _cluster_set() -> dict[str, EvidenceItem]:
    return {
        "vendor": it(RELEASE, url="https://www.acme.example/news/sentinel", title="Acme Payments newsroom"),
        "mirror": it(MIRROR, url="https://wire.example/acme-sentinel", publisher="Example Wire",
                     family=SourceFamily.IND, title="Acme Payments launches Sentinel screening service"),
        "blog": it(BATCH, url="https://blog.example/acme", publisher="Payments Weekly", family=SourceFamily.IND,
                   title="ACME PAYMENTS LAUNCHES SENTINEL SCREENING SERVICE"),
        "support": it(SUPPORT, url="https://www.acme.example/support", title="Support"),
        "other_vendor": it(RELEASE, url="https://www.acme.example/news/sentinel", vendor_id="V-902"),
    }


def test_assign_clusters_is_transitive_and_names_clusters_by_their_smallest_key():
    items = _cluster_set()
    out = {k: v for k, v in zip(items, C.assign_clusters(list(items.values())), strict=True)}
    release = min(items[k].item_key for k in ("vendor", "mirror", "blog"))[:12]
    assert out["vendor"].cluster_id == out["mirror"].cluster_id == out["blog"].cluster_id == release
    assert out["support"].cluster_id == items["support"].item_key[:12]
    assert out["other_vendor"].cluster_id == items["other_vendor"].item_key[:12]
    assert not C.same_origin(items["vendor"], items["blog"])  # linked only through the mirror


def test_assign_clusters_preserves_order_and_ignores_input_order():
    items = list(_cluster_set().values())
    forward = C.assign_clusters(items)
    backward = C.assign_clusters(items[::-1])
    assert [i.item_key for i in forward] == [i.item_key for i in items]
    assert {i.item_key + i.vendor_id: i.cluster_id for i in forward} == {
        i.item_key + i.vendor_id: i.cluster_id for i in backward}
    for before, after in zip(items, forward, strict=True):
        assert after.model_copy(update={"cluster_id": ""}) == before


# --------------------------------------------------------------------------- independent


def test_independent_needs_another_cluster_and_another_publisher_or_family():
    items = _cluster_set()
    out = dict(zip(items, C.assign_clusters(list(items.values())), strict=True))
    third = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly", family=SourceFamily.IND)
    third = C.assign_clusters([out["vendor"], third])[1]
    assert C.independent(out["vendor"], third) and C.independent(third, out["vendor"])
    assert not C.independent(out["vendor"], out["mirror"])  # one release, one cluster
    assert not C.independent(out["vendor"], out["other_vendor"])
    assert not C.independent(out["vendor"], out["vendor"])


def test_independent_publisher_comparison_ignores_case_and_family_counts():
    a = it(SUPPORT, url="https://www.acme.example/support", publisher="Acme Payments", cluster_id="aaa")
    b = it(BATCH, url="https://www.acme.example/batch", publisher="acme  payments", cluster_id="bbb")
    assert not C.independent(a, b)
    job = it(BATCH, url="https://jobs.acme.example/123", publisher="Acme Payments", family=SourceFamily.JOB,
             cluster_id="ccc")
    assert C.independent(a, job)


def test_independent_without_cluster_ids_falls_back_to_same_origin():
    vendor = it(RELEASE, url="https://www.acme.example/news/sentinel")
    mirror = it(MIRROR, url="https://wire.example/acme-sentinel", publisher="Example Wire")
    press = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly")
    assert not C.independent(vendor, mirror)
    assert C.independent(vendor, press)


# --------------------------------------------------------------------------- DNS records, vendor-origin copies


DNS_URL = "https://dns.google/resolve?name=acme.example&type=TXT"
DNS_DOC = "d" * 64


def dns(token: str, providers: list[str], *, url: str = DNS_URL, doc_id: str = DNS_DOC) -> EvidenceItem:
    return it(token, url=url, doc_id=doc_id, family=SourceFamily.DNS, source_type="DNS TXT record",
              title="DNS acme.example", providers=providers, tag_u_class="U4", tag_locus="relationship",
              tag_sp="S1", tag_rl="R2", tag_strength="Context - relationship only")


def test_dns_records_of_one_answer_are_separate_origins_per_provider():
    """Every TXT record of one DoH answer shares the document, the URL and the title "DNS <domain>"; each token is
    still its own relationship, so column P can cite the Anthropic and the OpenAI token (review finding V-004)."""
    anthropic = dns("anthropic-domain-verification-abc123=Xy7Zq9Lm2Np4Rs6Tu8Vw0", ["Anthropic"])
    openai = dns("openai-domain-verification=dv-Qw3Er5Ty7Ui9Op1As3Df5Gh7", ["OpenAI"])
    assert not C.same_origin(anthropic, openai)
    out = C.assign_clusters([anthropic, openai])
    assert out[0].cluster_id != out[1].cluster_id
    # the same provider's record read through a second resolver is one origin
    again = dns("anthropic-domain-verification-abc123=Xy7Zq9Lm2Np4Rs6Tu8Vw0", ["Anthropic"],
                url="https://cloudflare-dns.com/dns-query?name=acme.example&type=TXT", doc_id="e" * 64)
    assert C.same_origin(anthropic, again)
    # a record with no provider is its own origin by its text
    spf = dns("v=spf1 include:spf.protection.outlook.com -all", [])
    assert not C.same_origin(spf, openai) and C.same_origin(spf, dns("v=spf1 include:spf.protection.outlook.com -all",
                                                                      [], doc_id="f" * 64))
    # a DNS record never shares an origin with a page item, even in the same document
    assert not C.same_origin(anthropic, it(SUPPORT, url=DNS_URL, doc_id=DNS_DOC, title="DNS acme.example"))


def _vendor_origin_set() -> dict[str, EvidenceItem]:
    items = {
        "filing": it(SUPPORT, url="https://www.sec.gov/Archives/edgar/data/123456/x/d1defa14a.htm",
                     family=SourceFamily.REG, source_type="SEC Form DEFA14A", publisher="Acme Payments"),
        "page": it(BATCH, url="https://www.acme.example/solutions/batch", publisher="Acme Payments"),
        "mirror": it("Across Acme, AI already powers fraud detection and risk management for every client.",
                     url="https://wire.example/acme-microsoft", family=SourceFamily.IND,
                     source_type="Press release mirror", publisher="Example Wire",
                     title="Acme Collaborates With Microsoft To Accelerate AI"),
        "joint": it("Acme Payments and Google Cloud deploy Gemini models to screen instant payments.",
                    url="https://www.googlecloudpresscorner.com/acme-google", family=SourceFamily.IND,
                    source_type="Partner press release", publisher="Google Cloud",
                    title="Acme Payments Selects Google Cloud For Screening"),
        "press": it("Acme Payments now routes support tickets with a machine learning model, the bank said.",
                    url="https://trade.example/acme-tickets", family=SourceFamily.IND, source_type="Trade press",
                    publisher="Payments Weekly", title="Acme Payments routes tickets with ML"),
    }
    return dict(zip(items, C.assign_clusters(list(items.values())), strict=True))


def test_a_vendor_origin_copy_is_never_independent_of_the_vendor():
    """A press-release mirror or a partner's press-room copy carries the vendor's own words: it is not independent
    corroboration of the vendor's filing or pages (review finding T7: a Mondo Visione mirror of Fiserv's release was
    counted as an independent source), nor of another vendor release."""
    s = _vendor_origin_set()
    assert len({i.cluster_id for i in s.values()}) == len(s)  # five origin clusters
    assert C.vendor_copy(s["mirror"]) and C.vendor_copy(s["joint"]) and not C.vendor_copy(s["press"])
    for copy in ("mirror", "joint"):
        for own in ("filing", "page"):
            assert not C.independent(s[copy], s[own]) and not C.independent(s[own], s[copy]), (copy, own)
    assert not C.independent(s["mirror"], s["joint"])
    # a third-party source of another publisher stays independent of the copy, and of the vendor
    assert C.independent(s["mirror"], s["press"]) and C.independent(s["press"], s["joint"])
    assert C.independent(s["press"], s["filing"])
    # the vendor's own channels of different families stay independent of each other (design 2.7)
    assert C.independent(s["filing"], s["page"])


def test_vendor_origin_types_come_from_rules_when_present(monkeypatch: pytest.MonkeyPatch):
    s = _vendor_origin_set()
    fake_rules(monkeypatch, vendor_origin_types=lambda: frozenset({"Trade press"}))
    assert C.vendor_copy(s["press"]) and not C.vendor_copy(s["mirror"])
    assert not C.independent(s["press"], s["filing"])


def test_distinct_sources_counts_one_item_per_independent_source():
    """Two articles of one outlet are one source (review finding T10: two The Asian Banker articles were counted as
    two independent sources); a vendor release beside the vendor's filing is one source too."""
    s = _vendor_origin_set()
    second = it("Acme Payments also uses machine learning to triage support tickets, its operations head said.",
                url="https://trade.example/acme-ops", family=SourceFamily.IND, source_type="Trade press",
                publisher="payments weekly", title="Inside Acme Payments operations")
    other = it("Acme Payments says machine learning now drafts replies to customer tickets.",
               url="https://news.example/acme", family=SourceFamily.IND, source_type="Trade press",
               publisher="Banking Daily", title="Acme Payments drafts replies with ML")
    second, other = C.assign_clusters([s["press"], second, other])[1:]
    assert C.distinct_sources([s["press"], second, other]) == [s["press"], other]
    assert C.distinct_sources([s["press"], second, other], prefer=[second]) == [second, other]
    assert C.distinct_sources([s["filing"], s["mirror"]]) == [s["filing"]]
    assert C.distinct_sources([s["filing"], s["page"]]) == [s["filing"], s["page"]]
    assert C.distinct_sources([]) == []
    assert C.distinct_sources([s["press"], s["press"]]) == [s["press"]]


def test_real_rules_vendor_origin_types_mark_mirrors_and_partner_press_rooms():
    rules = pytest.importorskip("footprint.rules")
    names = rules.vendor_origin_types()
    assert {"Press release mirror", "Partner press release"} <= names
    assert not names & {"Trade press", "Provider customer story", "Press release", "SEC Form 8-K exhibit"}


# --------------------------------------------------------------------------- link_corroboration


def _linked(items: list[EvidenceItem], **kw: Any) -> list[EvidenceItem]:
    return C.link_corroboration(C.assign_clusters(items), **kw)


def test_link_corroboration_links_independent_k_items_and_sets_ic_1():
    q = it(RELEASE, url="https://www.acme.example/news/sentinel")
    echo = it(MIRROR, url="https://wire.example/acme-sentinel", publisher="Example Wire")
    press = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly", family=SourceFamily.IND)
    bystander = it(BATCH, url="https://www.acme.example/batch", tag_ic=3)
    ks = {q.item_key, echo.item_key, press.item_key}
    out = _linked([q, echo, press, bystander], is_q=lambda i: i.item_key == q.item_key,
                  is_k=lambda i: i.item_key in ks)
    by = {i.item_key: i for i in out}
    assert by[q.item_key].corroborates == [press.item_key]
    assert by[echo.item_key].corroborates == [press.item_key]
    assert by[press.item_key].corroborates == sorted([q.item_key, echo.item_key])
    assert by[bystander.item_key].corroborates == []
    assert [by[k].tags.ic for k in (q.item_key, echo.item_key, press.item_key)] == [1, 1, 1]
    assert by[bystander.item_key].tags.ic == 3
    assert [i.item_key for i in out] == [q.item_key, echo.item_key, press.item_key, bystander.item_key]


def test_link_corroboration_only_k_candidates_corroborate():
    q = it(RELEASE, url="https://www.acme.example/news/sentinel")
    press = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly")
    out = _linked([q, press], is_q=lambda i: i.item_key == q.item_key, is_k=lambda i: False)
    assert [i.corroborates for i in out] == [[], []]
    assert [i.tags.ic for i in out] == [2, 2]


def test_link_corroboration_marks_contradicted_items_ic_5():
    q = it(RELEASE, url="https://www.acme.example/news/sentinel", published="2026-08-01")

    def u8(**kw: Any) -> EvidenceItem:
        base: dict[str, Any] = dict(url="https://www.acme.example/legal/ai", published="2026-09-01",
                                    tag_u_class="U8", tag_sr="A", tag_rl="R3", tag_strength="Negative")
        base.update(kw)
        return it("Sentinel does not use artificial intelligence to make any payment decision.", **base)

    none = {"is_q": lambda i: False, "is_k": lambda i: False}
    assert C.link_corroboration([q, u8()], **none)[0].tags.ic == 5
    assert C.link_corroboration([q, u8(published="2026-08-01")], **none)[0].tags.ic == 5  # same day
    assert C.link_corroboration([q, u8(published="", date_basis="retrieval")], **none)[0].tags.ic == 5
    for kw in ({"published": "2026-07-01"}, {"tag_rl": "R2"}, {"tag_sr": "C"}, {"review_status": "rejected"},
               {"vendor_id": "V-902"}):
        assert C.link_corroboration([q, u8(**kw)], **none)[0].tags.ic == 2, kw
    u5 = it(SUPPORT, url="https://www.acme.example/careers", tag_u_class="U5", tag_ic=3)
    assert C.link_corroboration([u5, u8()], **none)[0].tags.ic == 3
    assert C.link_corroboration([q, u8()], **none)[1].tags.ic == 2  # the U8 itself is untouched


def test_contradiction_outranks_corroboration():
    q = it(RELEASE, url="https://www.acme.example/news/sentinel", published="2026-08-01")
    press = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly")
    neg = it("Sentinel does not use artificial intelligence to make any payment decision.",
             url="https://www.acme.example/legal/ai", published="2026-09-01", tag_u_class="U8", tag_sr="A",
             tag_rl="R3", tag_strength="Negative")
    out = _linked([q, press, neg], is_q=lambda i: i.item_key == q.item_key,
                  is_k=lambda i: i.item_key in {q.item_key, press.item_key})
    assert out[0].corroborates == [press.item_key] and out[0].tags.ic == 5
    assert out[1].tags.ic == 1  # press is dated by retrieval, after the U8, so it is not contradicted


@pytest.mark.parametrize(("change", "counts"), [
    ({}, True),
    ({"tag_sr": "C", "tag_locus": "corporate_internal"}, True),   # K allows SR C and any locus
    ({"tag_sr": "D"}, False),
    ({"tag_u_class": "U5"}, False),
    ({"tag_sp": "S1"}, False),
    ({"tag_rl": "R1"}, False),
    ({"tag_rc": "T0"}, False),
    ({"review_status": "rejected"}, False),
    ({"tag_ai_type": "not_ai"}, False),
    ({"method": "llm_proposed_accepted"}, False),
    ({"method": "llm_proposed_accepted", "review_status": "accepted"}, True),
])
def test_fallback_predicates_follow_the_contract_when_rules_is_missing(change: dict[str, Any], counts: bool):
    anchor = it(RELEASE, url="https://www.acme.example/news/sentinel")
    other = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly", **change)
    out = _linked([anchor, other])
    assert out[0].corroborates == ([other.item_key] if counts else [])


def test_fallback_k_only_items_corroborate_each_other():
    anchor = it(RELEASE, url="https://www.acme.example/news/sentinel", tag_sr="C", tag_locus="corporate_internal")
    k_only = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly", tag_sr="C")
    # neither is Q (SR C), both are K: each corroborates the other
    out = _linked([anchor, k_only])
    assert out[0].corroborates == [k_only.item_key] and out[1].corroborates == [anchor.item_key]


def test_default_predicates_come_from_rules_when_present(monkeypatch: pytest.MonkeyPatch):
    # the fake rules call SR D items K and SR E items Q; the contract fallback would count neither
    fake_rules(monkeypatch, is_qualifying=lambda i: i.tags.sr == "E", is_corroborating=lambda i: i.tags.sr == "D")
    d1 = it(RELEASE, url="https://www.acme.example/news/sentinel", tag_sr="D")
    d2 = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly", tag_sr="D")
    e = it(BATCH, url="https://fintech.example/acme", publisher="Fintech Daily", tag_sr="E")
    out = _linked([d1, d2, e])
    assert out[0].corroborates == sorted([d2.item_key, e.item_key])
    assert out[2].corroborates == []  # Q only: corroborated, never corroborating
    assert out[2].tags.ic == 1


def test_stale_ic_and_links_are_recomputed(monkeypatch: pytest.MonkeyPatch):
    stale = it(RELEASE, url="https://www.acme.example/news/sentinel", tag_ic=1, corroborates=["0" * 64])
    once = C.link_corroboration([stale])
    assert once[0].tags.ic == 2 and once[0].corroborates == []
    for change, ic in (({"tag_sr": "C"}, 4), ({"tag_sp": "S1"}, 3), ({"tag_rc": "T0"}, 6), ({"tag_sr": "E"}, 6)):
        item = it(RELEASE, url="https://www.acme.example/news/sentinel", tag_ic=5, **change)
        assert C.link_corroboration([item])[0].tags.ic == ic, change
    fake_rules(monkeypatch, is_qualifying=lambda i: False, is_corroborating=lambda i: False,
               retag=lambda i: i.model_copy(update={"tags": i.tags.model_copy(update={"ic": 4})}))
    assert C.link_corroboration([stale])[0].tags.ic == 4


def test_stale_ic_of_a_dns_token_returns_to_2_without_rules():
    token = it("google-site-verification=openai-domain-verification=dv-abc123", url="https://dns.google/resolve",
               family=SourceFamily.DNS, tag_u_class="U4", tag_sp="S1", tag_rl="R2", tag_locus="relationship",
               tag_strength="Context - relationship only", tag_ic=1)
    assert C.link_corroboration([token])[0].tags.ic == 2  # not 3: a DNS token is a direct record


def test_stale_ic_is_recomputed_by_the_retag_hook_with_a_neutral_ic(monkeypatch: pytest.MonkeyPatch):
    seen: list[int] = []

    def retag(item: EvidenceItem) -> EvidenceItem:  # like rules.retag: keeps an IC of 1 or 5 it is given
        seen.append(item.tags.ic)
        ic = item.tags.ic if item.tags.ic in (1, 5) else 4
        return item.model_copy(update={"tags": item.tags.model_copy(update={"ic": ic})})

    fake_rules(monkeypatch, is_qualifying=lambda i: False, is_corroborating=lambda i: False, retag=retag)
    stale = it(RELEASE, url="https://www.acme.example/news/sentinel", tag_ic=5)
    assert C.link_corroboration([stale])[0].tags.ic == 4
    assert seen == [2]


def test_link_corroboration_is_idempotent():
    items = [it(RELEASE, url="https://www.acme.example/news/sentinel"),
             it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly"),
             it(BATCH, url="https://www.acme.example/batch", tag_sp="S1", tag_ic=3)]
    once = _linked(items)
    assert C.link_corroboration(once) == once
    assert [i.tags.ic for i in once] == [1, 1, 3]


# --------------------------------------------------------------------------- the real rules module (when present)


def test_real_rules_module_offers_the_predicates_cluster_calls():
    rules = pytest.importorskip("footprint.rules")
    for name in ("is_qualifying", "is_corroborating", "retag"):
        fn = getattr(rules, name, None)
        assert callable(fn), name
        inspect.signature(fn).bind(S.item())


def test_real_rules_link_and_unlink():
    pytest.importorskip("footprint.rules")
    q = it(RELEASE, url="https://www.acme.example/news/sentinel")
    mirror = it(MIRROR, url="https://wire.example/acme-sentinel", publisher="Example Wire", family=SourceFamily.IND)
    press = it(SUPPORT, url="https://trade.example/acme", publisher="Payments Weekly", family=SourceFamily.IND,
               tag_sr="C", tag_ic=4)
    out = _linked([q, mirror, press])
    assert [i.corroborates for i in out] == [[press.item_key], [press.item_key], sorted([q.item_key, mirror.item_key])]
    assert [i.tags.ic for i in out] == [1, 1, 1]
    # the trade press item is rejected in review: the links go and each IC returns to the rules' value
    rejected = [out[0], out[1], out[2].model_copy(update={"review_status": "rejected"})]
    again = C.link_corroboration(rejected)
    assert [i.corroborates for i in again] == [[], [], []]
    assert [i.tags.ic for i in again] == [2, 2, 4]
