"""JOB collector: the vendor's own ATS (Workday, Oracle Recruiting Cloud, Workable).

Detail pages are fetched for postings whose title matches the role keyword list (design 2.5: AI terms, the
vendor's service and family terms, delivery roles such as NOC, SRE, operations, engineering, data, fraud and
support), for postings the ATS's own keyword search returns for AI terms, and for every seeded posting. ATS
career pages are script-rendered shells, so seeded postings on the ATS host are read through the same JSON API
(the seeds collector defers them here). A fetched posting becomes a Document when its text names an AI term, a
service or family term or an AI specialist role, or when it was seeded; postings older than 24 months (by their
posted date, against the run's as-of date) are skipped.

``ats.platform`` none -> not_applicable. ``manual`` (or a manual-only host, e.g. Fiserv's Phenom site whose terms
bar automation) -> done_manual when analyst captures of postings are in the evidence store, else pending with the
note "awaiting manual capture" (manual sampling protocol, design 2.5).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import quote, urlsplit

from lxml import etree

from footprint.collectors.base import (
    AI_ROLE_TERMS,
    AI_SEARCH_TERMS,
    DELIVERY_ROLE_TERMS,
    CollectContext,
    ai_regex,
    host_of,
    is_manual_only,
    make_text_document,
    seed_terms,
    status_for,
)
from footprint.collectors.wordpress import html_to_text
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile

ORC_KEYWORDS: tuple[str, ...] = ("artificial intelligence", "machine learning", "AI")
AUTOMATED_PLATFORMS: tuple[str, ...] = ("workday", "oracle_orc", "workable")
MAX_POSTING_AGE_MONTHS = 24
WORKABLE_HOST = "apply.workable.com"


def job_matcher(seeds: dict) -> re.Pattern[str]:
    """Document filter: AI lexicon terms, the vendor's service/family terms and AI specialist roles."""
    extra = seed_terms(seeds, "service_term") + seed_terms(seeds, "family_term") + list(AI_ROLE_TERMS)
    return ai_regex(extra)


def title_matcher(seeds: dict) -> re.Pattern[str]:
    """Detail-fetch gate on the posting title: the role keyword list of design 2.5."""
    extra = seed_terms(seeds, "service_term") + seed_terms(seeds, "family_term") + list(DELIVERY_ROLE_TERMS)
    return ai_regex(extra)


def matched_terms(pat: re.Pattern[str], text: str) -> list[str]:
    return sorted({m.group(0) for m in pat.finditer(text)}, key=str.lower)


def ats_host(ats: dict) -> str:
    platform = str(ats.get("platform", "")).lower()
    return WORKABLE_HOST if platform == "workable" else str(ats.get("host", "")).lower()


def is_ats_seed(seed: dict, seeds: dict) -> bool:
    """A seeded URL on the vendor's automated ATS host: read through the ATS JSON API, not as an HTML page."""
    ats = seeds.get("ats") or {}
    if str(ats.get("platform", "")).lower() not in AUTOMATED_PLATFORMS:
        return False
    host = ats_host(ats)
    return bool(host) and host_of(str(seed.get("url", ""))) == host


def ats_seed_urls(seeds: dict) -> list[str]:
    return [str(s["url"]) for s in seeds.get("seed", []) or []
            if isinstance(s, dict) and s.get("url") and is_ats_seed(s, seeds)]


def months_before(iso: str, months: int) -> str:
    """ISO date ``months`` before ``iso`` (clamped to month end); '' if ``iso`` is not a date."""
    try:
        d = dt.date.fromisoformat(iso[:10])
    except ValueError:
        return ""
    import calendar

    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return dt.date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1])).isoformat()


def manual_job_captures(ctx: CollectContext) -> list:
    finder = getattr(ctx.store, "manual_captures", None)
    if not callable(finder):
        return []
    try:
        return [c for c in finder(ctx.vendor_id) if c.family == SourceFamily.JOB]
    except Exception:  # noqa: BLE001
        return []


