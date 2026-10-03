"""Verification gates V1-V9 for Gemini claims (design Appendix A 2.6; contract: docs/contracts_p3.md section 5).

Nothing Gemini returns counts as evidence until these gates pass. The excerpt stored for a claim is always the exact
slice of the captured document text, never the model's quote: the quote is only used to find that slice, so
``doc_text[start:end] == excerpt`` holds for every result.

- V1  the claim names the passage it was checked against (``passage.passage_id == claim.passage_id``).
- V2  after the payload guard's placeholders are restored, the quote is an exact substring of the passage window, or
      it matches after NFKC, whitespace, quote and dash normalisation (:func:`normalise`); a normalised match is
      mapped back to source offsets (:func:`locate`).
- V3  no inserted ellipsis; the excerpt is MIN_EXCERPT..MAX_EXCERPT characters.
- V4  (repair) each indicator span lies inside the excerpt and passes the rules' lexical test. G6/G7 come from the
      source type only and G9/G10 are undefined, so they are always dropped. Kept spans are re-cut from the excerpt.
- V5  (repair) each named provider appears in the excerpt or the page title (case-insensitive, whole words,
      normalised). Kept names use the source's spelling; URL-like names are always dropped.
- V6  the entity guard passes. Given the source (``document``, ``capture``, ``seeds``, ``profile``),
      :func:`verify_claim` calls ``rules.entity_ok`` on the document text, as ``rules.tag_passage`` does; otherwise
      ``entity_ok`` is a bool or a callable given the excerpt (:func:`entity_guard` binds ``rules.entity_ok``).
- V7  relevance and locus are computed locally: the claim schema carries neither, so nothing Gemini returns can set
      them and V7 never fails here. ``rules.claim_item`` computes them over the verified excerpt.
- V8  the rules' temporal, action-level and SP labels stand. With ``rule_labels``, a temporal or action-level
      disagreement is recorded as a non-blocking V8 for the HC2 review queue. SP is compared after
      ``rules.claim_item`` has graded the surviving indicators (``EvidenceItem.label_disagreements``).
- V9  duplicates: with ``seen``, a second claim on an excerpt that already passed is rejected. Evidence items are
      deduplicated by ``footprint.cluster.dedupe``.

V4, V5 and V8 never reject a claim; V1, V2, V3, V6 and V9 do (``models.BLOCKING_VERIFY_CODES``). V1 and V2 stop the
checks. ``details`` holds one plain line per failure and quotes at most 80 characters; a quote that failed V2 is
quoted as Gemini returned it, with the guard's placeholders still in place.

:func:`reverify_item` backs the demo's "Re-verify" button: it re-hashes the stored capture and text and re-cuts the
excerpt. footprint.rules is imported lazily, so this module also works (and is testable) without it.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import unicodedata
import zlib
from array import array
from collections.abc import Callable, Mapping, MutableSet, Sequence
from functools import lru_cache
from types import ModuleType
from typing import TYPE_CHECKING, Any

from footprint.models import (
    BLOCKING_VERIFY_CODES,
    SOURCE_TYPE_INDICATORS,
    UNDEFINED_INDICATORS,
    V8_LABEL_KEYS,
    Claim,
    EvidenceItem,
    Indicator,
    Passage,
    VerifyResult,
    sha256_text,
)

if TYPE_CHECKING:
    from footprint.capture.store import EvidenceStore

MIN_EXCERPT = 25
MAX_EXCERPT = 600
DETAIL_QUOTE = 80
"""The most characters of any text a detail line quotes."""

_RULES = "footprint.rules"

EntityCheck = Callable[[str], bool]
LexicalTest = Callable[[str, str], bool]
LabelSource = Mapping[str, str] | Callable[[str], Mapping[str, str]]


def _rules() -> ModuleType | None:
    """footprint.rules when it is installed, else None. Errors raised inside an existing rules module propagate."""
    try:
        return importlib.import_module(_RULES)
    except ModuleNotFoundError as exc:
        if exc.name == _RULES:
            return None
        raise


# --------------------------------------------------------------------------- normalisation

_SINGLE_QUOTES = "\u2018\u2019\u201a\u201b\u2032\u2035\u2039\u203a`\xb4\u02bb\u02bc"
_DOUBLE_QUOTES = "\u201c\u201d\u201e\u201f\u2033\u2036\xab\xbb\u301d\u301e\u301f"
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2e3a\u2e3b\ufe58\ufe63\uff0d"
_TRANSLATE = {
    **{ord(c): "'" for c in _SINGLE_QUOTES},
    **{ord(c): '"' for c in _DOUBLE_QUOTES},
    **{ord(c): "-" for c in _DASHES},
}


def _cp1252(byte: int) -> str:
    """The character windows-1252 decodes ``byte`` to; the latin-1 character for its five undefined bytes."""
    try:
        return bytes([byte]).decode("cp1252")
    except UnicodeDecodeError:
        return chr(byte)


# ISO-8859-1 text that really holds windows-1252 bytes keeps 0x80-0x9F as C1 control characters; no web text uses
# them, so they are read as windows-1252 (curly quotes, dashes, the ellipsis, the euro sign).
_C1 = {chr(b): _cp1252(b) for b in range(0x80, 0xA0)}
# UTF-8 decoded as windows-1252 or latin-1 ("mojibake", e.g. a page served as ISO-8859-1 that is really UTF-8): a
# lead byte 0xC2-0xEF read as one character, then continuation bytes 0x80-0xBF under either reading.
_LEADS = re.compile("[\xc2-\xef]")
_CONTINUATION = {**{chr(b): b for b in range(0x80, 0xC0)}, **{_cp1252(b): b for b in range(0x80, 0xC0)}}
# Only characters that web text really carries are repaired: Latin-1 and Latin Extended-A letters and signs, general
# punctuation, currency, letterlike symbols, arrows, dingbats. So an accented capital before a curly quote is not
# misread as an IPA letter (E-acute + right quote would decode to U+0252).
_REPAIRABLE = ((0x00A0, 0x017F), (0x2000, 0x206F), (0x20A0, 0x20CF), (0x2100, 0x214F), (0x2190, 0x21FF),
               (0x2600, 0x27BF))


def _mojibake(text: str, i: int) -> tuple[int, str] | None:
    """(length, character) when ``text[i:]`` starts with one 2- or 3-byte UTF-8 character misread as windows-1252
    or latin-1, and that character is one web text plausibly holds."""
    lead = ord(text[i])
    if not 0xC2 <= lead <= 0xEF:
        return None
    size = 2 if lead < 0xE0 else 3
    if i + size > len(text):
        return None
    raw = bytearray([lead])
    for ch in text[i + 1 : i + size]:
        byte = _CONTINUATION.get(ch)
        if byte is None:
            return None
        raw.append(byte)
    try:
        char = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not any(lo <= ord(char) <= hi for lo, hi in _REPAIRABLE):
        return None
    return size, char


def _normalise_piece(piece: str) -> str:
    if len(piece) == 1 and "\x80" <= piece <= "\x9f":
        piece = _C1[piece]
    if piece.isascii():
        return piece.translate(_TRANSLATE)
    if piece == " \u0301":  # the acute accent after NFKC: the apostrophe it stands for, as when not decomposed
        return "'"
    # translate before NFKC too: NFKC turns the acute accent and the double prime into other characters
    return unicodedata.normalize("NFKC", piece.translate(_TRANSLATE)).translate(_TRANSLATE)


def _invisible(c: str) -> bool:
    """Format and control characters, and variation selectors: dropped by normalisation."""
    return (unicodedata.category(c) in ("Cf", "Cc") or "\ufe00" <= c <= "\ufe0f"
            or "\U000e0100" <= c <= "\U000e01ef")


# Printable ASCII words (no backtick) joined by single spaces are already normal: they are copied in runs, for speed.
_PLAIN_RUN = re.compile("[!-_a-~]+(?: [!-_a-~]+)*")


@lru_cache(maxsize=128)
def _normalise(text: str, repair: bool = False) -> tuple[str, array, array]:
    """(normalised, starts, ends): normalised[j] comes from text[starts[j]:ends[j]].

    The text is read as pieces: one character with any combining marks that follow it, or (with ``repair``) one
    UTF-8 character that was misread as windows-1252 or latin-1. Each piece is normalised on its own, so every
    normalised character maps back to whole source characters, and a normalised span [a, b) is the source span
    [starts[a], ends[b - 1]).
    """
    out: list[str] = []
    starts = array("l")
    ends = array("l")
    i, n = 0, len(text)
    while i < n:
        run = _PLAIN_RUN.match(text, i)
        if run is not None:
            j = run.end()
            if j < n and unicodedata.combining(text[j]):
                j -= 1  # the last character takes its combining marks in the general path
            if j > i:
                out.append(text[i:j])
                starts.extend(range(i, j))
                ends.extend(range(i + 1, j + 1))
                i = j
                continue
        repaired = _mojibake(text, i) if repair and "\xc2" <= text[i] <= "\xef" else None
        if repaired is not None:
            j = i + repaired[0]
            piece = repaired[1]
        else:
            j = i + 1
            while j < n and text[j] >= "\u0300" and unicodedata.combining(text[j]):
                j += 1
            piece = text[i:j]
        for c in _normalise_piece(piece):
            if c.isspace():
                if out and out[-1] == " ":
                    ends[-1] = j  # one space for the whole run, mapped to all of it
                    continue
                c = " "
            elif not " " < c < "\x7f" and _invisible(c):
                continue
            out.append(c)
            starts.append(i)
            ends.append(j)
        i = j
    return "".join(out), starts, ends


def normalise(text: str, *, repair_mojibake: bool = False) -> tuple[str, list[int]]:
    """Normalise ``text`` for quote matching and return ``(normalised, index)``.

    Normalisation applies NFKC, collapses each whitespace run to one space, turns curly quotes, primes and guillemets
    straight and every dash and minus sign into "-", drops invisible format characters (soft hyphen, zero-width
    space and joiners, BOM, bidi marks) and reads windows-1252 bytes that an ISO-8859-1 decode left as C1 control
    characters. With ``repair_mojibake`` it also repairs UTF-8 text that was decoded as windows-1252 or latin-1
    (:func:`locate` tries that second). ``index[j]`` is the offset in ``text`` of the character that produced
    ``normalised[j]`` (non-decreasing). Use :func:`locate` to map a match back to a source span.
    """
    norm, starts, _ = _normalise(text, repair_mojibake)
    return norm, list(starts)


def _passes(*texts: str) -> tuple[bool, ...]:
    """Normalisation passes to try: plain, then mojibake repair when any text could hold mojibake."""
    return (False, True) if any(_LEADS.search(t) for t in texts) else (False,)


def locate(quote: str, text: str, lo: int = 0, hi: int | None = None) -> tuple[int, int] | None:
    """The ``(start, end)`` span of ``quote`` within ``text[lo:hi]``, in ``text`` offsets, or None.

    Surrounding whitespace of the quote is ignored. An exact ``text.find`` is tried first, then a match of the
    normalised quote in the normalised window, then the same with mojibake repaired on both sides. A normalised match
    is mapped back to whole source characters, so ``text[start:end]`` is always the source's own spelling of the
    quote. The first match wins.
    """
    q = quote.strip()
    lo = max(0, lo)
    hi = len(text) if hi is None else min(hi, len(text))
    if not q or lo >= hi:
        return None
    i = text.find(q, lo, hi)
    if i >= 0:
        return i, i + len(q)
    window = text[lo:hi]
    for repair in _passes(q, window):
        nq = _normalise(q, repair)[0].strip()
        if not nq:
            return None
        norm, starts, ends = _normalise(window, repair)
        j = norm.find(nq)
        if j >= 0:
            return lo + starts[j], lo + ends[j + len(nq) - 1]
    return None


def _is_word(c: str) -> bool:
    return c.isalnum() or c == "_"


def _folded(text: str, repair: bool) -> tuple[str, Sequence[int], Sequence[int]]:
    """The normalised, case-folded text with offsets back to ``text`` (casefold may lengthen a character)."""
    norm, starts, ends = _normalise(text, repair)
    if norm.isascii():
        return norm.lower(), starts, ends
    out: list[str] = []
    fstarts: list[int] = []
    fends: list[int] = []
    for c, s, e in zip(norm, starts, ends, strict=True):
        f = c.casefold()
        out.append(f)
        fstarts.extend([s] * len(f))
        fends.extend([e] * len(f))
    return "".join(out), fstarts, fends


def find_name(name: str, text: str) -> tuple[int, int] | None:
    """The source span of ``name`` in ``text``: case-insensitive, whole words, after normalisation; else None.

    A name never matches inside a longer word or version: "GPT-4" is not found in "GPT-4o", nor "Claude 3" in
    "Claude 3.5".
    """
    if not name.strip() or not text:
        return None
    for repair in _passes(name, text):
        target = _normalise(name, repair)[0].strip().casefold()
        if not target:
            return None
        folded, starts, ends = _folded(text, repair)
        left = r"(?<!\w)" if _is_word(target[0]) else ""
        right = r"(?!\w)(?![.\-]\d)" if _is_word(target[-1]) else ""
        match = re.search(left + re.escape(target) + right, folded)
        if match is not None:
            return starts[match.start()], ends[match.end() - 1]
    return None


_ELLIPSIS = re.compile(r"\.(?: ?\.){2,}")


def _ellipses(text: str) -> int:
    """Ellipses in ``text`` after normalisation: the ellipsis character, "...", ". . ." and "[...]" each count once
    (the larger count of the plain and the mojibake-repaired reading)."""
    return max(len(_ELLIPSIS.findall(_normalise(text, repair)[0])) for repair in _passes(text))


def restore_placeholders(text: str, placeholders: Mapping[str, str] | None) -> str:
    """Put the payload guard's redactions back: each placeholder token becomes its original text.

    ``placeholders`` maps a token as it appeared in the text sent to Gemini (``"[EMAIL_1]"``; a key without
    brackets, ``"EMAIL_1"``, means ``"[EMAIL_1]"``) to the original text. Tokens match case-insensitively, longest
    first, in one pass, so a restored value is never expanded again. Unknown tokens are left as they are.
    """
    if not placeholders or not text:
        return text
    tokens: dict[str, str] = {}
    for key, value in placeholders.items():
        key = key.strip()
        if key:
            tokens[(key if key.startswith("[") and key.endswith("]") else f"[{key}]").casefold()] = value
    if not tokens:
        return text
    pattern = "|".join(re.escape(t) for t in sorted(tokens, key=lambda t: (-len(t), t)))
    return re.sub(pattern, lambda m: tokens[m.group(0).casefold()], text, flags=re.IGNORECASE)


def verify_excerpt(text: str, start: int, end: int, excerpt: str) -> bool:
    """True when ``excerpt`` is a non-empty, exact slice ``text[start:end]`` (replay and re-verify checks)."""
    return (bool(excerpt) and 0 <= start <= end <= len(text) and end - start == len(excerpt)
            and text[start:end] == excerpt)


def _clip(text: str, limit: int = DETAIL_QUOTE) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "\u2026"


# --------------------------------------------------------------------------- the gates


def entity_guard(document: Any, capture: Any, seeds: Mapping[str, Any], profile: Any, *, doc_text: str = "",
                 log: list[str] | None = None) -> EntityCheck:
    """A V6 check for :func:`verify_claim`: ``rules.entity_ok`` bound to one source.

    The entity guard judges the source (design 2.3), so the check gives ``rules.entity_ok`` the document text
    ``doc_text`` when it is set, and otherwise the text it is called with (the excerpt). ``log`` gets the guard's
    reason on a failure. Raises RuntimeError when footprint.rules is not installed; then pass ``entity_ok`` to
    :func:`verify_claim` as a bool or callable.
    """
    rules = _rules()
    if rules is None:
        raise RuntimeError("footprint.rules is not installed: pass entity_ok to verify_claim as a bool or callable")
    check_source = rules.entity_ok

    def check(text: str) -> bool:
        return bool(check_source(document, capture, dict(seeds or {}), profile, text=doc_text or text, log=log))

    return check


def _entity_verdict(entity_ok: bool | EntityCheck | None, excerpt: str, doc_text: str, source: tuple[Any, ...],
                    log: list[str] | None) -> tuple[bool, str]:
    """V6: (passes, the guard's reason on a failure, or "")."""
    if entity_ok is not None:
        return bool(entity_ok(excerpt) if callable(entity_ok) else entity_ok), ""
    document, capture, seeds, profile = source
    if document is None and capture is None and profile is None:
        return True, ""  # no source to judge: the contract's default
    if document is None or capture is None or profile is None:
        raise ValueError("V6 needs document, capture and profile together (or pass entity_ok)")
    reasons: list[str] = []
    ok = entity_guard(document, capture, seeds or {}, profile, doc_text=doc_text, log=reasons)(excerpt)
    if log is not None:
        log.extend(reasons)
    return ok, reasons[-1] if reasons else ""


def _window(passage: Passage, doc_text: str) -> str | None:
    """The passage's source text: ``doc_text[start:end]`` (None when that is not the passage), or its own text."""
    if not doc_text:
        return passage.text
    window = doc_text[passage.start : passage.end]
    return window if window == passage.text else None


def _check_indicators(indicators: Sequence[Indicator], excerpt: str, placeholders: Mapping[str, str] | None,
                      lexical: LexicalTest | None, failures: list[str], details: list[str]) -> list[Indicator]:
    """V4: the indicators whose spans are in the excerpt and pass the lexical test, re-cut from the excerpt."""
    kept: list[Indicator] = []
    have: set[tuple[str, str]] = set()
    for ind in indicators:
        span, reason = "", ""
        if ind.code in UNDEFINED_INDICATORS:
            reason = "not a defined indicator"
        elif ind.code in SOURCE_TYPE_INDICATORS:
            reason = "set from the source type only"
        else:
            where = locate(restore_placeholders(ind.span, placeholders), excerpt)
            if where is None:
                reason = "not in the excerpt"
            else:
                span = excerpt[where[0] : where[1]]
                if lexical is not None and not lexical(ind.code, span):
                    reason = f"fails the lexical test for {ind.code}"
        if reason:
            failures.append("V4")
            details.append(f"V4: dropped {ind.code} {_clip(ind.span, 40)!r}: {reason}")
        elif (ind.code, span) not in have:
            have.add((ind.code, span))
            kept.append(Indicator(code=ind.code, span=span))
    return kept


_URL_LIKE = re.compile(r"://|^www\.|\.[a-z]{2,}/", re.IGNORECASE)


def _clean_name(text: str) -> str:
    return " ".join("".join(c for c in text if c.isspace() or not _invisible(c)).split())


def _check_providers(names: Sequence[str], excerpt: str, title: str, placeholders: Mapping[str, str] | None,
                     failures: list[str], details: list[str]) -> list[str]:
    """V5: the provider names found verbatim in the excerpt or the title, in the source's spelling, de-duplicated."""
    kept: list[str] = []
    have: set[str] = set()
    for raw in names:
        name = restore_placeholders(raw, placeholders).strip()
        found = ""
        if not name:
            reason = "empty name"
        elif _URL_LIKE.search(name):
            reason = "URL-like name"
        else:
            for source in (excerpt, title):
                where = find_name(name, source) if source else None
                if where is not None:
                    found = _clean_name(source[where[0] : where[1]])
                    break
            reason = "" if found else "not in the excerpt or the page title"
        if reason:
            failures.append("V5")
            details.append(f"V5: dropped provider {_clip(raw, 40)!r}: {reason}")
            continue
        key = _normalise(found)[0].casefold()
        if key not in have:
            have.add(key)
            kept.append(found)
    return kept


def verify_claim(
    claim: Claim,
    passage: Passage | None,
    doc_text: str,
    *,
    title: str = "",
    placeholders: Mapping[str, str] | None = None,
    entity_ok: bool | EntityCheck | None = None,
    document: Any = None,
    capture: Any = None,
    seeds: Mapping[str, Any] | None = None,
    profile: Any = None,
    indicator_ok: LexicalTest | None = None,
    rule_labels: LabelSource | None = None,
    seen: MutableSet[str] | None = None,
    log: list[str] | None = None,
) -> VerifyResult:
    """Run gates V1-V9 on one claim and return the located source excerpt with what survived V4 and V5.

    ``passage`` is the passage the claim's id names (None when no sent passage has that id) and ``doc_text`` is the
    full text of its document; "" means "use ``passage.text``" (offsets stay document offsets). ``title`` is the page
    title for V5 (default: ``document.title``) and ``placeholders`` the payload guard's map for this passage.

    V6: ``entity_ok`` is the verdict, or a callable given the excerpt (see :func:`entity_guard`). Left as None, the
    claim's source (``document``, ``capture``, ``seeds``, ``profile``) goes to ``rules.entity_ok`` with the document
    text, and the guard's reason is appended to ``log``; with no source either, V6 passes (the contract default).

    ``indicator_ok(code, span)`` is the V4 lexical test; by default ``rules.indicator_ok`` when footprint.rules is
    installed, otherwise no lexical test. ``rule_labels`` (a mapping, or a callable given the excerpt) are the
    rules' labels for V8. ``seen`` collects the excerpts that passed (``"{doc_id}|{start}|{end}"``) and rejects
    repeats (V9).
    """
    if not title and document is not None:
        title = getattr(document, "title", "") or ""
    if passage is None or passage.passage_id != claim.passage_id:
        return VerifyResult(ok=False, failures=["V1"],
                            details=[f"V1: no passage with id {_clip(claim.passage_id, 40)!r} was sent"])
    window = _window(passage, doc_text)
    if window is None:
        return VerifyResult(ok=False, failures=["V2"], details=[
            f"V2: passage {passage.passage_id} is not the document text at {passage.start}-{passage.end}"])
    failures: list[str] = []
    details: list[str] = []
    quote = restore_placeholders(claim.quote, placeholders)
    found = locate(quote, window)
    if found is None:
        failures.append("V2")
        details.append(f"V2: quote not found in passage {passage.passage_id}: {_clip(claim.quote)!r}")
        if _ellipses(quote) > _ellipses(window):  # the likely cause: an elision
            failures.append("V3")
            details.append("V3: the quote holds an ellipsis the source does not")
        return VerifyResult(ok=False, failures=failures, details=details)

    excerpt = window[found[0] : found[1]]
    start, end = passage.start + found[0], passage.start + found[1]
    if _ellipses(quote) > _ellipses(excerpt):
        failures.append("V3")
        details.append("V3: the quote holds an ellipsis the source does not")
    if not MIN_EXCERPT <= len(excerpt) <= MAX_EXCERPT:
        failures.append("V3")
        details.append(f"V3: the excerpt is {len(excerpt)} characters, outside {MIN_EXCERPT}-{MAX_EXCERPT}")

    if indicator_ok is None:
        rules = _rules()
        indicator_ok = getattr(rules, "indicator_ok", None) if rules is not None else None
    indicators = _check_indicators(claim.indicators, excerpt, placeholders, indicator_ok, failures, details)
    providers = _check_providers(claim.named_providers, excerpt, title, placeholders, failures, details)

    passes, reason = _entity_verdict(entity_ok, excerpt, doc_text or window, (document, capture, seeds, profile), log)
    if not passes:
        failures.append("V6")
        details.append("V6: the source fails the entity guard" + (f": {_clip(reason)}" if reason else ""))

    blocked = any(f in BLOCKING_VERIFY_CODES for f in failures)
    if rule_labels is not None and not blocked:
        labels = rule_labels(excerpt) if callable(rule_labels) else rule_labels
        gemini = {"temporal": claim.temporal, "action_level": claim.action_level}
        for key in V8_LABEL_KEYS:
            if key in labels and key in gemini and str(labels[key]) != gemini[key]:
                failures.append("V8")
                details.append(f"V8: {key} is {labels[key]} by the rules and {gemini[key]} by Gemini; "
                               "the rule value stands until an analyst adjudicates")
    if seen is not None and not blocked:
        slot = f"{passage.doc_id}|{start}|{end}"
        if slot in seen:
            failures.append("V9")
            details.append(f"V9: duplicate of a claim already verified at {start}-{end}")
        else:
            seen.add(slot)

    return VerifyResult(
        ok=not any(f in BLOCKING_VERIFY_CODES for f in failures), failures=failures, start=start, end=end,
        excerpt=excerpt, indicators=indicators, providers=providers, details=details,
    )


def reverify_item(item: EvidenceItem, store: EvidenceStore) -> VerifyResult:
    """Re-check a stored evidence item against the evidence store (the demo's "Re-verify" button).

    Checks: the raw capture still hashes to ``capture_sha256``, the extracted text to ``text_sha256``,
    ``text[start:end] == excerpt``, and the excerpt to ``excerpt_sha256`` (and the item key to both). Any mismatch
    gives ``ok=False``, ``failures=["V2"]`` and one detail line per mismatch.
    """
    details: list[str] = []
    try:
        raw = store.get_raw(item.capture_id)
    except KeyError:
        details.append(f"V2: raw capture {item.capture_id[:12]} is missing from the evidence store")
    except (OSError, EOFError, zlib.error):
        details.append(f"V2: raw capture {item.capture_id[:12]} cannot be read")
    else:
        if hashlib.sha256(raw).hexdigest() != item.capture_sha256:
            details.append("V2: the stored raw capture no longer hashes to capture_sha256")
    try:
        text: str | None = store.get_text(item.doc_id)
    except KeyError:
        text = None
        details.append(f"V2: extracted text {item.doc_id[:12]} is missing from the evidence store")
    except (OSError, UnicodeDecodeError):
        text = None
        details.append(f"V2: extracted text {item.doc_id[:12]} cannot be read")
    if text is not None:
        if sha256_text(text) != item.text_sha256:
            details.append("V2: the stored text no longer hashes to text_sha256")
        if not verify_excerpt(text, item.start, item.end, item.excerpt):
            details.append(f"V2: the stored text at {item.start}-{item.end} is not the excerpt")
    if sha256_text(item.excerpt) != item.excerpt_sha256:
        details.append("V2: the excerpt no longer hashes to excerpt_sha256")
    if EvidenceItem.make_key(item.url_final, item.excerpt_sha256) != item.item_key:
        details.append("V2: item_key does not match url_final and excerpt_sha256")
    located = item.start >= 0 and item.end - item.start == len(item.excerpt)
    return VerifyResult(
        ok=not details, failures=["V2"] if details else [],
        start=item.start if located else None, end=item.end if located else None,
        excerpt=item.excerpt if located else "", indicators=list(item.indicators), providers=list(item.providers),
        details=details,
    )


__all__ = [
    "DETAIL_QUOTE",
    "MAX_EXCERPT",
    "MIN_EXCERPT",
    "entity_guard",
    "find_name",
    "locate",
    "normalise",
    "restore_placeholders",
    "reverify_item",
    "verify_claim",
    "verify_excerpt",
]
