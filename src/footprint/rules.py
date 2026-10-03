"""Rules tagger (P3): source reliability, entity guard, the four genuine-vs-marketing tests, G/M indicators,
tags and strength labels for one passage at a time.

Contract: docs/contracts_p3.md section 3. Design: Appendix A 2.3 (sources, tags, strength labels, the four tests,
entity guard) and 2.6 (lexicon, suppressors, provider guards). Config: config/lexicon.toml (the tables after
[suppressors]) and config/sources.toml (the only thing that sets SR).

Everything here is deterministic and offline: no clock (``as_of`` is passed in), no network, no LLM. The tagger
never invents text: every excerpt is an exact slice of the document text and every indicator span is an exact
substring of its excerpt.

Interpretations of gaps in the design are marked *(interpretation)* in the docstrings and listed in the final
report of the P3 rules work.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import itertools
import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from footprint.collectors.base import host_of, seed_terms
from footprint.extract import split_sentences
from footprint.models import (
    QUALIFYING_LOCI,
    STRENGTH_ORDER,
    ActionLevel,
    AiType,
    Capture,
    Claim,
    DepthPlan,
    Document,
    EvidenceItem,
    Indicator,
    Locus,
    Passage,
    RecencyGrade,
    RelevanceGrade,
    SignalTags,
    SourceFamily,
    SourceReliability,
    SpecificityGrade,
    Strength,
    Temporal,
    UClass,
    VendorProfile,
    VerifyResult,
)
from footprint.review import OverrideStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEXICON = REPO_ROOT / "config" / "lexicon.toml"
DEFAULT_SOURCES = REPO_ROOT / "config" / "sources.toml"

MIN_EXCERPT = 25
MAX_EXCERPT = 600

RELATIONS: tuple[str, ...] = (
    "first_party", "ats", "filing", "dns", "affiliate_confirmed", "affiliate_inferred", "platform_supplier",
    "executive", "third_party",
)
FIRST_PARTY_RELATIONS: frozenset[str] = frozenset({"first_party", "ats", "filing", "dns", "affiliate_confirmed",
                                                   "executive"})
PRODUCT_SOURCE_TYPES: frozenset[str] = frozenset({"Product page", "Technical documentation", "Release notes",
                                                  "Fact sheet", "Homepage", "Partner page"})
REVIEW_LABEL_KEYS: tuple[str, ...] = ("temporal", "action_level", "sp", "rl", "locus", "u_class", "ai_type")
U_PRECEDENCE: tuple[str, ...] = ("U8", "U1", "U2", "U3", "U4", "U6", "U5", "U7")
"""Contract precedence guidance when a claim fits several classes (first wins)."""

_ARTICLE_DATE_BASES: frozenset[str] = frozenset({
    "json-ld datePublished", "meta article:published_time", "wp-json date", "ATS posted date", "pdf creation_date",
    "htmldate", "edgar filing date", "wayback first-seen",
})
_FUNCTION_WORDS: frozenset[str] = frozenset("""
a an the of to in on for with and or but nor is are was were be been being by as at from that this these those it
its we our us you your they their them how why what when which who whom has have had can could will would shall
should may might must not no into onto than then so if about more most all any each every do does did there here
such also only just very own both either neither while where whose per via over under across between within without
""".split())
_GENERIC_NAME_WORDS: frozenset[str] = frozenset("""
automation software technologies technology services service solutions solution platform platforms cloud systems
system group holdings inc llc corp corporation company labs digital data payments financial global international the
and of for networks network partners consulting
""".split())
_MODEL_WORDS_AFTER_NAME: frozenset[str] = frozenset("""
Code Opus Sonnet Haiku Instant AI Enterprise Pro Flash Ultra Nano Studio Assist Chat Agents Agent API Models Model
Labs Cloud Foundry Copilot Business Team Max Desktop
""".split())


# =========================================================================== compiled lexicons


_SEP = r"[\s\-‐‑‒–—]+"
_WORD_CHAR = re.compile(r"\w")


def _phrase_pattern(phrase: str, *, plural: bool = False) -> str:
    """Regex for a phrase: whole words; inside it a space also matches a hyphen, a dash or a line break."""
    m = re.match(r"^(\W*)(.*?)(\W*)$", phrase.strip(), re.S)
    assert m is not None
    lead, core, trail = m.groups()
    words = [w for w in re.split(r"[\s\-]+", core) if w]
    body = _SEP.join(re.escape(w).replace("'", "['’]").replace("\\'", "['’]") for w in words)
    if plural and core and core[-1].isalpha():
        body += "(?:s|es)?"
    pre = re.escape(lead) if lead else r"(?<!\w)"
    post = re.escape(trail) if trail else r"(?!\w)"
    return pre + body + post


def _word_pattern(word: str) -> str:
    pre = r"(?<!\w)" if _WORD_CHAR.match(word[:1] or " ") else ""
    post = r"(?!\w)" if _WORD_CHAR.match(word[-1:] or " ") else ""
    return pre + re.escape(word) + post


_TOKEN = re.compile(r"\w+")


@functools.lru_cache(maxsize=2048)
def _tokens(text: str) -> frozenset[str]:
    """Lower-cased word tokens of ``text`` (the prefilter of Lex; cached because many lexicons scan one text)."""
    return frozenset(t.lower() for t in _TOKEN.findall(text))


def _first_token(entry: str) -> str:
    toks = _TOKEN.findall(entry.lower())
    return toks[0] if toks else ""


@dataclass(frozen=True)
class Lex:
    """One compiled lexicon: finds non-overlapping matches with their exact spans.

    ``keys`` holds the first word token of every phrase and word (with the plural forms of one-word phrases); a text
    that contains none of them cannot match, so the regex is skipped. A lexicon with regex entries has no keys and
    always runs. The prefilter never changes a result, it only saves time."""

    rx: re.Pattern[str] | None = None
    keys: frozenset[str] | None = None

    def _may_match(self, text: str) -> bool:
        return self.keys is None or not self.keys.isdisjoint(_tokens(text))

    def find(self, text: str) -> list[tuple[int, int, str]]:
        if self.rx is None or not text or not self._may_match(text):
            return []
        return [(m.start(), m.end(), m.group(0)) for m in self.rx.finditer(text) if m.end() > m.start()]

    def search(self, text: str) -> re.Match[str] | None:
        if self.rx is None or not text or not self._may_match(text):
            return None
        return self.rx.search(text)

    def __contains__(self, text: str) -> bool:  # "text in lex"
        return self.search(text) is not None


def _compile(phrases: Iterable[str] = (), case_sensitive: Iterable[str] = (), regex: Iterable[str] = (), *,
             plural: bool = False) -> Lex:
    """phrases: case-insensitive; case_sensitive: exact words; regex: case-insensitive unless scoped flags say."""
    parts: list[str] = []
    keys: set[str] | None = set()
    for p in sorted({p for p in phrases if p.strip()}, key=lambda s: (-len(s), s)):
        parts.append(f"(?i:{_phrase_pattern(p, plural=plural)})")
        first = _first_token(p)
        if not first:
            keys = None
        elif keys is not None:
            keys.add(first)
            if plural and len(_TOKEN.findall(p)) == 1 and p.strip()[-1:].isalpha():
                keys.update({first + "s", first + "es"})
    for w in sorted({w for w in case_sensitive if w.strip()}, key=lambda s: (-len(s), s)):
        parts.append(f"(?:{_word_pattern(w)})")
        first = _first_token(w)
        if not first:
            keys = None
        elif keys is not None:
            keys.add(first)
    for r in regex:
        if r.strip():
            re.compile(r)  # fail loudly on a bad config regex
            parts.append(f"(?i:{r})")
            keys = None
    return Lex(re.compile("|".join(parts)) if parts else None, frozenset(keys) if keys is not None else None)


def _table_lex(table: Mapping[str, Any] | None, *, plural: bool = False) -> Lex:
    table = table or {}
    return _compile(table.get("phrases", ()), table.get("case_sensitive", ()), table.get("regex", ()), plural=plural)


@dataclass(frozen=True)
class ProviderRule:
    """A named AI provider entry: case-sensitive name patterns plus an optional guard on the context."""

    name: str
    rx: re.Pattern[str]
    guard: re.Pattern[str] | None = None
    single_word: bool = False


@dataclass(frozen=True)
class Signals:
    """Every lexicon the tagger uses, compiled once per lexicon file (``load_signals``)."""

    path: str
    version: str
    sha256: str
    ai_terms: Lex
    guarded_terms: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...]
    mcp_guard: re.Pattern[str] | None
    suppressors: Lex
    conditional: Lex
    rule_suppressors: Lex
    brand_names: Lex
    skip_url: tuple[re.Pattern[str], ...]
    providers: tuple[ProviderRule, ...]
    relabel: Lex
    ml_evidence: Lex
    model_word: re.Pattern[str]
    model_not_ml: frozenset[str]
    indicators: dict[str, Lex]
    modal: Lex
    job_role: Lex
    imperatives: frozenset[str]
    governance: Lex
    limiting: Lex
    human_reviewed: Lex
    automated: Lex
    advisory: Lex
    pilot: Lex
    deployed: Lex
    present: Lex
    loci: dict[str, Lex]
    ai_types: tuple[tuple[str, Lex], ...]
    job_skills: Lex
    building: Lex
    data_terms: Lex
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    past: Lex = field(default_factory=Lex)
    """[use_state.past]: past-tense deployment cues, in production unless M2 wording is present."""
    second_person: Lex = field(default_factory=Lex)
    """[indicators.M3].second_person: not M3 on a first-party spoken source."""


@functools.lru_cache(maxsize=8)
def _load_signals_cached(path: str) -> Signals:
    raw_bytes = Path(path).read_bytes()
    data = tomllib.loads(raw_bytes.decode("utf-8"))
    core = data.get("core", {})
    guards = {k: re.compile(v) for k, v in (data.get("guards") or {}).items()}

    # AI terms: [core] (plural, hyphen compounds) plus [ai_terms]; short all-caps core terms are case-sensitive.
    ci_terms, cs_words = [], []
    for t in core.get("terms", []):
        (cs_words if t.isupper() and len(t) <= 4 else ci_terms).append(t)
    extra = data.get("ai_terms", {})
    term_guards = {k: re.compile(v) for k, v in (data.get("ai_term_guards") or {}).items()}
    guarded: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = []
    plain_ci, plain_cs = [], []
    for t in ci_terms:
        if t in term_guards:
            guarded.append((t, re.compile(f"(?i:{_phrase_pattern(t, plural=True)})"), term_guards[t]))
        else:
            plain_ci.append(t)
    for t in cs_words:
        if t in term_guards:
            guarded.append((t, re.compile(_word_pattern(t) + "|" + _word_pattern(t + "s")), term_guards[t]))
        else:
            plain_cs.append(t)
    cs_regex = []
    for r in core.get("case_sensitive", []):
        if r == "MCP":
            continue  # guarded below
        cs_regex.append(rf"(?-i:(?<!\w)(?:{r})s?(?!\w))")
    ai_terms = _compile([*plain_ci, *extra.get("phrases", [])], [*plain_cs, *extra.get("case_sensitive", [])],
                        [*cs_regex, *extra.get("regex", [])], plural=True)
    mcp_guard = guards.get("MCP")
    if "MCP" in core.get("case_sensitive", []):
        guarded.append(("MCP", re.compile(r"(?<!\w)MCPs?(?!\w)"),
                        mcp_guard or re.compile(r"(?i)model context protocol")))

    sup = data.get("suppressors", {})
    suppressors = _compile([p for p in sup.get("phrases", []) if not p.isupper()],
                           [p for p in sup.get("phrases", []) if p.isupper()])
    conditional = _compile(sup.get("conditional", []))
    rs = data.get("rule_suppressors", {})
    rule_suppressors = _compile(rs.get("phrases", []), rs.get("case_sensitive", []), rs.get("regex", []))
    skip_url = tuple(re.compile(r, re.I) for r in rs.get("skip_url", []))
    brand_names = _table_lex(data.get("brand_names"))

    providers: list[ProviderRule] = []
    for entry in data.get("provider", []) or []:
        pats = entry.get("patterns", [])
        rx = re.compile(r"(?<!\w)(?:" + "|".join(pats) + r")(?!\w)")
        g = entry.get("guard")
        guard = guards.get(g) if g in guards else (re.compile(g) if g else None)
        single = all(re.fullmatch(r"[A-Za-z]+", p) for p in pats)
        providers.append(ProviderRule(name=entry["name"], rx=rx, guard=guard, single_word=single and bool(g)))

    dt_cfg = data.get("definition_test", {})
    relabel = _compile(regex=dt_cfg.get("relabel_regex", []))
    ml_evidence = _compile(dt_cfg.get("ml_phrases", []), dt_cfg.get("ml_case_sensitive", []))
    model_not_ml = frozenset(w.lower() for w in dt_cfg.get("model_not_ml", []))

    ind = data.get("indicators", {})
    indicators = {code: _table_lex(ind.get(code)) for code in ("G2", "G3", "G4", "G5", "G8", "G11", "M1", "M2",
                                                               "M3", "M5")}
    modal = _compile(case_sensitive=[], phrases=(ind.get("M2", {}) or {}).get("modal", []))
    job_role = _compile((ind.get("M2", {}) or {}).get("job_role_words", []))
    imperatives = frozenset(w.lower() for w in (ind.get("M3", {}) or {}).get("imperatives", []))

    auto = data.get("autonomy", {})
    us = data.get("use_state", {})
    loci_cfg = data.get("loci", {})
    loci = {name: _table_lex(loci_cfg.get(name)) for name in ("sdlc", "delivery_ops", "corporate_internal",
                                                              "vendor_addon", "relationship", "staff")}
    loci["delivery_ops_weak"] = _compile((loci_cfg.get("delivery_ops") or {}).get("weak_phrases", []))
    loci["delivery_ops_md3p"] = _compile((loci_cfg.get("delivery_ops") or {}).get("md3p_phrases", []))
    loci["delivery_ops_artefact"] = _compile((loci_cfg.get("delivery_ops") or {}).get("artefact_phrases", []))
    at = data.get("ai_types", {})
    ai_types = tuple((name, _table_lex(at.get(name))) for name in ("agentic", "genai_llm", "document_ai",
                                                                    "conversational", "aiops", "predictive_ml"))
    return Signals(
        path=path,
        version=str((data.get("rules") or {}).get("version") or data.get("version", "")),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        ai_terms=ai_terms,
        guarded_terms=tuple(guarded),
        mcp_guard=mcp_guard,
        suppressors=suppressors,
        conditional=conditional,
        rule_suppressors=rule_suppressors,
        brand_names=brand_names,
        skip_url=skip_url,
        providers=tuple(providers),
        relabel=relabel,
        ml_evidence=ml_evidence,
        model_word=re.compile(r"(?i)(?<!\w)(\w+)?[\s\-]*(?<!\w)models?(?!\w)"),
        model_not_ml=model_not_ml,
        indicators=indicators,
        modal=modal,
        job_role=job_role,
        imperatives=imperatives,
        governance=_table_lex(data.get("governance")),
        limiting=_table_lex(data.get("limiting")),
        human_reviewed=_table_lex(auto.get("human_reviewed")),
        automated=_table_lex(auto.get("automated")),
        advisory=_table_lex(auto.get("advisory")),
        pilot=_table_lex(us.get("pilot")),
        deployed=_table_lex(us.get("deployed")),
        present=_table_lex(us.get("present")),
        loci=loci,
        ai_types=ai_types,
        job_skills=_compile((data.get("job") or {}).get("skill_phrases", [])),
        building=_table_lex(data.get("building")),
        data_terms=_table_lex(data.get("data_terms")),
        raw=data,
        past=_table_lex(us.get("past")),
        second_person=_compile((ind.get("M3", {}) or {}).get("second_person", [])),
    )


def load_signals(path: str | Path | None = None) -> Signals:
    """Compile the lexicon tables of ``config/lexicon.toml`` (cached per resolved path)."""
    return _load_signals_cached(str(Path(path or DEFAULT_LEXICON).resolve()))


# =========================================================================== source register


@dataclass(frozen=True)
class SourceType:
    """One [[type]] entry of config/sources.toml."""

    id: str
    source_type: str
    sr: str
    family: str
    dated_by: Literal["publication", "retrieval"]
    legal: bool = False
    filing: bool = False
    reason: str = ""
    relations: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    url: re.Pattern[str] | None = None
    path: re.Pattern[str] | None = None
    title: re.Pattern[str] | None = None
    dated: bool | None = None
    origin: str = ""
    """'vendor' when the page carries the vendor's own release (a wire mirror, a partner's press-room copy of a joint
    release): its words originate with the vendor, so it never corroborates the vendor independently."""

    def matches(self, *, relation: str, family: str, kind: str, host: str, url: str, path: str, title: str,
                dated: bool) -> bool:
        if self.relations and relation not in self.relations:
            return False
        if self.families and family not in self.families:
            return False
        if self.kinds and kind not in self.kinds:
            return False
        if self.hosts and not any(_host_matches(host, h) for h in self.hosts):
            return False
        if self.url is not None and not self.url.search(url):
            return False
        if self.path is not None and not self.path.search(path):
            return False
        if self.title is not None and not self.title.search(title or ""):
            return False
        if self.dated is not None and self.dated != dated:
            return False
        return True


@dataclass(frozen=True)
class SourceRegister:
    """config/sources.toml: ordered source types (first match wins) and third-party publisher names."""

    path: str
    version: str
    sha256: str
    types: tuple[SourceType, ...]
    publishers: tuple[tuple[str, str], ...]

    def publisher_for(self, host: str) -> str:
        for suffix, name in self.publishers:
            if _host_matches(host, suffix):
                return name
        return host[4:] if host.startswith("www.") else host

    def type_by_id(self, type_id: str) -> SourceType:
        for t in self.types:
            if t.id == type_id:
                return t
        raise KeyError(type_id)

    @functools.cached_property
    def vendor_origin_types(self) -> frozenset[str]:
        """The ``source_type`` names of the entries whose ``origin`` is 'vendor'."""
        return frozenset(t.source_type for t in self.types if t.origin == "vendor")


_SR_GRADES = ("A", "B", "C", "D", "E", "F")


@functools.lru_cache(maxsize=8)
def _load_sources_cached(path: str) -> SourceRegister:
    raw = Path(path).read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    types: list[SourceType] = []
    for t in data.get("type", []) or []:
        sr = t.get("sr", "")
        if sr not in _SR_GRADES:
            raise ValueError(f"{path}: type {t.get('id')!r} has SR {sr!r}; expected one of A..F")
        if t.get("dated_by") not in ("publication", "retrieval"):
            raise ValueError(f"{path}: type {t.get('id')!r} needs dated_by = publication or retrieval")
        if t.get("family") not in {f.value for f in SourceFamily}:
            raise ValueError(f"{path}: type {t.get('id')!r} has an unknown family {t.get('family')!r}")
        bad = set(t.get("relations", [])) - set(RELATIONS)
        if bad:
            raise ValueError(f"{path}: type {t.get('id')!r} has unknown relations {sorted(bad)}")
        if t.get("origin", "") not in ("", "vendor"):
            raise ValueError(f"{path}: type {t.get('id')!r} has origin {t.get('origin')!r}; expected 'vendor' or none")
        types.append(SourceType(
            id=t["id"], source_type=t["source_type"], sr=sr, family=t["family"], dated_by=t["dated_by"],
            legal=bool(t.get("legal", False)), filing=bool(t.get("filing", False)), reason=t.get("reason", ""),
            relations=tuple(t.get("relations", ())), families=tuple(t.get("families", ())),
            kinds=tuple(t.get("kinds", ())), hosts=tuple(h.lower() for h in t.get("hosts", ())),
            url=re.compile(t["url"], re.I) if t.get("url") else None,
            path=re.compile(t["path"], re.I) if t.get("path") else None,
            title=re.compile(t["title"], re.I) if t.get("title") else None,
            dated=t.get("dated"), origin=t.get("origin", ""),
        ))
    vendor_names = {t.source_type for t in types if t.origin == "vendor"}
    shared = sorted(vendor_names & {t.source_type for t in types if t.origin != "vendor"})
    if shared:  # cluster.independent reads the origin from the item's source_type, so the name must be unambiguous
        raise ValueError(f"{path}: source types {shared} are used with and without origin = 'vendor'")
    pubs = tuple(sorted(((k.lower(), v) for k, v in (data.get("publishers") or {}).items()),
                        key=lambda kv: (-len(kv[0]), kv[0])))
    return SourceRegister(path=path, version=str(data.get("version", "")), sha256=hashlib.sha256(raw).hexdigest(),
                          types=tuple(types), publishers=pubs)


def load_sources(path: str | Path | None = None) -> SourceRegister:
    """Load ``config/sources.toml`` (cached per resolved path)."""
    return _load_sources_cached(str(Path(path or DEFAULT_SOURCES).resolve()))


def vendor_origin_types(sources: SourceRegister | None = None) -> frozenset[str]:
    """Source types whose words originate with the vendor although a third party hosts them (``origin =
    "vendor"`` in config/sources.toml: press-release mirrors, partners' press-room copies of joint releases).
    footprint.cluster never counts such an item as independent of the vendor's own channels."""
    return sources.vendor_origin_types if sources is not None else _default_vendor_origin_types()


@functools.lru_cache(maxsize=1)
def _default_vendor_origin_types() -> frozenset[str]:
    """``vendor_origin_types`` of the default register, computed once (cluster.independent asks for every pair)."""
    return load_sources().vendor_origin_types


def _host_matches(host: str, suffix: str) -> bool:
    host, suffix = host.lower(), suffix.lower()
    if suffix.startswith("."):
        return host.endswith(suffix)
    return host == suffix or host.endswith("." + suffix)


class SourceInfo(BaseModel):
    """What the register says about one document (contract section 3)."""

    model_config = ConfigDict(frozen=True)

    source_type: str
    publisher: str
    sr: SourceReliability
    first_party: bool
    dated_by: Literal["publication", "retrieval"]
    legal: bool = False
    filing: bool = False
    type_id: str = ""
    family: SourceFamily = SourceFamily.PRD
    relation: str = "third_party"


# =========================================================================== seeds: names, parties, collisions


@dataclass(frozen=True)
class _Party:
    """An affiliate or platform supplier from the seeds."""

    name: str
    domain: str
    names: tuple[str, ...]
    status: str = ""


@dataclass(frozen=True)
class _Names:
    vendor_id: str
    short: str
    aliases: tuple[str, ...]
    legal: tuple[str, ...]
    vendor_names: tuple[str, ...]
    domains: tuple[str, ...]
    cik: str
    service_terms: tuple[str, ...]
    family_terms: tuple[str, ...]
    persons: tuple[str, ...]
    collisions: tuple[str, ...]
    collision_ciks: tuple[str, ...]
    affiliates: tuple[_Party, ...]
    suppliers: tuple[_Party, ...]
    ats_hosts: tuple[str, ...]
    vendor_rx: re.Pattern[str] | None
    collision_rx: re.Pattern[str] | None
    person_rx: re.Pattern[str] | None
    product_rx: re.Pattern[str] | None
    service_rx: re.Pattern[str] | None
    family_rx: re.Pattern[str] | None
    supplier_rx: re.Pattern[str] | None
    affiliate_rx: re.Pattern[str] | None
    inferred_affiliate_rx: re.Pattern[str] | None


def _name_pattern(name: str) -> str:
    """Whole-word pattern for an entity name: acronyms exact case, other names case-insensitive."""
    words = name.split()
    if len(words) == 1 and name.isupper() and len(name) <= 6:
        return _word_pattern(name)
    return f"(?i:{_phrase_pattern(name)})"


def _alt(patterns: Iterable[str]) -> re.Pattern[str] | None:
    pats = [p for p in patterns if p]
    return re.compile("|".join(pats)) if pats else None


def _strip_paren(name: str) -> tuple[str, str]:
    m = re.match(r"^\s*(.*?)\s*(?:\((.*)\))?\s*$", name or "")
    return ((m.group(1) or "").strip(), (m.group(2) or "").strip()) if m else (name.strip(), "")


def _party_names(name: str) -> tuple[str, ...]:
    """Names to look for: the base name, the parenthetical names or products, and a distinctive first word."""
    base, paren = _strip_paren(name)
    out: list[str] = []
    if base:
        out.append(base)
    for part in re.split(r"\s+d/?b/?a\s+|;|,", paren, flags=re.I):
        part = part.strip()
        if part and not re.search(r"\b(?:software|software provider)\b", part, re.I):
            out.append(part)
    for phrase in list(out):
        first = phrase.split()[0] if phrase.split() else ""
        if (len(phrase.split()) > 1 and len(first) >= 4 and first[:1].isupper() and not first.isupper()
                and first.lower() not in _GENERIC_NAME_WORDS):
            out.append(first)
    seen: dict[str, None] = {}
    for n in out:
        if n.lower() not in {s.lower() for s in seen}:
            seen[n] = None
    return tuple(seen)


def _strong_terms(terms: Iterable[str]) -> list[str]:
    """Product or service terms distinctive enough to corroborate an entity or name a product: multi-word terms,
    acronyms and capitalised names (single lower-case words such as 'clearing' are too generic)."""
    return [t for t in terms if len(t.split()) > 1 or any(c.isupper() for c in t)]


def _term_pattern(term: str) -> str:
    if len(term.split()) == 1 and any(c.isupper() for c in term):
        return _word_pattern(term)
    return f"(?i:{_phrase_pattern(term)})"


@functools.lru_cache(maxsize=64)
def _names_cached(key: str) -> _Names:
    seeds, vendor_id, profile_name, profile_domain = json.loads(key)
    aliases = tuple(a for a in seeds.get("aliases", []) or [] if a)
    legal = tuple(n for n in seeds.get("legal_names", []) or [] if n)
    seed_name = _strip_paren(str(seeds.get("name", "")))[0]
    names: list[str] = []
    for n in (*aliases, *legal, seed_name, profile_name):
        if n and n.lower() not in {x.lower() for x in names}:
            names.append(n)
    short = aliases[0] if aliases else (seed_name or profile_name or vendor_id)
    domains = [d.lower().removeprefix("www.") for d in seeds.get("domains", []) or [] if d]
    if profile_domain and profile_domain not in domains:
        domains.insert(0, profile_domain)
    cik = str(seeds.get("sec_cik") or "").lstrip("0")
    own = {n.lower() for n in names}
    collisions: list[str] = []
    collision_ciks: list[str] = []
    for c in seeds.get("collisions", []) or []:
        base, note = _strip_paren(str(c))
        m = re.search(r"CIK\s*0*(\d+)", note, re.I)
        if m:
            collision_ciks.append(m.group(1))
        if base and base.lower() not in own:
            collisions.append(base)
    persons = tuple(p for p in seeds.get("person_scrub", []) or [] if p)
    services = tuple(seed_terms(seeds, "service_term"))
    families = tuple(seed_terms(seeds, "family_term"))
    affiliates = tuple(
        _Party(name=str(a.get("name", "")), domain=str(a.get("domain", "")).lower().removeprefix("www."),
               names=_party_names(str(a.get("name", ""))), status=str(a.get("status", "")).lower())
        for a in seeds.get("affiliate", []) or [] if isinstance(a, dict))
    suppliers = tuple(
        _Party(name=str(s.get("name", "")), domain=str(s.get("domain", "")).lower().removeprefix("www."),
               names=_party_names(str(s.get("name", ""))))
        for s in seeds.get("platform_supplier", []) or [] if isinstance(s, dict))
    ats = seeds.get("ats", {}) or {}
    ats_hosts = tuple(h for h in (str(ats.get("host", "")).lower(),
                                  urlsplit("https://" + str(ats.get("backend", ""))).hostname or "") if h)
    strong = _strong_terms([*services, *families])
    return _Names(
        vendor_id=vendor_id, short=short, aliases=aliases, legal=legal, vendor_names=tuple(names),
        domains=tuple(domains), cik=cik, service_terms=services, family_terms=families, persons=persons,
        collisions=tuple(collisions), collision_ciks=tuple(collision_ciks), affiliates=affiliates,
        suppliers=suppliers, ats_hosts=ats_hosts,
        vendor_rx=_alt(_name_pattern(n) for n in sorted(names, key=len, reverse=True)),
        collision_rx=_alt(f"(?i:{_phrase_pattern(c)})" for c in sorted(collisions, key=len, reverse=True)),
        person_rx=_alt(f"(?i:{_phrase_pattern(p)})" for p in persons),
        product_rx=_alt(_term_pattern(t) for t in sorted(strong, key=len, reverse=True)),
        service_rx=_alt(f"(?i:{_phrase_pattern(t)})" for t in sorted(services, key=len, reverse=True)),
        family_rx=_alt(f"(?i:{_phrase_pattern(t)})" for t in sorted(families, key=len, reverse=True)),
        supplier_rx=_alt(_term_pattern(n) for p in suppliers for n in sorted(p.names, key=len, reverse=True)),
        affiliate_rx=_alt(_term_pattern(n) for p in affiliates for n in sorted(p.names, key=len, reverse=True)),
        inferred_affiliate_rx=_alt(_term_pattern(n) for p in affiliates if p.status != "confirmed"
                                   for n in sorted(p.names, key=len, reverse=True)),
    )


SEED_KEYS: tuple[str, ...] = ("name", "aliases", "legal_names", "domains", "sec_cik", "collisions", "person_scrub",
                              "service_term", "family_term", "affiliate", "platform_supplier", "ats")
"""The seed keys the rules may read (contract section 0). A seed's expect, relevance and note are never read."""


def _allowed_seeds(seeds: Mapping[str, Any]) -> dict[str, Any]:
    return {k: seeds.get(k) for k in SEED_KEYS if k in seeds}


def _names(seeds: Mapping[str, Any], profile: VendorProfile) -> _Names:
    """Names, terms and parties from the seeds the contract allows (never expect, relevance or note)."""
    key = json.dumps([_allowed_seeds(seeds), profile.vendor_id, profile.name, profile.domain], sort_keys=True,
                     default=str)
    return _names_cached(key)


def _mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """Blank out spans (same length, so offsets stay valid)."""
    chars = list(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(chars), e)):
            if chars[i] not in "\n":
                chars[i] = " "
    return "".join(chars)


