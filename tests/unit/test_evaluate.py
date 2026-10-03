"""Offline tests for footprint.evaluate: gold URL recall, passage recall, strength agreement, traps, markdown.

The synthetic world is a fictional vendor on acme.example, written to tmp_path through the real evidence store and
pipeline.write_run. A few tests read the real gold file and config (read-only) to pin the gold set's shape.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from footprint.capture.store import EvidenceStore
from footprint.evaluate import (
    DEFAULT_GOLD,
    EXPECT_STRENGTHS,
    NONE_LABEL,
    TRAP_LABEL,
    check_gold,
    gold_report,
    load_findings,
    load_gold,
    load_runs,
    normalize_text,
    normalize_url,
    render_markdown,
    segments,
    write_report,
)
from footprint.models import (
    CONTEXT_STRENGTHS,
    AssessmentResult,
    Capture,
    CoverageEntry,
    CoverageStatus,
    Document,
    EvidenceItem,
    Passage,
    SignalTags,
    SourceFamily,
)
from footprint.net.tou import TouRegister
from footprint.pipeline import CollectionRun, write_run

_p = Path(__file__).resolve().parents[1] / "fixtures" / "p3_samples.py"
_spec = importlib.util.spec_from_file_location("p3_samples", _p)
samples = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p3_samples", samples)
_spec.loader.exec_module(samples)

REPO = Path(__file__).resolve().parents[2]
V = "V-001"
AI_URL = "https://www.acme.example/ai/"
OLD_URL = "https://acme.example/old"
NEW_URL = "https://www.acme.example/new"
DNS_URL = "https://dns.google/resolve?name=acme.example&type=TXT"
MIRROR_URL = "https://news.example.org/acme-ml"
SEC_URL = "https://www.sec.gov/Archives/acme-10k.htm"
TRAP_URL = "https://www.acme.example/infrastructure"
ANVILS_URL = "https://www.anvils.example/news/forge"
LEAD_URL = "https://www.acme.example/lead-page"

AI_TEXT = ("Overview\n\nAcme Payments uses a machine learning model to screen every inbound payment. Alerts are "
           "reviewed by an analyst before any payment is held.\n\nContact us for a demo of the platform today.\n")
AI_SENTENCE = "Acme Payments uses a machine learning model to screen every inbound payment."
ROUTER_TEXT = ("Products\n\nAcme’s “Smart Router” uses AI-driven routing — introduced in 2025 "
               "— for card payments across all regions.\n")
ROUTER_SENTENCE = ("Acme’s “Smart Router” uses AI-driven routing — introduced in 2025 — for "
                   "card payments across all regions.")
MIRROR_TEXT = ("Industry news\n\nAcme Payments said its fraud team now relies on a neural network to rank alerts "
               "for review. The company did not name a provider.\n")
TRAP_TEXT = "Infrastructure\n\nAcme utilizes high-speed intelligent inserting equipment with automated collating.\n"
DNS_TEXT = ("DNS records for acme.example (DoH: dns.google, cloudflare-dns.com)\n"
            "TXT\topenai-domain-verification=dv-abc123XYZ\nTXT\tv=spf1 include:mail.acme.example ~all\n"
            "AI_TOKEN\tOpenAI\topenai-domain-verification=dv-abc123XYZ\n")

LEXICON = r"""version = "test"
[core]
terms = ["machine learning", "neural", "artificial intelligence"]
case_sensitive = ['AI', 'LLMs?']
[guards]
[suppressors]
phrases = ["intelligent inserting", "llms.txt"]
conditional = ["intelligent tools"]
[rule_suppressors]
phrases = ["meant for consumption by LLMs"]
regex = ['\b23ai\b']
skip_url = ['/llms(?:-full)?\.txt$']
[definition_test]
relabel_regex = ['\b(?:intelligent|smart)\s+(?:automation|scheduling)\b', '\bAI[- ]powered\s+scheduling\b']
ml_phrases = ["machine learning", "AI model"]
ml_case_sensitive = ["ML"]
model_not_ml = ["operating", "business"]
"""

SEEDS = """vendor_id = "V-001"
name = "Acme Payments Ltd (Acme)"
legal_names = ["Acme Payments Ltd"]
aliases = ["Acme Payments", "Acme"]
domains = ["acme.example"]
collisions = ["Acme Anvils", "Claude Reumert", "Acme Holdings (shell company, CIK 7654321)", "Acme (the vendor)"]
[[seed]]
url = "https://robots.example/blocked"
family = "IND"
automation = "auto"
[[seed]]
url = "https://paywall.example/story"
family = "IND"
automation = "manual"
[[seed]]
url = "https://news.example.org/acme-ml"
family = "IND"
automation = "auto"
"""


def g(url: str, expect: str = "weak", excerpt: str = "", family: str = "PRD", vendor: str = V) -> dict:
    """A synthetic gold row in the real file's shape."""
    return {"vendor_id": vendor, "url": url, "family": family, "expect": expect, "relevance": "adjacent",
            "source_family": "synthetic", "published": "", "excerpt_reported": excerpt,
            "shows": "Synthetic analyst note that must never appear in a report."}


