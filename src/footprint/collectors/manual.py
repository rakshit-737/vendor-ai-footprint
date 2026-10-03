"""Manual collector: bring analyst captures (``footprint capture import``) into the collection run.

No network. Every manual capture of the vendor in the evidence store is extracted into a Document, so manual
evidence (fiserv.com sampling, LinkedIn posts, pages behind a refusal) flows into passages like any other capture.
Each family with manual captures gets a ``done_manual`` Coverage Log row naming the analysts and the counts
(design 2.5: "Completing the checklist counts as done_manual"). A vendor without manual captures gets no row, only
a run note: this is the one collector exempt from "every collector emits a CoverageEntry", because a
not_applicable row would read as complete coverage for whatever family it named.

Captures retrieved after the run's as-of date are left out, so a replay at an earlier as-of reproduces.
"""

from __future__ import annotations

from footprint.collectors.base import CollectContext, to_document
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, FetchOutcome, SourceFamily, VendorProfile


def _manual(store, vendor_id: str, as_of: str = "") -> list:
    finder = getattr(store, "manual_captures", None)
    if not callable(finder):
        return []
    try:
        caps = list(finder(vendor_id))
    except Exception:  # noqa: BLE001
        return []
    if as_of:
        caps = [c for c in caps if c.retrieved_at[:10] <= as_of]
    return caps


class ManualCollector:
    name = "manual"
    family = SourceFamily.PRD
    seeded = True  # runs first with the seeds; never counts toward saturation

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        return True

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        caps = _manual(ctx.store, ctx.vendor_id, ctx.as_of)
        per: dict[SourceFamily, dict] = {}
        latest: dict[str, object] = {}
        for c in caps:  # the newest import of a URL wins (re-captures supersede)
            latest[c.url_requested] = c
        for c in latest.values():
            d = per.setdefault(c.family, {"n": 0, "docs": 0, "who": set(), "fail": 0})
            d["n"] += 1
            d["who"].add(c.captured_by or "human:unknown")
            res.captures.append(c)
            doc = to_document(ctx, FetchOutcome(ok=True, capture=c, status=c.status or 200), c.family)
            if doc is None:
                d["fail"] += 1
                continue
            res.documents.append(doc.model_copy(update={"url": c.url_requested}))
            d["docs"] += 1
        for fam, d in sorted(per.items(), key=lambda kv: kv[0].value):
            note = (f"{d['n']} manual captures imported by {', '.join(sorted(d['who']))}; {d['docs']} documents "
                    "extracted (no automated request)")
            if d["fail"]:
                note += f"; {d['fail']} could not be extracted"
            res.coverage.append(ctx.entry(fam, CoverageStatus.DONE_MANUAL, self.name, endpoint="evidence/index.jsonl "
                                          "(manual=true)", documents=d["docs"], note=note))
        if not per:
            res.notes.append(f"manual: no manual captures for {ctx.vendor_id} in the evidence store")
        return res