def _spans(rx: re.Pattern[str] | None, text: str) -> list[tuple[int, int]]:
    if rx is None or not text:
        return []
    return [(m.start(), m.end()) for m in rx.finditer(text) if m.end() > m.start()]


# =========================================================================== entity guard


@dataclass(frozen=True)
class _Entity:
    ok: bool
    relation: str
    reason: str
    party: str = ""


def _doc_url(document: Document, capture: Capture) -> str:
    return document.url or capture.url_final or capture.url_requested


def _relation(document: Document, capture: Capture, names: _Names) -> tuple[str, str]:
    """(relation, party name) of the document to the vendor, from the URL and the seeds only."""
    url = _doc_url(document, capture)
    host = host_of(url)
    if document.kind == "dns" or host in ("dns.google", "cloudflare-dns.com", "dns.quad9.net"):
        qname = (parse_qs(urlsplit(url).query).get("name") or [""])[0].lower().rstrip(".")
        if qname and any(_host_matches(qname, d) for d in names.domains):
            return "dns", ""
    if any(_host_matches(host, d) for d in names.domains):
        return "first_party", ""
    if names.ats_hosts and any(host == h for h in names.ats_hosts) and (
            document.family == SourceFamily.JOB or capture.family == SourceFamily.JOB):
        return "ats", ""
    if _host_matches(host, "sec.gov") and names.cik and re.search(rf"/edgar/data/0*{names.cik}/", url):
        return "filing", ""
    for p in names.affiliates:
        if p.domain and _host_matches(host, p.domain):
            return ("affiliate_confirmed" if p.status == "confirmed" else "affiliate_inferred"), p.names[0]
    for p in names.suppliers:
        if p.domain and _host_matches(host, p.domain):
            return "platform_supplier", p.names[0] if p.names else p.name
    if document.family == SourceFamily.EXEC or capture.family == SourceFamily.EXEC:
        return "executive", ""
    return "third_party", ""


