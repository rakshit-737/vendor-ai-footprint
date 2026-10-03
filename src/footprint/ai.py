"""Gemini layer: the only module that talks to Gemini (design Appendix A 2.6; docs/contracts_p3.md section 4).

Public vendor text crosses to Gemini only inside a ``PublicPayload`` whose items ``PayloadGuard.check`` released.
The guard withholds an item that carries "Meridian", the team name, a profile free-text shingle or a volume phrase,
skips hosts whose terms, robots.txt (Google-Extended) or Content-Signal refuse AI use, and redacts e-mail addresses,
phone numbers and seeded person names behind per-item placeholders. Every call, cache lookup and withholding is
logged to ``llm_calls.jsonl`` without any text.

Back ends: ``GeminiLLM`` (google-genai, stateless generate_content, structured JSON output, seed 1234, minimal
thinking, no tools), ``CachedLLM`` (deterministic JSON cache under evidence/llm_cache, the only thing replay reads)
and ``NullLLM`` (keyless: the rules and the local fallbacks do the work).

Import rule: this module never imports rules, verdict, risk, compose, review, criticality, depth, workbook or
pipeline, and its functions never take a profile, tier, verdict, review or finding object.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from footprint.models import AssessmentMode, Claim, ClaimBatch, Document, Passage
from footprint.net.tou import HARD_MANUAL, TouRegister, load_tou

if TYPE_CHECKING:
    from footprint.net.robots import RobotsCache

# =========================================================================== constants

MODEL_CHAIN: tuple[str, ...] = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
SEED = 1234
CACHE_DIR = Path("evidence/llm_cache")
AI_POLICY_PATH = Path("evidence/ai_policy.jsonl")
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
PROMPT_NAMES: tuple[str, ...] = ("expand_v1", "triage_v1", "extract_v1")
BATCH_SIZE = 12
BATCH_SIZE_TIGHT = 25           # design 2.6: 25 passages per call when the quota gate requires it
MAX_BATCH_CHARS = 100_000       # whole-page chunks (up to 30k characters) keep a batch within this many characters
TRIAGE_BATCH_SIZE = 120
MIN_INTERVAL_S = 6.0
MAX_ATTEMPTS = 5
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 60.0
BAD_OUTPUT_RETRIES = 1          # an unparseable or wrongly shaped reply is asked again once, then skipped
REQUEST_TIMEOUT_MS = 120_000
MAX_NAMES = 50
MAX_NAME_CHARS = 80
GOOGLE_EXTENDED = "Google-Extended"
GEMINI_UNAVAILABLE_NOTE = "Gemini unavailable – rules + local model"

PROFILE_GUARD_FIELDS: tuple[str, ...] = ("description", "service", "business_process", "data_accessed",
                                         "data_volume")
"""VendorProfile fields of the free-text columns D, E, H, J and K. The pipeline passes their values (every row,
V-000 included) to PayloadGuard as ``profile_texts``; the enumerated columns F and I, the website (G) and the vendor
name (C) are never blocked."""

LLMRole = Literal["expand", "triage", "extract"]
ReplyStatus = Literal["ok", "cache_hit", "cache_miss", "error", "quota", "null", "budget"]
WithheldReason = Literal["hard_block_meridian", "hard_block_team", "profile_shingle", "volume_phrase",
                         "host_ai_disallowed", "robots_google_extended", "content_signal"]
WITHHELD_REASONS: tuple[str, ...] = get_args(WithheldReason)
HOST_REASONS: frozenset[str] = frozenset({"host_ai_disallowed", "robots_google_extended", "content_signal"})
AUDIT_STATUSES: frozenset[str] = frozenset(get_args(ReplyStatus)) | {"withheld"}
AUDIT_FIELDS: frozenset[str] = frozenset({
    "ts", "vendor_id", "role", "model", "status", "cache_key", "prompt_sha256", "schema_sha256", "payload_sha256",
    "items_sent", "withheld", "n_out", "attempts", "hosts", "n_dropped", "n_skipped",
})

# =========================================================================== closed payload types


class PayloadItem(BaseModel):
    """One public text item: the only shape public text crosses to Gemini in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    text: str


class PublicPayload(BaseModel):
    """Everything one Gemini call sees besides the frozen prompt and schema: the company's public name and items."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    company: str
    items: tuple[PayloadItem, ...]

    def sha256(self) -> str:
        """sha256 of the canonical JSON (sorted keys, compact separators, UTF-8)."""
        return _sha(_canonical(self.model_dump(mode="json")))


class GuardInput(BaseModel):
    """A candidate item before the guard. ``url`` is the cited URL, used for the host policy only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    text: str
    url: str = ""


class Withheld(BaseModel):
    """An item the guard kept back; logged without text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    reason: WithheldReason


class LLMReply(BaseModel):
    """What a back end returns: the parsed JSON object (None when there is none) and how it was obtained."""

    model_config = ConfigDict(extra="forbid")

    data: dict | None
    model: str = ""
    status: ReplyStatus
    cache_key: str = ""
    attempts: int = 0


class TriageRow(BaseModel):
    """One candidate page for triage, rendered ``id | path | title | date | family``.

    ``url`` is never rendered: the guard uses it for the host policy, and the audit log keeps only its host.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    title: str = ""
    date: str = ""
    family: str = ""
    url: str = ""


# --------------------------------------------------------------------------- response schemas (triage, expand)

TriageReason = Literal["ai_in_products", "ai_in_operations", "ai_policy", "subprocessors_or_data", "ai_filing",
                       "ai_job_duties"]
NameKind = Literal["alias", "product", "ai_programme", "provider", "affiliate"]


class TriagePick(BaseModel):
    model_config = ConfigDict(json_schema_extra={"description": "One row worth reading."})

    id: str = Field(description="the row id, copied exactly as given")
    priority: Literal["high", "medium"]
    reason: TriageReason


class TriageBatch(BaseModel):
    model_config = ConfigDict(json_schema_extra={"description": "Rows worth reading; empty if none."})

    items: list[TriagePick]


class ExpandedName(BaseModel):
    model_config = ConfigDict(json_schema_extra={"description": "One name found in the texts."})

    name: str = Field(description="the name as written in the texts; a name only, never a URL")
    kind: NameKind


class NameBatch(BaseModel):
    model_config = ConfigDict(json_schema_extra={"description": "Names found in the texts; empty if none."})

    names: list[ExpandedName]


EXTRACT_SCHEMA: dict[str, Any] = ClaimBatch.model_json_schema()
TRIAGE_SCHEMA: dict[str, Any] = TriageBatch.model_json_schema()
EXPAND_SCHEMA: dict[str, Any] = NameBatch.model_json_schema()

# =========================================================================== hashing, prompts, rendering


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=None)
def prompt_text(name: str) -> str:
    """The frozen prompt ``prompts/<name>.txt`` (LF line ends). Refuses a prompt that mentions "Meridian"."""
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError(f"invalid prompt name {name!r}")
    text = (PROMPTS_DIR / f"{name}.txt").read_bytes().decode("utf-8").replace("\r\n", "\n")
    if _has_meridian(text):
        raise ValueError(f"prompt {name} contains a forbidden word")
    return text


def prompt_sha256(name: str) -> str:
    return _sha(prompt_text(name))


def schema_sha256(schema: Mapping[str, Any]) -> str:
    return _sha(_canonical(schema))


def cache_key(model: str, prompt_sha256: str, schema_sha256: str, payload_sha256: str, seed: int = SEED) -> str:
    """sha256 of ``"{model}|{prompt_sha}|{schema_sha}|{payload_sha}|{seed}"`` (design 2.6)."""
    return _sha(f"{model}|{prompt_sha256}|{schema_sha256}|{payload_sha256}|{seed}")


