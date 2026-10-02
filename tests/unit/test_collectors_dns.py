from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from footprint.collectors.dns import DnsCollector, find_ai_tokens, parse_doh
from footprint.models import CoverageStatus, SourceFamily

_p = Path(__file__).resolve().parents[1] / "fixtures" / "collectors" / "_fakes.py"
_spec = importlib.util.spec_from_file_location("collector_fakes", _p)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)
fx = F.fx

G = "https://dns.google/resolve?name=automworx.com&type="
C = "https://cloudflare-dns.com/dns-query?name=automworx.com&type="


def _empty():
    return json.dumps({"Status": 0, "Answer": []}).encode()


def test_parse_doh_unquotes_cloudflare_and_matches_google():
    g = parse_doh(fx("doh_google_automworx_TXT.json"), "TXT")
    c = parse_doh(fx("doh_cloudflare_automworx_TXT.json"), "TXT")
    assert g == c and any(r.startswith("google-site-verification=") for r in g)


def test_find_ai_tokens():
    toks = find_ai_tokens(["openai-domain-verification=dv-abc", "anthropic-domain-verification-xyz=1",
                           "google-site-verification=zzz", "v=spf1 include:_spf.google.com ~all"])
    assert {p for p, _ in toks} == {"OpenAI", "Anthropic"}


def test_collect_agreeing_resolvers_emits_dns_document():
    routes = {G + "TXT": (200, fx("doh_google_automworx_TXT.json"), "application/json"),
              C + "TXT": (200, fx("doh_cloudflare_automworx_TXT.json"), "application/json")}
    for t in ("CNAME", "MX"):
        routes[G + t] = routes[C + t] = (200, _empty(), "application/json")
    ctx = F.ctx(routes, seeds={"domains": ["automworx.com"]})
    res = DnsCollector().collect(ctx)
    assert len(res.documents) == 1 and res.documents[0].kind == "dns"
    text = ctx.store.get_text(res.documents[0].doc_id)
    assert "TXT\tgoogle-site-verification=" in text
    cov = res.coverage[0]
    assert cov.status == CoverageStatus.DONE and cov.family == SourceFamily.DNS and cov.requests_used == 6
    assert "AI verification tokens: none" in cov.note


def test_disagreement_is_error():
    other = json.dumps({"Answer": [{"type": 16, "data": "openai-domain-verification=dv-1"}]}).encode()
    routes = {G + "TXT": (200, fx("doh_google_automworx_TXT.json"), "x"), C + "TXT": (200, other, "x")}
    for t in ("CNAME", "MX"):
        routes[G + t] = routes[C + t] = (200, _empty(), "x")
    res = DnsCollector().collect(F.ctx(routes, seeds={"domains": ["automworx.com"]}))
    assert res.coverage[0].status == CoverageStatus.ERROR and "disagree" in res.coverage[0].note
    assert res.coverage[0].ai_passages == 1


def test_cap_zero_still_emits_coverage():
    res = DnsCollector().collect(F.ctx({}, seeds={"domains": ["automworx.com"]}, caps={SourceFamily.DNS: 0}))
    assert res.coverage and res.coverage[0].status == CoverageStatus.STOPPED
