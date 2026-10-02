"""Criticality rubric v1.1 (Outcome 04): profile fields only, deterministic, reproducible by another analyst.

Each factor gets a level 0-4 from the ordered regex anchors in `config/rubric.toml`: the highest level whose
anchor matches wins and the exact matched profile text is kept as the trigger. O, D and P are read from
anchors, V from customer and record counts, and R last because it also uses D and V.
Score = 7 O + 6 D + 5 P + 4 R + 3 V (0-100). The tier is the higher of the score tier and every floor that fires.
One-step sensitivity and +/-1 weight perturbation are recomputed by brute force. `render_rationale` writes
column M in plain language: factor codes and floor ids (F1-F6) stay in the Criticality Workings sheet.
"""

from __future__ import annotations

import functools
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from footprint.models import (
    CriticalityResult,
    FactorCode,
    FactorScore,
    Tier,
    TierOverride,
    VendorProfile,
    max_tier,
)

FACTORS: tuple[str, ...] = ("O", "D", "P", "R", "V")
DEFAULT_RUBRIC_PATH = Path(__file__).resolve().parents[2] / "config" / "rubric.toml"
RATIONALE_MAX_CHARS = 1050
NO_ANCHOR_NOTE = "no anchor matched - analyst to confirm (HC1)"
EMPTY_FIELD_NOTE = "field empty - analyst to confirm (HC1)"
R_RULES = frozenset({"npi_at_scale", "privileged_access", "npi_below_scale", "confidential_data"})


# --------------------------------------------------------------------------- rubric (config/rubric.toml)


@functools.lru_cache(maxsize=None)
def _regex(pattern: str) -> re.Pattern[str]:
    """Compile an anchor pattern: case-insensitive; (?-i:...) inside a pattern keeps an acronym case-sensitive."""
    return re.compile(pattern, re.IGNORECASE)


def _check_patterns(patterns: Sequence[str]) -> None:
    for pattern in patterns:
        try:
            _regex(pattern)
        except re.error as exc:
            raise ValueError(f"bad pattern {pattern!r}: {exc}") from exc


