"""Offline fakes for collector tests: an in-memory store and a URL->response fetcher (no network)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from footprint.models import Capture, CriticalityResult, DepthPlan, FamilyPlan, FetchOutcome, SourceFamily, Tier, VendorProfile

HERE = Path(__file__).parent


def fx(name: str) -> bytes:
    return (HERE / name).read_bytes()


class MemStore:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.texts: dict[str, str] = {}
        self.by_url: dict[str, Capture] = {}

    def put_raw(self, data: bytes, **meta) -> Capture:
        sha = hashlib.sha256(data).hexdigest()
        self.blobs[sha] = data
        meta.setdefault("retrieved_at", "2026-10-02T12:00:00Z")
        cap = Capture(capture_id=sha, size=len(data), blob_path=f"evidence/blobs/{sha[:2]}/{sha}.gz", **meta)
        self.by_url[cap.url_requested] = cap
        return cap

    def get_raw(self, capture_id: str) -> bytes:
        return self.blobs[capture_id]

    def find_by_url(self, url: str):
        return self.by_url.get(url)

    def put_text(self, text: str):
        sha = hashlib.sha256(text.encode()).hexdigest()
        self.texts[sha] = text
        return sha, f"evidence/text/{sha}.txt"

    def get_text(self, sha: str) -> str:
        return self.texts[sha]


class FakeFetcher:
    """routes: url -> (status, body bytes, content_type) or a reason string like 'blocked_robots'."""

    def __init__(self, store: MemStore, routes: dict):
        self.store, self.routes, self.calls = store, routes, []

    def get(self, url, *, vendor_id, family, collector, accept=None, seeded=False):
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            r = (404, b"not found", "text/plain")
        if isinstance(r, str):
            return FetchOutcome(ok=False, reason=r)
        status, body, ctype = r
        cap = self.store.put_raw(body, vendor_id=vendor_id, family=family, collector=collector, url_requested=url,
                                 status=status, content_type=ctype, robots_decision="allowed")
        ok = 200 <= status < 300
        return FetchOutcome(ok=ok, capture=cap, status=status, reason="" if ok else "http_error")


def profile(vid="V-001", name="AutomWorx", website="https://www.automworx.com/") -> VendorProfile:
    return VendorProfile(row=5, vendor_id=vid, name=name, website=website)


def plan(vid="V-001", caps: dict | None = None) -> DepthPlan:
    caps = caps or {f: 50 for f in SourceFamily}
    fams = [FamilyPlan(family=f, mandatory=True, mode="full", cap=c, reason="test") for f, c in caps.items()]
    return DepthPlan(vendor_id=vid, tier=Tier.HIGH, label="t", families=fams, discretionary_fetches=0,
                     gemini_calls=0, analyst_minutes=0, saturation_window=5, reserved_for_meridian=[])


def ctx(routes: dict, seeds: dict | None = None, caps: dict | None = None, prof: VendorProfile | None = None):
    from footprint.collectors.base import CollectContext
    store = MemStore()
    p = prof or profile()
    return CollectContext(profile=p, seeds=seeds or {}, plan=plan(p.vendor_id, caps), fetcher=FakeFetcher(store, routes),
                          store=store, budget={})
