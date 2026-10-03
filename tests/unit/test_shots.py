"""capture.shots: excerpt-anchored screenshots (contracts_p3 section 12).

The default run is offline: a fake browser object stands in for Playwright, the PDF path renders generated PDFs
with pypdfium2 (no sockets), and the policy objects get fake robots/rate limiters. The real-Chromium tests at the
end load pages from a local http.server on 127.0.0.1; they are marked ``network`` (deselected by default) and skip
when Playwright's Chromium is not installed.
"""

from __future__ import annotations

import hashlib
import io
import json
import pickle
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin

import pytest

from footprint.capture import shots
from footprint.capture.shots import (
    MAX_SHOT_PX,
    SHOTS_INDEX,
    VIEWPORT,
    GatePolicy,
    ShotResult,
    Span,
    clip_box,
    find_shot,
    is_tracker,
    locate,
    manual_only,
    needed_height,
    normalise_key,
    occurrence_index,
    render_pdf_excerpt,
    route_decision,
    screenshot_excerpt,
    screenshot_excerpts,
    shot_key,
    site_of,
)
from footprint.capture.store import EvidenceStore
from footprint.models import Capture, Document, EvidenceItem, SourceFamily, sha256_text
from footprint.net.tou import TouRegister

URL = "https://www.example.com/payments/instant.html"
TEXTS = ["Instant payments", "A single connection to the RTP", "\u00ae", " network with ", "AI-enabled",
         " anomaly detection for every payment.", "Contact us"]
EXCERPT = "A single connection to the RTP\u00ae network with AI-enabled anomaly detection for every payment."
DOC_TEXT = "Instant payments\n\n" + EXCERPT + "\n\nContact us"
START = DOC_TEXT.index(EXCERPT)
END = START + len(EXCERPT)
HTML = ("<html><body><h1>Instant payments</h1><p>A single connection to the RTP<sup>\u00ae</sup> network with "
        "<b>AI-enabled</b> anomaly detection for every payment.</p><p>Contact us</p></body></html>").encode()


# =========================================================================== fakes


class FakeAPIResponse:
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b""):
        self.status = status
        self.headers = dict(headers or {})
        self._body = body

    def body(self) -> bytes:
        return self._body


class FakeFrame:
    def __init__(self, page, parent=None):
        self.page = page
        self.parent_frame = parent


class FakeRequest:
    def __init__(self, page, url, resource_type="document", method="GET", nav=True, main=True, frame_page=None):
        self.url = url
        self.resource_type = resource_type
        self.method = method
        self._nav = nav
        self.frame = FakeFrame(frame_page or page, None if main else FakeFrame(page))

    def is_navigation_request(self):
        return self._nav


class FakeRoute:
    def __init__(self, request, site):
        self.request = request
        self.site = site
        self.outcome: tuple[str, object] | None = None

    def fetch(self, **kw):
        self.site.fetched.append(self.request.url)
        return FakeAPIResponse(*self.site.respond(self.request.url))

    def fulfill(self, response=None, **kw):
        self.outcome = ("fulfill", response)

    def continue_(self, **kw):
        self.outcome = ("continue", None)

    def abort(self, error_code=None):
        self.outcome = ("abort", error_code)


class FakeResponse:
    def __init__(self, status, url):
        self.status = status
        self.url = url


class FakeSite:
    """What the fake browser 'renders': HTTP answers per URL, the DOM text nodes and how each node measures.

    Like real Chromium, a redirect that reaches the browser (a fulfilled 3xx, or a continued request whose answer
    is a 3xx) is followed without asking the router again; such hops land in ``unrouted``, which must stay empty.
    """

    def __init__(self, pages=None, texts=TEXTS, hidden=(), covered=(), union=None, subresources=(), popups=()):
        self.pages = dict(pages if pages is not None else {URL: (200, {"content-type": "text/html"}, HTML)})
        self.texts = list(texts)
        self.hidden = set(hidden)      # nodes with no layout box until revealed (display:none, closed details)
        self.covered = set(covered)    # nodes rendered but covered by something
        self.union = dict(union or {"x": 100.0, "y": 380.0, "width": 600.0, "height": 48.0})
        self.subresources = list(subresources)
        self.popups = list(popups)
        self.fetched: list[str] = []
        self.unrouted: list[str] = []

    def follow_unrouted(self, url, status, headers):
        while 300 <= status < 400 and headers.get("location"):
            url = urljoin(url, headers["location"])
            self.unrouted.append(url)
            status, headers, _ = self.respond(url)
        return status

    def respond(self, url):
        return self.pages.get(url, (404, {"content-type": "text/plain"}, b"not found"))

    def probe(self, arg, revealed, vh):
        sn = arg["sn"]
        if sn in self.hidden and not revealed:
            return {"ok": True, "rendered": False, "visible": False, "reason": "no layout boxes"}
        return {"ok": True, "rendered": True, "visible": sn not in self.hidden and sn not in self.covered,
                "share": 0.0 if sn in self.hidden else 1.0, "union": dict(self.union), "vw": 1280, "vh": vh}


class FakePage:
    def __init__(self, ctx, site):
        self.ctx = ctx
        self.site = site
        self.viewport = dict(VIEWPORT)
        self.viewports: list[dict] = []
        self.gotos: list[str] = []
        self.scripts: list[str] = []
        self.select_args: list[dict] = []
        self.screens: list[dict] = []
        self.selected: dict | None = None
        self.revealed = False
        self.waits: list[int] = []
        self.sub_outcomes: dict[str, str] = {}

    def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append(url)
        req = FakeRequest(self, url)
        route = FakeRoute(req, self.site)
        self.ctx.handler(route, req)
        kind, resp = route.outcome
        if kind == "abort":
            raise RuntimeError(f"net::ERR_BLOCKED_BY_CLIENT at {url}")
        if kind == "continue":
            status, headers, _ = self.site.respond(url)
        else:
            status, headers = resp.status, resp.headers
        status = self.site.follow_unrouted(url, status, headers)
        for u, rtype, method in self.site.subresources:
            sub = FakeRequest(self, u, rtype, method, nav=False)
            r = FakeRoute(sub, self.site)
            self.ctx.handler(r, sub)
            self.sub_outcomes[u] = r.outcome[0]
            if r.outcome[0] == "continue":
                s, h, _ = self.site.respond(u)
                self.site.follow_unrouted(u, s, h)
            elif r.outcome[0] == "fulfill":
                self.site.follow_unrouted(u, r.outcome[1].status, r.outcome[1].headers)
        for u in self.site.popups:
            pop = FakeRequest(self, u, frame_page=object())
            r = FakeRoute(pop, self.site)
            self.ctx.handler(r, pop)
            self.sub_outcomes[u] = r.outcome[0]
        return FakeResponse(status, url)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def evaluate(self, script, arg=None):
        self.scripts.append(script)
        vh = self.viewport["height"]
        if script == shots.JS_INSTALL:
            return True
        if script == shots.JS_COLLECT:
            return list(self.site.texts)
        if script == shots.JS_SELECT:
            self.selected, self.revealed = arg, False
            self.select_args.append(arg)
            return self.site.probe(arg, False, vh)
        if script == shots.JS_REVEAL:
            self.revealed = True
            return self.site.probe(self.selected, True, vh)
        if script == shots.JS_MEASURE:
            return self.site.probe(self.selected, self.revealed, vh)
        if script == shots.JS_MARK:
            return {"ok": True}
        if script == shots.JS_CLEAR:
            return True
        raise AssertionError(f"unexpected script: {script[:60]}")

    def set_viewport_size(self, size):
        self.viewport = dict(size)
        self.viewports.append(dict(size))

    def screenshot(self, **kw):
        self.screens.append(kw)
        return b"\x89PNG\r\n\x1a\n" + json.dumps(kw["clip"], sort_keys=True).encode()


class FakeContext:
    def __init__(self, site):
        self.site = site
        self.handler = None
        self.pattern = None
        self.pages: list[FakePage] = []
        self.closed = False

    def new_page(self):
        p = FakePage(self, self.site)
        self.pages.append(p)
        return p

    def route(self, pattern, handler):
        self.pattern, self.handler = pattern, handler

    def route_web_socket(self, pattern, handler):
        self.ws_pattern, self.ws_handler = pattern, handler

    def close(self):
        self.closed = True