class Anchor(BaseModel):
    """One anchor: the level it sets and the regexes (or, for R, computed rules) that trigger it."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="e.g. 'O3', 'D3-P'")
    level: int = Field(ge=0, le=4)
    description: str
    patterns: tuple[str, ...] = ()
    rules: tuple[str, ...] = Field(default=(), description="computed R rules, see _r_rule")
    label: str = Field(default="", description="overrides the factor's level label (D3-P)")
    lower_phrase: str = Field(default="", description="sensitivity wording when this anchor moves one level down")

    @model_validator(mode="after")
    def _valid(self) -> Anchor:
        _check_patterns(self.patterns)
        unknown = set(self.rules) - R_RULES
        if unknown:
            raise ValueError(f"anchor {self.id}: unknown rules {sorted(unknown)}")
        return self


class VolumeCounts(BaseModel):
    """How V reads counts: the nouns that make a number a customer or record count, and the level bands."""

    model_config = ConfigDict(frozen=True)

    customer_nouns: tuple[str, ...]
    record_nouns: tuple[str, ...]
    customer_thresholds: tuple[int, int, int, int] = Field(description="minimum customers for levels 1-4")
    record_thresholds: tuple[int, int, int, int] = Field(description="minimum records a year for levels 1-4")
    max_gap_words: int = Field(default=3, ge=0, description="words allowed between the number and the noun")
    frequency_words: tuple[str, ...] = Field(default=(), description="a gap with one of these is not a volume")
    entire_base_patterns: tuple[str, ...] = Field(default=(), description="phrases meaning the entire base (level 4)")
    zero_patterns: tuple[str, ...] = Field(default=(), description="'Not applicable', 'No direct customer data'")

    @model_validator(mode="after")
    def _valid(self) -> VolumeCounts:
        _check_patterns(self.entire_base_patterns + self.zero_patterns)
        return self


class FactorSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(description="plain-language name, e.g. 'operational dependency'")
    fields: tuple[str, ...] = Field(description="VendorProfile fields read, in search order")
    default_level: int = Field(ge=0, le=4, description="level when no anchor matches (note HC1)")
    empty_level: int | None = Field(default=None, ge=0, le=4, description="level when every field is empty")
    labels: tuple[str, str, str, str, str] = Field(description="level label by level 0-4")
    raise_phrases: tuple[str, str, str, str, str] = Field(description="'if ...' wording for a move up to level n")
    lower_phrases: tuple[str, str, str, str, str] = Field(description="'if ...' wording for a move down to level n")
    anchors: tuple[Anchor, ...]
    counts: VolumeCounts | None = Field(default=None, description="factor V only")

    def anchor(self, anchor_id: str) -> Anchor | None:
        return next((a for a in self.anchors if a.id == anchor_id), None)

    def ranked_anchors(self) -> list[Anchor]:
        """Anchors from level 4 down; the file order is kept within a level (D3 before D3-P)."""
        return sorted(self.anchors, key=lambda a: -a.level)


class FloorCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    factor: FactorCode
    min: int | None = Field(default=None, ge=0, le=4)
    anchor: str | None = None

    def holds(self, levels: Mapping[str, int], anchors: Mapping[str, str]) -> bool:
        if self.min is not None and levels[self.factor] < self.min:
            return False
        return self.anchor is None or anchors.get(self.factor) == self.anchor


class Floor(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    tier: Tier
    name: str = Field(description="e.g. 'payment-path floor'")
    description: str
    reason: str = Field(description="rationale wording; {O} {D} {P} {R} {V} become level labels")
    conditions: tuple[FloorCondition, ...]

    def fires(self, levels: Mapping[str, int], anchors: Mapping[str, str]) -> bool:
        return all(c.holds(levels, anchors) for c in self.conditions)


class Rubric(BaseModel):
    """The whole rubric file. Frozen, so the default instance can be cached."""

    model_config = ConfigDict(frozen=True)

    version: str
    score_tiers: dict[Tier, int] = Field(description="minimum score for Critical, High and Medium")
    weights: dict[str, int]
    factors: dict[str, FactorSpec]
    floors: tuple[Floor, ...]

    @model_validator(mode="after")
    def _valid(self) -> Rubric:
        if set(self.weights) != set(FACTORS) or set(self.factors) != set(FACTORS):
            raise ValueError(f"weights and factors must cover exactly {FACTORS}")
        if 4 * sum(self.weights.values()) != 100:
            raise ValueError("weights must sum to 25 so that the score runs 0-100")
        if set(self.score_tiers) != {Tier.CRITICAL, Tier.HIGH, Tier.MEDIUM}:
            raise ValueError("score_tiers needs Critical, High and Medium thresholds")
        if self.factors["V"].counts is None:
            raise ValueError("factor V needs a [factors.V.counts] table")
        return self

    def score_tier(self, score: int) -> Tier:
        for tier in (Tier.CRITICAL, Tier.HIGH, Tier.MEDIUM):
            if score >= self.score_tiers[tier]:
                return tier
        return Tier.LOW

    def floor(self, floor_id: str) -> Floor:
        for floor in self.floors:
            if floor.id == floor_id:
                return floor
        raise ValueError(f"unknown floor {floor_id!r}")


def load_rubric(path: Path | None = None) -> Rubric:
    """Read and validate a rubric file (default: config/rubric.toml next to the package, whatever the cwd)."""
    with open(path or DEFAULT_RUBRIC_PATH, "rb") as handle:
        return Rubric.model_validate(tomllib.load(handle))


@functools.lru_cache(maxsize=1)
def _default_rubric() -> Rubric:
    return load_rubric()


def factor_label(code: str) -> str:
    """Plain-language factor name, e.g. 'O' -> 'operational dependency'."""
    if code not in FACTORS:
        raise ValueError(f"unknown factor {code!r}")
    return _default_rubric().factors[code].name


def floor_description(floor_id: str) -> str:
    """One-sentence description of a floor, e.g. 'F2' -> 'Payment-flow involvement 3 or more ...'."""
    return _default_rubric().floor(floor_id).description


# --------------------------------------------------------------------------- anchor matching

_CLAUSE_END = re.compile(r"[.;:!?](?=\s|$)")
_NEGATED = re.compile(r"\b(?:no|not|without|never|nor|excluding|excludes)\b(?:\W+\w+){0,3}\W*$", re.IGNORECASE)
_IN_TRANSIT = re.compile(r"\bin\s+transit\b", re.IGNORECASE)
_MULTIPLIERS = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9}
_PER_YEAR = {"day": 365, "week": 52, "month": 12, "quarter": 4, "year": 1}  # '30,000 wires per month' = 360,000


@dataclass(frozen=True)
class _Hit:
    """An anchor that matched: its level, id, the matched profile text and an optional note."""

    level: int
    anchor: str
    trigger: str
    note: str = ""


def _search(pattern: str, text: str) -> re.Match[str] | None:
    """First match that is not negated in its own clause ('No direct customer data' is not customer data)."""
    for match in _regex(pattern).finditer(text):
        clause_start = max((end.end() for end in _CLAUSE_END.finditer(text, 0, match.start())), default=0)
        if not _NEGATED.search(text, clause_start, match.start()):
            return match
    return None


def _first_hit(level: int, anchor_id: str, patterns: Sequence[str], texts: Sequence[str]) -> _Hit | None:
    """Patterns in file order, then fields in `fields` order: the first match is the trigger."""
    for pattern in patterns:
        for text in texts:
            match = _search(pattern, text)
            if match:
                return _Hit(level, anchor_id, match.group(0).strip())
    return None


def _anchor_hits(spec: FactorSpec, texts: Sequence[str]) -> list[_Hit]:
    hits = (_first_hit(a.level, a.id, a.patterns, texts) for a in spec.ranked_anchors())
    return [hit for hit in hits if hit]


def _texts(profile: VendorProfile, spec: FactorSpec) -> list[str]:
    return [str(getattr(profile, field) or "") for field in spec.fields]


def _resolve(code: str, spec: FactorSpec, weight: int, hits: Sequence[_Hit], texts: Sequence[str],
             notes: Sequence[str] = ()) -> FactorScore:
    """Highest level wins (the first hit on a tie); log adjacent fits; fall back to the documented default."""
    found = list(notes)
    if hits:
        best = max(hits, key=lambda h: h.level)
        level, anchor, trigger = best.level, best.anchor, best.trigger
        if best.note:
            found.insert(0, best.note)
        if any(h.level == level - 1 for h in hits):
            found.append(f"levels {level} and {level - 1} both fit; higher level taken")
    elif spec.empty_level is not None and not any(t.strip() for t in texts):
        level, anchor, trigger = spec.empty_level, f"{code}{spec.empty_level}", ""
        found.insert(0, EMPTY_FIELD_NOTE)
    else:
        level, anchor, trigger = spec.default_level, f"{code}{spec.default_level}", ""
        found.insert(0, NO_ANCHOR_NOTE)
    return FactorScore(factor=code, level=level, anchor=anchor, trigger=trigger, source_fields=list(spec.fields),
                       weight=weight, points=weight * level, note="; ".join(found))


# --------------------------------------------------------------------------- V counts


@dataclass(frozen=True)
class _Volume:
    customers: _Hit | None
    records: _Hit | None
    zero: _Hit | None


def _count_regex(nouns: Sequence[str], max_gap: int) -> re.Pattern[str]:
    """A number ('38,000', '1.4 million', '2.1m') followed within `max_gap` plain words by one of `nouns`,
    optionally followed by a period ('per month', 'a day') that annualises the count."""
    return re.compile(
        r"(?<![\w.,])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
        r"(?:\s*(?P<mult>thousand|million|billion|mn|bn|k|m)\b)?"
        rf"\s+(?P<gap>(?:[A-Za-z][\w'-]*\s+){{0,{max_gap}}}?)"
        rf"(?:{'|'.join(re.escape(n) for n in nouns)})\b"
        rf"(?:\s+(?:a|an|per|each|every)\s+(?P<per>{'|'.join(_PER_YEAR)})\b)?",
        re.IGNORECASE,
    )


def _level_for(count: float, thresholds: Sequence[int]) -> int:
    return sum(count >= t for t in thresholds)


def _best_count(counts: VolumeCounts, nouns: Sequence[str], thresholds: Sequence[int],
                texts: Sequence[str], extra: Sequence[_Hit] = ()) -> _Hit | None:
    """The highest-level count in reading order (first on a tie); a frequency word in the gap disqualifies."""
    regex = _count_regex(nouns, counts.max_gap_words)
    frequency = {w.lower() for w in counts.frequency_words}
    hits: list[_Hit] = []
    for text in texts:
        for match in regex.finditer(text):
            if frequency & set(match.group("gap").lower().split()):
                continue
            multiplier = _MULTIPLIERS.get((match.group("mult") or "").lower(), 1)
            per_year = _PER_YEAR[match.group("per").lower()] if match.group("per") else 1
            level = _level_for(float(match.group("num").replace(",", "")) * multiplier * per_year, thresholds)
            if level:
                hits.append(_Hit(level, f"V{level}", match.group(0).strip()))
    hits.extend(extra)
    return max(hits, key=lambda h: h.level) if hits else None


def _volume(spec: FactorSpec, texts: Sequence[str]) -> _Volume:
    counts = spec.counts
    assert counts is not None  # guaranteed by Rubric validation
    entire = _first_hit(4, "V4", counts.entire_base_patterns, texts)
    customers = _best_count(counts, counts.customer_nouns, counts.customer_thresholds, texts,
                            [entire] if entire else [])
    records = _best_count(counts, counts.record_nouns, counts.record_thresholds, texts)
    return _Volume(customers, records, _first_hit(0, "V0", counts.zero_patterns, texts))


def _volume_hits(volume: _Volume) -> list[_Hit]:
    """Customers and records both count; with no customer count, 'Not applicable' / 'no customer data' gives 0."""
    if volume.customers:
        return [h for h in (volume.customers, volume.records) if h]
    if volume.zero:
        return [volume.zero]
    return [volume.records] if volume.records else []


# --------------------------------------------------------------------------- R (computed after D and V)


def _r_rule(rule: str, level: int, anchor_id: str, d: FactorScore, customers: _Hit | None) -> _Hit | None:
    customer_npi = d.level >= 3 and d.anchor != "D3-P"
    at_scale = customers is not None and customers.level >= 2  # 10,000 or more customers, or the entire base
    if rule == "npi_at_scale" and customer_npi and at_scale and customers is not None:
        return _Hit(level, anchor_id, customers.trigger, "customer non-public data for 10,000 or more customers")
    if rule == "privileged_access" and d.anchor == "D3-P":
        return _Hit(level, anchor_id, d.trigger, "privileged production access without customer non-public data")
    if rule == "npi_below_scale" and customer_npi and not at_scale:
        return _Hit(level, anchor_id, d.trigger,
                    "customer non-public data without 10,000 or more stated customers - analyst to confirm (HC1)")
    if rule == "confidential_data" and d.level == 2:
        return _Hit(level, anchor_id, d.trigger, "confidential data only")
    return None


def _r_hits(spec: FactorSpec, texts: Sequence[str], d: FactorScore, customers: _Hit | None) -> list[_Hit]:
    hits: list[_Hit] = []
    for anchor in spec.ranked_anchors():
        hit = None
        for rule in anchor.rules:  # computed rules first, then the anchor's own patterns
            hit = hit or _r_rule(rule, anchor.level, anchor.id, d, customers)
        hit = hit or _first_hit(anchor.level, anchor.id, anchor.patterns, texts)
        if hit:
            hits.append(hit)
    return hits


def _score_factors(profile: VendorProfile, rubric: Rubric) -> dict[str, FactorScore]:
    specs, weights = rubric.factors, rubric.weights
    scores: dict[str, FactorScore] = {}
    for code in ("O", "D", "P"):
        texts = _texts(profile, specs[code])
        in_transit = code == "D" and any(_IN_TRANSIT.search(t) for t in texts)
        notes = ["'in transit' does not lower data sensitivity"] if in_transit else []
        scores[code] = _resolve(code, specs[code], weights[code], _anchor_hits(specs[code], texts), texts, notes)
    v_texts = _texts(profile, specs["V"])
    volume = _volume(specs["V"], v_texts)
    v_score = _resolve("V", specs["V"], weights["V"], _volume_hits(volume), v_texts)
    r_texts = _texts(profile, specs["R"])
    r_hits = _r_hits(specs["R"], r_texts, scores["D"], volume.customers)
    scores["R"] = _resolve("R", specs["R"], weights["R"], r_hits, r_texts)
    scores["V"] = v_score
    return scores


# --------------------------------------------------------------------------- score, floors, tier


@dataclass(frozen=True)
class _Outcome:
    score: int
    score_tier: Tier
    floors: tuple[str, ...]
    tier: Tier


def _evaluate(levels: Mapping[str, int], anchors: Mapping[str, str], rubric: Rubric,
              weights: Mapping[str, int] | None = None) -> _Outcome:
    weights = weights or rubric.weights
    score = sum(weights[code] * levels[code] for code in FACTORS)
    score_tier = rubric.score_tier(score)
    floors = tuple(f.id for f in rubric.floors if f.fires(levels, anchors))
    return _Outcome(score, score_tier, floors, max_tier(score_tier, *(rubric.floor(f).tier for f in floors)))


def _perturbation_stable(levels: Mapping[str, int], anchors: Mapping[str, str], rubric: Rubric, tier: Tier) -> bool:
    """True when the tier survives every single weight changed by +1 or -1 (10 variants)."""
    for code in FACTORS:
        for delta in (1, -1):
            weights = {**rubric.weights, code: rubric.weights[code] + delta}
            if _evaluate(levels, anchors, rubric, weights).tier != tier:
                return False
    return True


# --------------------------------------------------------------------------- one-step sensitivity


@dataclass(frozen=True)
class _Change:
    """One factor moved one level: the 'if ...' wording, which floors it adds or removes, and the outcome."""

    step: int
    phrase: str
    outcome: _Outcome
    annotation: str


def _join(items: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _join_conditions(items: Sequence[str]) -> str:
    """'if a', 'if a or if b', 'if a, if b or if c'."""
    parts = [f"if {item}" for item in items]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " or " + parts[-1]


def _floor_names(floors: Sequence[Floor]) -> str:
    """Plain floor names without ids: 'payment-path floor', 'operational-dependency and payment-path floors'."""
    if len(floors) == 1:
        return floors[0].name
    return _join([f.name.removesuffix(" floor") for f in floors]) + " floors"


def _annotation(rubric: Rubric, base: _Outcome, new: _Outcome) -> str:
    """Why the tier moved: floors newly reached at the new tier, floors lost that held the old tier, or the score."""
    if new.tier.rank > base.tier.rank:
        added = [rubric.floor(f) for f in new.floors if f not in base.floors and rubric.floor(f).tier == new.tier]
        if added:
            return _floor_names(added)
    elif new.tier.rank < base.tier.rank:
        lost = [rubric.floor(f) for f in base.floors if f not in new.floors and rubric.floor(f).tier == base.tier]
        if lost:
            return f"{_floor_names(lost)} no longer {'applies' if len(lost) == 1 else 'apply'}"
    return f"score {new.score}"


def _changes(levels: Mapping[str, int], anchors: Mapping[str, str], rubric: Rubric, base: _Outcome) -> list[_Change]:
    """Every factor moved one level up and one level down (clamped 0-4), in factor order.

    Moving D off D3-P loses the privileged-access anchor: down gives D2 (floor F4 no longer applies), up gives D4.
    """
    changes: list[_Change] = []
    for code in FACTORS:
        spec = rubric.factors[code]
        for step in (1, -1):
            new_level = levels[code] + step
            if not 0 <= new_level <= 4:
                continue
            current = spec.anchor(anchors[code])
            if step < 0 and current is not None and current.lower_phrase:
                phrase = current.lower_phrase
            else:
                phrase = (spec.raise_phrases if step > 0 else spec.lower_phrases)[new_level]
            outcome = _evaluate({**levels, code: new_level}, {**anchors, code: f"{code}{new_level}"}, rubric)
            changes.append(_Change(step, phrase, outcome, _annotation(rubric, base, outcome)))
    return changes


def _tier_statement(verb: str, tier: Tier, changes: Sequence[_Change]) -> str:
    """'Would become Critical if a (payment-path floor) or if b (population-scale floor).'

    A shared annotation is given once, at the end.
    """
    annotations = {c.annotation for c in changes}
    if len(annotations) == 1:
        return f"{verb} {tier.value} {_join_conditions([c.phrase for c in changes])} ({annotations.pop()})."
    return f"{verb} {tier.value} {_join_conditions([f'{c.phrase} ({c.annotation})' for c in changes])}."


def _sensitivity(levels: Mapping[str, int], anchors: Mapping[str, str], rubric: Rubric, base: _Outcome) -> list[str]:
    """Plain-language statements of every single one-step change that moves the tier, then what stays put."""
    changes = _changes(levels, anchors, rubric, base)
    statements: list[str] = []
    for verb, moved in (("Would become", lambda t: t.rank > base.tier.rank),
                        ("Would drop to", lambda t: t.rank < base.tier.rank)):
        tiers = sorted({c.outcome.tier for c in changes if moved(c.outcome.tier)}, key=lambda t: -t.rank)
        statements += [_tier_statement(verb, t, [c for c in changes if c.outcome.tier == t]) for t in tiers]
    directions = []
    ups, downs = [c for c in changes if c.step > 0], [c for c in changes if c.step < 0]
    if base.tier != Tier.CRITICAL and ups and all(c.outcome.tier == base.tier for c in ups):
        directions.append("increase")
    if base.tier != Tier.LOW and downs and all(c.outcome.tier == base.tier for c in downs):
        directions.append("decrease")
    if directions:
        statements.append(f"Stays {base.tier.value} for any single one-step {' or '.join(directions)}.")
    return statements


# --------------------------------------------------------------------------- public scoring entry point


def score_profile(profile: VendorProfile, rubric: Rubric | None = None,
                  override: TierOverride | None = None) -> CriticalityResult:
    """Score one vendor profile with the rubric (default: config/rubric.toml); an override sets the final tier."""
    rubric = rubric or _default_rubric()
    if override is not None and override.vendor_id != profile.vendor_id:
        raise ValueError(f"override is for {override.vendor_id}, not {profile.vendor_id}")
    factors = _score_factors(profile, rubric)
    levels = {code: fs.level for code, fs in factors.items()}
    anchors = {code: fs.anchor for code, fs in factors.items()}
    outcome = _evaluate(levels, anchors, rubric)
    return CriticalityResult(
        vendor_id=profile.vendor_id,
        rubric_version=rubric.version,
        factors=factors,
        score=outcome.score,
        score_tier=outcome.score_tier,
        floors_fired=list(outcome.floors),
        computed_tier=outcome.tier,
        tier=override.tier if override is not None else outcome.tier,
        sensitivity=_sensitivity(levels, anchors, rubric, outcome),
        perturbation_stable=_perturbation_stable(levels, anchors, rubric, outcome.tier),
        override=override,
    )


# --------------------------------------------------------------------------- column M rationale

_P_CLAUSES = ("it has no stated payment role", "it reports on activity after the fact",
              "it influences payments without being in the payment path", "it sits in the payment message path",
              "it executes or settles funds")
_R_CLAUSES = ("it carries no stated regulatory exposure", "it carries low regulatory exposure",
              "it carries moderate regulatory exposure", "it carries high regulatory exposure",
              "the service is itself a regulated activity")
_LEGAL_SUFFIX = re.compile(r",?\s+(?:Inc|L\.?L\.?C|Ltd|Limited|Corp|Corporation|plc|LLP)\.?$", re.IGNORECASE)


def _short_name(name: str) -> str:
    """'Financial Statement Services, Inc. (FSSI)' -> 'FSSI'; 'Fiserv, Inc.' -> 'Fiserv'."""
    acronym = re.search(r"\(([A-Z][A-Z0-9&]{1,7})\)", name)
    if acronym:
        return acronym.group(1)
    if "—" in name and name.upper().startswith("EXAMPLE"):
        name = name.split("—", 1)[1]
    name = re.sub(r"\s*\([^)]*\)", "", name).strip(" ,")
    return _LEGAL_SUFFIX.sub("", name).strip(" ,") or "The vendor"


def _lower_first(text: str) -> str:
    """Lower-case a leading capitalised word ('Payment instructions'), never an acronym ('ACH', 'RTP')."""
    word = text.split(" ", 1)[0]
    return text[0].lower() + text[1:] if len(word) > 1 and word[1:].islower() else text


def _split_items(sentence: str) -> list[str]:
    """Split a list sentence on commas outside parentheses; drop a leading 'and ' from each item."""
    items, depth, current = [], 0, ""
    for char in sentence:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += char
    items.append(current)
    return [re.sub(r"^and\s+", "", item.strip()) for item in items if item.strip()]


def _shorten(sentence: str, limit: int, keep: str = "") -> str:
    """Shorten a list sentence to about `limit` characters by whole items, keeping (or adding) `keep`."""
    if len(sentence) <= limit:
        return sentence
    budget = limit - (len(keep) + 5 if keep else 0)
    kept: list[str] = []
    for item in _split_items(sentence):
        if len(_join(kept + [item])) > budget:
            break
        kept.append(item)
    if keep and not any(keep in item for item in kept):
        kept.append(keep)
    return _join(kept) if kept else _clip(sentence, limit)


def _data_phrase(text: str, trigger: str, limit: int) -> str:
    """The data sentence that holds the trigger, in the profile's words, shortened to `limit` by list items."""
    sentences = [s.strip(" .;") for s in re.split(r"(?<=[.;])\s+", text.strip()) if s.strip(" .;")]
    if not sentences:
        return ""
    sentence = next((s for s in sentences if trigger and trigger in s), sentences[0])
    return _lower_first(_shorten(sentence, limit, trigger))


