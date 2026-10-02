"""JOB collector: the vendor's own ATS (Workday, Oracle Recruiting Cloud, Workable).

Postings are filtered by AI lexicon terms, seed service/family terms and delivery-role terms; only matched
postings become Documents. ``ats.platform`` none -> not_applicable; manual -> done_manual pending (an analyst
samples by hand, e.g. Fiserv's Phenom site whose terms bar automation).
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlsplit

from lxml import etree

from footprint.collectors.base import (
    AI_SEARCH_TERMS,
    DELIVERY_ROLE_TERMS,
    CollectContext,
    ai_regex,
    is_manual_only,
    make_text_document,
    seed_terms,
    status_for,
)
from footprint.collectors.wordpress import html_to_text
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile

ORC_KEYWORDS: tuple[str, ...] = ("artificial intelligence", "machine learning", "AI")


def job_matcher(seeds: dict) -> re.Pattern[str]:
    extra = seed_terms(seeds, "service_term") + seed_terms(seeds, "family_term") + list(DELIVERY_ROLE_TERMS)
    return ai_regex(extra)


def matched_terms(pat: re.Pattern[str], text: str) -> list[str]:
    return sorted({m.group(0) for m in pat.finditer(text)}, key=str.lower)


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
            if host:
                res.leads.append(f"https://{host}/")
            res.coverage.append(ctx.entry(self.family, CoverageStatus.PENDING, self.name, endpoint=host,
                                          note=f"done_manual pending: manual sampling protocol (2.5). {ats.get('note', '')}".strip()))
            return res
        handler = {"workday": self._workday, "oracle_orc": self._orc, "workable": self._workable}.get(platform)
        if handler is None:
            res.coverage.append(ctx.entry(self.family, CoverageStatus.NOT_APPLICABLE, self.name,
                                          note=f"ATS platform '{platform}' has no automated reader; read via PRD"))
            return res
        handler(ctx, ats, job_matcher(ctx.seeds), res)
        return res

    # ------------------------------------------------------------------ helpers
    def _get(self, ctx, url, res, stats, accept=None):
        out = ctx.fetch(url, self.family, self.name, accept=accept)
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

    def _add_doc(self, ctx, out, res, stats, title, url, text, posted=""):
        doc = make_text_document(ctx, out.capture, text, kind="json", title=title, extractor="footprint.jobs 1")
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
        note = f"{listed} postings listed; {stats['docs']} matched AI/service/delivery terms"
        if stats["capped"]:
            note += f"; stopped(rule: cap), {stats['capped']} not fetched"
        if stats["fail"]:
            note += f"; failures: {', '.join(sorted(set(stats['fail'])))}"
        res.coverage.append(ctx.entry(self.family, status, self.name, endpoint=endpoint,
                                      requests_used=stats["used"], documents=stats["docs"], note=note))

    @staticmethod
    def _stats():
        return {"used": 0, "docs": 0, "capped": 0, "fail": []}

    # ------------------------------------------------------------------ workday
    def _workday(self, ctx, ats, pat, res):
        host, tenant, site = ats["host"], ats.get("tenant") or ats["host"].split(".")[0], ats["site"]
        stats = self._stats()
        sm_url = f"https://{host}/{site}/siteMap.xml"
        out = self._get(ctx, sm_url, res, stats)
        jobs: list[str] = []
        if out.ok:
            try:
                root = etree.fromstring(ctx.raw(out), parser=etree.XMLParser(recover=True, resolve_entities=False))
                jobs = [str(e.text).strip() for e in root.iter("{*}loc") if e.text and "/job/" in e.text]
            except etree.XMLSyntaxError:
                stats["fail"].append("bad_sitemap")
        # title prefilter from the URL slug (Workday slugs carry the job title)
        def slug_text(u):
            return urlsplit(u).path.split("/job/", 1)[1].replace("-", " ").replace("_", " ")
        pre_terms = re.compile("|".join([pat.pattern, r"(?i:\b(?:ai|ml|data|automation|engineer|analyst)\b)"]))
        ranked = sorted(jobs, key=lambda u: (not pat.search(slug_text(u)), not pre_terms.search(slug_text(u))))
        for u in ranked:
            if not pre_terms.search(slug_text(u)):
                continue
            path = urlsplit(u).path.split("/job/", 1)[1]
            api = f"https://{host}/wday/cxs/{tenant}/{site}/job/{path}"
            o = self._get(ctx, api, res, stats, accept="application/json")
            if not o.ok:
                continue
            try:
                info = json.loads(ctx.raw(o)).get("jobPostingInfo", {})
            except ValueError:
                continue
            title = str(info.get("title", ""))
            text = f"{title}\n\n{html_to_text(str(info.get('jobDescription', '')))}\n"
            if pat.search(text):
                self._add_doc(ctx, o, res, stats, title, str(info.get("externalUrl") or u), text,
                              str(info.get("startDate") or ""))
        self._finish(ctx, res, stats, sm_url + " + /wday/cxs job JSON", len(jobs))

    # ------------------------------------------------------------------ oracle recruiting cloud
    def _orc(self, ctx, ats, pat, res):
        host, site = ats["host"], ats.get("site_number") or ats.get("site")
        site_name = ats.get("site", site)
        stats = self._stats()
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
        for rid, r in seen.items():
            url = (f"{base}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;"
                   f"Id=%22{rid}%22,siteNumber={site}")
            o = self._get(ctx, url, res, stats, accept="application/json")
            if not o.ok:
                continue
            try:
                d = (json.loads(ctx.raw(o)).get("items") or [{}])[0]
            except ValueError:
                continue
            title = str(d.get("Title") or r.get("Title", ""))
            body = " ".join(html_to_text(str(d.get(k) or "")) for k in
                            ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
            text = f"{title}\n\n{body}\n"
            if pat.search(text):
                public = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site_name}/job/{rid}"
                self._add_doc(ctx, o, res, stats, title, public, text, str(d.get("ExternalPostedStartDate") or r.get("PostedDate") or ""))
        self._finish(ctx, res, stats, f"{base}/recruitingCEJobRequisitions keyword={'|'.join(ORC_KEYWORDS)}", len(seen))

    # ------------------------------------------------------------------ workable
    def _workable(self, ctx, ats, pat, res):
        acct = ats.get("account") or ats.get("site")
        stats = self._stats()
        url = f"https://apply.workable.com/api/v1/widget/accounts/{acct}?details=true"
        o = self._get(ctx, url, res, stats, accept="application/json")
        jobs = []
        if o.ok:
            try:
                jobs = json.loads(ctx.raw(o)).get("jobs", []) or []
            except ValueError:
                stats["fail"].append("bad_json")
        for j in jobs:
            title = str(j.get("title", ""))
            text = f"{title}\n\n{html_to_text(str(j.get('description') or ''))}\n"
            if pat.search(text):
                self._add_doc(ctx, o, res, stats, title, str(j.get("url") or j.get("shortlink") or url), text,
                              str(j.get("published_on") or ""))
        self._finish(ctx, res, stats, url, len(jobs))


__all__ = ["JobsCollector", "job_matcher", "matched_terms", "AI_SEARCH_TERMS"]
