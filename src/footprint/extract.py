"""Text extraction, sentence splitting and lexicon passage finding (P2 contract: docs/contracts_p2.md).

Optional dependencies (trafilatura, htmldate, pypdf, charset-normalizer) are imported lazily and
degrade gracefully: HTML falls back to lxml text, dates fall back to the Last-Modified header.
"""

from __future__ import annotations

import codecs
import functools
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


def _core(term: str, flags: int = 0) -> re.Pattern[str]:
    """Whole-word core term that also matches as the head of a hyphen compound ("AI-powered", "ML-based"); a
    hyphen *before* the term still blocks it ("non-AI")."""
    return re.compile(r"(?<![\w-])" + term + r"(?!\w)", flags)


def load_core_lexicon(path: str | Path | None = None) -> Lexicon:
    """Load config/lexicon.toml ([core] terms/case_sensitive, [guards], [suppressors]).

    Core phrases match case-insensitively with an optional plural ("AI agents", "chatbots"); core terms of either
    kind also match at the head of a hyphen compound ("AI-powered"), as the [ai_terms] note in the lexicon says.
    """
    data = tomllib.loads(Path(path or DEFAULT_LEXICON).read_text(encoding="utf-8"))
    lex = Lexicon(version=data.get("version", ""))
    core = data.get("core", {})
    for t in core.get("terms", []):
        lex.core[t] = _core(re.escape(t).replace(r"\ ", r"[\s-]+") + r"(?:e?s)?", re.IGNORECASE)
    for t in core.get("case_sensitive", []):
        lex.core[t] = _core(t)
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
    hits = [name for name, rx in lex.core.items() if name not in lex.guards and rx.search(masked)]
    masked_ctx: str | None = None
    for name, rx in lex.guard_terms.items():
        if rx.search(masked):
            if masked_ctx is None:
                masked_ctx = masked if context is None else _masked(context, lex)
            if lex.guards[name].search(masked_ctx):
                hits.append(name)
    return hits


def hit_spans(text: str, lex: Lexicon) -> list[tuple[int, int]]:
    """Sorted (start, end) offsets of every lexicon match in ``text`` (suppressors masked; a guarded term only
    when its guard matches ``text``)."""
    masked = _masked(text, lex)
    spans = [m.span() for name, rx in lex.core.items() if name not in lex.guards for m in rx.finditer(masked)]
    for name, rx in lex.guard_terms.items():
        if lex.guards[name].search(masked):
            spans.extend(m.span() for m in rx.finditer(masked))
    return sorted(spans)


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


def _long_sentence_windows(text: str, s: int, e: int, lexicon: Lexicon) -> list[tuple[int, int]]:
    """Windows of at most MAX_PASSAGE characters around each cluster of lexicon hits inside an over-long sentence
    (a run-on block such as a job description whose markup lost its sentence breaks). Windows start and end on
    word boundaries and keep the hit roughly centred."""
    spans = [(a + s, b + s) for a, b in hit_spans(text[s:e], lexicon)]
    clusters: list[list[int]] = []
    for a, b in spans:
        if clusters and b - clusters[-1][0] <= MAX_PASSAGE // 2:
            clusters[-1][1] = max(clusters[-1][1], b)
        else:
            clusters.append([a, b])
    out: list[tuple[int, int]] = []
    for ca, cb in clusters:
        lead = (MAX_PASSAGE - (cb - ca)) // 2
        ws = max(s, ca - lead)
        we = min(e, ws + MAX_PASSAGE)
        ws = max(s, we - MAX_PASSAGE)
        if ws > s and not text[ws - 1].isspace():  # move forward to the next word start (never past the hit)
            nxt = re.search(r"\s", text[ws:ca])
            if nxt:
                ws += nxt.end()
        if we < e and not text[we].isspace():  # move back to the previous word end (never before the hit)
            cut = text.rfind(" ", cb, we)
            cut = max(cut, text.rfind("\n", cb, we))
            if cut > cb:
                we = cut
        while ws < we and text[ws].isspace():
            ws += 1
        while we > ws and text[we - 1].isspace():
            we -= 1
        if out and ws < out[-1][1]:
            ws = out[-1][1]
            while ws < we and text[ws].isspace():
                ws += 1
        if we > ws:
            out.append((ws, we))
    return out