def _level_label(rubric: Rubric, fs: FactorScore) -> str:
    spec = rubric.factors[fs.factor]
    anchor = spec.anchor(fs.anchor)
    return anchor.label if anchor is not None and anchor.label else spec.labels[fs.level]


def _driver_clause(code: str, profile: VendorProfile, result: CriticalityResult, rubric: Rubric, limit: int,
                   process: str) -> str:
    """One driver in the profile's words; a trigger already named in the business process is not repeated."""
    fs = result.factors[code]
    quoted = f' ("{fs.trigger}")' if fs.trigger and fs.trigger.lower() not in process.lower() else ""
    if code == "O":
        return f'an outage would disrupt that process (dependency rated "{fs.trigger or _level_label(rubric, fs)}")'
    if code == "D":
        verb = "holds" if fs.anchor == "D3-P" else "handles"
        return f"it {verb} {_data_phrase(profile.data_accessed, fs.trigger, limit) or 'no stated data'}"
    if code == "P":
        return _P_CLAUSES[fs.level] + quoted
    if code == "R":
        return _R_CLAUSES[fs.level] + quoted
    return f"it covers {fs.trigger}" if fs.level and fs.trigger else "no customer-data volume is stated"


def _opening(profile: VendorProfile, result: CriticalityResult, rubric: Rubric, limit: int) -> str:
    """Name, business process, and the two factors with the most points, rendered in factor order."""
    process = _shorten(profile.business_process.strip().rstrip("."), 2 * limit)
    process = _lower_first(process) or "its contracted service"
    ranked = sorted(FACTORS, key=lambda c: (-result.factors[c].points, -result.factors[c].weight))
    clauses = [_driver_clause(c, profile, result, rubric, limit, process) for c in FACTORS if c in ranked[:2]]
    return f"{_short_name(profile.name)} supports {process}; {clauses[0]}, and {clauses[1]}."