def render_payload(role: LLMRole, payload: PublicPayload) -> str:
    """The user content of one call: a pure function of the payload (so the payload hash covers what is sent)."""
    lines = [f"Company: {payload.company}"]
    if role == "triage":
        lines += ["Rows (id | path | title | date | family):"]
        lines += [f"{item.id} | {item.text}" for item in payload.items]
    else:
        lines += ["Texts:" if role == "expand" else "Items:"]
        for item in payload.items:
            lines += ["", f"[{item.id}] ({item.kind}) {item.text}"]
    return "\n".join(lines) + "\n"


# =========================================================================== text normalisation

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                         "“": '"', "”": '"', "„": '"', "″": '"'})
_POSSESSIVE = re.compile(r"(?<=[^\W\d_])'s\b")
_WORD = re.compile(r"[^\W_]+")
_SCAN = re.compile(r"[^\W_]+|[^\w\s]")
_NUMBER = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"
    r"(?:\s*(thousand|million|billion|trillion)\b|(k|mm|mn|m|bn|b|tn)\b)?"
)
_SCALE = {"thousand": 10**3, "k": 10**3, "million": 10**6, "mm": 10**6, "mn": 10**6, "m": 10**6,
          "billion": 10**9, "bn": 10**9, "b": 10**9, "trillion": 10**12, "tn": 10**12}
_VOLUME_STOP = frozenset(
    "a an the of per and or are is was were be been in on for to with under over about around approximately "
    "some than more less each every across by from at its their our your his her we they this that these those "
    "up nearly almost roughly approx estimated".split()
)
SHINGLE = 6
# "Meridian" not inside a longer word: digits and underscores are boundaries ("Meridian_Vendor_Input",
# "Meridian2026" block), letters are not ("MeridianLink" and "meridians" are other words).
_MERIDIAN = re.compile(r"(?<![^\W\d_])meridian(?![^\W\d_])")


def _fold(text: str) -> str:
    """NFKC, zero-width characters removed, curly quotes straightened, case-folded."""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).translate(_QUOTES).casefold()


def _has_meridian(text: str) -> bool:
    return bool(_MERIDIAN.search(_fold(text)))


