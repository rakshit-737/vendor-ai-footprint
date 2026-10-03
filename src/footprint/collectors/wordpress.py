"""WordPress REST sweep: /wp-json/wp/v2/{posts,pages}?search=<AI term> on vendor-owned WordPress sites.

Detection is a GET of ``/wp-json/`` (the ``Link: rel="https://api.w.org/"`` header is not kept in Capture
headers). Each matching post/page becomes its own Document whose ``url`` is the post's public link, so a
citation points at the page, while ``capture_id`` points at the API response actually retrieved.
Per-host ceilings (e.g. FSSI <= 25 requests/run) are enforced by the Fetcher via the ToS register.
"""

from __future__ import annotations

import json
from urllib.parse import quote_plus

from footprint.collectors.base import (
    AI_SEARCH_TERMS,
    CollectContext,
    is_manual_only,
    make_text_document,
    status_for,
    vendor_domains,
)
from footprint.extract import html_fragment_text
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile

FIELDS = "id,date,modified,link,title,content"
PER_PAGE = 100


def html_to_text(fragment: str) -> str:
    """Plain text of an HTML fragment with one block per paragraph, list item or line break (blank-line
    separated), so sentences of adjacent blocks never run together (``footprint.extract.html_fragment_text``)."""
    return html_fragment_text(fragment)


def html_title(fragment: str) -> str:
    """A title or other one-line field: entities decoded, tags dropped, whitespace collapsed."""
    return " ".join(html_fragment_text(fragment).split())


def _rendered(v) -> str:
    return v.get("rendered", "") if isinstance(v, dict) else str(v or "")


class WordPressCollector:
    name = "wordpress"
    family = SourceFamily.PRD

    def __init__(self, terms: tuple[str, ...] = AI_SEARCH_TERMS, kinds: tuple[str, ...] = ("posts", "pages")):
        self.terms = terms
        self.kinds = kinds

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        fp = plan.family(SourceFamily.PRD)
        return bool(fp and fp.cap > 0 and vendor_domains(profile, seeds))

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        doms = vendor_domains(ctx.profile, ctx.seeds)
        for aff in ctx.seeds.get("affiliate", []) or []:
            if isinstance(aff, dict) and aff.get("domain") and aff.get("status") in ("confirmed", "inferred"):
                doms.append(str(aff["domain"]).lower())
        if not doms:
            res.coverage.append(ctx.entry(self.family, CoverageStatus.NOT_APPLICABLE, self.name, note="no domain"))
        for d in dict.fromkeys(doms):
            self._host(ctx, d, res)
        return res

    def _base(self, ctx: CollectContext, domain: str) -> str:
        w = ctx.profile.website.strip().rstrip("/")
        if w and ctx.profile.domain == domain:
            return w if w.startswith("http") else "https://" + w
        return f"https://www.{domain}" if domain.count(".") == 1 else f"https://{domain}"

    def _host(self, ctx: CollectContext, domain: str, res: CollectorResult) -> None:
        base = self._base(ctx, domain)
        if is_manual_only(base):
            res.coverage.append(ctx.entry(self.family, CoverageStatus.BLOCKED_TOU, self.name, endpoint=base,
                                          note="manual-only host"))
            return
        used = 0
        probe = ctx.fetch(base + "/wp-json/", self.family, self.name, accept="application/json")
        used += probe.reason != "cap_reached"
        if probe.capture is not None:
            res.captures.append(probe.capture)
        is_wp = False
        if probe.ok:
            try:
                is_wp = "wp/v2" in (json.loads(ctx.raw(probe)).get("namespaces") or [])
            except (ValueError, AttributeError):
                is_wp = False
        if not is_wp:
            status = CoverageStatus.NOT_APPLICABLE if probe.ok or probe.reason == "http_error" else status_for(probe.reason)
            res.coverage.append(ctx.entry(self.family, status, self.name, endpoint=base + "/wp-json/", requests_used=used,
                                          note=f"WordPress REST API not detected ({probe.reason or 'no wp/v2 namespace'})"))
            return
        seen: set[str] = set()
        docs = 0
        stopped = False
        failures: list[str] = []
        for term in self.terms:
            for kind in self.kinds:
                url = f"{base}/wp-json/wp/v2/{kind}?per_page={PER_PAGE}&_fields={FIELDS}&search={quote_plus(term)}"
                out = ctx.fetch(url, self.family, self.name, accept="application/json")
                if out.reason == "cap_reached":
                    stopped = True
                    res.leads.append(url)
                    continue
                used += 1
                if out.capture is not None:
                    res.captures.append(out.capture)
                if not out.ok:
                    failures.append(f"{kind}/{term}:{out.reason}")
                    if out.reason in ("blocked_bot", "blocked_robots", "blocked_tou"):
                        stopped = True
                    continue
                try:
                    items = json.loads(ctx.raw(out))
                except ValueError:
                    failures.append(f"{kind}/{term}:bad_json")
                    continue
                for it in items if isinstance(items, list) else []:
                    link = str(it.get("link") or "")
                    key = link or f"{kind}:{it.get('id')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    title = html_title(_rendered(it.get("title")))
                    body = html_to_text(_rendered(it.get("content")))
                    text = f"{title}\n\n{body}\n"
                    doc = make_text_document(ctx, out.capture, text, kind="html", title=title,
                                             extractor="footprint.wordpress 1")
                    pub = str(it.get("date") or "")[:10]
                    res.documents.append(doc.model_copy(update={
                        "url": link or doc.url, "published": pub,
                        "date_basis": "wp-json date" if pub else "",
                    }))
                    docs += 1
        if stopped and failures and all(f.split(":")[-1] in ("blocked_bot",) for f in failures):
            status = CoverageStatus.BLOCKED_BOT
        elif stopped:
            status = CoverageStatus.STOPPED
        elif failures and docs == 0 and len(failures) == len(self.terms) * len(self.kinds):
            status = CoverageStatus.ERROR
        else:
            status = CoverageStatus.DONE
        note = f"terms: {', '.join(self.terms)}; {docs} unique posts/pages matched"
        if failures:
            note += f"; failures: {'; '.join(failures[:6])}"
        if stopped:
            note += "; stopped(rule: cap/host ceiling)"
        res.coverage.append(ctx.entry(self.family, status, self.name, endpoint=f"{base}/wp-json/wp/v2/{{posts,pages}}?search=",
                                      requests_used=used, documents=docs, note=note))
