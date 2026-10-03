from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from footprint.collectors.site import SiteCollector, is_legal, parse_sitemap, parse_sitemap_entries, score_url
from footprint.collectors.wordpress import WordPressCollector
from footprint.models import CoverageStatus, SourceFamily

_p = Path(__file__).resolve().parents[1] / "fixtures" / "collectors" / "_fakes.py"
_spec = importlib.util.spec_from_file_location("collector_fakes", _p)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)
fx = F.fx

B = "https://www.automworx.com"


# ------------------------------------------------------------------ site


def test_score_url_prefers_legal_and_ai():
    assert score_url(B + "/privacy-policy/") > score_url(B + "/about-us/")
    assert score_url(B + "/ai/") > 0 and score_url(B + "/hello/") == 0
    assert score_url(B + "/workload-automation-services/", ["workload automation"]) > 0


def test_training_and_shop_pages_rank_after_articles():
    """Regression (V-006): the PRD cap went on ACH course and shop pages whose slugs carry the service term."""
    tch = "https://www.theclearinghouse.org"
    course = score_url(tch + "/payments-services/education/detail/ondemand/2025-on-demand/2025-ach-rules", ["ACH"])
    article = score_url(tch + "/payment-systems/Articles/2025/11/ach-and-rtp-update", ["ACH"])
    assert article > course >= 1


def test_parse_sitemap_index():
    kids, pages = parse_sitemap(fx("site_sitemap_index.xml"))
    assert kids == [B + "/post-sitemap.xml"] and pages == []


def test_collect_routes_legal_to_LEG_and_follows_depth2():
    routes = {
        B + "/": (200, fx("site_home.html"), "text/html"),
        B + "/sitemap.xml": (200, fx("site_sitemap_index.xml"), "application/xml"),
        B + "/post-sitemap.xml": (200, fx("site_post_sitemap.xml"), "application/xml"),
        B + "/privacy-policy/": (200, b"<html>privacy</html>", "text/html"),
        B + "/ai/responsible-ai/": (200, fx("site_ai.html"), "text/html"),
        B + "/machine-learning-in-batch-scheduling/": (200, b"<html>ml</html>", "text/html"),
        B + "/trust/subprocessors/": (200, b"<html>subs</html>", "text/html"),
    }
    ctx = F.ctx(routes)
    res = SiteCollector().collect(ctx)
    calls = ctx.fetcher.calls
    assert B + "/trust/subprocessors/" in calls  # depth-2 link
    assert B + "/machine-learning-in-batch-scheduling/" in calls  # from sitemap index recursion
    assert "https://other.com/terms" not in calls and B + "/hello/" not in calls and B + "/logo.png" not in calls
    fams = {c.url_requested: c.family for c in res.captures}
    assert fams[B + "/privacy-policy/"] == SourceFamily.LEG and fams[B + "/ai/responsible-ai/"] == SourceFamily.PRD
    cov = {c.family: c for c in res.coverage}
    assert cov[SourceFamily.PRD].status == CoverageStatus.DONE and cov[SourceFamily.LEG].requests_used == 2


def test_cap_stops_and_logs_leads():
    routes = {B + "/": (200, fx("site_home.html"), "text/html")}
    res = SiteCollector().collect(F.ctx(routes, caps={SourceFamily.PRD: 2, SourceFamily.LEG: 0}))
    cov = {c.family: c for c in res.coverage}
    assert cov[SourceFamily.LEG].status == CoverageStatus.STOPPED
    assert B + "/privacy-policy/" in res.leads


def test_home_blocked_by_robots():
    res = SiteCollector().collect(F.ctx({B + "/": "blocked_robots"}))
    assert {c.family: c.status for c in res.coverage}[SourceFamily.PRD] == CoverageStatus.BLOCKED_ROBOTS


def test_is_legal_uses_whole_words_not_substrings():
    """Regression (V-005 / V-006): 'trust' matched trustee news and 'compliance'/'security' matched training
    courses, so the LEG cap went on articles and the privacy notice was never fetched."""
    bny = "https://www.bny.com/corporate/global/en"
    assert is_legal(B + "/privacy-policy/") and is_legal(B + "/trust/subprocessors/")
    assert is_legal(bny + "/privacy.html") and is_legal("https://x.com/security/") and is_legal("https://x.com/terms-of-use")
    assert is_legal("https://www.fssi-ca.com/soc-2-hitrust-csf-for-secure-print-and-mail/")
    assert not is_legal(bny + "/about-us/newsroom/company-news/bny-mellon-appointed-trustee-for-landmark.html")
    assert not is_legal(bny + "/about-us/newsroom/company-news/chief_privacy_officer_on_gdpr.html")
    tch = "https://www.theclearinghouse.org/payments-services"
    assert not is_legal(tch + "/education/ach/compliance-risk-management")
    assert not is_legal(tch + "/store/detail/dod-ach-data-security-framework")