def _stem(token: str) -> str:
    """Light plural folding, applied on both sides of every comparison."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss") and not token.isdigit():
        return token[:-1]
    return token


def _number_value(m: re.Match[str]) -> str:
    """Canonical digits of a number expression: '1.4 million', '1,400,000' and '1.4m' all give '1400000'."""
    whole = m.group(1).replace(",", "")
    frac = m.group(2) or ""
    scale = _SCALE.get(m.group(3) or m.group(4) or "", 1)
    try:
        value = Decimal(f"{whole}.{frac}" if frac else whole) * scale
    except InvalidOperation:  # pragma: no cover - the regex only admits digits
        return m.group(0)
    return format(value.normalize(), "f")


def _tokens(text: str) -> list[str]:
    """Normalised word tokens: folded, possessives dropped, numbers canonical, light plural folding."""
    s = _POSSESSIVE.sub("", _fold(text))
    s = _NUMBER.sub(lambda m: f" {_number_value(m)} ", s)
    return [_stem(t) for t in _WORD.findall(s)]


def _volume_phrases(text: str) -> set[tuple[str, tuple[str, ...]]]:
    """(canonical number, first one or two content words after it) for every number followed by words.

    Leading stop words are skipped ("1.4 million of our customers"); punctuation, another number or a stop word
    after the first content word ends the phrase. A number with no word after it is not a volume phrase.
    """
    s = _POSSESSIVE.sub("", _fold(text))
    out: set[tuple[str, tuple[str, ...]]] = set()
    for m in _NUMBER.finditer(s):
        words: list[str] = []
        for t in _SCAN.finditer(s, m.end(), min(len(s), m.end() + 160)):
            w = t.group(0)
            if not w[0].isalnum() or w.isdigit():
                break
            if w in _VOLUME_STOP:
                if words:
                    break
                continue
            words.append(_stem(w))
            if len(words) == 2:
                break
        if words:
            out.add((_number_value(m), tuple(words)))
    return out


# =========================================================================== payload guard

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_PHONE = re.compile(
    r"(?<![\w+])(?:"
    r"\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{1,4}(?:[ .-]\d{2,4}){1,4}"
    r"|(?:1[ .-])?(?:\(\d{3}\) ?|\d{3}[.-])\d{3}[.-]\d{4}"
    r")(?![\w-])"
)
_PLACEHOLDER = re.compile(r"\[(?:EMAIL|PHONE|PERSON)_\d+\]")

# Every item a guard released, by digest of (kind, text): GeminiLLM sends nothing else.
_RELEASED: set[str] = set()


def _item_digest(kind: str, text: str) -> str:
    return _sha(f"{kind}\x00{text}")


def _host(url: str) -> str:
    if not url or "://" not in url:
        return ""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _hard_manual(host: str) -> bool:
    return any(host == m or host.endswith("." + m) for m in HARD_MANUAL)


def _team_pattern(team_name: str) -> re.Pattern[str] | None:
    """Whole words of the team name, with or without a leading "Team "; a core under 4 characters needs it."""
    words = _fold(team_name).split()
    if not words:
        return None
    core = words[1:] if words[0] == "team" and len(words) > 1 else words
    body = r"\s+".join(map(re.escape, core))
    if len("".join(core)) < 4:
        return re.compile(rf"(?<![^\W_])team\s+{body}(?![^\W_])")
    return re.compile(rf"(?<![^\W_])(?:team\s+)?{body}(?![^\W_])")


class PayloadGuard:
    """Decides, item by item, what may be sent to Gemini (design 2.6 payload guard).

    ``profile_texts`` are the profile free-text values (columns D, E, H, J, K of every row). Values of six or more
    words are matched as normalised 6-word shingles, values of four or five words as a whole; shorter values never
    block. Volume phrases (a number plus the words after it) are matched with numbers normalised. ``allow_shingles``
    maps a phrase to the written reason it may still be sent (for example, it appears on the vendor's own public
    pages). ``host_policy(url) -> (allowed, reason)`` refuses hosts; fiserv.com and linkedin.com are refused even
    without one.
    """

    def __init__(self, profile_texts: Sequence[str], team_name: str, scrub_names: Sequence[str] = (), *,
                 host_policy: Callable[[str], tuple[bool, str]] | None = None,
                 allow_shingles: Mapping[str, str] | None = None) -> None:
        if isinstance(profile_texts, str) or isinstance(scrub_names, str):
            raise TypeError("profile_texts and scrub_names are sequences of strings")
        self._team = _team_pattern(team_name or "")
        self._host_policy = host_policy
        self._shingles: set[tuple[str, ...]] = set()
        self._wholes: set[tuple[str, ...]] = set()
        self._volumes: dict[str, set[tuple[str, ...]]] = {}
        for value in profile_texts:
            if not isinstance(value, str):
                raise TypeError("profile_texts must be strings")
            self._add_profile_value(value)
        self.allowances = 0
        for phrase, reason in (allow_shingles or {}).items():
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("every allowed shingle needs a written reason")
            self._allow(phrase)
        self._whole_lengths = sorted({len(w) for w in self._wholes})
        names = sorted({n.strip() for n in scrub_names if n and n.strip()}, key=lambda n: (-len(n), n))
        self._names = [re.compile(r"(?<![^\W_])" + r"\s+".join(map(re.escape, n.split())) + r"(?![^\W_])",
                                  re.IGNORECASE) for n in names]
        self._maps: dict[str, dict[str, str]] = {}
        self.n_checked = 0
        self.n_sent = 0
        self.withheld_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ blocklist
    def _add_profile_value(self, value: str) -> None:
        n_words = len(value.split())
        toks = _tokens(value)
        if n_words >= SHINGLE and len(toks) >= SHINGLE:
            self._shingles.update(tuple(toks[i:i + SHINGLE]) for i in range(len(toks) - SHINGLE + 1))
        elif n_words >= 4 and toks:
            self._wholes.add(tuple(toks))
        for number, words in _volume_phrases(value):
            self._volumes.setdefault(number, set()).add(words)

    def _allow(self, phrase: str) -> None:
        toks = _tokens(phrase)
        before = len(self._shingles) + len(self._wholes) + sum(map(len, self._volumes.values()))
        self._shingles.difference_update(tuple(toks[i:i + SHINGLE]) for i in range(len(toks) - SHINGLE + 1))
        self._wholes.discard(tuple(toks))
        for number, words in _volume_phrases(phrase):
            self._volumes.get(number, set()).discard(words)
        after = len(self._shingles) + len(self._wholes) + sum(map(len, self._volumes.values()))
        self.allowances += before - after

    def content_reason(self, text: str) -> str | None:
        """The withholding reason the text itself triggers (hard blocks, profile shingles, volume phrases)."""
        folded = _fold(text)
        if _MERIDIAN.search(folded):
            return "hard_block_meridian"
        if self._team is not None and self._team.search(folded):
            return "hard_block_team"
        if self._shingles or self._wholes:
            toks = _tokens(text)
            if self._shingles and any(tuple(toks[i:i + SHINGLE]) in self._shingles
                                      for i in range(len(toks) - SHINGLE + 1)):
                return "profile_shingle"
            for n in self._whole_lengths:
                if any(tuple(toks[i:i + n]) in self._wholes for i in range(len(toks) - n + 1)):
                    return "profile_shingle"
        if self._volumes:
            for number, words in _volume_phrases(text):
                if any(words[:len(w)] == w for w in self._volumes.get(number, ())):
                    return "volume_phrase"
        return None

    def _host_reason(self, url: str) -> str | None:
        host = _host(url)
        if host and _hard_manual(host):
            return "host_ai_disallowed"
        if self._host_policy is None:
            return None
        if not url:
            return "host_ai_disallowed"             # no URL, no host decision: fail closed
        allowed, reason = self._host_policy(url)
        if allowed:
            return None
        return reason if reason in HOST_REASONS else "host_ai_disallowed"

    # ------------------------------------------------------------------ redaction
    def _redact(self, text: str) -> tuple[str, dict[str, str]]:
        spans: list[tuple[int, int, str]] = []
        for label, rx in (("EMAIL", _EMAIL), ("PHONE", _PHONE)):
            spans += [(m.start(), m.end(), label) for m in rx.finditer(text)]
        for rx in self._names:
            spans += [(m.start(), m.end(), "PERSON") for m in rx.finditer(text)]
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        counters: dict[str, int] = {}
        seen: dict[tuple[str, str], str] = {}
        mapping: dict[str, str] = {}
        out: list[str] = []
        pos = 0
        for start, end, label in spans:
            if start < pos:
                continue
            original = text[start:end]
            ph = seen.get((label, original))
            if ph is None:
                counters[label] = counters.get(label, 0) + 1
                ph = f"[{label}_{counters[label]}]"
                seen[(label, original)] = ph
                mapping[ph] = original
            out += [text[pos:start], ph]
            pos = end
        out.append(text[pos:])
        return "".join(out), mapping

    # ------------------------------------------------------------------ public API
    def check(self, items: Sequence[GuardInput]) -> tuple[list[PayloadItem], list[Withheld]]:
        """Split ``items`` (in input order) into redacted sendable items and withholdings (logged without text)."""
        sendable: list[PayloadItem] = []
        withheld: list[Withheld] = []
        for item in items:
            if not isinstance(item, GuardInput):
                raise TypeError("PayloadGuard.check takes GuardInput items")
            self.n_checked += 1
            reason = self.content_reason(item.text) or self.content_reason(item.kind) or self._host_reason(item.url)
            if reason:
                self._maps.pop(item.id, None)
                withheld.append(Withheld(id=item.id, reason=reason))
                self.withheld_counts[reason] = self.withheld_counts.get(reason, 0) + 1
                continue
            text, mapping = self._redact(item.text)
            self._maps[item.id] = mapping
            out = PayloadItem(id=item.id, kind=item.kind, text=text)
            _RELEASED.add(_item_digest(out.kind, out.text))
            sendable.append(out)
            self.n_sent += 1
        return sendable, withheld

    def placeholders(self, item_id: str) -> dict[str, str]:
        """Placeholder -> original text for one released item ({} when it had none or is unknown)."""
        return dict(self._maps.get(item_id, {}))

    def restore(self, item_id: str, text: str) -> str:
        """Put the originals back into a returned quote (unknown placeholders stay as they are)."""
        mapping = self._maps.get(item_id)
        if not mapping:
            return text
        return _PLACEHOLDER.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)


def _check_released(payload: PublicPayload) -> None:
    if _has_meridian(payload.company):
        raise ValueError("the company name in a payload must not contain a hard-blocked word")
    unreleased = [item.id for item in payload.items if _item_digest(item.kind, item.text) not in _RELEASED]
    if unreleased:
        raise ValueError(f"{len(unreleased)} payload item(s) did not pass the payload guard")


def _check_company(company: str, guard: PayloadGuard) -> None:
    if not company.strip():
        raise ValueError("company is required")
    if _has_meridian(company) or (guard._team is not None and guard._team.search(_fold(company))):
        raise ValueError("the company name fails the payload guard")


# =========================================================================== host policy (ToS, robots, signals)


def parse_content_signals(body: bytes | str) -> dict[str, str]:
    """Content-Signal directives of a robots.txt body, e.g. {"search": "yes", "ai-train": "no"}; any "no" wins."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    signals: dict[str, str] = {}
    for line in text.lstrip("﻿").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"(?i)content-signal\s*:\s*(.*)$", line)
        if not m:
            continue
        for part in m.group(1).split(","):
            if "=" not in part:
                continue
            key, value = (x.strip().lower() for x in part.split("=", 1))
            if key and (value == "no" or key not in signals):
                signals[key] = value
    return signals


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


