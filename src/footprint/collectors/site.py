"""Site collector: homepage + sitemap(s) + homepage links to depth 2, scored by URL-slug keywords.

Legal/trust pages are fetched under family LEG, everything else under PRD. robots/ToS/rate limits are enforced
by the Fetcher; this module only decides *what* to request and stays within the plan's family caps.
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlsplit

from lxml import etree, html as lhtml

from footprint.collectors.base import (
    CollectContext,
    host_of,
    is_manual_only,
    seed_terms,
    status_for,
    to_document,
    vendor_domains,
)
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile

LEGAL_SLUGS: tuple[str, ...] = (
    "privacy", "terms", "legal", "trust", "security", "subprocessor", "sub-processor", "dpa", "data-processing",
    "cookie", "compliance", "gdpr", "acceptable-use",
)
AI_SLUGS: tuple[str, ...] = (
    "artificial-intelligence", "machine-learning", "responsible", "genai", "generative", "ai", "ml", "llm",
)
OTHER_SLUGS: tuple[str, ...] = ("careers", "newsroom", "news", "press", "product", "solutions", "services", "platform")

MAX_SITEMAPS = 5
MAX_SITEMAP_URLS = 5000
SKIP_EXT = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|ico|css|js|zip|mp4|mp3|woff2?|ttf|xml)$", re.I)


def _tokens(url: str) -> list[str]:
    path = urlsplit(url).path.lower()
    return [t for t in re.split(r"[/\-_.]+", path) if t]


def is_legal(url: str) -> bool:
    p = urlsplit(url).path.lower()
    return any(s in p for s in LEGAL_SLUGS)


def score_url(url: str, extra_terms: list[str] = ()) -> int:
    """Higher = more worth fetching. 0 = not interesting."""
    p = urlsplit(url).path.lower()
    toks = set(_tokens(url))
    score = 0
    if any(s in p for s in LEGAL_SLUGS):
        score += 10
    for s in AI_SLUGS:
        if ("-" in s and s in p) or s in toks:
            score += 8
            break
    for t in extra_terms:
        slug = re.sub(r"\s+", "-", t.lower().strip())
        if slug and slug in p:
            score += 5
            break
    if any(s in toks for s in OTHER_SLUGS):
        score += 2
    if score and p.count("/") <= 2:
        score += 1  # prefer shallow hub pages
    return score


def parse_links(raw: bytes, base_url: str) -> list[str]:
    try:
        tree = lhtml.fromstring(raw)
    except (etree.ParserError, ValueError):
        return []
    out = []
    for href in tree.xpath("//a/@href"):
        href = str(href).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append(urldefrag(urljoin(base_url, href))[0])
    return out


def parse_sitemap(raw: bytes) -> tuple[list[str], list[str]]:
    """(child sitemap urls, page urls) from a sitemap or sitemap index."""
    try:
        root = etree.fromstring(raw, parser=etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    tag = etree.QName(root).localname
    locs = [str(e.text).strip() for e in root.iter("{*}loc") if e.text]
    return (locs, []) if tag == "sitemapindex" else ([], locs)


class SiteCollector:
    name = "site"
    family = SourceFamily.PRD

    def __init__(self, max_depth: int = 2):
        self.max_depth = max_depth

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        return bool(vendor_domains(profile, seeds)) and any(
            (fp := plan.family(f)) is not None and fp.cap > 0 for f in (SourceFamily.PRD, SourceFamily.LEG)
        )

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        domains = vendor_domains(ctx.profile, ctx.seeds)
        if not domains:
            for fam in (SourceFamily.PRD, SourceFamily.LEG):
                res.coverage.append(ctx.entry(fam, CoverageStatus.NOT_APPLICABLE, self.name, note="no website"))
            return res
        extra = seed_terms(ctx.seeds, "service_term") + seed_terms(ctx.seeds, "family_term")
        for domain in domains[:1]:
            self._site(ctx, domain, extra, res)
        return res

    def _site(self, ctx: CollectContext, domain: str, extra: list[str], res: CollectorResult) -> None:
        own = {d for d in vendor_domains(ctx.profile, ctx.seeds)}
        home = ctx.profile.website.strip() or f"https://{domain}/"
        if not home.startswith("http"):
            home = "https://" + home
        stats = {SourceFamily.PRD: [0, 0], SourceFamily.LEG: [0, 0]}  # requests, docs
        problems: dict[SourceFamily, list[str]] = {SourceFamily.PRD: [], SourceFamily.LEG: []}

        def mine(u: str) -> bool:
            h = host_of(u).removeprefix("www.")
            return any(h == d or h.endswith("." + d) for d in own) and not is_manual_only(u)

        def get(u: str, fam: SourceFamily):
            out = ctx.fetch(u, fam, self.name)
            if out.reason != "cap_reached":
                stats[fam][0] += 1
            if out.capture is not None:
                res.captures.append(out.capture)
            if not out.ok:
                problems[fam].append(out.reason or "error")
            return out

        if is_manual_only(home):
            for fam in stats:
                res.coverage.append(ctx.entry(fam, CoverageStatus.BLOCKED_TOU, self.name, endpoint=home,
                                              note="manual-only host (terms bar automation)"))
            return
        seen: set[str] = {home}
        candidates: dict[str, int] = {}  # url -> depth
        home_out = get(home, SourceFamily.PRD)
        if home_out.ok:
            doc = to_document(ctx, home_out)
            if doc:
                res.documents.append(doc)
                stats[SourceFamily.PRD][1] += 1
            for link in parse_links(ctx.raw(home_out), home_out.capture.url_final or home if home_out.capture else home):
                if mine(link) and not SKIP_EXT.search(urlsplit(link).path):
                    candidates.setdefault(link, 1)
        # sitemaps (index recursion, capped)
        root = f"{urlsplit(home).scheme}://{urlsplit(home).netloc}"
        queue, fetched_maps = [root + "/sitemap.xml"], 0
        while queue and fetched_maps < MAX_SITEMAPS:
            sm = queue.pop(0)
            fetched_maps += 1
            out = get(sm, SourceFamily.PRD)
            if not out.ok:
                continue
            kids, pages = parse_sitemap(ctx.raw(out))
            queue.extend(k for k in kids if mine(k))
            for p in pages[:MAX_SITEMAP_URLS]:
                if mine(p):
                    candidates.setdefault(urldefrag(p)[0], 1)
        if queue:
            res.leads.extend(queue)
            res.notes.append(f"site: {len(queue)} sitemap(s) not read (cap {MAX_SITEMAPS})")

        # breadth-first by depth, best score first
        depth = 1
        while depth <= self.max_depth:
            ranked = sorted(
                ((score_url(u, extra), u) for u, d in candidates.items() if d == depth and u not in seen),
                key=lambda t: (-t[0], t[1]),
            )
            ranked = [(s, u) for s, u in ranked if s > 0]
            for _, u in ranked:
                fam = SourceFamily.LEG if is_legal(u) else SourceFamily.PRD
                if ctx.remaining(fam) <= 0:
                    res.leads.append(u)
                    continue
                seen.add(u)
                out = get(u, fam)
                if not out.ok:
                    continue
                doc = to_document(ctx, out, fam)
                if doc:
                    res.documents.append(doc)
                    stats[fam][1] += 1
                if depth < self.max_depth:
                    for link in parse_links(ctx.raw(out), u):
                        if mine(link) and not SKIP_EXT.search(urlsplit(link).path):
                            candidates.setdefault(link, depth + 1)
            depth += 1

        for fam in (SourceFamily.PRD, SourceFamily.LEG):
            used, docs = stats[fam]
            probs = problems[fam]
            capped = any(l for l in res.leads if (SourceFamily.LEG if is_legal(l) else SourceFamily.PRD) == fam)
            if fam == SourceFamily.PRD and not home_out.ok:
                status = status_for(home_out.reason)
            elif capped:
                status = CoverageStatus.STOPPED
            elif used == 0 and fam == SourceFamily.LEG:
                status = CoverageStatus.DONE  # searched slugs, no legal page linked: negative evidence
            else:
                status = CoverageStatus.DONE
            note = f"{len(candidates)} candidate URLs; fetched {used}"
            if fam == SourceFamily.LEG and used == 0:
                note += "; no legal/trust page found via homepage links or sitemap"
            if capped:
                note += "; stopped(rule: cap), remainder logged as leads"
            if probs:
                note += f"; failures: {', '.join(sorted(set(probs)))}"
            res.coverage.append(ctx.entry(fam, status, self.name, endpoint=f"{home} + sitemap + links depth<={self.max_depth}",
                                          requests_used=used, documents=docs, note=note))