GOLD = [
    g("https://www.acme.example/ai", "strong",  # 1: fuzzy match (hyphen), URL without the trailing slash
      "Acme Payments uses a machine-learning model to screen every inbound payment."),
    g(OLD_URL, "moderate",  # 2: requested URL of a redirect; straight quotes and hyphens
      'Acme\'s "Smart Router" uses AI-driven routing - introduced in 2025 - for card payments across all regions.'),
    g("https://www.acme.example/blocked", "weak", "Blocked statement about generative onboarding pipelines."),  # 3
    g("https://robots.example/blocked", "weak", "Robots statement about agent orchestration layers.", "IND"),  # 4
    g("https://paywall.example/story", "weak", "Paywalled statement about predictive scoring engines.", "IND"),  # 5
    g(LEAD_URL, "weak", "Lead statement about conversational assistants for clients."),  # 6
    g("https://www.acme.example/never", "moderate", "Never fetched statement about document understanding."),  # 7
    g("https://www.acme.example/legal/privacy", "weak", "Privacy statement about automated decision making.",
      "LEG"),  # 8
    g(DNS_URL, "weak", "openai-domain-verification=dv-abc123XYZ", "DNS"),  # 9
    g("https://patents.example.com/p/1", "weak", "A patent about anomaly detection models.", "SKIP"),  # 10
    g("https://www.fiserv.com/en/x.html", "moderate", "Manual-only statement about agent platforms."),  # 11
    g("https://www.other.example/acme-story", "moderate",  # 12: found in another captured document
      "Acme Payments said its fraud team now relies on a neural network to rank alerts for review.", "IND"),
    g(AI_URL, "strong",  # 13: elided excerpt, both pieces present
      "Acme Payments uses a machine learning model [...] Alerts are reviewed by an analyst before any payment "
      "is held."),
    g(AI_URL, "marketing-only",  # 14: one piece missing
      "Acme Payments uses a machine learning model … and the model also writes every customer letter itself."),
    g(NEW_URL, "weak",  # 15: summarised by the scout, so not found
      "The router product of Acme relies on artificial intelligence for card transaction routing worldwide."),
    g(SEC_URL, "strong", "We use artificial intelligence in our payment processing services.", "REG"),  # 16
    g(TRAP_URL, "weak", "Acme utilizes high-speed intelligent inserting equipment with automated collating."),  # 17
    g("https://www.acme.example/llms.txt", "weak", "This is an llms.txt file, meant for consumption by LLMs."),  # 18
    g(ANVILS_URL, "weak", "Acme Anvils announced a new forge for heavy industry.", "IND"),  # 19
]


# --------------------------------------------------------------------------- world builders


def _capture(store: EvidenceStore, url: str, *, status: int = 200, family: SourceFamily = SourceFamily.PRD,
             final: str = "", vendor: str = V, collector: str = "site", manual: bool = False) -> Capture:
    return store.put_raw(f"raw bytes of {url} {status} {manual}".encode(), vendor_id=vendor, family=family,
                         collector=collector, url_requested=url, url_final=final, status=status,
                         content_type="text/html", retrieved_at="2026-10-02T12:00:00Z",
                         robots_decision="manual" if manual else "allowed", manual=manual)


def _document(store: EvidenceStore, cap: Capture, text: str, *, kind: str = "html") -> Document:
    sha, path = store.put_text(text)
    return Document(doc_id=sha, capture_id=cap.capture_id, vendor_id=cap.vendor_id, family=cap.family,
                    url=cap.url_final or cap.url_requested, title="", kind=kind, text_path=path, text_len=len(text))


def _passage(doc: Document, text: str, sentence: str) -> Passage:
    s = text.index(sentence)
    e = s + len(sentence)
    return Passage(passage_id=Passage.make_id(doc.doc_id, s, e), doc_id=doc.doc_id, vendor_id=doc.vendor_id,
                   start=s, end=e, text=text[s:e], hits=["machine learning"])


def _coverage(family: SourceFamily, status: CoverageStatus, collector: str, note: str = "") -> CoverageEntry:
    return CoverageEntry(vendor_id=V, family=family, mandatory=True, status=status, collector=collector, note=note)


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    store = EvidenceStore(tmp_path / "evidence")
    c_ai = _capture(store, AI_URL)
    c_old = _capture(store, OLD_URL, final=NEW_URL)
    c_403 = _capture(store, "https://www.acme.example/blocked/", status=403)
    c_dns = _capture(store, DNS_URL, family=SourceFamily.DNS, collector="dns")
    c_mirror = _capture(store, MIRROR_URL, family=SourceFamily.IND, collector="seeds")
    c_sec = _capture(store, SEC_URL, family=SourceFamily.REG, collector="sec")
    c_trap = _capture(store, TRAP_URL)
    d_ai = _document(store, c_ai, AI_TEXT)
    docs = [d_ai, _document(store, c_old, ROUTER_TEXT), _document(store, c_dns, DNS_TEXT, kind="dns"),
            _document(store, c_mirror, MIRROR_TEXT), _document(store, c_trap, TRAP_TEXT)]
    coverage = [
        _coverage(SourceFamily.PRD, CoverageStatus.DONE, "site", "12 candidate URLs; fetched 6"),
        _coverage(SourceFamily.IND, CoverageStatus.DONE, "seeds",
                  "seeded=True; 3 seeds, 2 fetched, 1 documents; 1 awaiting manual capture; failures: blocked_robots"),
        _coverage(SourceFamily.LEG, CoverageStatus.STOPPED, "site", "fetched 9; stopped(rule: cap), remainder logged"),
        _coverage(SourceFamily.DNS, CoverageStatus.DONE, "dns", "AI verification tokens: OpenAI"),
        _coverage(SourceFamily.REG, CoverageStatus.DONE, "sec", "1 filing fetched"),
    ]
    run = CollectionRun(run_id="V-001-20261002-aaaaaaaa", vendor_id=V, mode="replay", as_of="2026-10-02",
                        captures=[c_ai, c_old, c_403, c_dns, c_mirror, c_sec, c_trap], documents=docs,
                        passages=[_passage(d_ai, AI_TEXT, AI_SENTENCE)], coverage=coverage, leads=[LEAD_URL])
    runs = tmp_path / "runs"
    write_run(run, runs)
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    (seeds / f"{V}.toml").write_text(SEEDS, encoding="utf-8")
    lexicon = tmp_path / "lexicon.toml"
    lexicon.write_text(LEXICON, encoding="utf-8")
    return {"store": store, "runs": runs, "seeds": seeds, "lexicon": lexicon}


