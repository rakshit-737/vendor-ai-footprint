"""verify.py: gates V1-V9 for Gemini claims (design Appendix A 2.6; docs/contracts_p3.md section 5).

Offline: no network, no Gemini. footprint.rules is hidden by default (sys.modules entry None) so these tests pin
verify's own behaviour; tests that need the rules hooks install a fake module. All text is synthetic (Acme, V-901).
"""

from __future__ import annotations

import importlib.util
import inspect
import random
import sys
import types
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from footprint import verify as V
from footprint.capture.store import EvidenceStore
from footprint.extract import decode_bytes
from footprint.models import Capture, Claim, Document, Indicator, Passage, SourceFamily, VerifyResult

_p = Path(__file__).resolve().parents[1] / "fixtures" / "p3_samples.py"
_spec = importlib.util.spec_from_file_location("p3_samples", _p)
S = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p3_samples", S)
_spec.loader.exec_module(S)

DOC_ID = "d" * 64
TITLE = "Acme Sentinel \u2013 real\u2011time payment screening"

# Synthetic page text with the typography the gates must survive: curly quotes, em dashes, a non-breaking hyphen,
# NBSP, a double space between sentences and a paragraph break inside the multi-sentence claim.
DOC = (
    "Acme Payments\n\n"
    "Real\u2011time screening\n\n"
    "Acme\u2019s \u201cSentinel\u201d engine uses a machine\xa0learning model \u2014 trained on 2025 data \u2014 "
    "to score every inbound payment.  Alerts are reviewed by an analyst before any payment is held.\n\n"
    "Sentinel runs on Azure OpenAI GPT-4o models in production since March 2025.\n\n"
    "Questions? Contact jane.doe@acme.example or call +44 20 7946 0000 to ask about Sentinel.\n"
)
P_START = DOC.index("Acme\u2019s")
P_END = DOC.index("Questions?") - 2  # the passage window stops before the contact paragraph
EMAIL = "jane.doe@acme.example"