class FakeWebSocketRoute:
    def __init__(self, url):
        self.url = url

    def connect_to_server(self):
        raise AssertionError("a WebSocket must never reach the server")

    def close(self, code=None, reason=None):
        raise AssertionError("calling back into Playwright from the sync handler deadlocks the page load")


class FakeBrowser:
    def __init__(self, site=None):
        self.site = site or FakeSite()
        self.contexts: list[FakeContext] = []
        self.kwargs: list[dict] = []

    def new_context(self, **kw):
        self.kwargs.append(kw)
        c = FakeContext(self.site)
        self.contexts.append(c)
        return c

    @property
    def page(self) -> FakePage:
        return self.contexts[-1].pages[-1]


class ExplodingBrowser:
    def new_context(self, **kw):
        raise AssertionError("the browser must not be used")


class RecordingPolicy:
    """A bare ShotPolicy (check only)."""

    def __init__(self, allow=True, why="blocked_robots", deny=()):
        self.calls: list[str] = []
        self.allow, self.why, self.deny = allow, why, set(deny)

    def check(self, url):
        self.calls.append(url)
        if not self.allow or url in self.deny:
            return False, self.why
        return True, ""


class AssetPolicy(RecordingPolicy):
    """A ShotPolicy that also gates page assets (first-party only), like GatePolicy without the register."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.assets: list[tuple[str, bool]] = []

    def allows_subresource(self, url, *, first_party):
        self.assets.append((url, first_party))
        return first_party


class NoCallPolicy:
    def check(self, url):
        raise AssertionError("policy must not be consulted")


class FakeRobots:
    def __init__(self, deny=(), delay=None, boom=False):
        self.deny, self.delay, self.boom = tuple(deny), delay, boom
        self.calls: list[tuple[str, str]] = []

    def allowed(self, url, ua):
        self.calls.append((url, ua))
        if self.boom:
            raise OSError("unreachable")
        return not any(d in url for d in self.deny), "robots-sha"

    def crawl_delay(self, url, ua):
        return self.delay


class FakeRateLimiter:
    def __init__(self):
        self.waits: list[tuple] = []

    def wait(self, host, max_rps, crawl_delay=None):
        self.waits.append((host, max_rps, crawl_delay))
        return 0.0


TOU = {
    "version": "test",
    "default": {"automation": "limited", "max_rps": 1.0, "max_requests_per_run": 3},
    "host": [
        {"match": "example.com", "automation": "full", "max_rps": 2.0, "max_requests_per_run": 0},
        {"match": "capped.example.org", "automation": "full", "max_rps": 0.5, "max_requests_per_run": 2},
        {"match": "seeded.example.net", "automation": "limited", "max_rps": 1.0, "max_requests_per_run": 5},
        {"match": "nobots.example.io", "automation": "none", "max_rps": 0.0},
        {"match": "fiserv.com", "automation": "full", "max_rps": 1.0},   # a register mistake: the hard list wins
    ],
}


def gate(**kw) -> tuple[GatePolicy, FakeRobots, FakeRateLimiter]:
    robots = kw.pop("robots", None) or FakeRobots()
    rl = FakeRateLimiter()
    return GatePolicy(TouRegister(TOU), robots, rl, **kw), robots, rl


# =========================================================================== helpers


def store_at(tmp_path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence")


def html_capture(store, url=URL, body=HTML, **kw) -> Capture:
    meta = dict(vendor_id="V-901", family=SourceFamily.PRD, collector="test", url_requested=url, status=200,
                content_type="text/html; charset=utf-8", retrieved_at="2026-10-02T12:00:00Z")
    meta.update(kw)
    return store.put_raw(body, **meta)


def html_document(store, cap, text=DOC_TEXT) -> Document:
    sha, path = store.put_text(text)
    return Document(doc_id=sha, capture_id=cap.capture_id, vendor_id=cap.vendor_id, family=cap.family,
                    url=cap.url_requested, kind="html", text_path=path, text_len=len(text))


def index_lines(store) -> list[dict]:
    p = Path(store.root) / SHOTS_INDEX
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def shoot(store, cap, policy, browser, excerpt=EXCERPT, start=START, end=END, **kw):
    return screenshot_excerpt(cap, excerpt, start, end, store, policy, browser=browser, settle_ms=0, **kw)


# =========================================================================== ShotResult


def test_shot_result_is_the_contract_tuple():
    r = ShotResult("evidence/shots/a.png", "ab" * 32, True, reason="", method="chromium", match="exact")
    path, sha, visible = r
    assert (path, sha, visible) == ("evidence/shots/a.png", "ab" * 32, True)
    assert r == ("evidence/shots/a.png", "ab" * 32, True)
    assert r.path == path and r.sha256 == sha and r.visible is True and r.ok
    miss = ShotResult("", "", False, reason="excerpt not found in the rendered page")
    assert miss == ("", "", False) and not miss.ok and miss.reason.startswith("excerpt not found")
    assert "visible=False" in repr(miss)


def test_shot_result_pickles_with_its_attributes():
    # Round-trip of an object this test just built (trusted bytes): checks copy/cache support, e.g. Streamlit.
    r = ShotResult("p.png", "c" * 64, None, reason="why", method="pdfium", page=3, match="partial")
    back = pickle.loads(pickle.dumps(r))
    assert back == r and isinstance(back, ShotResult)
    assert (back.reason, back.method, back.page, back.match) == ("why", "pdfium", 3, "partial")


def test_shot_key_matches_the_evidence_item_key(tmp_path):
    st = store_at(tmp_path)
    cap = html_capture(st, url="https://example.com/a", url_final="https://www.example.com/a")
    key = shot_key(cap, EXCERPT)
    assert key == EvidenceItem.make_key("https://www.example.com/a", sha256_text(EXCERPT))
    cap2 = html_capture(st, url="https://example.com/b")
    assert shot_key(cap2, EXCERPT) == EvidenceItem.make_key("https://example.com/b", sha256_text(EXCERPT))


# =========================================================================== normalisation and location


def test_normalise_key_ignores_layout_punctuation_case_and_accents():
    assert normalise_key("  AI-enabled\n\nAnomaly   DETECTION. ") == "aienabledanomalydetection"
    assert normalise_key("\u201cSmart\u201d \u2013 caf\u00e9 \u2014 na\u00efve") == "smartcafenaive"
    assert normalise_key("o\ufb03ce \u00a0work\u00ad\u200bflow") == "officeworkflow"       # ligature, nbsp, shy, zwsp
    assert normalise_key("RTP\u00ae | Cell \u2022 bullet") == "rtpcellbullet"
    assert normalise_key("Stra\u00dfe 42\u00b2") == "strasse422"


def test_locate_exact_across_text_nodes_and_keeps_edge_punctuation():
    spans = locate(TEXTS, EXCERPT)
    assert spans == [Span(1, 0, 5, len(TEXTS[5]), "exact")]   # the final "." is included


def test_locate_includes_opening_quote():
    texts = ["He said \u201cwe use AI daily\u201d."]
    [sp] = locate(texts, "\u201cwe use AI daily\u201d")
    assert texts[0][sp.start_offset:sp.end_offset] == "\u201cwe use AI daily\u201d"


def test_locate_joins_blocks_split_differently():
    assert locate(["Heading", "Body text goes here"], "Heading\n\nBody text goes here")[0].match == "exact"


def test_locate_returns_every_occurrence_with_the_preferred_one_first():
    texts = ["Intro. ", "We use AI tools daily.", " Footer ", "We use AI tools daily.", "x"]
    spans = locate(texts, "We use AI tools daily.")
    assert [s.start_node for s in spans] == [1, 3]
    assert [s.start_node for s in locate(texts, "We use AI tools daily.", prefer=1)] == [3, 1]
    assert [s.start_node for s in locate(texts, "We use AI tools daily.", prefer=7)] == [1, 3]


def test_locate_anchors_bridge_an_inserted_footnote_marker():
    texts = ["Our platform applies machine learning models to every sanctions alert", "1",
             " and routes the riskiest cases to human analysts for review."]
    excerpt = ("Our platform applies machine learning models to every sanctions alert and routes the riskiest cases "
               "to human analysts for review.")
    [sp] = locate(texts, excerpt)
    assert sp.match == "anchors" and (sp.start_node, sp.end_node) == (0, 2)


def test_locate_partial_and_fuzzy_only_when_asked():
    page = "Intro text. Customer data is not retained by the Model and is not used to train the Model. By using"
    excerpt = ("Customer data is not retained by the Model and is not used to train the Model. By using and/or "
               "submitting data to these Specific AI Features, Customer agrees to the terms.")
    assert locate([page], excerpt, modes=("exact", "anchors")) == []
    [sp] = locate([page], excerpt, modes=("partial",))
    assert sp.match == "partial" and page[sp.start_offset:].startswith("Customer data")
    tail_page = "submitting data to these Specific AI Features, Customer agrees to the terms. Next section."
    [tp] = locate([tail_page], excerpt, modes=("partial",))
    assert tail_page[tp.start_offset:tp.end_offset] == ("submitting data to these Specific AI Features, Customer "
                                                         "agrees to the terms")
    assert locate(["Features, Customer agrees to the terms."], excerpt, modes=("partial",)) == []   # < 30%
    sentence = "The platform uses machine learning models to score every payment in real time."
    middle = "x " * 30 + "The platform uses machine-learnt models to score every payment in real time." + " y" * 30
    assert locate([middle], sentence)[0].match == "anchors"            # both ends exact: anchors win
    typos = "x " * 30 + "The platfrom uses machine learning models to score every paymnet in real time." + " y" * 30
    [fz] = locate([typos], sentence)
    assert fz.match.startswith("fuzzy:") and int(fz.match.split(":")[1]) >= 90
    assert typos[fz.start_offset:fz.end_offset].startswith("The platfrom")
    assert locate([typos], sentence, modes=("exact", "anchors")) == []


def test_locate_rejects_short_or_absent_excerpts():
    assert locate(["AI"], "AI") == []
    assert locate(TEXTS, "This sentence is nowhere on the page.") == []
    assert locate([], EXCERPT) == []


def test_occurrence_index_counts_earlier_exact_occurrences():
    text = "We use AI.\n\nFooter.\n\nWe use AI.\n\nWe use AI."
    assert occurrence_index(text, 0, "We use AI.") == 0
    assert occurrence_index(text, text.index("We use AI.", 5), "We use AI.") == 1
    assert occurrence_index(text, text.rindex("We use AI."), "We use AI.") == 2
    assert occurrence_index("", 0, "x") == 0


def test_dom_offsets_are_utf16():
    texts = ["\U0001f600 emoji first, then the excerpt about AI tools here"]
    [sp] = locate(texts, "the excerpt about AI tools here")
    args = shots._js_args(sp, texts)
    assert args["so"] == sp.start_offset + 1 and args["eo"] == sp.end_offset + 1


# =========================================================================== request policy


def test_site_of():
    assert site_of("www.bny.com") == "bny.com"
    assert site_of("theclearinghouse.wd108.myworkdayjobs.com") == "myworkdayjobs.com"
    assert site_of("news.bbc.co.uk") == "bbc.co.uk"
    assert site_of("127.0.0.1") == "127.0.0.1"
    assert site_of("localhost") == "localhost"
    assert site_of("") == ""


@pytest.mark.parametrize("host,url,expected", [
    ("www.googletagmanager.com", "", True),
    ("www.google-analytics.com", "", True),
    ("js.hs-scripts.com", "", True),
    ("widget.intercom.io", "", True),
    ("cdn.cookielaw.org", "", True),
    ("smetrics.bny.com", "", True),
    ("www.example.com", "https://www.example.com/b/ss/rsid/1/JS-2.0", True),
    ("www.bny.com", "https://www.bny.com/etc.clientlibs/site.css", False),
    ("fonts.gstatic.com", "", False),
])
def test_is_tracker(host, url, expected):
    assert is_tracker(host, url) is expected


@pytest.mark.parametrize("url,rtype,method,nav,main,expected", [
    (URL, "document", "GET", True, True, ("navigate", "")),
    ("https://www.example.com/frame", "document", "GET", True, False, ("block", "sub-frame")),
    ("https://www.example.com/app.css", "stylesheet", "GET", False, False, ("first_party", "")),
    ("https://static.example.com/app.js", "script", "GET", False, False, ("first_party", "")),
    ("https://www.example.com/api/content", "fetch", "GET", False, False, ("first_party", "")),
    ("https://www.example.com/api/track", "xhr", "POST", False, False, ("block", "method POST")),
    ("https://www.example.com/clip.mp4", "media", "GET", False, False, ("block", "media")),
    ("https://www.example.com/live", "websocket", "GET", False, False, ("block", "websocket")),
    ("https://www.googletagmanager.com/gtm.js", "script", "GET", False, False, ("block", "tracker")),
    ("https://cdn.vendorcdn.net/lib.js", "script", "GET", False, False, ("block", "third-party script")),
    ("https://cdn.vendorcdn.net/site.css", "stylesheet", "GET", False, False, ("third_party", "")),
    ("https://cdn.vendorcdn.net/font.woff2", "font", "GET", False, False, ("third_party", "")),
    ("https://static.licdn.com/badge.png", "image", "GET", False, False, ("block", "tracker")),
    ("https://www.fiserv.com/logo.png", "image", "GET", False, False, ("block", "manual-only host")),
    ("data:image/png;base64,AAAA", "image", "GET", False, False, ("local", "")),
    ("ftp://example.com/x", "other", "GET", False, False, ("block", "scheme ftp")),
])
def test_route_decision(url, rtype, method, nav, main, expected):
    assert route_decision(url, rtype, method, URL, navigation=nav, main_frame=main) == expected


def test_clip_box_margins_clamps_and_minimum_width():
    c = clip_box({"x": 100.4, "y": 400.2, "width": 600, "height": 50}, 1280, 900)
    assert c == {"x": 68, "y": 280, "width": 665, "height": 291}
    narrow = clip_box({"x": 600, "y": 10, "width": 40, "height": 20}, 1280, 900)
    assert narrow["width"] == 480 and narrow["y"] == 0
    edge = clip_box({"x": 1250, "y": 850, "width": 100, "height": 100}, 1280, 900)
    assert edge["x"] + edge["width"] <= 1280 and edge["y"] + edge["height"] <= 900
    huge = clip_box({"x": 0, "y": 0, "width": 800, "height": 40000}, 1280, 40000)
    assert huge["height"] == MAX_SHOT_PX
    assert needed_height({"height": 100}) == 340


# =========================================================================== GatePolicy


def test_gate_allows_counts_and_waits_in_order():
    pol, robots, rl = gate()
    assert pol.check("https://www.example.com/a") == (True, "")
    assert pol.counts == {"example.com": 1}
    assert robots.calls == [("https://www.example.com/a", "footprint-osint")]
    assert rl.waits == [("www.example.com", 2.0, None)]


def test_gate_tou_refusal_comes_before_robots_and_rate_limit():
    pol, robots, rl = gate()
    ok, why = pol.check("https://nobots.example.io/x")
    assert not ok and why.startswith("blocked_tou")
    for url in ("https://www.fiserv.com/x", "https://careers.fiserv.com/y", "https://www.linkedin.com/in/a",
                "https://lnkd.in/abc"):
        ok, why = pol.check(url)
        assert not ok and "manual capture only" in why
    assert robots.calls == [] and rl.waits == [] and pol.counts == {}


def test_gate_refuses_non_get_targets():
    pol, robots, _ = gate()
    assert pol.check("https://web.archive.org/save/https://www.example.com/")[0] is False
    assert pol.check("ftp://www.example.com/x")[0] is False
    assert robots.calls == []


def test_gate_limited_hosts_need_seeded_urls():
    pol, _, _ = gate()
    ok, why = pol.check("https://seeded.example.net/a")
    assert not ok and "not seeded" in why
    pol2, _, _ = gate(seeded={"https://seeded.example.net/a"})
    assert pol2.check("https://seeded.example.net/a") == (True, "")
    assert pol2.check("https://seeded.example.net/b")[0] is False
    pol3, _, _ = gate(seeded=lambda u: u.endswith("/b"))
    assert pol3.check("https://seeded.example.net/b") == (True, "")
    pol4, _, _ = gate(seeded=True)
    assert pol4.check("https://seeded.example.net/zzz") == (True, "")


def test_gate_enforces_the_per_run_cap():
    pol, _, _ = gate()
    assert pol.check("https://capped.example.org/1")[0]
    assert pol.check("https://capped.example.org/2")[0]
    ok, why = pol.check("https://capped.example.org/3")
    assert not ok and why.startswith("cap_reached")


def test_gate_robots_refusal_is_not_counted():
    pol, _, rl = gate(robots=FakeRobots(deny=("/private",), delay=4.0))
    assert pol.check("https://www.example.com/private/x") == (False, "blocked_robots")
    assert pol.counts == {} and rl.waits == []
    assert pol.check("https://www.example.com/public") == (True, "")
    assert rl.waits == [("www.example.com", 2.0, 4.0)]


def test_gate_unreachable_robots_refuses():
    pol, _, _ = gate(robots=FakeRobots(boom=True))
    ok, why = pol.check("https://www.example.com/a")
    assert not ok and why.startswith("network_error")


def test_gate_from_fetcher_shares_counters_robots_and_ua():
    from footprint.net.fetcher import DEFAULT_UA, LiveFetcher

    robots, rl = FakeRobots(), FakeRateLimiter()
    f = LiveFetcher(None, TouRegister(TOU), robots=robots, ratelimit=rl, sec_contact="tprm@example.org")
    f.request_counts["capped.example.org"] = 2          # collection already used the budget
    pol = GatePolicy.from_fetcher(f)
    assert pol.check("https://capped.example.org/x")[0] is False
    assert pol.check("https://www.example.com/a")[0]
    assert f.request_counts["example.com"] == 1
    assert pol.robots is robots and pol.ratelimit is rl
    assert pol.user_agent("https://www.example.com/") == DEFAULT_UA
    assert "tprm@example.org" in pol.user_agent("https://www.sec.gov/Archives/x.htm")
    no_contact = GatePolicy.from_fetcher(LiveFetcher(None, TouRegister(TOU), robots=robots, ratelimit=rl))
    with pytest.raises(ValueError):
        no_contact.user_agent("https://www.sec.gov/x")


def test_gate_default_builds_without_network():
    pol = GatePolicy.default(tou=TouRegister(TOU), sec_contact="a@b.c")
    assert pol.tou.version == "test" and pol.counts == {}
    assert "a@b.c" in pol.user_agent("https://efts.sec.gov/LATEST/x")


def test_gate_subresources_need_robots_and_a_registered_host_when_third_party():
    pol, _, _ = gate(robots=FakeRobots(deny=("/blocked/",)))
    assert pol.allows_subresource("https://www.example.com/a.css", first_party=True)
    assert not pol.allows_subresource("https://www.example.com/blocked/a.css", first_party=True)
    assert not pol.allows_subresource("https://cdn.unknown.net/a.css", first_party=False)
    assert pol.allows_subresource("https://assets.capped.example.org/a.css", first_party=False)
    assert not pol.allows_subresource("https://nobots.example.io/a.png", first_party=True)
    assert not pol.allows_subresource("https://www.fiserv.com/logo.png", first_party=False)
    assert pol.counts == {}


def test_gate_capped_and_manual_only():
    pol, _, _ = gate()
    assert pol.capped("https://capped.example.org/x") and pol.capped("https://unregistered.example.dev/")
    assert not pol.capped("https://www.example.com/")
    assert pol.manual_only("https://nobots.example.io/") and pol.manual_only("https://www.linkedin.com/x")
    assert not pol.manual_only("https://www.example.com/")


def test_module_manual_only_uses_the_project_register():
    from footprint.net.tou import load_tou

    reg = load_tou()                                   # config/tou.toml as it stands (entries change over time)
    assert manual_only("https://www.fiserv.com/en/x.html") and manual_only("https://www.linkedin.com/in/x")
    for e in reg.entries:
        url = f"https://www.{e.match}/x"
        hard = any(e.match == m or e.match.endswith("." + m) for m in shots.MANUAL_ONLY)
        assert manual_only(url) == (e.automation == "none" or hard), e.match
    assert any(e.automation == "full" for e in reg.entries)
    assert not manual_only("not a url")


# =========================================================================== HTML with the fake browser


def test_html_shot_happy_path(tmp_path):
    st = store_at(tmp_path)
    cap = html_capture(st)
    doc = html_document(st, cap)
    pol = AssetPolicy()
    br = FakeBrowser()
    res = shoot(st, cap, pol, br, document=doc)
    path, sha, visible = res
    assert visible is True and res.method == "chromium" and res.match == "exact" and res.reason == ""
    png = (tmp_path / "evidence" / "shots" / f"{sha}.png").read_bytes()
    assert hashlib.sha256(png).hexdigest() == sha and path == f"evidence/shots/{sha}.png"
    assert pol.calls == [URL]                                  # gated exactly once
    kw = br.kwargs[0]
    assert kw["user_agent"].startswith("footprint-osint/1.0") and kw["viewport"] == VIEWPORT
    assert kw["service_workers"] == "block" and kw["accept_downloads"] is False
    ctx = br.contexts[0]
    assert ctx.pattern == "**/*" and ctx.closed
    page = br.page
    assert page.gotos == [URL] and page.select_args[0]["sn"] == 1
    clip = page.screens[0]["clip"]
    assert clip["y"] >= 0 and clip["y"] + clip["height"] <= VIEWPORT["height"] and clip["width"] <= 1280
    assert page.scripts[-1] == shots.JS_CLEAR
    [entry] = index_lines(st)
    assert {"item_key", "capture_id", "url", "path", "sha256", "visible", "taken_at"} <= set(entry)
    assert entry["item_key"] == shot_key(cap, EXCERPT) and entry["capture_id"] == cap.capture_id
    assert (entry["path"], entry["sha256"], entry["visible"]) == (path, sha, True)
    assert find_shot(shot_key(cap, EXCERPT), st) == (path, sha, True)


def test_html_policy_refusal_never_touches_the_browser(tmp_path):
    st = store_at(tmp_path)
    cap = html_capture(st)
    res = shoot(st, cap, RecordingPolicy(allow=False, why="blocked_robots"), ExplodingBrowser())
    assert res == ("", "", None) and res.reason == "blocked_robots"
    [entry] = index_lines(st)
    assert entry["path"] == "" and entry["visible"] is None and entry["reason"] == "blocked_robots"
    assert list((tmp_path / "evidence" / "shots").glob("*.png")) == []


def test_html_without_policy_is_not_loaded(tmp_path):
    st = store_at(tmp_path)
    res = shoot(st, html_capture(st), None, ExplodingBrowser())
    assert res == ("", "", None) and "no policy" in res.reason


def test_html_excerpt_not_found(tmp_path):
    st = store_at(tmp_path)
    cap = html_capture(st)
    text = "This sentence does not appear anywhere in the rendered page."
    res = screenshot_excerpt(cap, text, 0, len(text), st, RecordingPolicy(), browser=FakeBrowser(), settle_ms=0)
    assert res == ("", "", False) and "not found" in res.reason
    assert list((tmp_path / "evidence" / "shots").glob("*.png")) == []


def test_html_prefers_the_cited_occurrence(tmp_path):
    st = store_at(tmp_path)
    texts = ["Intro. ", "We use AI tools daily.", " Footer: ", "We use AI tools daily."]
    doc_text = "Intro.\n\nWe use AI tools daily.\n\nFooter:\n\nWe use AI tools daily."
    cap = html_capture(st)
    doc = html_document(st, cap, doc_text)
    start = doc_text.rindex("We use AI tools daily.")
    br = FakeBrowser(FakeSite(texts=texts))
    res = screenshot_excerpt(cap, "We use AI tools daily.", start, start + 22, st, RecordingPolicy(), document=doc,
                             browser=br, settle_ms=0)
    assert res.visible is True and br.page.select_args[0]["sn"] == 3


def test_html_skips_an_invisible_occurrence(tmp_path):
    st = store_at(tmp_path)
    texts = ["We use AI tools daily.", " menu ", "We use AI tools daily."]
    br = FakeBrowser(FakeSite(texts=texts, covered={0}))
    res = screenshot_excerpt(html_capture(st), "We use AI tools daily.", 0, 22, st, RecordingPolicy(), browser=br,
                             settle_ms=0)
    assert res.visible is True and [a["sn"] for a in br.page.select_args] == [0, 2]


def test_html_hidden_excerpt_is_revealed_but_not_visible(tmp_path):
    st = store_at(tmp_path)
    br = FakeBrowser(FakeSite(hidden={1}))
    res = shoot(st, html_capture(st), RecordingPolicy(), br)
    assert res.ok and res.visible is False and "revealed" in res.reason
    assert shots.JS_REVEAL in br.page.scripts


def test_html_covered_excerpt_is_shot_but_not_visible(tmp_path):
    st = store_at(tmp_path)
    br = FakeBrowser(FakeSite(covered={1}))
    res = shoot(st, html_capture(st), RecordingPolicy(), br)
    assert res.ok and res.visible is False and "covered" in res.reason
    assert shots.JS_REVEAL not in br.page.scripts


def test_html_tall_excerpt_grows_the_viewport_up_to_the_limit(tmp_path):
    st = store_at(tmp_path)
    br = FakeBrowser(FakeSite(union={"x": 40, "y": 120, "width": 900, "height": 2000}))
    res = shoot(st, html_capture(st), RecordingPolicy(), br)
    page = br.page
    assert res.visible is True
    assert page.viewports[0] == {"width": 1280, "height": 2240} and page.viewports[-1] == VIEWPORT
    assert page.screens[0]["clip"]["height"] <= 2240
    br2 = FakeBrowser(FakeSite(union={"x": 40, "y": 120, "width": 900, "height": 30000}))
    shoot(st, html_capture(st), RecordingPolicy(), br2)
    assert br2.page.viewports[0]["height"] == MAX_SHOT_PX
    assert br2.page.screens[0]["clip"]["height"] <= MAX_SHOT_PX


def test_html_router_filters_what_the_page_requests(tmp_path):
    st = store_at(tmp_path)
    subs = [
        ("https://www.example.com/site.css", "stylesheet", "GET"),
        ("https://static.example.com/app.js", "script", "GET"),
        ("https://www.googletagmanager.com/gtm.js", "script", "GET"),
        ("https://cdn.vendorcdn.net/lib.js", "script", "GET"),
        ("https://cdn.vendorcdn.net/site.css", "stylesheet", "GET"),
        ("https://www.example.com/beacon", "xhr", "POST"),
        ("https://www.example.com/intro.mp4", "media", "GET"),
        ("data:image/png;base64,AAAA", "image", "GET"),
    ]
    br = FakeBrowser(FakeSite(subresources=subs, popups=["https://www.example.com/popup"]))
    pol = AssetPolicy()
    res = shoot(st, html_capture(st), pol, br)
    assert res.visible is True
    assert br.page.sub_outcomes == {
        "https://www.example.com/site.css": "fulfill",
        "https://static.example.com/app.js": "fulfill",
        "https://www.googletagmanager.com/gtm.js": "abort",
        "https://cdn.vendorcdn.net/lib.js": "abort",
        "https://cdn.vendorcdn.net/site.css": "abort",       # a bare policy cannot vouch for third parties
        "https://www.example.com/beacon": "abort",
        "https://www.example.com/intro.mp4": "abort",
        "data:image/png;base64,AAAA": "continue",
        "https://www.example.com/popup": "abort",
    }
    assert br.site.unrouted == []
    assert pol.assets == [("https://www.example.com/site.css", True), ("https://static.example.com/app.js", True),
                          ("https://cdn.vendorcdn.net/site.css", False)]     # trackers and scripts never asked


def test_html_check_only_policy_renders_lean(tmp_path):
    st = store_at(tmp_path)
    site = FakeSite(subresources=[("https://www.example.com/site.css", "stylesheet", "GET"),
                                  ("https://www.example.com/app.js", "script", "GET")])
    br = FakeBrowser(site)
    res = shoot(st, html_capture(st), RecordingPolicy(), br)     # cannot check robots for assets: none load
    assert res.visible is True and "lean render" in res.reason
    assert br.page.sub_outcomes == {"https://www.example.com/site.css": "abort",
                                    "https://www.example.com/app.js": "abort"}
    assert site.fetched == [URL]


def test_html_websockets_are_mocked_and_never_connected(tmp_path):
    st = store_at(tmp_path)
    br = FakeBrowser()
    res = shoot(st, html_capture(st), RecordingPolicy(), br)
    assert res.visible is True
    ctx = br.contexts[0]
    matcher = ctx.ws_pattern
    assert callable(matcher) and matcher("wss://www.example.com/socket") and matcher("ws://127.0.0.1:9/x")
    ctx.ws_handler(FakeWebSocketRoute("wss://www.example.com/socket"))   # mocked in the page, never connected


def test_html_router_refuses_assets_that_redirect(tmp_path):
    st = store_at(tmp_path)
    pages = {URL: (200, {"content-type": "text/html"}, HTML),
             "https://www.example.com/r.css": (302, {"location": "https://www.googletagmanager.com/gtm.css"}, b""),
             "https://www.example.com/ok.css": (200, {"content-type": "text/css"}, b"p{}")}
    site = FakeSite(pages=pages, subresources=[("https://www.example.com/r.css", "stylesheet", "GET"),
                                               ("https://www.example.com/ok.css", "stylesheet", "GET")])
    br = FakeBrowser(site)
    res = shoot(st, html_capture(st), AssetPolicy(), br)
    assert res.visible is True
    assert br.page.sub_outcomes == {"https://www.example.com/r.css": "abort",
                                    "https://www.example.com/ok.css": "fulfill"}
    assert site.fetched == [URL, "https://www.example.com/r.css", "https://www.example.com/ok.css"]
    assert site.unrouted == []                       # the tracker the asset pointed at was never requested


def test_html_router_asks_a_gate_policy_about_assets(tmp_path):
    st = store_at(tmp_path)
    subs = [("https://www.example.com/ok.css", "stylesheet", "GET"),
            ("https://www.example.com/blocked/x.css", "stylesheet", "GET"),
            ("https://assets.capped.example.org/x.css", "stylesheet", "GET"),
            ("https://cdn.unknown.net/x.css", "stylesheet", "GET")]
    br = FakeBrowser(FakeSite(subresources=subs))
    pol, _, _ = gate(robots=FakeRobots(deny=("/blocked/",)))
    res = shoot(st, html_capture(st), pol, br)
    assert res.visible is True and pol.counts == {"example.com": 1}
    assert br.page.sub_outcomes == {"https://www.example.com/ok.css": "fulfill",
                                    "https://www.example.com/blocked/x.css": "abort",
                                    "https://assets.capped.example.org/x.css": "fulfill",
                                    "https://cdn.unknown.net/x.css": "abort"}


def test_html_capped_host_renders_lean(tmp_path):
    st = store_at(tmp_path)
    url = "https://capped.example.org/news/ai.html"
    site = FakeSite(pages={url: (200, {"content-type": "text/html"}, HTML)},
                    subresources=[("https://capped.example.org/theme.css", "stylesheet", "GET")])
    pol, _, _ = gate()
    res = shoot(st, html_capture(st, url=url), pol, FakeBrowser(site))
    assert res.visible is True and "lean render" in res.reason
    assert pol.counts == {"capped.example.org": 1}


def test_html_redirect_hops_are_gated(tmp_path):
    st = store_at(tmp_path)
    start, moved = "https://www.example.com/start", "https://www.example.com/moved"
    pages = {start: (301, {"location": "/moved"}, b""), moved: (200, {"content-type": "text/html"}, HTML)}
    pol = RecordingPolicy(deny={moved})
    site = FakeSite(pages=pages)
    res = shoot(st, html_capture(st, url=start), pol, FakeBrowser(site))
    assert res == ("", "", None) and res.reason.startswith("blocked_robots") and moved in res.reason
    assert pol.calls == [start, moved] and site.fetched == [start] and site.unrouted == []
    ok_pol = RecordingPolicy()
    site2 = FakeSite(pages=pages)
    br2 = FakeBrowser(site2)
    res2 = shoot(st, html_capture(st, url=start), ok_pol, br2)
    assert res2.visible is True and ok_pol.calls == [start, moved] and site2.fetched == [start, moved]
    assert br2.page.gotos == [start, moved] and site2.unrouted == []


def test_html_uses_the_final_url_of_the_capture(tmp_path):
    st = store_at(tmp_path)
    cap = html_capture(st, url="https://example.com/old", url_final=URL)
    pol = RecordingPolicy()
    res = shoot(st, cap, pol, FakeBrowser())
    assert res.visible is True and pol.calls == [URL]
    assert index_lines(st)[0]["url"] == URL


def test_html_redirect_loop_is_capped(tmp_path):
    st = store_at(tmp_path)
    a, b = "https://www.example.com/a", "https://www.example.com/b"
    site = FakeSite(pages={a: (302, {"location": b}, b""), b: (302, {"location": a}, b"")})
    pol = RecordingPolicy()
    res = shoot(st, html_capture(st, url=a), pol, FakeBrowser(site))
    assert res == ("", "", None) and res.reason == "too many redirects"
    assert len(pol.calls) == shots.MAX_NAVIGATIONS     # the page itself, then every hop up to the cap
    assert site.unrouted == []


def test_html_http_error_and_bot_wall(tmp_path):
    st = store_at(tmp_path)
    gone = FakeSite(pages={URL: (404, {}, b"missing")})
    res = shoot(st, html_capture(st), RecordingPolicy(), FakeBrowser(gone))
    assert res == ("", "", None) and res.reason == "http 404"
    wall = FakeSite(pages={URL: (403, {"server": "cloudflare"}, b"<title>Just a moment...</title>")})
    res2 = shoot(st, html_capture(st), RecordingPolicy(), FakeBrowser(wall))
    assert res2 == ("", "", None) and res2.reason == "blocked_bot"


def test_html_launches_and_closes_its_own_browser(tmp_path, monkeypatch):
    st = store_at(tmp_path)
    events: list[str] = []

    class Launcher:
        def __enter__(self):
            events.append("enter")
            return FakeBrowser()

        def __exit__(self, *exc):
            events.append("exit")
            return False

    monkeypatch.setattr(shots, "chromium", lambda **kw: Launcher())
    res = shoot(st, html_capture(st), RecordingPolicy(), None)
    assert res.visible is True and events == ["enter", "exit"]


def test_html_missing_chromium_is_a_reason_not_an_error(tmp_path, monkeypatch):
    st = store_at(tmp_path)

    class Broken:
        def __enter__(self):
            raise RuntimeError("Executable doesn't exist at C:\\ms-playwright\\chromium\nRun playwright install")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(shots, "chromium", lambda **kw: Broken())
    res = shoot(st, html_capture(st), RecordingPolicy(), None)
    assert res == ("", "", None) and res.reason == "chromium unavailable: Executable doesn't exist at " \
                                                   "C:\\ms-playwright\\chromium"


def test_html_sec_pages_need_a_contact_user_agent(tmp_path):
    st = store_at(tmp_path)
    cap = html_capture(st, url="https://www.sec.gov/Archives/edgar/data/1/x.htm")
    pol = RecordingPolicy()
    res = shoot(st, cap, pol, ExplodingBrowser())
    assert res == ("", "", None) and res.reason.startswith("blocked_tou") and pol.calls == []


def test_batch_loads_the_page_once(tmp_path):
    st = store_at(tmp_path)
    cap = html_capture(st)
    texts = ["We use AI tools daily.", " and ", "Our chatbot answers questions."]
    br = FakeBrowser(FakeSite(texts=texts))
    pol = RecordingPolicy()
    a, b = "We use AI tools daily.", "Our chatbot answers questions."
    res = screenshot_excerpts(cap, [(a, 0, len(a)), (b, 30, 30 + len(b))], st, pol, browser=br, settle_ms=0)
    assert [r.visible for r in res] == [True, True]
    assert pol.calls == [URL] and len(br.contexts) == 1 and len(br.page.gotos) == 1
    assert [e["item_key"] for e in index_lines(st)] == [shot_key(cap, a), shot_key(cap, b)]
    assert screenshot_excerpts(cap, [], st, pol) == []


@pytest.mark.parametrize("excerpt,start,end", [("", 0, 0), ("abc", 0, 5), ("abc", -1, 2)])
def test_bad_excerpt_offsets_are_a_contract_error(tmp_path, excerpt, start, end):
    st = store_at(tmp_path)
    with pytest.raises(ValueError):
        screenshot_excerpt(html_capture(st), excerpt, start, end, st, RecordingPolicy(), browser=FakeBrowser())


# =========================================================================== manual-only hosts and other kinds


def test_manual_only_host_reuses_the_analyst_screenshot(tmp_path):
    st = store_at(tmp_path)
    shot_bytes = b"\x89PNG\r\n\x1a\nmanual screenshot"
    shot_path = st.put_shot(shot_bytes, ".png")
    cap = html_capture(st, url="https://www.fiserv.com/en/about-fiserv/ai.html", manual=True, collector="manual",
                       robots_decision="manual", screenshot_path=shot_path)
    res = shoot(st, cap, NoCallPolicy(), ExplodingBrowser())
    assert res == (shot_path, hashlib.sha256(shot_bytes).hexdigest(), None)
    assert res.method == "manual" and "manual-only" in res.reason


def test_manual_only_host_without_screenshot(tmp_path):
    st = store_at(tmp_path)
    cap = html_capture(st, url="https://www.linkedin.com/posts/x", manual=True)
    res = shoot(st, cap, NoCallPolicy(), ExplodingBrowser())
    assert res == ("", "", None) and "no manual screenshot" in res.reason
    gate_pol, robots, _ = gate()
    cap2 = html_capture(st, url="https://nobots.example.io/page")       # automation none in the policy's register
    assert shoot(st, cap2, gate_pol, ExplodingBrowser()) == ("", "", None)
    assert robots.calls == [] and gate_pol.counts == {}


def test_manual_screenshot_is_the_fallback_when_nothing_could_be_checked(tmp_path):
    st = store_at(tmp_path)
    shot_path = st.put_shot(b"\x89PNGfallback", ".png")
    cap = html_capture(st, manual=True, screenshot_path=shot_path)
    res = shoot(st, cap, RecordingPolicy(allow=False), ExplodingBrowser())
    assert res == (shot_path, hashlib.sha256(b"\x89PNGfallback").hexdigest(), None)
    assert res.reason.startswith("blocked_robots") and res.method == "manual"
    text = "This sentence does not appear anywhere in the rendered page."
    miss = screenshot_excerpt(cap, text, 0, len(text), st, RecordingPolicy(), browser=FakeBrowser(), settle_ms=0)
    assert miss == ("", "", False)            # the page rendered and the excerpt is not there: no substitute


def test_manual_screenshot_sha_from_the_name_when_the_file_is_gone(tmp_path):
    st = store_at(tmp_path)
    sha = "ab" * 32
    cap = html_capture(st, url="https://www.fiserv.com/x", manual=True, screenshot_path=f"evidence/shots/{sha}.png")
    assert shoot(st, cap, NoCallPolicy(), ExplodingBrowser()) == (f"evidence/shots/{sha}.png", sha, None)


def test_json_and_dns_captures_have_no_page(tmp_path):
    st = store_at(tmp_path)
    body = b'{"jobPostingInfo": {"jobDescription": "Utilize AI-powered tools such as Microsoft Copilot"}}'
    cap = st.put_raw(body, vendor_id="V-901", family=SourceFamily.JOB, collector="jobs",
                     url_requested="https://x.wd1.myworkdayjobs.com/wday/cxs/x/y/job/1", status=200,
                     content_type="application/json", retrieved_at="2026-10-02T12:00:00Z")
    ex = "Utilize AI-powered tools such as Microsoft Copilot"
    res = screenshot_excerpt(cap, ex, 10, 10 + len(ex), st, NoCallPolicy(), browser=ExplodingBrowser())
    assert res == ("", "", None) and res.reason == "no page to render for a json capture"
    assert index_lines(st)[0]["method"] == "none"


# =========================================================================== PDF (offline, real pypdfium2)


def make_pdf(pages: list[list[str]], size: tuple[int, int] = (612, 792)) -> bytes:
    """Minimal PDF: Helvetica 12 pt, 16 pt leading, one line of WinAnsi text per entry."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    for i, lines in enumerate(pages):
        content = ("BT /F1 12 Tf 16 TL 72 720 Td " + " ".join(f"({esc(t)}) Tj T*" for t in lines) + " ET")
        data = content.encode("latin-1")
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] "
                     f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>").encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for k, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