def _entity(document: Document, capture: Capture, names: _Names, text: str) -> _Entity:
    relation, party = _relation(document, capture, names)
    url = _doc_url(document, capture)
    if names.collision_ciks and _host_matches(host_of(url), "sec.gov"):
        for c in names.collision_ciks:
            if re.search(rf"/edgar/data/0*{c}/", url) or re.search(rf"\bCIK\s*0*{c}\b", text or ""):
                return _Entity(False, relation, f"collision: SEC filer CIK {c} is a different company")
    if relation in ("first_party", "ats", "filing", "dns", "affiliate_confirmed"):
        return _Entity(True, relation, relation)
    if relation in ("affiliate_inferred", "platform_supplier"):
        return _Entity(True, relation, f"{relation}: {party}", party)
    title = document.title or ""
    hay = f"{title}\n{text or ''}"
    masked = _mask(hay, _spans(names.collision_rx, hay))
    found = sorted({m.group(0) for m in names.vendor_rx.finditer(masked)}) if names.vendor_rx else []
    if relation == "executive":
        if found or _spans(names.person_rx, hay):
            return _Entity(True, relation, "executive statement naming the vendor or a seeded executive")
        relation = "third_party"
    if not found:
        hits = sorted({hay[s:e] for s, e in _spans(names.collision_rx, hay)})
        if hits:
            return _Entity(False, relation, f"collision: {hits[0]!r} is a different entity")
        return _Entity(False, relation, "the source does not name the vendor")
    # corroboration: a link to a vendor domain, the CIK, a legal name, a seeded executive, a product or service
    # term, or two different names of the vendor (an acronym and its expansion)  *(interpretation)*
    low = (url + "\n" + hay).lower()
    if any(re.search(rf"(?<![\w-]){re.escape(d)}(?![\w-])", low) for d in names.domains):
        return _Entity(True, relation, "vendor named and linked")
    if names.cik and re.search(rf"\b(?:cik\s*)?0*{names.cik}\b", low) and "cik" in low:
        return _Entity(True, relation, "vendor named with its SEC CIK")
    if any(re.search(_phrase_pattern(n), masked, re.I) for n in names.legal):
        return _Entity(True, relation, "vendor named by its legal name")
    if _spans(names.person_rx, masked):
        return _Entity(True, relation, "vendor named with a seeded executive")
    alias_spans = _spans(names.vendor_rx, masked)
    if names.product_rx and _spans(names.product_rx, _mask(masked, alias_spans)):
        return _Entity(True, relation, "vendor named with one of its products or services")
    lowered = {f.lower() for f in found}
    distinct = [a for a in lowered if not any(a != b and (a in b or b in a) for b in lowered)]
    if len(distinct) >= 2:
        return _Entity(True, relation, "vendor named by two different names")
    return _Entity(False, relation, "vendor named but not corroborated by a link, CIK, legal name, executive or "
                                    "product")


def entity_ok(document: Document, capture: Capture, seeds: dict, profile: VendorProfile, *, text: str = "",
              log: list[str] | None = None) -> bool:
    """The entity guard of design 2.3 (also check V6).

    True for a first-party domain, the vendor's own ATS, an SEC filing under the vendor's CIK, a DoH answer for a
    vendor domain, a confirmed affiliate, and for the seeded relationships that only ever give context items
    (inferred affiliates and platform suppliers). Any other source must name the vendor (legal name or alias, with
    seeded collision phrases masked first) and corroborate it. ``text`` is the document text (or the best text
    available); the title is always checked.

    *(interpretation)* A collision only blocks when it accounts for the vendor's name (for example "Fiserv Forum"
    with no other mention of Fiserv, the GSA "FSSI" programme, Telik's EDGAR filings under CIK 1109196). A first-party
    page is never blocked by a collision, and a collision that is not a name of the vendor (the BNY executive
    "Claude Reumert") is handled by the provider guards instead, so a BNY article quoting her is still BNY evidence.
    """
    ent = _entity(document, capture, _names(seeds, profile), text)
    if not ent.ok and log is not None:
        log.append(f"entity guard: {ent.reason} ({_doc_url(document, capture)})")
    return ent.ok


# =========================================================================== source classification


def _is_dated(document: Document) -> bool:
    return bool(document.published) and document.date_basis in _ARTICLE_DATE_BASES


def classify_source(document: Document, capture: Capture, seeds: dict, profile: VendorProfile, *,
                    sources: SourceRegister | None = None) -> SourceInfo:
    """Source type, publisher and SR from ``config/sources.toml`` (the only thing that sets SR)."""
    reg = sources or load_sources()
    names = _names(seeds, profile)
    relation, party = _relation(document, capture, names)
    url = _doc_url(document, capture)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    url_key = (host + parts.path + (("?" + parts.query) if parts.query else "")).lower()
    path = parts.path.lower()
    family = document.family.value if isinstance(document.family, SourceFamily) else str(document.family)
    chosen: SourceType | None = None
    for t in reg.types:
        if t.matches(relation=relation, family=family, kind=document.kind, host=host, url=url_key, path=path,
                     title=document.title, dated=_is_dated(document)):
            chosen = t
            break
    if chosen is None:
        chosen = SourceType(id="unknown", source_type="Unknown source", sr="F", family=family or "IND",
                            dated_by="publication", reason="no register entry matched")
    if relation in FIRST_PARTY_RELATIONS:
        publisher = names.short
    elif relation in ("affiliate_inferred", "platform_supplier"):
        publisher = party or reg.publisher_for(host)
    else:
        publisher = reg.publisher_for(host)
    return SourceInfo(source_type=chosen.source_type, publisher=publisher, sr=chosen.sr,
                      first_party=relation in FIRST_PARTY_RELATIONS, dated_by=chosen.dated_by,
                      legal=chosen.legal, filing=chosen.filing, type_id=chosen.id,
                      family=SourceFamily(chosen.family), relation=relation)


# =========================================================================== text segmentation


_GLUED = re.compile(r"(?<=[a-z0-9)\]”\"%][.!?])(?=[A-Z][a-z])")
_QUOTE_END = re.compile(r"(?<=[.!?][”’])\s+(?=[“‘\"'(\[]?[A-Z0-9])")
"""A sentence that ends inside a closing curly quote ('... today?” At Automworx'); extract's splitter only knows
straight quotes."""
_NAV_CUT = re.compile(r"\b(?:Read|Learn) [Mm]ore\b(?:\s*[>»›]|(?=\s*(?:arrow_forward|\n|$|[A-Z])))|"
                      r"\b(?:Recent|Related|Popular|Latest) (?:[Pp]osts|[Aa]rticles|[Ii]nsights|[Ss]tories)\b|"
                      r"\barrow_(?:forward|back)\b|\bkeyboard_arrow_\w+")