@pytest.fixture(autouse=True)
def _rules_hidden(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Hide footprint.rules unless a test installs a fake; test_real_rules_* see the real module (if any)."""
    if not request.node.name.startswith("test_real_rules"):
        monkeypatch.setitem(sys.modules, "footprint.rules", None)


def fake_rules(monkeypatch: pytest.MonkeyPatch, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType("footprint.rules")
    for name, value in attrs.items():
        setattr(mod, name, value)
    monkeypatch.setitem(sys.modules, "footprint.rules", mod)
    return mod


def passage(start: int = P_START, end: int = P_END, text: str = DOC, doc_id: str = DOC_ID) -> Passage:
    return Passage(passage_id=Passage.make_id(doc_id, start, end), doc_id=doc_id, vendor_id="V-901", start=start,
                   end=end, text=text[start:end], hits=["machine learning"])


PASSAGE = passage()


def claim(quote: str, **kw: Any) -> Claim:
    base: dict[str, Any] = dict(
        passage_id=PASSAGE.passage_id, quote=quote, claim_kind="uses_ai", subject="vendor_product",
        ai_type="predictive_ml", temporal="in_production", action_level="human_reviewed_decision",
        named_providers=[], data_mentioned=[], indicators=[],
    )
    base.update(kw)
    return Claim(**base)


def check(c: Claim, p: Passage | None = PASSAGE, doc: str = DOC, **kw: Any) -> VerifyResult:
    return V.verify_claim(c, p, doc, **kw)


SENTENCE = "Alerts are reviewed by an analyst before any payment is held."


# --------------------------------------------------------------------------- normalise


def test_normalise_quotes_dashes_and_whitespace_with_an_index_map():
    text = "\u201cAI\u201d\xa0\xa0\u2014 Acme\u2019s\n\n\tmodel \u2212 v2 \u2018x\u2019 \u2013 \u2010"
    norm, index = V.normalise(text)
    assert norm == "\"AI\" - Acme's model - v2 'x' - -"
    assert len(index) == len(norm)
    for j, ch in enumerate(norm):  # every normalised char points at the source char that produced it
        src = text[index[j]]
        assert ch == src or (ch == " " and src.isspace()) or ch in "\"'-"
    assert index[norm.index("Acme")] == text.index("Acme")
    assert index == sorted(index)


def test_normalise_nfkc_expansions_and_compositions_map_to_one_source_char():
    text = "Acme classi\ufb01cation \u2026 e\u0301tude \uff21\uff29 caf\xe9 \u2122"
    norm, index = V.normalise(text)
    assert norm == "Acme classification ... \xe9tude AI caf\xe9 TM"
    fi = norm.index("fi")
    assert index[fi] == index[fi + 1] == text.index("\ufb01")
    dots = norm.index("...")
    assert index[dots] == index[dots + 1] == index[dots + 2] == text.index("\u2026")
    assert index[norm.index("\xe9tude")] == text.index("e\u0301")  # composed with its combining accent


def test_normalise_drops_invisible_format_characters():
    text = "ma\xadchine\u200b learn\u2060ing\ufeff model\u200d \u200e!"
    norm, index = V.normalise(text)
    assert norm == "machine learning model !"
    assert len(index) == len(norm)


def test_normalise_reads_cp1252_bytes_left_as_c1_controls():
    raw = "Acme\u2019s \u201cSentinel\u201d \u2013 AI \u2026 \u20ac5".encode("cp1252")
    text = raw.decode("latin-1")  # strict ISO-8859-1 leaves 0x91-0x97 and 0x85 as C1 control characters
    assert "\x92" in text and "\x93" in text and "\x96" in text and "\x85" in text
    assert V.normalise(text)[0] == "Acme's \"Sentinel\" - AI ... \u20ac5"


def test_normalise_repairs_utf8_mojibake_on_request():
    clean = "Acme\u2019s caf\xe9 \u2014 AI"
    for wrong in ("cp1252", "latin-1"):
        text = clean.encode("utf-8").decode(wrong)
        assert text != clean
        assert V.normalise(text)[0] != V.normalise(clean)[0]  # the plain reading leaves mojibake alone
        norm, index = V.normalise(text, repair_mojibake=True)
        assert norm == V.normalise(clean)[0] == "Acme's caf\xe9 - AI"
        assert len(index) == len(norm)
    # real accents stay, also an accented capital or sharp s before a curly quote (they would decode as UTF-8)
    legit = "Ma\xeetre d\u2019h\xf4tel, \xc0 la carte, \xe2ge, CAF\xc9\u2019S, Fu\xdf\u201c"
    expected = "Ma\xeetre d'h\xf4tel, \xc0 la carte, \xe2ge, CAF\xc9'S, Fu\xdf\x22"
    assert V.normalise(legit, repair_mojibake=True)[0] == V.normalise(legit)[0] == expected


def test_normalise_is_idempotent_and_handles_empty_text():
    assert V.normalise("") == ("", [])
    assert V.normalise("don\xb4t")[0] == V.normalise(unicodedata.normalize("NFKC", "don\xb4t"))[0] == "don't"
    for text in (DOC, TITLE, "  lead and trail  ", "\u201c\u2026\u201d"):
        once = V.normalise(text)[0]
        assert V.normalise(once)[0] == once
        assert unicodedata.is_normalized("NFKC", once)


# --------------------------------------------------------------------------- locate


def test_locate_exact_match_inside_the_window_only():
    text = "Acme uses AI. Filler words here. Acme uses AI."
    assert V.locate("Acme uses AI.", text) == (0, 13)
    second = text.rindex("Acme")
    assert V.locate("Acme uses AI.", text, lo=5) == (second, second + 13)
    assert V.locate("Acme uses AI.", text, lo=5, hi=len(text) - 1) is None
    assert V.locate("  Acme uses AI. ", text) == (0, 13)  # surrounding whitespace is not part of the quote
    assert V.locate("Acme uses ML.", text) is None
    assert V.locate("", text) is None and V.locate("   ", text) is None


def test_locate_maps_a_normalised_match_back_to_the_exact_source_slice():
    quote = ("Acme's \"Sentinel\" engine uses a machine learning model - trained on 2025 data - to score every "
             "inbound payment. Alerts are reviewed by an analyst before any payment is held.")
    span = V.locate(quote, DOC)
    assert span is not None
    s, e = span
    assert DOC[s:e].startswith("Acme\u2019s \u201cSentinel\u201d") and DOC[s:e].endswith("is held.")
    assert "\xa0" in DOC[s:e] and "  " in DOC[s:e] and "\u2014" in DOC[s:e]
    assert not DOC[s:e][0].isspace() and not DOC[s:e][-1].isspace()


def test_locate_window_offsets_are_document_offsets():
    quote = "Sentinel runs on Azure OpenAI GPT\u20114o models"  # non-breaking hyphen in the quote only
    s, e = V.locate(quote, DOC, P_START, P_END)
    assert DOC[s:e] == "Sentinel runs on Azure OpenAI GPT-4o models"
    assert V.locate(quote, DOC, 0, P_START) is None


def test_locate_does_not_return_trailing_invisible_characters():
    text = "Acme uses machine learning\xad\u200b to score payments."
    s, e = V.locate("Acme uses machine learning", text)
    assert text[s:e] == "Acme uses machine learning"
    s, e = V.locate("machine learning to score", text)
    assert text[s:e] == "machine learning\xad\u200b to score"


def test_locate_iso_8859_1_decoded_text():
    raw = "Acme\u2019s caf\xe9 assistant uses machine\xa0learning \u2013 in production since 2024.".encode("cp1252")
    quote = "Acme's caf\xe9 assistant uses machine learning - in production since 2024."
    for text in (raw.decode("latin-1"), decode_bytes(raw, "text/html; charset=ISO-8859-1")):
        s, e = V.locate(quote, text)
        assert (s, e) == (0, len(text))
        assert text[s:e] == text  # the stored excerpt keeps the source characters (\x92 / U+2019, NBSP, dash)


def test_locate_straightened_quote_after_an_accented_capital():
    text = "Menu. LE CAF\xc9\u2019S new menu uses machine learning \u2013 since 2024. End."
    s, e = V.locate("LE CAF\xc9'S new menu uses machine learning - since 2024.", text)
    assert text[s:e] == "LE CAF\xc9\u2019S new menu uses machine learning \u2013 since 2024."


def test_locate_mojibake_text_with_a_clean_quote():
    clean = "Acme\u2019s assistant \u2014 now generally available \u2014 uses machine learning."
    text = "Intro. " + clean.encode("utf-8").decode("cp1252", errors="strict") + " Outro."
    s, e = V.locate("Acme's assistant - now generally available - uses machine learning.", text)
    assert text[s:e] == clean.encode("utf-8").decode("cp1252")


def test_locate_round_trips_typographic_variants_both_ways():
    rng = random.Random(1234)
    variants = {"'": ["\u2019", "\u2018", "'", "\xb4"], '"': ["\u201c", "\u201d", '"', "\xab"],
                "-": ["\u2013", "\u2014", "-", "\u2212", "\u2011"],
                " ": [" ", "\xa0", "  ", "\n", " \u200b", "\r\n\t"]}
    base = ("Acme's \"Sentinel\" engine - trained on 2025 data - scores every payment. "
            "Alerts are reviewed by an analyst's team - always.")
    for _ in range(300):
        src = "".join(rng.choice(variants[c]) if c in variants else c for c in base)
        doc = "Intro text.  " + src + "  Outro."
        s, e = V.locate(base, doc)  # Gemini straightened the typography
        assert doc[s:e] == src
        styled = "".join(rng.choice(variants[c]) if c in variants else c for c in base)
        s, e = V.locate(styled, "Intro. " + base + " Outro.")  # Gemini added typography
        assert ("Intro. " + base + " Outro.")[s:e] == base


def test_normalise_index_map_is_monotonic_and_in_range():
    rng = random.Random(99)
    alphabet = list("abc XYZ.,'-") + ["\u2019", "\u201c", "\u2014", "\xa0", "\xad", "\u200b", "e\u0301", "\ufb01",
                                     "\u2026", "\x92", "\xc3\xa9", "\xe2\x80\x99", "\n\n", "\t"]
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        for repair in (False, True):
            norm, index = V.normalise(text, repair_mojibake=repair)
            assert len(index) == len(norm)
            assert index == sorted(index) and all(0 <= k < len(text) for k in index)
            assert "  " not in norm


# --------------------------------------------------------------------------- placeholders


def test_restore_placeholders():
    mapping = {"[EMAIL_1]": EMAIL, "PHONE_1": "+44 20 7946 0000", "[PERSON_1]": "Jane Doe",
               "[PERSON_10]": "John Roe"}
    text = "Ask [PERSON_10] or [person_1] at [EMAIL_1] or [PHONE_1]; [EMAIL_2] stays."
    assert V.restore_placeholders(text, mapping) == (
        f"Ask John Roe or Jane Doe at {EMAIL} or +44 20 7946 0000; [EMAIL_2] stays.")
    assert V.restore_placeholders(text, None) == text
    assert V.restore_placeholders(text, {}) == text
    # values are inserted once, never re-expanded
    assert V.restore_placeholders("[A]", {"[A]": "[B]", "[B]": "x"}) == "[B]"


# --------------------------------------------------------------------------- verify_excerpt


def test_verify_excerpt():
    s = DOC.index(SENTENCE)
    assert V.verify_excerpt(DOC, s, s + len(SENTENCE), SENTENCE)
    assert not V.verify_excerpt(DOC, s + 1, s + 1 + len(SENTENCE), SENTENCE)
    assert not V.verify_excerpt(DOC, s, s + len(SENTENCE) - 1, SENTENCE)
    assert not V.verify_excerpt(DOC, -1, len(SENTENCE) - 1, SENTENCE)
    assert not V.verify_excerpt(DOC, len(DOC) - 3, len(DOC) + 10, SENTENCE)
    assert not V.verify_excerpt(DOC, s, s, "")


# --------------------------------------------------------------------------- V1, V2


def test_exact_quote_passes_with_document_offsets():
    r = check(claim(SENTENCE))
    assert r.ok and r.failures == [] and r.details == []
    assert r.excerpt == SENTENCE
    assert (r.start, r.end) == (DOC.index(SENTENCE), DOC.index(SENTENCE) + len(SENTENCE))
    assert DOC[r.start:r.end] == r.excerpt


def test_v1_unknown_passage_stops():
    for p in (None, passage(P_START, P_END - 1)):
        r = check(claim(SENTENCE), p=p)
        assert not r.ok and r.failures == ["V1"]
        assert r.start is None and r.excerpt == "" and r.indicators == [] and r.providers == []
        assert r.details and r.details[0].startswith("V1")


def test_v2_normalised_multi_sentence_quote_stores_the_source_slice():
    quote = ("Acme's \"Sentinel\" engine uses a machine learning model - trained on 2025 data - to score every "
             "inbound payment. Alerts are reviewed by an analyst before any payment is held.")
    r = check(claim(quote))
    assert r.ok, r.details
    assert r.excerpt != quote
    assert r.excerpt == DOC[r.start:r.end]
    assert r.excerpt.startswith("Acme\u2019s \u201cSentinel\u201d engine uses a machine\xa0learning")
    assert r.excerpt.endswith("before any payment is held.")


def test_v2_quote_outside_the_passage_window_fails():
    r = check(claim("Questions? Contact jane.doe@acme.example or call +44 20 7946 0000 to ask about Sentinel."))
    assert not r.ok and r.failures == ["V2"] and r.excerpt == "" and r.start is None


def test_v2_paraphrase_fails_and_detail_quotes_at_most_80_characters():
    paraphrase = "Acme says its Sentinel engine screens every inbound payment with machine learning " * 3
    r = check(claim(paraphrase))
    assert r.failures == ["V2"] and not r.ok
    assert len(r.details) == 1 and r.details[0].startswith("V2")
    assert len(r.details[0]) < 80 + 60


def test_v2_restores_placeholders_before_matching():
    window = passage(DOC.index("Questions?"), len(DOC) - 1)
    redacted = "Contact [EMAIL_1] or call [PHONE_1] to ask about Sentinel."
    c = claim(redacted, passage_id=window.passage_id)
    r = check(c, p=window, placeholders={"[EMAIL_1]": EMAIL, "[PHONE_1]": "+44 20 7946 0000"})
    assert r.ok, r.details
    assert r.excerpt == f"Contact {EMAIL} or call +44 20 7946 0000 to ask about Sentinel."
    assert DOC[r.start:r.end] == r.excerpt
    missing = check(c, p=window)
    assert missing.failures == ["V2"]
    assert EMAIL not in " ".join(missing.details)  # logs keep the redacted form


def test_v2_document_text_must_match_the_passage_offsets():
    shifted = "X" + DOC
    r = check(claim(SENTENCE), doc=shifted)
    assert r.failures == ["V2"] and not r.ok


def test_v2_without_document_text_uses_the_passage_text():
    r = check(claim(SENTENCE), doc="")
    assert r.ok and (r.start, r.end) == (DOC.index(SENTENCE), DOC.index(SENTENCE) + len(SENTENCE))


def test_v2_iso_8859_1_decoded_document():
    raw = ("Acme Payments Ltd\n\nAcme\u2019s caf\xe9 assistant uses machine\xa0learning \u2013 in production "
           "since 2024.\n").encode("cp1252")
    text = raw.decode("iso-8859-1")
    p = passage(text.index("Acme\x92s"), len(text) - 1, text=text)
    c = claim("Acme's caf\xe9 assistant uses machine learning - in production since 2024.",
              passage_id=p.passage_id)
    r = check(c, p=p, doc=text)
    assert r.ok, r.details
    assert r.excerpt == text[r.start:r.end] and "\x92" in r.excerpt and "\x96" in r.excerpt


# --------------------------------------------------------------------------- V3


def test_v3_inserted_ellipsis_is_reported_with_v2():
    for mark in ("...", "\u2026", "[...]"):
        r = check(claim(f"Acme\u2019s \u201cSentinel\u201d engine {mark} to score every inbound payment."))
        assert r.failures == ["V2", "V3"], mark
        assert not r.ok and any(d.startswith("V3") and "ellipsis" in d for d in r.details)


def test_v3_is_not_added_when_the_source_has_as_many_ellipses_as_the_failed_quote():
    text = "Overview\n\nAcme applies machine learning to fraud, disputes and more\u2026 all in production today.\n"
    p = passage(text.index("Acme"), len(text) - 1, text=text)
    c = claim("Acme applies deep learning to fraud, disputes and more... all in production today.",
              passage_id=p.passage_id)
    assert check(c, p=p, doc=text).failures == ["V2"]


def test_v3_an_ellipsis_that_is_in_the_source_is_not_inserted():
    text = "Overview\n\nAcme applies machine learning to fraud, disputes and more\u2026 all in production today.\n"
    p = passage(text.index("Acme"), len(text) - 1, text=text)
    c = claim("Acme applies machine learning to fraud, disputes and more... all in production today.",
              passage_id=p.passage_id)
    r = check(c, p=p, doc=text)
    assert r.ok and r.failures == []
    assert "\u2026" in r.excerpt


def test_v3_excerpt_length_bounds():
    short = check(claim("machine\xa0learning model"))  # 22 characters
    assert short.failures == ["V3"] and not short.ok
    assert short.excerpt == "machine\xa0learning model" and short.start is not None
    assert check(claim("Alerts are reviewed by an analyst")).ok  # 33 characters

    long_sentence = "Acme uses machine learning " + "and more " * 70 + "in production."
    text = f"Intro.\n\n{long_sentence}\n"
    p = passage(text.index("Acme"), len(text) - 1, text=text)
    r = check(claim(long_sentence, passage_id=p.passage_id), p=p, doc=text)
    assert len(r.excerpt) > V.MAX_EXCERPT and r.failures == ["V3"]
    assert V.MIN_EXCERPT == 25 and V.MAX_EXCERPT == 600


# --------------------------------------------------------------------------- V4


def test_v4_drops_undefined_source_type_and_unlocated_indicators():
    inds = [
        Indicator(code="G2", span="\"Sentinel\" engine uses"),     # straight quotes: kept as the source slice
        Indicator(code="G6", span="machine learning model"),       # source type only
        Indicator(code="G7", span="machine learning model"),
        Indicator(code="G9", span="machine learning model"),       # undefined
        Indicator(code="G10", span="machine learning model"),
        Indicator(code="G1", span="GPT-5"),                         # not in the quote
        Indicator(code="M2", span="   "),                           # empty
        Indicator(code="G11", span="every inbound payment"),
        Indicator(code="G11", span="every  inbound payment"),       # duplicate after normalisation
    ]
    quote = ("Acme's \"Sentinel\" engine uses a machine learning model - trained on 2025 data - to score every "
             "inbound payment.")
    r = check(claim(quote, indicators=inds))
    assert r.ok and r.failures == ["V4"]
    assert [(i.code, i.span) for i in r.indicators] == [
        ("G2", "\u201cSentinel\u201d engine uses"), ("G11", "every inbound payment")]
    for i in r.indicators:
        assert i.span in r.excerpt
    v4 = [d for d in r.details if d.startswith("V4")]
    assert len(v4) == 6  # G6, G7, G9, G10, GPT-5 and the empty span; the duplicate G11 is merged silently
    assert any("G6" in d and "source type" in d for d in v4)
    assert any("G9" in d and "not a defined" in d for d in v4)


def test_v4_lexical_test_injected_or_from_rules(monkeypatch: pytest.MonkeyPatch):
    inds = [Indicator(code="M2", span="trained on 2025 data"), Indicator(code="G2", span="to score every")]
    quote = "uses a machine learning model - trained on 2025 data - to score every inbound payment"
    calls: list[tuple[str, str]] = []

    def lexical(code: str, span: str) -> bool:
        calls.append((code, span))
        return code != "M2"

    r = check(claim(quote, indicators=inds), indicator_ok=lexical)
    assert [i.code for i in r.indicators] == ["G2"] and r.failures == ["V4"] and r.ok
    assert calls == [("M2", "trained on 2025 data"), ("G2", "to score every")]
    assert any("lexical" in d for d in r.details)

    # without an explicit test the rules' indicator_ok is used when footprint.rules exists
    seen: list[str] = []
    fake_rules(monkeypatch, indicator_ok=lambda code, span, **kw: seen.append(code) or code == "M2")
    r = check(claim(quote, indicators=inds))
    assert [i.code for i in r.indicators] == ["M2"] and seen == ["M2", "G2"]


def test_v4_without_rules_or_callable_keeps_located_spans():
    r = check(claim(SENTENCE, indicators=[Indicator(code="G3", span="reviewed by an analyst")]))
    assert r.ok and r.failures == [] and r.indicators == [Indicator(code="G3", span="reviewed by an analyst")]


def test_v4_spans_get_placeholders_restored():
    window = passage(DOC.index("Questions?"), len(DOC) - 1)
    c = claim("Contact [EMAIL_1] or call [PHONE_1] to ask about Sentinel.", passage_id=window.passage_id,
              indicators=[Indicator(code="G5", span="[EMAIL_1]")])
    r = check(c, p=window, placeholders={"[EMAIL_1]": EMAIL, "[PHONE_1]": "+44 20 7946 0000"})
    assert r.ok and r.indicators == [Indicator(code="G5", span=EMAIL)]


# --------------------------------------------------------------------------- V5


def test_v5_providers_must_appear_verbatim_in_the_excerpt_or_title():
    quote = "Sentinel runs on Azure OpenAI GPT-4o models in production since March 2025."
    names = ["azure openai", "GPT-4o", "GPT-4", "Gemini", "Anthropic", "https://openai.com/gpt", "www.openai.com",
             "Azure OpenAI", "screening", ""]
    title = "Acme picks Google Gemini for real\u2011time payment screening"
    r = check(claim(quote, named_providers=names), title=title)
    assert r.ok and r.failures == ["V5"]
    assert r.providers == ["Azure OpenAI", "GPT-4o", "Gemini", "screening"]
    v5 = [d for d in r.details if d.startswith("V5")]
    assert len(v5) == 5  # GPT-4 (not a whole word), Anthropic, two URL-like names, the empty name


def test_v5_matches_whole_words_case_insensitively_after_normalisation():
    text = "Overview\n\nAcme\u2019s assistant uses Claude\xa03.5 Sonnet from Anthropic for summaries.\n"
    p = passage(text.index("Acme"), len(text) - 1, text=text)
    c = claim("Acme's assistant uses Claude 3.5 Sonnet from Anthropic for summaries.", passage_id=p.passage_id,
              named_providers=["claude 3.5 sonnet", "ANTHROPIC", "Claude 3", "Sonnet from"])
    r = check(c, p=p, doc=text)
    assert r.providers == ["Claude 3.5 Sonnet", "Anthropic", "Sonnet from"]
    assert r.failures == ["V5"]  # "Claude 3" is not a whole-word match of "Claude 3.5"


# --------------------------------------------------------------------------- V6


def test_v6_entity_guard_bool_and_callable():
    r = check(claim(SENTENCE), entity_ok=False)
    assert not r.ok and r.failures == ["V6"] and r.excerpt == SENTENCE
    assert any(d.startswith("V6") for d in r.details)

    seen: list[str] = []
    r = check(claim(SENTENCE), entity_ok=lambda text: seen.append(text) or True)
    assert r.ok and seen == [SENTENCE]

    never: list[str] = []
    r = check(claim("not in the source at all, not anywhere"), entity_ok=lambda t: never.append(t) or True)
    assert r.failures == ["V2"] and never == []  # V2 stops before the guard runs


def test_entity_guard_binds_rules_entity_ok(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[Any, ...]] = []

    def entity_ok(document, capture, seeds, profile, *, text="", log=None):
        calls.append((document, capture, seeds, profile, text))
        if "Terrapinn" in text:
            log.append("collision: Terrapinn")
            return False
        return True

    fake_rules(monkeypatch, entity_ok=entity_ok)
    log: list[str] = []
    guard = V.entity_guard("doc", "cap", {"aliases": []}, "profile", log=log)
    assert check(claim(SENTENCE), entity_ok=guard).ok
    assert calls == [("doc", "cap", {"aliases": []}, "profile", SENTENCE)]
    assert guard("Terrapinn hosts an AI conference for payments people.") is False
    assert log == ["collision: Terrapinn"]


def test_entity_guard_needs_the_rules_module():
    with pytest.raises(RuntimeError, match="footprint.rules"):
        V.entity_guard("doc", "cap", {}, "profile")


def source(url: str = "https://news.example/acme", title: str = "") -> tuple[Document, Capture]:
    cap = Capture(capture_id="c" * 64, vendor_id="V-901", family=SourceFamily.IND, collector="site",
                  url_requested=url, retrieved_at="2026-10-02T12:00:00Z")
    doc = Document(doc_id=DOC_ID, capture_id=cap.capture_id, vendor_id="V-901", family=SourceFamily.IND, url=url,
                   title=title, kind="html")
    return doc, cap


def test_entity_guard_judges_the_document_text_when_given(monkeypatch: pytest.MonkeyPatch):
    texts: list[str] = []
    fake_rules(monkeypatch, entity_ok=lambda d, c, s, p, *, text="", log=None: texts.append(text) or True)
    assert V.entity_guard("doc", "cap", {}, "profile", doc_text=DOC)(SENTENCE) is True
    assert V.entity_guard("doc", "cap", {}, "profile")(SENTENCE) is True
    assert texts == [DOC, SENTENCE]


def test_v6_calls_rules_entity_ok_with_the_source_and_document_text(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[Any, ...]] = []

    def entity_ok(document, capture, seeds, profile, *, text="", log=None):
        calls.append((document, capture, seeds, profile, text))
        if "Terrapinn" in document.title:
            log.append("entity guard: collision: 'Terrapinn' is a different entity (https://news.example/acme)")
            return False
        return True

    fake_rules(monkeypatch, entity_ok=entity_ok)
    doc, cap = source()
    log: list[str] = []
    seeds = {"aliases": ["Acme"]}
    r = check(claim(SENTENCE), document=doc, capture=cap, seeds=seeds, profile="profile", log=log)
    assert r.ok and r.failures == [] and log == []
    assert calls == [(doc, cap, seeds, "profile", DOC)]  # the whole document, as rules.tag_passage judges it

    calls.clear()
    r = check(claim(SENTENCE), doc="", document=doc, capture=cap, seeds=seeds, profile="profile")
    assert r.ok and calls[0][4] == PASSAGE.text  # no document text: the passage window

    bad, cap = source(title="Terrapinn payments summit")
    r = check(claim(SENTENCE), document=bad, capture=cap, seeds=seeds, profile="profile", log=log)
    assert not r.ok and r.failures == ["V6"] and r.excerpt == SENTENCE
    prefix = "V6: the source fails the entity guard: "
    assert len(r.details) == 1 and r.details[0].startswith(prefix + "entity guard: collision: 'Terrapinn'")
    assert len(r.details[0]) - len(prefix) <= V.DETAIL_QUOTE and r.details[0].endswith("…")
    assert log == ["entity guard: collision: 'Terrapinn' is a different entity (https://news.example/acme)"]

    calls.clear()  # an explicit verdict or callable wins over the source
    assert check(claim(SENTENCE), entity_ok=True, document=bad, capture=cap, seeds=seeds, profile="profile").ok
    assert calls == []


def test_v6_source_must_be_complete_and_needs_the_rules_module(monkeypatch: pytest.MonkeyPatch):
    doc, cap = source()
    with pytest.raises(RuntimeError, match="footprint.rules"):
        check(claim(SENTENCE), document=doc, capture=cap, seeds={}, profile="profile")
    fake_rules(monkeypatch, entity_ok=lambda *a, **k: True)
    with pytest.raises(ValueError, match="document, capture and profile"):
        check(claim(SENTENCE), document=doc, seeds={})
    assert check(claim(SENTENCE)).ok  # no source and no verdict: V6 passes (the contract default)


def test_v5_title_defaults_to_the_document_title(monkeypatch: pytest.MonkeyPatch):
    fake_rules(monkeypatch, entity_ok=lambda *a, **k: True)
    doc, cap = source(title="Acme picks Google Gemini for screening")
    c = claim(SENTENCE, named_providers=["Gemini"])
    assert check(c, document=doc, capture=cap, seeds={}, profile="profile").providers == ["Gemini"]
    assert check(c, title="Other title", document=doc, capture=cap, seeds={}, profile="profile").failures == ["V5"]
    assert check(c).failures == ["V5"]


# --------------------------------------------------------------------------- V7, V8, V9


def test_v8_rule_labels_win_and_disagreements_are_flagged():
    labels = {"temporal": "in_production", "action_level": "advisory", "sp": "S2", "rl": "R3",
              "locus": "service_feature"}
    r = check(claim(SENTENCE, temporal="planned"), rule_labels=labels)
    assert r.ok and r.failures == ["V8"]
    v8 = [d for d in r.details if d.startswith("V8")]
    assert len(v8) == 2
    assert any("temporal" in d and "in_production" in d and "planned" in d for d in v8)
    assert any("action_level" in d and "advisory" in d for d in v8)

    agree = check(claim(SENTENCE, action_level="advisory"), rule_labels=labels)
    assert agree.ok and agree.failures == []

    got: list[str] = []
    r = check(claim(SENTENCE), rule_labels=lambda text: got.append(text) or {"temporal": "unclear"})
    assert got == [SENTENCE] and r.failures == ["V8"] and r.ok

    blocked: list[str] = []
    r = check(claim(SENTENCE), entity_ok=False, rule_labels=lambda t: blocked.append(t) or {})
    assert blocked == [] and r.failures == ["V6"]


def test_v9_seen_rejects_a_second_claim_on_the_same_excerpt():
    seen: set[str] = set()
    first = check(claim(SENTENCE), seen=seen)
    second = check(claim(SENTENCE.replace("analyst", "analyst")), seen=seen)
    other = check(claim("Alerts are reviewed by an analyst before any payment"), seen=seen)
    assert first.ok and not second.ok and second.failures == ["V9"] and other.ok
    assert len(seen) == 2
    assert any(d.startswith("V9") for d in second.details)


def test_failures_are_unique_and_in_gate_order():
    c = claim("machine\xa0learning model", indicators=[Indicator(code="G9", span="model")],
              named_providers=["OpenAI"])
    r = check(c, entity_ok=False)
    assert r.failures == ["V3", "V4", "V5", "V6"] and not r.ok
    assert [d[:2] for d in r.details] == ["V3", "V4", "V5", "V6"]


def test_verify_claim_is_deterministic():
    quote = "Acme's \"Sentinel\" engine uses a machine learning model"
    c = claim(quote, named_providers=["Sentinel"], indicators=[Indicator(code="G2", span="Sentinel")])
    assert check(c) == check(c)


# --------------------------------------------------------------------------- reverify_item


def _stored_item(tmp_path: Path) -> tuple[EvidenceStore, Any]:
    store = EvidenceStore(tmp_path / "evidence")
    raw = f"<html><body><p>{DOC}</p></body></html>".encode()
    cap = store.put_raw(raw, vendor_id="V-901", family=SourceFamily.PRD, collector="site",
                        url_requested=S.URL, retrieved_at="2026-10-02T12:00:00Z")
    doc_id, _ = store.put_text(DOC)
    s = DOC.index(SENTENCE)
    item = S.item(excerpt=SENTENCE, start=s, doc_id=doc_id, capture_id=cap.capture_id)
    return store, item


def test_reverify_item_passes_on_an_intact_store(tmp_path: Path):
    store, item = _stored_item(tmp_path)
    r = V.reverify_item(item, store)
    assert r.ok and r.failures == [] and r.details == []
    assert (r.start, r.end, r.excerpt) == (item.start, item.end, item.excerpt)


def test_reverify_item_reports_each_mismatch(tmp_path: Path):
    store, item = _stored_item(tmp_path)
    (store.root / "text" / f"{item.doc_id}.txt").write_bytes(DOC.replace("analyst", "algorithm").encode())
    r = V.reverify_item(item, store)
    assert not r.ok and r.failures == ["V2"]
    assert len(r.details) == 2  # text hash and excerpt slice
    assert all(d.startswith("V2") for d in r.details)

    store2, item2 = _stored_item(tmp_path / "b")
    tampered = item2.model_copy(update={"excerpt": SENTENCE.replace("held", "HELD")})
    r = V.reverify_item(tampered, store2)
    assert not r.ok and len(r.details) >= 2  # slice and excerpt hash

    blob = next((store2.root / "blobs").rglob("*.gz"))
    blob.unlink()
    r = V.reverify_item(item2, store2)
    assert not r.ok and any("raw" in d for d in r.details)


def test_reverify_item_missing_text(tmp_path: Path):
    store, item = _stored_item(tmp_path)
    (store.root / "text" / f"{item.doc_id}.txt").unlink()
    r = V.reverify_item(item, store)
    assert not r.ok and r.failures == ["V2"] and any("text" in d for d in r.details)


# --------------------------------------------------------------------------- the real rules module (when present)


def test_real_rules_module_offers_the_hooks_verify_calls():
    rules = pytest.importorskip("footprint.rules")
    for name in ("entity_ok", "indicator_ok"):
        assert callable(getattr(rules, name, None)), name
    inspect.signature(rules.indicator_ok).bind("G1", "OpenAI")
    inspect.signature(rules.entity_ok).bind("doc", "cap", {}, "profile", text="x", log=[])


REAL_SEEDS = {"aliases": ["Acme Payments", "Acme"], "legal_names": ["Acme Payments Ltd"], "domains": ["acme.example"],
              "collisions": ["Acme Anvils"]}


def test_real_rules_v6_judges_the_source():
    pytest.importorskip("footprint.rules")
    first_party = source("https://www.acme.example/sentinel")
    assert check(claim(SENTENCE), document=first_party[0], capture=first_party[1], seeds=REAL_SEEDS,
                 profile=S.profile()).ok

    text = ("Fintech Weekly\n\nA payments firm says its engine uses a machine learning model to score every inbound "
            "payment. Alerts are reviewed by an analyst.\n")
    p = passage(text.index("A payments"), len(text) - 1, text=text)
    c = claim("A payments firm says its engine uses a machine learning model to score every inbound payment.",
              passage_id=p.passage_id)
    doc, cap = source("https://news.example/fintech-weekly")
    log: list[str] = []
    r = check(c, p=p, doc=text, document=doc, capture=cap, seeds=REAL_SEEDS, profile=S.profile(), log=log)
    assert r.failures == ["V6"] and not r.ok
    assert len(log) == 1 and "does not name the vendor" in log[0] and "does not name the vendor" in r.details[0]

    named = text.replace("A payments firm", "Acme Payments Ltd")
    p = passage(named.index("Acme"), len(named) - 1, text=named)
    c = claim("Acme Payments Ltd says its engine uses a machine learning model to score every inbound payment.",
              passage_id=p.passage_id)
    assert check(c, p=p, doc=named, document=doc, capture=cap, seeds=REAL_SEEDS, profile=S.profile()).ok


def test_real_rules_v4_lexical_test():
    pytest.importorskip("footprint.rules")
    quote = "Sentinel runs on Azure OpenAI GPT-4o models in production since March 2025."
    inds = [Indicator(code="G1", span="Azure OpenAI GPT-4o"), Indicator(code="G1", span="in production since")]
    r = check(claim(quote, indicators=inds))
    assert r.ok and r.failures == ["V4"]
    assert r.indicators == [Indicator(code="G1", span="Azure OpenAI GPT-4o")]
    assert any("lexical test for G1" in d for d in r.details)