def _factor_phrase(rubric: Rubric, fs: FactorScore, with_trigger: bool) -> str:
    detail = f', "{fs.trigger}"' if with_trigger and fs.trigger else ""
    return f"{rubric.factors[fs.factor].name} {_level_label(rubric, fs)} ({fs.level} of 4{detail})"


def _score_sentence(result: CriticalityResult, rubric: Rubric) -> str:
    """'Score 79 of 100; raised to Critical by the payment-path floor (in the payment ...).' or '... on score alone'."""
    labels = {code: _level_label(rubric, fs) for code, fs in result.factors.items()}
    at_tier = [rubric.floor(f) for f in result.floors_fired if rubric.floor(f).tier == result.computed_tier]
    if result.computed_tier.rank > result.score_tier.rank:
        reasons = [f"the {f.name} ({f.reason.format(**labels)})" for f in at_tier]
        return f"Score {result.score} of 100; raised to {result.computed_tier.value} by {_join(reasons)}."
    text = f"Score {result.score} of 100, {result.computed_tier.value} on score alone"
    if at_tier:
        text += f" and by the {_floor_names(at_tier)}"
    return text + "."


def _sensitivity_text(statements: Sequence[str]) -> str:
    """Join the statements into one sentence: 'Would become ... (payment-path floor); stays High for ...'."""
    parts = [s.rstrip(".") for s in statements]
    return "; ".join([parts[0]] + [p[0].lower() + p[1:] for p in parts[1:]]) + "." if parts else ""


