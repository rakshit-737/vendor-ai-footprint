"""Gold-set evaluation: how the collected evidence and the pipeline's labels compare with the scouting gold set.

Contract: docs/contracts_p3.md §13. Gates it reports on (docs/design.md §Phases): P2 "≥90% of scouting gold URLs
captured or explained" and P3 "traps 100% rejected".

The report covers each vendor and the whole set:

* **URL recall.** Each unique gold URL is *captured* (a 2xx or manual capture, or an extracted document, carries it
  as its requested, final or Wayback-original URL), *explained* (not captured, but the run records why: a refused
  fetch, a logged lead, a failed or manual seed, an incomplete coverage entry), *missed*, or *excluded* (gold family
  SKIP; a manual-only host until the vendor has a manual capture; a DNS lookup until the vendor has a DNS capture).
* **Passage recall.** Each gold ``excerpt_reported`` is located in the captured text with rapidfuzz
  ``partial_ratio`` >= 90 after normalising case, quotes, dashes and whitespace. Elided excerpts ("[...]") are split
  and every segment must be found. The scout's reported excerpts may be summarised: they are search keys only, never
  evidence, and the report never reproduces them.
* **Strength agreement** (needs findings). Gold ``expect`` against the strength of the item at the same URL,
  preferring the item whose excerpt overlaps the gold excerpt: a confusion matrix and the agreement rate.
* **Trap checks** (needs findings for the item checks). Definition test: the lexicon's ``[suppressors]`` and
  ``[rule_suppressors]`` (a text that rests only on them) and ``[definition_test]`` (automation relabelled as AI with
  no ML evidence). Source test: ``[rule_suppressors] skip_url`` (llms.txt). Entity test: each vendor's seed
  ``collisions``, read as footprint.rules reads them (parenthetical notes removed, "CIK n" notes as other EDGAR
  filers, a collision counting only where it accounts for the vendor's name and never on the vendor's, an
  affiliate's or a platform supplier's own pages), plus provider names read out of a collision. No citable item may
  fail one of these tests, and gold trap rows must end up rejected or suppressed.
* **Gemini vs rules.** Agreement on the V8 labels over the items that carry LLM labels.

Everything is offline, read-only and deterministic (no network, no clock, sorted output). ``render_markdown`` turns
a report into a docs page.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from array import array
from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from rapidfuzz import fuzz

from footprint.capture.store import EvidenceStore
from footprint.collectors.base import is_manual_only
from footprint.extract import DEFAULT_LEXICON, Lexicon, load_core_lexicon, term_hits
from footprint.models import (
    CONTEXT_STRENGTHS,
    STRENGTH_ORDER,
    V8_LABEL_KEYS,
    AssessmentResult,
    Capture,
    CoverageEntry,
    CoverageStatus,
    Document,
    EvidenceItem,
    Passage,
    SourceFamily,
    VendorFindings,
)

DEFAULT_GOLD = Path("tests/gold/gold_v1.json")
DEFAULT_RUNS = Path("runs")
SCHEMA = "footprint.gold_report/1"
THRESHOLD = 90.0
"""rapidfuzz partial_ratio a gold excerpt segment must reach to count as located."""
MIN_SEGMENT = 12
"""Elided pieces shorter than this (normalised characters) are dropped, unless nothing longer is left."""
URL_GATE_PCT = 90.0
"""P2 gate: share of in-scope gold URLs that are captured or explained."""
EXPECTS: tuple[str, ...] = ("strong", "moderate", "weak", "marketing-only")
EXPECT_STRENGTHS: dict[str, frozenset[str]] = {
    "strong": frozenset({"Strong"}),
    "moderate": frozenset({"Moderate"}),
    "weak": frozenset({"Weak"}) | CONTEXT_STRENGTHS,
    "marketing-only": frozenset({"Marketing only"}),
}
"""Item strengths that agree with each gold ``expect`` (docs/contracts_p3.md §13)."""
TRAP_LABEL = "Trap (not AI)"
NONE_LABEL = "none"
LABEL_ORDER: tuple[str, ...] = (*STRENGTH_ORDER, TRAP_LABEL, NONE_LABEL)
REQUIRED_GOLD_KEYS: tuple[str, ...] = ("vendor_id", "url", "family", "expect", "excerpt_reported")
URL_STATUSES: tuple[str, ...] = ("captured", "explained", "missed", "excluded")
PASSAGE_STATUSES: tuple[str, ...] = ("located", "located_elsewhere", "partial", "not_located", "no_excerpt")

RULES: tuple[str, ...] = (
    "URL recall counts unique gold URLs per vendor. A URL is captured when a 2xx or manual capture, or an extracted "
    "document, has it as its requested, final or Wayback-original URL, compared after normalising (lower-case "
    "scheme and host, no fragment, no trailing slash). A DNS row also matches a DNS capture of the same domain.",
    "Explained: not captured, but the collection run records why: a refused fetch (HTTP status), a logged lead, a "
    "failed or manual seed, or a coverage entry for the family that is not done.",
    "Excluded: gold family SKIP; a manual-only host (terms bar automation) until the vendor has a manual capture; a "
    "DNS lookup until the vendor has a DNS capture.",
    "Passage recall locates each gold excerpt in the captured text with rapidfuzz partial_ratio at or above the "
    "threshold, after normalising case, quotes, dashes and whitespace. Excerpts are split at elisions ([...], ...) "
    "and every segment must be found; 'elsewhere' means another captured document of the same vendor.",
    "Reported excerpts may be summarised by the scout. They are used only as search keys, are never evidence and are "
    "not reproduced in this report.",
    "Strength agreement: strong = Strong; moderate = Moderate; weak = Weak or a Context label; marketing-only = "
    "Marketing only. Items are matched by vendor and normalised URL, preferring the item whose excerpt overlaps the "
    "gold excerpt. Rows whose item is a definition-test trap are scored by the trap check instead.",
    "Trap checks. Definition test: a text that rests only on suppressor or rule-suppressor phrases, or that labels "
    "automation as AI with no ML evidence. Source test: the lexicon's skip_url paths (llms.txt). Entity test: a seed "
    "collision that accounts for the vendor's name (no vendor name left once collisions are masked; never on the "
    "vendor's, an affiliate's or a platform supplier's pages), an EDGAR filing under a colliding CIK, or a provider "
    "name read out of a collision. A citable item that fails one of these tests is a leak.",
)

_CHAR_MAP: dict[str, str] = {
    **dict.fromkeys("\u2018\u2019\u201a\u201b\u2032\u00b4", "'"),
    **dict.fromkeys("\u201c\u201d\u201e\u201f\u2033\u00ab\u00bb", '"'),
    **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d", "-"),
    **dict.fromkeys("\u00ad\u200b\u200c\u200d\u2060\ufeff\u00ae\u2122\u00a9\u2026", " "),
}
"""One-to-one character substitutions (all non-ASCII), so offsets in the mapped text equal the original offsets."""
_CHAR_RX = re.compile("[" + "".join(_CHAR_MAP) + "]")
_TOKEN = re.compile(r"\S+")
_ELISION = re.compile(r"\[\s*(?:\.{3,}|\u2026)\s*\]|\.{3,}|\u2026")
_PROBE_WORD = re.compile(r"[a-z0-9][a-z0-9'-]{5,}")
_WAYBACK = re.compile(r"^https?://web\.archive\.org/web/\d{1,14}[a-z_]*/(?P<original>.+)$", re.IGNORECASE)
_FAILURES = re.compile(r"failures:\s*([^;]+)")
_OK_FAMILIES = frozenset(f.value for f in SourceFamily)

GoldSource = str | PathLike[str] | Sequence[Mapping[str, Any]]
FindingsSource = AssessmentResult | VendorFindings | Sequence[VendorFindings] | Sequence[EvidenceItem]


# --------------------------------------------------------------------------- gold rows


def load_gold(path: str | PathLike[str] = DEFAULT_GOLD) -> list[dict]:
    """Read and check the gold set: a JSON list of rows with vendor_id, url, family, expect and excerpt_reported.

    Raises ValueError when a row breaks the shape (missing key, empty URL, unknown ``expect``).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return check_gold(data, where=Path(path).as_posix())


def check_gold(rows: Any, *, where: str = "gold set") -> list[dict]:
    """Validate gold rows (see load_gold) and return plain copies in file order."""
    if not isinstance(rows, list):
        raise ValueError(f"{where}: the gold set must be a JSON list of rows")
    out: list[dict] = []
    for n, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise ValueError(f"{where}: row {n} is not an object")
        missing = [k for k in REQUIRED_GOLD_KEYS if not isinstance(row.get(k), str)]
        if missing:
            raise ValueError(f"{where}: row {n} lacks string field(s) {', '.join(missing)}")
        blank = [k for k in ("vendor_id", "url", "family") if not row[k].strip()]
        if blank:
            raise ValueError(f"{where}: row {n} has empty {', '.join(blank)}")
        if row["expect"] not in EXPECTS:
            raise ValueError(f"{where}: row {n} has expect {row['expect']!r}; allowed: {', '.join(EXPECTS)}")
        out.append(dict(row))
    return out


@dataclass(frozen=True)
class _Row:
    row: int
    vendor_id: str
    url: str
    key: str
    family: str
    expect: str
    excerpt: str
    segments: tuple[str, ...]