def find_passages(document_text: str, lexicon: Lexicon) -> list[tuple[int, int, list[str]]]:
    """Hit sentence +/-1 neighbour, capped at 700 chars; exact slices; overlapping windows merged.

    A sentence longer than 700 characters gets one window per cluster of hits inside it, so no mention is lost
    when the source has no sentence breaks (for example list items glued together by the markup).
    """
    sents = split_sentences(document_text)
    out: list[tuple[int, int, list[str]]] = []
    for i, (s, e) in enumerate(sents):
        if not term_hits(document_text[s:e], lexicon, None):
            continue
        if e - s > MAX_PASSAGE:  # single over-long sentence: window each hit cluster
            for ws, we in _long_sentence_windows(document_text, s, e, lexicon):
                if out and ws < out[-1][1]:
                    continue
                hits = term_hits(document_text[ws:we], lexicon, document_text[s:e])
                if hits:
                    out.append((ws, we, hits))
            continue
        ws, we = s, e
        if i > 0 and we - sents[i - 1][0] <= MAX_PASSAGE:
            ws = sents[i - 1][0]
        if i + 1 < len(sents) and sents[i + 1][1] - ws <= MAX_PASSAGE:
            we = sents[i + 1][1]
        if out and ws < out[-1][1]:
            if we - out[-1][0] <= MAX_PASSAGE:
                ws = out.pop()[0]
            else:
                ws = max(s, out[-1][1])
                while ws < we and document_text[ws].isspace():
                    ws += 1
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


BLOCK_TAGS: frozenset[str] = frozenset({
    "address", "article", "blockquote", "body", "br", "caption", "dd", "details", "div", "dl", "dt", "figcaption",
    "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "html", "li", "main", "ol", "p", "pre", "section", "summary",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "document", "text", "center",
})
"""Elements that end a text block (a sentence never runs across them)."""
DROP_TAGS: frozenset[str] = frozenset({
    "script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "button", "select", "option",
    "textarea", "input", "head", "title", "meta", "link", "nav", "header", "footer", "aside", "form", "dialog",
    "ix:header",
})
"""Elements whose text is never document text (code, navigation, page chrome, inline XBRL headers)."""
_BOILER = re.compile(
    r"(?:^|[\s_-])(?:nav|navbar|navigation|menu|submenu|megamenu|footer|header|masthead|breadcrumbs?|cookies?|"
    r"consent|social|share|sharing|skip|sidebar|newsletter|subscribe|modal|popup)(?=$|[\s_-])", re.I)
_BOILER_ROLES = frozenset({"navigation", "banner", "contentinfo", "search", "menu", "menubar", "dialog"})
_KEEP_TAGS = frozenset({"html", "body", "main", "article"})
_XML_DECL = re.compile(r"^\s*(?:﻿)?<\?xml[^>]*\?>", re.I)
MIN_TRAFILATURA_CHARS = 200
"""Below this, trafilatura's text is treated as a miss and the block text is used."""
MIN_MISSED_BLOCK_WORDS = 8
"""An AI-bearing block this long that trafilatura dropped makes the block text win (short menu items do not)."""


def _boilerplate(el: Any) -> bool:
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag in _KEEP_TAGS:
        return False
    if (el.get("role") or "").lower() in _BOILER_ROLES or (el.get("aria-hidden") or "").lower() == "true":
        return True
    return bool(_BOILER.search(f"{el.get('class') or ''} {el.get('id') or ''}"))


