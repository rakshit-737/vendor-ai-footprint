"""Text extraction, sentence splitting and lexicon passage finding (P2 contract: docs/contracts_p2.md).

Optional dependencies (trafilatura, htmldate, pypdf, charset-normalizer) are imported lazily and
degrade gracefully: HTML falls back to lxml text, dates fall back to the Last-Modified header.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import json
import re
import tomllib
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from footprint.models import Capture, Document

DEFAULT_LEXICON = Path(__file__).resolve().parents[2] / "config" / "lexicon.toml"
MAX_PASSAGE = 700

# ---------------------------------------------------------------- lexicon


@dataclass
class Lexicon:
    """Compiled lexicon. ``core`` maps term label -> compiled regex."""

    version: str = ""
    core: dict[str, re.Pattern[str]] = field(default_factory=dict)
    guards: dict[str, re.Pattern[str]] = field(default_factory=dict)
    guard_terms: dict[str, re.Pattern[str]] = field(default_factory=dict)
    suppressors: list[re.Pattern[str]] = field(default_factory=list)
    conditional: list[re.Pattern[str]] = field(default_factory=list)


def _wb(term: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + term + r"(?![\w-])", flags)


def load_core_lexicon(path: str | Path | None = None) -> Lexicon:
    """Load config/lexicon.toml ([core] terms/case_sensitive, [guards], [suppressors])."""
    data = tomllib.loads(Path(path or DEFAULT_LEXICON).read_text(encoding="utf-8"))
    lex = Lexicon(version=data.get("version", ""))
    core = data.get("core", {})
    for t in core.get("terms", []):
        lex.core[t] = _wb(re.escape(t).replace(r"\ ", r"[\s-]+"), re.IGNORECASE)
    for t in core.get("case_sensitive", []):
        lex.core[t] = _wb(t)
    for name, ctx in data.get("guards", {}).items():
        lex.guards[name] = re.compile(ctx)
        # guarded provider names are matched as terms too (only counted when guard context present)
        lex.guard_terms[name] = lex.core.get(name) or _wb(re.escape(name))
    sup = data.get("suppressors", {})
    lex.suppressors = [_wb(re.escape(p), 0 if p.isupper() else re.IGNORECASE) for p in sup.get("phrases", [])]
    lex.conditional = [_wb(re.escape(p), re.IGNORECASE) for p in sup.get("conditional", [])]
    return lex


def _masked(text: str, lex: Lexicon) -> str:
    """Blank out suppressor phrases (same length, so offsets stay valid)."""
    for rx in lex.suppressors:
        text = rx.sub(lambda m: " " * len(m.group(0)), text)
    return text


def term_hits(text: str, lex: Lexicon, context: str | None = None) -> list[str]:
    """Lexicon terms found in ``text`` after suppression; guarded terms need guard context in ``context``."""
    masked = _masked(text, lex)
    ctx = context if context is not None else text
    hits = [name for name, rx in lex.core.items() if name not in lex.guards and rx.search(masked)]
    for name, rx in lex.guard_terms.items():
        if rx.search(masked) and lex.guards[name].search(_masked(ctx, lex)):
            hits.append(name)
    return hits


# ---------------------------------------------------------------- sentences

_ABBREV = [
    "Inc.", "Corp.", "Co.", "Ltd.", "L.L.C.", "LLC.", "U.S.", "U.K.", "e.g.", "i.e.", "etc.", "vs.",
    "Mr.", "Mrs.", "Ms.", "Dr.", "St.", "No.", "Jan.", "Feb.", "Mar.", "Apr.", "Jun.", "Jul.", "Aug.",
    "Sep.", "Sept.", "Oct.", "Nov.", "Dec.", "approx.", "Fig.", "N.A.",
]
_BOUNDARY = re.compile(r"[.!?]+[\"')\]]*(?=\s+[\"'(\[]?[A-Z0-9])|\n\s*\n|\n(?=\s*[-*•])")


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of sentences; never splits after known abbreviations."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        end = m.end()
        if m.group(0)[0] == ".":
            head = text[max(0, m.start() - 10) : m.start() + 1]
            if any(head.endswith(a) for a in _ABBREV) or re.search(r"(?:^|\s)[A-Z]\.$", head):
                continue
        _add_span(text, start, end, spans)
        start = end
    _add_span(text, start, len(text), spans)
    return spans


def _add_span(text: str, s: int, e: int, spans: list[tuple[int, int]]) -> None:
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    if e > s:
        spans.append((s, e))


def find_passages(document_text: str, lexicon: Lexicon) -> list[tuple[int, int, list[str]]]:
    """Hit sentence +/-1 neighbour, capped at 700 chars; exact slices; overlapping windows merged."""
    sents = split_sentences(document_text)
    out: list[tuple[int, int, list[str]]] = []
    for i, (s, e) in enumerate(sents):
        if not term_hits(document_text[s:e], lexicon, None):
            continue
        ws, we = s, e
        if i > 0 and we - sents[i - 1][0] <= MAX_PASSAGE:
            ws = sents[i - 1][0]
        if i + 1 < len(sents) and sents[i + 1][1] - ws <= MAX_PASSAGE:
            we = sents[i + 1][1]
        if we - ws > MAX_PASSAGE:  # single over-long sentence: clamp around it
            ws, we = s, min(e, s + MAX_PASSAGE)
        if out and ws < out[-1][1] and we - out[-1][0] <= MAX_PASSAGE:
            ws = out.pop()[0]
        hits = term_hits(document_text[ws:we], lexicon)
        if hits:
            out.append((ws, we, hits))
    return out


# ---------------------------------------------------------------- decoding


def _norm_charset(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name.strip().strip("\"'")).name
    except LookupError:
        return None


def decode_bytes(raw: bytes, content_type: str = "") -> str:
    """Decode by HTTP header charset, then <meta> charset, then BOM/utf-8, then charset-normalizer."""
    m = re.search(r"charset=([\w.:-]+)", content_type or "", re.I)
    cs = _norm_charset(m.group(1) if m else None)
    if not cs:
        mm = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", raw[:4096], re.I)
        cs = _norm_charset(mm.group(1).decode("ascii", "ignore") if mm else None)
    if cs:
        if cs == "iso8859-1":
            cs = "cp1252"  # WHATWG: latin-1 labels mean windows-1252
        try:
            return raw.decode(cs)
        except UnicodeDecodeError:
            pass
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except ImportError:
        pass
    return raw.decode("cp1252", "replace")


# ---------------------------------------------------------------- html


def _iso(val: str) -> str:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", val or "")
    return "-".join(m.groups()) if m else ""


def _jsonld_dates(tree: Any) -> str:
    for node in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(node.text or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                if d.get("datePublished"):
                    return _iso(str(d["datePublished"]))
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
    return ""


def _last_modified(capture: Capture) -> str:
    lm = capture.headers.get("last-modified", "")
    try:
        return parsedate_to_datetime(lm).date().isoformat() if lm else ""
    except (TypeError, ValueError):
        return ""


def html_date(html: str, tree: Any, capture: Capture) -> tuple[str, str]:
    """(date, basis): JSON-LD datePublished, meta article:published_time, htmldate, Last-Modified."""
    d = _jsonld_dates(tree)
    if d:
        return d, "json-ld datePublished"
    for v in tree.xpath('//meta[@property="article:published_time" or @name="article:published_time"]/@content'):
        if _iso(v):
            return _iso(v), "meta article:published_time"
    try:
        from htmldate import find_date

        d = find_date(html, original_date=True, extensive_search=False) or ""
        if d:
            return _iso(d), "htmldate"
    except Exception:  # noqa: BLE001 - optional dependency / parser failure
        pass
    d = _last_modified(capture)
    return (d, "last-modified") if d else ("", "")


def _lxml_text(tree: Any) -> str:
    for bad in tree.xpath("//script|//style|//noscript|//template|//svg"):
        bad.getparent().remove(bad)
    body = tree.find(".//body")
    root = body if body is not None else tree
    blocks = []
    for el in root.iter():
        if el.tag in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "blockquote", "pre", "dd", "dt"}:
            t = " ".join("".join(el.itertext()).split())
            if t:
                blocks.append(t)
    if not blocks:
        blocks = [" ".join("".join(root.itertext()).split())]
    return "\n\n".join(blocks)


def extract_html(html: str, capture: Capture) -> tuple[str, str, str, str, str]:
    """Return (text, title, published, date_basis, extractor)."""
    import lxml
    from lxml import html as lh

    tree = lh.fromstring(html) if html.strip() else lh.fromstring("<html/>")
    title = " ".join((tree.findtext(".//title") or "").split())
    if not title:
        og = tree.xpath('//meta[@property="og:title"]/@content')
        title = og[0].strip() if og else ""
    published, basis = html_date(html, tree, capture)
    text, extractor = "", ""
    try:
        import trafilatura

        text = trafilatura.extract(html, favor_precision=False, include_tables=True, include_comments=False) or ""
        extractor = f"trafilatura {trafilatura.__version__}"
    except Exception:  # noqa: BLE001 - optional/broken dependency
        text = ""
    if not text.strip():
        text = _lxml_text(lh.fromstring(html) if html.strip() else tree)
        extractor = f"lxml {lxml.__version__}"
    return text, title, published, basis, extractor


# ---------------------------------------------------------------- pdf


def extract_pdf(raw: bytes) -> tuple[str, list[int], str, str]:
    """Return (text, page_offsets, title, extractor); pages joined with a form feed + blank line."""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(raw))
    parts: list[str] = []
    pages: list[int] = []
    pos = 0
    sep = "\n\n"
    for page in reader.pages:
        pages.append(pos)
        t = (page.extract_text() or "").strip()
        parts.append(t)
        pos += len(t) + len(sep)
    title = ""
    try:
        title = str((reader.metadata or {}).get("/Title") or "")
    except Exception:  # noqa: BLE001
        title = ""
    return sep.join(parts), pages, title, f"pypdf {pypdf.__version__}"


def _pdf_date(raw: bytes) -> str:
    try:
        import pypdf

        meta = pypdf.PdfReader(io.BytesIO(raw)).metadata
        d = meta.creation_date if meta else None
        return d.date().isoformat() if d else ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- document


def _kind(capture: Capture, raw: bytes) -> str:
    ct = (capture.content_type or capture.headers.get("content-type", "")).lower()
    url = (capture.url_final or capture.url_requested).lower().split("?")[0]
    if raw[:5] == b"%PDF-" or "pdf" in ct or url.endswith(".pdf"):
        return "pdf"
    if "json" in ct or url.endswith(".json"):
        return "json"
    if "html" in ct or "xml" in ct or re.search(rb"<(html|body|p|div)\b", raw[:2048], re.I):
        return "html"
    return "text"


def extract_document(capture: Capture, raw_bytes: bytes, store: Any) -> Document:
    """Extract text from a capture, store it via ``store.put_text`` and return the Document."""
    ct = capture.content_type or capture.headers.get("content-type", "")
    kind = _kind(capture, raw_bytes)
    title = published = basis = ""
    pages: list[int] = []
    if kind == "pdf":
        text, pages, title, extractor = extract_pdf(raw_bytes)
        published = _pdf_date(raw_bytes)
        basis = "pdf creation_date" if published else ""
        if not published:
            published = _last_modified(capture)
            basis = "last-modified" if published else ""
    elif kind == "html":
        text, title, published, basis, extractor = extract_html(decode_bytes(raw_bytes, ct), capture)
    elif kind == "json":
        s = decode_bytes(raw_bytes, ct)
        try:
            text = json.dumps(json.loads(s), indent=2, ensure_ascii=False, sort_keys=True)
        except ValueError:
            text = s
        extractor = "json"
        published = _last_modified(capture)
        basis = "last-modified" if published else ""
    else:
        text = decode_bytes(raw_bytes, ct)
        extractor = "text"
        published = _last_modified(capture)
        basis = "last-modified" if published else ""
    sha, path = store.put_text(text)
    return Document(
        doc_id=sha or hashlib.sha256(text.encode()).hexdigest(),
        capture_id=capture.capture_id,
        vendor_id=capture.vendor_id,
        family=capture.family,
        url=capture.url_final or capture.url_requested,
        title=title,
        kind=kind,
        published=published,
        date_basis=basis,
        text_path=str(path),
        text_len=len(text),
        extractor=extractor,
        pages=pages,
    )


__all__ = [
    "Lexicon",
    "decode_bytes",
    "extract_document",
    "extract_html",
    "extract_pdf",
    "find_passages",
    "load_core_lexicon",
    "split_sentences",
    "term_hits",
]