PDF_PAGES = [
    ["Quarterly update", "Operations overview for the payments business.", "Volumes grew in every region."],
    ["Payments operations", "The platform applies AI-enabled anomaly detection",
     "to every instant payment on the RTP network.", "Human reviewers confirm each alert before release."],
    ["Appendix", "Definitions and notes."],
]


@pytest.fixture
def pdf_case(tmp_path):
    pytest.importorskip("pypdfium2")
    pytest.importorskip("pypdf")
    pytest.importorskip("PIL")
    from footprint.extract import extract_document

    st = store_at(tmp_path)
    raw = make_pdf(PDF_PAGES)
    cap = st.put_raw(raw, vendor_id="V-901", family=SourceFamily.PRD, collector="test",
                     url_requested="https://www.example.com/docs/update.pdf", status=200,
                     content_type="application/pdf", retrieved_at="2026-10-02T12:00:00Z")
    doc = extract_document(cap, raw, st)
    assert doc.kind == "pdf" and len(doc.pages) == 3
    return st, cap, doc, st.get_text(doc.doc_id), raw


HIGHLIGHT_ON_WHITE = (255, 229, 102)     # rgba(255, 212, 0, 0.6) over white paper


def _highlight_pixels(png: bytes, tol: int = 10) -> int:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    return sum(n for n, c in img.getcolors(img.size[0] * img.size[1])
               if all(abs(a - b) <= tol for a, b in zip(c, HIGHLIGHT_ON_WHITE)))