class HostAiPolicy:
    """``host_policy`` for PayloadGuard: may passages from this URL's host go to Gemini? Returns (allowed, reason).

    1. The ToS register: a host whose entry (the default entry for unlisted hosts) sets ``ai_processing_allowed``
       false is refused, as are fiserv.com and linkedin.com.
    2. Live (``robots`` given): robots.txt disallowing Google-Extended for the URL, or a Content-Signal with
       ai-train=no or ai-input=no, refuses it. Each decision is appended once to ``decisions_path`` as
       {host, url, allowed, reason, robots_sha256, decided_on}.
    3. Replay (no ``robots``): only records with ``decided_on`` on or before ``as_of`` count (all of them when
       ``as_of`` is empty). The URL's latest such record decides; else the latest record of each other URL of the
       host (any refusal refuses); else the ToS decision alone.

    The Content-Signal check needs the robots.txt body: ``robots_body(url)`` when given, else ``robots.body(url)``
    when the cache offers it, else one extra fetch of /robots.txt per origin through ``robots._fetch_raw``.
    """

    def __init__(self, tou: TouRegister, decisions_path: str | Path | None = AI_POLICY_PATH, *,
                 robots: RobotsCache | Any | None = None, robots_body: Callable[[str], bytes] | None = None,
                 as_of: str = "") -> None:
        self.tou = tou
        self.path = Path(decisions_path) if decisions_path else None
        self.robots = robots
        self._robots_body = robots_body
        self.as_of = as_of
        self._memo: dict[str, tuple[bool, str]] = {}
        self._bodies: dict[str, bytes] = {}
        self._by_url: dict[str, dict[str, Any]] = {}
        self._by_host: dict[str, list[dict[str, Any]]] = {}
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("url") and rec.get("host"):
                    self._remember(rec)

    def _remember(self, rec: dict[str, Any]) -> None:
        self._by_url[rec["url"]] = rec
        self._by_host.setdefault(rec["host"], []).append(rec)

    def __call__(self, url: str) -> tuple[bool, str]:
        if url not in self._memo:
            self._memo[url] = self._decide(url)
        return self._memo[url]

    def _decide(self, url: str) -> tuple[bool, str]:
        host = _host(url)
        if not host or _hard_manual(host) or not self.tou.entry_for(url).ai_processing_allowed:
            return False, "host_ai_disallowed"
        if self.robots is None:
            return self._replayed(url, host)
        ok, sha = self.robots.allowed(url, GOOGLE_EXTENDED)
        if not ok:
            allowed, reason = False, "robots_google_extended"
        else:
            signals = parse_content_signals(self._body(url))
            if "no" in (signals.get("ai-train"), signals.get("ai-input")):
                allowed, reason = False, "content_signal"
            else:
                allowed, reason = True, ""
        self._record(host, url, allowed, reason, sha or "")
        return allowed, reason

    def _replayed(self, url: str, host: str) -> tuple[bool, str]:
        """The decision as it stood on ``as_of``: only records decided on or before it count, and each URL's latest
        such record stands (a later robots.txt or Content-Signal change never rewrites an earlier run's replay).
        Without ``as_of`` every record counts."""
        latest: dict[str, dict[str, Any]] = {}
        for rec in sorted(self._by_host.get(host, []), key=lambda r: str(r.get("decided_on") or "")):
            if not self.as_of or str(rec.get("decided_on") or "") <= self.as_of:
                latest[rec["url"]] = rec  # stable sort: on one day the later line wins
        rec = latest.get(url)
        if rec is not None:
            return (True, "") if rec.get("allowed") else (False, _host_reason_code(rec.get("reason")))
        refusals = [r for r in latest.values() if not r.get("allowed")]
        if refusals:
            return False, _host_reason_code(refusals[-1].get("reason"))
        return True, ""

    def _body(self, url: str) -> bytes:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        if origin not in self._bodies:
            body = b""
            try:
                if self._robots_body is not None:
                    body = self._robots_body(url) or b""
                elif callable(getattr(self.robots, "body", None)):
                    body = self.robots.body(url) or b""
                elif callable(getattr(self.robots, "_fetch_raw", None)):
                    status, raw = self.robots._fetch_raw(origin + "/robots.txt")
                    body = raw if 200 <= status < 300 else b""
            except Exception:  # noqa: BLE001 - an unreadable body means no signal; Google-Extended already passed
                body = b""
            self._bodies[origin] = body if isinstance(body, bytes) else str(body).encode("utf-8")
        return self._bodies[origin]

    def _record(self, host: str, url: str, allowed: bool, reason: str, sha: str) -> None:
        last = self._by_url.get(url)
        if last and (last.get("allowed"), last.get("reason"), last.get("robots_sha256")) == (allowed, reason, sha):
            return
        rec = {"host": host, "url": url, "allowed": allowed, "reason": reason, "robots_sha256": sha,
               "decided_on": self.as_of or _today()}
        self._remember(rec)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")


def _host_reason_code(reason: Any) -> str:
    return reason if reason in HOST_REASONS else "host_ai_disallowed"


# =========================================================================== LLM back ends


class LLM(Protocol):
    name: str

    def generate(self, *, role: LLMRole, prompt: str, payload: PublicPayload,
                 schema: dict[str, Any]) -> LLMReply: ...


class NullLLM:
    """Keyless back end: never sends anything; the rules and local fallbacks do the work."""

    name = "null"

    def __init__(self, note: str = "") -> None:
        self.note = note
        self.switches: list[dict[str, Any]] = []

    def generate(self, *, role: LLMRole, prompt: str, payload: PublicPayload,
                 schema: dict[str, Any]) -> LLMReply:
        return LLMReply(data=None, status="null")


def backoff_delay(attempt: int, hint: float | None = None) -> float:
    """Seconds to wait after failed attempt ``attempt`` (1-based): 2, 4, 8, ... capped at 60; a server RetryInfo
    delay is honoured within the cap."""
    base = BACKOFF_MIN_S * 2 ** (attempt - 1)
    return float(min(BACKOFF_MAX_S, max(base, hint or 0.0)))