def _rows(gold: Sequence[Mapping[str, Any]]) -> list[_Row]:
    out = []
    for n, g in enumerate(gold, 1):
        family = g["family"].strip().upper()
        out.append(_Row(row=n, vendor_id=g["vendor_id"].strip(), url=g["url"].strip(), key=normalize_url(g["url"]),
                        family=family, expect=g["expect"], excerpt=g["excerpt_reported"],
                        segments=tuple(segments(g["excerpt_reported"], family=family))))
    return out


# --------------------------------------------------------------------------- normalisation


def normalize_url(url: str) -> str:
    """Matching key for a URL: lower-case scheme and host, no fragment, no trailing slash; path case and query kept."""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def _original_url(url: str) -> str:
    """The archived URL inside a Wayback replay URL, or ''."""
    m = _WAYBACK.match(url or "")
    return m["original"] if m else ""


def _dns_name(url: str) -> str:
    """The ``name`` query parameter of a DNS-over-HTTPS URL, lower-case without the trailing dot, or ''."""
    names = parse_qs(urlsplit(url or "").query).get("name") or []
    return names[0].strip().rstrip(".").lower() if names else ""


def _url_path(url: str) -> str:
    return unquote(urlsplit(url or "").path)


def _prep(text: str) -> str:
    """One-to-one character mapping (quotes, dashes, invisible marks, case), so offsets stay valid."""
    t = text if text.isascii() else _CHAR_RX.sub(lambda m: _CHAR_MAP[m.group()], text)
    low = t.lower()
    if len(low) != len(t):
        low = "".join(ch.lower() if len(ch.lower()) == 1 else ch for ch in t)
    return low


def normalize_text(text: str) -> str:
    """Text as the fuzzy matcher compares it: straight quotes, ASCII dashes, lower case, single spaces."""
    return " ".join(_TOKEN.findall(_prep(text)))


class _TokenTable:
    """Maps offsets in normalize_text(text) back to offsets in ``text`` (one entry per whitespace-free token)."""

    def __init__(self, text: str) -> None:
        self.orig_start = array("q")
        self.orig_end = array("q")
        self.norm_start = array("q")
        pos = 0
        for m in _TOKEN.finditer(_prep(text)):
            s, e = m.span()
            self.orig_start.append(s)
            self.orig_end.append(e)
            self.norm_start.append(pos)
            pos += e - s + 1

    def original(self, p: int) -> int:
        """Original offset of normalised offset ``p``; the space after a token maps to the whitespace after it."""
        i = bisect_right(self.norm_start, p) - 1
        if i < 0:
            return 0
        off = p - self.norm_start[i]
        length = self.orig_end[i] - self.orig_start[i]
        return self.orig_start[i] + off if off < length else self.orig_end[i]

    def span(self, ns: int, ne: int) -> tuple[int, int] | None:
        if not self.norm_start or ne <= ns:
            return None
        return self.original(ns), self.original(ne - 1) + 1


