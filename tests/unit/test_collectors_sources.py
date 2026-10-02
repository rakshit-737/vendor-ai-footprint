"""SEC, JOB, Wayback and Seeds collectors (offline, recorded/handmade fixtures)."""

from __future__ import annotations

import datetime as dt
import importlib.util
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
    assert r.coverage[0].status == CoverageStatus.PENDING and "done_manual" in r.coverage[0].note
    assert r.leads == ["https://careers.fiserv.com/"] and not r.captures


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
    h, s = "eofe.fa.us2.oraclecloud.com", "CX_3001"
    base = f"https://{h}/hcmRestApi/resources/latest"
    lst = (f"{base}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;"
           f"siteNumber={s},keyword=%22artificial%20intelligence%22,limit=50,sortBy=RELEVANCY")
    det = f"{base}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id=%22555%22,siteNumber={s}"
    ctx = F.ctx({lst: (200, fx("orc_list.json"), "application/json"), det: (200, fx("orc_detail.json"), "application/json")},
                seeds={"ats": {"platform": "oracle_orc", "host": h, "site": "BNY-Careers", "site_number": s}})
    r = JobsCollector().collect(ctx)
    assert [d.title for d in r.documents] == ["Data Scientist"]
    assert r.documents[0].url.endswith("/sites/BNY-Careers/job/555")


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
