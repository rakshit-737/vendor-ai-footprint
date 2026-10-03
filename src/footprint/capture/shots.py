"""Excerpt-anchored screenshots (design: Traceability & workbook I/O; docs/contracts_p3.md section 12).

``screenshot_excerpt`` turns one verified excerpt into a PNG under ``evidence/shots/`` and returns the contract's
``(repo-relative path, sha256 of the PNG bytes, visible_in_render)`` as a ``ShotResult``:

* **PDF** captures are rendered offline from the stored bytes with pypdfium2. The page that holds the excerpt
  (``Document.pages`` gives the hint) is rendered, the excerpt's character boxes are highlighted and the image is
  cropped around them. No request is made, so the PNG is reproducible.
* **Manual-only hosts** (fiserv.com, linkedin.com and every ``automation = "none"`` entry of the ToS register) are
  never loaded. A manual capture's own screenshot is reused with visible None; without one the result is
  ("", "", None).
* **HTML** pages are loaded in Playwright Chromium (sync API) with the honest UA. The page load passes the same
  gates as a collector request (``ShotPolicy.check``: ToS register -> robots.txt -> rate-limit wait, counted in the
  per-run host budget), and so does every redirect hop and every later main-frame navigation. Chromium follows a
  redirect without consulting the request router, so redirects are never handed to it: documents are fetched
  with ``max_redirects=0``, a 3xx aborts the load, and the target is gated and navigated to explicitly; an asset
  that redirects is refused. Requests the page makes itself go through ``route_decision``: GET/HEAD only; no
  sub-frames, media, event streams or beacons; no tracker, analytics, chat or marketing hosts; no third-party
  scripts. First-party assets also need robots.txt permission, and third-party stylesheets, fonts and images need
  a ``full`` ToS entry plus robots permission. WebSockets, which the request router never sees, are mocked inside
  the page (``route_web_socket``) and never connect. Pages of hosts with a per-run request cap render "lean" (the
  document alone), so a screenshot costs one request of the budget; so does every page when the policy has no
  ``allows_subresource`` asset gate (a bare ``ShotPolicy``), because then nothing can check robots.txt for assets.
  After the load event and a short delay the excerpt is found in the DOM text (whitespace, punctuation, case and
  accents normalised), highlighted (a CSS custom highlight styled through a constructable stylesheet, which a
  strict page CSP does not block), centred and clipped with a margin. No clip, viewport or render is taller than
  ``MAX_SHOT_PX`` (16384 px, Chromium's limit) and no full-page screenshot is ever taken. Not found:
  ("", "", False).
* Other kinds (JSON, DNS, plain text) have no page to render.

Nothing here raises for an operational failure: the result has an empty path and ``ShotResult.reason`` says why.
Every result is appended to ``evidence/shots/index.jsonl``; replay calls ``find_shot`` and never renders.
"""

from __future__ import annotations

import bisect
import io
import ipaddress
import json
import math
import os
import re
import unicodedata
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from footprint.capture.store import dumps_sorted, sha256_hex
from footprint.collectors.base import MANUAL_ONLY_HOSTS
from footprint.models import Capture, Document, EvidenceItem, sha256_text
from footprint.net.tou import HARD_MANUAL, TouRegister, load_tou

SHOTS_INDEX = "shots/index.jsonl"   # under the store root
MAX_SHOT_PX = 16384                 # Chromium's texture limit: no clip, viewport or render is taller
VIEWPORT = {"width": 1280, "height": 900}
SETTLE_MS = 1500                    # short delay after the load event
SCROLL_SETTLE_MS = 250              # after scrolling to the excerpt (lazy images, sticky headers)
NAV_TIMEOUT_MS = 30_000
MARGIN_X = 32                       # context kept around the excerpt in a page shot (CSS px)
MARGIN_Y = 120
MIN_CLIP_WIDTH = 480
MAX_NAVIGATIONS = 6                 # main-frame document requests per load: the page plus redirects
MAX_SUBRESOURCES = 80               # first-party asset requests per page load
MAX_CANDIDATES = 10                 # DOM occurrences of an excerpt that are tried
MIN_KEY_CHARS = 4                   # shorter excerpts cannot be anchored
ANCHOR_CHARS = 24
FUZZY_MIN_SCORE = 90.0
FUZZY_MAX_TEXT = 400_000
FUZZY_MAX_EXCERPT = 2_000
PDF_SCALE = 2.0                     # 144 dpi
PDF_MAX_RENDER_PX = 6000
PDF_MARGIN_PX = (48, 120)
PDF_MIN_WIDTH_PX = 600
PDF_PAGE_FIT_PX = 1400              # width of a full-page fallback render
PDF_TINT = (255, 226, 92)           # multiplied into the excerpt's line boxes (keeps the text dark)
OUTLINE_RGB = (215, 38, 61)

FIRST_PARTY_TYPES = frozenset({"stylesheet", "script", "font", "image", "xhr", "fetch"})
THIRD_PARTY_TYPES = frozenset({"stylesheet", "font", "image"})
MANUAL_ONLY: tuple[str, ...] = tuple(sorted(set(HARD_MANUAL) | set(MANUAL_ONLY_HOSTS)))

# Analytics, tag managers, session replay, advertising, marketing automation, chat widgets and consent managers.
# Requests to these hosts (and their subdomains) are always aborted, first- or third-party.
TRACKER_DOMAINS: frozenset[str] = frozenset({
    # analytics, tag managers, monitoring
    "google-analytics.com", "googletagmanager.com", "googletagservices.com", "analytics.google.com",
    "googleadservices.com", "googlesyndication.com", "doubleclick.net", "adservice.google.com",
    "segment.com", "segment.io", "mixpanel.com", "amplitude.com", "heap.io", "heapanalytics.com", "pendo.io",
    "kissmetrics.com", "chartbeat.com", "chartbeat.net", "parsely.com", "newrelic.com", "nr-data.net",
    "datadoghq-browser-agent.com", "browser-intake-datadoghq.com", "sentry.io", "sentry-cdn.com", "bugsnag.com",
    "adobedtm.com", "demdex.net", "omtrdc.net", "everesttech.net", "2o7.net", "tealiumiq.com", "tiqcdn.com",
    "matomo.cloud", "statcounter.com", "quantserve.com", "scorecardresearch.com", "stats.wp.com", "pixel.wp.com",
    "siteimprove.com", "siteimproveanalytics.com", "siteimproveanalytics.io", "monsido.com", "cloudflareinsights.com",
    "plausible.io", "clicky.com", "woopra.com",
    # session replay and experimentation
    "hotjar.com", "hotjar.io", "mouseflow.com", "fullstory.com", "crazyegg.com", "luckyorange.com",
    "luckyorange.net", "clarity.ms", "smartlook.com", "logrocket.com", "logrocket.io", "lr-ingest.io",
    "quantummetric.com", "contentsquare.net", "decibelinsight.net", "glassboxdigital.io", "optimizely.com",
    "visualwebsiteoptimizer.com", "abtasty.com", "kameleoon.com", "kameleoon.eu",
    # advertising and social pixels
    "facebook.net", "facebook.com", "fbcdn.net", "licdn.com", "ads-twitter.com", "analytics.twitter.com",
    "bat.bing.com", "ads.linkedin.com", "adroll.com", "taboola.com", "outbrain.com", "criteo.com", "criteo.net",
    "bluekai.com", "krxd.net", "rlcdn.com", "casalemedia.com", "adnxs.com", "rubiconproject.com", "pubmatic.com",
    "openx.net", "analytics.yahoo.com", "analytics.tiktok.com", "sc-static.net", "ct.pinterest.com",
    "redditstatic.com", "alb.reddit.com", "q.quora.com", "ml314.com", "agkn.com", "tapad.com",
    # marketing automation and account intelligence
    "marketo.net", "marketo.com", "mktoresp.com", "mktoweb.com", "pardot.com", "hubspot.com", "hs-scripts.com",
    "hs-analytics.net", "hsforms.net", "hsforms.com", "hscollectedforms.net", "hs-banner.com", "hsadspixel.net",
    "usemessages.com", "eloqua.com", "en25.com", "act-on.com", "actonsoftware.com", "exacttarget.com",
    "6sc.co", "6sense.com", "demandbase.com", "company-target.com", "bizible.com", "clearbit.com",
    "clearbitjs.com", "zoominfo.com", "lfeeder.com", "leadfeeder.com", "terminus.services", "g2crowd.com",
    # chat and support widgets
    "intercom.io", "intercomcdn.com", "intercomassets.com", "drift.com", "driftt.com", "livechatinc.com",
    "livechat.com", "tawk.to", "crisp.chat", "olark.com", "zopim.com", "zdassets.com", "zendesk.com",
    "freshchat.com", "qualified.com", "liveperson.net", "lpsnmedia.net", "salesforceliveagent.com", "tidio.co",
    "tidiochat.com", "userlike.com", "smartsupp.com",
    # consent and accessibility overlays (banners only cover the page)
    "cookielaw.org", "onetrust.com", "cookiebot.com", "consensu.org", "trustarc.com", "truste.com", "termly.io",
    "iubenda.com", "usercentrics.eu", "osano.com", "cookieyes.com", "didomi.io", "userway.org", "acsbapp.com",
    "accessibe.com", "audioeye.com",
    # surveys and feedback
    "qualtrics.com", "foresee.com", "medallia.com", "kampyle.com", "usabilla.com", "surveymonkey.com",
})
TRACKER_HOST_PREFIXES: tuple[str, ...] = (
    "smetrics.", "metrics.", "analytics.", "tracking.", "track.", "pixel.", "collect.", "telemetry.", "beacon.",
)
TRACKER_PATH_MARKERS: tuple[str, ...] = (
    "/b/ss/", "/g/collect", "/j/collect", "/gtm.js", "/gtag/js", "/analytics.js", "/fbevents.js",
)

