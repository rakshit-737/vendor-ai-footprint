"""HIST collector: Wayback availability + CDX first-seen (read-only) and ``id_`` replay for blocked pages.

Never uses Save Page Now. Never queried for hosts whose terms bar automation (manual-only hosts) or for seeds with
``automation = "manual"``. A page counts as blocked when its seed has ``blocked = true`` / ``wayback = true``, when
this run could not read it because the host refused the client (``blocked_bot``, or a stored 401/403/429/503
answer), or when its latest stored capture is such a refusal. Pages barred by robots.txt or the terms register are
not replayed unless the seed says ``wayback = true`` (an analyst decision recorded in the seeds file).

Plan mode ``full`` (Critical; FSSI via M-D4): CDX first-seen for every auto seed, then replay of blocked ones,
within the HIST cap. Plan mode ``on_lead`` (High): only the blocked pages are the lead; no first-seen sweep, and
requests come from the vendor's discretionary budget (design 2.5: "HIST and EXEC on a lead").
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
    lead_driven = True  # in on_lead mode the blocked pages are the lead, so saturation never skips it

    def applies(self, profile: VendorProfile, seeds: dict, plan: DepthPlan) -> bool:
        fp = plan.family(SourceFamily.HIST)
        return bool(fp and (fp.cap > 0 or fp.mode == "on_lead"))

    def _blocked(self, ctx: CollectContext, seed: dict) -> str:
        """Why the page needs an archive copy ('' if it does not)."""
        if seed.get("blocked") or seed.get("wayback"):
            return "seed flag"
        why = ctx.is_blocked(seed["url"])
        if why == "blocked_bot" or why.startswith("http_"):
            return why
        try:
            cap = ctx.store.find_by_url(seed["url"])
        except Exception:  # noqa: BLE001
            cap = None
        return f"http_{cap.status}" if cap is not None and cap.status in BOT_WALL else ""

    def collect(self, ctx: CollectContext) -> CollectorResult:
        res = CollectorResult()
        fam = self.family
        on_lead = ctx.on_lead(fam) and ctx.cap(fam) <= 0
        seeds = [s for s in ctx.seeds.get("seed", []) or [] if isinstance(s, dict) and s.get("url")]
        home = ctx.profile.website.strip()
        if home and not home.startswith("http"):
            home = "https://" + home
        if home and home not in {s["url"] for s in seeds} and ctx.is_blocked(home):
            seeds.append({"url": home, "family": "PRD", "automation": "auto"})
        targets = [s for s in seeds if s.get("automation", "auto") == "auto" and not is_manual_only(s["url"])]
        skipped = len(seeds) - len(targets)
        if on_lead:
            targets = [s for s in targets if self._blocked(ctx, s)]
            if not targets:
                res.coverage.append(ctx.entry(fam, CoverageStatus.NOT_APPLICABLE, self.name,
                                              note="on_lead: no lead (no seeded or home page was refused this run)"))
                return res
        used, docs, dated, replayed = 0, 0, 0, 0
        failures: list[str] = []
        stopped = False

        def get(url: str, accept: str | None = None):
            nonlocal used, stopped
            out = ctx.fetch(url, fam, self.name, accept=accept, discretionary=on_lead)
            if out.reason == "cap_reached":
                stopped = True
                return out
            used += 1
            if out.capture is not None:
                res.captures.append(out.capture)
            return out

        for s in targets:
            u = s["url"]
            if not on_lead:
                out = get(CDX_URL.format(url=quote(u, safe="")), "application/json")
                if out.reason == "cap_reached":
                    res.leads.append(u)
                    continue
                first = parse_cdx_first(ctx.raw(out)) if out.ok else ""
                if not out.ok:
                    failures.append(f"cdx:{out.reason}")
                if first:
                    dated += 1
                    res.notes.append(f"wayback first-seen {ts_to_date(first)} for {u}")
            why = self._blocked(ctx, s)
            if not why:
                continue
            av = get(AVAILABILITY_URL.format(url=quote(u, safe="")), "application/json")
            if av.reason == "cap_reached":
                res.leads.append(u)
                continue
            ts, _ = parse_availability(ctx.raw(av)) if av.ok else ("", "")
            if not av.ok:
                failures.append(f"availability:{av.reason}")
            if not ts:
                res.notes.append(f"wayback: no snapshot for blocked page {u} ({why})")
                continue
            rp = get(REPLAY_URL.format(ts=ts, url=u))
            if rp.reason == "cap_reached":
                res.leads.append(u)
                continue
            if rp.capture is not None:
                fixed = rp.capture.model_copy(update={"via_wayback": True, "wayback_timestamp": ts})
                rp = rp.model_copy(update={"capture": fixed})
                res.captures[-1] = fixed
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
                res.notes.append(f"wayback: {u} replayed from snapshot {ts} ({why})")
        if not targets:
            status = CoverageStatus.NOT_APPLICABLE
        elif stopped:
            status = CoverageStatus.STOPPED
        elif failures and used == len(failures):
            status = status_for(failures[0].split(":", 1)[1])
        else:
            status = CoverageStatus.DONE
        if on_lead:
            note = (f"on_lead (discretionary budget): {len(targets)} refused pages looked up, {replayed} replayed via "
                    f"id_; never Save Page Now")
        else:
            note = (f"CDX first-seen for {dated}/{len(targets)} auto seeds; {replayed} blocked pages replayed via id_; "
                    f"{skipped} manual/terms-barred seeds skipped; never Save Page Now")
        if stopped:
            note += "; stopped(rule: cap)"
        if failures:
            note += f"; failures: {', '.join(sorted(set(failures)))}"
        res.coverage.append(ctx.entry(fam, status, self.name, endpoint="web.archive.org CDX + availability (read-only)",
                                      requests_used=used, documents=docs, note=note))
        return res
