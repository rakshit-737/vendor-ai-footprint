"""Seeds collector: fetch curated ``[[seed]]`` URLs with ``automation = "auto"``; manual ones become leads.

Seeds are found during scouting; ``expect`` is a gold label only and is never used here. Each seed is fetched
under its own family's budget. Postings on the vendor's automated ATS host are deferred to the jobs collector,
which reads them through the ATS JSON API (the career pages are script shells with no text). Manual seeds (and
manual-only hosts) -> leads; a manual seed already imported with ``footprint capture import`` counts as captured
(its document comes from the manual collector). A family whose seeds are all manual is ``done_manual`` once every
one is imported, else ``pending`` with the note "awaiting manual capture".
"""

from __future__ import annotations

from footprint.collectors.base import CollectContext, is_manual_only, status_for, to_document
from footprint.collectors.jobs import is_ats_seed
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile


def _family(s: dict) -> SourceFamily:
    try:
        return SourceFamily(str(s.get("family", "PRD")).upper())
    except ValueError:
        return SourceFamily.PRD


def _manually_captured(ctx: CollectContext, url: str) -> bool:
    finder = getattr(ctx.store, "manual_captures", None)
    if not callable(finder):
        return False
    try:
        return any(c.url_requested == url or c.url_final == url for c in finder(ctx.vendor_id))
    except Exception:  # noqa: BLE001
        return False


class SeedsCollector:
    name = "seeds"
    family = SourceFamily.PRD
    seeded = True

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        return bool(seeds.get("seed"))

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        seeds = [s for s in ctx.seeds.get("seed", []) or [] if isinstance(s, dict) and s.get("url")]
        per: dict[SourceFamily, dict] = {}

        def st(f):
            return per.setdefault(f, {"used": 0, "docs": 0, "manual": 0, "manual_done": 0, "fail": [], "capped": 0,
                                      "n": 0, "ats": 0})

        for s in seeds:
            fam, u = _family(s), s["url"]
            d = st(fam)
            d["n"] += 1
            if s.get("automation", "auto") != "auto" or is_manual_only(u):
                if _manually_captured(ctx, u):
                    d["manual_done"] += 1
                else:
                    d["manual"] += 1
                    res.leads.append(u)
                continue
            if is_ats_seed(s, ctx.seeds):
                d["ats"] += 1
                continue
            out = ctx.fetch(u, fam, self.name, seeded=True)
            if out.reason == "cap_reached":
                # seeded URLs are pre-vetted: allow up to the family cap only, remainder logged
                d["capped"] += 1
                res.leads.append(u)
                continue
            d["used"] += 1
            if out.capture is not None:
                res.captures.append(out.capture)
            if not out.ok:
                d["fail"].append(out.reason)
                continue
            doc = to_document(ctx, out, fam)
            if doc:
                res.documents.append(doc)
                d["docs"] += 1
        for fam, d in per.items():
            auto = d["used"] + d["capped"] + d["ats"]
            if d["capped"]:
                status = CoverageStatus.STOPPED
            elif d["used"] and d["fail"] and len(d["fail"]) == d["used"] and not d["ats"]:
                status = status_for(d["fail"][0])
            elif auto:
                status = CoverageStatus.DONE
            elif d["manual"]:
                status = CoverageStatus.PENDING
            else:
                status = CoverageStatus.DONE_MANUAL
            note = f"seeded=True; {d['n']} seeds, {d['used']} fetched, {d['docs']} documents"
            if d["ats"]:
                note += f"; {d['ats']} ATS postings read by the jobs collector (JSON API)"
            if d["manual_done"]:
                note += f"; {d['manual_done']} captured manually"
            if d["manual"]:
                note += f"; {d['manual']} awaiting manual capture"
            if d["capped"]:
                note += f"; {d['capped']} not fetched (cap)"
            if d["fail"]:
                note += f"; failures: {', '.join(sorted(set(d['fail'])))}"
            if status == CoverageStatus.PENDING:
                note = "awaiting manual capture: " + note
            res.coverage.append(ctx.entry(fam, status, self.name, endpoint="seeds/" + ctx.vendor_id + ".toml",
                                          requests_used=d["used"], documents=d["docs"], note=note))
        if not per:
            res.coverage.append(ctx.entry(self.family, CoverageStatus.NOT_APPLICABLE, self.name, note="no seeds"))
        return res