"""Navigation markers: link lists ('Running AI in Production Read more >', 'Recent Posts') are cut apart here, so a
linked title is shaped on its own (a heading, never a claim) - the design's 'navigation topic links'."""
_LINE_ITEM = re.compile(r"\n(?=[ \t]*(?:[•●▪◦‣*\-–—]\s|[~><≈]?\s?\d[\d,.]*\s?%|\d{1,2}\.\s))")
_CODE = re.compile(r"\{[^}]*\}|:not\(|url\(|px;|var\(--|elementor|function\s*\(|=>|\w+\s*\{")
_WORDS = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_BULLET_START = re.compile(r"\s*(?:[•●▪◦‣*\-–—]\s|[~><≈]?\s?\d[\d,.]*\s?%)")
_LABEL_LIST = re.compile(r"\s*(?:Keywords|Key words|Tags|Topics|Categories|Filed under|People|Regions?|Countries|"
                         r"Industries|Related topics)\s*:", re.I)
"""A metadata list ('Keywords: ... AI infrastructure ...'): navigation, never a claim."""


def _trim(text: str, s: int, e: int) -> tuple[int, int]:
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return s, e


def _segments(text: str) -> list[tuple[int, int]]:
    """Sentence-like units of ``text`` as (start, end): extract's splitter, then glued sentences ("done.The"),
    sentences ending in a closing curly quote, navigation markers ("Read more >", "Recent Posts") and list items
    (bullets, percentages, numbered lines) are split apart."""
    out: list[tuple[int, int]] = []
    for s, e in split_sentences(text):
        marks = [m.start() for m in _GLUED.finditer(text, s, e)] + [m.end() for m in _QUOTE_END.finditer(text, s, e)]
        for m in _NAV_CUT.finditer(text, s, e):
            marks += [m.start(), m.end()]
        cuts = sorted({s, *marks, e})
        for a, b in itertools.pairwise(cuts):
            inner = [a, *[m.start() for m in _LINE_ITEM.finditer(text, a, b)], b]
            for c, d in itertools.pairwise(inner):
                c, d = _trim(text, c, d)
                if d > c:
                    out.append((c, d))
    return out


_NUMERIC_TOKEN = re.compile(r"[~≈<>$€£(+\-]*\d[\d,.:]*(?:%|[xX]|[KkMmBb]n?|bps)?[)+*]*")


def _is_table(t: str, n_words: int, func: int, caps: int) -> bool:
    """A slide, chart or table dump flattened into text: a quarter or more of its tokens are numbers (amounts,
    percentages, years, timestamps), or a run of 20+ words that is three-quarters capitalised labels with few function
    words. It reads as a sentence to the splitter but states nothing,
    so it never becomes a quote (an SEC exhibit's KPI table, an investor-day slide, a podcast's chapter list)."""
    tokens = t.split()
    if len(tokens) >= 8 and sum(1 for w in tokens if _NUMERIC_TOKEN.fullmatch(w)) / len(tokens) >= 0.25:
        return True
    return n_words >= 20 and func / n_words < 0.16 and caps / n_words >= 0.75


def _shape(t: str) -> str:
    """'code', 'nav', 'table', 'heading' or 'prose' (only prose can carry a claim)."""
    if _CODE.search(t):
        return "code"
    if _LABEL_LIST.match(t) or _NAV_CUT.fullmatch(t.strip()):
        return "nav"
    words = _WORDS.findall(t)
    n = len(words)
    if n < 3:
        return "nav"
    if t.count("|") >= 2:
        return "nav"
    func = sum(w.lower() in _FUNCTION_WORDS for w in words)
    caps = sum(w[0].isupper() for w in words)
    if _is_table(t, n, func, caps):
        return "table"
    end = t.rstrip().rstrip("\"'”’)]*").rstrip()
    terminal = end.endswith((".", "!", "?", ":", ";"))
    if terminal:
        if func == 0 and n >= 4 and caps / n >= 0.8:
            return "nav"
        return "prose"
    if _BULLET_START.match(t) and n >= 4:
        return "prose"
    if n >= 13 and (func / n >= 0.1 or caps / n < 0.6):
        return "prose"
    if func == 0 and caps / n >= 0.6:
        return "nav"
    return "heading"


def _heading_before(doc_text: str, pos: int, *, pdf: bool) -> str:
    """The nearest heading line above ``pos`` (within 3,000 characters), or ''."""
    if not doc_text or pos <= 0:
        return ""
    line_start = doc_text.rfind("\n", 0, pos) + 1
    lo = max(0, line_start - 3000)
    lines = doc_text[lo:line_start].split("\n")
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line or len(line) > 140:
            continue
        if _shape(line) != "heading":
            continue
        if pdf:
            prev_blank = i == 0 or not lines[i - 1].strip()
            next_blank = i + 1 >= len(lines) or not lines[i + 1].strip()
            if not (line.isupper() or (prev_blank and next_blank)):
                continue
        return line
    return ""


# =========================================================================== AI terms and providers


@dataclass(frozen=True)
class _AiView:
    """AI-term and provider matches over one passage (offsets in the passage text)."""

    masked: str
    terms: tuple[tuple[int, int, str], ...]
    providers: tuple[tuple[int, int, str, str], ...]  # start, end, surface, canonical name


def _masked_for_ai(text: str, sig: Signals, names: _Names) -> str:
    spans: list[tuple[int, int]] = []
    for lex in (sig.suppressors, sig.rule_suppressors, sig.brand_names):
        spans += [(s, e) for s, e, _ in lex.find(text)]
    spans += _spans(names.collision_rx, text)
    # seeded names that contain an AI term name an organisation, not a claim ("Labarum AI")
    org_names = [n for p in (*names.affiliates, *names.suppliers) for n in p.names] + list(names.vendor_names)
    for n in org_names:
        if sig.ai_terms.search(n):
            spans += [(m.start(), m.end()) for m in re.finditer(_phrase_pattern(n), text, re.I)]
    masked = _mask(text, spans)
    if sig.conditional.search(masked) and not sig.ai_terms.search(masked):
        masked = _mask(masked, [(s, e) for s, e, _ in sig.conditional.find(masked)])
    return masked


def _find_providers(masked: str, context: str, sig: Signals) -> list[tuple[int, int, str, str]]:
    found: list[tuple[int, int, str, str]] = []
    for rule in sig.providers:
        for m in rule.rx.finditer(masked):
            if rule.guard is not None and not rule.guard.search(context):
                continue
            if rule.single_word:
                nxt = re.match(r"\s+([A-Z][a-z]+)", masked[m.end():m.end() + 30])
                if nxt and nxt.group(1) not in _MODEL_WORDS_AFTER_NAME:
                    continue  # "Claude Reumert", "Gemini Trust": a person or another company
            found.append((m.start(), m.end(), m.group(0), rule.name))
    found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
    kept: list[tuple[int, int, str, str]] = []
    for f in found:
        if kept and f[0] < kept[-1][1]:
            continue
        kept.append(f)
    return kept


def _ai_view(text: str, sig: Signals, names: _Names, *, doc_text: str = "") -> _AiView:
    masked = _masked_for_ai(text, sig, names)
    terms = list(sig.ai_terms.find(masked))
    others = bool(terms)
    providers = _find_providers(masked, masked, sig)
    for label, rx, guard in sig.guarded_terms:
        for m in rx.finditer(masked):
            if label == "MCP":
                ok = bool(re.search(r"(?i)model[\s-]+context[\s-]+protocol", doc_text or masked)) or others or \
                    bool(providers)
            else:
                ok = bool(guard.search(_mask(masked, [(m.start(), m.end())])))
            if ok:
                terms.append((m.start(), m.end(), m.group(0)))
    terms += [(s, e, surface) for s, e, surface, _ in providers]
    terms.sort()
    return _AiView(masked=masked, terms=tuple(terms), providers=tuple(providers))


# =========================================================================== indicators


def _ind(code: str, span: str) -> Indicator:
    return Indicator(code=code, span=span)


def _first_ai_term(text: str, sig: Signals) -> str:
    hits = sig.ai_terms.find(text)
    return hits[0][2] if hits else ""


def find_indicators(text: str, *, source: SourceInfo | None = None,
                    signals: Signals | None = None) -> list[Indicator]:
    """G/M indicator hits in ``text`` whose spans are exact substrings of it, one per (code, span), in text order.

    Purely lexical: G1 provider names, G2 features, G3 data flow, G4 release wording, G5 developer artifacts, G11
    quantities, M1 buzzwords, M2 aspirational and modal wording, M5 superlatives, M4 automation relabelled as AI.
    G6 (legal artifact) and G7 (filing) come from ``source`` only; their span is the first AI term of the text.
    G8 (job duties), M3 (commentary), M6 (no product named) need the claim context and are added by tag_passage.
    """
    sig = signals or load_signals()
    hits: list[tuple[int, str, str]] = []
    masked = _masked_for_ai(text, sig, _EMPTY_NAMES)
    for s, e, _surface, _ in _find_providers(masked, masked, sig):
        hits.append((s, "G1", text[s:e]))
    for code in ("G2", "G3", "G4", "G5", "G11", "M1", "M2", "M5"):
        for s, e, _ in sig.indicators[code].find(text):
            hits.append((s, code, text[s:e]))
    for s, e, _ in sig.modal.find(text):
        hits.append((s, "M2", text[s:e]))
    if not sig.ml_evidence.search(text) and not _model_is_ml(text, sig):
        for s, e, _ in sig.relabel.find(text):
            hits.append((s, "M4", text[s:e]))
    if source is not None and (source.legal or source.filing):
        term = _first_ai_term(_masked_for_ai(text, sig, _EMPTY_NAMES), sig)
        if term:
            pos = _masked_for_ai(text, sig, _EMPTY_NAMES).find(term)
            if source.legal:
                hits.append((pos, "G6", text[pos:pos + len(term)]))
            if source.filing:
                hits.append((pos, "G7", text[pos:pos + len(term)]))
    return _dedupe_indicators(hits)


def _dedupe_indicators(hits: Iterable[tuple[int, str, str]]) -> list[Indicator]:
    seen: set[tuple[str, str]] = set()
    out: list[Indicator] = []
    order = {c: i for i, c in enumerate(("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11", "M1",
                                         "M2", "M3", "M4", "M5", "M6", "M7"))}
    for _, code, span in sorted(hits, key=lambda h: (h[0], order.get(h[1], 99), h[2])):
        if not span or (code, span) in seen:
            continue
        seen.add((code, span))
        out.append(_ind(code, span))
    return out


def _model_is_ml(text: str, sig: Signals) -> bool:
    for m in re.finditer(r"(?i)(?<!\w)models?(?!\w)", text):
        before = re.findall(r"[A-Za-z]+", text[max(0, m.start() - 30):m.start()])
        prev = before[-1].lower() if before else ""
        if prev and prev in sig.model_not_ml:
            continue
        if prev == "context":  # "Model Context Protocol" is counted on its own
            continue
        return True
    return False


def indicator_ok(code: str, span: str, *, signals: Signals | None = None) -> bool:
    """The lexical test of check V4 for one indicator span (G6, G7, G9 and G10 always fail)."""
    sig = signals or load_signals()
    if code in ("G6", "G7", "G9", "G10") or not span or not span.strip():
        return False
    if code == "G1":
        masked = _masked_for_ai(span, sig, _EMPTY_NAMES)
        return bool(_find_providers(masked, masked, sig))
    if code == "M2":
        return bool(sig.indicators["M2"].search(span) or sig.modal.search(span))
    if code == "M4":
        return bool(sig.relabel.search(span))
    if code == "G8":
        return bool(sig.indicators["G8"].search(span))
    if code in sig.indicators:
        return bool(sig.indicators[code].search(span))
    return True  # M6 (no product named) and M7 (logo-only partnership) have no lexical test


# =========================================================================== tag functions


def specificity(indicators: Sequence[Indicator], *, named_target: bool,
                temporal: Temporal = "unclear") -> SpecificityGrade:
    """SP from indicator codes (design 2.3; contract section 3, first rule wins).

    1. S0 when temporal is planned, or M2 or M3 is present and none of G2, G3, G4, G8, G11 is ("M2/M3 dominate",
       *(interpretation)*).
    2. S3 when any of G1, G5, G6, G7 is present together with any of G2, G3.
    3. S2 when any of G2, G4, G5, G8, G11 is present, the target is named, and M4 is absent.
    4. S1 otherwise (generic legal or filing language, M1/M5/M6/M7 only, G6 or G7 alone, nothing at all).
    """
    codes = {i.code for i in indicators}
    if temporal == "planned" or (codes & {"M2", "M3"} and not codes & {"G2", "G3", "G4", "G8", "G11"}):
        return "S0"
    if codes & {"G1", "G5", "G6", "G7"} and codes & {"G2", "G3"}:
        return "S3"
    if codes & {"G2", "G4", "G5", "G8", "G11"} and named_target and "M4" not in codes:
        return "S2"
    return "S1"


def _service_text(text: str, names: _Names) -> str:
    """``text`` with the vendor's own names masked, so 'The Clearing House' never counts as the term 'clearing'."""
    return _mask(text, _spans(names.vendor_rx, text))


