"""SEC, JOB, Wayback and Seeds collectors (offline, recorded/handmade fixtures)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from urllib.parse import quote

from footprint.collectors.jobs import JobsCollector
from footprint.collectors.sec import TICKERS_URL, SecCollector, efts_url, norm_name, resolve_cik_from_tickers
from footprint.collectors.seeds import SeedsCollector
from footprint.collectors.wayback import AVAILABILITY_URL, CDX_URL, REPLAY_URL, WaybackCollector, parse_cdx_first
from footprint.models import CoverageStatus, SourceFamily

_p = Path(__file__).resolve().parents[1] / "fixtures" / "collectors" / "_fakes.py"
_spec = importlib.util.spec_from_file_location("collector_fakes", _p)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)
fx = F.fx

TODAY = dt.date(2026, 10, 2)

# ------------------------------------------------------------------ SEC


def test_resolve_cik_exact_only():
    raw = fx("sec_company_tickers.json")
    assert resolve_cik_from_tickers(raw, ["Fiserv, Inc."]) == "0000798354"
    assert resolve_cik_from_tickers(raw, ["Fiserv Solutions Something"]) == ""
    assert norm_name("The Bank of New York Mellon Corporation") == norm_name("Bank of New York Mellon Corp")


def test_sec_not_registrant_not_applicable_with_evidence():
    ctx = F.ctx({TICKERS_URL: (200, fx("sec_company_tickers.json"), "application/json")}, seeds={"aliases": ["AutomWorx"]})
    res = SecCollector(today=TODAY).collect(ctx)
    cov = res.coverage[0]
    assert cov.status == CoverageStatus.NOT_APPLICABLE and "company_tickers" in cov.note and res.captures


def test_sec_registrant_flow_fetches_primary_docs():
    cik = "0001390777"
    start = dt.date(2024, 10, 2)
    routes = {
        "https://data.sec.gov/submissions/CIK0001390777.json": (200, fx("sec_submissions.json"), "application/json"),
        efts_url('"artificial intelligence"', cik, start, TODAY): (200, fx("sec_efts.json"), "application/json"),
        "https://www.sec.gov/Archives/edgar/data/1390777/000139077726000010/bk-20250930.htm": (200, b"<html>q</html>", "text/html"),
        "https://www.sec.gov/Archives/edgar/data/1390777/000139077726000020/bk-ex99.htm": (200, b"<html>8k</html>", "text/html"),
    }
    ctx = F.ctx(routes, seeds={"sec_cik": "1390777"}, prof=F.profile("V-005", "BNY", "https://www.bny.com"))
    res = SecCollector(today=TODAY, queries=('"artificial intelligence"',)).collect(ctx)
    assert set(routes) <= set(ctx.fetcher.calls)
    assert "dateRange=custom&startdt=2024-10-02" in efts_url("q", cik, start, TODAY)
    cov = res.coverage[0]
    assert cov.status == CoverageStatus.DONE and "2 filing docs hit" in cov.note and cov.requests_used == 4


# ------------------------------------------------------------------ JOB

WK = "https://apply.workable.com/api/v1/widget/accounts/fssi?details=true"


def test_jobs_none_and_manual():
    r = JobsCollector().collect(F.ctx({}, seeds={"ats": {"platform": "none", "note": "no ATS"}}))
    assert r.coverage[0].status == CoverageStatus.NOT_APPLICABLE
    r = JobsCollector().collect(F.ctx({}, seeds={"ats": {"platform": "manual", "host": "careers.fiserv.com"}}))
    assert r.coverage[0].status == CoverageStatus.PENDING
    assert r.coverage[0].note.startswith("awaiting manual capture")
    assert r.leads == ["https://careers.fiserv.com/"] and not r.captures


def test_jobs_manual_is_done_manual_once_postings_are_imported():
    """Regression (V-002 JOB 'pending'): with analyst captures of postings in the store the family is done_manual."""
    from footprint.models import Capture

    ctx = F.ctx({}, seeds={"ats": {"platform": "manual", "host": "careers.fiserv.com"}})
    cap = Capture(capture_id="a" * 64, vendor_id="V-001", family=SourceFamily.JOB, collector="manual",
                  url_requested="https://careers.fiserv.com/us/en/job/R-1", status=200, manual=True,
                  captured_by="human:RK", retrieved_at="2026-10-03T10:00:00Z")
    ctx.store.manual_captures = lambda vid: [cap] if vid == "V-001" else []
    r = JobsCollector().collect(ctx)
    assert r.coverage[0].status == CoverageStatus.DONE_MANUAL and "human:RK" in r.coverage[0].note
    assert ctx.fetcher.calls == []


WD = "tch.wd108.myworkdayjobs.com"
WD_ATS = {"platform": "workday", "host": WD, "tenant": "tch", "site": "TCH"}


def _wd_job(title, html, start="2026-09-01", path="Charlotte/X_R1"):
    return json.dumps({"jobPostingInfo": {"title": title, "jobDescription": html, "startDate": start,
                                          "externalUrl": f"https://{WD}/TCH/job/{path}"}}).encode()


def test_workday_seeded_posting_read_through_json_api():
    """Regression (V-006): seeded Workday postings whose titles miss the role list (a paralegal) are still read,
    through the CXS JSON API, because the career page itself is a script shell with no text."""
    sm = (f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          f'<url><loc>https://{WD}/TCH/job/Charlotte/Senior-Contracts-Paralegal_JR1</loc></url>'
          f'<url><loc>https://{WD}/TCH/job/NC/Manager--NOC-Operations_JR2</loc></url>'
          f'<url><loc>https://{WD}/TCH/job/NY/Receptionist_JR3</loc></url></urlset>').encode()
    seed_url = f"https://{WD}/TCH/job/Charlotte/Senior-Contracts-Paralegal_JR1"
    routes = {
        f"https://{WD}/TCH/siteMap.xml": (200, sm, "application/xml"),
        f"https://{WD}/wday/cxs/tch/TCH/job/Charlotte/Senior-Contracts-Paralegal_JR1":
            (200, _wd_job("Senior Contracts Paralegal", "<p>Use AI tools to review contracts.</p>",
                          path="Charlotte/Senior-Contracts-Paralegal_JR1"), "application/json"),
        f"https://{WD}/wday/cxs/tch/TCH/job/NC/Manager--NOC-Operations_JR2":
            (200, _wd_job("Manager, NOC Operations",
                          "<p>Drives continuous process improvement.</p><p>The successful candidate leverages AI."
                          "</p><ul><li>Lead operations</li><li>Utilize AI-powered tools such as Microsoft Copilot, "
                          "ChatGPT, ServiceNow AI.</li></ul>", path="NC/Manager--NOC-Operations_JR2"),
             "application/json"),
    }
    seeds = {"ats": WD_ATS, "seed": [{"url": seed_url, "family": "JOB", "automation": "auto"}]}
    ctx = F.ctx(routes, seeds=seeds)
    ctx.as_of = "2026-10-02"
    r = JobsCollector().collect(ctx)
    assert {d.title for d in r.documents} == {"Senior Contracts Paralegal", "Manager, NOC Operations"}
    assert not any("Receptionist" in c for c in ctx.fetcher.calls)
    assert seed_url in {d.url for d in r.documents}
    noc = next(d for d in r.documents if d.title.startswith("Manager"))
    text = ctx.store.get_text(noc.doc_id)
    assert "improvement.\n\nThe successful" in text and "\n\nUtilize AI-powered tools" in text
    from footprint.extract import find_passages, load_core_lexicon

    ps = [text[s:e] for s, e, _ in find_passages(text, load_core_lexicon())]
    assert any("Microsoft Copilot" in p for p in ps)
    assert "1 seeded" in r.coverage[0].note


def test_postings_older_than_24_months_are_skipped():
    sm = (f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          f'<url><loc>https://{WD}/TCH/job/NY/AI-Engineer_R9</loc></url></urlset>').encode()
    routes = {f"https://{WD}/TCH/siteMap.xml": (200, sm, "application/xml"),
              f"https://{WD}/wday/cxs/tch/TCH/job/NY/AI-Engineer_R9":
                  (200, _wd_job("AI Engineer", "<p>Build AI.</p>", start="2024-06-30"), "application/json")}
    ctx = F.ctx(routes, seeds={"ats": WD_ATS})
    ctx.as_of = "2026-10-02"
    r = JobsCollector().collect(ctx)
    assert r.documents == [] and "older than 24 months" in r.coverage[0].note


def test_workable_filters_ai_postings():
    ctx = F.ctx({WK: (200, fx("workable_fssi.json"), "application/json")},
                seeds={"ats": {"platform": "workable", "account": "fssi"}})
    r = JobsCollector().collect(ctx)
    assert [d.title for d in r.documents] == ["Machine Learning Engineer"]
    assert r.documents[0].url == "https://apply.workable.com/j/AB1" and r.documents[0].family == SourceFamily.JOB
    assert r.coverage[0].status == CoverageStatus.DONE and "2 postings listed" in r.coverage[0].note


def test_workday_sitemap_then_job_json():
    h = "tch.wd108.myworkdayjobs.com"
    routes = {f"https://{h}/TCH/siteMap.xml": (200, fx("workday_sitemap.xml"), "application/xml"),
              f"https://{h}/wday/cxs/tch/TCH/job/New-York/AI-Engineer_R100": (200, fx("workday_job.json"), "application/json")}
    ctx = F.ctx(routes, seeds={"ats": {"platform": "workday", "host": h, "tenant": "tch", "site": "TCH"}})
    r = JobsCollector().collect(ctx)
    assert [d.title for d in r.documents] == ["AI Engineer"] and r.documents[0].published == "2026-09-01"
    assert not any("Receptionist" in c for c in ctx.fetcher.calls)


def test_oracle_orc_keyword_and_detail():
    h, s = "acme.fa.us2.oraclecloud.com", "CX_3001"  # an unregistered tenant (BNY's is manual-only, see below)
    base = f"https://{h}/hcmRestApi/resources/latest"
    lst = (f"{base}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;"
           f"siteNumber={s},keyword=%22artificial%20intelligence%22,limit=50,sortBy=RELEVANCY")
    det = f"{base}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id=%22555%22,siteNumber={s}"
    ctx = F.ctx({lst: (200, fx("orc_list.json"), "application/json"), det: (200, fx("orc_detail.json"), "application/json")},
                seeds={"ats": {"platform": "oracle_orc", "host": h, "site": "BNY-Careers", "site_number": s}})
    r = JobsCollector().collect(ctx)
    assert [d.title for d in r.documents] == ["Data Scientist"]
    assert r.documents[0].url.endswith("/sites/BNY-Careers/job/555")


def test_ats_on_a_terms_barred_host_is_manual_sampling():
    """BNY's Terms of Use bar automated systems (config/tou.toml, verified 2026-10-03): its Oracle tenant is
    never requested, even when the seeds still name the automated reader."""
    from footprint.collectors.base import is_manual_only

    h = "eofe.fa.us2.oraclecloud.com"
    assert is_manual_only(f"https://{h}/x") and is_manual_only("https://www.bny.com/x")
    assert not is_manual_only("https://www.theclearinghouse.org/x")
    ctx = F.ctx({}, seeds={"ats": {"platform": "oracle_orc", "host": h, "site": "BNY-Careers", "site_number": "CX_3001"}})
    r = JobsCollector().collect(ctx)
    assert ctx.fetcher.calls == [] and r.coverage[0].status == CoverageStatus.PENDING


def test_jobs_cap_stops():
    r = JobsCollector().collect(F.ctx({WK: (200, b"{}", "x")}, seeds={"ats": {"platform": "workable", "account": "fssi"}},
                                      caps={SourceFamily.JOB: 0}))
    assert r.coverage[0].status == CoverageStatus.STOPPED


# ------------------------------------------------------------------ Wayback + Seeds

U = "https://example.com/"
SEEDS = {"seed": [{"url": U, "family": "PRD", "automation": "auto", "blocked": True, "expect": "strong"},
                  {"url": "https://dir.texas.gov/x", "family": "IND", "automation": "manual"},
                  {"url": "https://www.fiserv.com/ai", "family": "PRD", "automation": "auto"}]}


def test_cdx_first():
    assert parse_cdx_first(fx("wayback_cdx.json")) == "20190305120000"


def test_wayback_first_seen_and_id_replay_for_blocked_seed():
    q = quote(U, safe="")
    routes = {CDX_URL.format(url=q): (200, fx("wayback_cdx.json"), "application/json"),
              AVAILABILITY_URL.format(url=q): (200, fx("wayback_available.json"), "application/json"),
              REPLAY_URL.format(ts="20260101000000", url=U): (200, b"<html>archived</html>", "text/html")}
    ctx = F.ctx(routes, seeds=SEEDS)
    r = WaybackCollector().collect(ctx)
    assert not any("/save/" in c for c in ctx.fetcher.calls)
    assert not any("fiserv" in c or "texas" in c for c in ctx.fetcher.calls)
    rep = [c for c in r.captures if c.via_wayback]
    assert rep and rep[0].wayback_timestamp == "20260101000000"
    assert any("2019-03-05" in n for n in r.notes)
    assert r.coverage[0].status == CoverageStatus.DONE and r.coverage[0].family == SourceFamily.HIST


def test_seeds_auto_fetched_manual_are_leads():
    ctx = F.ctx({U: (200, b"<html>x</html>", "text/html")}, seeds=SEEDS)
    r = SeedsCollector().collect(ctx)
    assert ctx.fetcher.calls == [U]
    assert set(r.leads) == {"https://dir.texas.gov/x", "https://www.fiserv.com/ai"}
    cov = {c.family: c for c in r.coverage}
    assert cov[SourceFamily.IND].status == CoverageStatus.PENDING
    assert "awaiting manual capture" in cov[SourceFamily.IND].note
    assert cov[SourceFamily.PRD].status == CoverageStatus.DONE and "seeded=True" in cov[SourceFamily.PRD].note


def test_seeds_defer_ats_postings_to_jobs_collector():
    """Regression (V-003/V-005/V-006): ATS career pages are script shells; seeds on the ATS host are not fetched as
    HTML (they produced empty documents) but handed to the jobs collector."""
    seed = {"url": f"https://{WD}/TCH/job/Charlotte/Paralegal_JR1", "family": "JOB", "automation": "auto"}
    ctx = F.ctx({}, seeds={"ats": WD_ATS, "seed": [seed]})
    r = SeedsCollector().collect(ctx)
    assert ctx.fetcher.calls == [] and r.documents == []
    cov = r.coverage[0]
    assert cov.family == SourceFamily.JOB and cov.status == CoverageStatus.DONE and "jobs collector" in cov.note


def test_seeds_manual_seed_already_imported_is_done_manual():
    from footprint.models import Capture

    seeds = {"seed": [{"url": "https://dir.texas.gov/x", "family": "IND", "automation": "manual"}]}
    ctx = F.ctx({}, seeds=seeds)
    cap = Capture(capture_id="b" * 64, vendor_id="V-001", family=SourceFamily.IND, collector="manual",
                  url_requested="https://dir.texas.gov/x", status=200, manual=True, captured_by="human:RK",
                  retrieved_at="2026-10-03T10:00:00Z")
    ctx.store.manual_captures = lambda vid: [cap]
    r = SeedsCollector().collect(ctx)
    assert r.coverage[0].status == CoverageStatus.DONE_MANUAL and "1 captured manually" in r.coverage[0].note
    assert r.leads == []


def test_manual_collector_extracts_imported_captures():
    from footprint.collectors.manual import ManualCollector

    ctx = F.ctx({})
    ctx.as_of = "2026-10-05"
    html = b"<html><body><p>Fiserv agentOS uses agentic AI to run bank workflows end to end every day.</p></body></html>"
    c1 = ctx.store.put_raw(html, vendor_id="V-001", family=SourceFamily.PRD, collector="manual",
                           url_requested="https://www.fiserv.com/en/lp/agentos.html", status=200,
                           content_type="text/html", manual=True, captured_by="human:RK",
                           retrieved_at="2026-10-03T10:00:00Z")
    late = ctx.store.put_raw(b"<html><body><p>later</p></body></html>", vendor_id="V-001", family=SourceFamily.JOB,
                             collector="manual", url_requested="https://careers.fiserv.com/x", status=200,
                             content_type="text/html", manual=True, captured_by="human:AB",
                             retrieved_at="2026-10-09T10:00:00Z")
    ctx.store.manual_captures = lambda vid: [c1, late]
    r = ManualCollector().collect(ctx)
    assert ctx.fetcher.calls == []
    assert [d.url for d in r.documents] == ["https://www.fiserv.com/en/lp/agentos.html"]
    assert "agentic AI" in ctx.store.get_text(r.documents[0].doc_id)
    assert [(e.family, e.status) for e in r.coverage] == [(SourceFamily.PRD, CoverageStatus.DONE_MANUAL)]
    assert "human:RK" in r.coverage[0].note


def test_manual_collector_without_captures_emits_no_coverage_row():
    from footprint.collectors.manual import ManualCollector

    ctx = F.ctx({})
    ctx.store.manual_captures = lambda vid: []
    r = ManualCollector().collect(ctx)
    assert r.coverage == [] and r.notes


def test_wayback_on_lead_replays_only_refused_pages_from_discretionary_budget():
    """Regression (V-004 Terrapin, High tier, HIST on_lead): the site refuses the declared UA; the refused seeds are
    the lead, so their archive copies are read with the discretionary budget, without a CDX first-seen sweep."""
    from footprint.models import DepthPlan, FamilyPlan, FetchOutcome, Tier

    blocked, fine = "https://terrapintech.com/platform/", "https://pod.co/ep"
    q = quote(blocked, safe="")
    routes = {AVAILABILITY_URL.format(url=q): (200, fx("wayback_available.json"), "application/json"),
              REPLAY_URL.format(ts="20260101000000", url=blocked):
                  (200, b"<html><body><p>An AI-ready data platform.</p></body></html>", "text/html")}
    seeds = {"seed": [{"url": blocked, "family": "PRD", "automation": "auto"},
                      {"url": fine, "family": "IND", "automation": "auto"}]}
    ctx = F.ctx(routes, seeds=seeds)
    ctx.plan = DepthPlan(vendor_id="V-001", tier=Tier.HIGH, label="t", discretionary_fetches=50, gemini_calls=0,
                         analyst_minutes=0, saturation_window=6, reserved_for_meridian=[],
                         families=[FamilyPlan(family=SourceFamily.HIST, mandatory=False, mode="on_lead", cap=0,
                                              reason="t")])
    ctx.blocked[blocked] = "blocked_bot"
    assert WaybackCollector().applies(ctx.profile, seeds, ctx.plan)
    r = WaybackCollector().collect(ctx)
    assert not any("cdx" in c for c in ctx.fetcher.calls) and not any("pod.co" in c for c in ctx.fetcher.calls)
    assert [d.url for d in r.documents] == [blocked] and r.documents[0].family == SourceFamily.HIST
    assert ctx.discretionary_used == 2 and r.coverage[0].status == CoverageStatus.DONE
    assert "on_lead" in r.coverage[0].note


def test_wayback_on_lead_without_lead_is_not_applicable():
    from footprint.models import DepthPlan, FamilyPlan, Tier

    ctx = F.ctx({}, seeds={"seed": [{"url": "https://pod.co/ep", "family": "IND", "automation": "auto"}]})
    ctx.plan = DepthPlan(vendor_id="V-001", tier=Tier.HIGH, label="t", discretionary_fetches=50, gemini_calls=0,
                         analyst_minutes=0, saturation_window=6, reserved_for_meridian=[],
                         families=[FamilyPlan(family=SourceFamily.HIST, mandatory=False, mode="on_lead", cap=0,
                                              reason="t")])
    r = WaybackCollector().collect(ctx)
    assert ctx.fetcher.calls == [] and r.coverage[0].status == CoverageStatus.NOT_APPLICABLE


def test_context_fetch_dedupes_urls_and_records_blocks():
    ctx = F.ctx({"https://a.example/": (200, b"<html>a</html>", "text/html"),
                 "https://a.example/x": (403, b"no", "text/html"), "https://a.example/r": "blocked_robots"})
    first = ctx.fetch("https://a.example/", SourceFamily.PRD, "t")
    again = ctx.fetch("https://a.example/", SourceFamily.PRD, "t2")
    assert first is again and ctx.fetcher.calls == ["https://a.example/"] and ctx.budget[SourceFamily.PRD] == 1
    ctx.fetch("https://a.example/x", SourceFamily.PRD, "t")
    ctx.fetch("https://a.example/r", SourceFamily.PRD, "t")
    assert ctx.is_blocked("https://a.example/x") == "http_403"
    assert ctx.is_blocked("https://a.example/r") == "blocked_robots" and ctx.is_blocked("https://a.example/") == ""
