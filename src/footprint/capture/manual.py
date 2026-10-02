"""Import analyst-captured files from ``evidence/manual_inbox/<VENDOR>/captures.csv``.

See docs/manual_capture_checklist.md. Rows with an empty or missing ``file`` are
recorded as skips (note ``"skipped: <reason>"``) in the report and never stored as
captures. Re-import is idempotent: a (capture_id, url) already in the index as a
manual capture is not added again.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from urllib.parse import urlsplit

from footprint.capture.store import EvidenceStore, sha256_hex
from footprint.models import Capture, SourceFamily

_CT = {".html": "text/html", ".htm": "text/html", ".pdf": "application/pdf", ".json": "application/json",
       ".txt": "text/plain", ".mhtml": "multipart/related", ".mht": "multipart/related"}


@dataclass
class SkippedRow:
    vendor_id: str
    nn: str
    url: str
    note: str  # "skipped: <reason>"


@dataclass
class ImportReport:
    captures: list[Capture] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    duplicates: int = 0


def infer_family(url: str, explicit: str = "") -> SourceFamily:
    """Optional ``family`` CSV column wins; else host heuristics; default PRD."""
    if explicit.strip():
        return SourceFamily(explicit.strip().upper())
    host = (urlsplit(url).hostname or "").lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return SourceFamily.EXEC
    if host.startswith(("careers.", "jobs.")):
        return SourceFamily.JOB
    if host in {"openai.com", "dir.texas.gov"} or host.endswith((".openai.com", ".texas.gov")):
        return SourceFamily.IND
    low = url.lower()
    if any(k in low for k in ("privacy", "terms", "legal", "sub-processor", "subprocessor", "trust")):
        return SourceFamily.LEG
    return SourceFamily.PRD


def _to_utc(local: str, tz: tzinfo | None) -> str:
    local = local.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(local, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"bad captured_at_local {local!r}")
    dt = dt.replace(tzinfo=tz) if tz is not None else dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_inbox_report(store: EvidenceStore, inbox: str | os.PathLike = "evidence/manual_inbox",
                        analyst: str | None = None, tz: tzinfo | None = None) -> ImportReport:
    """Import every ``<inbox>/<VENDOR>/captures.csv``.

    ``tz`` is the zone of ``captured_at_local`` (default: this machine's local zone).
    ``analyst`` overrides the CSV analyst column.
    """
    rep = ImportReport()
    inbox = Path(inbox)
    if not inbox.is_dir():
        return rep
    existing = {(c.capture_id, c.url_requested) for c in store.captures() if c.manual}
    for vdir in sorted(p for p in inbox.iterdir() if p.is_dir()):
        csv_path = vdir / "captures.csv"
        if not csv_path.exists():
            continue
        vendor = vdir.name
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for raw in rows:
            row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if isinstance(v, str) or v is None}
            nn, url, fname, note = row.get("nn", ""), row.get("url", ""), row.get("file", ""), row.get("note", "")
            if not fname:
                rep.skipped.append(SkippedRow(vendor, nn, url, f"skipped: {note or 'no file'}"))
                continue
            fpath = vdir / fname
            if not fpath.is_file():
                rep.skipped.append(SkippedRow(vendor, nn, url, f"skipped: file not found ({fname})"))
                continue
            data = fpath.read_bytes()
            if (sha256_hex(data), url) in existing:
                rep.duplicates += 1
                continue
            try:
                retrieved = _to_utc(row.get("captured_at_local", ""), tz)
            except ValueError as e:
                rep.skipped.append(SkippedRow(vendor, nn, url, f"skipped: {e}"))
                continue
            shot_path = ""
            shot = row.get("screenshot", "")
            if shot and (vdir / shot).is_file():
                shot_path = store.put_shot((vdir / shot).read_bytes(), (vdir / shot).suffix or ".png")
            who = analyst or row.get("analyst", "") or "unknown"
            parts = [f"nn={nn}", f"file={fname}", f"captured_at_local={row.get('captured_at_local', '')}"]
            if shot and not shot_path:
                parts.append(f"screenshot missing ({shot})")
            if note:
                parts.append(note)
            cap = store.put_raw(
                data, vendor_id=vendor, family=infer_family(url, row.get("family", "")), collector="manual",
                url_requested=url, status=200,
                content_type=_CT.get(fpath.suffix.lower(), "application/octet-stream"),
                retrieved_at=retrieved, robots_decision="manual", manual=True, captured_by=f"human:{who}",
                screenshot_path=shot_path, note="; ".join(parts),
            )
            existing.add((cap.capture_id, url))
            rep.captures.append(cap)
    return rep


def import_inbox(store: EvidenceStore, inbox: str | os.PathLike = "evidence/manual_inbox",
                 analyst: str | None = None, tz: tzinfo | None = None) -> list[Capture]:
    """Contract entry point; returns only the newly added captures."""
    return import_inbox_report(store, inbox, analyst, tz).captures