def _term_hit(rx: re.Pattern[str] | None, *texts: str) -> bool:
    return rx is not None and any(t and rx.search(t) for t in texts)


def relevance(claim_text: str, seeds: dict, locus: Locus, *, title: str = "", heading: str = "") -> RelevanceGrade:
    """RL, computed locally only (check V7). R3 needs an exact-service term in the claim sentence, its heading or
    the page title (whole words, case-insensitive; the vendor's own names are masked first) and a service locus."""
    if locus == "commentary":
        return "R0"
    if locus in ("platform_supplier", "affiliate_inferred"):
        return "R1"
    if locus == "relationship":
        return "R2"
    names = _names_cached(json.dumps([_allowed_seeds(seeds), str(seeds.get("vendor_id", "")), "", ""],
                                     sort_keys=True, default=str))
    return relevance_from_names(claim_text, names, locus, title=title, heading=heading)


def _months_between(earlier: dt.date, later: dt.date) -> int:
    if earlier >= later:
        return 0
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(0, months)


def _iso_date(value: str) -> dt.date | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def recency(published: str, retrieved_at: str, as_of: str, *,
            dated_by: Literal["publication", "retrieval"]) -> RecencyGrade:
    """RC against ``as_of``: T3 is 12 months or less, T2 24 or less, T1 36 or less, T0 older or undated.

    A source dated by publication (articles, releases, filings, postings) is dated by ``published`` only; with no
    publication date it is undated, so T0 (design 2.3: "T0 is older or undated"). Extraction already falls back to
    the Wayback first-seen date (date basis "wayback first-seen") when one is known, so an empty ``published`` here
    means no date was found at all. The retrieval date never stands in for a publication date: it would make any
    undated article look current. A source dated by retrieval (live product, docs, policy and trust pages) uses
    the retrieval date. A date after ``as_of`` counts as 0 months.
    """
    ref = _iso_date(as_of)
    if ref is None:
        raise ValueError(f"as_of must be an ISO date YYYY-MM-DD, not {as_of!r}")
    if dated_by == "publication":
        date = _iso_date(published)
    else:
        date = _iso_date(retrieved_at[:10] if retrieved_at else "")
    if date is None:
        return "T0"
    months = _months_between(date, ref)
    return "T3" if months <= 12 else "T2" if months <= 24 else "T1" if months <= 36 else "T0"


def strength_label(u_class: UClass, sp: SpecificityGrade, rl: RelevanceGrade, locus: Locus, *,
                   is_q: bool, is_k: bool) -> Strength:
    """The fixed label order of design 2.3 (first rule that matches wins)."""
    if u_class == "U8":
        return "Negative"
    if is_q and rl == "R3":
        return "Strong"
    if is_q or is_k:
        return "Moderate"
    if locus == "relationship":
        return "Context - relationship only"
    if locus == "platform_supplier":
        return "Context - platform supplier"
    if locus == "affiliate_inferred":
        return "Context - inferred affiliate"
    if u_class == "U7" or sp == "S0":
        return "Marketing only"
    return "Weak"


def _q_tags(tags: SignalTags) -> bool:
    return (tags.sr in ("A", "B") and tags.sp_level >= 2 and tags.rl_level >= 2 and tags.rc_level >= 1
            and tags.u_number <= 4 and tags.locus in QUALIFYING_LOCI)


def _k_tags(tags: SignalTags) -> bool:
    return (tags.sr in ("A", "B", "C") and tags.sp_level >= 2 and tags.rl_level >= 2 and tags.rc_level >= 1
            and tags.u_number <= 4)


def is_qualifying(item: EvidenceItem) -> bool:
    """Q (design 2.7): citable, SR A/B, SP S2+, RL R2+, RC T1+, U1-U4, a qualifying locus."""
    return item.citable and _q_tags(item.tags)


def is_corroborating(item: EvidenceItem) -> bool:
    """The item's own part of K (design 2.7): citable, SR A-C, SP S2+, RL R2+, RC T1+, U1-U4 *(interpretation:
    a K corroborates use, so U5-U8 never count)*. Independence is cluster.independent's job."""
    return item.citable and _k_tags(item.tags)


def _initial_ic(sr: str, sp: str, rc: str, *, dns_token: bool = False) -> int:
    """IC set by the rules (contract section 1): 6 cannot be judged (T0, SR E/F), 3 S0/S1 only, 4 uncorroborated
    C/D at S2+, 2 consistent A/B. A DNS verification token is a direct record: 2 (contract, tag_document)."""
    if dns_token:
        return 2
    if rc == "T0" or sr in ("E", "F"):
        return 6
    if sp in ("S0", "S1"):
        return 3
    if sr in ("C", "D"):
        return 4
    return 2


# =========================================================================== claim analysis


@dataclass
class _Ctx:
    """Per-document context shared by every claim of one document."""

    document: Document
    capture: Capture
    seeds: dict
    profile: VendorProfile
    plan: DepthPlan | None
    as_of: str
    sig: Signals
    names: _Names
    source: SourceInfo
    entity: _Entity
    doc_text: str
    md3p: bool

    @property
    def is_job(self) -> bool:
        return self.source.family == SourceFamily.JOB or self.source.source_type == "Job posting"

    @property
    def first_party(self) -> bool:
        return self.source.first_party


@dataclass
class _Tagged:
    """Everything the rules decide about one claim excerpt."""

    start: int  # document offsets
    end: int
    excerpt: str
    indicators: list[Indicator]
    providers: list[str]
    data_mentioned: list[str]
    temporal: Temporal
    action_level: ActionLevel
    ai_type: AiType
    locus: Locus
    u_class: UClass
    sp: SpecificityGrade
    rl: RelevanceGrade
    rc: RecencyGrade
    ic: int
    strength: Strength
    trap: str
    named_target: bool
    heading: str
    claim_kind: str
    subject: str
    ai_terms: int

    def rank(self) -> tuple:
        """Preference among the claims of one passage: no trap; Negative/Strong/Moderate in label order; then
        relevance, specificity, label order, number of genuine indicators, providers and position."""
        idx = STRENGTH_ORDER.index(self.strength)
        top = idx if self.strength in ("Negative", "Strong", "Moderate") else 3
        genuine = len({i.code for i in self.indicators if i.code.startswith("G")})
        return (bool(self.trap), top, -int(self.rl[1]), -int(self.sp[1]), idx, -genuine, -len(self.providers),
                self.start)


def _names_vendor(text: str, names: _Names) -> bool:
    masked = _mask(text, _spans(names.collision_rx, text))
    return bool(names.vendor_rx and names.vendor_rx.search(masked))


def _names_product(text: str, names: _Names) -> bool:
    return bool(names.product_rx and names.product_rx.search(_service_text(text, names)))


def _names_supplier(text: str, names: _Names) -> bool:
    return bool(names.supplier_rx and names.supplier_rx.search(text))


def _names_inferred_affiliate(text: str, names: _Names) -> bool:
    return bool(names.inferred_affiliate_rx and names.inferred_affiliate_rx.search(text))


_FIRST_PERSON = re.compile(r"(?<!\w)(?:[Ww]e|[Oo]urs?|us|[Ww]e['’](?:re|ve|ll|d))(?!\w)")


def _subject_ok(text: str, ctx: _Ctx) -> bool:
    """Test 2 (subject): first-party pages imply the product; other sources must name the vendor, an alias or a
    product in the claim or the title (a supplier or affiliate page may name the supplier or affiliate)."""
    rel = ctx.entity.relation
    if rel in FIRST_PARTY_RELATIONS:
        return True
    title = ctx.document.title or ""
    for t in (text, title):
        if _names_vendor(t, ctx.names) or _names_product(t, ctx.names):
            return True
        if rel == "platform_supplier" and _names_supplier(t, ctx.names):
            return True
        if rel == "affiliate_inferred" and _names_inferred_affiliate(t, ctx.names):
            return True
    return rel in ("platform_supplier", "affiliate_inferred")


def _is_commentary(text: str, ctx: _Ctx, sig: Signals) -> list[str]:
    """M3 spans when the claim is about other companies, the industry or the reader (else []). A job posting is
    never commentary: it describes the vendor's own role, and 'you' is the candidate. On a first-party spoken
    source (:func:`_spoken`) 'you' and 'your' are spoken style or the audience, not reader advice, so they are not
    M3 there."""
    if ctx.is_job:
        return []
    if ctx.entity.relation in ("platform_supplier", "affiliate_inferred"):
        about_us = _names_supplier(text, ctx.names) or _names_inferred_affiliate(text, ctx.names)
    else:
        about_us = False
    about_us = about_us or _names_vendor(text, ctx.names) or _names_product(_PARTICIPANTS.sub(" ", text), ctx.names)
    if ctx.first_party and _FIRST_PERSON.search(text):
        about_us = True
    if about_us:
        return []
    spans: list[str] = []
    first = re.match(r"\s*(?:\d+\.\s*)?(?:[•*\-–]\s*)?([A-Za-z]+)", text)
    if first and first.group(1).lower() in sig.imperatives and first.group(1)[0].isupper():
        spans.append(first.group(1))
    m3 = sig.indicators["M3"].find(text)
    if ctx.first_party and _spoken(ctx):
        m3 = [h for h in m3 if not sig.second_person.rx or not sig.second_person.rx.fullmatch(h[2])]
    spans += [text[s:e] for s, e, _ in m3]
    return spans


_SPOKEN_SOURCE_TYPES: frozenset[str] = frozenset({"SEC Form DEFA14A", "Executive statement", "Vendor podcast page",
                                                  "Webinar page"})
"""Source types that usually carry spoken words: investor-day transcripts filed as DEFA14A, executive statements,
the vendor's own podcasts and webinars."""
_TRANSCRIPT = re.compile(r"(?i)transcript|earnings[\s_-]*call|investor[\s_-]*day|fireside|keynote")


def _spoken(ctx: _Ctx) -> bool:
    """The source is a transcript or other spoken words (by source type, or 'transcript' in its title or URL)."""
    return (ctx.source.source_type in _SPOKEN_SOURCE_TYPES
            or bool(_TRANSCRIPT.search(f"{ctx.document.title or ''} {ctx.document.url}")))


_PARTICIPANTS = re.compile(r"[\w®™-]+(?:\s+[\w®™-]+)?(?=\s+(?:network\s+)?participants\b)")
"""'RTP participants', 'RTP network participants': the members of a network named after a product. A sentence about
them is about other institutions, so the product name alone does not make it a claim about the vendor."""

_ARTICLE_TYPES: frozenset[str] = frozenset({"Blog post"})


def _about_us(text: str, ctx: _Ctx) -> bool:
    """The sentence names the vendor, one of its products (or, on a supplier or affiliate source, that party), or
    speaks in the first person on a first-party source."""
    if ctx.entity.relation in ("platform_supplier", "affiliate_inferred") and (
            _names_supplier(text, ctx.names) or _names_inferred_affiliate(text, ctx.names)):
        return True
    return (_names_vendor(text, ctx.names) or _names_product(text, ctx.names)
            or bool(ctx.first_party and _FIRST_PERSON.search(text)))


def _carried_commentary(text: str, prev: str, ctx: _Ctx, sig: Signals) -> bool:
    """A claim sentence of an article that names no one continues the previous sentence's subject: when that
    sentence is commentary about a class of other companies ("Modern outsourcing providers leverage cutting-edge
    technologies ..."), the claim ("Innovations such as AI-driven personalization ...") is commentary too. Limited
    to articles (reader-advice blogs, design 2.3 test 2); product pages and postings keep their implied subject."""
    if not prev or ctx.is_job or ctx.source.source_type not in _ARTICLE_TYPES:
        return False
    if _about_us(text, ctx) or _about_us(prev, ctx):
        return False
    return bool(_is_commentary(prev, ctx, sig))


def _job_role_words(text: str, sig: Signals) -> list[tuple[int, int]]:
    """Spans of the M2 words that, in a job posting, describe the role rather than a plan ('you will build',
    'this role will apply', 'can travel'): [indicators.M2].job_role_words."""
    return [(s, e) for s, e, _ in sig.job_role.find(text)]


def _m2_spans(text: str, sig: Signals, *, job: bool) -> list[tuple[int, int, str]]:
    """M2 (aspirational wording and the modal hedges) in ``text``; job-role words are not M2 on a job posting."""
    hits = [*sig.indicators["M2"].find(text), *sig.modal.find(text)]
    if job:
        role = set(_job_role_words(text, sig))
        hits = [h for h in hits if (h[0], h[1]) not in role]
    return hits


def _temporal(text: str, sig: Signals, *, commentary: bool, job: bool = False) -> Temporal:
    """Test 3 (use state): pilot or beta > explicit deployed cue > aspirational or modal (planned) > past-tense
    deployment cue ('re-architected', 'two weeks ago', in production) > present-tense feature wording (in
    production) > unclear. Commentary about others is never dated as the vendor's use. On a job posting, 'will',
    'would', 'can' and the other role words describe the role, not a plan."""
    if commentary:
        return "unclear"
    if sig.pilot.search(text):
        return "pilot_or_beta"
    if sig.deployed.search(text):
        return "in_production"
    if _m2_spans(text, sig, job=job):
        return "planned"
    if sig.past.search(text) or sig.present.search(text):
        return "in_production"
    return "unclear"