def segments(excerpt: str, *, family: str = "") -> list[str]:
    """Normalised search segments of a reported excerpt: split at elisions ([...], ..., …) and, for DNS rows, at ';'.

    Pieces shorter than MIN_SEGMENT are dropped unless nothing longer is left (then the longest piece is kept).
    """
    parts = _ELISION.split(excerpt or "")
    if family.upper() == "DNS":
        parts = [p for part in parts for p in part.split(";")]
    pieces = [s for s in (normalize_text(p) for p in parts) if s]
    long_pieces = [s for s in pieces if len(s) >= MIN_SEGMENT]
    if long_pieces:
        return long_pieces
    return [max(pieces, key=len)] if pieces else []


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _short(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# --------------------------------------------------------------------------- collection runs


@dataclass
class RunPack:
    """One vendor's collection run, as ``pipeline.write_run`` stores it under ``runs/<run_id>/``."""

    run_id: str
    vendor_id: str
    as_of: str = ""
    created_at: str = ""
    mode: str = ""
    captures: list[Capture] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    coverage: list[CoverageEntry] = field(default_factory=list)
    leads: list[str] = field(default_factory=list)


def _jsonl(path: Path, model: Any) -> list[Any]:
    if not path.is_file():
        return []
    return [model.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_run(run_dir: str | PathLike[str]) -> RunPack:
    """Read one collection run directory (manifest.json, captures.jsonl, passages.jsonl, coverage.jsonl)."""
    d = Path(run_dir)
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    return RunPack(
        run_id=str(manifest.get("run_id") or d.name), vendor_id=str(manifest["vendor_id"]),
        as_of=str(manifest.get("as_of", "")), created_at=str(manifest.get("created_at", "")),
        mode=str(manifest.get("mode", "")), captures=_jsonl(d / "captures.jsonl", Capture),
        documents=[Document.model_validate(x) for x in manifest.get("documents", [])],
        passages=_jsonl(d / "passages.jsonl", Passage), coverage=_jsonl(d / "coverage.jsonl", CoverageEntry),
        leads=[str(u) for u in manifest.get("leads", [])],
    )


def load_runs(runs_dir: str | PathLike[str] = DEFAULT_RUNS, *, pinned: Mapping[str, str] | None = None,
              notes: list[str] | None = None) -> dict[str, RunPack]:
    """The collection run to evaluate for each vendor.

    A run listed in ``pinned`` (vendor id -> run id, e.g. an assessment manifest's ``collection_runs``) wins when its
    directory exists; otherwise the newest run by (as_of, created_at, run_id). Directories without captures.jsonl
    (assessment runs ``A-*``) are skipped, and unreadable runs are skipped with a line in ``notes``.
    """
    root = Path(runs_dir)
    heads: dict[str, list[tuple[str, str, str, Path]]] = {}
    if root.is_dir():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (d / "manifest.json").is_file() or not (d / "captures.jsonl").is_file():
                continue
            try:
                m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                vid, run_id = str(m["vendor_id"]), str(m.get("run_id") or d.name)
                heads.setdefault(vid, []).append((str(m.get("as_of", "")), str(m.get("created_at", "")), run_id, d))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if notes is not None:
                    notes.append(f"skipped run {d.name}: unreadable manifest ({type(exc).__name__})")
    packs: dict[str, RunPack] = {}
    for vid in sorted(heads):
        candidates = sorted(heads[vid])
        want = (pinned or {}).get(vid)
        chosen = next((c for c in candidates if want and c[2] == want), candidates[-1])
        if want and chosen[2] != want and notes is not None:
            notes.append(f"{vid}: pinned run {want} not found; using {chosen[2]}")
        try:
            packs[vid] = load_run(chosen[3])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if notes is not None:
                notes.append(f"skipped run {chosen[3].name}: unreadable ({type(exc).__name__})")
    return packs


def _load_seeds(seeds_dir: str | PathLike[str] | None, vendor_id: str, notes: list[str]) -> dict:
    if seeds_dir is None:
        return {}
    p = Path(seeds_dir) / f"{vendor_id}.toml"
    if not p.is_file():
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        notes.append(f"{vendor_id}: seeds file unreadable ({type(exc).__name__})")
        return {}


class _Texts:
    """Document text from the evidence store, normalised and cached per doc_id."""

    def __init__(self, store: EvidenceStore | None) -> None:
        self.store = store
        self._norm: dict[str, str] = {}
        self._missing: set[str] = set()
        self._tables: dict[str, _TokenTable] = {}

    def raw(self, doc: Document) -> str | None:
        if self.store is not None:
            try:
                return self.store.get_text(doc.doc_id)
            except (KeyError, OSError, UnicodeDecodeError):
                pass
        p = Path(doc.text_path) if doc.text_path else None
        if p is not None and p.is_file():
            try:
                return p.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return None
        return None

    def norm(self, doc: Document) -> str:
        if doc.doc_id not in self._norm:
            raw = self.raw(doc)
            if raw is None:
                self._missing.add(doc.doc_id)
            self._norm[doc.doc_id] = normalize_text(raw) if raw else ""
        return self._norm[doc.doc_id]

    def missing(self, doc: Document) -> bool:
        """True when the document's text is in neither the store nor its text_path."""
        self.norm(doc)
        return doc.doc_id in self._missing

    def offsets(self, doc: Document, ns: int, ne: int) -> tuple[int, int] | None:
        """Original-text offsets of the normalised span [ns, ne)."""
        if doc.doc_id not in self._tables:
            raw = self.raw(doc)
            if raw is None:
                return None
            self._tables[doc.doc_id] = _TokenTable(raw)
        return self._tables[doc.doc_id].span(ns, ne)


def _ok(c: Capture) -> bool:
    return c.manual or 200 <= c.status < 300


@dataclass
class _Vendor:
    """Everything the evaluation needs about one vendor's collection."""

    vendor_id: str
    pack: RunPack | None
    captures: dict[str, list[Capture]] = field(default_factory=dict)
    dns: dict[str, list[Capture]] = field(default_factory=dict)
    docs_by_key: dict[str, list[Document]] = field(default_factory=dict)
    docs_by_capture: dict[str, list[Document]] = field(default_factory=dict)
    documents: list[Document] = field(default_factory=list)
    passages: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    coverage: list[CoverageEntry] = field(default_factory=list)
    leads: set[str] = field(default_factory=set)
    seeds: dict[str, dict] = field(default_factory=dict)
    has_manual: bool = False
    has_dns: bool = False


def _vendor(vendor_id: str, pack: RunPack | None, extra: Sequence[Capture], seeds: Mapping[str, Any]) -> _Vendor:
    v = _Vendor(vendor_id=vendor_id, pack=pack)
    caps = [*(pack.captures if pack else []), *(c for c in extra if c.vendor_id == vendor_id)]
    for c in caps:
        keys = {normalize_url(u) for u in (c.url_requested, c.url_final, _original_url(c.url_requested),
                                            _original_url(c.url_final)) if u}
        for k in sorted(keys):
            v.captures.setdefault(k, []).append(c)
        if c.family == SourceFamily.DNS:
            name = _dns_name(c.url_requested)
            if name:
                v.dns.setdefault(name, []).append(c)
            v.has_dns = v.has_dns or _ok(c)
        v.has_manual = v.has_manual or c.manual
    seen: set[str] = set()
    for d in pack.documents if pack else []:
        if d.doc_id in seen:
            continue
        seen.add(d.doc_id)
        v.documents.append(d)
        v.docs_by_key.setdefault(normalize_url(d.url), []).append(d)
        v.docs_by_capture.setdefault(d.capture_id, []).append(d)
    for p in pack.passages if pack else []:
        v.passages.setdefault(p.doc_id, []).append((p.start, p.end))
    v.coverage = list(pack.coverage) if pack else []
    v.leads = {normalize_url(u) for u in (pack.leads if pack else [])}
    for s in seeds.get("seed", []) or []:
        if isinstance(s, Mapping) and s.get("url"):
            v.seeds.setdefault(normalize_url(str(s["url"])), dict(s))
    return v


# --------------------------------------------------------------------------- URL recall


def _row_captures(r: _Row, v: _Vendor) -> tuple[list[Capture], list[Document], str]:
    """Captures and documents of a gold URL, and the match basis ('url', 'dns domain' or '')."""
    caps = list({c.capture_id + c.url_requested: c for c in v.captures.get(r.key, [])}.values())
    docs = list(v.docs_by_key.get(r.key, []))
    basis = "url" if caps or docs else ""
    if not basis and r.family == "DNS" and _dns_name(r.url):
        caps = list(v.dns.get(_dns_name(r.url), []))
        basis = "dns domain" if caps else ""
    for c in caps:
        if _ok(c):
            docs.extend(d for d in v.docs_by_capture.get(c.capture_id, []) if d not in docs)
    return caps, docs, basis


def _seed_failures(v: _Vendor, family: str) -> list[str]:
    found: set[str] = set()
    for e in v.coverage:
        if e.collector == "seeds" and e.family.value == family:
            for m in _FAILURES.finditer(e.note):
                found.update(x.strip() for x in m.group(1).split(",") if x.strip())
    return sorted(found)


def _explain(r: _Row, v: _Vendor, caps: Sequence[Capture]) -> str:
    reasons: list[str] = []
    refused = sorted({c.status for c in caps if not _ok(c)})
    if refused:
        reasons.append("fetched but refused (HTTP " + ", ".join(str(s) for s in refused) + ")")
    if r.key in v.leads:
        reasons.append("logged as a lead, not fetched")
    family = r.family
    seed = v.seeds.get(r.key)
    if seed is not None:
        family = str(seed.get("family") or family).upper()
        if str(seed.get("automation", "auto")) != "auto":
            reasons.append("seeded for manual capture, not yet imported")
        elif not refused:
            failures = _seed_failures(v, family)
            if failures:
                reasons.append("seed not captured; the seeds collector reported " + ", ".join(failures))
    if family in _OK_FAMILIES and v.pack is not None:
        entries = [e for e in v.coverage if e.family.value == family]
        open_entries = [e for e in entries if e.status not in (CoverageStatus.DONE, CoverageStatus.DONE_MANUAL)]
        if not entries:
            reasons.append(f"no {family} coverage entry (family not planned)")
        elif open_entries and reasons:  # a specific cause is known: summarise the coverage
            states = dict.fromkeys(f"{e.collector or 'coverage'} {e.status.value}" for e in open_entries)
            reasons.append(f"{family} coverage: " + ", ".join(states))
        elif open_entries:
            states = dict.fromkeys(f"{family} {e.collector or 'coverage'}: {e.status.value}"
                                   + (f" ({_short(e.note, 80)})" if e.note else "") for e in open_entries)
            reasons.extend(states)
    return "; ".join(reasons)


def _url_outcome(r: _Row, v: _Vendor, manual_only: bool) -> dict[str, Any]:
    caps, docs, basis = _row_captures(r, v)
    out: dict[str, Any] = {"vendor_id": r.vendor_id, "url": r.url, "family": r.family, "rows": [r.row],
                           "manual_only": manual_only, "capture_ids": [], "basis": basis}
    if r.family == "SKIP":
        return {**out, "status": "excluded", "reason": "gold family SKIP (outside the collection plan)"}
    ok_caps = [c for c in caps if _ok(c)]
    if ok_caps or docs:
        ids = sorted({c.capture_id for c in ok_caps} | {d.capture_id for d in docs})
        how = "manual capture" if any(c.manual for c in ok_caps) else f"matched by {basis}"
        reason = how if docs else f"{how}; no extracted document"
        return {**out, "status": "captured", "capture_ids": ids, "reason": reason}
    if manual_only and not v.has_manual:
        return {**out, "status": "excluded",
                "reason": "manual-only host (terms bar automation); no manual capture for this vendor yet"}
    if r.family == "DNS" and not v.has_dns:
        return {**out, "status": "excluded", "reason": "DNS lookup; no DNS capture for this vendor"}
    if v.pack is None and not caps:
        return {**out, "status": "missed", "reason": "no collection run for this vendor"}
    reason = _explain(r, v, caps)
    if reason:
        return {**out, "status": "explained", "reason": reason}
    return {**out, "status": "missed", "reason": "not captured, and the coverage log does not explain it"}


def _manual_only(url: str, tou: Any) -> bool:
    if is_manual_only(url):
        return True
    if tou is None:
        return False
    try:
        return tou.entry_for(url).automation == "none"
    except Exception:  # noqa: BLE001 - a broken register must not sink the evaluation
        return False


def _count(rows: Sequence[Mapping[str, Any]], key: str, values: Sequence[str]) -> dict[str, int]:
    c = Counter(r[key] for r in rows)
    return {k: c.get(k, 0) for k in values}


def _url_summary(urls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = _count(urls, "status", URL_STATUSES)
    in_scope = n["captured"] + n["explained"] + n["missed"]
    return {"urls": len(urls), "in_scope": in_scope, **n, "captured_pct": _pct(n["captured"], in_scope),
            "captured_or_explained_pct": _pct(n["captured"] + n["explained"], in_scope)}


# --------------------------------------------------------------------------- passage recall


@dataclass(frozen=True)
class _Hit:
    score: float
    doc: Document
    ns: int
    ne: int


def _locate(seg: str, docs: Sequence[Document], texts: _Texts, cutoff: float = 0.0) -> _Hit | None:
    """Best alignment (score >= ``cutoff``) of a normalised segment in the documents, earlier documents winning
    ties: an exact substring first, then rapidfuzz partial_ratio. A document shorter than the segment must match as
    a whole (ratio), so a short text never "contains" a long segment."""
    best: _Hit | None = None
    for doc in docs:
        hay = texts.norm(doc)
        if not hay:
            continue
        i = hay.find(seg)
        if i >= 0:
            return _Hit(100.0, doc, i, i + len(seg))
        floor = max(cutoff, best.score if best else 0.0)
        if len(hay) >= len(seg):
            res = fuzz.partial_ratio_alignment(seg, hay, score_cutoff=floor)
            if res is None:
                continue
            hit = _Hit(float(res.score), doc, res.dest_start, res.dest_end)
        else:
            score = float(fuzz.ratio(seg, hay, score_cutoff=floor))
            if not score:
                continue
            hit = _Hit(score, doc, 0, len(hay))
        if best is None or hit.score > best.score:
            best = hit
    return best


def _candidates(seg: str, docs: Sequence[Document], texts: _Texts) -> list[Document]:
    """Documents worth a fuzzy search: those holding at least half of the segment's longest words."""
    probes = sorted(set(_PROBE_WORD.findall(seg)), key=lambda w: (-len(w), w))[:4]
    if len(probes) < 2:
        return list(docs)
    need = (len(probes) + 1) // 2
    return [d for d in docs if sum(w in texts.norm(d) for w in probes) >= need]


def _in_passage(place: Mapping[str, Any], v: _Vendor) -> bool:
    """Does a located span overlap a lexicon passage of its document (passages.jsonl)?"""
    start, end = place["start"], place["end"]
    if start is None or end is None:
        return False
    return any(ps < end and start < pe for ps, pe in v.passages.get(place["doc_id"], ()))


def _passage_outcome(r: _Row, v: _Vendor, url_row: Mapping[str, Any], texts: _Texts,
                     threshold: float) -> dict[str, Any]:
    out: dict[str, Any] = {"row": r.row, "vendor_id": r.vendor_id, "url": r.url, "family": r.family,
                           "expect": r.expect, "scope": "excluded" if url_row["status"] == "excluded" else "in",
                           "segments": len(r.segments), "segments_located": 0, "score": None, "located_in": [],
                           "in_passage": None}
    if not r.segments:
        return {**out, "status": "no_excerpt", "reason": "the gold row has no excerpt"}
    _, own, _ = _row_captures(r, v)
    own_ids = {d.doc_id for d in own}
    others = [d for d in v.documents if d.doc_id not in own_ids]
    hits: list[tuple[_Hit | None, str]] = []
    for seg in r.segments:
        best = _locate(seg, own, texts)
        where = "url"
        if best is None or best.score < threshold:
            alt = _locate(seg, _candidates(seg, others, texts), texts, cutoff=threshold)
            if alt is not None:
                best, where = alt, "elsewhere"
        hits.append((best, where))
    located = [(h, w) for h, w in hits if h is not None and h.score >= threshold]
    has_text = any(texts.norm(d) for d in own)
    if has_text or located:  # the weakest segment decides; no score when there was no text to compare with
        out["score"] = round(min(h.score if h is not None else 0.0 for h, _ in hits), 1)
    out["segments_located"] = len(located)
    places: dict[tuple[str, int, int], dict[str, Any]] = {}
    for h, w in located:
        span = texts.offsets(h.doc, h.ns, h.ne)
        start, end = span if span else (None, None)
        places.setdefault((h.doc.doc_id, start or 0, end or 0), {
            "doc_id": h.doc.doc_id, "url": h.doc.url, "where": w, "start": start, "end": end,
            "score": round(h.score, 1),
        })
    out["located_in"] = sorted(places.values(), key=lambda p: (p["where"] != "url", p["doc_id"], p["start"] or 0))
    if r.family != "DNS" and located:  # DNS records are tagged per token, not through lexicon passages
        out["in_passage"] = any(_in_passage(p, v) for p in out["located_in"])
    if len(located) == len(hits):
        if all(w == "url" for _, w in located):
            return {**out, "status": "located", "reason": ""}
        return {**out, "status": "located_elsewhere", "reason": "found in another captured document of the vendor"}
    if located:
        return {**out, "status": "partial", "reason": f"{len(located)} of {len(hits)} segments found (the reported "
                                                       "excerpt may be summarised)"}
    if has_text:
        reason = (f"not found in the captured text (best score {out['score']}); the reported excerpt may be "
                  "summarised, or the page changed")
    elif own and all(texts.missing(d) for d in own):
        reason = "the document text is missing from the evidence store"
    elif own:
        reason = "the captured document has no text (the page may be rendered by script)"
    elif url_row["status"] == "captured":
        reason = "the URL was captured, but no text was extracted for it"
    else:
        reason = f"URL not captured ({url_row['status']}); not found elsewhere"
    return {**out, "status": "not_located", "reason": reason}


def _passage_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scoped = [r for r in rows if r["scope"] == "in"]
    n = _count(scoped, "status", PASSAGE_STATUSES)
    found = n["located"] + n["located_elsewhere"]
    checked = [r for r in scoped if r["in_passage"] is not None]
    return {"rows": len(rows), "in_scope": len(scoped), **n, "recall": _pct(found, len(scoped)),
            "recall_at_url": _pct(n["located"], len(scoped)),
            "in_passage": sum(1 for r in checked if r["in_passage"]), "in_passage_of": len(checked)}


# --------------------------------------------------------------------------- items: strength, traps, LLM


def load_findings(path: str | PathLike[str]) -> AssessmentResult | VendorFindings | list[EvidenceItem]:
    """Findings saved by an assessment run, for ``gold_report``.

    ``path`` is an assessment run directory (``runs/A-*/``: its ``assessment.json``, else its ``evidence.jsonl``), an
    ``assessment.json`` or VendorFindings JSON file, or an ``evidence.jsonl`` file of EvidenceItems.
    Raises FileNotFoundError when there is nothing to read, ValueError when the file breaks the models.
    """
    p = Path(path)
    if p.is_dir():
        for name in ("assessment.json", "evidence.jsonl"):
            if (p / name).is_file():
                return load_findings(p / name)
        raise FileNotFoundError(f"{p.as_posix()}: no assessment.json or evidence.jsonl")
    if not p.is_file():
        raise FileNotFoundError(p.as_posix())
    text = p.read_bytes().decode("utf-8")
    if p.suffix.lower() == ".jsonl":
        return [EvidenceItem.model_validate_json(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, list):
        return [EvidenceItem.model_validate(x) for x in data]
    if isinstance(data, Mapping) and "vendors" in data:
        return AssessmentResult.model_validate(data)
    return VendorFindings.model_validate(data)


def _findings(findings: FindingsSource | None) -> tuple[dict[str, list[EvidenceItem]] | None, dict[str, str]]:
    """Items per vendor and pinned collection runs from whatever findings form was passed."""
    if findings is None:
        return None, {}
    pinned: dict[str, str] = {}
    if isinstance(findings, AssessmentResult):
        runs = findings.manifest.get("collection_runs") if isinstance(findings.manifest, Mapping) else None
        if isinstance(runs, Mapping):
            pinned = {str(k): str(val) for k, val in runs.items()}
        seq: Sequence[Any] = findings.vendors
    elif isinstance(findings, VendorFindings | EvidenceItem):
        seq = [findings]
    else:
        seq = list(findings)
    items: dict[str, list[EvidenceItem]] = {}
    for f in seq:
        if isinstance(f, VendorFindings):
            items.setdefault(f.vendor_id, []).extend(f.evidence)
        elif isinstance(f, EvidenceItem):
            items.setdefault(f.vendor_id, []).append(f)
        else:
            raise TypeError(f"findings must be AssessmentResult, VendorFindings or EvidenceItem, "
                            f"not {type(f).__name__}")
    return items, pinned


class _Items:
    """One vendor's items, indexed by URL key and DNS domain, with normalised excerpts."""

    def __init__(self, items: Sequence[EvidenceItem]) -> None:
        self.by_key: dict[str, list[EvidenceItem]] = {}
        self.by_dns: dict[str, list[EvidenceItem]] = {}
        self.norm: dict[str, str] = {}
        for it in items:
            urls = (it.url, it.url_final, _original_url(it.url), _original_url(it.url_final))
            for k in sorted({normalize_url(u) for u in urls if u}):
                self.by_key.setdefault(k, []).append(it)
            if it.family == SourceFamily.DNS:
                name = _dns_name(it.url) or _dns_name(it.url_final)
                if name:
                    self.by_dns.setdefault(name, []).append(it)
            self.norm[it.item_key] = normalize_text(it.excerpt)

    def at(self, r: _Row) -> list[EvidenceItem]:
        found = {i.item_key: i for i in self.by_key.get(r.key, [])}
        if not found and r.family == "DNS" and _dns_name(r.url):
            found = {i.item_key: i for i in self.by_dns.get(_dns_name(r.url), [])}
        return [found[k] for k in sorted(found)]


def _overlap(segs: Sequence[str], text: str) -> float:
    """How well an item excerpt and a gold excerpt overlap (partial_ratio of the shorter inside the longer)."""
    best = 0.0
    if len(text) < MIN_SEGMENT:
        return best
    for s in segs:
        if s in text or text in s:
            return 100.0
        best = max(best, float(fuzz.partial_ratio(s, text)))
    return best


def _is_trap(item: EvidenceItem) -> bool:
    return item.tags.ai_type == "not_ai"


def _strength_row(r: _Row, items: _Items, threshold: float) -> dict[str, Any]:
    out: dict[str, Any] = {"row": r.row, "vendor_id": r.vendor_id, "url": r.url, "expect": r.expect,
                           "label": NONE_LABEL, "item_key": "", "evidence_id": "", "basis": "", "overlap": None,
                           "citable": None, "agree": None}
    cands = items.at(r)
    if not cands:
        return out
    scored = [(_overlap(r.segments, items.norm[i.item_key]), i) for i in cands]
    rank = {s: n for n, s in enumerate(STRENGTH_ORDER)}
    score, best = min(scored, key=lambda si: (si[0] < threshold, not si[1].citable, -si[0],
                                              rank.get(si[1].strength, len(rank)), si[1].item_key))
    trap = _is_trap(best)
    label = TRAP_LABEL if trap else best.strength
    return {**out, "label": label, "item_key": best.item_key, "evidence_id": best.evidence_id,
            "basis": "excerpt" if score >= threshold else "url", "overlap": round(score, 1),
            "citable": best.citable, "agree": None if trap else best.strength in EXPECT_STRENGTHS[r.expect]}


def _strength_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    matched = [r for r in rows if r["agree"] is not None]
    by_excerpt = [r for r in matched if r["basis"] == "excerpt"]
    agree = sum(1 for r in matched if r["agree"])
    agree_x = sum(1 for r in by_excerpt if r["agree"])
    return {"rows": len(rows), "matched": len(matched), "agree": agree, "label_agreement": _pct(agree, len(matched)),
            "matched_by_excerpt": len(by_excerpt), "agree_by_excerpt": agree_x,
            "label_agreement_excerpt": _pct(agree_x, len(by_excerpt)),
            "traps": sum(1 for r in rows if r["label"] == TRAP_LABEL),
            "unmatched": sum(1 for r in rows if r["label"] == NONE_LABEL)}


def _confusion(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {e: {} for e in EXPECTS}
    for r in rows:
        out[r["expect"]][r["label"]] = out[r["expect"]].get(r["label"], 0) + 1
    order = {label: n for n, label in enumerate(LABEL_ORDER)}
    return {e: dict(sorted(c.items(), key=lambda kv: order.get(kv[0], len(order)))) for e, c in out.items()}


def _llm(items: Sequence[EvidenceItem]) -> dict[str, Any]:
    with_llm = [i for i in items if i.llm_labels]
    disagree = [i for i in with_llm if i.label_disagreements]
    fields = Counter(k for i in disagree for k in i.label_disagreements)
    methods = Counter(i.method for i in items)
    return {"evaluated": True, "items": len(items), "items_with_llm": len(with_llm),
            "agree": len(with_llm) - len(disagree), "disagree": len(disagree),
            "agree_pct": _pct(len(with_llm) - len(disagree), len(with_llm)),
            "disagree_by_field": {k: fields.get(k, 0) for k in V8_LABEL_KEYS},
            "methods": dict(sorted(methods.items())), "proposed": sum(1 for i in items if i.proposed)}


@dataclass(frozen=True)
class _Phrase:
    """A trap pattern. ``text`` labels it; a regex (``literal`` False) reports the text it matched instead."""

    text: str
    rx: re.Pattern[str]
    literal: bool = True

    def found(self, text: str) -> str:
        m = self.rx.search(text) if text else None
        if m is None:
            return ""
        return self.text if self.literal else m.group(0)


def _phrase(text: str, *, ignore_case: bool) -> _Phrase:
    """A suppressor phrase matched as footprint.extract matches it: whole words, a hyphen joining words."""
    flags = re.IGNORECASE if ignore_case else 0
    return _Phrase(text, re.compile(r"(?<![\w-])" + re.escape(text) + r"(?![\w-])", flags))


_SEP = r"[\s\-‐-―]+"


def _flex(text: str) -> _Phrase:
    """A phrase or name matched as footprint.rules matches its tables and seed names: any case, whole words, and a
    space inside it also matches a hyphen, a dash or a line break."""
    m = re.match(r"^(\W*)(.*?)(\W*)$", text.strip(), re.S)
    lead, core, trail = m.groups() if m else ("", text.strip(), "")
    body = _SEP.join(re.escape(w).replace("'", "['’]") for w in re.split(r"[\s\-]+", core) if w)
    pattern = (re.escape(lead) if lead else r"(?<!\w)") + body + (re.escape(trail) if trail else r"(?!\w)")
    return _Phrase(text, re.compile(pattern, re.IGNORECASE))


def _word(text: str) -> _Phrase:
    """A whole word in exact case (acronyms)."""
    pre = r"(?<!\w)" if re.match(r"\w", text[:1]) else ""
    post = r"(?!\w)" if re.match(r"\w", text[-1:]) else ""
    return _Phrase(text, re.compile(pre + re.escape(text) + post))


def _regex(pattern: str) -> _Phrase:
    """A config regex: case-insensitive unless it scopes its own flags."""
    return _Phrase(pattern, re.compile(pattern, re.IGNORECASE), literal=False)


def _found(phrases: Sequence[_Phrase], *texts: str) -> list[str]:
    """Labels of the phrases found in any of ``texts``, in vocabulary order, without repeats."""
    out: dict[str, None] = {}
    for p in phrases:
        for t in texts:
            hit = p.found(t)
            if hit:
                out.setdefault(hit, None)
                break
    return list(out)


def _mask(text: str, phrases: Sequence[_Phrase]) -> str:
    """Blank out every match (same length, so offsets stay valid)."""
    for p in phrases:
        text = p.rx.sub(lambda m: " " * len(m.group(0)), text)
    return text


@dataclass
class _Vocab:
    """Definition-test and source trap vocabulary from the lexicon."""

    definition: list[_Phrase] = field(default_factory=list)
    """[suppressors] phrases and conditional phrases, then the [rule_suppressors] phrases and regexes."""
    masks: list[_Phrase] = field(default_factory=list)
    """[rule_suppressors]: masked, like the suppressors, before core AI terms are looked for."""
    relabel: list[_Phrase] = field(default_factory=list)
    """[definition_test] relabel_regex: automation labelled as AI."""
    ml: list[_Phrase] = field(default_factory=list)
    """[definition_test] ml_phrases and ml_case_sensitive: ML or LLM evidence that passes the definition test."""
    model_not_ml: frozenset[str] = frozenset()
    source: list[_Phrase] = field(default_factory=list)
    """[rule_suppressors] skip_url (else the suppressor phrases that are file names), matched on URL paths."""
    lex: Lexicon | None = None


def _toml_table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    table = data.get(key)
    return table if isinstance(table, Mapping) else {}


def _strings(table: Mapping[str, Any], key: str) -> list[str]:
    values = table.get(key)
    return [str(v) for v in values if str(v).strip()] if isinstance(values, list) else []


def _load_vocab(path: str | PathLike[str] | None, notes: list[str]) -> _Vocab:
    p = Path(path) if path is not None else DEFAULT_LEXICON
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        lex = load_core_lexicon(p)
        sup, rs, dt = (_toml_table(data, k) for k in ("suppressors", "rule_suppressors", "definition_test"))
        definition = [_phrase(t, ignore_case=not t.isupper()) for t in _strings(sup, "phrases")]
        definition += [_phrase(t, ignore_case=True) for t in _strings(sup, "conditional")]
        masks = [_flex(t) for t in _strings(rs, "phrases")] + [_word(t) for t in _strings(rs, "case_sensitive")]
        masks += [_regex(t) for t in _strings(rs, "regex")]
        source = [_regex(t) for t in _strings(rs, "skip_url")]
        if not source:
            source = [_phrase(t, ignore_case=True) for t in _strings(sup, "phrases") if "." in t and " " not in t]
        relabel = [_regex(t) for t in _strings(dt, "relabel_regex")]
        ml = [_flex(t) for t in _strings(dt, "ml_phrases")] + [_word(t) for t in _strings(dt, "ml_case_sensitive")]
        not_ml = frozenset(t.lower() for t in _strings(dt, "model_not_ml"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, re.error) as exc:
        notes.append(f"lexicon unreadable ({type(exc).__name__}); definition-test and source traps not checked")
        return _Vocab()
    return _Vocab(definition=[*definition, *masks], masks=masks, relabel=relabel, ml=ml, model_not_ml=not_ml,
                  source=source, lex=lex)


_MODEL = re.compile(r"(?i)(?<!\w)models?(?!\w)")


def _ml_evidence(text: str, vocab: _Vocab) -> bool:
    """ML or LLM evidence in the text: an ml phrase, or "model(s)" not preceded by a model_not_ml word."""
    if any(p.rx.search(text) for p in vocab.ml):
        return True
    for m in _MODEL.finditer(text):
        before = re.findall(r"[A-Za-z]+", text[max(0, m.start() - 30):m.start()])
        prev = before[-1].lower() if before else ""
        if prev in vocab.model_not_ml or prev == "context":  # "Model Context Protocol" is an ml phrase of its own
            continue
        return True
    return False


def _definition_failures(text: str, vocab: _Vocab) -> tuple[list[str], list[str]]:
    """The definition test (design 2.3): the trap phrases a text rests on (no core AI term is left once they and
    the rule suppressors are masked), and its automation-relabelled-as-AI spans when it has no ML evidence."""
    phrases = _found(vocab.definition, text)
    if phrases and (vocab.lex is None or term_hits(_mask(text, vocab.masks), vocab.lex)):
        phrases = []
    relabels = [] if not vocab.relabel or _ml_evidence(text, vocab) else _found(vocab.relabel, text)
    return phrases, relabels


def _source_hits(vocab: _Vocab, *urls: str) -> list[str]:
    """Trap-source labels (for example llms.txt) found in the URL paths."""
    return list(dict.fromkeys(h.lstrip("/") for h in _found(vocab.source, *(_url_path(u) for u in urls if u))))


@dataclass
class _Entity:
    """One vendor's entity-test vocabulary, read from its seeds as footprint.rules reads them."""

    collisions: list[_Phrase] = field(default_factory=list)
    """Seeded collision names, parenthetical notes removed and the vendor's own names left out."""
    ciks: list[str] = field(default_factory=list)
    """SEC filer CIKs of other companies (a collision's "(..., CIK n)" note)."""
    names: list[_Phrase] = field(default_factory=list)
    """The vendor's names: the seed name, legal names and aliases."""
    hosts: list[str] = field(default_factory=list)
    """Domains whose pages never fail the entity test: the vendor's, its affiliates' and its platform suppliers'."""


_PAREN = re.compile(r"^\s*(.*?)\s*(?:\((.*)\))?\s*$", re.S)
_CIK = re.compile(r"CIK\s*0*(\d+)", re.IGNORECASE)


def _strip_paren(name: str) -> tuple[str, str]:
    m = _PAREN.match(name or "")
    return ((m.group(1) or "").strip(), (m.group(2) or "").strip()) if m else (name.strip(), "")


def _vendor_name(name: str) -> _Phrase:
    """A short all-capitals acronym matches in exact case; any other name in any case."""
    if len(name.split()) == 1 and name.isupper() and len(name) <= 6:
        return _word(name)
    return _flex(name)


def _entity_vocab(seeds: Mapping[str, Any]) -> _Entity:
    names = list(dict.fromkeys([*_strings(seeds, "aliases"), *_strings(seeds, "legal_names"),
                                _strip_paren(str(seeds.get("name") or ""))[0]]))
    names = [n for n in names if n]
    own = {n.lower() for n in names}
    collisions: dict[str, None] = {}
    ciks: dict[str, None] = {}
    for c in _strings(seeds, "collisions"):
        base, note = _strip_paren(c)
        m = _CIK.search(note)
        if m:
            ciks.setdefault(m.group(1), None)
        if base and base.lower() not in own:
            collisions.setdefault(base, None)
    hosts = [*_strings(seeds, "domains")]
    for key in ("affiliate", "platform_supplier"):
        hosts += [str(e.get("domain") or "") for e in seeds.get(key) or [] if isinstance(e, Mapping)]
    return _Entity(collisions=[_flex(c) for c in collisions], ciks=list(ciks),
                   names=[_vendor_name(n) for n in names],
                   hosts=list(dict.fromkeys(h.strip().lower().removeprefix("www.") for h in hosts if h.strip())))


def _host(url: str) -> str:
    return (urlsplit(_original_url(url) or url or "").hostname or "").lower()


def _exempt(ev: _Entity, *urls: str) -> bool:
    """A page of the vendor, an affiliate or a platform supplier (never blocked by a collision)."""
    return any(h == d or h.endswith("." + d) for h in (_host(u) for u in urls if u) for d in ev.hosts)


def _cik_collision(ev: _Entity, url: str, text: str = "") -> str:
    """The colliding CIK when ``url`` is an EDGAR document of another company, else ''."""
    if not ev.ciks or not url or not (_host(url) == "sec.gov" or _host(url).endswith(".sec.gov")):
        return ""
    u = _original_url(url) or url
    for c in ev.ciks:
        if re.search(rf"/edgar/data/0*{c}/", u) or (text and re.search(rf"\bCIK\s*0*{c}\b", text)):
            return c
    return ""


def _collision_only(ev: _Entity, hay: str) -> list[str]:
    """Collision names in ``hay`` when no name of the vendor is left once they are masked, else []."""
    found = _found(ev.collisions, hay)
    if not found:
        return []
    masked = _mask(hay, ev.collisions)
    return [] if any(n.rx.search(masked) for n in ev.names) else found


def _provider_collisions(item: EvidenceItem, ev: _Entity) -> list[str]:
    """Provider names the item found only inside a collision name ("Claude" in "Claude Reumert")."""
    spans = [m.span() for p in ev.collisions for m in p.rx.finditer(item.excerpt)]
    out = []
    for name in item.providers if spans else []:
        occ = [m.span() for m in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", item.excerpt)]
        if occ and all(any(s <= a and b <= e for s, e in spans) for a, b in occ):
            out.append(name)
    return out


def _trap_rows(rows: Sequence[_Row], vocab: _Vocab, entity: Mapping[str, _Entity]) -> list[dict[str, Any]]:
    """Gold rows that are traps: one entry per (row, kind), kinds definition, source and entity."""
    out = []
    for r in rows:
        kinds = []
        phrases, relabels = _definition_failures(r.excerpt, vocab)
        if phrases or relabels:
            kinds.append(("definition", list(dict.fromkeys([*phrases, *relabels]))))
        source = _source_hits(vocab, r.url)
        if source:
            kinds.append(("source", source))
        ev = entity.get(r.vendor_id) or _Entity()
        cik = _cik_collision(ev, r.url)
        names = [f"CIK {cik}"] if cik else [] if _exempt(ev, r.url) else _collision_only(ev, r.excerpt)
        if names:
            kinds.append(("entity", names))
        for kind, found in kinds:
            out.append({"row": r.row, "vendor_id": r.vendor_id, "url": r.url, "kind": kind, "phrases": found,
                        "status": "not_evaluated", "items": [], "leaked": []})
    return out


def _check_trap_row(t: dict[str, Any], r: _Row, items: _Items, leak_keys: set[str], threshold: float,
                    entity: Mapping[str, _Entity]) -> dict[str, Any]:
    """The items at a gold trap row's URL, and whether a citable one cites the trap (it is in the item leaks).
    Statuses: leaked; rejected (items there, none cites the trap); suppressed (no item at the URL)."""
    cands = items.at(r)
    whole_source = t["kind"] == "source" or bool(_cik_collision(entity.get(r.vendor_id) or _Entity(), r.url))
    if whole_source:
        at = cands
    else:
        words = [w.lower() for w in t["phrases"]]
        at = [i for i in cands if _overlap(r.segments, items.norm[i.item_key]) >= threshold
              or any(w in i.excerpt.lower() for w in words)]
    leaked = [i.item_key for i in at if i.item_key in leak_keys]
    status = "leaked" if leaked else "rejected" if at else "suppressed"
    return {**t, "status": status, "items": [i.item_key for i in at], "leaked": leaked}


def _item_leaks(items: Mapping[str, Sequence[EvidenceItem]], vocab: _Vocab, entity: Mapping[str, _Entity],
                text_of: Any) -> list[dict[str, Any]]:
    """Citable items that rest on a trap, with the reasons. ``text_of(item)`` gives the document text or None."""
    leaks = []
    for vid in sorted(items):
        ev = entity.get(vid) or _Entity()
        for it in items[vid]:
            if not it.citable:
                continue
            why = []
            if _is_trap(it):
                why.append("a definition-test trap is citable")
            phrases, relabels = _definition_failures(it.excerpt, vocab)
            if phrases:
                why.append("rests only on trap phrase(s): " + ", ".join(phrases))
            if relabels:
                why.append("relabelled automation without ML evidence: " + ", ".join(relabels))
            source = _source_hits(vocab, it.url, it.url_final)
            if source:
                why.append("comes from a trap source: " + ", ".join(source))
            if ev.collisions or ev.ciks:
                text = text_of(it)
                cik = _cik_collision(ev, it.url, text or "") or _cik_collision(ev, it.url_final, text or "")
                if cik:
                    why.append(f"an SEC filing of CIK {cik}, a different company")
                elif not _exempt(ev, it.url, it.url_final):
                    names = _collision_only(ev, f"{it.title}\n{text if text is not None else it.excerpt}")
                    if names:
                        why.append("names a collision, not the vendor: " + ", ".join(names))
                providers = _provider_collisions(it, ev)
                if providers:
                    why.append("provider read from a collision: " + ", ".join(providers))
            if why:
                leaks.append({"vendor_id": vid, "item_key": it.item_key, "evidence_id": it.evidence_id,
                              "url": it.url, "reasons": why})
    return sorted(leaks, key=lambda x: (x["vendor_id"], x["url"], x["item_key"]))


class _ItemTexts:
    """Document text of items (for the entity test), from the evidence store, cached per doc_id."""

    def __init__(self, store: EvidenceStore | None) -> None:
        self.store = store
        self._cache: dict[str, str | None] = {}

    def __call__(self, item: EvidenceItem) -> str | None:
        if item.doc_id not in self._cache:
            text = None
            if self.store is not None:
                try:
                    text = self.store.get_text(item.doc_id)
                except (KeyError, OSError, UnicodeDecodeError):
                    text = None
            self._cache[item.doc_id] = text
        return self._cache[item.doc_id]


# --------------------------------------------------------------------------- report


def _gold_rows_arg(gold: GoldSource) -> tuple[list[dict], str]:
    if isinstance(gold, str | PathLike):
        return load_gold(gold), Path(gold).as_posix()
    return check_gold(list(gold)), ""


def gold_report(runs_dir: str | PathLike[str] | FindingsSource | None = DEFAULT_RUNS,
                gold_path: GoldSource = DEFAULT_GOLD,
                findings: FindingsSource | str | PathLike[str] | None = None, *,
                store: EvidenceStore | str | PathLike[str] | None = None,
                seeds_dir: str | PathLike[str] | None = "seeds", lexicon: str | PathLike[str] | None = None,
                tou: Any = "default", captures: Sequence[Capture] | None = None,
                vendors: Sequence[str] | None = None, threshold: float = THRESHOLD) -> dict[str, Any]:
    """Evaluate the collection runs (and, optionally, the findings) against the gold set.

    ``runs_dir`` holds the collection runs (runs/<run_id>/ with manifest.json, captures.jsonl, passages.jsonl,
    coverage.jsonl); the newest run per vendor is used, unless ``findings`` is an AssessmentResult whose manifest
    pins ``collection_runs``. ``gold_path`` is the gold JSON file or already-loaded rows. ``findings`` is an
    AssessmentResult, VendorFindings, a sequence of VendorFindings or EvidenceItems, or a path that ``load_findings``
    reads (an assessment run directory, assessment.json or evidence.jsonl); without it the strength, LLM and item
    trap sections are marked not evaluated.

    ``store`` (default ``EvidenceStore("evidence")``) supplies document text and manual captures. ``seeds_dir``
    supplies seed URLs (for explanations) and the entity-test names, domains and collisions; ``lexicon`` the
    definition-test and source trap vocabulary (default config/lexicon.toml);
    ``tou`` the terms-of-use register for manual-only hosts ("default" loads config/tou.toml, None skips it).
    ``captures`` adds captures to the URL index (for example manual captures held in memory). ``vendors`` limits the
    evaluation to those vendor ids.

    The contract form ``gold_report(findings, gold_rows, captures=...)`` also works: a findings object in the first
    position is taken as ``findings``, and URL recall then uses only ``captures``, with passage recall not evaluated.

    Returns a JSON-serialisable dict (see ``RULES`` for the scoring rules). It never reproduces a gold excerpt; trap
    rows list only the trap phrases or pattern matches found in it.
    """
    notes: list[str] = []
    if runs_dir is not None and not isinstance(runs_dir, str | PathLike):
        if findings is not None:
            raise TypeError("pass the findings once: either first (contract form) or as findings=")
        findings, runs_dir = runs_dir, None
    if isinstance(findings, str | PathLike):
        findings = load_findings(findings)
    gold, gold_where = _gold_rows_arg(gold_path)
    rows = _rows(gold)
    items_by_vendor, pinned = _findings(findings)
    if vendors is not None:
        wanted = set(vendors)
        rows = [r for r in rows if r.vendor_id in wanted]
        if items_by_vendor is not None:
            items_by_vendor = {k: val for k, val in items_by_vendor.items() if k in wanted}
    if isinstance(store, str | PathLike):
        store = EvidenceStore(store)
    elif store is None and runs_dir is not None:
        store = EvidenceStore("evidence")
    if tou == "default":
        try:
            from footprint.net.tou import load_tou

            tou = load_tou()
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            notes.append(f"terms-of-use register unreadable ({type(exc).__name__}); hard manual-only hosts only")
            tou = None

    packs = load_runs(runs_dir, pinned=pinned, notes=notes) if runs_dir is not None else {}
    extra = list(captures or [])
    if store is not None:
        try:
            extra.extend(c for c in store.captures() if c.manual)
        except (OSError, ValueError) as exc:
            notes.append(f"evidence index unreadable ({type(exc).__name__}); manual captures not counted")
    vendor_ids = sorted({r.vendor_id for r in rows})
    url_evaluated = runs_dir is not None or captures is not None
    passages_evaluated = runs_dir is not None

    vocab = _load_vocab(lexicon, notes)
    entity: dict[str, _Entity] = {}
    vendors_ix: dict[str, _Vendor] = {}
    for vid in sorted({*vendor_ids, *(items_by_vendor or {})}):
        seeds = _load_seeds(seeds_dir, vid, notes)
        entity[vid] = _entity_vocab(seeds)
        if vid in vendor_ids:
            vendors_ix[vid] = _vendor(vid, packs.get(vid), extra, seeds)

    # URL recall, one entry per unique (vendor, URL).
    url_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r.vendor_id, r.key)
        if k in url_rows:
            url_rows[k]["rows"].append(r.row)
            continue
        url_rows[k] = _url_outcome(r, vendors_ix[r.vendor_id], _manual_only(r.url, tou))
    urls = sorted(url_rows.values(), key=lambda u: (u["vendor_id"], u["rows"][0]))

    # Passage recall, one entry per gold row.
    passage_rows: list[dict[str, Any]] = []
    if passages_evaluated:
        for vid in vendor_ids:
            texts = _Texts(store)  # one vendor's texts in memory at a time
            for r in (x for x in rows if x.vendor_id == vid):
                passage_rows.append(_passage_outcome(r, vendors_ix[vid], url_rows[(vid, r.key)], texts, threshold))
    passage_rows.sort(key=lambda p: p["row"])

    # Strength agreement, trap checks and the LLM comparison need items.
    strength_rows: list[dict[str, Any]] = []
    traps = _trap_rows(rows, vocab, entity)
    leaks = _item_leaks(items_by_vendor, vocab, entity, _ItemTexts(store)) if items_by_vendor is not None else []
    leak_keys = {x["item_key"] for x in leaks}
    item_index: dict[str, _Items] = {}
    if items_by_vendor is not None:
        item_index = {vid: _Items(items_by_vendor.get(vid, [])) for vid in vendor_ids}
        by_row = {r.row: r for r in rows}
        for r in rows:
            if r.family == "SKIP" or r.vendor_id not in items_by_vendor:
                continue
            strength_rows.append(_strength_row(r, item_index[r.vendor_id], threshold))
        traps = [_check_trap_row(t, by_row[t["row"]], item_index[t["vendor_id"]], leak_keys, threshold, entity)
                 if t["vendor_id"] in items_by_vendor else t for t in traps]
        missing = sorted(set(vendor_ids) - set(items_by_vendor))
        if missing:
            notes.append("no findings for " + ", ".join(missing) + "; their rows are not scored for strength")
    all_items = [i for vid in sorted(items_by_vendor or {}) for i in (items_by_vendor or {})[vid]]
    trap_items = [i for i in all_items if _is_trap(i)]

    # Gold URLs the pipeline output does not match.
    unmatched = [{"vendor_id": u["vendor_id"], "url": u["url"], "rows": u["rows"], "stage": "capture",
                  "status": u["status"], "reason": u["reason"]} for u in urls if u["status"] != "captured"]
    if items_by_vendor is not None:
        no_item: dict[tuple[str, str], list[int]] = {}
        for s in strength_rows:
            if s["label"] == NONE_LABEL and url_rows[(s["vendor_id"], normalize_url(s["url"]))]["status"] == "captured":
                no_item.setdefault((s["vendor_id"], s["url"]), []).append(s["row"])
        unmatched += [{"vendor_id": vid, "url": url, "rows": rs, "stage": "evidence", "status": "captured",
                       "reason": "captured, but no evidence item for this URL"} for (vid, url), rs in no_item.items()]
    unmatched.sort(key=lambda u: (u["vendor_id"], u["rows"][0], u["stage"]))

    url_overall = _url_summary(urls)
    passage_overall = _passage_summary(passage_rows)
    strength_overall = _strength_summary(strength_rows) if items_by_vendor is not None else None
    by_vendor: dict[str, Any] = {}
    for vid in vendor_ids:
        pack = packs.get(vid)
        by_vendor[vid] = {
            "run_id": pack.run_id if pack else "",
            "gold_rows": sum(1 for r in rows if r.vendor_id == vid),
            "urls": _url_summary([u for u in urls if u["vendor_id"] == vid]) if url_evaluated else None,
            "passages": _passage_summary([p for p in passage_rows if p["vendor_id"] == vid])
            if passages_evaluated else None,
            "strength": _strength_summary([s for s in strength_rows if s["vendor_id"] == vid])
            if items_by_vendor is not None and vid in items_by_vendor else None,
        }

    trap_leaked = [t for t in traps if t["status"] == "leaked"]
    traps_passed: bool | None = None
    if items_by_vendor is not None and all_items:
        traps_passed = not leaks and not trap_leaked
    elif items_by_vendor is not None:
        notes.append("the findings hold no evidence items; the trap gate is not decided")
    url_gate = url_overall["captured_or_explained_pct"] if url_evaluated else None
    confusion = _confusion(strength_rows) if items_by_vendor is not None else {}
    gold_sha = hashlib.sha256(json.dumps(gold, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema": SCHEMA,
        "gold": {"path": gold_where, "rows": len(gold), "evaluated_rows": len(rows), "vendors": vendor_ids,
                 "sha256": gold_sha},
        "params": {"threshold": threshold, "min_segment": MIN_SEGMENT, "url_gate_pct": URL_GATE_PCT},
        "rules": list(RULES),
        "notes": sorted(set(notes)),
        "runs": {vid: (packs[vid].run_id if vid in packs else "") for vid in vendor_ids},
        "gold_items": len(rows),
        "captured": url_overall["captured"] if url_evaluated else None,
        "captured_pct": url_overall["captured_pct"] if url_evaluated else None,
        "url_recall": {"evaluated": url_evaluated, "overall": url_overall if url_evaluated else None,
                       "urls": urls if url_evaluated else []},
        "passage_recall": {"evaluated": passages_evaluated, "overall": passage_overall if passages_evaluated else None,
                           "by_expect": {e: _passage_summary([p for p in passage_rows if p["expect"] == e])
                                         for e in EXPECTS} if passages_evaluated else {},
                           "rows": passage_rows},
        "strength": {"evaluated": items_by_vendor is not None, "overall": strength_overall, "confusion": confusion,
                     "rows": strength_rows},
        "matched": strength_overall["matched"] if strength_overall else None,
        "label_agreement": strength_overall["label_agreement"] if strength_overall else None,
        "confusion": confusion,
        "unmatched": unmatched if url_evaluated else [],
        "llm": _llm(all_items) if items_by_vendor is not None else {"evaluated": False},
        "traps": {
            "evaluated": items_by_vendor is not None,
            "items": len(trap_items), "rejected": sum(1 for i in trap_items if not i.citable),
            "gold_rows": traps, "leaks": leaks, "passed": traps_passed,
            "vocabulary": {"definition": [p.text for p in vocab.definition], "relabel": len(vocab.relabel),
                           "source": [p.text for p in vocab.source],
                           "entity": {vid: [p.text for p in entity[vid].collisions] for vid in vendor_ids},
                           "collision_ciks": {vid: list(entity[vid].ciks) for vid in vendor_ids}},
        },
        "by_vendor": by_vendor,
        "gates": {
            "p2_url_recall": {"target": f">= {URL_GATE_PCT:g}% of in-scope gold URLs captured or explained",
                              "value": url_gate, "pass": None if url_gate is None else url_gate >= URL_GATE_PCT},
            "p3_traps": {"target": "every trap rejected; no citable item rests on a trap", "pass": traps_passed},
        },
    }


# --------------------------------------------------------------------------- markdown


def _cell(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _pct_cell(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _table(headers: Sequence[str], body: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in body]
    return lines


def _gate_cell(passed: bool | None) -> str:
    return "not evaluated" if passed is None else "PASS" if passed else "FAIL"


def render_markdown(report: Mapping[str, Any]) -> str:
    """A deterministic Markdown page (LF line ends) for docs: gates, URL and passage recall, strength agreement,
    Gemini vs rules and trap checks. It quotes no gold excerpt."""
    out: list[str] = []
    add = out.extend
    g = report["gold"]
    params = report["params"]
    add(["# Gold-set evaluation", "",
         f"Gold set: {g['evaluated_rows']} of {g['rows']} rows, vendors {', '.join(g['vendors']) or 'none'} "
         f"(`{g['path'] or 'in memory'}`, sha256 `{g['sha256'][:12]}`). Fuzzy threshold: partial_ratio >= "
         f"{params['threshold']:g}.", "",
         "> Reported excerpts may be summarised or elided by the scout. They are only search keys for the captured "
         "text: they are never evidence and are not reproduced here.", ""])

    gates = report["gates"]
    url_overall = report["url_recall"]["overall"]
    traps = report["traps"]
    trap_rows = traps["gold_rows"]
    add(["## Gates", ""])
    add(_table(["Gate", "Target", "Result", "Status"], [
        ["P2 URL recall", gates["p2_url_recall"]["target"], _pct_cell(gates["p2_url_recall"]["value"]),
         _gate_cell(gates["p2_url_recall"]["pass"])],
        ["P3 traps", gates["p3_traps"]["target"],
         f"{sum(1 for t in trap_rows if t['status'] in ('rejected', 'suppressed'))} of {len(trap_rows)} gold traps "
         f"rejected or suppressed; {len(traps['leaks'])} item leak(s)" if traps["evaluated"] else "no findings",
         _gate_cell(gates["p3_traps"]["pass"])],
    ]))
    add([""])

    add(["## URL recall", ""])
    if not report["url_recall"]["evaluated"]:
        add(["Not evaluated: no collection runs or captures were given.", ""])
    else:
        body = []
        for vid, v in report["by_vendor"].items():
            u = v["urls"]
            body.append([vid, v["run_id"] or "none", u["in_scope"], u["captured"], u["explained"], u["missed"],
                         u["excluded"], _pct_cell(u["captured_or_explained_pct"])])
        o = url_overall
        body.append(["All", "", o["in_scope"], o["captured"], o["explained"], o["missed"], o["excluded"],
                     _pct_cell(o["captured_or_explained_pct"])])
        add(_table(["Vendor", "Run", "In scope", "Captured", "Explained", "Missed", "Excluded",
                    "Captured or explained"], body))
        add([""])
        rest = [u for u in report["url_recall"]["urls"] if u["status"] != "captured"]
        if rest:
            add(["### Gold URLs not captured", ""])
            add(_table(["Vendor", "Rows", "URL", "Status", "Reason"],
                       [[u["vendor_id"], ", ".join(str(n) for n in u["rows"]), u["url"], u["status"], u["reason"]]
                        for u in rest]))
            add([""])

    add(["## Passage recall", ""])
    pr = report["passage_recall"]
    if not pr["evaluated"]:
        add(["Not evaluated: no collection runs were given.", ""])
    else:
        body = []
        for vid, v in report["by_vendor"].items():
            p = v["passages"]
            body.append([vid, p["in_scope"], p["located"], p["located_elsewhere"], p["partial"], p["not_located"],
                         _pct_cell(p["recall"]), f"{p['in_passage']} of {p['in_passage_of']}"])
        o = pr["overall"]
        body.append(["All", o["in_scope"], o["located"], o["located_elsewhere"], o["partial"], o["not_located"],
                     _pct_cell(o["recall"]), f"{o['in_passage']} of {o['in_passage_of']}"])
        add(_table(["Vendor", "Rows in scope", "Located", "Elsewhere", "Partial", "Not located", "Recall",
                    "In a lexicon passage"], body))
        add(["", "By gold strength:", ""])
        add(_table(["Expect", "Rows in scope", "Located", "Elsewhere", "Recall", "In a lexicon passage"],
                   [[e, s["in_scope"], s["located"], s["located_elsewhere"], _pct_cell(s["recall"]),
                     f"{s['in_passage']} of {s['in_passage_of']}"] for e, s in pr["by_expect"].items()]))
        add([""])
        rest = [p for p in pr["rows"] if p["scope"] == "in" and p["status"] != "located"]
        if rest:
            add(["### Rows not located at their own URL", ""])
            add(_table(["Row", "Vendor", "URL", "Status", "Best score", "Reason"],
                       [[p["row"], p["vendor_id"], p["url"], p["status"], p["score"], p["reason"]] for p in rest]))
            add([""])
        outside = [p for p in pr["rows"] if p["scope"] == "in" and p["in_passage"] is False and p["expect"] != "weak"]
        if outside:
            add(["### Located excerpts outside every lexicon passage", "",
                 "These strong, moderate and marketing-only excerpts are in the captured text but in no lexicon "
                 "passage (passages.jsonl), so rules that read passages never see them.", ""])
            add(_table(["Row", "Vendor", "URL", "Expect", "Status"],
                       [[p["row"], p["vendor_id"], p["url"], p["expect"], p["status"]] for p in outside]))
            add([""])

    add(["## Strength agreement", ""])
    st = report["strength"]
    if not st["evaluated"]:
        add(["Not evaluated: no findings were given.", ""])
    else:
        o = st["overall"]
        add([f"Matched {o['matched']} rows; {o['agree']} agree ({_pct_cell(o['label_agreement'])}). Matched on the "
             f"excerpt itself: {o['matched_by_excerpt']}, of which {o['agree_by_excerpt']} agree "
             f"({_pct_cell(o['label_agreement_excerpt'])}). Rows with no item: {o['unmatched']}; rows whose item "
             f"is a trap: {o['traps']}.", ""])
        always = {s for e in EXPECTS for s in EXPECT_STRENGTHS[e] if s not in CONTEXT_STRENGTHS} | {NONE_LABEL}
        present = [label for label in LABEL_ORDER
                   if label in always or any(label in c for c in st["confusion"].values())]
        add(_table(["Gold expect", *present],
                   [[e, *(st["confusion"][e].get(label, 0) for label in present)] for e in EXPECTS]))
        add([""])

    add(["## Gemini vs rules", ""])
    llm = report["llm"]
    if not llm.get("evaluated"):
        add(["Not evaluated: no findings were given.", ""])
    else:
        fields = ", ".join(f"{k} {n}" for k, n in llm["disagree_by_field"].items())
        add([f"{llm['items_with_llm']} of {llm['items']} items carry Gemini labels: {llm['agree']} agree with the "
             f"rules, {llm['disagree']} disagree ({fields}). Pending proposals: {llm['proposed']}.", ""])

    add(["## Trap checks", ""])
    vocab = traps["vocabulary"]
    collisions = sum(len(v) for v in vocab["entity"].values())
    ciks = sum(len(v) for v in vocab["collision_ciks"].values())
    add([f"Vocabulary: {len(vocab['definition'])} definition-test phrases or patterns, {vocab['relabel']} relabelled-"
         f"automation patterns, {len(vocab['source'])} trap-source path pattern(s), {collisions} seeded collision "
         f"name(s) and {ciks} colliding SEC CIK(s).", ""])
    if trap_rows:
        add(_table(["Row", "Vendor", "URL", "Kind", "Trap phrase", "Status"],
                   [[t["row"], t["vendor_id"], t["url"], t["kind"], ", ".join(t["phrases"]), t["status"]]
                    for t in trap_rows]))
        add([""])
    else:
        add(["No gold row is a trap.", ""])
    if traps["evaluated"]:
        add([f"Definition-test trap items in the findings: {traps['items']}, rejected: {traps['rejected']}.", ""])
        if traps["leaks"]:
            add(_table(["Vendor", "Evidence", "URL", "Why"],
                       [[x["vendor_id"], x["evidence_id"] or x["item_key"][:12], x["url"], "; ".join(x["reasons"])]
                        for x in traps["leaks"]]))
        else:
            add(["No citable item fails the definition, source or entity trap tests."])
        add([""])

    if report["notes"]:
        add(["## Notes", ""])
        add([f"- {_cell(n)}" for n in report["notes"]])
        add([""])
    return "\n".join(out).rstrip("\n") + "\n"


def write_report(report: Mapping[str, Any], json_path: str | PathLike[str],
                 md_path: str | PathLike[str] | None = None) -> dict[str, str]:
    """Save a report as JSON (sorted keys, 2-space indent) and, optionally, as the Markdown page; UTF-8 with LF.

    Parent directories are created. Returns {"json": path[, "markdown": path]} as POSIX strings.
    """
    out: dict[str, str] = {}
    jp = Path(json_path)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_bytes((json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    out["json"] = jp.as_posix()
    if md_path is not None:
        mp = Path(md_path)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_bytes(render_markdown(report).encode("utf-8"))
        out["markdown"] = mp.as_posix()
    return out