def _walk(obj: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def classify_error(exc: BaseException) -> tuple[Literal["retry", "switch", "stop", "error"], str, float | None]:
    """(action, reason, retry-delay hint) for an exception raised by generate_content.

    retry: 408, 429 (per-minute), 5xx and network errors. switch (to the next model): a daily-quota 429, 403, 404.
    stop (no model can answer): an invalid API key (401, or 400 API_KEY_INVALID). error: anything else.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 100 <= code <= 599:
        details = getattr(exc, "details", None)
        quota_ids = [str(v) for k, v in _walk(details) if k in ("quotaId", "quota_id") and isinstance(v, str)]
        if code == 429:
            text = " ".join(quota_ids + [str(getattr(exc, "message", "") or "")]).lower()
            if "perday" in text or "per day" in text or "daily" in text:
                return "switch", "daily_quota", None
            hint = None
            for k, v in _walk(details):
                if k in ("retryDelay", "retry_delay") and isinstance(v, str):
                    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", v)
                    if m:
                        hint = float(m.group(1))
            return "retry", "rate_limited", hint
        if code == 401 or (code == 400 and any(
                k == "reason" and v == "API_KEY_INVALID" for k, v in _walk(details))):
            return "stop", "api_key_invalid", None   # the key is per project: the fallback model would fail too
        if code == 403:
            return "switch", "permission_denied", None
        if code == 404:
            return "switch", "model_not_found", None
        if code == 408 or 500 <= code <= 599:
            return "retry", f"http_{code}", None
        return "error", f"http_{code}", None
    if isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__module__.split(".")[0] in ("httpx", "httpcore"):
        return "retry", "network", None
    return "error", type(exc).__name__, None


def _parse_reply(response: Any, schema: Mapping[str, Any]) -> dict | None:
    """The reply's JSON object when it parses and has the schema's top-level shape, else None."""
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if any(key not in data for key in schema.get("required", [])):
        return None
    types = {"array": list, "object": dict, "string": str}
    for key, spec in (schema.get("properties") or {}).items():
        expected = types.get(spec.get("type")) if isinstance(spec, Mapping) else None
        if key in data and expected is not None and not isinstance(data[key], expected):
            return None
    return data


class GeminiLLM:
    """Live Gemini back end: google-genai ``models.generate_content`` only (no Files API, caching, tools or
    grounding), structured JSON output, seed 1234, minimal thinking, automatic function calling off, no temperature.

    Calls are paced ``min_interval_s`` apart. HTTP 408, 429 and 5xx (and network errors) are retried, up to
    ``max_attempts`` with 2-60 s backoff. A daily-quota 429, a 403 or a 404 (model not found) switches to the next
    model of the chain, and an invalid API key skips the rest of the chain; after the last model every call returns
    status "quota" for the rest of the run (callers fall back to the rules; ``note`` carries the "Gemini unavailable"
    banner). Each switch is appended to ``switches`` for the run manifest. The API key is read once and kept only
    inside the client.
    """

    name = "gemini"

    def __init__(self, api_key: str | None = None, *, models: Sequence[str] = MODEL_CHAIN, client: Any = None,
                 min_interval_s: float = MIN_INTERVAL_S, max_attempts: int = MAX_ATTEMPTS,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        if not models:
            raise ValueError("at least one model is needed")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if client is None:
            key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
            if not key:
                raise ValueError("GEMINI_API_KEY is not set")
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))
        self._client = client
        self.models: tuple[str, ...] = tuple(models)
        self.min_interval_s = float(min_interval_s)
        self.max_attempts = int(max_attempts)
        self._sleep = sleep
        self._clock = clock
        self._index = 0
        self._last_start: float | None = None
        self.api_calls = 0
        self.switches: list[dict[str, Any]] = []
        self.exhausted = False

    def __repr__(self) -> str:
        return f"GeminiLLM(models={self.models!r}, exhausted={self.exhausted})"

    @property
    def note(self) -> str:
        return GEMINI_UNAVAILABLE_NOTE if self.exhausted else ""

    @staticmethod
    def _config(prompt: str, schema: dict[str, Any]) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=prompt,
            response_mime_type="application/json",
            response_json_schema=schema,
            seed=SEED,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def _pace(self) -> None:
        now = self._clock()
        if self._last_start is not None:
            wait = self.min_interval_s - (now - self._last_start)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_start = now
        self.api_calls += 1

    def _call(self, model: str, contents: str, config: Any, schema: dict[str, Any]) -> tuple[str, Any, int]:
        bad_output_retries = BAD_OUTPUT_RETRIES
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            try:
                response = self._client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as exc:  # noqa: BLE001 - classified; the message is never logged (it may echo input)
                action, reason, hint = classify_error(exc)
                if action in ("switch", "stop"):
                    return action, reason, attempt
                if action == "retry" and attempt < self.max_attempts:
                    self._sleep(backoff_delay(attempt, hint))
                    continue
                return "error", reason, attempt
            data = _parse_reply(response, schema)
            if data is not None:
                return "ok", data, attempt
            if bad_output_retries > 0 and attempt < self.max_attempts:
                bad_output_retries -= 1
                continue
            return "error", "bad_output", attempt
        return "error", "attempts", self.max_attempts  # pragma: no cover - the loop always returns

    def generate(self, *, role: LLMRole, prompt: str, payload: PublicPayload,
                 schema: dict[str, Any]) -> LLMReply:
        _check_released(payload)
        if self.exhausted:
            return LLMReply(data=None, status="quota")
        contents = render_payload(role, payload)
        config = self._config(prompt, schema)
        attempts = 0
        while self._index < len(self.models):
            model = self.models[self._index]
            outcome, value, used = self._call(model, contents, config, schema)
            attempts += used
            if outcome == "ok":
                return LLMReply(data=value, model=model, status="ok", attempts=attempts)
            if outcome in ("switch", "stop"):
                self._index = self._index + 1 if outcome == "switch" else len(self.models)
                target = self.models[self._index] if self._index < len(self.models) else "null"
                self.switches.append({"from": model, "to": target, "reason": value, "role": role})
                continue
            return LLMReply(data=None, model=model, status="error", attempts=attempts)
        self.exhausted = True
        return LLMReply(data=None, status="quota", attempts=attempts)


class CachedLLM:
    """Deterministic response cache in front of an optional inner LLM (``inner=None`` is replay: cache only).

    Entries live at ``cache_dir/<key[:2]>/<key>.json`` with key, model, role, prompt_sha256, schema_sha256,
    payload_sha256, seed and response (sorted keys, UTF-8, LF, written atomically). A lookup tries every model of
    the chain in order, so a fallback model's answer replays too. Only ok replies are stored, and never one that
    contains "Meridian".
    """

    def __init__(self, inner: LLM | None, cache_dir: str | Path = CACHE_DIR, *, models: Sequence[str] = MODEL_CHAIN,
                 seed: int = SEED) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.models: tuple[str, ...] = tuple(models)
        self.seed = seed
        self.name = f"cached:{inner.name}" if inner is not None else "replay"
        self.hits = 0
        self.misses = 0

    @property
    def note(self) -> str:
        return str(getattr(self.inner, "note", "") or "")

    @property
    def switches(self) -> list[dict[str, Any]]:
        return list(getattr(self.inner, "switches", []) or [])

    def path_for(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _read(self, key: str) -> dict | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get("key") != key or not isinstance(entry.get("response"), dict):
            return None
        return entry

    def _write(self, key: str, entry: dict[str, Any]) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(entry, sort_keys=True, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def generate(self, *, role: LLMRole, prompt: str, payload: PublicPayload,
                 schema: dict[str, Any]) -> LLMReply:
        psha = _sha(prompt.replace("\r\n", "\n"))
        ssha = schema_sha256(schema)
        paysha = payload.sha256()
        for model in self.models:
            key = cache_key(model, psha, ssha, paysha, self.seed)
            entry = self._read(key)
            if entry is not None:
                self.hits += 1
                return LLMReply(data=entry["response"], model=model, status="cache_hit", cache_key=key)
        self.misses += 1
        if self.inner is None:
            return LLMReply(data=None, status="cache_miss",
                            cache_key=cache_key(self.models[0], psha, ssha, paysha, self.seed))
        reply = self.inner.generate(role=role, prompt=prompt, payload=payload, schema=schema)
        if reply.status != "ok" or not isinstance(reply.data, dict) or not reply.model:
            return reply
        if _has_meridian(json.dumps(reply.data, ensure_ascii=False)):
            return LLMReply(data=None, model=reply.model, status="error", attempts=reply.attempts)
        key = cache_key(reply.model, psha, ssha, paysha, self.seed)
        self._write(key, {"key": key, "model": reply.model, "role": role, "prompt_sha256": psha,
                          "schema_sha256": ssha, "payload_sha256": paysha, "seed": self.seed,
                          "response": reply.data})
        return reply.model_copy(update={"cache_key": key})


def make_llm(mode: AssessmentMode, *, cache_dir: str | Path = CACHE_DIR) -> LLM:
    """replay: CachedLLM(None); live_rules: NullLLM(); live_ai: CachedLLM(GeminiLLM()), or NullLLM with the
    "Gemini unavailable" note when GEMINI_API_KEY is unset (load .env first) or google-genai is missing."""
    if mode == "replay":
        return CachedLLM(None, cache_dir)
    if mode == "live_rules":
        return NullLLM()
    if mode == "live_ai":
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            return NullLLM(note=GEMINI_UNAVAILABLE_NOTE)
        try:
            return CachedLLM(GeminiLLM(), cache_dir)
        except ImportError:
            return NullLLM(note=GEMINI_UNAVAILABLE_NOTE)
    raise ValueError(f"unknown mode {mode!r}")


def _is_null(llm: LLM) -> bool:
    return isinstance(llm, NullLLM) or getattr(llm, "name", "") == "null"


# =========================================================================== audit log


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLog:
    """``runs/<run_id>/llm_calls.jsonl``: one line per call or cache lookup and one per withheld item, never any
    text (no passage, quote, prompt, API key or profile value). ``path=None`` keeps the records in memory only."""

    def __init__(self, path: str | Path | None, *, now: Callable[[], str] | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.records: list[dict[str, Any]] = []
        self._now = now or _utc_now

    def _write(self, rec: dict[str, Any]) -> None:
        self.records.append(rec)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")

    def record(self, *, vendor_id: str, role: LLMRole, reply: LLMReply, prompt_sha256: str, schema_sha256: str,
               payload_sha256: str, items_sent: Sequence[str], withheld: Sequence[Withheld], n_out: int,
               hosts: Iterable[str] = (), n_dropped: int = 0, n_skipped: int = 0) -> None:
        """One call or cache lookup. ``hosts`` are the hosts of the items sent (checked by ``check_audit``)."""
        self._write({
            "ts": self._now(), "vendor_id": vendor_id, "role": role, "model": reply.model, "status": reply.status,
            "cache_key": reply.cache_key, "prompt_sha256": prompt_sha256, "schema_sha256": schema_sha256,
            "payload_sha256": payload_sha256, "items_sent": list(items_sent),
            "withheld": [{"id": w.id, "reason": w.reason} for w in withheld], "n_out": int(n_out),
            "attempts": reply.attempts, "hosts": sorted({h for h in hosts if h}), "n_dropped": int(n_dropped),
            "n_skipped": int(n_skipped),
        })

    def record_withheld(self, *, vendor_id: str, role: LLMRole, withheld: Sequence[Withheld]) -> None:
        """One line per withheld item, with its reason code and no text."""
        for w in withheld:
            self._write({
                "ts": self._now(), "vendor_id": vendor_id, "role": role, "model": "", "status": "withheld",
                "cache_key": "", "prompt_sha256": "", "schema_sha256": "", "payload_sha256": "", "items_sent": [],
                "withheld": [{"id": w.id, "reason": w.reason}], "n_out": 0, "attempts": 0, "hosts": [],
                "n_dropped": 0, "n_skipped": 0,
            })

    def model_for(self, item_id: str) -> str:
        """Model of the latest successful call (ok or cache hit) that sent ``item_id``; "" if none."""
        for rec in reversed(self.records):
            if rec.get("status") in ("ok", "cache_hit") and item_id in rec.get("items_sent", ()):
                return str(rec.get("model", ""))
        return ""

    def stats(self) -> dict[str, dict[str, Any]]:
        """Per vendor: calls (lookups), api_calls (fresh answers), cache_hits, errors, quota, budget, withheld,
        sent (distinct items sent) and rate = withheld / (withheld + sent), for the run manifest."""
        out: dict[str, dict[str, Any]] = {}
        sent: dict[str, set[str]] = {}
        for rec in self.records:
            vid = str(rec.get("vendor_id", ""))
            s = out.setdefault(vid, {"calls": 0, "api_calls": 0, "cache_hits": 0, "errors": 0, "quota": 0,
                                     "budget": 0, "withheld": 0, "sent": 0, "rate": 0.0})
            status = rec.get("status")
            if status == "withheld":
                s["withheld"] += len(rec.get("withheld", []))
                continue
            if status == "budget":
                s["budget"] += 1
                continue
            s["calls"] += 1
            s["api_calls"] += status == "ok"
            s["cache_hits"] += status == "cache_hit"
            s["errors"] += status == "error"
            s["quota"] += status == "quota"
            sent.setdefault(vid, set()).update(rec.get("items_sent", []))
        for vid, s in out.items():
            s["sent"] = len(sent.get(vid, set()))
            total = s["withheld"] + s["sent"]
            s["rate"] = round(s["withheld"] / total, 4) if total else 0.0
        return out


_API_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")


def _strings(obj: Any, key: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(obj, str):
        yield key, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, key)


def _audit_secrets() -> list[bytes]:
    """The configured secret values (environment and .env, of the working directory and of the repository), held in
    memory only and never printed: footprint.bundle.secret_values."""
    from footprint.bundle import secret_values

    roots = {Path(".").resolve(), Path(__file__).resolve().parents[2]}
    return sorted({value for root in roots for value in secret_values(root)})


def check_audit(path: str | Path, *, tou: TouRegister | None = None, team_name: str = "",
                secrets: Sequence[bytes] | None = None) -> list[str]:
    """Release gate over an llm_calls.jsonl file: the problems found (an empty list means clean).

    It flags lines that are not JSON objects, unexpected (text-like) fields, string values with whitespace,
    "Meridian", the team name, an API key, an unknown status, a withholding without a valid reason, items sent
    without their hosts, and a sent item whose host the ToS register refuses. A missing file is clean.

    An API key is any token footprint.bundle.SECRET_PATTERN matches (classic "AIza" and "AQ." Google keys, GitHub
    tokens) and any of ``secrets``: by default the configured secret values (GEMINI_API_KEY and the other key-,
    token-, secret- or password-named entries of the environment and .env; footprint.bundle.secret_values). The
    values stay in memory and a problem line never quotes them.
    """
    from footprint.bundle import holds_secret

    p = Path(path)
    if not p.exists():
        return []
    tou = tou or load_tou()
    team = _team_pattern(team_name) if team_name else None
    values = list(_audit_secrets() if secrets is None else secrets)
    problems: list[str] = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        if _has_meridian(line.replace('"hard_block_meridian"', '""')):    # the reason code is not a leak
            problems.append(f"line {n}: contains the word Meridian")
        if team is not None and team.search(_fold(line)):
            problems.append(f"line {n}: contains the team name")
        if _API_KEY.search(line) or holds_secret(line.encode("utf-8"), values):
            problems.append(f"line {n}: contains an API key")
        try:
            rec = json.loads(line)
        except ValueError:
            problems.append(f"line {n}: not valid JSON")
            continue
        if not isinstance(rec, dict):
            problems.append(f"line {n}: not a JSON object")
            continue
        for key in sorted(set(rec) - AUDIT_FIELDS):
            problems.append(f"line {n}: unexpected field {key!r} (text-like)")
        for key, value in _strings(rec):
            if any(ch.isspace() for ch in value):
                problems.append(f"line {n}: text-like value in {key!r}")
                break
        status = rec.get("status")
        if status not in AUDIT_STATUSES:
            problems.append(f"line {n}: unknown status {status!r}")
        withheld = rec.get("withheld", [])
        if not isinstance(withheld, list) or (status == "withheld" and not withheld):
            problems.append(f"line {n}: withholding without a valid reason")
            withheld = []
        for w in withheld:
            if (not isinstance(w, dict) or set(w) - {"id", "reason"} or not w.get("id")
                    or w.get("reason") not in WITHHELD_REASONS):
                problems.append(f"line {n}: withholding without a valid reason")
                break
        sent = rec.get("items_sent") or []
        hosts = rec.get("hosts") or []
        if sent and not hosts:
            problems.append(f"line {n}: items sent without their hosts")
        for host in hosts if isinstance(hosts, list) else []:
            if not isinstance(host, str) or _hard_manual(host.lower()) or \
                    not tou.entry_for(host.lower()).ai_processing_allowed:
                problems.append(f"line {n}: sent an item from {host}, which the ToS register refuses")
    return problems


# =========================================================================== shared role helpers

_URL_LIKE = re.compile(r"(?i)://|\bwww\.|@|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")


def _url_like(text: str) -> bool:
    return bool(_URL_LIKE.search(text))


def _payload(company: str, batch: Sequence[PayloadItem], prefix: str) -> tuple[PublicPayload, dict[str, str]]:
    """Relabel released items with short per-call ids (P1, R1, ...) and keep the map back to the real ids."""
    local = {f"{prefix}{k}": item.id for k, item in enumerate(batch, 1)}
    items = tuple(PayloadItem(id=short, kind=item.kind, text=item.text) for short, item in zip(local, batch))
    return PublicPayload(company=company, items=items), local


def _batch_bounds(lengths: Sequence[int], size: int, max_chars: int | None = None) -> list[tuple[int, int]]:
    """[start, end) index ranges of consecutive batches: at most ``size`` items and ``max_chars`` characters each
    (an item longer than ``max_chars`` goes alone)."""
    bounds: list[tuple[int, int]] = []
    start, chars = 0, 0
    for k, n in enumerate(lengths):
        if k > start and (k - start >= size or (max_chars is not None and chars + n > max_chars)):
            bounds.append((start, k))
            start, chars = k, 0
        chars += n
    if start < len(lengths):
        bounds.append((start, len(lengths)))
    return bounds


def _batches(items: Sequence[PayloadItem], size: int, max_chars: int | None = None) -> Iterator[list[PayloadItem]]:
    for start, end in _batch_bounds([len(item.text) for item in items], size, max_chars):
        yield list(items[start:end])


def extract_batch_size(lengths: Sequence[int], max_calls: int | None, *,
                       max_chars: int | None = MAX_BATCH_CHARS) -> int:
    """Passages per extract call (design 2.6): ``BATCH_SIZE`` (12), or ``BATCH_SIZE_TIGHT`` (25) when the quota gate
    requires it, that is when 12 per call would need more calls than the remaining budget ``max_calls`` and 25 per
    call needs fewer. ``lengths`` are the character lengths of the items to send, in order."""
    if max_calls is not None and max_calls < 0:
        raise ValueError("max_calls must not be negative")
    if max_calls is None:
        return BATCH_SIZE
    normal = len(_batch_bounds(lengths, BATCH_SIZE, max_chars))
    tight = len(_batch_bounds(lengths, BATCH_SIZE_TIGHT, max_chars))
    return BATCH_SIZE_TIGHT if normal > max_calls and tight < normal else BATCH_SIZE


def _local_id(raw: str, local: Mapping[str, str]) -> str:
    """Map a returned id back to the real id: short ids (P3, [P3]) and real ids of the call both resolve."""
    key = raw.strip().strip("[]").strip()
    if key in local:
        return local[key]
    if key in local.values():
        return key
    return raw


# =========================================================================== role: extract


def _passage_kind(document: Document | None) -> str:
    if document is None:
        return "unknown"
    family = getattr(document.family, "value", document.family)
    kind = f"{family} {document.kind}"
    return f"{kind}, {document.published}" if document.published else kind


def _parse_claims(data: Any, local: Mapping[str, str]) -> tuple[list[Claim], int]:
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return [], 0
    out: list[Claim] = []
    dropped = 0
    for raw in data["claims"]:
        try:
            claim = Claim.model_validate(raw)
        except ValidationError:
            dropped += 1
            continue
        out.append(claim.model_copy(update={
            "passage_id": _local_id(claim.passage_id, local),
            "named_providers": [p for p in claim.named_providers if p.strip() and not _url_like(p)],
            "data_mentioned": [d for d in claim.data_mentioned if d.strip() and not _url_like(d)],
        }))
    return out, dropped


def extract_claims(llm: LLM, passages: Sequence[Passage], *, company: str, guard: PayloadGuard,
                   docs: Mapping[str, Document], audit: AuditLog | None = None, vendor_id: str,
                   batch_size: int | None = BATCH_SIZE, max_calls: int | None = None,
                   max_batch_chars: int | None = MAX_BATCH_CHARS) -> list[Claim]:
    """Gemini claim extraction (design 2.6 role "extract"), unverified.

    Passages are sorted by (doc_id, start), checked by the guard and sent in batches of ``batch_size`` (and at most
    ``max_batch_chars`` characters), each rendered ``[P#] ({family} {doc kind}, {date}) {text}``.
    ``batch_size=None`` picks 12 or 25 by ``extract_batch_size`` over the released items. ``max_calls`` is the
    vendor's remaining Gemini budget: batches beyond it are not sent (audit status "budget") and stay with the
    rules. Returns every schema-valid claim in batch order, with ``passage_id`` mapped back to the real passage id;
    quotes keep their placeholders (``guard.placeholders`` / ``guard.restore`` serve verify.V2), and unknown ids are
    kept so that V1 rejects and logs them. URL-like provider names are dropped. NullLLM returns [] and sends nothing.
    """
    _check_company(company, guard)
    if batch_size is not None and batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if _is_null(llm):
        return []
    unique: dict[str, Passage] = {}
    for p in passages:
        unique.setdefault(p.passage_id, p)
    ordered = sorted(unique.values(), key=lambda p: (p.doc_id, p.start, p.end, p.passage_id))
    urls: dict[str, str] = {}
    inputs: list[GuardInput] = []
    for p in ordered:
        document = docs.get(p.doc_id)
        urls[p.passage_id] = document.url if document is not None else ""
        inputs.append(GuardInput(id=p.passage_id, kind=_passage_kind(document), text=p.text,
                                 url=urls[p.passage_id]))
    sendable, withheld = guard.check(inputs)
    if audit is not None and withheld:
        audit.record_withheld(vendor_id=vendor_id, role="extract", withheld=withheld)
    prompt = prompt_text("extract_v1")
    psha, ssha = prompt_sha256("extract_v1"), schema_sha256(EXTRACT_SCHEMA)
    if batch_size is None:
        batch_size = extract_batch_size([len(i.text) for i in sendable], max_calls, max_chars=max_batch_chars)
    claims: list[Claim] = []
    calls = 0
    for batch in _batches(sendable, batch_size, max_batch_chars):
        ids = [item.id for item in batch]
        if max_calls is not None and calls >= max_calls:
            if audit is not None:
                audit.record(vendor_id=vendor_id, role="extract", reply=LLMReply(data=None, status="budget"),
                             prompt_sha256=psha, schema_sha256=ssha, payload_sha256="", items_sent=[], withheld=[],
                             n_out=0, n_skipped=len(batch))
            continue
        payload, local = _payload(company, batch, "P")
        reply = llm.generate(role="extract", prompt=prompt, payload=payload, schema=EXTRACT_SCHEMA)
        calls += 1
        got, dropped = _parse_claims(reply.data, local)
        claims += got
        if audit is not None:
            audit.record(vendor_id=vendor_id, role="extract", reply=reply, prompt_sha256=psha, schema_sha256=ssha,
                         payload_sha256=payload.sha256(), items_sent=ids, withheld=[], n_out=len(got),
                         hosts=[_host(urls[i]) for i in ids], n_dropped=dropped)
    return claims


# =========================================================================== role: triage

_TRIAGE_GROUPS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (3, re.compile(r"\b(?:ai|artificial intelligence|machine learning|ml|genai|gen ai|generative|llms?|gpt|"
                   r"copilots?|agentic|chatbots?|virtual assistants?|neural|nlp|deep learning|predictive|"
                   r"intelligent automation)\b")),
    (2, re.compile(r"\b(?:sub ?processors?|privacy|dpa|data processing|data protection|trust|responsible|ethics|"
                   r"governance|security|terms|legal|compliance)\b")),
    (1, re.compile(r"\b(?:10 ?k|10 ?q|8 ?k|annual report|proxy|def ?14a|press|news|newsroom|releases?|blog|"
                   r"insights?|careers?|jobs?|engineers?|scientists?|innovation|technology|platforms?|products?|"
                   r"solutions?)\b")),
)


def _triage_score(row: TriageRow) -> int:
    text = re.sub(r"[/_\-.|:]+", " ", _fold(f"{row.path} {row.title}"))
    return sum(weight for weight, rx in _TRIAGE_GROUPS if rx.search(text))


def keyword_triage(rows: Sequence[TriageRow]) -> list[str]:
    """Keyless triage: ids of rows whose slug and title score above zero, best first, ties in input order."""
    scored = [(-_triage_score(row), k, row.id) for k, row in enumerate(rows)]
    seen: set[str] = set()
    out: list[str] = []
    for score, _, rid in sorted(scored):
        if score < 0 and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def _row_text(row: TriageRow) -> str:
    return f"{row.path} | {row.title} | {row.date} | {row.family}"


def triage(llm: LLM, rows: Sequence[TriageRow], *, company: str, guard: PayloadGuard, audit: AuditLog | None = None,
           vendor_id: str, batch_size: int = TRIAGE_BATCH_SIZE, max_calls: int | None = None,
           fallback: bool = False) -> list[str]:
    """Gemini triage (design 2.6 role "triage"): ids only, never URLs.

    Rows go through the guard (``row.url`` drives the host policy and is never sent) and are rendered
    ``R# | path | title | date | family``. Returns only ids that were sent: high priority first, then medium, each
    in input order. With NullLLM it returns ``keyword_triage(rows)``. With ``fallback=True``, rows that were withheld
    or whose call failed are ranked by ``keyword_triage`` and appended after Gemini's picks.
    """
    _check_company(company, guard)
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    unique: dict[str, TriageRow] = {}
    for row in rows:
        unique.setdefault(row.id, row)
    rows = list(unique.values())
    if _is_null(llm):
        return keyword_triage(rows)
    sendable, withheld = guard.check([GuardInput(id=r.id, kind="row", text=_row_text(r), url=r.url) for r in rows])
    if audit is not None and withheld:
        audit.record_withheld(vendor_id=vendor_id, role="triage", withheld=withheld)
    url_of = {r.id: r.url for r in rows}
    prompt = prompt_text("triage_v1")
    psha, ssha = prompt_sha256("triage_v1"), schema_sha256(TRIAGE_SCHEMA)
    priority: dict[str, str] = {}
    answered: set[str] = set()
    calls = 0
    for batch in _batches(sendable, batch_size):
        ids = [item.id for item in batch]
        if max_calls is not None and calls >= max_calls:
            if audit is not None:
                audit.record(vendor_id=vendor_id, role="triage", reply=LLMReply(data=None, status="budget"),
                             prompt_sha256=psha, schema_sha256=ssha, payload_sha256="", items_sent=[], withheld=[],
                             n_out=0, n_skipped=len(batch))
            continue
        payload, local = _payload(company, batch, "R")
        reply = llm.generate(role="triage", prompt=prompt, payload=payload, schema=TRIAGE_SCHEMA)
        calls += 1
        picks, dropped = _parse_triage(reply.data, local)
        if reply.data is not None:
            answered.update(ids)
        for rid, level in picks.items():
            if priority.get(rid) != "high":
                priority[rid] = level
        if audit is not None:
            audit.record(vendor_id=vendor_id, role="triage", reply=reply, prompt_sha256=psha, schema_sha256=ssha,
                         payload_sha256=payload.sha256(), items_sent=ids, withheld=[], n_out=len(picks),
                         hosts=[_host(url_of[i]) for i in ids], n_dropped=dropped)
    order = [r.id for r in rows]
    out = [rid for rid in order if priority.get(rid) == "high"] + \
          [rid for rid in order if priority.get(rid) == "medium"]
    if fallback:
        rest = [r for r in rows if r.id not in answered]
        out += [rid for rid in keyword_triage(rest) if rid not in out]
    return out


def _parse_triage(data: Any, local: Mapping[str, str]) -> tuple[dict[str, str], int]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {}, 0
    picks: dict[str, str] = {}
    dropped = 0
    for raw in data["items"]:
        try:
            pick = TriagePick.model_validate(raw)
        except ValidationError:
            dropped += 1
            continue
        key = pick.id.strip().strip("[]").strip()
        rid = local.get(key)
        if rid is None:
            dropped += 1                    # an id that was not sent is never returned
            continue
        if picks.get(rid) != "high":
            picks[rid] = pick.priority
    return picks, dropped


# =========================================================================== role: expand

_CAP_NGRAM = re.compile(r"\b[A-Z][\w&+-]*(?:[ \t]+(?:[A-Z][\w&+-]*|\d+(?:\.\d+)*))*")
_NAME_STOP = frozenset(
    "the a an our we your you with for and of in on to by at from this that these those its their it is are "
    "learn more read discover explore new how why what get see contact home about meet introducing powered "
    "welcome all".split()
)
_AI_WORDS = frozenset(
    "ai ml genai gen llm llms artificial intelligence machine learning generative deep agentic agent agents nlp "
    "rag mcp model models neural chatbot chatbots assistant assistants".split()
)
_SENTENCE_START = re.compile(r"(?:^|[.!?:;|]\s+|\n\s*)$")


@lru_cache(maxsize=1)
def _core_lexicon() -> Any:
    from footprint.extract import load_core_lexicon

    return load_core_lexicon()


def _ai_spans(text: str) -> list[tuple[int, int]]:
    """Offsets of core AI terms (config/lexicon.toml) after blanking suppressor phrases (same length)."""
    lex = _core_lexicon()
    masked = text
    for rx in lex.suppressors:
        masked = rx.sub(lambda m: " " * len(m.group(0)), masked)
    return sorted({(m.start(), m.end()) for rx in lex.core.values() for m in rx.finditer(masked)})


def local_names(texts: Iterable[str], *, window: int = 80) -> list[str]:
    """Keyless name expansion (design 2.6): capitalised n-grams within ``window`` characters of an AI term, in order
    of appearance. Pure AI words, stop words and sentence-initial single words are left out."""
    out: list[str] = []
    for text in texts:
        spans = _ai_spans(text)
        if not spans:
            continue
        for m in _CAP_NGRAM.finditer(text):
            if not any(m.start() < e + window and m.end() > s - window for s, e in spans):
                continue
            words = m.group(0).split()
            while words and words[0].lower() in _NAME_STOP:
                words.pop(0)
            while words and words[-1].lower() in _NAME_STOP:
                words.pop()
            words = words[:4]
            if not words or all(w.lower().strip("-&+") in _AI_WORDS for w in words):
                continue
            name = " ".join(words)
            if len(words) == 1 and not re.search(r"[A-Z0-9]", name[1:]):
                start = m.start() + m.group(0).index(words[0])
                if _SENTENCE_START.search(text[:start]):
                    continue                # "Discover", "Powered": a capital that only starts a sentence
            out.append(name)
    return _clean_names(out)


def _clean_names(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        name = " ".join(str(raw).split()).strip(" .,;:!?\"'()[]{}")
        if not name or len(name) > MAX_NAME_CHARS or _url_like(name) or _has_meridian(name):
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= MAX_NAMES:
            break
    return out


def _parse_names(data: Any) -> tuple[list[str], int]:
    if not isinstance(data, dict) or not isinstance(data.get("names"), list):
        return [], 0
    names: list[str] = []
    dropped = 0
    for raw in data["names"]:
        try:
            names.append(ExpandedName.model_validate(raw).name)
        except ValidationError:
            dropped += 1
    return names, dropped


def expand_names(llm: LLM, company: str, texts: Sequence[str], *, guard: PayloadGuard, audit: AuditLog | None = None,
                 vendor_id: str, url: str = "", aliases: Sequence[str] = (), max_calls: int | None = None) -> list[str]:
    """Gemini name expansion (design 2.6 role "expand"): names only, used as search keys and never as evidence.

    ``texts`` are public homepage texts (title, meta description, navigation) from ``url``; ``aliases`` are the
    seed aliases. Returns the aliases, then Gemini's names, then local n-grams of texts that were withheld or not
    answered. Anything URL-like (``://``, ``www.``, a dotted host, ``@``) is dropped. With NullLLM it returns the
    aliases plus capitalised n-grams next to AI terms.
    """
    _check_company(company, guard)
    inputs = [GuardInput(id=f"T{k}", kind="homepage text", text=t, url=url)
              for k, t in enumerate((t for t in texts if t and t.strip()), 1)]
    names: list[str] = list(aliases)
    if _is_null(llm) or not inputs or max_calls == 0:
        return _clean_names(names + local_names(i.text for i in inputs))
    sendable, withheld = guard.check(inputs)
    if audit is not None and withheld:
        audit.record_withheld(vendor_id=vendor_id, role="expand", withheld=withheld)
    by_id = {i.id: i.text for i in inputs}
    local_from = [by_id[w.id] for w in withheld]
    if sendable:
        payload = PublicPayload(company=company, items=tuple(sendable))
        reply = llm.generate(role="expand", prompt=prompt_text("expand_v1"), payload=payload, schema=EXPAND_SCHEMA)
        got, dropped = _parse_names(reply.data)
        if reply.data is None:
            local_from += [by_id[i.id] for i in sendable]
        names += got
        if audit is not None:
            audit.record(vendor_id=vendor_id, role="expand", reply=reply, prompt_sha256=prompt_sha256("expand_v1"),
                         schema_sha256=schema_sha256(EXPAND_SCHEMA), payload_sha256=payload.sha256(),
                         items_sent=[i.id for i in sendable], withheld=[], n_out=len(got),
                         hosts=[_host(url)] if _host(url) else [], n_dropped=dropped)
    return _clean_names(names + local_names(local_from))