def _outlined(colours) -> bool:
    return any(abs(r - 215) < 12 and abs(g - 38) < 12 and abs(b - 61) < 12 for r, g, b in colours)


def _pixels(png: bytes):
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    return img, {colour for _count, colour in img.getcolors(img.size[0] * img.size[1])}


def test_pdf_shot_is_cropped_highlighted_and_offline(pdf_case):
    st, cap, doc, text, _raw = pdf_case
    s = text.index("AI-enabled anomaly detection")
    e = text.index("RTP network.") + len("RTP network.")
    res = screenshot_excerpt(cap, text[s:e], s, e, st, NoCallPolicy(), document=doc, browser=ExplodingBrowser())
    path, sha, visible = res
    assert visible is True and res.page == 2 and res.method == "pdfium" and res.match == "exact"
    png = (Path(st.root) / "shots" / f"{sha}.png").read_bytes()
    assert hashlib.sha256(png).hexdigest() == sha and path.endswith(f"{sha}.png")
    img, colours = _pixels(png)
    assert img.size[0] < 612 * 2 and img.size[1] < 792 * 2 / 2      # a crop, not the page
    assert shots.PDF_TINT in colours                                   # the excerpt's paper is tinted
    assert shots.OUTLINE_RGB in colours                                # and outlined
    [entry] = index_lines(st)
    assert entry["page"] == 2 and entry["method"] == "pdfium" and entry["visible"] is True