def _action_level(text: str, sig: Signals) -> ActionLevel:
    if sig.human_reviewed.search(text):
        return "human_reviewed_decision"
    if sig.automated.search(text):
        return "automated_action"
    if sig.advisory.search(text):
        return "advisory"
    return "unknown"


def _ai_type(text: str, sig: Signals, *, trap: bool) -> AiType:
    if trap:
        return "not_ai"
    for name, lex in sig.ai_types:
        if lex.search(text):
            return name  # type: ignore[return-value]
    return "unspecified"


def _supplier_context(text: str, ctx: _Ctx, heading: str) -> bool:
    """*(interpretation)* A claim that names no one, under a heading or page title about a platform supplier's
    product and not about the vendor, describes the supplier's product: an AutomWorx post titled 'Automic Automation
    V26: ...' saying 'AI Assistant, built on MCP.' is Broadcom's capability, not AutomWorx's use (design 5, V-001)."""
    names = ctx.names
    if names.supplier_rx is None or _names_vendor(text, names) or (
            _names_product(text, names) and not _names_supplier(text, names)):
        return False
    if ctx.first_party and _FIRST_PERSON.search(text):
        return False
    for t in (heading, _strip_site_name(ctx.document.title or "", names)):
        if t and _names_supplier(_service_text(t, names), names) and not _names_vendor(t, names):
            return True
    return False


_TITLE_SEP = re.compile(r"\s+[|–—\-:]\s+")


def _strip_site_name(title: str, names: _Names) -> str:
    """The title without a trailing site-name part ('... | AutomWorx', '... - BNY') that only names the vendor."""
    parts = _TITLE_SEP.split(title.strip())
    if len(parts) > 1 and names.vendor_rx is not None and names.vendor_rx.fullmatch(parts[-1].strip()):
        return title[:title.rfind(parts[-1])].rstrip(" |–—-:")
    return title


def _locus(text: str, ctx: _Ctx, *, prev: str, commentary: bool, providers: Sequence[str],
           features: bool, vendor_use: bool, heading: str = "") -> Locus:
    sig, names, rel = ctx.sig, ctx.names, ctx.entity.relation
    if rel == "platform_supplier":
        return "platform_supplier"
    if rel == "affiliate_inferred":
        return "affiliate_inferred"
    if not vendor_use and _names_supplier(_service_text(text, names), names) and not _names_vendor(text, names):
        return "platform_supplier"
    if not vendor_use and _supplier_context(text, ctx, heading):
        return "platform_supplier"
    if _names_inferred_affiliate(text, names) and not _names_vendor(text, names):
        return "affiliate_inferred"
    if commentary:
        return "commentary"
    loci = sig.loci
    if providers and loci["relationship"].search(text) and not features:
        return "relationship"
    product_source = ctx.source.source_type in PRODUCT_SOURCE_TYPES
    staff = bool(loci["staff"].search(text)) or ctx.is_job
    ctx_text = f"{prev} {text}"
    sdlc = bool(loci["sdlc"].search(text))
    ops_strong = bool(loci["delivery_ops"].search(text)) or (
        ctx.md3p and bool(loci["delivery_ops_md3p"].search(text)))
    ops_weak = bool(loci["delivery_ops_weak"].search(text))
    corp = bool(loci["corporate_internal"].search(text))
    if sdlc and (staff or not product_source):
        return "sdlc"
    if (ops_strong and (staff or not product_source)) or (ops_weak and (ctx.is_job or (staff and not product_source))):
        return "delivery_ops"
    if corp and (staff or not product_source):
        return "corporate_internal"
    if not product_source and not (sdlc or corp) and loci["delivery_ops"].search(ctx_text):
        return "delivery_ops"  # the previous sentence sets the operational context ("sanctions screening")
    if ctx.is_job:
        return "corporate_internal"
    if loci["vendor_addon"].search(text):
        return "vendor_addon"
    if ctx.first_party or _names_product(text, names) or _names_vendor(text, names):
        return "service_feature"
    return "unknown"


def _u_class(*, trap: bool, limiting: bool, locus: Locus, generic: bool, ctx: _Ctx, has_g8: bool, skills: bool,
             governance: bool, building: bool, providers: bool, service_hit: bool, temporal: Temporal) -> UClass:
    """U class with the contract's precedence guidance U8 > U1 > U2 > U3 > U4 > U6 > U5 > U7."""
    if trap:
        return "U7"
    if limiting and locus not in ("platform_supplier", "affiliate_inferred", "commentary", "relationship"):
        return "U8"
    if locus == "commentary":
        return "U7"
    if locus == "relationship":
        return "U4"
    if ctx.is_job and locus not in ("platform_supplier", "affiliate_inferred"):
        # a posting is U3 when it assigns duties that use AI tools, U5 when it only asks for AI skills (or describes
        # building AI capability: hackathons, bootcamps, AI talent); a posting is never a marketing claim
        return "U3" if has_g8 and not skills and not building else "U6" if governance else "U5"
    if generic and not (governance or building or providers):
        return "U7"
    if locus in ("platform_supplier", "affiliate_inferred"):
        if locus == "platform_supplier" and providers and not generic:
            return "U2"
        return "U2" if not generic else "U7"
    if locus == "service_feature":
        if generic:
            return "U6" if governance else "U5" if building else "U4" if providers else "U7"
        return "U1" if service_hit else "U2"
    if locus == "vendor_addon":
        return "U2"
    if locus in ("delivery_ops", "sdlc", "corporate_internal"):
        if temporal in ("in_production", "pilot_or_beta", "planned"):
            return "U3"
        if governance:
            return "U6"
        if building:
            return "U5"
        return "U3"
    if providers:
        return "U4"
    if governance:
        return "U6"
    if building:
        return "U5"
    return "U7"


def _own_operations(text: str, ctx: _Ctx, *, locus: Locus, commentary: bool) -> bool:
    """A first-party sentence in the first person about the vendor's own delivery operations that names an
    operational artefact ([loci.delivery_ops].artefact_phrases: tickets, cases, incidents ...). Such a sentence
    states what the vendor does with AI in its operations, so it is not generic marketing (U7) even when its use
    state is unclear (design 5, V-002: the Investor Day ticket-resolution statement)."""
    return (not commentary and locus == "delivery_ops" and ctx.first_party and not ctx.is_job
            and bool(_FIRST_PERSON.search(text)) and bool(ctx.sig.loci["delivery_ops_artefact"].search(text)))


_CLAIM_KIND = {"U1": "offers_ai_feature", "U2": "offers_ai_feature", "U3": "uses_ai", "U4": "ai_partnership",
               "U5": "uses_ai", "U6": "ai_governance", "U7": "generic_ai_marketing", "U8": "negative_or_limiting"}
_SUBJECT = {"service_feature": "vendor_product", "vendor_addon": "vendor_product", "delivery_ops": "vendor_operations",
            "sdlc": "vendor_staff_tools", "corporate_internal": "vendor_staff_tools",
            "platform_supplier": "third_party_product", "affiliate_inferred": "third_party_product",
            "commentary": "other_or_industry"}


def _analyse(start: int, end: int, ctx: _Ctx, *, prev: str = "", ai_terms: int = 0,
             force_commentary: bool = False) -> _Tagged:
    """Tag the excerpt doc_text[start:end] (tests 1-4, indicators, locus, U class, SP, RL, RC, IC, strength).
    ``force_commentary`` marks a claim that failed the subject test (an LLM claim about someone else)."""
    sig, names = ctx.sig, ctx.names
    text = ctx.doc_text[start:end]
    view = _ai_view(text, sig, names, doc_text=ctx.doc_text)
    providers: list[str] = []
    for s, e, surface, _ in view.providers:
        if surface.lower() not in {p.lower() for p in providers}:
            providers.append(text[s:e])
    title = ctx.document.title or ""
    heading = _heading_before(ctx.doc_text, start, pdf=ctx.document.kind == "pdf")

    # test 1: definition. An AI label on automation with no ML or LLM evidence is a trap.
    trap_hits = [] if (sig.ml_evidence.search(text) or _model_is_ml(text, sig)) else sig.relabel.find(text)
    trap = trap_hits[0][2] if trap_hits else ""

    m3 = _is_commentary(text, ctx, sig)
    commentary = bool(m3) or force_commentary or _carried_commentary(text, prev, ctx, sig)
    raw = find_indicators(text, source=ctx.source, signals=sig)
    hits: list[tuple[int, str, str]] = []
    for ind in raw:
        if ind.code == "G1":
            continue  # providers are taken from the masked view (seeded collisions removed)
        if commentary and ind.code in ("G2", "G3", "G4", "G5", "G11"):
            continue  # features of other companies are not the vendor's genuine-use indicators
        if ind.code == "M4" and not trap:
            continue
        if ind.code == "M2" and ctx.is_job and sig.job_role.rx is not None and sig.job_role.rx.fullmatch(ind.span):
            continue  # 'you will', 'this role will': the role, not a plan
        hits.append((text.find(ind.span), ind.code, ind.span))
    for s, e, _surface, _ in view.providers:
        hits.append((s, "G1", text[s:e]))
    has_g8 = False
    skills = bool(sig.job_skills.search(text))
    if ctx.is_job and not commentary and not skills:
        # G8 is a duty that uses AI; a sentence that only asks for AI skills or experience is not one (U5)
        for s, e, _ in sig.indicators["G8"].find(text):
            hits.append((s, "G8", text[s:e]))
            has_g8 = True
    for span in m3:
        hits.append((text.find(span), "M3", span))
    if trap:
        hits.append((text.find(trap), "M4", trap))
    codes = {c for _, c, _ in hits}
    features = bool(codes & {"G2", "G3", "G4", "G5", "G8", "G11"})
    limiting = bool(sig.limiting.search(text))
    governance = bool(sig.governance.search(text))
    building = bool(sig.building.search(text))
    temporal = _temporal(text, sig, commentary=commentary, job=ctx.is_job)
    vendor_use = (_names_vendor(text, names) or (ctx.first_party and bool(_FIRST_PERSON.search(text)))) and \
        bool(sig.deployed.search(text) or sig.past.search(text) or sig.present.search(text))
    locus = _locus(text, ctx, prev=prev, commentary=commentary, providers=providers, features=features,
                   vendor_use=vendor_use, heading=heading)
    service_texts = [_service_text(t, names) for t in (text, heading, title)]
    service_hit = _term_hit(names.service_rx, *service_texts)
    family_hit = _term_hit(names.family_rx, *service_texts)
    named_target = (not commentary) and (
        _names_product(text, names) or _names_product(heading, names) or _names_product(title, names)
        or service_hit or family_hit or bool(codes & {"G2", "G4", "G5", "G8"}) or ctx.is_job
        or (locus in ("platform_supplier", "affiliate_inferred") and (
            _names_supplier(f"{text} {title}", names) or _names_inferred_affiliate(f"{text} {title}", names)))
        or _names_product(prev, names)
        # a named function: the claim's own locus cues name the team or process (engineers, the NOC, the contact
        # centre) - "tied to a named product or function" (design 2.3, S2)
        or locus in ("sdlc", "delivery_ops", "corporate_internal"))
    if not named_target and not trap and view.terms:
        s0, e0, _ = view.terms[0][:3]
        hits.append((s0, "M6", text[s0:e0]))  # no product named: the span is the AI term itself
    indicators = _dedupe_indicators(hits)
    generic = temporal == "unclear" and not features and not providers and not limiting
    if generic and not trap and _own_operations(text, ctx, locus=locus, commentary=commentary):
        generic = False  # the vendor's own operations on a named artefact: U3 (weak), never a marketing claim
    if temporal == "planned" and not features and not providers:
        generic = True
    u = _u_class(trap=bool(trap), limiting=limiting, locus=locus, generic=generic, ctx=ctx, has_g8=has_g8,
                 skills=skills, governance=governance, building=building, providers=bool(providers),
                 service_hit=service_hit, temporal=temporal)
    sp = specificity(indicators, named_target=named_target, temporal=temporal)
    if ctx.is_job and sp == "S3":
        sp = "S2"  # postings describe intentions: specificity capped at S2
    rl = relevance_from_names(text, names, locus, title=title, heading=heading)
    rc = recency(_published(ctx), ctx.capture.retrieved_at, ctx.as_of, dated_by=ctx.source.dated_by)
    ic = _initial_ic(ctx.source.sr, sp, rc)
    action = _action_level(text, sig)
    ai_type = _ai_type(text, sig, trap=bool(trap))
    tags = dict(u_class=u, sr=ctx.source.sr, sp=sp, rl=rl, rc=rc, locus=locus)
    is_q = not trap and _q_tags(SignalTags(**tags, ic=ic, ai_type=ai_type, strength="Weak"))
    is_k = not trap and _k_tags(SignalTags(**tags, ic=ic, ai_type=ai_type, strength="Weak"))
    strength = strength_label(u, sp, rl, locus, is_q=is_q, is_k=is_k)
    if trap:
        strength = "Marketing only"
    data = []
    for s, e, _ in sig.data_terms.find(text):
        if text[s:e].lower() not in {d.lower() for d in data}:
            data.append(text[s:e])
    claim_kind = "not_ai" if trap else ("industry_commentary" if locus == "commentary" else
                                        "names_ai_provider" if locus == "relationship" and ctx.document.kind == "dns"
                                        else "ai_hiring" if ctx.is_job else _CLAIM_KIND[u])
    return _Tagged(start=start, end=end, excerpt=text, indicators=indicators, providers=providers,
                   data_mentioned=data, temporal=temporal, action_level=action, ai_type=ai_type, locus=locus,
                   u_class=u, sp=sp, rl=rl, rc=rc, ic=ic, strength=strength, trap=trap, named_target=named_target,
                   heading=heading, claim_kind=claim_kind, subject=_SUBJECT.get(locus, ""), ai_terms=ai_terms)