# Second-level public suffixes, so that ``site_of("a.example.co.uk")`` is "example.co.uk".
MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "com.au", "net.au", "org.au", "edu.au",
    "gov.au", "co.nz", "org.nz", "co.in", "net.in", "org.in", "gov.in", "co.jp", "or.jp", "ne.jp", "com.br",
    "com.cn", "com.hk", "com.sg", "com.my", "com.tw", "com.mx", "com.ar", "com.tr", "co.za", "co.kr", "co.il",
    "co.id", "co.th", "com.ph", "com.vn", "com.pk", "com.ng", "com.eg", "com.sa", "com.co", "com.pe",
})


class ShotPolicy(Protocol):
    def check(self, url: str) -> tuple[bool, str]:  # ToS register -> robots -> rate-limit wait; counts the request
        ...


# =========================================================================== results


class ShotResult(tuple):
    """``(path, sha256, visible)``: the contract's 3-tuple, with how and why kept as attributes.

    It unpacks and compares like the plain tuple, so ``ShotResult("", "", False, reason="...") == ("", "", False)``.
    ``reason`` is "" for a clean shot; otherwise it says why there is no shot or why the excerpt is not fully
    visible. ``method`` is chromium, pdfium, manual or none; ``page`` is the 1-based PDF page (0 otherwise);
    ``match`` is how the excerpt was located (exact, anchors, partial or fuzzy:NN).
    """

    def __new__(cls, path: str = "", sha256: str = "", visible: bool | None = None, *, reason: str = "",
                method: str = "", page: int = 0, match: str = "") -> ShotResult:
        self = super().__new__(cls, (str(path), str(sha256), visible))
        self.reason = reason
        self.method = method
        self.page = int(page or 0)
        self.match = match
        return self

    def __getnewargs_ex__(self) -> tuple[tuple, dict]:
        return tuple(self), {"reason": self.reason, "method": self.method, "page": self.page, "match": self.match}

    @property
    def path(self) -> str:
        return self[0]

    @property
    def sha256(self) -> str:
        return self[1]

    @property
    def visible(self) -> bool | None:
        return self[2]

    @property
    def ok(self) -> bool:
        """A PNG exists for the excerpt (it may still be marked not visible)."""
        return bool(self[0])

    def __repr__(self) -> str:
        return (f"ShotResult(path={self[0]!r}, sha256={self[1][:12]!r}, visible={self[2]!r}, "
                f"method={self.method!r}, reason={self.reason!r})")


def shot_key(capture: Capture, excerpt: str) -> str:
    """The item key a shot is indexed under (EvidenceItem.make_key over the retrieved URL)."""
    return EvidenceItem.make_key(capture.url_final or capture.url_requested, sha256_text(excerpt))


# =========================================================================== excerpt location (pure)


class _KeyTable(dict):
    """``str.translate`` table: each character -> its comparison key (alphanumerics only, lower-cased, no accents,
    compatibility forms unfolded), or None to drop it. Filled lazily and cached."""

    def __missing__(self, cp: int) -> str | None:
        s = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", chr(cp)).casefold())
        out = "".join(c for c in s if c.isalnum() and not unicodedata.combining(c)) or None
        self[cp] = out
        return out


_KEY = _KeyTable({cp: (chr(cp).lower() if chr(cp).isalnum() else None) for cp in range(128)})


def normalise_key(text: str) -> str:
    """Comparison key: only letters and digits, case-folded, accents and compatibility forms removed.

    Whitespace, punctuation, bullets, table pipes, quotes and dashes all drop out, so the extracted text of a page
    (trafilatura or lxml) and its DOM text compare equal even where blocks were joined or split differently.
    """
    return text.translate(_KEY)


def _key_map(text: str) -> list[int]:
    """For each key character of ``text``, the index of the source character it came from."""
    out: list[int] = []
    for i, ch in enumerate(text):
        v = _KEY[ord(ch)]
        if v:
            out.extend([i] * len(v))
    return out


@dataclass(frozen=True)
class Span:
    """An excerpt located in a list of text nodes: [start_node:start_offset] .. [end_node:end_offset) (code
    points), with how it matched: exact, anchors (both ends exact, length plausible), partial (the excerpt runs
    off the start or end of the text, e.g. a PDF page break) or fuzzy:NN."""

    start_node: int
    start_offset: int
    end_node: int
    end_offset: int
    match: str = "exact"


DEFAULT_MODES: tuple[str, ...] = ("exact", "anchors", "fuzzy")


def _find_all(hay: str, needle: str, cap: int) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    i = hay.find(needle)
    while i != -1 and len(out) < cap:
        out.append((i, i + len(needle), "exact"))
        i = hay.find(needle, i + len(needle))
    return out


