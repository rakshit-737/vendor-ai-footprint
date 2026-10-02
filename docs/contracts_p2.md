# P2 contracts (capture, extraction, collectors)

Data models live in `src/footprint/models.py`: `Capture`, `Document`, `Passage` (`Passage.make_id`), `CollectorResult`, `FetchOutcome`, `CAPTURE_HEADER_KEYS`.

## net.fetcher
- `class Fetcher(Protocol): get(url, *, vendor_id, family: SourceFamily, collector: str, accept: str|None=None) -> FetchOutcome`
- `LiveFetcher(store, tou, robots, ratelimit, user_agent, sec_contact)`: GET only. Order: ToS register -> robots (UA token `footprint-osint`) -> rate limit -> GET. Never Wayback Save Page Now; no bot evasion. Bot wall -> `reason="blocked_bot"`. Non-2xx -> `ok=False, reason="http_error"` (body may still be stored).
- `ReplayFetcher(store)`: latest capture by `url_requested`; miss -> `FetchOutcome(ok=False, reason="replay_miss")`. No network.

## net.tou
`load_tou(path=None) -> TouRegister` (default `config/tou.toml`); `.match(host) -> entry|None`; `.check(url, run_counter) -> (allowed: bool, reason: str)`. fiserv.com and linkedin are manual-only (always disallowed for automation).

## net.robots
`RobotsCache(fetch_raw: Callable[[str], tuple[int, bytes]])`; `.allowed(url, ua="footprint-osint") -> (bool, robots_sha256)`. Protego parsing, one fetch per host per run.

## net.ratelimit
`RateLimiter().wait(host, max_rps, crawl_delay=None)` blocks until next request permitted (honours the larger of 1/max_rps and crawl_delay).

## capture.store
`EvidenceStore(root="evidence")`
- `.put_raw(data: bytes, **meta) -> Capture` (meta = Capture fields except capture_id/size/blob_path); blob `evidence/blobs/ab/<sha>.gz`; appends one JSON line (sorted keys) to `evidence/index.jsonl` (append-only).
- `.get_raw(capture_id) -> bytes`; `.find_by_url(url) -> Capture|None` (latest by retrieved_at); `.put_text(text) -> (sha, path)`; `.get_text(sha) -> str`.

## capture.manual
`import_inbox(store, inbox="evidence/manual_inbox", analyst=None) -> list[Capture]` reads `<inbox>/<vendor>/captures.csv` (see docs/manual_capture_checklist.md); sets `manual=True`, `robots_decision="manual"`, `captured_by="human:<initials>"`.

## extract
- `extract_document(capture, raw_bytes, store) -> Document`: html via trafilatura (fallback lxml text), pdf via pypdf per page (`pages` = start offsets), json pretty-printed, decode by declared charset; stores text via `store.put_text`.
- `find_passages(document_text, lexicon) -> list[tuple[int, int, list[str]]]`: hit sentence +/-1 sentence, <=700 chars, exact slice, no ellipsis.
- `load_core_lexicon(path=None)` reads `config/lexicon.toml` ([core] terms/case_sensitive, [guards], [suppressors]).

## collectors.base
- `CollectContext(profile: VendorProfile, seeds: dict, plan: DepthPlan, fetcher: Fetcher, store: EvidenceStore, budget: dict[SourceFamily,int])` (requests used per family; enforce `FamilyPlan.cap`).
- `class Collector(Protocol): name: str; family: SourceFamily; applies(profile, seeds, plan) -> bool; collect(ctx) -> CollectorResult`. Every collector emits at least one `CoverageEntry` (negative evidence included).