def _report(world: dict, findings=None, gold=None, **kw) -> dict:
    return gold_report(world["runs"], GOLD if gold is None else gold, findings, store=world["store"],
                       seeds_dir=world["seeds"], lexicon=world["lexicon"], tou=None, **kw)


def _item(url: str, excerpt: str, *, strength: str = "Weak", ai_type: str = "unspecified", u_class: str = "U3",
          family: SourceFamily = SourceFamily.PRD, url_final: str = "", locus: str = "service_feature",
          title: str = "", rule_labels: dict | None = None, llm_labels: dict | None = None,
          method: str = "rule", review_status: str = "unreviewed", vendor: str = V, providers: list | None = None,
          doc_id: str = "d" * 64) -> EvidenceItem:
    tags = SignalTags(u_class=u_class, sr="B", sp="S2", rl="R2", rc="T3", ic=2, locus=locus, ai_type=ai_type,
                      strength=strength)
    return EvidenceItem(vendor_id=vendor, doc_id=doc_id, capture_id="c" * 64, family=family, providers=providers or [],
                        source_type="Product page", publisher="Acme", title=title, url=url, url_final=url_final,
                        retrieved_at="2026-10-02T12:00:00Z", excerpt=excerpt, start=0, end=len(excerpt), tags=tags,
                        rule_labels=rule_labels or {}, llm_labels=llm_labels or {}, method=method,
                        review_status=review_status)


LABELS = {"temporal": "in_production", "action_level": "advisory", "sp": "S2"}


def _clean_items() -> list[EvidenceItem]:
    """Items a correct pipeline would produce: the trap is kept but never citable."""
    return [
        _item(AI_URL, AI_SENTENCE, strength="Strong", u_class="U1", rule_labels=LABELS, llm_labels=LABELS,
              method="rule+llm_agree"),
        _item(OLD_URL, ROUTER_SENTENCE, strength="Moderate", url_final=NEW_URL, rule_labels=LABELS,
              llm_labels={**LABELS, "temporal": "planned"}),
        _item(DNS_URL, "openai-domain-verification=dv-abc123XYZ", strength="Context - relationship only",
              u_class="U4", family=SourceFamily.DNS, locus="relationship"),
        _item(TRAP_URL, "Acme utilizes high-speed intelligent inserting equipment with automated collating.",
              strength="Marketing only", ai_type="not_ai", u_class="U7"),
    ]


def _leaky_items() -> list[EvidenceItem]:
    return [
        *_clean_items(),
        _item("https://www.acme.example/press", "These intelligent tools let Acme manage complex client work fast."),
        _item("https://www.acme.example/llms.txt", "This is an llms.txt file, meant for consumption by LLMs."),
        _item(ANVILS_URL, "Acme Anvils uses machine learning to forge anvils faster.", strength="Moderate"),
    ]


def _by_row(report: dict, section: str) -> dict[int, dict]:
    return {r["row"]: r for r in report[section]["rows"]}


def _by_url(report: dict) -> dict[str, dict]:
    return {u["url"]: u for u in report["url_recall"]["urls"]}


# --------------------------------------------------------------------------- gold rows and helpers


def test_real_gold_file_shape() -> None:
    rows = load_gold(REPO / DEFAULT_GOLD)
    assert len(rows) == 114
    assert {r["vendor_id"] for r in rows} == {f"V-00{n}" for n in range(1, 7)}
    assert {r["expect"] for r in rows} == set(EXPECT_STRENGTHS)
    assert {r["family"] for r in rows} <= {"PRD", "IND", "REG", "JOB", "DNS", "LEG", "SKIP"}
    assert all(r["url"].startswith("https://") for r in rows)


def test_check_gold_rejects_broken_rows() -> None:
    with pytest.raises(ValueError, match="JSON list"):
        check_gold({"rows": []})
    with pytest.raises(ValueError, match="row 2 lacks"):
        check_gold([g("https://a.example/x"), {"vendor_id": V, "url": "https://a.example"}])
    with pytest.raises(ValueError, match="expect 'certain'"):
        check_gold([g("https://a.example/x", expect="certain")])
    with pytest.raises(ValueError, match="empty url"):
        check_gold([g("  ")])


def test_expect_mapping_follows_the_contract() -> None:
    assert EXPECT_STRENGTHS["strong"] == {"Strong"}
    assert EXPECT_STRENGTHS["moderate"] == {"Moderate"}
    assert EXPECT_STRENGTHS["weak"] == {"Weak"} | CONTEXT_STRENGTHS
    assert EXPECT_STRENGTHS["marketing-only"] == {"Marketing only"}


@pytest.mark.parametrize(("url", "key"), [
    ("HTTPS://WWW.Example.COM/Path/?q=1#frag", "https://www.example.com/Path?q=1"),
    ("https://labarum.ai/", "https://labarum.ai"),
    ("https://labarum.ai", "https://labarum.ai"),
    ("https://dns.google/resolve?name=bny.com&type=TXT", "https://dns.google/resolve?name=bny.com&type=TXT"),
])
def test_normalize_url(url: str, key: str) -> None:
    assert normalize_url(url) == key


def test_normalize_text_maps_quotes_dashes_case_and_spaces() -> None:
    assert normalize_text("  Acme’s “AI”—now LIVE\n\tToday ") == "acme's \"ai\"-now live today"
    assert normalize_text("RTP® network") == "rtp network"