def _anchored(hay: str, ex: str, cap: int) -> list[tuple[int, int, str]]:
    n = len(ex)
    a = min(ANCHOR_CHARS, n // 3)
    if a < 8:
        return []
    head, tail = ex[:a], ex[-a:]
    out: list[tuple[int, int, str]] = []
    i = hay.find(head)
    while i != -1 and len(out) < cap:
        lo = max(i + a, i + math.ceil(0.8 * n) - a)
        j = hay.find(tail, lo, i + int(1.25 * n) + 1)
        if j != -1:
            out.append((i, j + a, "anchors"))
            i = hay.find(head, j + a)
        else:
            i = hay.find(head, i + 1)
    return out


def _partial(hay: str, ex: str, cap: int) -> list[tuple[int, int, str]]:
    """A leading (else trailing) part of the excerpt, at least 30% of it and 24 key characters, matches exactly:
    the excerpt crosses a page break and only this part is on the page."""
    n = len(ex)
    need = max(min(ANCHOR_CHARS, n), math.ceil(0.3 * n))
    out: list[tuple[int, int, str]] = []
    head = ex[:need]
    i = hay.find(head)
    while i != -1 and len(out) < cap:
        common = len(os.path.commonprefix([hay[i:i + n], ex]))
        out.append((i, i + common, "partial"))
        i = hay.find(head, i + common)
    if out:
        return out
    rev_hay, rev_ex = hay[::-1], ex[::-1]
    tail = rev_ex[:need]
    j = rev_hay.find(tail)
    while j != -1 and len(out) < cap:
        common = len(os.path.commonprefix([rev_hay[j:j + n], rev_ex]))
        out.append((len(hay) - j - common, len(hay) - j, "partial"))
        j = rev_hay.find(tail, j + common)
    return out


def _fuzzy(hay: str, ex: str) -> list[tuple[int, int, str]]:
    if len(ex) < 20 or len(ex) > FUZZY_MAX_EXCERPT or len(hay) > FUZZY_MAX_TEXT or len(hay) < len(ex):
        return []
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - rapidfuzz is a core dependency
        return []
    res = fuzz.partial_ratio_alignment(ex, hay, score_cutoff=FUZZY_MIN_SCORE)
    if res is None or res.score < FUZZY_MIN_SCORE or res.dest_end <= res.dest_start:
        return []
    return [(res.dest_start, res.dest_end, f"fuzzy:{int(res.score)}")]


def _edge_punct(excerpt: str) -> tuple[int, int]:
    """How many punctuation characters open and close the excerpt (an opening quote, a final stop), max 3."""
    s = excerpt.strip()

    def run(chars: Iterator[str]) -> int:
        n = 0
        for c in chars:
            if c.isalnum() or c.isspace() or n == 3:
                break
            n += 1
        return n

    return run(iter(s)), run(reversed(s))


def locate(texts: Sequence[str], excerpt: str, *, prefer: int = 0, modes: Sequence[str] = DEFAULT_MODES,
           limit: int = MAX_CANDIDATES) -> list[Span]:
    """Where ``excerpt`` occurs in the concatenation of ``texts`` (DOM text nodes, or one PDF page's text).

    Matching compares ``normalise_key`` forms, so whitespace, punctuation and node boundaries do not matter. The
    first mode in ``modes`` that finds anything wins: exact occurrences, then ``anchors`` (the first and last 24
    key characters exact, 80-125% of the excerpt's length apart; covers inserted footnote markers or dropped
    inline text), ``partial`` (for page breaks) and ``fuzzy`` (rapidfuzz partial alignment >= 90). Results are in
    document order with occurrence ``prefer`` first; offsets are code points.
    """
    ex = normalise_key(excerpt)
    if len(ex) < MIN_KEY_CHARS:
        return []
    keys = [t.translate(_KEY) for t in texts]
    hay = "".join(keys)
    cum = [0]
    for k in keys:
        cum.append(cum[-1] + len(k))
    hits: list[tuple[int, int, str]] = []
    for mode in modes:
        if mode == "exact":
            hits = _find_all(hay, ex, 1000)
        elif mode == "anchors":
            hits = _anchored(hay, ex, 1000)
        elif mode == "partial":
            hits = _partial(hay, ex, 1000)
        elif mode == "fuzzy":
            hits = _fuzzy(hay, ex)
        else:
            raise ValueError(f"unknown match mode {mode!r}")
        if hits:
            break
    if not hits:
        return []
    if 0 < prefer < len(hits):
        hits = [hits[prefer]] + hits[:prefer] + hits[prefer + 1:]
    lead, trail = _edge_punct(excerpt)
    maps: dict[int, list[int]] = {}

    def char_at(kp: int) -> tuple[int, int]:
        node = bisect.bisect_right(cum, kp) - 1
        if node not in maps:
            maps[node] = _key_map(texts[node])
        return node, maps[node][kp - cum[node]]

    spans: list[Span] = []
    for s, e, how in hits[:limit]:
        a, so = char_at(s)
        b, last = char_at(e - 1)
        eo = last + 1
        if how in ("exact", "anchors"):
            ta, tb = texts[a], texts[b]
            for _ in range(lead):
                if so > 0 and not ta[so - 1].isalnum() and not ta[so - 1].isspace():
                    so -= 1
            for _ in range(trail):
                if eo < len(tb) and not tb[eo].isalnum() and not tb[eo].isspace():
                    eo += 1
        spans.append(Span(a, so, b, eo, how))
    return spans


def occurrence_index(doc_text: str, start: int, excerpt: str) -> int:
    """Which exact occurrence (0-based, in key space) of the excerpt in the extracted text the item cites."""
    ex = normalise_key(excerpt)
    if not ex or not doc_text:
        return 0
    before = normalise_key(doc_text[:max(0, start)])
    full = before + normalise_key(doc_text[max(0, start):])
    n, i = 0, full.find(ex)
    while i != -1 and i < len(before):
        n += 1
        i = full.find(ex, i + len(ex))
    return n


def _u16(text: str, i: int) -> int:
    """Code-point offset -> UTF-16 offset (what a DOM Range expects)."""
    return i + sum(1 for ch in text[:i] if ord(ch) > 0xFFFF)


# =========================================================================== request policy (pure)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def site_of(host: str) -> str:
    """Registrable-domain approximation used to tell first- from third-party requests."""
    host = (host or "").lower().strip("[]").rstrip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last2 = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if last2 in MULTI_LABEL_SUFFIXES else last2


def _under(host: str, domains: Collection[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_tracker(host: str, url: str = "") -> bool:
    """Analytics, advertising, marketing, chat, consent or session-replay endpoint (host, CNAME prefix or path)."""
    host = (host or "").lower()
    if _under(host, TRACKER_DOMAINS) or host.startswith(TRACKER_HOST_PREFIXES):
        return True
    low = url.lower()
    return any(m in low for m in TRACKER_PATH_MARKERS)


def route_decision(url: str, resource_type: str, method: str, page_url: str, *, navigation: bool,
                   main_frame: bool) -> tuple[str, str]:
    """Classify one request made while rendering a page.

    Returns ``("navigate", "")`` for a main-frame document (it must pass the full policy gate), ``("first_party",
    "")`` or ``("third_party", "")`` for an asset that may load once robots/ToS allow it, ``("local", "")`` for
    data:/blob: URLs, or ``("block", why)``.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in ("data", "blob", "about"):
        return "local", ""
    if scheme not in ("http", "https"):
        return "block", f"scheme {scheme or '?'}"
    if (method or "GET").upper() not in ("GET", "HEAD"):
        return "block", f"method {method.upper()}"
    rtype = (resource_type or "other").lower()
    if navigation or rtype == "document":
        return ("navigate", "") if main_frame else ("block", "sub-frame")
    host = (parts.hostname or "").lower()
    if _under(host, MANUAL_ONLY):
        return "block", "manual-only host"
    if is_tracker(host, url):
        return "block", "tracker"
    if site_of(host) == site_of(_host(page_url)):
        return ("first_party", "") if rtype in FIRST_PARTY_TYPES else ("block", rtype)
    return ("third_party", "") if rtype in THIRD_PARTY_TYPES else ("block", f"third-party {rtype}")


def clip_box(union: dict, vw: float, vh: float, *, margin_x: float = MARGIN_X, margin_y: float = MARGIN_Y,
             min_width: float = MIN_CLIP_WIDTH, max_height: float = MAX_SHOT_PX) -> dict:
    """Clip (viewport CSS px) around ``union`` {x, y, width, height}: margins, a minimum width, clamped to the
    viewport and never taller than ``max_height``."""
    x0, y0, x1, y1 = _clip_rect(float(union["x"]), float(union["y"]),
                                float(union["x"]) + float(union["width"]),
                                float(union["y"]) + float(union["height"]),
                                float(vw), float(vh), margin_x, margin_y, min_width, max_height)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def needed_height(union: dict, *, margin_y: float = MARGIN_Y) -> int:
    """Viewport height that fits ``union`` plus its margins."""
    return math.ceil(float(union["height"]) + 2 * margin_y)


def _clip_rect(ux0: float, uy0: float, ux1: float, uy1: float, width: float, height: float, mx: float, my: float,
               min_w: float, max_h: float) -> tuple[int, int, int, int]:
    x0, x1 = max(0.0, ux0 - mx), min(width, ux1 + mx)
    if x1 - x0 < min_w:
        cx = (ux0 + ux1) / 2
        x0 = max(0.0, min(cx - min_w / 2, width - min_w))
        x1 = min(width, x0 + min_w)
    y0, y1 = max(0.0, uy0 - my), min(height, uy1 + my)
    y1 = min(y1, y0 + max_h)
    ix0, iy0 = int(math.floor(x0)), int(math.floor(y0))
    ix1 = max(ix0 + 1, min(int(math.ceil(x1)), int(width)))
    iy1 = max(iy0 + 1, min(int(math.ceil(y1)), int(height), iy0 + int(max_h)))
    return ix0, iy0, ix1, iy1


# =========================================================================== gate policy


@lru_cache(maxsize=1)
def _register() -> TouRegister | None:
    try:
        return load_tou()
    except Exception:  # noqa: BLE001 - no register: only the hard-coded manual hosts apply
        return None


def manual_only(url: str, tou: TouRegister | None = None) -> bool:
    """Host is manual capture only: fiserv.com, linkedin.com, lnkd.in, or a register entry with automation none."""
    host = _host(url)
    if not host:
        return False
    if _under(host, MANUAL_ONLY):
        return True
    reg = tou if tou is not None else _register()
    entry = reg.match(host) if reg is not None else None
    return bool(entry is not None and entry.automation == "none")


class GatePolicy:
    """``ShotPolicy`` over the net policy objects: ToS register -> robots.txt (Protego) -> per-host rate limit.

    Build it with ``from_fetcher(live_fetcher)`` so screenshot loads share the run's register, robots cache, rate
    limiter and per-host request counters with collection (FSSI's budget of about 25 requests includes screenshot
    loads), or with ``default()`` for a standalone session. ``seeded`` says which URLs count as seeded for hosts
    with ``automation = "limited"``: a bool, a predicate or a collection of URLs (default: none).
    """

    def __init__(self, tou: TouRegister, robots: Any, ratelimit: Any = None, counts: dict[str, int] | None = None,
                 *, seeded: bool | Callable[[str], bool] | Collection[str] = False,
                 user_agent: str | Callable[[str], str] | None = None) -> None:
        from footprint.net.ratelimit import RateLimiter

        try:
            from footprint.net.robots import UA_TOKEN
        except ImportError:  # pragma: no cover - protego missing; a robots object was injected
            UA_TOKEN = "footprint-osint"
        self.tou = tou
        self.robots = robots
        self.ratelimit = ratelimit if ratelimit is not None else RateLimiter()
        self.counts = counts if counts is not None else {}
        self.ua_token = UA_TOKEN
        self._seeded = seeded
        self._ua = user_agent

    @classmethod
    def from_fetcher(cls, fetcher: Any, *, seeded: bool | Callable[[str], bool] | Collection[str] = False
                     ) -> GatePolicy:
        """Share a ``LiveFetcher``'s register, robots cache, rate limiter, request counters and UA rules."""
        return cls(fetcher.tou, fetcher.robots, fetcher.ratelimit, fetcher.request_counts, seeded=seeded,
                   user_agent=fetcher.ua_for)

    @classmethod
    def default(cls, *, sec_contact: str = "", seeded: bool | Callable[[str], bool] | Collection[str] = False,
                tou: TouRegister | None = None) -> GatePolicy:
        """A standalone policy over a fresh ``LiveFetcher`` (config/tou.toml, its own robots cache and counters)."""
        from footprint.net.fetcher import LiveFetcher

        return cls.from_fetcher(LiveFetcher(None, tou or load_tou(), sec_contact=sec_contact), seeded=seeded)

    def is_seeded(self, url: str) -> bool:
        s = self._seeded
        if isinstance(s, bool):
            return s
        if callable(s):
            return bool(s(url))
        return url in s

    def manual_only(self, url: str) -> bool:
        return manual_only(url, self.tou)

    def capped(self, url: str) -> bool:
        """The host has a per-run request cap: its pages render lean (no subresources)."""
        return self.tou.entry_for(url).max_requests_per_run > 0

    def user_agent(self, url: str) -> str:
        """The honest UA for ``url`` (SEC hosts need the contact address; raises ValueError without it)."""
        if callable(self._ua):
            return self._ua(url)
        if isinstance(self._ua, str) and self._ua:
            return self._ua
        from footprint.net.fetcher import DEFAULT_UA

        return DEFAULT_UA

    def check(self, url: str) -> tuple[bool, str]:
        """One more automated page load of ``url``? ToS register, then robots.txt, then the rate-limit wait; an
        allowed load is counted against the host's per-run budget."""
        from footprint.net.fetcher import is_save_page_now

        if urlsplit(url).scheme.lower() not in ("http", "https") or is_save_page_now(url):
            return False, "blocked_tou: not an allowed GET target"
        if self.manual_only(url):
            return False, f"blocked_tou: {_host(url)} is manual capture only"
        ok, why = self.tou.check(url, self.counts, seeded=self.is_seeded(url))
        if not ok:
            return False, why
        try:
            allowed, _sha = self.robots.allowed(url, self.ua_token)
            delay = self.robots.crawl_delay(url, self.ua_token) if hasattr(self.robots, "crawl_delay") else None
        except Exception:  # noqa: BLE001 - unreachable robots.txt is treated as a refusal
            return False, "network_error: robots.txt unavailable"
        if not allowed:
            return False, "blocked_robots"
        self.ratelimit.wait(_host(url), self.tou.entry_for(url).max_rps, delay)
        key = self.tou.counter_key(url)
        self.counts[key] = self.counts.get(key, 0) + 1
        return True, ""

    def allows_subresource(self, url: str, *, first_party: bool) -> bool:
        """May the page being rendered load this asset? Not counted and not rate-limited (it is part of one page
        view), but never from a manual-only host and always subject to robots.txt. A third-party asset also needs
        a registered ``full`` ToS entry."""
        if urlsplit(url).scheme.lower() not in ("http", "https") or self.manual_only(url):
            return False
        entry = self.tou.match(_host(url))
        if first_party:
            if entry is not None and entry.automation == "none":
                return False
        elif entry is None or entry.automation != "full":
            return False
        try:
            allowed, _sha = self.robots.allowed(url, self.ua_token)
        except Exception:  # noqa: BLE001
            return False
        return bool(allowed)


# =========================================================================== browser


@contextmanager
def chromium(*, headless: bool = True) -> Iterator[Any]:
    """A Playwright Chromium browser for a batch of shots. Sync API: use it from the thread that opened it.

    Raises ImportError or a Playwright error when Playwright or its Chromium build is missing
    (``uv run playwright install chromium``).
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=headless)
    except BaseException:
        with suppress(Exception):
            pw.stop()
        raise
    try:
        yield browser
    finally:
        with suppress(Exception):
            browser.close()
        with suppress(Exception):
            pw.stop()


_JS_LIB = r"""
() => {
  if (window.__fp) return true;
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "TITLE", "IFRAME", "FRAME", "OBJECT",
                        "EMBED", "CANVAS", "VIDEO", "AUDIO", "SELECT", "OPTION", "TEXTAREA"]);
  const S = {nodes: [], range: null, undo: []};
  const elOf = (n) => (!n ? null : (n.nodeType === 1 ? n : n.parentElement));
  const rectsOf = (r) => Array.from(r.getClientRects()).filter((q) => q.width >= 1 && q.height >= 1);
  const unionOf = (rs) => {
    let l = Infinity, t = Infinity, rt = -Infinity, b = -Infinity;
    for (const q of rs) {
      l = Math.min(l, q.left); t = Math.min(t, q.top); rt = Math.max(rt, q.right); b = Math.max(b, q.bottom);
    }
    return {x: l, y: t, width: rt - l, height: b - t};
  };
  const styleSet = (e, p, v) => {
    const ov = e.style.getPropertyValue(p), op = e.style.getPropertyPriority(p);
    e.style.setProperty(p, v, "important");
    S.undo.push(() => { if (ov) e.style.setProperty(p, ov, op); else e.style.removeProperty(p); });
  };
  const visibleEl = (el0) => {
    let el = el0;
    while (el && el.parentElement && getComputedStyle(el).display === "contents") el = el.parentElement;
    if (!el) return false;
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true, opacityProperty: true,
                                 visibilityProperty: true});
    }
    const cs = getComputedStyle(el);
    return cs.display !== "none" && cs.visibility === "visible" && parseFloat(cs.opacity) > 0;
  };
  function textParts(r) {
    const root = r.commonAncestorContainer;
    if (root.nodeType === 3) return root.parentElement ? [[root.parentElement, root.data.trim().length || 1]] : [];
    const out = [];
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      if (out.length >= 500) break;
      const len = n.data.trim().length;
      if (len && n.parentElement && r.intersectsNode(n)) out.push([n.parentElement, len]);
    }
    return out;
  }
  function visibleShare(r) {
    let vis = 0, tot = 0;
    for (const [el, len] of textParts(r)) { tot += len; if (visibleEl(el)) vis += len; }
    return tot ? vis / tot : 0;
  }
  function collect() {
    const nodes = [], texts = [];
    const stack = [document.body || document.documentElement];
    while (stack.length) {
      const n = stack.pop();
      if (!n) continue;
      if (n.nodeType === 3) { if (n.data) { nodes.push(n); texts.push(n.data); } continue; }
      if (n.nodeType === 1 && SKIP.has(String(n.tagName).toUpperCase())) continue;
      if (n.nodeType !== 1 && n.nodeType !== 11) continue;
      const kids = n.childNodes;
      for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
      if (n.nodeType === 1 && n.shadowRoot) stack.push(n.shadowRoot);
    }
    S.nodes = nodes;
    return texts;
  }
  function centre(r, my) {
    const el = elOf(r.startContainer);
    try { el.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"}); } catch (e) {}
    const rs = rectsOf(r);
    if (!rs.length) return false;
    const u = unionOf(rs);
    const dy = (u.height + 2 * my <= innerHeight) ? (u.y + u.height / 2 - innerHeight / 2) : (u.y - my);
    if (Math.abs(dy) >= 1) window.scrollBy({top: dy, left: 0, behavior: "instant"});
    return true;
  }
  function fixedAncestor(el) {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const p = getComputedStyle(e).position;
      if (p === "fixed" || p === "sticky") return e;
    }
    return null;
  }
  function hitTest(r, anc) {
    const rs = rectsOf(r);
    const rn = anc.getRootNode ? anc.getRootNode() : document;
    const root = rn && typeof rn.elementFromPoint === "function" ? rn : document;
    const picks = rs.length ? [rs[0], rs[Math.floor(rs.length / 2)], rs[rs.length - 1]] : [];
    let inView = 0;
    const covers = [];
    for (const q of picks) {
      const x = q.left + q.width / 2, y = q.top + q.height / 2;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      inView++;
      const h = root.elementFromPoint(x, y);
      if (h && (anc.contains(h) || (h.contains(anc) && getComputedStyle(anc).pointerEvents === "none"))) continue;
      covers.push(h);
    }
    return {inView, covers};
  }
  function measure(a) {
    const r = S.range;
    if (!r) return {ok: false, reason: "no range"};
    const anc = elOf(r.commonAncestorContainer) || document.documentElement;
    if (!centre(r, a.my)) return {ok: true, rendered: false, visible: false, reason: "no layout boxes"};
    let ht = hitTest(r, anc);
    for (let round = 0; round < 3 && ht.covers.length; round++) {
      let hid = 0;
      for (const c of ht.covers) {
        const f = c ? fixedAncestor(c) : null;
        if (f && !f.contains(anc) && f !== document.body && f !== document.documentElement) {
          styleSet(f, "visibility", "hidden");
          hid++;
        }
      }
      if (!hid) break;
      ht = hitTest(r, anc);
    }
    const rs = rectsOf(r);
    if (!rs.length) return {ok: true, rendered: false, visible: false, reason: "no layout boxes"};
    const share = visibleShare(r);
    return {ok: true, rendered: true, visible: share >= 0.8 && ht.inView > 0 && ht.covers.length === 0,
            share: share, inView: ht.inView, covered: ht.covers.length, union: unionOf(rs),
            vw: innerWidth, vh: innerHeight};
  }
  function clear() {
    while (S.undo.length) { const f = S.undo.pop(); try { f(); } catch (e) {} }
    S.range = null;
    return true;
  }
  function select(a) {
    clear();
    const sn = S.nodes[a.sn], en = S.nodes[a.en];
    if (!sn || !en || !sn.isConnected || !en.isConnected) return {ok: false, reason: "text node gone"};
    const r = document.createRange();
    try { r.setStart(sn, a.so); r.setEnd(en, a.eo); } catch (e) { return {ok: false, reason: String(e)}; }
    if (r.collapsed) return {ok: false, reason: "empty range"};
    S.range = r;
    return measure(a);
  }
  function reveal(a) {
    const r = S.range;
    if (!r) return {ok: false, reason: "no range"};
    const seen = new Set();
    for (const [el0] of textParts(r)) {
      for (let e = el0; e && e.nodeType === 1 && e !== document.documentElement; e = e.parentElement) {
        if (seen.has(e)) break;
        seen.add(e);
        if (e.tagName === "DETAILS" && !e.open) { e.open = true; S.undo.push(() => { e.open = false; }); }
        if (e.hasAttribute("hidden")) {
          const v = e.getAttribute("hidden");
          e.removeAttribute("hidden");
          S.undo.push(() => e.setAttribute("hidden", v));
        }
        const cs = getComputedStyle(e);
        if (cs.display === "none") styleSet(e, "display", "block");
        if (cs.visibility === "hidden" || cs.visibility === "collapse") styleSet(e, "visibility", "visible");
        if (parseFloat(cs.opacity) === 0) styleSet(e, "opacity", "1");
        const clipped = cs.overflowY !== "visible" || cs.overflowX !== "visible";
        if (clipped && (parseFloat(cs.maxHeight) < 4 || parseFloat(cs.height) < 4)) {
          styleSet(e, "max-height", "none"); styleSet(e, "height", "auto"); styleSet(e, "overflow", "visible");
        }
      }
    }
    return measure(a);
  }
  function addStyle(text) {
    // A constructable stylesheet is CSSOM, so a strict page CSP (style-src without 'unsafe-inline') cannot
    // block it the way it blocks an inserted <style> element.
    try {
      const sh = new CSSStyleSheet();
      sh.replaceSync(text);
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sh];
      S.undo.push(() => { document.adoptedStyleSheets = document.adoptedStyleSheets.filter((x) => x !== sh); });
      return sh.cssRules.length > 0;
    } catch (e) {}
    try {
      const st = document.createElement("style");
      st.textContent = text;
      (document.head || document.documentElement).appendChild(st);
      S.undo.push(() => st.remove());
      return !!(st.sheet && st.sheet.cssRules.length);
    } catch (e) { return false; }
  }
  function mark(a) {
    const r = S.range;
    if (!r) return {ok: false, reason: "no range"};
    let css = false;
    const inShadow = r.startContainer.getRootNode() !== document || r.endContainer.getRootNode() !== document;
    try {
      if (!inShadow && window.CSS && CSS.highlights && typeof Highlight === "function" &&
          addStyle("::highlight(fp-excerpt){background-color:rgba(255,212,0,0.6);color:inherit;}")) {
        CSS.highlights.set("fp-excerpt", new Highlight(r));
        css = true;
        S.undo.push(() => CSS.highlights.delete("fp-excerpt"));
      }
    } catch (e) { css = false; }
    const rs = rectsOf(r);
    const box = (q, extra) => {
      const d = document.createElement("div");
      d.style.cssText = "all:initial;position:fixed;pointer-events:none;z-index:2147483647;box-sizing:border-box;" +
        `left:${q.x}px;top:${q.y}px;width:${q.width}px;height:${q.height}px;` + extra;
      document.documentElement.appendChild(d);
      S.undo.push(() => d.remove());
    };
    if (!css) {
      for (const q of rs) {
        box({x: q.left, y: q.top, width: q.width, height: q.height},
            "background:rgba(255,212,0,0.6);mix-blend-mode:multiply;");
      }
    }
    const u = unionOf(rs);
    box({x: u.x - 5, y: u.y - 5, width: u.width + 10, height: u.height + 10},
        "border:3px solid rgb(215,38,61);border-radius:4px;");
    return {ok: true, highlight: css ? "css" : "overlay", union: u};
  }
  window.__fp = {collect, select, measure, reveal, mark, clear};
  return true;
}
"""

JS_INSTALL = _JS_LIB.strip()
JS_COLLECT = ("async () => { try { await Promise.race([document.fonts ? document.fonts.ready : Promise.resolve(), "
              "new Promise((r) => setTimeout(r, 1500))]); } catch (e) {} return window.__fp.collect(); }")
JS_SELECT = "(a) => window.__fp.select(a)"
JS_MEASURE = "(a) => window.__fp.measure(a)"
JS_REVEAL = "(a) => window.__fp.reveal(a)"
JS_MARK = "(a) => window.__fp.mark(a)"
JS_CLEAR = "() => window.__fp.clear()"


@dataclass
class _LoadState:
    """One page load: which navigation was gated already, and what the router refused (for the reason)."""

    page_url: str
    pregated: str
    lean: bool = False
    nav_block: str = ""
    redirect_to: str = ""
    main_ok: bool = False
    main_status: int = 0
    navigations: int = 0
    subresources: int = 0
    blocked: list[str] = field(default_factory=list)


def _norm_url(u: str) -> tuple[str, str, str, str]:
    s = urlsplit(u)
    scheme = s.scheme.lower()
    host = (s.hostname or "").lower()
    try:
        port = s.port
    except ValueError:
        port = None
    netloc = host if port in (None, {"http": 80, "https": 443}.get(scheme)) else f"{host}:{port}"
    return scheme, netloc, s.path or "/", s.query


def _first_line(e: BaseException) -> str:
    lines = [ln.strip() for ln in str(e).strip().splitlines() if ln.strip()]
    return (lines[0] if lines else type(e).__name__)[:200]


def _abort(route: Any, state: _LoadState, url: str, why: str) -> None:
    state.blocked.append(f"{why}: {url[:200]}")
    with suppress(Exception):
        route.abort("blockedbyclient")


def _navigate(policy: Any, state: _LoadState, route: Any, url: str, timeout_ms: int) -> None:
    """A main-frame document request: gate it (unless gated just before ``goto``), fetch it without following
    redirects, and hand the browser only a final, non-redirect response."""
    from footprint.net.fetcher import is_bot_wall

    state.navigations += 1
    if state.navigations > MAX_NAVIGATIONS:
        state.nav_block = state.nav_block or "too many navigations"
        _abort(route, state, url, "navigation cap")
        return
    if state.pregated and _norm_url(url) == _norm_url(state.pregated):
        state.pregated = ""
    else:
        ok, why = policy.check(url)
        if not ok:
            state.nav_block = state.nav_block or f"{why or 'blocked'} (navigation to {url})"
            _abort(route, state, url, why or "blocked")
            return
    resp = route.fetch(max_redirects=0, timeout=timeout_ms)
    status, headers = int(resp.status), {str(k).lower(): str(v) for k, v in dict(resp.headers).items()}
    if 300 <= status < 400 and headers.get("location"):
        # Chromium follows a redirect, even a fulfilled one, without asking the router again, so a 3xx is never
        # handed over: the load is aborted and _html_batch gates the target and navigates to it itself.
        if not state.main_ok:
            state.redirect_to = urljoin(url, headers["location"])
        _abort(route, state, url, f"redirect {status}")
        return
    if is_bot_wall(status, headers, resp.body()):
        state.nav_block = state.nav_block or "blocked_bot"
        _abort(route, state, url, "bot wall")
        return
    if status >= 400:
        state.nav_block = state.nav_block or f"http {status}"
        _abort(route, state, url, f"http {status}")
        return
    state.page_url = url
    if not state.main_ok:
        state.main_ok, state.main_status = True, status
    route.fulfill(response=resp)


def _router(policy: Any, state: _LoadState, page: Any, timeout_ms: int) -> Callable[[Any, Any], None]:
    allows = getattr(policy, "allows_subresource", None)

    def handle(route: Any, request: Any) -> None:
        url = str(request.url)
        try:
            frame = None
            with suppress(Exception):
                frame = request.frame
            if frame is not None and getattr(frame, "page", page) is not page:
                _abort(route, state, url, "other page")
                return
            nav = False
            with suppress(Exception):
                nav = bool(request.is_navigation_request())
            main = bool(nav and frame is not None and getattr(frame, "parent_frame", None) is None)
            verdict, why = route_decision(url, str(request.resource_type), str(request.method), state.page_url,
                                          navigation=nav, main_frame=main)
            if verdict == "navigate":
                _navigate(policy, state, route, url, timeout_ms)
                return
            if verdict == "local":
                route.continue_()
                return
            if verdict == "block":
                _abort(route, state, url, why)
                return
            first = verdict == "first_party"
            if state.lean:
                _abort(route, state, url, "lean render")
                return
            if state.subresources >= MAX_SUBRESOURCES:
                _abort(route, state, url, "subresource cap")
                return
            permitted = callable(allows) and allows(url, first_party=first)
            if not permitted:
                _abort(route, state, url, "robots or terms")
                return
            state.subresources += 1
            resp = route.fetch(max_redirects=0, timeout=timeout_ms)
            if 300 <= int(resp.status) < 400:   # the browser would follow it unchecked
                _abort(route, state, url, "asset redirect")
                return
            route.fulfill(response=resp)
        except Exception as e:  # noqa: BLE001 - a handler error must never let a request through
            if not state.main_ok and not state.nav_block:
                state.nav_block = f"network_error: {_first_line(e)}"
            _abort(route, state, url, "handler error")

    return handle


def _socket_guard(state: _LoadState) -> Callable[[Any], None]:
    """Handler for ``BrowserContext.route_web_socket``. ``context.route`` never sees WebSocket handshakes, so every
    socket the page opens is routed here instead. The handler never calls ``connect_to_server``: Playwright then
    mocks the socket inside the page, no connection to any server is made and the page's messages are dropped.
    It must not call back into Playwright either (``ws.close()`` from this sync callback deadlocks the load)."""

    def handle(ws: Any) -> None:
        url = ""
        with suppress(Exception):
            url = str(ws.url)
        state.blocked.append(f"websocket: {url[:200]}")

    return handle


def _any_url(_url: str) -> bool:
    return True


def _user_agent(policy: Any, url: str) -> str:
    ua = getattr(policy, "user_agent", None)
    if callable(ua):
        return str(ua(url))
    if isinstance(ua, str) and ua:
        return ua
    host = _host(url)
    if host == "sec.gov" or host.endswith(".sec.gov"):
        raise ValueError("SEC pages need a contact User-Agent (use GatePolicy.from_fetcher)")
    from footprint.net.fetcher import DEFAULT_UA

    return DEFAULT_UA


def _lean_note(policy: Any, url: str) -> str:
    """Why the page renders as its document alone ("" for a full render). A host with a per-run request cap keeps
    a screenshot to one request; a policy without ``allows_subresource`` cannot check robots.txt and the ToS
    register for page assets, so none are loaded."""
    capped = getattr(policy, "capped", None)
    if callable(capped) and capped(url):
        return "lean render: page document only (host has a per-run request cap)"
    if not callable(getattr(policy, "allows_subresource", None)):
        return "lean render: page document only (the policy cannot gate page assets)"
    return ""


def _is_timeout(e: BaseException) -> bool:
    return "timeout" in type(e).__name__.lower()


def _doc_text(store: Any, document: Document | None) -> str:
    if document is None or not document.doc_id:
        return ""
    try:
        return str(store.get_text(document.doc_id))
    except Exception:  # noqa: BLE001
        return ""


def _js_args(sp: Span, texts: Sequence[str]) -> dict:
    return {"sn": sp.start_node, "so": _u16(texts[sp.start_node], sp.start_offset),
            "en": sp.end_node, "eo": _u16(texts[sp.end_node], sp.end_offset), "my": MARGIN_Y}


def _shoot_dom(page: Any, texts: Sequence[str], excerpt: str, prefer: int, store: Any, note: str) -> ShotResult:
    spans = locate(texts, excerpt, prefer=prefer)
    if not spans:
        return ShotResult("", "", False, reason=_join(note, "excerpt not found in the rendered page"),
                          method="chromium")
    chosen: Span | None = None
    m: dict = {}
    fallback: tuple[Span, dict] | None = None
    for sp in spans:
        r = page.evaluate(JS_SELECT, _js_args(sp, texts))
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        if r.get("visible"):
            chosen, m = sp, r
            break
        if fallback is None:
            fallback = (sp, r)
    revealed = False
    if chosen is None:
        if fallback is None:
            return ShotResult("", "", False, reason=_join(note, "excerpt found in the DOM but not selectable"),
                              method="chromium", match=spans[0].match)
        sp, _first = fallback
        r = page.evaluate(JS_SELECT, _js_args(sp, texts))
        if not (isinstance(r, dict) and r.get("rendered") and float(r.get("share", 0)) >= 0.8):
            r = page.evaluate(JS_REVEAL, {"my": MARGIN_Y})
            revealed = True
        if not (isinstance(r, dict) and r.get("ok") and r.get("rendered")):
            with suppress(Exception):
                page.evaluate(JS_CLEAR)
            return ShotResult("", "", False, reason=_join(note, "excerpt hidden in the rendered page"),
                              method="chromium", match=sp.match)
        chosen, m = sp, r
    resized = False
    try:
        if needed_height(m["union"]) > float(m["vh"]):
            page.set_viewport_size({"width": VIEWPORT["width"],
                                    "height": min(MAX_SHOT_PX, needed_height(m["union"]))})
            resized = True
        page.wait_for_timeout(SCROLL_SETTLE_MS)
        m2 = page.evaluate(JS_MEASURE, {"my": MARGIN_Y})
        if isinstance(m2, dict) and m2.get("ok") and m2.get("rendered"):
            m = m2
        page.evaluate(JS_MARK, {})
        clip = clip_box(m["union"], m["vw"], m["vh"])
        png = page.screenshot(clip=clip, type="png", animations="disabled", caret="hide")
    finally:
        with suppress(Exception):
            page.evaluate(JS_CLEAR)
        if resized:
            with suppress(Exception):
                page.set_viewport_size(dict(VIEWPORT))
    visible = bool(m.get("visible")) and not revealed
    why = ""
    if revealed:
        why = "excerpt hidden in the default render; revealed for the shot"
    elif not visible:
        why = "excerpt covered, clipped or off-screen in the render"
    path = store.put_shot(png, ".png")
    return ShotResult(path, sha256_hex(png), visible, reason=_join(note, why), method="chromium",
                      match=chosen.match)


def _join(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def _html_batch(capture: Capture, jobs: Sequence[tuple[str, int, int]], store: Any, policy: Any,
                document: Document | None, browser: Any, settle_ms: int, timeout_ms: int) -> list[ShotResult]:
    url = capture.url_final or capture.url_requested

    def fail(reason: str, visible: bool | None = None) -> list[ShotResult]:
        return [ShotResult("", "", visible, reason=reason, method="chromium") for _ in jobs]

    if policy is None:
        return fail("no policy: live page loads are disabled")
    try:
        ua = _user_agent(policy, url)
    except ValueError as e:
        return fail(f"blocked_tou: {e}")
    ok, why = policy.check(url)
    if not ok:
        return fail(why or "blocked")
    own = None
    if browser is None:
        try:
            own = chromium()
            browser = own.__enter__()
        except Exception as e:  # noqa: BLE001
            return fail(f"chromium unavailable: {_first_line(e)}")
    try:
        context = browser.new_context(
            user_agent=ua, viewport=dict(VIEWPORT), device_scale_factor=1, service_workers="block",
            accept_downloads=False, java_script_enabled=True, locale="en-US", timezone_id="UTC",
            color_scheme="light", reduced_motion="reduce",
        )
        try:
            page = context.new_page()
            note = _lean_note(policy, url)
            state = _LoadState(page_url=url, pregated=url, lean=bool(note))
            context.route("**/*", _router(policy, state, page, timeout_ms))
            route_ws = getattr(context, "route_web_socket", None)   # Playwright >= 1.48
            if callable(route_ws):
                route_ws(_any_url, _socket_guard(state))
            resp, target = None, url
            for hop in range(MAX_NAVIGATIONS):
                state.redirect_to = ""
                try:
                    resp = page.goto(target, wait_until="load", timeout=timeout_ms)
                    break
                except Exception as e:  # noqa: BLE001
                    if state.main_ok:
                        if not _is_timeout(e):
                            return fail(f"navigation error: {_first_line(e)}")
                        note = _join(note, "load event timed out")
                        break
                    if not state.redirect_to or state.nav_block:
                        return fail(state.nav_block or f"navigation error: {_first_line(e)}")
                    if hop == MAX_NAVIGATIONS - 1:
                        return fail("too many redirects")
                    nxt = state.redirect_to
                    ok, why = policy.check(nxt)       # every redirect hop passes the same gate as the page
                    if not ok:
                        return fail(f"{why or 'blocked'} (redirect to {nxt})")
                    state.pregated = target = nxt
            if not state.main_ok:
                return fail(state.nav_block or "navigation refused")
            status = int(getattr(resp, "status", 0) or state.main_status)
            if status >= 400:
                return fail(f"http {status}")
            page.wait_for_timeout(settle_ms)
            page.evaluate(JS_INSTALL)
            texts = page.evaluate(JS_COLLECT)
            if not isinstance(texts, list):
                return fail("could not read the rendered page")
            texts = [str(t) for t in texts]
            doc_text = _doc_text(store, document)
            out = []
            for excerpt, start, _end in jobs:
                prefer = occurrence_index(doc_text, start, excerpt) if doc_text else 0
                out.append(_shoot_dom(page, texts, excerpt, prefer, store, note))
            return out
        finally:
            with suppress(Exception):
                context.close()
    except Exception as e:  # noqa: BLE001 - one page's failure never stops a run
        return fail(f"browser error: {_first_line(e)}")
    finally:
        if own is not None:
            own.__exit__(None, None, None)


# =========================================================================== PDF (offline, pypdfium2)


@dataclass(frozen=True)
class PdfShot:
    png: bytes
    visible: bool | None
    page: int = 0          # 1-based page rendered (0 = none)
    match: str = ""
    reason: str = ""


def _page_hint(pages: Sequence[int], start: int, n: int) -> int | None:
    if not pages or n <= 0:
        return None
    return max(0, min(n - 1, bisect.bisect_right(list(pages), start) - 1))


def _search_order(hint: int | None, n: int) -> list[int]:
    if hint is None:
        return list(range(n))
    first = [p for p in (hint, hint + 1, hint - 1) if 0 <= p < n]
    return first + [p for p in range(n) if p not in first]


def _png(img: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


Box = tuple[float, float, float, float]


def _group_lines(rects: Sequence[Box]) -> list[Box]:
    """Merge character boxes (in reading order) into line boxes."""
    lines: list[Box] = []
    for x0, y0, x1, y1 in rects:
        if lines:
            lx0, ly0, lx1, ly1 = lines[-1]
            if abs((y0 + y1) / 2 - (ly0 + ly1) / 2) <= 0.5 * max(y1 - y0, ly1 - ly0):
                lines[-1] = (min(lx0, x0), min(ly0, y0), max(lx1, x1), max(ly1, y1))
                continue
        lines.append((x0, y0, x1, y1))
    return lines


def _group_blocks(lines: Sequence[Box]) -> list[Box]:
    """Merge line boxes into text blocks: lines that overlap horizontally and are at most about a line apart
    (a paragraph, a table cell, a slide column), so each block gets its own outline."""
    blocks: list[Box] = []
    for x0, y0, x1, y1 in lines:
        h = y1 - y0
        for k, (bx0, by0, bx1, by1) in enumerate(blocks):
            overlap = min(x1, bx1) - max(x0, bx0)
            gap = max(by0 - y1, y0 - by1, 0.0)
            if overlap > 0.2 * min(x1 - x0, bx1 - bx0) and gap <= 1.2 * h:
                blocks[k] = (min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1))
                break
        else:
            blocks.append((x0, y0, x1, y1))
    return blocks


class _PdfRenderer:
    """One open PDF; renders excerpt crops from it. Needs pypdfium2 and Pillow (the ``live`` extra)."""

    def __init__(self, raw: bytes) -> None:
        import PIL.Image  # noqa: F401 - fail early when Pillow is missing
        import pypdfium2 as pdfium

        self.pdf = pdfium.PdfDocument(raw)
        self.n = len(self.pdf)
        self._texts: dict[int, str] = {}

    def close(self) -> None:
        with suppress(Exception):
            self.pdf.close()

    @staticmethod
    def _chars(textpage: Any) -> str:
        """The page's characters, one per pdfium char index (so offsets index ``get_charbox`` directly)."""
        import pypdfium2.raw as pdfium_c

        return "".join(chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, i) or 0x20)
                       for i in range(textpage.count_chars()))

    def _find(self, page_no: int, excerpt: str, modes: Sequence[str]) -> tuple[Span, list] | None:
        cached = self._texts.get(page_no)
        if cached is not None and not locate([cached], excerpt, modes=modes, limit=1):
            return None
        page = self.pdf[page_no]
        try:
            tp = page.get_textpage()
            try:
                chars = self._texts[page_no] = cached if cached is not None else self._chars(tp)
                spans = locate([chars], excerpt, modes=modes, limit=1)
                if not spans:
                    return None
                sp = spans[0]
                boxes = []
                for i in range(sp.start_offset, sp.end_offset):
                    if chars[i].isspace() or not chars[i].isprintable():
                        continue
                    left, bottom, right, top = tp.get_charbox(i)
                    if right - left > 0.01 and top - bottom > 0.01:
                        boxes.append((left, bottom, right, top))
                return (sp, boxes) if boxes else None
            finally:
                tp.close()
        finally:
            page.close()

    def _highlight(self, page_no: int, boxes: Sequence[tuple[float, float, float, float]]) -> bytes:
        from PIL import Image, ImageChops, ImageDraw

        page = self.pdf[page_no]
        try:
            w_pt, h_pt = page.get_size()
            scale = min(PDF_SCALE, PDF_MAX_RENDER_PX / max(w_pt, h_pt, 1.0))
            bitmap = page.render(scale=scale)
            try:
                conv = bitmap.get_posconv(page)
                img = bitmap.to_pil().convert("RGB")
                rects = []
                for left, bottom, right, top in boxes:
                    ax, ay = conv.to_bitmap(left, top)
                    bx, by = conv.to_bitmap(right, bottom)
                    rects.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)))
            finally:
                with suppress(Exception):
                    bitmap.close()
        finally:
            page.close()
        lines = _group_lines(rects)
        tint = Image.new("RGB", img.size, (255, 255, 255))
        draw = ImageDraw.Draw(tint)
        for x0, y0, x1, y1 in lines:
            draw.rectangle((x0 - 1, y0 - 2, x1 + 1, y1 + 2), fill=PDF_TINT)
        img = ImageChops.multiply(img, tint)   # text stays dark; the paper under the excerpt turns yellow
        outline = ImageDraw.Draw(img)
        for x0, y0, x1, y1 in _group_blocks(lines):
            outline.rectangle((x0 - 6, y0 - 6, x1 + 6, y1 + 6), outline=OUTLINE_RGB, width=3)
        ux0, uy0 = min(r[0] for r in rects), min(r[1] for r in rects)
        ux1, uy1 = max(r[2] for r in rects), max(r[3] for r in rects)
        crop = _clip_rect(ux0, uy0, ux1, uy1, img.size[0], img.size[1], PDF_MARGIN_PX[0], PDF_MARGIN_PX[1],
                          PDF_MIN_WIDTH_PX, MAX_SHOT_PX)
        return _png(img.crop(crop))

    def _full_page(self, page_no: int) -> bytes:
        page = self.pdf[page_no]
        try:
            w_pt, h_pt = page.get_size()
            scale = min(PDF_SCALE, PDF_PAGE_FIT_PX / max(w_pt, 1.0), MAX_SHOT_PX / max(h_pt, 1.0))
            bitmap = page.render(scale=scale)
            try:
                img = bitmap.to_pil().convert("RGB")
            finally:
                with suppress(Exception):
                    bitmap.close()
        finally:
            page.close()
        return _png(img)

    def shot(self, excerpt: str, start: int, pages: Sequence[int]) -> PdfShot:
        hint = _page_hint(pages, start, self.n)
        crosses = hint is not None and _page_hint(pages, start + max(len(excerpt) - 1, 0), self.n) != hint
        order = _search_order(hint, self.n)
        for modes in (("exact", "anchors"), ("partial", "fuzzy")):
            for page_no in order:
                found = self._find(page_no, excerpt, modes)
                if found is None:
                    continue
                sp, boxes = found
                why = "" if sp.match == "exact" else f"located by {sp.match} match"
                if sp.match == "partial":
                    why = ("excerpt crosses a page break; the part on this page is highlighted" if crosses else
                           "only part of the excerpt matched this page's text order; that part is highlighted")
                return PdfShot(self._highlight(page_no, boxes), True, page_no + 1, sp.match, why)
        if hint is None:
            return PdfShot(b"", False, 0, "", "excerpt not located in the PDF text layer")
        return PdfShot(self._full_page(hint), False, hint + 1, "",
                       f"excerpt not located in the PDF text layer; full page {hint + 1} rendered")