def test_pdf_render_is_deterministic(pdf_case):
    _st, _cap, doc, text, raw = pdf_case
    s = text.index("Human reviewers")
    a = render_pdf_excerpt(raw, text[s:s + 30], start=s, pages=doc.pages)
    b = render_pdf_excerpt(raw, text[s:s + 30], start=s, pages=doc.pages)
    assert a.png == b.png and a.visible is True and a.page == 2


def test_pdf_without_document_searches_every_page(pdf_case):
    st, cap, _doc, text, _raw = pdf_case
    s = text.index("Definitions and notes.")
    res = screenshot_excerpt(cap, "Definitions and notes.", s, s + 22, st, None)
    assert res.visible is True and res.page == 3


def test_pdf_excerpt_across_a_page_break(pdf_case):
    _st, _cap, doc, text, raw = pdf_case
    s = text.index("Volumes grew in every region.")
    e = text.index("The platform applies") + len("The platform applies")
    shot = render_pdf_excerpt(raw, text[s:e], start=s, pages=doc.pages)
    assert shot.visible is True and shot.page == 1 and shot.match == "partial"
    assert "page break" in shot.reason


def test_pdf_excerpt_not_in_the_text_layer_renders_the_hinted_page(pdf_case):
    st, cap, doc, text, _raw = pdf_case
    ghost = "This sentence was never printed on any page of the document."
    s = text.index("Payments operations")
    res = screenshot_excerpt(cap, ghost, s, s + len(ghost), st, None, document=doc)
    assert res.ok and res.visible is False and res.page == 2 and "not located" in res.reason
    img, _ = _pixels((Path(st.root) / "shots" / f"{res.sha256}.png").read_bytes())
    assert img.size[0] == shots.PDF_PAGE_FIT_PX or img.size[0] == 612 * 2
    nohint = screenshot_excerpt(cap, ghost, 0, len(ghost), st, None)
    assert nohint == ("", "", False)