def test_segments_split_at_elisions_and_dns_records() -> None:
    assert segments("First part of the claim here. [...] Second part of the claim.") == [
        "first part of the claim here.", "second part of the claim."]
    assert segments("Alpha statement text… and a much longer closing statement.") == [
        "alpha statement text", "and a much longer closing statement."]
    assert segments("A long enough opening piece [...] ok") == ["a long enough opening piece"]
    assert segments("token-one=abcdefghij ; token-two=klmnopqrst", family="DNS") == [
        "token-one=abcdefghij", "token-two=klmnopqrst"]
    assert segments("token-one=abcdefghij ; token-two=klmnopqrst") == ["token-one=abcdefghij ; token-two=klmnopqrst"]
    assert segments("short") == ["short"]
    assert segments("") == []


# --------------------------------------------------------------------------- URL recall


def test_url_recall_statuses(world: dict) -> None:
    rep = _report(world)
    urls = _by_url(rep)
    assert urls["https://www.acme.example/ai"]["status"] == "captured"
    assert urls["https://www.acme.example/ai"]["rows"] == [1, 13, 14]  # one entry per normalised URL
    assert AI_URL not in urls
    assert urls[OLD_URL]["status"] == "captured"
    assert urls[NEW_URL]["status"] == "captured"  # the final URL of the redirect
    assert urls[DNS_URL]["status"] == "captured"
    assert urls[SEC_URL]["status"] == "captured"
    assert "no extracted document" in urls[SEC_URL]["reason"]
    assert urls["https://www.acme.example/blocked"]["status"] == "explained"
    assert "HTTP 403" in urls["https://www.acme.example/blocked"]["reason"]
    assert urls["https://robots.example/blocked"]["status"] == "explained"
    assert "blocked_robots" in urls["https://robots.example/blocked"]["reason"]
    assert "manual capture" in urls["https://paywall.example/story"]["reason"]
    assert "lead" in urls[LEAD_URL]["reason"]
    assert urls["https://www.acme.example/legal/privacy"]["status"] == "explained"
    assert "LEG site: stopped" in urls["https://www.acme.example/legal/privacy"]["reason"]
    assert urls["https://www.acme.example/never"]["status"] == "missed"
    assert urls["https://patents.example.com/p/1"]["status"] == "excluded"
    assert urls["https://www.fiserv.com/en/x.html"]["status"] == "excluded"
    assert "manual-only" in urls["https://www.fiserv.com/en/x.html"]["reason"]


def test_url_recall_counts_and_gate(world: dict) -> None:
    rep = _report(world)
    o = rep["url_recall"]["overall"]
    assert (o["urls"], o["captured"], o["explained"], o["missed"], o["excluded"]) == (17, 6, 5, 4, 2)
    assert o["in_scope"] == 15
    assert o["captured_pct"] == round(100 * 6 / 15, 1)
    assert o["captured_or_explained_pct"] == round(100 * 11 / 15, 1)
    assert rep["captured"] == 6 and rep["captured_pct"] == o["captured_pct"]
    assert rep["by_vendor"][V]["urls"] == o
    assert rep["runs"] == {V: "V-001-20261002-aaaaaaaa"}
    assert rep["gates"]["p2_url_recall"]["value"] == o["captured_or_explained_pct"]
    assert rep["gates"]["p2_url_recall"]["pass"] is False


def test_manual_only_rows_count_once_the_vendor_has_manual_captures(world: dict) -> None:
    gold = [*GOLD, g("https://www.fiserv.com/en/y.html", "weak", "Another manual-only statement about bots.")]
    rep = _report(world, gold=gold)
    assert _by_url(rep)["https://www.fiserv.com/en/y.html"]["status"] == "excluded"
    _capture(world["store"], "https://www.fiserv.com/en/x.html", collector="manual", manual=True)
    rep = _report(world, gold=gold)
    urls = _by_url(rep)
    assert urls["https://www.fiserv.com/en/x.html"]["status"] == "captured"
    assert urls["https://www.fiserv.com/en/x.html"]["reason"].startswith("manual capture")
    assert urls["https://www.fiserv.com/en/y.html"]["status"] == "missed"


def test_terms_register_marks_manual_only_hosts(world: dict) -> None:
    tou = TouRegister({"default": {"automation": "limited"},
                       "host": [{"match": "manual.example", "automation": "none"}]})
    gold = [g("https://docs.manual.example/page", "weak", "A statement on a host whose terms bar automation.")]
    rep = gold_report(world["runs"], gold, store=world["store"], seeds_dir=world["seeds"],
                      lexicon=world["lexicon"], tou=tou)
    url = rep["url_recall"]["urls"][0]
    assert url["status"] == "excluded" and url["manual_only"] is True


def test_dns_rows_match_by_domain_and_are_excluded_without_dns_captures(world: dict) -> None:
    cf = "https://cloudflare-dns.com/dns-query?name=acme.example&type=TXT"
    other = "https://dns.google/resolve?name=other.example&type=TXT"
    rep = _report(world, gold=[g(cf, "weak", "openai-domain-verification=dv-abc123XYZ", "DNS")])
    url = rep["url_recall"]["urls"][0]
    assert (url["status"], url["basis"]) == ("captured", "dns domain")
    assert _by_row(rep, "passage_recall")[1]["status"] == "located"
    rep = _report(world, gold=[g(other, "weak", "token=abcdefghijklmnop", "DNS", vendor="V-002")])
    assert rep["url_recall"]["urls"][0]["status"] == "excluded"
    assert "DNS" in rep["url_recall"]["urls"][0]["reason"]


# --------------------------------------------------------------------------- passage recall