def render_pdf_excerpt(raw: bytes, excerpt: str, *, start: int = 0, pages: Sequence[int] = ()) -> PdfShot:
    """Render the page holding ``excerpt`` (``pages`` = ``Document.pages`` offsets, ``start`` its offset in the
    extracted text), highlight it and crop around it. Offline and deterministic."""
    r = _PdfRenderer(raw)
    try:
        return r.shot(excerpt, start, pages)
    finally:
        r.close()


def _pdf_batch(capture: Capture, jobs: Sequence[tuple[str, int, int]], store: Any,
               document: Document | None) -> list[ShotResult]:
    def fail(reason: str) -> list[ShotResult]:
        return [ShotResult("", "", None, reason=reason, method="pdfium") for _ in jobs]

    try:
        raw = store.get_raw(capture.capture_id)
    except Exception:  # noqa: BLE001
        return fail("raw PDF bytes not in the evidence store")
    try:
        renderer = _PdfRenderer(raw)
    except ImportError as e:
        return fail(f"PDF rendering unavailable: {_first_line(e)}")
    except Exception as e:  # noqa: BLE001
        return fail(f"PDF could not be opened: {_first_line(e)}")
    pages = list(document.pages) if document is not None else []
    out: list[ShotResult] = []
    try:
        for excerpt, start, _end in jobs:
            try:
                shot = renderer.shot(excerpt, start, pages)
            except Exception as e:  # noqa: BLE001
                out.append(ShotResult("", "", None, reason=f"PDF render error: {_first_line(e)}", method="pdfium"))
                continue
            if not shot.png:
                out.append(ShotResult("", "", shot.visible, reason=shot.reason, method="pdfium"))
                continue
            out.append(ShotResult(store.put_shot(shot.png, ".png"), sha256_hex(shot.png), shot.visible,
                                  reason=shot.reason, method="pdfium", page=shot.page, match=shot.match))
    finally:
        renderer.close()
    return out