def html_blocks(root: Any, *, prune: bool = True) -> list[str]:
    """Text blocks of an lxml tree in document order: one block per block-level element, whitespace collapsed.

    Code, navigation and page chrome are skipped (``DROP_TAGS``); with ``prune`` also elements whose class, id or
    role marks them as menus, headers, footers, cookie banners and the like.
    """
    blocks: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        t = " ".join("".join(cur).split())
        if t:
            blocks.append(t)
        cur.clear()

    def walk(el: Any) -> None:
        tag = el.tag.lower() if isinstance(el.tag, str) else ""
        if not tag or tag in DROP_TAGS or (prune and _boilerplate(el)):  # comments, PIs and dropped elements
            if el.tail:
                cur.append(el.tail)
            return
        block = tag in BLOCK_TAGS
        if block:
            flush()
        if el.text:
            cur.append(el.text)
        for child in el:
            walk(child)
        if block:
            flush()
        if el.tail:
            cur.append(el.tail)

    try:
        walk(root)
        flush()
    except RecursionError:  # pathological nesting: fall back to one flat block
        blocks = [" ".join("".join(root.itertext()).split())]
    return blocks


def _parse_html(html: str) -> Any:
    from lxml import html as lh

    html = _XML_DECL.sub("", html, count=1)
    try:
        return lh.fromstring(html) if html.strip() else lh.fromstring("<html/>")
    except Exception:  # noqa: BLE001 - lxml raises ParserError/ValueError on odd input
        return lh.fromstring("<html/>")


def html_fragment_text(fragment: str) -> str:
    """Plain text of an HTML fragment (WordPress post content, job descriptions), one block per paragraph, list
    item or line break, joined by blank lines so sentences never run together."""
    if not fragment or not fragment.strip():
        return ""
    if "<" not in fragment:
        return " ".join(fragment.split())
    from lxml import html as lh

    try:
        root = lh.fragment_fromstring(_XML_DECL.sub("", fragment, count=1), create_parent="div")
    except Exception:  # noqa: BLE001
        return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())
    return "\n\n".join(html_blocks(root, prune=False))


def _lxml_text(tree: Any) -> str:
    """Block text of a parsed page (boilerplate pruned; unpruned if pruning leaves nothing)."""
    body = tree.find(".//body")
    root = body if body is not None else tree
    blocks = html_blocks(root) or html_blocks(root, prune=False)
    return "\n\n".join(blocks)


def _flat(text: str) -> str:
    return " ".join(text.split()).lower()


@functools.lru_cache(maxsize=4)
def _cached_lexicon(path: str) -> Lexicon:
    return load_core_lexicon(path)


def missed_ai_blocks(main_text: str, blocks: list[str], lexicon: Lexicon | None = None) -> list[str]:
    """Blocks of at least MIN_MISSED_BLOCK_WORDS words that carry a lexicon hit but are absent from ``main_text``."""
    lex = lexicon or _cached_lexicon(str(DEFAULT_LEXICON))
    flat = _flat(main_text)
    return [b for b in blocks
            if len(b.split()) >= MIN_MISSED_BLOCK_WORDS and term_hits(b, lex) and _flat(b) not in flat]


def extract_html(html: str, capture: Capture, lexicon: Lexicon | None = None) -> tuple[str, str, str, str, str]:
    """Return (text, title, published, date_basis, extractor).

    trafilatura gives the main text. The block text (every block-level element, page chrome pruned) replaces it
    when trafilatura fails, returns under MIN_TRAFILATURA_CHARS, or dropped an AI-bearing block of the page
    (builders such as Elementor keep body copy in bare <div>s, which trafilatura skips).
    """
    import lxml

    html = _XML_DECL.sub("", html, count=1)  # lxml refuses a str that declares an encoding (SEC inline XBRL)
    tree = _parse_html(html)
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
    blocks_text = _lxml_text(_parse_html(html))
    if len(text.strip()) < MIN_TRAFILATURA_CHARS and len(blocks_text) > len(text.strip()):
        return blocks_text, title, published, basis, f"lxml-blocks {lxml.__version__}"
    if text.strip() and missed_ai_blocks(text, blocks_text.split("\n\n"), lexicon):
        return blocks_text, title, published, basis, f"lxml-blocks {lxml.__version__} (trafilatura missed AI text)"
    if not text.strip():
        return blocks_text, title, published, basis, f"lxml-blocks {lxml.__version__}"
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
