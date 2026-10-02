"""Seeds collector: fetch curated ``[[seed]]`` URLs with ``automation = "auto"``; manual ones become leads.

Seeds are found during scouting; ``expect`` is a gold label only and is never used here. Each seed is fetched
under its own family's budget. Manual seeds (and manual-only hosts) -> leads + coverage note
"awaiting manual capture" (status pending until ``capture import`` brings them in).
"""

from __future__ import annotations

from footprint.collectors.base import CollectContext, is_manual_only, status_for, to_document
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile


def _family(s: dict) -> SourceFamily:
    try:
        return SourceFamily(str(s.get("family", "PRD")).upper())
    except ValueError:
        return SourceFamily.PRD


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
            return per.setdefault(f, {"used": 0, "docs": 0, "manual": 0, "fail": [], "capped": 0, "n": 0})

        for s in seeds:
            fam, u = _family(s), s["url"]
            d = st(fam)
            d["n"] += 1
            if s.get("automation", "auto") != "auto" or is_manual_only(u):
                d["manual"] += 1
                res.leads.append(u)
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
            if d["manual"] and d["used"] == 0 and not d["capped"]:
                status = CoverageStatus.PENDING
            elif d["capped"]:
                status = CoverageStatus.STOPPED
            elif d["fail"] and len(d["fail"]) == d["used"]:
                status = status_for(d["fail"][0])
            else:
                status = CoverageStatus.DONE
            note = f"seeded=True; {d['n']} seeds, {d['used']} fetched, {d['docs']} documents"
            if d["manual"]:
                note += f"; {d['manual']} awaiting manual capture"
            if d["capped"]:
                note += f"; {d['capped']} not fetched (cap)"
            if d["fail"]:
                note += f"; failures: {', '.join(sorted(set(d['fail'])))}"
            res.coverage.append(ctx.entry(fam, status, self.name, endpoint="seeds/" + ctx.vendor_id + ".toml",
                                          requests_used=d["used"], documents=d["docs"], note=note))
        if not per:
            res.coverage.append(ctx.entry(self.family, CoverageStatus.NOT_APPLICABLE, self.name, note="no seeds"))
        return res