# =========================================================================== entry points


def _kind_of(capture: Capture, document: Document | None, store: Any) -> str:
    """The document kind, as ``footprint.extract`` decides it."""
    if document is not None and document.kind:
        return document.kind
    ct = (capture.content_type or capture.headers.get("content-type", "")).lower()
    path = (capture.url_final or capture.url_requested).lower().split("?")[0]
    head = b""
    with suppress(Exception):
        head = bytes(store.get_raw(capture.capture_id)[:2048])
    if head.startswith(b"%PDF-") or "pdf" in ct or path.endswith(".pdf"):
        return "pdf"
    if "json" in ct or path.endswith(".json"):
        return "json"
    if "html" in ct or "xml" in ct or re.search(rb"<(html|body|p|div)\b", head, re.I):
        return "html"
    return "text"


def _resolve(path: str, store: Any) -> Path | None:
    p = Path(path)
    root = Path(getattr(store, "root", "evidence"))
    for cand in (p, Path.cwd() / p, root.parent / p, root / "shots" / p.name):
        with suppress(OSError):
            if cand.is_file():
                return cand
    return None


def _manual_shot(capture: Capture, store: Any, reason: str) -> ShotResult:
    if not capture.screenshot_path:
        return ShotResult("", "", None, reason=_join(reason, "no manual screenshot"), method="manual")
    found = _resolve(capture.screenshot_path, store)
    if found is not None:
        sha = sha256_hex(found.read_bytes())
    else:
        stem = Path(capture.screenshot_path).stem
        sha = stem if re.fullmatch(r"[0-9a-f]{64}", stem) else ""
    return ShotResult(capture.screenshot_path, sha, None, reason=_join(reason, "analyst screenshot reused"),
                      method="manual")