def test_pdf_missing_or_broken_bytes(tmp_path):
    pytest.importorskip("pypdfium2")
    st = store_at(tmp_path)
    ghost = Capture(capture_id="f" * 64, vendor_id="V-901", family=SourceFamily.PRD, collector="test",
                    url_requested="https://www.example.com/x.pdf", content_type="application/pdf",
                    retrieved_at="2026-10-02T12:00:00Z")
    res = screenshot_excerpt(ghost, "anything at all here", 0, 20, st, None)
    assert res == ("", "", None) and "not in the evidence store" in res.reason
    broken = st.put_raw(b"%PDF-1.4 this is not really a pdf", vendor_id="V-901", family=SourceFamily.PRD,
                        collector="test", url_requested="https://www.example.com/y.pdf",
                        content_type="application/pdf", retrieved_at="2026-10-02T12:00:00Z")
    res2 = screenshot_excerpt(broken, "anything at all here", 0, 20, st, None)
    assert res2 == ("", "", None) and "could not be opened" in res2.reason


def test_pdf_from_a_manual_only_host_is_rendered_offline(pdf_case, tmp_path):
    st, _cap, doc, text, raw = pdf_case
    cap = st.put_raw(raw, vendor_id="V-901", family=SourceFamily.PRD, collector="manual",
                     url_requested="https://www.fiserv.com/content/dam/x.pdf", status=200, manual=True,
                     content_type="application/pdf", retrieved_at="2026-10-02T12:00:00Z")
    s = text.index("Human reviewers")
    res = screenshot_excerpt(cap, text[s:s + 30], s, s + 30, st, NoCallPolicy(), document=doc,
                             browser=ExplodingBrowser())
    assert res.visible is True and res.method == "pdfium"