def test_home_refused_marks_both_families_blocked_and_skips_sitemap():
    ctx = F.ctx({B + "/": "blocked_bot"})
    res = SiteCollector().collect(ctx)
    assert {c.family: c.status for c in res.coverage} == {SourceFamily.PRD: CoverageStatus.BLOCKED_BOT,
                                                          SourceFamily.LEG: CoverageStatus.BLOCKED_BOT}
    assert ctx.fetcher.calls == [B + "/"]


def test_terms_barred_site_awaits_manual_capture_without_requests():
    """fiserv.com / bny.com: no request; LEG and PRD wait for the manual sampling protocol (not a bare block)."""
    ctx = F.ctx({}, prof=F.profile("V-002", "Fiserv", "https://www.fiserv.com/"))
    res = SiteCollector().collect(ctx)
    assert ctx.fetcher.calls == []
    assert {c.family: c.status for c in res.coverage} == {SourceFamily.PRD: CoverageStatus.PENDING,
                                                          SourceFamily.LEG: CoverageStatus.PENDING}
    assert all(c.note.startswith("awaiting manual capture") and "blocked_tou" in c.note for c in res.coverage)


def test_host_ceiling_stops_the_crawl_and_refusals_cost_no_budget():
    """Regression (V-003 FSSI, 25 requests per run): once the ToS register's host ceiling answers cap_reached the
    crawl stops asking (the first run made 102 refused attempts) and logs the rest as leads."""
    routes = {B + "/": (200, fx("site_home.html"), "text/html"), B + "/sitemap.xml": "cap_reached",
              B + "/privacy-policy/": "cap_reached", B + "/ai/responsible-ai/": "cap_reached"}
    ctx = F.ctx(routes)
    res = SiteCollector().collect(ctx)
    assert ctx.fetcher.calls == [B + "/", B + "/sitemap.xml", B + "/privacy-policy/"]
    assert B + "/ai/responsible-ai/" in res.leads and ctx.budget[SourceFamily.PRD] == 1
    cov = {c.family: c for c in res.coverage}
    assert cov[SourceFamily.LEG].status == CoverageStatus.STOPPED and cov[SourceFamily.LEG].requests_used == 0


def test_sitemap_lastmod_puts_newest_pages_first():
    sm = (b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          b'<url><loc>https://www.automworx.com/ai/old/</loc><lastmod>2019-01-01</lastmod></url>'
          b'<url><loc>https://www.automworx.com/ai/new/</loc><lastmod>2026-09-01T00:00:00Z</lastmod></url>'
          b'<url><loc>https://www.automworx.com/ai/undated/</loc></url></urlset>')
    kids, entries = parse_sitemap_entries(sm)
    assert kids == [] and dict(entries)[B + "/ai/new/"] == "2026-09-01"
    routes = {B + "/": (200, b"<html><body>home</body></html>", "text/html"),
              B + "/sitemap.xml": (200, sm, "application/xml")}
    ctx = F.ctx(routes, caps={SourceFamily.PRD: 3, SourceFamily.LEG: 0})
    SiteCollector().collect(ctx)
    assert ctx.fetcher.calls == [B + "/", B + "/sitemap.xml", B + "/ai/new/"]


# ------------------------------------------------------------------ wordpress

W = B + "/wp-json/"
Q = "wp/v2/posts?per_page=100&_fields=id,date,modified,link,title,content&search="


def test_wp_not_detected_is_not_applicable():
    res = WordPressCollector().collect(F.ctx({}))
    assert res.coverage[0].status == CoverageStatus.NOT_APPLICABLE


def test_wp_sweep_creates_per_post_documents_dedup():
    routes = {W: (200, fx("wp_root.json"), "application/json"),
              W + Q + "AI": (200, fx("wp_posts_ai.json"), "application/json"),
              W + Q + "LLM": (200, fx("wp_posts_ai.json"), "application/json")}
    ctx = F.ctx(routes)
    res = WordPressCollector(terms=("AI", "LLM"), kinds=("posts",)).collect(ctx)
    assert len(res.documents) == 1
    d = res.documents[0]
    assert d.url.endswith("/what-is-ai-enablement/") and d.published == "2025-08-20"
    assert "Generative AI in Automic." in ctx.store.get_text(d.doc_id)
    assert res.coverage[0].status == CoverageStatus.DONE and res.coverage[0].requests_used == 3


def test_wp_host_cap_stops():
    routes = {W: (200, fx("wp_root.json"), "application/json")}
    res = WordPressCollector().collect(F.ctx(routes, caps={SourceFamily.PRD: 3}))
    assert res.coverage[0].status == CoverageStatus.STOPPED and res.leads


def test_wp_root_json_fixture_is_valid():
    assert "wp/v2" in json.loads(fx("wp_root.json"))["namespaces"]


def test_wp_content_keeps_paragraph_breaks():
    from footprint.collectors.wordpress import html_title, html_to_text

    assert html_to_text("<p>First AI step.</p><p>Second step.</p>") == "First AI step.\n\nSecond step."
    assert html_title("Automic &amp; <em>AI</em>") == "Automic & AI"