def _is_manual_only(policy: Any, url: str) -> bool:
    fn = getattr(policy, "manual_only", None)
    return manual_only(url) or bool(callable(fn) and fn(url))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(store: Any, key: str, capture: Capture, url: str, res: ShotResult) -> None:
    entry = {
        "item_key": key, "capture_id": capture.capture_id, "url": url, "path": res.path, "sha256": res.sha256,
        "visible": res.visible, "taken_at": _now_iso(), "method": res.method, "reason": res.reason,
        "page": res.page, "match": res.match,
    }
    path = Path(store.root) / SHOTS_INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(dumps_sorted(entry) + "\n")


def _job(item: Sequence[Any]) -> tuple[str, int, int]:
    excerpt, start, end = item
    if not isinstance(excerpt, str) or not excerpt:
        raise ValueError("excerpt must be a non-empty string")
    start, end = int(start), int(end)
    if start < 0 or end - start != len(excerpt):
        raise ValueError("excerpt must be the source slice [start:end] (end - start == len(excerpt))")
    return excerpt, start, end


def screenshot_excerpts(capture: Capture, excerpts: Sequence[tuple[str, int, int]], store: Any,
                        policy: ShotPolicy | None, *, document: Document | None = None, browser: Any = None,
                        settle_ms: int = SETTLE_MS, timeout_ms: int = NAV_TIMEOUT_MS) -> list[ShotResult]:
    """Shots for several excerpts of one capture, in input order, loading the page (or opening the PDF) once.

    ``excerpts`` holds ``(excerpt, start, end)`` triples (offsets in the document text). Each result is appended
    to ``SHOTS_INDEX``. See ``screenshot_excerpt``.
    """
    jobs = [_job(e) for e in excerpts]
    if not jobs:
        return []
    url = capture.url_final or capture.url_requested
    kind = _kind_of(capture, document, store)
    if kind == "pdf":
        results = _pdf_batch(capture, jobs, store, document)
    elif _is_manual_only(policy, url):
        results = [_manual_shot(capture, store, "manual-only host: never loaded") for _ in jobs]
    elif kind == "html":
        results = _html_batch(capture, jobs, store, policy, document, browser, settle_ms, timeout_ms)
    else:
        results = [ShotResult("", "", None, reason=f"no page to render for a {kind} capture", method="none")
                   for _ in jobs]
    if capture.screenshot_path:   # nothing could be checked: fall back to the analyst's own screenshot
        results = [_manual_shot(capture, store, r.reason) if (not r.path and r.visible is None
                                                               and r.method != "manual") else r
                   for r in results]
    for (excerpt, _start, _end), res in zip(jobs, results):
        _record(store, shot_key(capture, excerpt), capture, url, res)
    return results


