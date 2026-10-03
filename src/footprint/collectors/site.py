"""Site collector: homepage + sitemap(s) + homepage links to depth 2, scored by URL-slug keywords.

Legal/trust pages are fetched under family LEG, everything else under PRD. robots/ToS/rate limits are enforced
by the Fetcher; this module only decides *what* to request and stays within the plan's family caps. Candidates
are ranked by slug score (legal/trust, AI, service terms, hub pages), then newest sitemap ``lastmod`` first.
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlsplit

from lxml import etree, html as lhtml

from footprint.collectors.base import (
    NOT_REQUESTED_REASONS,
    CollectContext,
    host_of,
    is_manual_only,
    seed_terms,
    status_for,
    to_document,
    vendor_domains,
)
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile

LEGAL_TOKENS: frozenset[str] = frozenset({
    "privacy", "terms", "legal", "subprocessor", "subprocessors", "dpa", "cookie", "cookies", "gdpr", "ccpa",
    "hitrust", "soc", "trustcenter",
})
"""Path words (split on / - _ .) that make a page a legal/trust page wherever they appear."""
LEGAL_SEGMENTS: frozenset[str] = frozenset({
    "trust", "trust-center", "security", "compliance", "acceptable-use", "data-processing", "sub-processors",
    "sub-processor", "data-processing-addendum", "responsible-disclosure", "security-and-privacy",
    "information-security", "data-protection",
})
"""Whole path segments that mark a legal/trust page ("/trust/" yes, "/corporate-trust-news/" no)."""
NEWS_SEGMENTS: frozenset[str] = frozenset({
    "news", "newsroom", "company-news", "blog", "blogs", "insights", "articles", "press", "press-releases",
    "events", "podcast", "podcasts", "webinars", "education", "store", "advocacy", "careers",
})
"""A page under one of these is an article, even when its slug mentions privacy or security."""
LOW_VALUE_SEGMENTS: frozenset[str] = frozenset({
    "education", "store", "events", "event", "webinars", "ondemand", "on-demand", "inperson", "in-person",
    "certificates", "forms-old", "404", "tag", "tags", "author", "search",
})
"""Sections that rarely say how the vendor itself uses AI (course catalogues, shops, listings). On 2 and 3 Oct the
TCH crawl spent its PRD cap on ACH training pages because their slugs carry the service term."""
LEGAL_SLUGS: tuple[str, ...] = tuple(sorted(LEGAL_TOKENS | LEGAL_SEGMENTS))
AI_SLUGS: tuple[str, ...] = (
    "artificial-intelligence", "machine-learning", "responsible", "genai", "generative", "ai", "ml", "llm",
    "agentic", "agents",
)
OTHER_SLUGS: tuple[str, ...] = ("careers", "newsroom", "news", "press", "product", "products", "solutions",
                                "services", "platform", "technology", "innovation", "articles", "insights", "blog")

MAX_SITEMAPS = 5
MAX_SITEMAP_URLS = 5000
SKIP_EXT = re.compile(r"\.(?:jpe?g|png|gif|svg|webp|ico|css|js|zip|mp4|mp3|woff2?|ttf|xml)$", re.I)
_PAGE_EXT = re.compile(r"\.(?:html?|aspx?|php)$")


def _tokens(url: str) -> list[str]:
    path = urlsplit(url).path.lower()
    return [t for t in re.split(r"[/\-_.]+", path) if t]


def _segments(url: str) -> list[str]:
    return [_PAGE_EXT.sub("", seg) for seg in urlsplit(url).path.lower().split("/") if seg]


def is_legal(url: str) -> bool:
    """Legal / trust page (privacy, terms, cookies, sub-processors, trust or security centre), judged on whole path
    words and segments, never substrings: "trustee" news and "data-security-framework" courses are not legal pages,
    nor is anything under a news, blog, insights or education section."""
    segs = _segments(url)
    if any(seg in NEWS_SEGMENTS for seg in segs[:-1]):
        return False
    return bool(set(_tokens(url)) & LEGAL_TOKENS) or any(seg in LEGAL_SEGMENTS for seg in segs)


def score_url(url: str, extra_terms: list[str] = ()) -> int:
    """Higher = more worth fetching. 0 = not interesting."""
    p = urlsplit(url).path.lower()
    toks = set(_tokens(url))
    score = 0
    if is_legal(url):
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
    if score and any(seg in LOW_VALUE_SEGMENTS for seg in _segments(url)):
        score = max(1, score - 6)  # training catalogues, shops, event listings: kept, but read last
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


def parse_sitemap_entries(raw: bytes) -> tuple[list[str], list[tuple[str, str]]]:
    """(child sitemap urls, [(page url, lastmod 'YYYY-MM-DD' or '')]) from a sitemap or sitemap index."""
    try:
        root = etree.fromstring(raw, parser=etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    tag = etree.QName(root).localname
    entries: list[tuple[str, str]] = []
    for node in root:
        if not isinstance(node.tag, str):
            continue
        loc = node.find("{*}loc")
        if loc is None or not loc.text:
            continue
        mod = node.find("{*}lastmod")
        entries.append((str(loc.text).strip(), str(mod.text).strip()[:10] if mod is not None and mod.text else ""))
    if not entries:  # unusual nesting: fall back to every <loc>
        entries = [(str(e.text).strip(), "") for e in root.iter("{*}loc") if e.text]
    if tag == "sitemapindex":
        return [u for u, _ in entries], []
    return [], entries


def parse_sitemap(raw: bytes) -> tuple[list[str], list[str]]:
    """(child sitemap urls, page urls) from a sitemap or sitemap index."""
    kids, entries = parse_sitemap_entries(raw)
    return kids, [u for u, _ in entries]


def _newest_first(lastmod: str) -> str:
    """Sort key that puts newer ISO dates first and undated pages last."""
    if not lastmod:
        return "~"
    return "".join(chr(ord("9") - ord(ch) + ord("0")) if ch.isdigit() else ch for ch in lastmod)


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
            if out.reason not in NOT_REQUESTED_REASONS:
                stats[fam][0] += 1
            if out.capture is not None:
                res.captures.append(out.capture)
            if not out.ok:
                problems[fam].append(out.reason or "error")
            return out

        if is_manual_only(home):
            for fam in stats:  # the manual sampling protocol (design 2.5) covers LEG and PRD for this host
                res.coverage.append(ctx.entry(
                    fam, CoverageStatus.PENDING, self.name, endpoint=home,
                    note="awaiting manual capture: manual-only host, its terms bar automation (blocked_tou); "
                         "manual sampling protocol (design 2.5)"))
            return
        seen: set[str] = {home}
        candidates: dict[str, int] = {}  # url -> depth
        lastmod: dict[str, str] = {}  # url -> sitemap lastmod (newer pages first among equal scores)
        home_out = get(home, SourceFamily.PRD)
        if home_out.ok:
            doc = to_document(ctx, home_out)
            if doc:
                res.documents.append(doc)
                stats[SourceFamily.PRD][1] += 1
            for link in parse_links(ctx.raw(home_out), home_out.capture.url_final or home if home_out.capture else home):
                if mine(link) and not SKIP_EXT.search(urlsplit(link).path):
                    candidates.setdefault(link, 1)
        # sitemaps (index recursion, capped); skipped when the host refused the homepage outright
        root = f"{urlsplit(home).scheme}://{urlsplit(home).netloc}"
        queue, fetched_maps = [root + "/sitemap.xml"], 0
        if home_out.reason in ("blocked_bot", "blocked_tou"):
            queue = []
        while queue and fetched_maps < MAX_SITEMAPS:
            sm = queue.pop(0)
            fetched_maps += 1
            out = get(sm, SourceFamily.PRD)
            if not out.ok:
                continue
            kids, pages = parse_sitemap_entries(ctx.raw(out))
            queue.extend(k for k in kids if mine(k))
            for p, mod in pages[:MAX_SITEMAP_URLS]:
                if mine(p):
                    key = urldefrag(p)[0]
                    candidates.setdefault(key, 1)
                    if mod:
                        lastmod[key] = mod
        if queue:
            res.leads.extend(queue)
            res.notes.append(f"site: {len(queue)} sitemap(s) not read (cap {MAX_SITEMAPS})")

        # breadth-first by depth, best score first, newest first within a score
        depth = 1
        host_capped = False
        while depth <= self.max_depth:
            ranked = sorted(
                ((score_url(u, extra), u) for u, d in candidates.items() if d == depth and u not in seen),
                key=lambda t: (-t[0], _newest_first(lastmod.get(t[1], "")), t[1]),
            )
            ranked = [(s, u) for s, u in ranked if s > 0]
            for _, u in ranked:
                fam = SourceFamily.LEG if is_legal(u) else SourceFamily.PRD
                if ctx.remaining(fam) <= 0 or host_capped:
                    res.leads.append(u)
                    continue
                seen.add(u)
                out = get(u, fam)
                if out.reason == "cap_reached":  # the host's per-run ceiling (ToS register): stop asking
                    host_capped = True
                    res.leads.append(u)
                    continue
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
            if not home_out.ok and (fam == SourceFamily.PRD or docs == 0):
                status = status_for(home_out.reason)  # the site could not be read: no negative evidence either
            elif capped:
                status = CoverageStatus.STOPPED
            else:
                status = CoverageStatus.DONE  # LEG with 0 fetched: searched slugs, none linked (negative evidence)
            note = f"{len(candidates)} candidate URLs; fetched {used}"
            if not home_out.ok:
                note += f"; homepage not readable ({home_out.reason or 'error'})"
            if fam == SourceFamily.LEG and used == 0:
                note += "; no legal/trust page found via homepage links or sitemap"
            if capped:
                note += "; stopped(rule: cap), remainder logged as leads"
            if probs:
                note += f"; failures: {', '.join(sorted(set(probs)))}"
            res.coverage.append(ctx.entry(fam, status, self.name, endpoint=f"{home} + sitemap + links depth<={self.max_depth}",
                                          requests_used=used, documents=docs, note=note))