def relevance_from_names(claim_text: str, names: _Names, locus: Locus, *, title: str = "",
                         heading: str = "") -> RelevanceGrade:
    """``relevance`` with the seed names already compiled (same rules)."""
    if locus == "commentary":
        return "R0"
    if locus in ("platform_supplier", "affiliate_inferred"):
        return "R1"
    if locus == "relationship":
        return "R2"
    texts = [_service_text(t, names) for t in (claim_text, heading, title)]
    if locus in ("service_feature", "vendor_addon", "delivery_ops") and _term_hit(names.service_rx, *texts):
        return "R3"
    if _term_hit(names.family_rx, *texts) or locus in ("delivery_ops", "sdlc"):
        return "R2"
    return "R1"


def _published(ctx: _Ctx) -> str:
    return ctx.document.published if ctx.source.dated_by == "publication" else ""


def _item_dates(ctx: _Ctx) -> tuple[str, str]:
    """(published, date_basis) for the item: retrieval-dated items carry '' and 'retrieval'."""
    if ctx.source.dated_by == "publication" and ctx.document.published:
        return ctx.document.published, ctx.document.date_basis or "publication"
    return "", "retrieval"


def _rule_labels(t: _Tagged) -> dict[str, str]:
    labels = {"temporal": t.temporal, "action_level": t.action_level, "sp": t.sp, "rl": t.rl, "locus": t.locus,
              "u_class": t.u_class, "ai_type": t.ai_type, "claim_kind": t.claim_kind}
    if t.subject:
        labels["subject"] = t.subject
    if t.providers:
        labels["providers"] = "; ".join(t.providers)
    return labels


def _build_item(t: _Tagged, ctx: _Ctx, *, passage_id: str, **extra: Any) -> EvidenceItem:
    published, basis = _item_dates(ctx)
    data: dict[str, Any] = dict(
        vendor_id=ctx.profile.vendor_id, passage_id=passage_id, doc_id=ctx.document.doc_id,
        capture_id=ctx.capture.capture_id, family=ctx.source.family, source_type=ctx.source.source_type,
        publisher=ctx.source.publisher, title=ctx.document.title or "", url=ctx.document.url,
        url_final=ctx.capture.url_final or ctx.capture.url_requested or ctx.document.url, published=published,
        date_basis=basis, retrieved_at=ctx.capture.retrieved_at, excerpt=t.excerpt, start=t.start, end=t.end,
        tags=SignalTags(u_class=t.u_class, sr=ctx.source.sr, sp=t.sp, rl=t.rl, rc=t.rc, ic=t.ic, locus=t.locus,
                        ai_type=t.ai_type, strength=t.strength),
        indicators=t.indicators, providers=t.providers, data_mentioned=t.data_mentioned, temporal=t.temporal,
        action_level=t.action_level, method="rule", rule_labels=_rule_labels(t), role="Logged",
    )
    data.update(extra)
    return EvidenceItem.model_validate(data)


_EMPTY_NAMES = _Names(
    vendor_id="", short="", aliases=(), legal=(), vendor_names=(), domains=(), cik="", service_terms=(),
    family_terms=(), persons=(), collisions=(), collision_ciks=(), affiliates=(), suppliers=(), ats_hosts=(),
    vendor_rx=None, collision_rx=None, person_rx=None, product_rx=None, service_rx=None, family_rx=None,
    supplier_rx=None, affiliate_rx=None, inferred_affiliate_rx=None)


# =========================================================================== building items


_DOC_CACHE: dict[tuple, tuple[SourceInfo, _Entity]] = {}
_DOC_CACHE_MAX = 512


def _doc_facts(document: Document, capture: Capture, seeds: dict, profile: VendorProfile, names: _Names,
               doc_text: str, sources: SourceRegister | None) -> tuple[SourceInfo, _Entity]:
    """Source classification and entity guard for one document, computed once per document and text."""
    reg = sources or load_sources()
    key = (reg.sha256, json.dumps([_allowed_seeds(seeds), profile.vendor_id, profile.name, profile.domain],
                                  sort_keys=True, default=str),
           document.model_dump_json(), capture.capture_id, capture.url_final, capture.url_requested,
           capture.family, len(doc_text), hash(doc_text))
    hit = _DOC_CACHE.get(key)
    if hit is None:
        hit = (classify_source(document, capture, seeds, profile, sources=reg),
               _entity(document, capture, names, doc_text))
        if len(_DOC_CACHE) >= _DOC_CACHE_MAX:
            _DOC_CACHE.pop(next(iter(_DOC_CACHE)))
        _DOC_CACHE[key] = hit
    return hit


def _context(document: Document, capture: Capture, seeds: dict, profile: VendorProfile, plan: DepthPlan | None, *,
             as_of: str, doc_text: str, signals: Signals | None, sources: SourceRegister | None) -> _Ctx:
    sig = signals or load_signals()
    names = _names(seeds, profile)
    source, ent = _doc_facts(document, capture, seeds, profile, names, doc_text, sources)
    md3p = bool(plan and any(str(m).upper() == "M-D3P" for m in plan.modifiers))
    return _Ctx(document=document, capture=capture, seeds=seeds, profile=profile, plan=plan, as_of=as_of, sig=sig,
                names=names, source=source, entity=ent, doc_text=doc_text, md3p=md3p)


def _log(log: list[str] | None, ref: str, reason: str) -> None:
    if log is not None:
        log.append(f"rules {ref}: {reason}")


