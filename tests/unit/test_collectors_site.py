from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from footprint.collectors.site import SiteCollector, parse_sitemap, score_url
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
