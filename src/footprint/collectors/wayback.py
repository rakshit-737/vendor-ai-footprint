"""HIST collector: Wayback availability + CDX first-seen (read-only) and ``id_`` replay for blocked seeds.

Never uses Save Page Now. Never queried for hosts whose terms bar automation (manual-only hosts) or for
seeds with ``automation = "manual"``. A seed counts as blocked when it has ``blocked = true`` /
``wayback = true``, or its latest stored capture is a bot wall (status 401/403/429/503).
"""

from __future__ import annotations

import json
from urllib.parse import quote

from footprint.collectors.base import CollectContext, is_manual_only, status_for, to_document
from footprint.models import CollectorResult, CoverageStatus, DepthPlan, SourceFamily, VendorProfile

AVAILABILITY_URL = "https://archive.org/wayback/available?url={url}"
CDX_URL = "https://web.archive.org/cdx/search/cdx?url={url}&output=json&fl=timestamp,original,statuscode&filter=statuscode:200&limit=1"
REPLAY_URL = "https://web.archive.org/web/{ts}id_/{url}"
BOT_WALL = {401, 403, 429, 503}


def parse_availability(raw: bytes) -> tuple[str, str]:
    """(timestamp, archived_url) of the closest snapshot, ('', '') if none."""
    try:
        snap = (json.loads(raw).get("archived_snapshots") or {}).get("closest") or {}
    except (ValueError, AttributeError):
        return "", ""
    if not snap.get("available", True):
        return "", ""
    return str(snap.get("timestamp", "")), str(snap.get("url", ""))


def parse_cdx_first(raw: bytes) -> str:
    """Earliest 200 timestamp from CDX JSON (first row is the header)."""
    try:
        rows = json.loads(raw)
    except ValueError:
        return ""
    return str(rows[1][0]) if isinstance(rows, list) and len(rows) > 1 else ""


def ts_to_date(ts: str) -> str:
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ""


class WaybackCollector:
    name = "wayback"
    family = SourceFamily.HIST

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        fp = plan.family(SourceFamily.HIST)
        return bool(fp and fp.cap > 0)

    def _blocked(self, ctx: CollectContext, seed: dict) -> bool:
        if seed.get("blocked") or seed.get("wayback"):
            return True
        try:
            cap = ctx.store.find_by_url(seed["url"])
        except Exception:  # noqa: BLE001
            cap = None
        return bool(cap is not None and cap.status in BOT_WALL)

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        fam = self.family
        seeds = [s for s in ctx.seeds.get("seed", []) or [] if isinstance(s, dict) and s.get("url")]
        targets = [s for s in seeds if s.get("automation", "auto") == "auto" and not is_manual_only(s["url"])]
        skipped = len(seeds) - len(targets)
        used, docs, dated, replayed = 0, 0, 0, 0
        failures: list[str] = []
        stopped = False
        for s in targets:
            u = s["url"]
            out = ctx.fetch(CDX_URL.format(url=quote(u, safe="")), fam, self.name, accept="application/json")
            if out.reason == "cap_reached":
                stopped = True
                res.leads.append(u)
                continue
            used += 1
            if out.capture is not None:
                res.captures.append(out.capture)
            first = parse_cdx_first(ctx.raw(out)) if out.ok else ""
            if not out.ok:
                failures.append(f"cdx:{out.reason}")
            if first:
                dated += 1
                res.notes.append(f"wayback first-seen {ts_to_date(first)} for {u}")
            if not self._blocked(ctx, s):
                continue
            av = ctx.fetch(AVAILABILITY_URL.format(url=quote(u, safe="")), fam, self.name, accept="application/json")
            if av.reason == "cap_reached":
                stopped = True
                res.leads.append(u)
                continue
            used += 1
            if av.capture is not None:
                res.captures.append(av.capture)
            ts, _ = parse_availability(ctx.raw(av)) if av.ok else ("", "")
            if not ts:
                res.notes.append(f"wayback: no snapshot for blocked seed {u}")
                continue
            rp = ctx.fetch(REPLAY_URL.format(ts=ts, url=u), fam, self.name)
            if rp.reason == "cap_reached":
                stopped = True
                res.leads.append(u)
                continue
            used += 1
            if rp.capture is not None:
                rp = rp.model_copy(update={"capture": rp.capture.model_copy(
                    update={"via_wayback": True, "wayback_timestamp": ts})})
                res.captures.append(rp.capture)
            if not rp.ok:
                failures.append(f"replay:{rp.reason}")
                continue
            replayed += 1
            doc = to_document(ctx, rp, fam)
            if doc:
                res.documents.append(doc.model_copy(update={
                    "url": u, "published": doc.published or ts_to_date(ts),
                    "date_basis": doc.date_basis or "wayback snapshot timestamp"}))
                docs += 1
        if not targets:
            status = CoverageStatus.NOT_APPLICABLE
        elif stopped:
            status = CoverageStatus.STOPPED
        elif failures and used == len(failures):
            status = status_for(failures[0].split(":", 1)[1])
        else:
            status = CoverageStatus.DONE
        note = (f"CDX first-seen for {dated}/{len(targets)} auto seeds; {replayed} blocked seeds replayed via id_; "
                f"{skipped} manual/terms-barred seeds skipped; never Save Page Now")
        if failures:
            note += f"; failures: {', '.join(sorted(set(failures)))}"
        res.coverage.append(ctx.entry(fam, status, self.name, endpoint="web.archive.org CDX + availability (read-only)",
                                      requests_used=used, documents=docs, note=note))
        return res
