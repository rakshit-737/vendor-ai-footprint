"""SEC EDGAR collector (family REG): CIK -> submissions JSON -> EFTS full-text search (24 months) -> primary docs.

The SEC User-Agent with contact email is set by the LiveFetcher (``sec_contact``). Not a registrant ->
coverage ``not_applicable`` with the EDGAR lookup itself captured as evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import quote

from footprint.collectors.base import CollectContext, status_for, to_document
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{filename}"
FORMS: tuple[str, ...] = ("10-K", "10-Q", "8-K", "DEFA14A")
QUERIES: tuple[str, ...] = ('"artificial intelligence"', '"machine learning"', '"generative AI"')
MAX_PRIMARY_DOCS = 8

_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|company|llc|ltd|plc|holdings?|group|the)\b\.?", re.I)


def norm_name(s: str) -> str:
    s = _SUFFIX.sub(" ", s.lower().replace("&", " and "))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s).split())


def resolve_cik_from_tickers(raw: bytes, names: list[str]) -> str:
    """Exact normalised-name match only (never fuzzy: collisions are worse than a miss). '' if none."""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    wanted = {norm_name(n) for n in names if n and norm_name(n)}
    rows = data.values() if isinstance(data, dict) else data
    for row in rows:
        if norm_name(str(row.get("title", ""))) in wanted:
            return f"{int(row['cik_str']):010d}"
    return ""


def months_ago(d: dt.date, months: int) -> dt.date:
    """Same calendar day ``months`` earlier (clamped to month end)."""
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    import calendar

    return dt.date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def efts_url(query: str, cik: str, start: dt.date, end: dt.date, forms: tuple[str, ...] = FORMS) -> str:
    return (f"{EFTS_URL}?q={quote(query)}&ciks={cik}&forms={','.join(forms)}"
            f"&dateRange=custom&startdt={start.isoformat()}&enddt={end.isoformat()}")


def parse_efts(raw: bytes) -> list[dict]:
    """[{adsh, filename, form, file_date, cik}] from an EFTS response."""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out = []
    for h in (data.get("hits") or {}).get("hits", []) or []:
        src = h.get("_source", {}) or {}
        _id = str(h.get("_id", ""))
        adsh, _, filename = _id.partition(":")
        adsh = adsh or str(src.get("adsh", ""))
        ciks = src.get("ciks") or [""]
        out.append({
            "adsh": adsh, "filename": filename, "form": str(src.get("form") or (src.get("root_forms") or [""])[0]),
            "file_date": str(src.get("file_date", "")), "cik": str(ciks[0]),
        })
    return out


def primary_doc_url(hit: dict) -> str:
    return ARCHIVE_URL.format(cik=int(hit["cik"] or 0), adsh=hit["adsh"].replace("-", ""), filename=hit["filename"])


class SecCollector:
    name = "sec"
    family = SourceFamily.REG

    def __init__(self, today: dt.date | None = None, months: int = 24, queries: tuple[str, ...] = QUERIES):
        self.today = today
        self.months = months
        self.queries = queries

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        fp = plan.family(SourceFamily.REG)
        return bool(fp and fp.cap > 0)

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        fam, used = self.family, 0
        cik = str(ctx.seeds.get("sec_cik") or "").strip()
        if cik:
            cik = f"{int(cik):010d}"
        else:
            names = [ctx.profile.name, *ctx.seeds.get("legal_names", []), *ctx.seeds.get("aliases", [])]
            out = ctx.fetch(TICKERS_URL, fam, self.name, accept="application/json")
            used += 1
            if out.capture is not None:
                res.captures.append(out.capture)
            if not out.ok:
                res.coverage.append(ctx.entry(fam, status_for(out.reason), self.name, endpoint=TICKERS_URL,
                                              requests_used=used, note=f"company_tickers lookup failed: {out.reason}"))
                return res
            cik = resolve_cik_from_tickers(ctx.raw(out), names)
            if not cik:
                ev = out.capture.capture_id[:12] if out.capture else ""
                res.coverage.append(ctx.entry(
                    fam, CoverageStatus.NOT_APPLICABLE, self.name, endpoint=TICKERS_URL, requests_used=used,
                    note=(f"not an SEC registrant: no CIK in seeds and no exact match for {sorted(set(names))} "
                          f"in company_tickers.json (capture {ev})"),
                ))
                return res
        # submissions: confirms registrant + gives names/tickers
        sub_url = SUBMISSIONS_URL.format(cik=cik)
        sub = ctx.fetch(sub_url, fam, self.name, accept="application/json")
        used += sub.reason != "cap_reached"
        if sub.capture is not None:
            res.captures.append(sub.capture)
        entity = ""
        if sub.ok:
            try:
                entity = str(json.loads(ctx.raw(sub)).get("name", ""))
            except ValueError:
                pass
        today = self.today or dt.date.today()
        start = months_ago(today, self.months)
        hits: dict[str, dict] = {}
        failures: list[str] = []
        for q in self.queries:
            url = efts_url(q, cik, start, today)
            out = ctx.fetch(url, fam, self.name, accept="application/json")
            if out.reason == "cap_reached":
                res.leads.append(url)
                continue
            used += 1
            if out.capture is not None:
                res.captures.append(out.capture)
            if not out.ok:
                failures.append(f"efts {q}: {out.reason}")
                continue
            for h in parse_efts(ctx.raw(out)):
                if h["filename"]:
                    h["cik"] = h["cik"] or cik
                    hits.setdefault(primary_doc_url(h), h)
        ordered = sorted(hits.items(), key=lambda kv: kv[1]["file_date"], reverse=True)
        docs, stopped = 0, False
        for i, (url, h) in enumerate(ordered):
            if i >= MAX_PRIMARY_DOCS or ctx.remaining(fam) <= 0:
                res.leads.append(url)
                stopped = True
                continue
            out = ctx.fetch(url, fam, self.name)
            used += 1
            if out.capture is not None:
                res.captures.append(out.capture)
            if not out.ok:
                failures.append(f"{h['form']} {h['adsh']}: {out.reason}")
                continue
            doc = to_document(ctx, out, fam)
            if doc:
                if not doc.published and h["file_date"]:
                    doc = doc.model_copy(update={"published": h["file_date"][:10], "date_basis": "EDGAR file_date"})
                res.documents.append(doc)
                docs += 1
        if not sub.ok and not hits and failures:
            status = status_for(sub.reason)
        elif stopped or res.leads:
            status = CoverageStatus.STOPPED
        elif failures and not hits:
            status = CoverageStatus.ERROR
        else:
            status = CoverageStatus.DONE
        note = (f"CIK {cik} {entity}".strip() + f"; EFTS {start}..{today} forms {','.join(FORMS)}; "
                f"{len(hits)} filing docs hit, {docs} fetched")
        if res.leads:
            note += f"; {len(res.leads)} not fetched (cap)"
        if failures:
            note += "; failures: " + "; ".join(failures[:5])
        res.coverage.append(ctx.entry(fam, status, self.name, endpoint=f"EFTS {' | '.join(self.queries)} ciks={cik}",
                                      requests_used=used, documents=docs, note=note))
        return res