class JobsCollector:
    name = "jobs"
    family = SourceFamily.JOB

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        fp = plan.family(SourceFamily.JOB)
        return bool(fp and fp.cap > 0)

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        ats = ctx.seeds.get("ats") or {}
        platform = str(ats.get("platform", "none")).lower()
        if platform == "none":
            res.coverage.append(ctx.entry(self.family, CoverageStatus.NOT_APPLICABLE, self.name,
                                          note=f"no ATS: {ats.get('note', 'none recorded in seeds')}"))
            return res
        host = str(ats.get("host", ""))
        if platform == "manual" or (host and is_manual_only("https://" + host)):
            self._manual(ctx, ats, host, res)
            return res
        handler = {"workday": self._workday, "oracle_orc": self._orc, "workable": self._workable}.get(platform)
        if handler is None:
            res.coverage.append(ctx.entry(self.family, CoverageStatus.NOT_APPLICABLE, self.name,
                                          note=f"ATS platform '{platform}' has no automated reader; read via PRD"))
            return res
        handler(ctx, ats, res)
        return res

    # ------------------------------------------------------------------ manual sampling
    def _manual(self, ctx: CollectContext, ats: dict, host: str, res: CollectorResult) -> None:
        caps = manual_job_captures(ctx)
        note_ats = str(ats.get("note", "")).strip()
        if caps:
            who = ", ".join(sorted({c.captured_by for c in caps if c.captured_by})) or "analyst"
            note = (f"manual sampling protocol (design 2.5): {len(caps)} posting/search captures imported by {who}; "
                    "documents come from the manual collector")
            res.coverage.append(ctx.entry(self.family, CoverageStatus.DONE_MANUAL, self.name, endpoint=host,
                                          documents=0, note=f"{note}. {note_ats}".strip()))
            return
        if host:
            res.leads.append(f"https://{host}/")
        note = ("awaiting manual capture: manual sampling protocol (design 2.5) - keyword search for AI, machine "
                "learning, generative AI and agentic; result counts and the 10 most recent matching postings")
        res.coverage.append(ctx.entry(self.family, CoverageStatus.PENDING, self.name, endpoint=host,
                                      note=f"{note}. {note_ats}".strip()))

    # ------------------------------------------------------------------ helpers
    def _get(self, ctx, url, res, stats, accept=None, seeded=False):
        out = ctx.fetch(url, self.family, self.name, accept=accept, seeded=seeded)
        if out.reason == "cap_reached":
            stats["capped"] += 1
            res.leads.append(url)
            return out
        stats["used"] += 1
        if out.capture is not None:
            res.captures.append(out.capture)
        if not out.ok:
            stats["fail"].append(out.reason)
        return out

    def _keep(self, ctx, stats, text, posted, seeded) -> bool:
        """Document filter + 24-month recency rule (seeded postings skip the term filter, not the recency rule)."""
        cutoff = months_before(ctx.as_of, MAX_POSTING_AGE_MONTHS) if ctx.as_of else ""
        if cutoff and posted and posted[:10] < cutoff:
            stats["old"] += 1
            return False
        return seeded or bool(stats["pat"].search(text))

    def _add_doc(self, ctx, out, res, stats, title, url, text, posted=""):
        doc = make_text_document(ctx, out.capture, text, kind="json", title=title, extractor="footprint.jobs 2")
        upd = {"url": url}
        if posted:
            upd.update(published=posted[:10], date_basis="ATS posted date")
        res.documents.append(doc.model_copy(update=upd))
        stats["docs"] += 1

    def _finish(self, ctx, res, stats, endpoint, listed):
        if stats["used"] == 0 and stats["fail"] == [] and stats["capped"]:
            status = CoverageStatus.STOPPED
        elif stats["fail"] and stats["used"] == len(stats["fail"]):
            status = status_for(stats["fail"][0])
        elif stats["capped"]:
            status = CoverageStatus.STOPPED
        else:
            status = CoverageStatus.DONE
        note = (f"{listed} postings listed; {stats['fetched']} detail pages read "
                f"({stats['seeded']} seeded); {stats['docs']} matched AI/service terms or were seeded")
        if stats["old"]:
            note += f"; {stats['old']} older than {MAX_POSTING_AGE_MONTHS} months skipped"
        if stats["missing"]:
            note += f"; {stats['missing']} seeded postings no longer open"
        if stats["capped"]:
            note += f"; stopped(rule: cap), {stats['capped']} not fetched"
        if stats["fail"]:
            note += f"; failures: {', '.join(sorted(set(stats['fail'])))}"
        res.coverage.append(ctx.entry(self.family, status, self.name, endpoint=endpoint,
                                      requests_used=stats["used"], documents=stats["docs"], note=note))

    @staticmethod
    def _stats(seeds: dict) -> dict:
        return {"used": 0, "docs": 0, "capped": 0, "fail": [], "fetched": 0, "seeded": 0, "old": 0, "missing": 0,
                "pat": job_matcher(seeds)}

    # ------------------------------------------------------------------ workday
    @staticmethod
    def _workday_path(url: str) -> str:
        path = urlsplit(url).path
        return path.split("/job/", 1)[1] if "/job/" in path else ""

    def _workday(self, ctx, ats, res):
        host, tenant, site = ats["host"], ats.get("tenant") or ats["host"].split(".")[0], ats["site"]
        stats = self._stats(ctx.seeds)
        titles = title_matcher(ctx.seeds)
        sm_url = f"https://{host}/{site}/siteMap.xml"
        out = self._get(ctx, sm_url, res, stats)
        jobs: list[str] = []
        if out.ok:
            try:
                root = etree.fromstring(ctx.raw(out), parser=etree.XMLParser(recover=True, resolve_entities=False,
                                                                             no_network=True))
                jobs = [str(e.text).strip() for e in root.iter("{*}loc") if e.text and "/job/" in e.text]
            except etree.XMLSyntaxError:
                stats["fail"].append("bad_sitemap")
        seeded = [u for u in ats_seed_urls(ctx.seeds) if self._workday_path(u)]

        def slug_text(u):
            return self._workday_path(u).replace("-", " ").replace("_", " ")

        listed_paths = {self._workday_path(u) for u in jobs}
        ranked = sorted((u for u in jobs if titles.search(slug_text(u))),
                        key=lambda u: (not stats["pat"].search(slug_text(u)), u))
        todo = [(u, True) for u in seeded] + [(u, False) for u in ranked]
        done: set[str] = set()
        for u, is_seed in todo:
            path = self._workday_path(u)
            if path in done:
                continue
            done.add(path)
            api = f"https://{host}/wday/cxs/{tenant}/{site}/job/{path}"
            o = self._get(ctx, api, res, stats, accept="application/json", seeded=is_seed)
            if not o.ok:
                if is_seed and path not in listed_paths and o.status == 404:
                    stats["missing"] += 1
                continue
            stats["fetched"] += 1
            stats["seeded"] += is_seed
            try:
                info = json.loads(ctx.raw(o)).get("jobPostingInfo", {})
            except ValueError:
                continue
            title = str(info.get("title", ""))
            text = f"{title}\n\n{html_to_text(str(info.get('jobDescription', '')))}\n"
            posted = str(info.get("startDate") or "")
            if self._keep(ctx, stats, text, posted, is_seed):
                self._add_doc(ctx, o, res, stats, title, str(info.get("externalUrl") or u), text, posted)
        self._finish(ctx, res, stats, sm_url + " + /wday/cxs job JSON", len(jobs))

    # ------------------------------------------------------------------ oracle recruiting cloud
    def _orc(self, ctx, ats, res):
        host, site = ats["host"], ats.get("site_number") or ats.get("site")
        site_name = ats.get("site", site)
        stats = self._stats(ctx.seeds)
        seen: dict[str, dict] = {}
        base = f"https://{host}/hcmRestApi/resources/latest"
        for kw in ORC_KEYWORDS:
            url = (f"{base}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;"
                   f"siteNumber={site},keyword=%22{quote(kw)}%22,limit=50,sortBy=RELEVANCY")
            o = self._get(ctx, url, res, stats, accept="application/json")
            if not o.ok:
                continue
            try:
                items = json.loads(ctx.raw(o)).get("items", [])
            except ValueError:
                continue
            for it in items:
                for r in it.get("requisitionList", []) or []:
                    seen.setdefault(str(r.get("Id")), r)
        seeded_ids = []
        for u in ats_seed_urls(ctx.seeds):
            m = re.search(r"/job/(\d+)", urlsplit(u).path)
            if m:
                seeded_ids.append(m.group(1))
        todo = [(rid, True) for rid in seeded_ids] + [(rid, False) for rid in seen if rid not in seeded_ids]
        for rid, is_seed in todo:
            r = seen.get(rid, {})
            url = (f"{base}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;"
                   f"Id=%22{rid}%22,siteNumber={site}")
            o = self._get(ctx, url, res, stats, accept="application/json", seeded=is_seed)
            if not o.ok:
                continue
            try:
                d = (json.loads(ctx.raw(o)).get("items") or [{}])[0]
            except ValueError:
                continue
            if not d:
                stats["missing"] += is_seed
                continue
            stats["fetched"] += 1
            stats["seeded"] += is_seed
            title = str(d.get("Title") or r.get("Title", ""))
            body = "\n\n".join(t for t in (html_to_text(str(d.get(k) or "")) for k in
                               ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
                               if t)
            text = f"{title}\n\n{body}\n"
            posted = str(d.get("ExternalPostedStartDate") or r.get("PostedDate") or "")
            if self._keep(ctx, stats, text, posted, is_seed):
                public = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site_name}/job/{rid}"
                self._add_doc(ctx, o, res, stats, title, public, text, posted)
        self._finish(ctx, res, stats, f"{base}/recruitingCEJobRequisitions keyword={'|'.join(ORC_KEYWORDS)}",
                     len(seen))

    # ------------------------------------------------------------------ workable
    def _workable(self, ctx, ats, res):
        acct = ats.get("account") or ats.get("site")
        stats = self._stats(ctx.seeds)
        url = f"https://{WORKABLE_HOST}/api/v1/widget/accounts/{acct}?details=true"
        o = self._get(ctx, url, res, stats, accept="application/json")
        jobs = []
        if o.ok:
            try:
                jobs = json.loads(ctx.raw(o)).get("jobs", []) or []
            except ValueError:
                stats["fail"].append("bad_json")
        seeded_codes = {urlsplit(u).path.rstrip("/").rsplit("/", 1)[-1].upper() for u in ats_seed_urls(ctx.seeds)}
        listed_codes = set()
        for j in jobs:
            code = str(j.get("shortcode") or "").upper()
            listed_codes.add(code)
            is_seed = code in seeded_codes
            stats["fetched"] += 1  # the widget lists full descriptions: one request reads every posting
            stats["seeded"] += is_seed
            title = str(j.get("title", ""))
            text = f"{title}\n\n{html_to_text(str(j.get('description') or ''))}\n"
            if self._keep(ctx, stats, text, str(j.get("published_on") or ""), is_seed):
                self._add_doc(ctx, o, res, stats, title, str(j.get("url") or j.get("shortlink") or url), text,
                              str(j.get("published_on") or ""))
        stats["missing"] += len(seeded_codes - listed_codes) if o.ok else 0
        self._finish(ctx, res, stats, url, len(jobs))


__all__ = ["JobsCollector", "job_matcher", "title_matcher", "matched_terms", "is_ats_seed", "ats_seed_urls",
           "AI_SEARCH_TERMS"]