def screenshot_excerpt(capture: Capture, excerpt: str, start: int, end: int, store: Any, policy: ShotPolicy | None,
                       *, document: Document | None = None, browser: Any = None, settle_ms: int = SETTLE_MS,
                       timeout_ms: int = NAV_TIMEOUT_MS) -> ShotResult:
    """Excerpt-anchored screenshot: ``(repo-relative path, sha256 of the PNG, visible_in_render)``.

    ``store`` is the ``EvidenceStore`` (PNG under ``shots/``, index under ``shots/index.jsonl``). ``policy`` gates
    every page load (``GatePolicy.from_fetcher(fetcher)`` in a live run); PDFs and manual-only hosts never use it.
    ``browser`` is a Playwright Chromium ``Browser`` to reuse (see ``chromium()``); without one a browser is
    launched for this call. ``document`` gives the PDF page offsets and the extracted text used to pick the cited
    occurrence. Failures return an empty path with ``ShotResult.reason``; visible is False only when the page
    rendered and the excerpt was not seen, and None when nothing could be checked.
    """
    return screenshot_excerpts(capture, [(excerpt, start, end)], store, policy, document=document,
                               browser=browser, settle_ms=settle_ms, timeout_ms=timeout_ms)[0]


def find_shot(item_key: str, store: Any) -> ShotResult | None:
    """The latest recorded shot for ``item_key`` (what replay cites), or None when there is none."""
    path = Path(store.root) / SHOTS_INDEX
    if not path.is_file():
        return None
    best: dict | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("item_key") == item_key:
                best = d
    if best is None:
        return None
    visible = best.get("visible")
    return ShotResult(str(best.get("path") or ""), str(best.get("sha256") or ""),
                      visible if isinstance(visible, bool) else None, reason=str(best.get("reason") or ""),
                      method=str(best.get("method") or ""), page=int(best.get("page") or 0),
                      match=str(best.get("match") or ""))


__all__ = [
    "MAX_SHOT_PX",
    "SHOTS_INDEX",
    "GatePolicy",
    "PdfShot",
    "ShotPolicy",
    "ShotResult",
    "Span",
    "chromium",
    "clip_box",
    "find_shot",
    "is_tracker",
    "locate",
    "manual_only",
    "needed_height",
    "normalise_key",
    "occurrence_index",
    "render_pdf_excerpt",
    "route_decision",
    "screenshot_excerpt",
    "screenshot_excerpts",
    "shot_key",
    "site_of",
]