# =========================================================================== find_shot


def test_find_shot_returns_the_latest_entry(tmp_path):
    st = store_at(tmp_path)
    assert find_shot("k" * 64, st) is None
    idx = Path(st.root) / SHOTS_INDEX
    idx.parent.mkdir(parents=True)
    rows = [
        {"item_key": "k1", "path": "a.png", "sha256": "1" * 64, "visible": True, "method": "chromium"},
        "not json",
        {"item_key": "k2", "path": "", "sha256": "", "visible": None, "reason": "blocked_robots"},
        {"item_key": "k1", "path": "b.png", "sha256": "2" * 64, "visible": False, "reason": "covered"},
        ["a list"],
    ]
    idx.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows), encoding="utf-8")
    r1 = find_shot("k1", st)
    assert r1 == ("b.png", "2" * 64, False) and r1.reason == "covered"
    r2 = find_shot("k2", st)
    assert r2 == ("", "", None) and r2.reason == "blocked_robots"
    assert find_shot("k3", st) is None


def test_index_lines_are_sorted_json_with_lf(tmp_path):
    st = store_at(tmp_path)
    shoot(st, html_capture(st), RecordingPolicy(allow=False), ExplodingBrowser())
    shoot(st, html_capture(st), RecordingPolicy(), FakeBrowser())
    raw = (Path(st.root) / SHOTS_INDEX).read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    for line in raw.decode("utf-8").splitlines():
        obj = json.loads(line)
        assert list(obj) == sorted(obj)
    assert find_shot(shot_key(html_capture(st), EXCERPT), st).visible is True


# =========================================================================== real Chromium (local server)

FILLER = "".join(f"<p>Filler paragraph {i} about treasury services, settlement windows and cut-off times.</p>"
                 for i in range(60))