def test_passage_recall_rows(world: dict) -> None:
    rep = _report(world)
    rows = _by_row(rep, "passage_recall")
    assert rows[1]["status"] == "located" and 90 <= rows[1]["score"] < 100
    assert rows[1]["in_passage"] is True
    loc = rows[1]["located_in"][0]
    assert AI_TEXT[loc["start"]:loc["end"]] == AI_SENTENCE
    assert rows[2]["status"] == "located" and rows[2]["score"] == 100.0
    assert rows[2]["located_in"][0]["url"] == NEW_URL
    assert ROUTER_TEXT[rows[2]["located_in"][0]["start"]:rows[2]["located_in"][0]["end"]] == ROUTER_SENTENCE
    assert rows[9]["status"] == "located" and rows[9]["in_passage"] is None  # DNS: tagged per token
    assert rows[12]["status"] == "located_elsewhere"
    assert rows[12]["located_in"][0]["url"] == MIRROR_URL and rows[12]["located_in"][0]["where"] == "elsewhere"
    assert rows[12]["in_passage"] is False
    assert rows[13]["status"] == "located" and rows[13]["segments"] == rows[13]["segments_located"] == 2
    assert rows[14]["status"] == "partial" and rows[14]["segments_located"] == 1
    assert rows[15]["status"] == "not_located" and rows[15]["score"] < 90
    assert "summarised" in rows[15]["reason"]
    assert rows[16]["status"] == "not_located" and rows[16]["score"] is None
    assert "no text was extracted" in rows[16]["reason"]
    assert rows[3]["status"] == "not_located" and "not captured" in rows[3]["reason"]
    assert rows[10]["scope"] == rows[11]["scope"] == "excluded"


def test_passage_recall_summary(world: dict) -> None:
    rep = _report(world)
    o = rep["passage_recall"]["overall"]
    assert (o["rows"], o["in_scope"]) == (19, 17)
    assert (o["located"], o["located_elsewhere"], o["partial"], o["not_located"]) == (5, 1, 1, 10)
    assert o["recall"] == round(100 * 6 / 17, 1) and o["recall_at_url"] == round(100 * 5 / 17, 1)
    assert (o["in_passage"], o["in_passage_of"]) == (3, 6)  # rows 1, 13 and 14 hit the one lexicon passage
    assert rep["passage_recall"]["by_expect"]["strong"]["located"] == 2


def test_threshold_is_respected(world: dict) -> None:
    rows = _by_row(_report(world, threshold=100.0), "passage_recall")
    assert rows[1]["status"] == "not_located"  # "machine-learning" differs from the source by one character
    assert rows[2]["status"] == "located"


def test_a_short_document_never_contains_a_long_excerpt(world: dict, tmp_path: Path) -> None:
    store = world["store"]
    short = _capture(store, "https://www.acme.example/short")
    doc = _document(store, short, "Acme AI.")
    run = CollectionRun(run_id="V-001-20261003-bbbbbbbb", vendor_id=V, mode="replay", as_of="2026-10-03",
                        captures=[short], documents=[doc])
    runs = tmp_path / "short_runs"
    write_run(run, runs)
    gold = [g("https://www.acme.example/short", "weak", "Acme AI. A much longer claim that the page never makes.")]
    rep = gold_report(runs, gold, store=store, seeds_dir=None, lexicon=world["lexicon"], tou=None)
    assert rep["passage_recall"]["rows"][0]["status"] == "not_located"


def test_a_captured_page_without_text_is_reported_as_empty(world: dict, tmp_path: Path) -> None:
    store = world["store"]
    shell = _capture(store, "https://jobs.acme.example/job/42", family=SourceFamily.JOB, collector="seeds")
    doc = _document(store, shell, "")  # a script-rendered page: the static HTML has no text
    runs = tmp_path / "shell_runs"
    write_run(CollectionRun(run_id="V-001-20261003-eeeeeeee", vendor_id=V, mode="replay", as_of="2026-10-03",
                            captures=[shell], documents=[doc]), runs)
    gold = [g("https://jobs.acme.example/job/42", "moderate", "The analyst builds machine learning features.", "JOB")]
    rep = gold_report(runs, gold, store=store, seeds_dir=None, lexicon=world["lexicon"], tou=None)
    row = rep["passage_recall"]["rows"][0]
    assert rep["url_recall"]["urls"][0]["status"] == "captured"
    assert (row["status"], row["score"]) == ("not_located", None)
    assert "no text" in row["reason"] and "script" in row["reason"]


def test_wayback_copies_count_for_the_original_url(world: dict, tmp_path: Path) -> None:
    store = world["store"]
    original = "https://www.acme.example/retired-page"
    replay = f"https://web.archive.org/web/20250101000000id_/{original}"
    cap = _capture(store, replay, family=SourceFamily.HIST, collector="wayback")
    doc = _document(store, cap, "Retired page\n\nAcme once piloted a neural network for dispute triage.\n")
    runs = tmp_path / "wayback_runs"
    write_run(CollectionRun(run_id="V-001-20261003-dddddddd", vendor_id=V, mode="replay", as_of="2026-10-03",
                            captures=[cap], documents=[doc]), runs)
    gold = [g(original, "weak", "Acme once piloted a neural network for dispute triage.")]
    item = _item(replay, "Acme once piloted a neural network for dispute triage.")  # cited by its replay URL
    rep = gold_report(runs, gold, [item], store=store, seeds_dir=None, lexicon=world["lexicon"], tou=None)
    assert rep["url_recall"]["urls"][0]["status"] == "captured"
    assert rep["passage_recall"]["rows"][0]["status"] == "located"
    assert (rep["strength"]["rows"][0]["basis"], rep["strength"]["rows"][0]["agree"]) == ("excerpt", True)


# --------------------------------------------------------------------------- strength, LLM, traps