def _fit_excerpt(text: str, segs: Sequence[tuple[int, int]], i: int, anchor: tuple[int, int]) -> tuple[int, int]:
    """The smallest run of whole segments around segment i that is 25-600 characters (local offsets); an
    over-long segment is cut at clause boundaries, then word boundaries, around the AI term."""
    s, e = segs[i]
    lo, hi = i, i
    while e - s < MIN_EXCERPT and (hi + 1 < len(segs) or lo > 0):
        if hi + 1 < len(segs) and segs[hi + 1][1] - s <= MAX_EXCERPT:
            hi += 1
            e = segs[hi][1]
        elif lo > 0 and e - segs[lo - 1][0] <= MAX_EXCERPT:
            lo -= 1
            s = segs[lo][0]
        else:
            break
    if e - s <= MAX_EXCERPT:
        return s, e
    # one over-long unit (a run-on sentence or an unpunctuated PDF block): keep the longest run of whole clauses
    # around the AI term that fits, else a window of whole words
    a0, a1 = anchor
    bounds = sorted({s, e, *(s + m.end() for m in _CLAUSE_END.finditer(text[s:e]))})
    best: tuple[int, int] | None = None
    for x in bounds:
        if x > a0:
            break
        for y in bounds:
            if y <= x or y < a1:
                continue
            if y - x > MAX_EXCERPT:
                break
            cand = _trim(text, x, y)
            if cand[1] - cand[0] >= MIN_EXCERPT and (best is None or cand[1] - cand[0] > best[1] - best[0]):
                best = cand
    if best is not None:
        return best
    x = max(s, min(a0, a1 - MAX_EXCERPT // 2))
    while x > s and not text[x - 1].isspace() and x < a0:
        x += 1
    y = min(e, x + MAX_EXCERPT)
    while y > a1 and y < e and not text[y].isspace():
        y -= 1
    return _trim(text, x, y)


_CLAUSE_END = re.compile(r"[;:]\s+|,\s+|\s[–—-]\s|\n")


def tag_passage(passage: Passage, document: Document, capture: Capture, seeds: dict, profile: VendorProfile,
                plan: DepthPlan, *, as_of: str, doc_text: str | None = None, signals: Signals | None = None,
                sources: SourceRegister | None = None, log: list[str] | None = None) -> EvidenceItem | None:
    """One rule item for a passage, or None (the reason goes to ``log``).

    Steps (contract section 3): entity guard; AI terms after the provider guards and suppressors; the subject
    test; the excerpt (the smallest run of whole sentences that holds the claim, 25-600 characters, an exact
    slice); the definition test (a failing claim is kept as a visible, never citable trap); then source, indicators,
    use state, action level, AI type, locus, U class, SP (job postings capped at S2), RL, RC, IC and strength.
    When the passage holds several AI claims, the best one is kept (Negative, Strong, Moderate first, then
    relevance and specificity) *(interpretation: one item per passage)*.
    """
    ref = passage.passage_id
    if passage.vendor_id != profile.vendor_id or document.doc_id != passage.doc_id:
        raise ValueError("passage, document and profile must belong together")
    if doc_text is None:
        # without the document text the passage stands in for it (offsets stay document offsets)
        text = " " * passage.start + passage.text
    else:
        text = doc_text
        if text[passage.start:passage.end] != passage.text:
            raise ValueError(f"passage {ref} is not the slice doc_text[{passage.start}:{passage.end}]")
    sig = signals or load_signals()
    url = _doc_url(document, capture)
    if any(rx.search(urlsplit(url).path or "") for rx in sig.skip_url):
        _log(log, ref, "document never carries a claim (llms.txt)")
        return None
    ctx = _context(document, capture, seeds, profile, plan, as_of=as_of, doc_text=text, signals=sig,
                   sources=sources)
    if not ctx.entity.ok:
        _log(log, ref, f"entity guard: {ctx.entity.reason}")
        return None
    ptext = passage.text
    view = _ai_view(ptext, sig, ctx.names, doc_text=text)
    if not view.terms:
        _log(log, ref, "no AI term survives the provider guards and suppressors")
        return None
    segs = _segments(ptext)
    candidates: list[_Tagged] = []
    subject_failed = False
    shapes_seen: set[str] = set()
    for i, (s, e) in enumerate(segs):
        terms = [t for t in view.terms if s <= t[0] < e]
        if not terms:
            continue
        shape = _shape(ptext[s:e])
        shapes_seen.add(shape)
        if shape != "prose":
            continue
        if not _subject_ok(ptext[s:e], ctx):
            subject_failed = True
            continue
        ls, le = _fit_excerpt(ptext, segs, i, (terms[0][0], terms[0][1]))
        prev = ptext[segs[i - 1][0]:segs[i - 1][1]] if i > 0 else ""
        candidates.append(_analyse(passage.start + ls, passage.start + le, ctx, prev=prev, ai_terms=len(terms)))
    if not candidates:
        if subject_failed:
            _log(log, ref, "subject test failed: the claim does not name the vendor, an alias or a product")
        else:
            _log(log, ref, "no claim sentence: the AI terms are in navigation, headings or code "
                           f"({', '.join(sorted(shapes_seen)) or 'none'})")
        return None
    best = min(candidates, key=lambda t: t.rank())
    if not MIN_EXCERPT <= len(best.excerpt) <= MAX_EXCERPT:
        _log(log, ref, f"excerpt of {len(best.excerpt)} characters is outside {MIN_EXCERPT}-{MAX_EXCERPT}")
        return None
    return _build_item(best, ctx, passage_id=passage.passage_id)


_AI_TOKEN_LINE = re.compile(r"^AI_TOKEN\t([^\t\n]+)\t([^\n]+?)\s*$", re.M)


def tag_document(document: Document, doc_text: str, capture: Capture, seeds: dict, profile: VendorProfile,
                 plan: DepthPlan, *, as_of: str, signals: Signals | None = None,
                 sources: SourceRegister | None = None, log: list[str] | None = None) -> list[EvidenceItem]:
    """Items for documents that have no lexicon passages: one per ``AI_TOKEN\\t{provider}\\t{record}`` line of a
    DNS document (U4, SR B, S1, R2, RC by retrieval, IC 2, locus relationship, "Context - relationship only").
    The excerpt is the record itself, located on its TXT line when present. Other kinds return []."""
    if document.kind != "dns":
        return []
    ctx = _context(document, capture, seeds, profile, plan, as_of=as_of, doc_text=doc_text, signals=signals,
                   sources=sources)
    if not ctx.entity.ok:
        _log(log, document.doc_id[:12], f"entity guard: {ctx.entity.reason}")
        return []
    items: list[EvidenceItem] = []
    seen: set[tuple[str, str]] = set()
    for m in _AI_TOKEN_LINE.finditer(doc_text):
        provider, record = m.group(1).strip(), m.group(2)
        if not record or (provider, record) in seen:
            continue
        seen.add((provider, record))
        txt = re.search(r"^TXT\t" + re.escape(record) + r"\s*$", doc_text, re.M)
        start = txt.start() + 4 if txt else m.start(2)
        end = start + len(record)
        span = re.search(re.escape(provider.split()[0]), record, re.I)
        indicators = [_ind("G1", span.group(0))] if span else []
        rc = recency("", capture.retrieved_at, as_of, dated_by="retrieval")
        tags = SignalTags(u_class="U4", sr=ctx.source.sr, sp="S1", rl="R2", rc=rc,
                          ic=_initial_ic(ctx.source.sr, "S1", rc, dns_token=True), locus="relationship",
                          ai_type="unspecified", strength="Context - relationship only")
        labels = {"temporal": "unclear", "action_level": "unknown", "sp": "S1", "rl": "R2", "locus": "relationship",
                  "u_class": "U4", "ai_type": "unspecified", "claim_kind": "names_ai_provider",
                  "providers": provider}
        items.append(EvidenceItem.model_validate(dict(
            vendor_id=profile.vendor_id, passage_id="", doc_id=document.doc_id, capture_id=capture.capture_id,
            family=SourceFamily.DNS, source_type=ctx.source.source_type, publisher=ctx.names.short,
            title=document.title or "", url=document.url,
            url_final=capture.url_final or capture.url_requested or document.url, published="",
            date_basis="retrieval", retrieved_at=capture.retrieved_at, excerpt=record, start=start, end=end,
            tags=tags, indicators=indicators, providers=[provider], temporal="unclear", action_level="unknown",
            method="rule", rule_labels=labels, role="Logged")))
    if not items:
        _log(log, document.doc_id[:12], "DNS document has no AI verification token")
    return items


# =========================================================================== LLM claims


def _guarded_provider_ok(name: str, context: str, sig: Signals) -> bool:
    """A provider name proposed by the LLM must pass the same guard as a rule match (e.g. 'Claude' needs
    Anthropic or a model name in the excerpt or the title)."""
    for rule in sig.providers:
        m = rule.rx.fullmatch(name.strip())
        if m:
            return rule.guard is None or bool(rule.guard.search(context))
    return True


def claim_item(claim: Claim, result: VerifyResult, passage: Passage, document: Document, capture: Capture,
               seeds: dict, profile: VendorProfile, plan: DepthPlan, *, as_of: str, doc_text: str, llm_model: str,
               prompt_sha256: str, signals: Signals | None = None,
               sources: SourceRegister | None = None) -> EvidenceItem:
    """An item over a verified LLM claim (``result.ok``), tagged by the same rules as tag_passage.

    RL and locus are computed locally (V7); temporal, action level and SP come from the rules (V8) and Gemini's
    labels go to ``llm_labels``, with the SP its surviving indicators would give. The item is a proposal
    (method ``llm_proposed_accepted``) until merge_claims folds it into an overlapping rule item or an analyst
    accepts it. A claim that fails the definition test, or that the model itself calls ``not_ai``, is a visible
    trap.
    """
    if not result.ok or result.start is None or result.end is None:
        raise ValueError("claim_item needs a verified claim (VerifyResult.ok with its located excerpt)")
    if doc_text[result.start:result.end] != result.excerpt:
        raise ValueError("result.excerpt must be the document slice doc_text[start:end]")
    sig = signals or load_signals()
    ctx = _context(document, capture, seeds, profile, plan, as_of=as_of, doc_text=doc_text, signals=sig,
                   sources=sources)
    lo = max(0, result.start - 400)
    segs = _segments(doc_text[lo:result.start])
    prev = doc_text[lo + segs[-1][0]:lo + segs[-1][1]] if segs else ""
    t = _analyse(result.start, result.end, ctx, prev=prev, force_commentary=not _subject_ok(result.excerpt, ctx))
    if claim.claim_kind == "not_ai" and not t.trap:
        # the model itself says this is not AI: keep it as a visible trap, never citable
        cue = re.search(r"(?i)\b(?:intelligent|smart|automat\w*|rules?[- ]based|rules? engines?|schedul\w*|"
                        r"templat\w*|barcod\w*|dashboards?|RPA)\b", t.excerpt)
        span = cue.group(0) if cue else t.excerpt[:60].strip()
        t.trap, t.ai_type, t.u_class, t.strength, t.claim_kind = span, "not_ai", "U7", "Marketing only", "not_ai"
        t.indicators = _dedupe_indicators([(t.excerpt.find(i.span), i.code, i.span) for i in t.indicators]
                                          + [(t.excerpt.find(span), "M4", span)])
    title = document.title or ""
    context = f"{result.excerpt}\n{title}"
    indicators = list(t.indicators)
    for ind in result.indicators:
        if ind.code in ("G6", "G7", "G9", "G10"):
            continue
        if ind.span in result.excerpt and (ind.code, ind.span) not in {(i.code, i.span) for i in indicators}:
            indicators.append(ind)
    providers = list(t.providers)
    for p in result.providers:
        if p.lower() not in {x.lower() for x in providers} and _guarded_provider_ok(p, context, sig):
            providers.append(p)
    data = list(t.data_mentioned)
    for d in claim.data_mentioned:
        if d and d.lower() in result.excerpt.lower() and d.lower() not in {x.lower() for x in data}:
            data.append(d)
    llm_sp = specificity(result.indicators, named_target=t.named_target, temporal=claim.temporal)
    llm_labels = {"claim_kind": claim.claim_kind, "subject": claim.subject, "ai_type": claim.ai_type,
                  "temporal": claim.temporal, "action_level": claim.action_level,
                  "providers": "; ".join(result.providers), "sp": llm_sp}
    if t.trap:
        strength: Strength = "Marketing only"
    else:
        strength = strength_label(t.u_class, t.sp, t.rl, t.locus, is_q=False, is_k=False)  # a proposal is not citable
    t.indicators, t.providers, t.data_mentioned, t.strength = _sort_indicators(indicators, t.excerpt), providers, \
        data, strength
    return _build_item(t, ctx, passage_id=passage.passage_id, method="llm_proposed_accepted", llm_model=llm_model,
                       prompt_sha256=prompt_sha256, llm_labels=llm_labels)


def _sort_indicators(indicators: Sequence[Indicator], excerpt: str) -> list[Indicator]:
    return _dedupe_indicators([(max(0, excerpt.find(i.span)), i.code, i.span) for i in indicators])


def _overlap(a: EvidenceItem, b: EvidenceItem) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def _word_in(name: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I))


def merge_claims(rule_items: Sequence[EvidenceItem], claim_items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    """Fold each claim item into the best overlapping rule item of the same document (STRENGTH_ORDER, then the
    longer overlap, then item_key). The rule item gains the claim's llm_labels, model and prompt hash and those of
    its providers found in the rule excerpt or title; its method becomes ``rule+llm_agree`` when the V8 labels
    agree and stays ``rule`` otherwise. Unmatched claim items stay proposals. Output is sorted by
    (doc_id, start, end, item_key)."""
    by_doc: dict[str, list[EvidenceItem]] = {}
    for it in rule_items:
        by_doc.setdefault(it.doc_id, []).append(it)
    assigned: dict[str, list[tuple[int, EvidenceItem]]] = {}
    leftovers: list[EvidenceItem] = []
    for c in sorted(claim_items, key=lambda i: (i.doc_id, i.start, i.end, i.item_key)):
        overlapping = [r for r in by_doc.get(c.doc_id, []) if _overlap(r, c) > 0]
        if not overlapping:
            leftovers.append(c)
            continue
        target = min(overlapping, key=lambda r: (STRENGTH_ORDER.index(r.strength), -_overlap(r, c), r.item_key))
        assigned.setdefault(target.item_key, []).append((_overlap(target, c), c))
    merged: list[EvidenceItem] = []
    for r in rule_items:
        claims = assigned.get(r.item_key)
        if not claims:
            merged.append(r)
            continue
        _, lead = min(claims, key=lambda oc: (-oc[0], oc[1].item_key))
        providers = list(r.providers)
        for _, c in claims:
            for p in c.providers:
                if p.lower() not in {x.lower() for x in providers} and (_word_in(p, r.excerpt) or _word_in(p, r.title)):
                    providers.append(p)
        upd = r.model_copy(update={"llm_labels": dict(lead.llm_labels), "llm_model": lead.llm_model,
                                   "prompt_sha256": lead.prompt_sha256, "providers": providers})
        method = "rule+llm_agree" if not upd.label_disagreements else "rule"
        merged.append(upd.model_copy(update={"method": method}))
    return sorted([*merged, *leftovers], key=lambda i: (i.doc_id, i.start, i.end, i.item_key))


# =========================================================================== reviews


_LABEL_VALUES: dict[str, tuple[str, ...]] = {
    "temporal": ("in_production", "pilot_or_beta", "planned", "unclear"),
    "action_level": ("none", "advisory", "human_reviewed_decision", "automated_action", "unknown"),
}


def apply_reviews(items: Sequence[EvidenceItem], store: OverrideStore) -> list[EvidenceItem]:
    """Apply the latest HC2 ``evidence_review`` record of each item (keyed by item_key): status, reason and
    reviewer ("{analyst} {date}"); ``extra["labels"]`` overrides temporal, action_level, sp, rl, locus, u_class or
    ai_type, sets method ``adjudicated`` and retags. An accepted proposal is retagged too (it becomes citable).
    Items without a record are unchanged; order is preserved."""
    latest: dict[tuple[str, str], Any] = {}
    if store is not None:
        for rec in store.records("evidence_review"):
            latest[(rec.vendor_id, rec.key)] = rec
    out: list[EvidenceItem] = []
    for it in items:
        rec = latest.get((it.vendor_id, it.item_key))
        if rec is None:
            out.append(it)
            continue
        value = rec.value.strip().lower()
        if value not in ("accepted", "rejected"):
            raise ValueError(f"evidence_review for {it.item_key[:12]} has value {rec.value!r}; "
                             "expected 'accepted' or 'rejected'")
        new = it.model_copy(update={"review_status": value, "review_reason": rec.reason,
                                    "reviewer": f"{rec.analyst} {rec.date}"})
        labels = (rec.extra or {}).get("labels") or {}
        if labels:
            unknown = set(labels) - set(REVIEW_LABEL_KEYS)
            if unknown:
                raise ValueError(f"evidence_review labels may only set {', '.join(REVIEW_LABEL_KEYS)}; "
                                 f"got {sorted(unknown)}")
            tag_upd = {k: labels[k] for k in ("sp", "rl", "locus", "u_class", "ai_type") if k in labels}
            try:
                tags = SignalTags.model_validate({**new.tags.model_dump(), **tag_upd})
            except ValidationError as exc:
                raise ValueError(f"evidence_review labels for {it.item_key[:12]} are not valid tags: {exc}") from exc
            fields: dict[str, Any] = {}
            for k in ("temporal", "action_level"):
                if k in labels:
                    if labels[k] not in _LABEL_VALUES[k]:
                        raise ValueError(f"evidence_review label {k}={labels[k]!r} is not one of "
                                         f"{', '.join(_LABEL_VALUES[k])}")
                    fields[k] = labels[k]
            new = retag(new.model_copy(update={"tags": tags, **fields, "method": "adjudicated"}))
        elif value == "accepted" and it.proposed:
            new = retag(new)
        out.append(new)
    return out


def retag(item: EvidenceItem) -> EvidenceItem:
    """Recompute ``strength`` and the initial IC from the current tags. IC 1 and 5 come from cross-item
    corroboration (cluster.link_corroboration) and are kept *(interpretation)*. A definition-test trap stays
    'Marketing only'."""
    tags = item.tags
    if tags.ai_type == "not_ai":
        strength: Strength = "Marketing only"
    else:
        strength = strength_label(tags.u_class, tags.sp, tags.rl, tags.locus,
                                  is_q=item.citable and _q_tags(tags), is_k=item.citable and _k_tags(tags))
    dns_token = item.family == SourceFamily.DNS and tags.u_class == "U4" and tags.locus == "relationship"
    ic = tags.ic if tags.ic in (1, 5) else _initial_ic(tags.sr, tags.sp, tags.rc, dns_token=dns_token)
    return item.model_copy(update={"tags": tags.model_copy(update={"strength": strength, "ic": ic})})


__all__ = [
    "DEFAULT_LEXICON",
    "DEFAULT_SOURCES",
    "MAX_EXCERPT",
    "MIN_EXCERPT",
    "Signals",
    "SourceInfo",
    "SourceRegister",
    "apply_reviews",
    "claim_item",
    "classify_source",
    "entity_ok",
    "find_indicators",
    "indicator_ok",
    "is_corroborating",
    "is_qualifying",
    "load_signals",
    "load_sources",
    "merge_claims",
    "recency",
    "relevance",
    "retag",
    "specificity",
    "strength_label",
    "tag_document",
    "tag_passage",
    "vendor_origin_types",
]