def _local_pages(port: int) -> dict[str, tuple[int, dict, bytes]]:
    third = f"http://localhost:{port}"
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Instant payments</title>
<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="{third}/tp.css">
<script src="{third}/tp.js"></script><script src="/app.js"></script></head><body>
<header class="bar">Example Bank</header><main><h1>Instant payments</h1>{FILLER}
<p id="target">A single connection to the RTP<sup>&reg;</sup> network with <b>AI-enabled</b> anomaly detection
screens every payment in real time.</p>
<details><summary>Model governance</summary><p>Hidden governance note: every AI model is reviewed by the risk
committee before release.</p></details>{FILLER}
<p id="bottom">The closing statement mentions machine learning for reconciliation exceptions.</p></main></body></html>"""
    cover = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font:16px/1.5 sans-serif} .hdr{position:fixed;top:0;left:0;right:0;height:220px;background:#036;
color:#fff} p{margin:8px 24px}</style></head><body><div class="hdr">Fixed banner</div>
<p>Opening line: our assistant uses generative AI to draft account plans for relationship managers.</p>
<p>Short page.</p></body></html>"""
    html = {"content-type": "text/html; charset=utf-8"}
    return {
        "/robots.txt": (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private\n"),
        "/page.html": (200, html, page.encode("utf-8")),
        "/cover.html": (200, html, cover.encode("utf-8")),
        "/style.css": (200, {"content-type": "text/css"},
                       b"body{margin:0;font:16px/1.6 Georgia,serif}.bar{position:fixed;top:0;left:0;right:0;"
                       b"height:64px;background:#14213d;color:#fff;padding:16px}main{max-width:760px;"
                       b"margin:96px auto 0;padding:0 24px}"),
        "/app.js": (200, {"content-type": "application/javascript"},
                    b"document.documentElement.setAttribute('data-app','1');"),
        "/tp.js": (200, {"content-type": "application/javascript"}, b"window.tracked=1;"),
        "/tp.css": (200, {"content-type": "text/css"}, b"body{background:red}"),
        "/private.html": (200, html, b"<html><body><p>private</p></body></html>"),
        "/redirect": (302, {"location": "/private.html"}, b""),
        "/csp.html": (200, {**html, "content-security-policy": "default-src 'self'; style-src 'self'; "
                                                              "script-src 'self'"},
                      b"<!doctype html><html><head><meta charset='utf-8'><link rel='stylesheet' href='/style.css'>"
                      b"</head><body><main><p>Strict policy page: the bank applies machine learning to flag "
                      b"unusual wire transfers.</p></main></body></html>"),
        "/ws.html": (200, html, (
            "<!doctype html><html><head><meta charset='utf-8'></head><body><p>Socket page: the assistant "
            "summarises payment exceptions with machine learning.</p><script>try { window.__ws = new WebSocket("
            f"'ws://127.0.0.1:{port}/ws'); }} catch (e) {{}}</script></body></html>").encode("utf-8")),
    }


@pytest.fixture(scope="module")
def local_site():
    hits: list[tuple[str, str]] = []
    routes: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append((self.path, self.headers.get("User-Agent", "")))
            status, headers, body = routes.get(self.path, (404, {}, b"nf"))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    routes.update(_local_pages(srv.server_address[1]))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def chromium_browser():
    try:
        cm = shots.chromium()
        browser = cm.__enter__()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Playwright Chromium unavailable: {e}")
    yield browser
    cm.__exit__(None, None, None)


def _live_policy() -> GatePolicy:
    from footprint.net.fetcher import LiveFetcher

    tou = TouRegister({"host": [{"match": "127.0.0.1", "automation": "full", "max_rps": 100.0}]})
    return GatePolicy.from_fetcher(LiveFetcher(None, tou))


def _live(tmp_path, base, path, excerpt, browser):
    st = store_at(tmp_path)
    cap = html_capture(st, url=base + path, body=b"<html></html>")
    return st, screenshot_excerpt(cap, excerpt, 0, len(excerpt), st, _live_policy(), browser=browser, settle_ms=200)


@pytest.mark.network
def test_live_chromium_highlights_and_clips_the_excerpt(tmp_path, local_site, chromium_browser):
    from footprint.net.fetcher import DEFAULT_UA

    base, hits = local_site
    excerpt = ("A single connection to the RTP\u00ae network with AI-enabled anomaly detection screens every "
               "payment in real time.")
    st, res = _live(tmp_path, base, "/page.html", excerpt, chromium_browser)
    assert res.visible is True and res.match == "exact", res.reason
    png = (Path(st.root) / "shots" / f"{res.sha256}.png").read_bytes()
    img, colours = _pixels(png)
    assert img.size[0] <= VIEWPORT["width"] and img.size[1] <= VIEWPORT["height"]
    assert _highlight_pixels(png) > 2000 and _outlined(colours)          # the excerpt is highlighted and boxed
    paths = [p for p, _ in hits]
    assert "/style.css" in paths and "/app.js" in paths                                  # first-party assets
    assert "/tp.js" not in paths and "/tp.css" not in paths                              # third party blocked
    assert all(ua == DEFAULT_UA for p, ua in hits if p == "/page.html")


@pytest.mark.network
def test_live_chromium_scrolls_far_down_a_long_page(tmp_path, local_site, chromium_browser):
    base, _ = local_site
    excerpt = "The closing statement mentions machine learning for reconciliation exceptions."
    st, res = _live(tmp_path, base, "/page.html", excerpt, chromium_browser)
    assert res.visible is True, res.reason
    png = (Path(st.root) / "shots" / f"{res.sha256}.png").read_bytes()
    assert _highlight_pixels(png) > 1000 and _outlined(_pixels(png)[1])  # the clip follows the scroll


@pytest.mark.network
def test_live_chromium_excerpt_absent(tmp_path, local_site, chromium_browser):
    base, _ = local_site
    st, res = _live(tmp_path, base, "/page.html", "This sentence does not appear anywhere on the page.",
                    chromium_browser)
    assert res == ("", "", False)


@pytest.mark.network
def test_live_chromium_collapsed_details_are_not_visible(tmp_path, local_site, chromium_browser):
    base, _ = local_site
    excerpt = "Hidden governance note: every AI model is reviewed by the risk committee before release."
    st, res = _live(tmp_path, base, "/page.html", excerpt, chromium_browser)
    assert res.ok and res.visible is False and "revealed" in res.reason


@pytest.mark.network
def test_live_chromium_fixed_banner_is_moved_out_of_the_way(tmp_path, local_site, chromium_browser):
    base, _ = local_site
    excerpt = "Opening line: our assistant uses generative AI to draft account plans for relationship managers."
    st, res = _live(tmp_path, base, "/cover.html", excerpt, chromium_browser)
    assert res.visible is True, res.reason


@pytest.mark.network
def test_live_chromium_robots_refusal_and_gated_redirect(tmp_path, local_site, chromium_browser):
    base, hits = local_site
    st, res = _live(tmp_path, base, "/private.html", "private page text here", chromium_browser)
    assert res == ("", "", None) and res.reason == "blocked_robots"
    st2, res2 = _live(tmp_path / "b", base, "/redirect", "private page text here", chromium_browser)
    assert res2 == ("", "", None) and res2.reason.startswith("blocked_robots")
    paths = [p for p, _ in hits]
    assert "/redirect" in paths and "/private.html" not in paths


@pytest.mark.network
def test_live_chromium_highlight_survives_a_strict_content_security_policy(tmp_path, local_site, chromium_browser):
    base, _ = local_site
    excerpt = "Strict policy page: the bank applies machine learning to flag unusual wire transfers."
    st, res = _live(tmp_path, base, "/csp.html", excerpt, chromium_browser)
    assert res.visible is True, res.reason
    png = (Path(st.root) / "shots" / f"{res.sha256}.png").read_bytes()
    assert _highlight_pixels(png) > 1000 and _outlined(_pixels(png)[1])


@pytest.mark.network
def test_live_chromium_websockets_never_reach_the_server(tmp_path, local_site, chromium_browser, monkeypatch):
    base, hits = local_site
    seen: list[str] = []
    guard = shots._socket_guard

    def spy(state):
        inner = guard(state)

        def handle(ws):
            seen.append(str(ws.url))
            inner(ws)
        return handle

    monkeypatch.setattr(shots, "_socket_guard", spy)
    excerpt = "Socket page: the assistant summarises payment exceptions with machine learning."
    st, res = _live(tmp_path, base, "/ws.html", excerpt, chromium_browser)
    assert res.visible is True, res.reason
    assert seen == [f"ws://{base.split('://')[1]}/ws"]        # intercepted in the page, before any connection
    paths = [p for p, _ in hits]
    assert "/ws.html" in paths and "/ws" not in paths