def test_without_findings_item_sections_are_not_evaluated(world: dict) -> None:
    rep = _report(world)
    assert rep["strength"]["evaluated"] is False and rep["strength"]["rows"] == []
    assert rep["matched"] is None and rep["label_agreement"] is None and rep["confusion"] == {}
    assert rep["llm"] == {"evaluated": False}
    assert rep["traps"]["evaluated"] is False and rep["traps"]["passed"] is None
    assert {t["status"] for t in rep["traps"]["gold_rows"]} == {"not_evaluated"}
    assert rep["gates"]["p3_traps"]["pass"] is None


def test_strength_agreement(world: dict) -> None:
    rep = _report(world, _leaky_items())
    rows = _by_row(rep, "strength")
    assert (rows[1]["label"], rows[1]["basis"], rows[1]["agree"]) == ("Strong", "excerpt", True)
    assert (rows[2]["label"], rows[2]["basis"], rows[2]["agree"]) == ("Moderate", "excerpt", True)
    assert (rows[9]["label"], rows[9]["agree"]) == ("Context - relationship only", True)
    assert (rows[14]["label"], rows[14]["agree"]) == ("Strong", False)
    assert (rows[15]["label"], rows[15]["basis"], rows[15]["agree"]) == ("Moderate", "url", False)
    assert (rows[17]["label"], rows[17]["agree"]) == (TRAP_LABEL, None)
    assert rows[16]["label"] == NONE_LABEL and rows[16]["agree"] is None
    assert 10 not in rows  # SKIP rows are not scored
    o = rep["strength"]["overall"]
    assert (o["matched"], o["agree"]) == (8, 5)
    assert rep["matched"] == 8 and rep["label_agreement"] == 62.5
    assert (o["matched_by_excerpt"], o["traps"]) == (6, 1)
    assert rep["confusion"]["strong"] == {"Strong": 2, NONE_LABEL: 1}
    assert rep["confusion"]["weak"][TRAP_LABEL] == 1
    assert list(rep["confusion"]) == ["strong", "moderate", "weak", "marketing-only"]


def test_unmatched_lists_capture_and_evidence_gaps(world: dict) -> None:
    rep = _report(world, _clean_items())
    stages = {(u["url"], u["stage"]) for u in rep["unmatched"]}
    assert (SEC_URL, "evidence") in stages
    assert ("https://www.acme.example/never", "capture") in stages
    assert (AI_URL, "evidence") not in stages


def test_llm_comparison(world: dict) -> None:
    llm = _report(world, _clean_items())["llm"]
    assert (llm["items"], llm["items_with_llm"], llm["agree"], llm["disagree"]) == (4, 2, 1, 1)
    assert llm["disagree_by_field"] == {"temporal": 1, "action_level": 0, "sp": 0}
    assert llm["methods"] == {"rule": 3, "rule+llm_agree": 1}


def _kinds(traps: dict) -> dict[tuple[int, str], dict]:
    return {(t["row"], t["kind"]): t for t in traps["gold_rows"]}


def test_trap_checks_pass_for_a_clean_pipeline(world: dict) -> None:
    traps = _report(world, _clean_items())["traps"]
    kinds = _kinds(traps)
    assert set(kinds) == {(17, "definition"), (18, "definition"), (18, "source"), (19, "entity")}
    assert (kinds[17, "definition"]["phrases"], kinds[17, "definition"]["status"]) == (
        ["intelligent inserting"], "rejected")
    assert kinds[18, "definition"]["phrases"] == ["llms.txt", "meant for consumption by LLMs"]
    assert (kinds[18, "source"]["phrases"], kinds[18, "source"]["status"]) == (["llms.txt"], "suppressed")
    assert (kinds[19, "entity"]["phrases"], kinds[19, "entity"]["status"]) == (["Acme Anvils"], "suppressed")
    assert (traps["items"], traps["rejected"]) == (1, 1)
    assert traps["leaks"] == [] and traps["passed"] is True
    vocab = traps["vocabulary"]
    assert vocab["entity"] == {V: ["Acme Anvils", "Claude Reumert", "Acme Holdings"]}  # the vendor's own name: no
    assert vocab["collision_ciks"] == {V: ["7654321"]}
    assert vocab["source"] == [r"/llms(?:-full)?\.txt$"] and vocab["relabel"] == 2
    assert vocab["definition"] == ["intelligent inserting", "llms.txt", "intelligent tools",
                                   "meant for consumption by LLMs", r"\b23ai\b"]


def test_trap_checks_catch_leaks(world: dict) -> None:
    rep = _report(world, _leaky_items())
    traps = rep["traps"]
    kinds = _kinds(traps)
    assert kinds[18, "source"]["status"] == kinds[18, "definition"]["status"] == "leaked"
    assert kinds[19, "entity"]["status"] == "leaked"
    assert kinds[17, "definition"]["status"] == "rejected"
    reasons = {x["url"]: " ".join(x["reasons"]) for x in traps["leaks"]}
    assert "intelligent tools" in reasons["https://www.acme.example/press"]
    assert "trap source" in reasons["https://www.acme.example/llms.txt"]
    assert "Acme Anvils" in reasons[ANVILS_URL] and "not the vendor" in reasons[ANVILS_URL]
    assert traps["passed"] is False and rep["gates"]["p3_traps"]["pass"] is False