def render_rationale(profile: VendorProfile, result: CriticalityResult, rubric: Rubric | None = None) -> str:
    """Column M: tier, an opening in the profile's words, the workings, score, sensitivity (<= 1,050 chars).

    Deterministic. When the text is too long the data phrase is shortened, then quoted triggers are dropped
    from the workings; as a last resort the override reason, then the body, is cut at a word boundary, so
    "Analyst override to {tier}:" always survives.
    """
    rubric = rubric or _default_rubric()
    prefix, reason = "", ""
    if result.override is not None:
        prefix = f" Analyst override to {result.override.tier.value}: "
        reason = result.override.reason.strip()
        reason += "" if reason.endswith(".") else "."
    body = ""
    for limit, with_triggers in ((110, True), (70, True), (70, False)):
        workings = "; ".join(_factor_phrase(rubric, result.factors[c], with_triggers) for c in FACTORS)
        body = (f"{result.tier.value}. {_opening(profile, result, rubric, limit)} "
                f"How the tier was reached (rubric v{rubric.version}, profile only): {workings}. "
                f"{_score_sentence(result, rubric)} Sensitivity: {_sensitivity_text(result.sensitivity)}")
        if len(body) + len(prefix) + len(reason) <= RATIONALE_MAX_CHARS:
            return body + prefix + reason
    if not prefix:
        return _clip(body, RATIONALE_MAX_CHARS)
    min_reason = 60
    body = _clip(body, RATIONALE_MAX_CHARS - len(prefix) - min(len(reason), min_reason))
    return body + prefix + _clip(reason, RATIONALE_MAX_CHARS - len(body) - len(prefix))


def _clip(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` characters at a word boundary, marking the cut with '...'."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


# --------------------------------------------------------------------------- Method & Legend sheet


def rubric_table(rubric: Rubric | None = None) -> list[dict[str, str | int]]:
    """One row per anchor (factor order, level 4 down) for the Method & Legend sheet."""
    rubric = rubric or _default_rubric()
    rows: list[dict[str, str | int]] = []
    for code in FACTORS:
        spec = rubric.factors[code]
        for anchor in spec.ranked_anchors():
            rows.append({
                "factor": code,
                "factor_name": spec.name,
                "weight": rubric.weights[code],
                "level": anchor.level,
                "anchor": anchor.id,
                "label": anchor.label or spec.labels[anchor.level],
                "description": anchor.description,
                "patterns": " | ".join(anchor.patterns),
                "rules": ", ".join(anchor.rules),
                "source_fields": ", ".join(spec.fields),
            })
    return rows
