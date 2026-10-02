"""Offline tests for footprint.extract."""

from __future__ import annotations

import hashlib

import pytest

from footprint.extract import (
    decode_bytes,
    extract_document,
    find_passages,
    load_core_lexicon,
    split_sentences,
    term_hits,
)
from footprint.models import Capture


class FakeStore:
    def __init__(self):
        self.texts = {}

    def put_text(self, text):
        sha = hashlib.sha256(text.encode()).hexdigest()
        self.texts[sha] = text
        return sha, f"evidence/text/{sha}.txt"


@pytest.fixture(scope="module")
def lex():
    return load_core_lexicon()


def cap(**kw):
    base = dict(capture_id="c" * 64, vendor_id="V-001", family="PRD", collector="t",
                url_requested="https://example.com/a", retrieved_at="2026-10-02T00:00:00Z")
    base.update(kw)
    return Capture(**base)


def make_pdf(pages):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for txt in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({txt}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        cid = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cid} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return bytes(out)


# --- lexicon --------------------------------------------------------------

def test_core_terms_word_boundary(lex):
    assert "machine learning" in term_hits("We use Machine Learning daily.", lex)
    assert "AI" in term_hits("Our AI platform.", lex)
    assert "AI" not in term_hits("We said ai and PAID and MAIN.", lex)
    assert "LLMs?" in term_hits("Built on LLMs.", lex)
    assert "GenAI" in term_hits("A GenAI feature.", lex)
    assert term_hits("A neuralgic retrainer.", lex) == []


@pytest.mark.parametrize("txt", [
    "We print the Intelligent Mail barcode (IMb) on every piece.",
    "Intelligent inserting at our plant.",
    "Business intelligence dashboards.",
    "Service is in our DNA.",
    "Upgrade to Oracle 23ai now.",
    "Automic agents run batch jobs.",
])
def test_suppressors(lex, txt):
    assert term_hits(txt, lex) == []


def test_provider_guards(lex):
    assert "Claude" not in term_hits("Claude Monet painted lilies.", lex)
    assert "Claude" in term_hits("We use Claude from Anthropic.", lex)
    assert "Copilot" not in term_hits("The copilot landed. Copilot app.", lex)
    assert "Copilot" in term_hits("Microsoft Copilot rollout.", lex)
    assert "Gemini" not in term_hits("Gemini is a zodiac sign.", lex)
    assert "Gemini" in term_hits("Google Gemini assists agents.", lex)


# --- sentences / passages -------------------------------------------------

def test_sentence_split_abbreviations():
    t = "Acme Inc. and Foo Corp. merged in the U.S. today. Bar L.L.C. uses tools, e.g. Excel. Next one."
    sents = [t[s:e] for s, e in split_sentences(t)]
    assert sents == ["Acme Inc. and Foo Corp. merged in the U.S. today.",
                     "Bar L.L.C. uses tools, e.g. Excel.", "Next one."]


def test_passages_window_exact(lex):
    t = "Intro here. Second line. We deploy machine learning models. Fourth line. Fifth line."
    ps = find_passages(t, lex)
    assert len(ps) == 1
    s, e, hits = ps[0]
    assert t[s:e] == "Second line. We deploy machine learning models. Fourth line."
    assert hits == ["machine learning"]


def test_passages_cap_700(lex):
    filler = "Lorem ipsum dolor sit amet " * 20 + "."
    t = f"{filler} Our AI works. {filler}"
    for s, e, _ in find_passages(t, lex):
        assert e - s <= 700 and "AI" in t[s:e]


def test_long_sentence_clamped(lex):
    t = "AI " + "word " * 300 + "end."
    (s, e, _), = find_passages(t, lex)
    assert e - s == 700 and s == 0


# --- decoding / html ------------------------------------------------------

def test_decode_latin1_header_and_meta():
    raw = "<html><body><p>Café résumé</p></body></html>".encode("latin-1")
    assert "Café" in decode_bytes(raw, "text/html; charset=ISO-8859-1")
    raw2 = b'<html><head><meta charset="iso-8859-1"></head>' + raw
    assert "résumé" in decode_bytes(raw2, "text/html")
    assert "Café" in decode_bytes(raw, "text/html")  # fallback detection


HTML = b"""<html><head><title>AI News</title>
<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2025-03-04T10:00:00Z"}</script>
<meta property="article:published_time" content="2024-01-01"></head>
<body><nav>Menu</nav><article><h1>AI News</h1><p>Acme Inc. launched a generative AI assistant for banks.</p>
<p>It answers questions in seconds.</p><table><tr><td>Model</td><td>LLM</td></tr></table></article></body></html>"""


def test_extract_html_document(lex):
    st = FakeStore()
    d = extract_document(cap(content_type="text/html; charset=utf-8"), HTML, st)
    assert d.kind == "html" and d.title == "AI News"
    assert d.published == "2025-03-04" and d.date_basis == "json-ld datePublished"
    text = st.texts[d.doc_id]
    assert "generative AI assistant" in text and d.text_len == len(text)
    assert find_passages(text, lex)


def test_html_date_fallbacks():
    st = FakeStore()
    h = b'<html><head><meta property="article:published_time" content="2024-05-06T00:00:00Z"></head><body><p>x</p></body></html>'
    d = extract_document(cap(content_type="text/html"), h, st)
    assert (d.published, d.date_basis) == ("2024-05-06", "meta article:published_time")
    d2 = extract_document(cap(content_type="text/html",
                              headers={"last-modified": "Tue, 07 Jan 2025 10:00:00 GMT"}),
                          b"<html><body><p>plain</p></body></html>", st)
    assert d2.published in ("2025-01-07", "") or d2.date_basis == "htmldate"
    assert d2.date_basis in ("last-modified", "htmldate")


def test_extract_pdf_pages(lex):
    st = FakeStore()
    raw = make_pdf(["Page one about payments.", "Page two uses machine learning."])
    d = extract_document(cap(url_requested="https://example.com/r.pdf", content_type="application/pdf"), raw, st)
    text = st.texts[d.doc_id]
    assert d.kind == "pdf" and len(d.pages) == 2 and d.pages[0] == 0
    assert text[d.pages[1]:].startswith("Page two")
    assert find_passages(text, lex)


def test_extract_json():
    st = FakeStore()
    d = extract_document(cap(content_type="application/json"), b'{"b":1,"a":"AI"}', st)
    assert d.kind == "json" and '"a": "AI"' in st.texts[d.doc_id]