def test_trap_checks_follow_the_rules_semantics(world: dict) -> None:
    sec = "https://www.sec.gov/Archives/edgar/data/7654321/000765432126000001/x10k.htm"
    doc_text = ("Acme Payments Ltd partners with Acme Anvils on payments.\n\n"
                "Acme Anvils uses machine learning to forge anvils faster.\n")
    doc_id, _ = world["store"].put_text(doc_text)
    items = [
        *_clean_items(),
        # a first-party page never fails the entity test, whatever it names
        _item("https://www.acme.example/team", "Claude Reumert leads the Acme machine learning team."),
        # a third-party source that also names the vendor passes the entity test
        _item("https://news.example.org/q", "Claude Reumert said Acme Payments uses machine learning for fraud."),
        # the whole document names the vendor, so the collision does not account for the vendor's name
        _item("https://www.partners.example/acme", "Acme Anvils uses machine learning to forge anvils faster.",
              doc_id=doc_id),
        # a provider name read out of a collision
        _item("https://news.example.org/q2", "Claude Reumert said Acme Payments uses machine learning for alerts.",
              providers=["Claude"]),
        # an EDGAR filing of the colliding company
        _item(sec, "We use machine learning in underwriting.", family=SourceFamily.REG),
        # automation relabelled as AI, with and without ML evidence
        _item("https://www.acme.example/scheduler", "Acme AI-powered scheduling runs every batch job on time."),
        _item("https://www.acme.example/scheduler2",
              "Acme AI-powered scheduling uses a machine learning model to predict delays."),
        _item("https://www.acme.example/scheduler3", "Acme smart scheduling relies on a forecasting model."),
        _item("https://www.acme.example/scheduler4", "Acme smart scheduling follows our operating model."),
        # "23ai" is masked as a rule suppressor, so nothing AI is left
        _item("https://www.acme.example/db", "Acme runs Oracle 23ai for its ledgers."),
    ]
    leaks = {x["url"]: " ".join(x["reasons"]) for x in _report(world, items)["traps"]["leaks"]}
    assert set(leaks) == {"https://news.example.org/q2", sec, "https://www.acme.example/scheduler",
                          "https://www.acme.example/scheduler4", "https://www.acme.example/db"}
    assert "provider read from a collision: Claude" in leaks["https://news.example.org/q2"]
    assert "CIK 7654321" in leaks[sec]
    assert "relabelled automation without ML evidence: AI-powered scheduling" in leaks[
        "https://www.acme.example/scheduler"]
    assert "smart scheduling" in leaks["https://www.acme.example/scheduler4"]
    assert "23ai" in leaks["https://www.acme.example/db"]


def test_empty_findings_leave_the_trap_gate_undecided(world: dict) -> None:
    rep = _report(world, [])
    assert rep["traps"]["evaluated"] is True and rep["traps"]["passed"] is None
    assert rep["strength"]["evaluated"] is True and rep["strength"]["rows"] == []
    assert any("no evidence items" in n for n in rep["notes"])
    assert any("no findings for V-001" in n for n in rep["notes"])


def test_rejected_items_never_leak(world: dict) -> None:
    items = [*_clean_items(), _item(ANVILS_URL, "Acme Anvils uses machine learning daily.",
                                    review_status="rejected")]
    traps = _report(world, items)["traps"]
    assert traps["leaks"] == []
    assert _kinds(traps)[19, "entity"]["status"] == "rejected"


# --------------------------------------------------------------------------- inputs, runs, determinism


def test_assessment_result_and_pinned_collection_runs(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    vid = samples.VENDOR_ID
    runs = tmp_path / "runs"
    cap = _capture(store, samples.URL, vendor=vid)
    doc = _document(store, cap, samples.DOC_TEXT)
    for run_id, created in (("run-a", "2026-10-02T10:00:00Z"), ("run-b", "2026-10-02T09:00:00Z")):
        d = runs / run_id
        d.mkdir(parents=True)
        caps = [cap] if run_id == "run-b" else []
        (d / "captures.jsonl").write_text("".join(json.dumps(c.model_dump(mode="json")) + "\n" for c in caps),
                                          encoding="utf-8")
        manifest = {"run_id": run_id, "vendor_id": vid, "as_of": "2026-10-02", "created_at": created,
                    "documents": [doc.model_dump(mode="json")] if caps else [], "leads": []}
        (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (runs / "A-20261002-deadbeef").mkdir()
    (runs / "A-20261002-deadbeef" / "manifest.json").write_text("{}", encoding="utf-8")
    assert load_runs(runs)[vid].run_id == "run-a"  # newest by created_at
    result = samples.assessment(manifest={"collection_runs": {vid: "run-b"}})
    gold = [g(samples.URL, "strong", samples.EXCERPT, vendor=vid)]
    rep = gold_report(runs, gold, result, store=store, seeds_dir=None, lexicon=tmp_path / "missing.toml", tou=None)
    assert rep["runs"] == {vid: "run-b"}
    assert rep["url_recall"]["urls"][0]["status"] == "captured"
    assert rep["passage_recall"]["rows"][0]["status"] == "located"
    assert (rep["strength"]["rows"][0]["label"], rep["strength"]["rows"][0]["agree"]) == ("Strong", True)
    assert any("lexicon unreadable" in n for n in rep["notes"])


def test_findings_load_from_an_assessment_run(tmp_path: Path) -> None:
    result = samples.assessment()
    run = tmp_path / "runs" / result.run_id
    run.mkdir(parents=True)
    (run / "assessment.json").write_text(result.model_dump_json(), encoding="utf-8")
    loaded = load_findings(run)
    assert isinstance(loaded, AssessmentResult) and loaded == result
    assert load_findings(run / "assessment.json") == result
    items = [i for f in result.vendors for i in f.evidence]
    (tmp_path / "evidence.jsonl").write_text(
        "".join(json.dumps(i.model_dump(mode="json"), sort_keys=True) + "\n" for i in items), encoding="utf-8")
    assert load_findings(tmp_path / "evidence.jsonl") == items
    only_items = tmp_path / "items_only"
    only_items.mkdir()
    (only_items / "evidence.jsonl").write_bytes((tmp_path / "evidence.jsonl").read_bytes())
    assert load_findings(only_items) == items
    with pytest.raises(FileNotFoundError):
        load_findings(tmp_path / "nowhere")
    gold = [g(samples.URL, "strong", samples.EXCERPT, vendor=samples.VENDOR_ID)]
    rep = gold_report(tmp_path / "runs", gold, run, store=EvidenceStore(tmp_path / "evidence"), seeds_dir=None,
                      tou=None)
    assert rep["strength"]["evaluated"] is True and rep["strength"]["rows"][0]["agree"] is True


def test_write_report_saves_json_and_markdown(world: dict, tmp_path: Path) -> None:
    rep = _report(world, _clean_items())
    out = write_report(rep, tmp_path / "out" / "gold_report.json", tmp_path / "out" / "gold_report.md")
    assert out == {"json": (tmp_path / "out" / "gold_report.json").as_posix(),
                   "markdown": (tmp_path / "out" / "gold_report.md").as_posix()}
    raw = (tmp_path / "out" / "gold_report.json").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    assert json.loads(raw.decode("utf-8")) == json.loads(json.dumps(rep))
    assert raw.decode("utf-8") == json.dumps(rep, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    assert (tmp_path / "out" / "gold_report.md").read_bytes() == render_markdown(rep).encode("utf-8")
    assert write_report(rep, tmp_path / "only.json") == {"json": (tmp_path / "only.json").as_posix()}


def test_unreadable_runs_are_skipped_with_a_note(tmp_path: Path) -> None:
    d = tmp_path / "runs" / "V-001-20261002-cccccccc"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    (d / "captures.jsonl").write_text("", encoding="utf-8")
    notes: list[str] = []
    assert load_runs(tmp_path / "runs", notes=notes) == {}
    assert notes == ["skipped run V-001-20261002-cccccccc: unreadable manifest (JSONDecodeError)"]
    assert load_runs(tmp_path / "nowhere") == {}


def test_contract_call_form_takes_findings_first() -> None:
    vid = samples.VENDOR_ID
    gold = [g(samples.URL, "strong", samples.EXCERPT, vendor=vid)]
    rep = gold_report(samples.assessment(), gold, seeds_dir=None, tou=None)
    assert rep["url_recall"]["evaluated"] is False and rep["passage_recall"]["evaluated"] is False
    assert rep["captured"] is None and rep["unmatched"] == []
    assert rep["strength"]["rows"][0]["agree"] is True
    with pytest.raises(TypeError):
        gold_report(samples.assessment(), gold, samples.assessment())


def test_vendors_filter_and_missing_runs(world: dict) -> None:
    gold = [*GOLD, g("https://www.beta.example/ai", "weak", "Beta statement about machine learning.", vendor="V-002")]
    rep = _report(world, gold=gold, vendors=["V-002"])
    assert rep["gold"]["vendors"] == ["V-002"] and rep["gold_items"] == 1
    url = rep["url_recall"]["urls"][0]
    assert (url["status"], url["reason"]) == ("missed", "no collection run for this vendor")


def test_report_is_deterministic_and_json_serialisable(world: dict) -> None:
    a = _report(world, _leaky_items())
    b = _report(world, _leaky_items())
    assert a == b
    text = json.dumps(a, sort_keys=True, ensure_ascii=False)
    assert "Synthetic analyst note" not in text  # gold 'shows' never copied
    assert "Paywalled statement" not in text  # gold excerpts never copied
    assert a["gold"]["rows"] == len(GOLD) and len(a["gold"]["sha256"]) == 64


def test_markdown_page(world: dict) -> None:
    rep = _report(world, _leaky_items())
    md = render_markdown(rep)
    assert md == render_markdown(rep)
    assert md.endswith("\n") and "\r" not in md
    for heading in ("# Gold-set evaluation", "## Gates", "## URL recall", "## Passage recall",
                    "## Strength agreement", "## Gemini vs rules", "## Trap checks"):
        assert heading in md
    assert "| P2 URL recall |" in md and "FAIL" in md
    outside = md.split("### Located excerpts outside every lexicon passage")[1].split("\n## ")[0]
    assert "| 2 | V-001 |" in outside and "| 12 | V-001 |" in outside and "| 1 | V-001 |" not in outside
    assert "Paywalled statement" not in md and "Synthetic analyst note" not in md
    assert "never evidence" in md
    header = next(line for line in md.splitlines() if line.startswith("| Gold expect |"))
    assert all(f"| {label} |" in header + " |" for label in ("Strong", "Moderate", "Weak", "Marketing only", "none"))
    assert "Vocabulary: 5 definition-test phrases or patterns, 2 relabelled-automation patterns" in md
    bare = render_markdown(_report(world))
    assert "Not evaluated: no findings were given." in bare


# --------------------------------------------------------------------------- the real gold set


def test_real_gold_set_against_an_empty_collection(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    rep = gold_report(runs, REPO / DEFAULT_GOLD, store=EvidenceStore(tmp_path / "evidence"),
                      seeds_dir=REPO / "seeds")
    assert rep["gold_items"] == 114
    urls = rep["url_recall"]["urls"]
    assert {u["status"] for u in urls} == {"missed", "excluded"}
    excluded = {u["url"]: u for u in urls if u["status"] == "excluded"}
    # config/tou.toml changes over time: only the reasons for exclusion are pinned, not the host list
    assert all(u["manual_only"] or u["family"] in ("SKIP", "DNS") for u in excluded.values())
    assert all(u["manual_only"] for url, u in excluded.items() if url.startswith("https://www.fiserv.com/"))
    assert any("SKIP" in u["reason"] for u in excluded.values())
    assert not any(u["manual_only"] for u in urls if u["status"] == "missed")
    traps = {(t["row"], t["kind"]) for t in rep["traps"]["gold_rows"]}
    assert {(47, "definition"), (54, "definition"), (70, "source")} <= traps
    assert rep["passage_recall"]["overall"]["located"] == 0
    md = render_markdown(rep)
    assert md.count("\n## ") >= 6
