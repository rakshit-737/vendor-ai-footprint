"""AI risk engine (C9): the inputs and ordered steps behind columns S (class) and T (rationale).

Design Appendix A 2.8; docs/contracts_p3.md section 8. ARP = 2E + 2K + TP + TG, range 0-18:
- E, the exposure of Meridian data to AI (0-3), is the maximum over the Q and K pathways, by where the AI sits.
  E3 always needs an R3 item. An R2 item that would give E3 counts as E2 and carries ``e_if_confirmed=3``.
- K, the decision impact (0-3), comes from the pathways' action levels. A pathway in the service, its delivery or
  an unknown place whose action level is unknown takes the role's K (assumed), so K = max(evidenced, assumed) and
  adding an item never lowers K.
- TP is the tier points. TG counts the transparency checks t1-t6 that no citable first-party disclosure closes.
- The unknowns rule assumes E from data sensitivity and K from the vendor's role, for an Inconclusive verdict and
  for any input a Yes verdict leaves unknown. Assumed inputs never meet the materiality gate.

The order of adjustments is fixed, and every step is logged: score, base class, materiality gate, escalator floors
X1-X6 (Confirmed or Probable only, gate met), then the verdict cap, always last. The ceiling reruns the first four
steps with assumed inputs treated as evidenced. The flip condition names the single confirmation or change that
would move the class. Only citable items count. No clock is read: X5 is dated against ``as_of``.
Policy, lexicons and wording live in config/risk.toml; the anchor logic per locus is coded here.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from footprint.models import (
    CONTEXT_STRENGTHS,
    GAP_KEYS,
    RISK_CLASS_ORDER,
    AiType,
    CriticalityResult,
    DepthPlan,
    EvidenceItem,
    GapKey,
    RiskClass,
    RiskInputs,
    RiskResult,
    SourceFamily,
    Tier,
    UsageVerdict,
    VendorProfile,
    VerdictCap,
    arp_class,
    tg_for_missing,
)
from footprint.review import OverrideStore
from footprint.rules import is_corroborating, is_qualifying
from footprint.verdict import rank_key

__all__ = [
    "DEFAULT_CONFIG_PATH", "ESCALATOR_CODES", "GAP_KEYS", "THEME_CODES", "RiskConfig", "assess", "calibrate",
    "calibration_inputs", "calibration_verdict", "derive_inputs", "evaluate_escalators",
    "foundation_model_providers", "legend", "load_risk_config", "provider_names", "score",
]

DEFAULT_CONFIG_PATH: Path = Path(__file__).resolve().parents[2] / "config" / "risk.toml"

ESCALATOR_CODES: tuple[str, ...] = ("X1", "X2", "X3", "X4", "X5", "X6")
THEME_CODES: tuple[str, ...] = ("RT1", "RT2", "RT3", "RT4", "RT5", "RT6", "RT7")
DESIGN_CAPS: dict[str, str] = {
    "Confirmed": "", "Probable": "High", "Inconclusive": "Medium", "Affirmed negative": "None identified",
    "Not detected": "None identified",
}
"""The verdict caps of design 2.8 step 5. The models depend on them (Provisional = the Medium cap)."""
YES_LABELS: frozenset[str] = frozenset({"Confirmed", "Probable"})
NO_LABELS: frozenset[str] = frozenset({"Affirmed negative", "Not detected"})

OVERRIDE_KIND = "risk_input_override"
OVERRIDE_KEYS: tuple[str, ...] = ("e", "k", *GAP_KEYS)
D3P_ANCHOR = "D3-P"          # rubric anchor: privileged production access or service credentials
X5_WINDOW_MONTHS = 24
SERVICE_LOCI: frozenset[str] = frozenset({"service_feature", "vendor_addon"})
DELIVERY_LOCI: frozenset[str] = frozenset({"delivery_ops"})
THIRD_PARTY_LOCI: frozenset[str] = frozenset({"platform_supplier", "affiliate_inferred", "commentary"})
"""Loci whose statements are not the vendor's own disclosure: they never close a transparency check."""
K_ASSUMED_LOCI: frozenset[str] = SERVICE_LOCI | DELIVERY_LOCI | {"unknown"}
"""Where a pathway with an unknown action level takes the role's K: the AI may act on the service Meridian receives.
AI-assisted development, corporate tools and bare provider relationships are not assumed to act on it."""
FIRST_PARTY_SR: frozenset[str] = frozenset({"A", "B", "C"})
"""Source reliability a first-party disclosure may have (C: the vendor's own press releases, blogs, whitepapers)."""

CUE_KEYS: tuple[str, ...] = (
    "customer_data", "artefacts", "production_data", "processing", "k3_targets", "data_use", "human_oversight",
    "governance",
    "notification", "training", "no_training", "production", "privileged", "credit_holds", "incident",
    "hypothetical", "ai_terms", "boilerplate", "vendor_release",
)
REASON_KEYS: tuple[str, ...] = (
    "no_pathway_e", "no_pathway_k", "e_assumed_inconclusive", "e_assumed_unknown", "k_assumed_inconclusive",
    "k_assumed_unknown", "k_assumed_partial", "service_e3", "service_model", "service_capped", "service_e2",
    "service_e1",
    "delivery_e3", "delivery_capped", "delivery_e2", "delivery_e1", "sdlc_e2", "sdlc_e1", "corporate", "k3",
    "k2_reviewed", "k2_operational", "k1", "k0", "gap", "override",
)
DATA_KEYS: tuple[str, ...] = ("0", "1", "2", "3", "3-P", "4")
ROLE_KEYS: tuple[str, ...] = ("payment_path", "privileged", "pay", "customer_facing", "other")
FLIP_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "none_identified": (), "none_identified_platforms": (), "provisional": ("subject", "ceiling"),
    "probable_cap": ("cls",), "probable_confirm": ("e", "cls"), "raise_e": ("e3", "cls"),
    "raise_k": ("k3", "cls"), "raise_both": ("e3", "k3", "cls"), "lower_e": ("cls",), "lower_k": ("cls",),
    "probable_raise": ("what", "cls"), "what_e": ("e3",), "what_k": ("k3",), "what_or": (),
}


# --------------------------------------------------------------------------- config (config/risk.toml)


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GateSpec(_Spec):
    """Materiality gate: High or above needs evidenced E >= min_e or K >= min_k, else it is lowered."""

    min_e: int = Field(ge=0, le=3)
    min_k: int = Field(ge=0, le=3)
    lowered_to: RiskClass


class UnknownsSpec(_Spec):
    pay_level: int = Field(ge=0, le=4, description="payment-flow level whose outputs count as pay outputs (K 2)")
    customer_facing: tuple[str, ...] = Field(description="profile cues for customer-facing outputs (K 2)")


class WordingSpec(_Spec):
    data: dict[str, str] = Field(description="data sensitivity in words, by rubric level ('3-P' = D3-P)")
    role: dict[str, str] = Field(description="the vendor's role in words, for an assumed decision impact")


class EscalatorSpec(_Spec):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ThemeSpec(_Spec):
    name: str = Field(min_length=1)
    nist: str = Field(min_length=1, description="NIST AI 600-1 risk categories")
    owasp: str = Field(min_length=1, description="OWASP Top 10 for LLM applications entries")
    ai_types: tuple[AiType, ...] = ()


class FlipSpec(_Spec):
    none_identified: str
    none_identified_platforms: str
    provisional: str
    probable_cap: str
    probable_confirm: str
    raise_e: str
    raise_k: str
    raise_both: str
    lower_e: str
    lower_k: str
    probable_raise: str
    what_e: str
    what_k: str
    what_or: str
    k3: str
    e3: dict[str, str]
    subjects: dict[str, str]
    subjects_unnamed: dict[str, str] = Field(default_factory=dict)


class CalibrationSpec(_Spec):
    vendor_id: str
    tier: Tier
    e: int = Field(ge=0, le=3)
    k: int = Field(ge=0, le=3)
    gaps: dict[GapKey, bool]
    e_reason: str
    k_reason: str
    expected_arp: int = Field(ge=0, le=18)
    expected_class: RiskClass


class RiskConfig(_Spec):
    """config/risk.toml after validation: bands, gate and caps agree with the models, every lexicon compiles."""

    version: str = Field(min_length=1)
    tg_points: tuple[int, ...]
    floor_class: RiskClass
    bands: dict[str, int]
    gate: GateSpec
    caps: dict[str, VerdictCap]
    unknowns: UnknownsSpec
    wording: WordingSpec
    reasons: dict[str, str]
    cues: dict[str, tuple[str, ...]]
    gaps: dict[GapKey, str] = Field(description="plain name of each transparency check")
    escalators: dict[str, EscalatorSpec]
    themes: dict[str, ThemeSpec]
    foundation_models: dict[str, tuple[str, ...]]
    flip: FlipSpec
    calibration: CalibrationSpec

    @model_validator(mode="after")
    def _complete(self) -> RiskConfig:
        problems = _config_problems(self)
        if problems:
            raise ValueError("risk policy is invalid: " + "; ".join(problems))
        return self


def _config_problems(cfg: RiskConfig) -> list[str]:
    problems: list[str] = []
    if set(cfg.bands) != set(RISK_CLASS_ORDER):
        problems.append(f"bands must be {', '.join(RISK_CLASS_ORDER)}")
    elif any(_band_of(arp, cfg.bands) != arp_class(arp) for arp in range(19)):
        problems.append("bands must agree with footprint.models.arp_class (Critical 14, High 10, Medium 6, Low 0)")
    expected_tg = tuple(tg_for_missing(n) for n in range(len(GAP_KEYS) + 1))
    if cfg.tg_points != expected_tg:
        problems.append(f"tg_points must be {list(expected_tg)} (footprint.models.tg_for_missing)")
    if dict(cfg.caps) != DESIGN_CAPS:
        problems.append("caps must be the design's verdict caps (Probable High, Inconclusive Medium, No verdicts "
                        "None identified, Confirmed none)")
    if tuple(cfg.escalators) != ESCALATOR_CODES:
        problems.append("escalators must be X1..X6, in order")
    if tuple(cfg.themes) != THEME_CODES:
        problems.append("themes must be RT1..RT7, in order")
    if set(cfg.gaps) != set(GAP_KEYS):
        problems.append("gaps must name the six checks t1..t6")
    for label, keys, have in (("cues", CUE_KEYS, cfg.cues), ("reasons", REASON_KEYS, cfg.reasons),
                              ("wording.data", DATA_KEYS, cfg.wording.data),
                              ("wording.role", ROLE_KEYS, cfg.wording.role)):
        if missing := [k for k in keys if k not in have]:
            problems.append(f"{label} lacks {', '.join(missing)}")
    patterns = [*(p for pats in cfg.cues.values() for p in pats), *cfg.unknowns.customer_facing]
    for pattern in patterns:
        try:
            _rx(pattern)
        except re.error as exc:
            problems.append(f"bad pattern {pattern!r}: {exc}")
    for name, needed in FLIP_PLACEHOLDERS.items():
        template = getattr(cfg.flip, name)
        if missing := [p for p in needed if "{" + p + "}" not in template]:
            problems.append(f"flip.{name} lacks {', '.join('{' + p + '}' for p in missing)}")
        try:
            template.format(subject="", ceiling="", cls="", e="", e3="", k3="", what="")
        except (KeyError, IndexError, ValueError) as exc:
            problems.append(f"flip.{name} is not a valid template: {exc}")
    if missing := [k for k in ("service", "delivery") if k not in cfg.flip.e3]:
        problems.append(f"flip.e3 lacks {', '.join(missing)}")
    if "default" not in cfg.flip.subjects:
        problems.append("flip.subjects needs a default")
    for label, template in cfg.flip.subjects.items():
        if "{name}" in template and label not in cfg.flip.subjects_unnamed:
            problems.append(f"flip.subjects_unnamed needs {label!r} for when no name is known")
    if empty := [name for name, aliases in cfg.foundation_models.items() if not aliases]:
        problems.append(f"foundation_models without aliases: {', '.join(empty)}")
    cal = cfg.calibration
    try:
        inputs = RiskInputs(e=cal.e, k=cal.k, tp=cal.tier.points, gaps=dict(cal.gaps))
    except ValidationError as exc:
        problems.append(f"calibration inputs are invalid: {exc.errors()[0]['msg']}")
    else:
        arp = 2 * inputs.e + 2 * inputs.k + inputs.tp + inputs.tg
        if arp != cal.expected_arp or arp_class(arp) != cal.expected_class:
            problems.append(f"calibration scores {arp} ({arp_class(arp)}), not {cal.expected_arp} "
                            f"({cal.expected_class})")
    return problems


def _band_of(arp: int, bands: Mapping[str, int]) -> str:
    """The class whose band holds this ARP: the highest class whose lowest score it reaches."""
    reached = [cls for cls in RISK_CLASS_ORDER if arp >= bands[cls]]
    return reached[-1] if reached else RISK_CLASS_ORDER[0]


@functools.lru_cache(maxsize=None)
def _load(path: str) -> RiskConfig:
    with open(path, "rb") as fh:
        return RiskConfig.model_validate(tomllib.load(fh))


def load_risk_config(path: str | Path | None = None) -> RiskConfig:
    """Read and validate the risk policy (default: config/risk.toml next to the package). Cached per path."""
    return _load(str(Path(path or DEFAULT_CONFIG_PATH).resolve()))


# --------------------------------------------------------------------------- small helpers


@functools.lru_cache(maxsize=None)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _hits(patterns: Iterable[str], text: str) -> bool:
    return any(_rx(p).search(text) for p in patterns)


def _cue(cfg: RiskConfig, key: str, text: str) -> bool:
    return _hits(cfg.cues[key], text)


def _claim_text(item: EvidenceItem) -> str:
    """What the cue lexicons read: the excerpt plus the data the claim mentions."""
    return "\n".join([item.excerpt, *item.data_mentioned])


def _has(item: EvidenceItem, code: str) -> bool:
    return any(ind.code == code for ind in item.indicators)


def _rank(cls: str) -> int:
    return RISK_CLASS_ORDER.index(cls)


def _citable(items: Iterable[EvidenceItem], vendor_id: str | None) -> list[EvidenceItem]:
    """The citable items, best first in the verdict's order (verdict.rank_key), so E-IDs are cited alike."""
    return sorted((i for i in items if i.citable and (vendor_id is None or i.vendor_id == vendor_id)),
                  key=rank_key)


def _sentences(text: str) -> list[str]:
    """The sentences (and clauses after a semicolon) of a claim text, one per line of data mentioned."""
    return [s for s in re.split(r"(?<=[.!?;])\s+|\n+", text) if s.strip()]


def _join(names: Sequence[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _pathways(verdict: UsageVerdict, items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    """The citable Q and K items (rules.is_qualifying / is_corroborating, plus any the verdict lists), best first."""
    listed = set(verdict.qualifying) | set(verdict.corroborating)
    return [i for i in items if is_qualifying(i) or is_corroborating(i) or (i.citable and i.item_key in listed)]


def _data_sensitivity(criticality: CriticalityResult) -> tuple[int, bool]:
    factor = criticality.factors.get("D")
    if factor is None:
        raise ValueError(f"{criticality.vendor_id}: the criticality result has no data-sensitivity factor (D)")
    return factor.level, factor.anchor == D3P_ANCHOR


def _exposure_from_data(level: int, privileged: bool) -> int:
    """Unknowns rule: D4 or D3-P gives 3, D3 gives 2, anything else 1 (also E for AI in the exact service)."""
    return 3 if level >= 4 or privileged else 2 if level == 3 else 1


def _data_words(level: int, privileged: bool, cfg: RiskConfig) -> str:
    return cfg.wording.data["3-P" if privileged else str(max(0, min(level, 4)))]


def _role(criticality: CriticalityResult, profile: VendorProfile, cfg: RiskConfig) -> tuple[int, str]:
    """Unknowns rule: payment-flow 3+ or D3-P gives 3, pay or customer-facing outputs give 2, else 1."""
    payment = criticality.factors["P"].level if "P" in criticality.factors else 0
    _, privileged = _data_sensitivity(criticality)
    if payment >= 3:
        return 3, "payment_path"
    if privileged:
        return 3, "privileged"
    if payment >= cfg.unknowns.pay_level:
        return 2, "pay"
    if _hits(cfg.unknowns.customer_facing, "\n".join([profile.service, profile.business_process, profile.category])):
        return 2, "customer_facing"
    return 1, "other"


# --------------------------------------------------------------------------- providers


def _models_key(cfg: RiskConfig) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((name, tuple(aliases)) for name, aliases in cfg.foundation_models.items())


@functools.lru_cache(maxsize=None)
def _alias_patterns(models: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    pairs = sorted(((name, alias) for name, aliases in models for alias in aliases),
                   key=lambda pair: (-len(pair[1]), pair[1]))
    return tuple((name, re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])", re.IGNORECASE)) for name, alias in pairs)


def _canonical(name: str, cfg: RiskConfig) -> str:
    """'ChatGPT' -> 'OpenAI', 'Claude 3.5 Sonnet' -> 'Anthropic'; any other name as written (whitespace tidied)."""
    text = " ".join(name.split())
    for canonical, pattern in _alias_patterns(_models_key(cfg)):
        if pattern.search(text):
            return canonical
    return text


def provider_names(items: Iterable[EvidenceItem], *, config: RiskConfig | None = None) -> list[str]:
    """Canonical provider names across the citable items, sorted case-insensitively."""
    cfg = config or load_risk_config()
    names: dict[str, str] = {}
    for item in items:
        if item.citable:
            for raw in item.providers:
                if name := _canonical(raw, cfg):
                    key = name.casefold()
                    names[key] = min(names.get(key, name), name)
    return [names[k] for k in sorted(names)]


def foundation_model_providers(items: Iterable[EvidenceItem], *, config: RiskConfig | None = None) -> list[str]:
    """The canonical foundation-model providers (config [foundation_models]) named by the citable items."""
    cfg = config or load_risk_config()
    return [n for n in provider_names(items, config=cfg) if n in cfg.foundation_models]


def _external(provider: str, profile: VendorProfile, cfg: RiskConfig) -> bool:
    """A provider that is not the vendor itself: a known model provider, or a name unlike the vendor's own."""
    if _canonical(provider, cfg) in cfg.foundation_models:
        return True
    compact = re.sub(r"[^0-9a-z]", "", provider.casefold())
    label = re.sub(r"[^0-9a-z]", "", profile.domain.split(".")[0].casefold()) if profile.domain else ""
    if len(label) >= 3 and label in compact:
        return False
    return not (compact and compact in re.sub(r"[^0-9a-z]", "", profile.name.casefold()))


def _vendor_voice(item: EvidenceItem) -> bool:
    """The vendor's own words: not a DNS token, a third party's page, a third-party locus or a limiting statement."""
    t = item.tags
    return (t.u_class != "U8" and t.locus not in THIRD_PARTY_LOCI
            and item.family not in (SourceFamily.DNS, SourceFamily.IND))


def _names_its_providers(item: EvidenceItem) -> bool:
    """t2: the vendor itself names AI providers (not a DNS token, a third party, or a limiting statement)."""
    return bool(item.providers) and _vendor_voice(item)


def _makers_in(text: str, cfg: RiskConfig) -> set[str]:
    """The foundation-model makers whose names (config [foundation_models]) appear in the text."""
    return {canonical for canonical, pattern in _alias_patterns(_models_key(cfg)) if pattern.search(text)}


# --------------------------------------------------------------------------- the vendor's name


_LEGAL_SUFFIX = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|company|co|plc|lp|l\.p|n\.a|ag|sa|gmbh)\b\.?",
    re.IGNORECASE)


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def _name_keys(profile: VendorProfile) -> tuple[str, ...]:
    """Compact spellings of the vendor: its name without legal suffixes, an acronym in parentheses, its domain label.

    'The Clearing House Payments Company L.L.C.' -> 'theclearinghousepayments' and (theclearinghouse.org)
    'theclearinghouse'; 'Financial Statement Services, Inc. (FSSI)' -> 'financialstatementservices', 'fssi'.
    """
    keys = {_compact(acronym) for acronym in re.findall(r"\(([^)]*)\)", profile.name)}
    base = re.sub(r"\([^)]*\)", " ", profile.name).split(",")[0]
    keys.add(_compact(_LEGAL_SUFFIX.sub(" ", base)))
    if profile.domain:
        keys.add(_compact(profile.domain.split(".")[0]))
    return tuple(sorted(k for k in keys if len(k) >= 3))


def _name_at(text: str, start: int, key: str) -> int | None:
    """Where the vendor's compact spelling ends when it is spelled from text[start] on (spaces and punctuation
    between letters ignored, a whole word only); None when it is not."""
    pos, matched = start, 0
    while matched < len(key):
        if pos >= len(text):
            return None
        char = text[pos].casefold()
        if char.isalnum():
            if char != key[matched]:
                return None
            matched += 1
        pos += 1
    return None if pos < len(text) and text[pos].isalnum() else pos


def _opens_with_vendor(text: str, profile: VendorProfile) -> bool:
    """The text opens with the vendor's name as its subject ('BNY Collaborates with ...', not "BNY's ...")."""
    start = next((n for n, char in enumerate(text) if char.isalnum()), None)
    if start is None:
        return False
    for key in _name_keys(profile):
        end = _name_at(text, start, key)
        if end is not None and not re.match(r"\s*['’]s\b", text[end:]):
            return True
    return False


def _names_vendor(item: EvidenceItem, profile: VendorProfile) -> bool:
    """The excerpt or the title names the vendor (design 2.3 subject test for a third party's page)."""
    keys = _name_keys(profile)
    for text in (item.excerpt, item.title):
        for n, char in enumerate(text):
            if char.isalnum() and (n == 0 or not text[n - 1].isalnum()):
                if any(_name_at(text, n, key) is not None for key in keys):
                    return True
    return False


def _vendor_release(item: EvidenceItem, profile: VendorProfile | None, cfg: RiskConfig) -> bool:
    """A third party's copy of the vendor's own announcement: a press-release mirror or a joint release whose
    headline opens with the vendor's name ('Fiserv Collaborates With Microsoft ...'). It speaks in the vendor's
    voice, so the providers it names count as named by the vendor."""
    return (profile is not None and item.family == SourceFamily.IND and item.tags.u_class != "U8"
            and _cue(cfg, "vendor_release", item.source_type) and _opens_with_vendor(item.title, profile))


# --------------------------------------------------------------------------- per-item exposure and decision impact


@dataclass(frozen=True)
class _Level:
    level: int
    rule: str                             # key in cfg.reasons
    if_confirmed: int | None = None       # 3 when an R2 item would give E3 at R3
    providers: tuple[str, ...] = ()


def _exposure(item: EvidenceItem, level: int, privileged: bool, profile: VendorProfile,
              cfg: RiskConfig) -> _Level | None:
    """E for one pathway item by locus (design 2.8 table); None when its locus is not an exposure pathway."""
    locus = item.tags.locus
    r3 = item.tags.rl == "R3"
    text = _claim_text(item)
    if locus in SERVICE_LOCI:
        external = tuple(p for p in item.providers if _external(p, profile, cfg))
        model = bool(external) and (bool(item.data_mentioned) or _has(item, "G3") or _cue(cfg, "processing", text))
        sensitive = level >= 4 or privileged
        if sensitive or model:
            if not r3:
                return _Level(2, "service_capped", if_confirmed=3)
            return _Level(3, "service_e3" if sensitive else "service_model", providers=external)
        return _Level(2, "service_e2") if level == 3 else _Level(1, "service_e1")
    if locus in DELIVERY_LOCI:
        if _cue(cfg, "customer_data", text):
            return _Level(3, "delivery_e3") if r3 else _Level(2, "delivery_capped", if_confirmed=3)
        return _Level(2, "delivery_e2") if _cue(cfg, "artefacts", text) else _Level(1, "delivery_e1")
    if locus == "sdlc":
        return _Level(2, "sdlc_e2") if _cue(cfg, "production_data", text) else _Level(1, "sdlc_e1")
    if locus == "corporate_internal":
        return _Level(0, "corporate")
    return None


def _decision(item: EvidenceItem, cfg: RiskConfig) -> _Level | None:
    """K for one pathway item from its action level; None when the level is unknown."""
    action = item.action_level
    if action == "automated_action":
        return _Level(3, "k3") if _cue(cfg, "k3_targets", _claim_text(item)) else _Level(2, "k2_operational")
    if action == "human_reviewed_decision":
        return _Level(2, "k2_reviewed")
    if action == "advisory":
        return _Level(1, "k1")
    if action == "none":
        return _Level(0, "k0")
    return None


def _best(levels: Sequence[tuple[EvidenceItem, _Level]]) -> tuple[int, list[EvidenceItem], _Level] | None:
    """The maximum level, the items (best first) that reach it, and the first one's rule."""
    if not levels:
        return None
    top = max(lv.level for _, lv in levels)
    reached = [(item, lv) for item, lv in levels if lv.level == top]
    return top, [item for item, _ in reached], reached[0][1]


# --------------------------------------------------------------------------- transparency checks


def _first_party(item: EvidenceItem) -> bool:
    """A disclosure by the vendor itself: not a DNS token, a third party's page (IND: trade press, podcasts, provider
    stories, partner pages), a platform supplier, an inferred affiliate, commentary, or an aggregated,
    user-generated or unknown-origin source (SR D-F)."""
    t = item.tags
    return (t.locus not in THIRD_PARTY_LOCI and item.family not in (SourceFamily.DNS, SourceFamily.IND)
            and t.sr in FIRST_PARTY_SR)


def _gap_closers(items: Sequence[EvidenceItem], cfg: RiskConfig) -> dict[str, list[str]]:
    """Item keys (best first) that close each check t1..t6. Only the vendor's own citable disclosures count.

    t3-t6 need a disclosure of the vendor's own practice: a marketing statement (U7) or a job posting describes
    neither data-use terms, human oversight, a governance attestation nor a notification commitment. t3 needs a
    legal item whose data-flow statement (G3) is about data use, not only human review. t4-t6 never close on a
    description of a law, a risk factor or a forward-looking-statements disclaimer (cue ``boilerplate``), and t5
    needs a governance cue (NIST AI RMF, ISO/IEC 42001, an AI policy) in the claim or in the source type or title
    (an item of the vendor's 'AI policy' page): a U6 tag alone is not an attestation.
    """
    closers: dict[str, list[str]] = {g: [] for g in GAP_KEYS}
    for item in items:
        if not _first_party(item):
            continue
        t = item.tags
        text = _claim_text(item)
        key = item.item_key
        if t.sr in ("A", "B") and t.rl == "R3" and t.u_number <= 4:
            closers["t1"].append(key)
        if _names_its_providers(item):
            closers["t2"].append(key)
        if t.u_class == "U7" or item.family == SourceFamily.JOB:
            continue
        if item.family == SourceFamily.LEG and _has(item, "G3") and _cue(cfg, "data_use", text):
            closers["t3"].append(key)
        if _cue(cfg, "boilerplate", text):
            continue
        if item.action_level == "human_reviewed_decision" or _cue(cfg, "human_oversight", text):
            closers["t4"].append(key)
        if _cue(cfg, "governance", "\n".join([text, item.source_type, item.title])):  # e.g. the 'AI policy' page
            closers["t5"].append(key)
        if _cue(cfg, "notification", text):
            closers["t6"].append(key)
    return closers


# --------------------------------------------------------------------------- derive_inputs


def derive_inputs(verdict: UsageVerdict, items: Sequence[EvidenceItem], criticality: CriticalityResult,
                  profile: VendorProfile, plan: DepthPlan, *, overrides: OverrideStore | None = None,
                  config: RiskConfig | None = None) -> RiskInputs:
    """E, K, TP and TG for one vendor with their provenance (design 2.8 'Inputs').

    Yes verdicts take E and K from the citable Q and K pathways; an input they leave unknown is assumed. K is the
    maximum over the pathways, and a pathway in the service, its delivery or an unknown place whose action level is
    unknown counts at the role's K (assumed), so a stated action level on one pathway never lowers K below what the
    unknowns rule assumes for the others. An Inconclusive verdict assumes both (unknowns rule). A No verdict has no
    pathway: E0 and K0. TG counts the checks no citable first-party disclosure closes. A ``risk_input_override``
    record then replaces a value and its reason.
    """
    cfg = config or load_risk_config()
    vendor_id = profile.vendor_id
    if criticality.vendor_id != vendor_id or plan.vendor_id != vendor_id:
        raise ValueError(f"criticality ({criticality.vendor_id}), depth plan ({plan.vendor_id}) and profile "
                         f"({vendor_id}) must belong to one vendor")
    citable = _citable(items, vendor_id)
    level, privileged = _data_sensitivity(criticality)
    data = _data_words(level, privileged, cfg)
    state: dict[str, Any] = dict(e_items=[], k_items=[], e_assumed=False, k_assumed=False, e_if=None)
    label = verdict.label
    if label in NO_LABELS:
        state.update(e=0, k=0, e_reason=cfg.reasons["no_pathway_e"], k_reason=cfg.reasons["no_pathway_k"])
    else:
        why = "inconclusive" if label not in YES_LABELS else "unknown"
        paths = _pathways(verdict, citable) if label in YES_LABELS else []
        exposures = [(i, lv) for i in paths if (lv := _exposure(i, level, privileged, profile, cfg)) is not None]
        if (found := _best(exposures)) is None:
            state.update(e=_exposure_from_data(level, privileged), e_assumed=True,
                         e_reason=cfg.reasons[f"e_assumed_{why}"].format(data=data))
        else:
            e, top, lv = found
            state.update(e=e, e_items=[i.item_key for i in top],
                         e_reason=cfg.reasons[lv.rule].format(data=data, providers=_join(lv.providers)))
            if e < 3 and any(x.if_confirmed == 3 for _, x in exposures):
                state["e_if"] = 3
        decisions = [(i, lv) for i in paths if (lv := _decision(i, cfg)) is not None]
        open_paths = [i for i in paths if _decision(i, cfg) is None and i.tags.locus in K_ASSUMED_LOCI]
        role_k, role = _role(criticality, profile, cfg)
        if (found := _best(decisions)) is None:
            state.update(k=role_k, k_assumed=True,
                         k_reason=cfg.reasons[f"k_assumed_{why}"].format(role=cfg.wording.role[role]))
        elif open_paths and role_k > found[0]:
            # Monotone K: an action level stated for one pathway never lowers the assumption the unknowns rule makes
            # for the others, so K = max(evidenced, assumed) and adding evidence never lowers it.
            state.update(k=role_k, k_assumed=True, k_reason=cfg.reasons["k_assumed_partial"].format(
                role=cfg.wording.role[role], k=found[0]))
        else:
            k, top, lv = found
            state.update(k=k, k_items=[i.item_key for i in top], k_reason=cfg.reasons[lv.rule])
    closers = _gap_closers(citable, cfg)
    state["gaps"] = {g: not closers[g] for g in GAP_KEYS}
    state["gap_items"] = {g: closers[g] for g in GAP_KEYS if closers[g]}
    state["gap_reasons"] = {g: cfg.reasons["gap"] for g in GAP_KEYS if not closers[g]}
    if overrides is not None:
        _apply_overrides(state, overrides, vendor_id, citable, cfg)
    return RiskInputs(
        e=state["e"], k=state["k"], tp=criticality.tier.points, e_assumed=state["e_assumed"],
        k_assumed=state["k_assumed"], e_reason=state["e_reason"], k_reason=state["k_reason"], gaps=state["gaps"],
        gap_reasons=state["gap_reasons"], e_items=state["e_items"], k_items=state["k_items"],
        gap_items=state["gap_items"], e_if_confirmed=state["e_if"],
    )


def _apply_overrides(state: dict[str, Any], store: OverrideStore, vendor_id: str,
                     citable: Sequence[EvidenceItem], cfg: RiskConfig) -> None:
    """Apply the latest risk_input_override per key (e, k, t1..t6). Evidence keys make a value not assumed."""
    order = {item.item_key: n for n, item in enumerate(citable)}
    for key in OVERRIDE_KEYS:
        record = store.latest(OVERRIDE_KIND, vendor_id, key)
        if record is None:
            continue
        reason = cfg.reasons["override"].format(analyst=record.analyst, date=record.date,
                                                reason=record.reason.strip())
        raw = record.extra.get("evidence") or []
        raw = [raw] if isinstance(raw, str) else raw
        evidence = sorted({k for k in raw if isinstance(k, str) and k in order}, key=order.__getitem__)
        if key in ("e", "k"):
            try:
                value = int(record.value)
            except ValueError:
                value = -1
            if not 0 <= value <= 3:
                raise ValueError(f"{vendor_id}: a risk_input_override for {key} must be 0..3, not {record.value!r}")
            state.update({key: value, f"{key}_assumed": not evidence, f"{key}_items": evidence,
                          f"{key}_reason": reason})
            if key == "e":
                state["e_if"] = None   # the analyst's value replaces the derivation, 'if confirmed' included
        else:
            if record.value not in ("missing", "closed"):
                raise ValueError(f"{vendor_id}: a risk_input_override for {key} must be 'missing' or 'closed', "
                                 f"not {record.value!r}")
            missing = record.value == "missing"
            state["gaps"][key] = missing
            if missing or not evidence:
                state["gap_items"].pop(key, None)
            else:
                state["gap_items"][key] = evidence
            state["gap_reasons"][key] = reason


# --------------------------------------------------------------------------- escalators


def _day(value: str) -> dt.date:
    return dt.date.fromisoformat(value[:10])


def _item_day(item: EvidenceItem) -> dt.date | None:
    """Publication date, else retrieval date."""
    for raw in (item.published, item.retrieved_at):
        if raw:
            try:
                return _day(raw)
            except ValueError:
                continue
    return None


def _months_before(day: dt.date, as_of: dt.date) -> int:
    """Whole calendar months from day to as_of; a day on or after as_of counts as 0."""
    if day >= as_of:
        return 0
    months = (as_of.year - day.year) * 12 + (as_of.month - day.month)
    return months - 1 if as_of.day < day.day else months


def _vendor_named(citable: Sequence[EvidenceItem], profile: VendorProfile | None, cfg: RiskConfig) -> set[str]:
    """Case-folded canonical providers the vendor names itself: the providers of its own items and of its own
    releases as a third party carries them, plus every foundation-model maker its own words name (so 'Integrates
    models from e.g., OpenAI, Google, Anthropic' names Google even when the item's provider list missed it)."""
    own = [i for i in citable if _vendor_voice(i) or _vendor_release(i, profile, cfg)]
    names = {n.casefold() for n in provider_names(own, config=cfg)}
    for item in own:
        names |= {n.casefold() for n in _makers_in(item.excerpt, cfg)}
    return names


def evaluate_escalators(verdict: UsageVerdict, items: Sequence[EvidenceItem], inputs: RiskInputs,
                        criticality: CriticalityResult, *, as_of: str, profile: VendorProfile | None = None,
                        config: RiskConfig | None = None) -> dict[str, bool]:
    """X1..X6 -> whether the citable items evidence the condition (design 2.8 step 4).

    Whether a true escalator is applied (Confirmed or Probable with the gate met) is decided by ``score``.
    ``profile`` (``assess`` passes it) lets the vendor's name be recognised: a third party's copy of the vendor's
    own release names providers in the vendor's voice (X1, X6), and a third party's incident report must name the
    vendor (X5). Without it, no release is recognised and X5 does not check the name.

    - X1: a provider (normalised to its maker) that a third party names at RL R2+ and RC T1+ (a T0 item is
      historical context), and that the vendor's own words do not name, with evidenced exposure 2+.
    - X5: an AI incident in the vendor's own items or a third party's report naming it: the incident, an AI term
      and no hypothetical wording in one sentence, within 24 months of ``as_of``. Commentary, platform suppliers,
      inferred affiliates, limiting statements and items about other companies (M3) never count.
    - X6: exactly one foundation-model maker across the Q and K pathways and the vendor's own provider naming.
    """
    cfg = config or load_risk_config()
    if profile is not None and profile.vendor_id != criticality.vendor_id:
        raise ValueError(f"profile ({profile.vendor_id}) and criticality ({criticality.vendor_id}) must belong to "
                         "one vendor")
    as_of_day = _day(as_of)
    citable = _citable(items, criticality.vendor_id)
    paths = _pathways(verdict, citable)
    e_evidenced = inputs.e >= 2 and not inputs.e_assumed
    _, privileged = _data_sensitivity(criticality)

    vendor_named = _vendor_named(citable, profile, cfg)
    third_party = {n.casefold() for n in provider_names(
        [i for i in citable if i.family == SourceFamily.IND and i.tags.rl_level >= 2 and i.tags.rc_level >= 1
         and not _vendor_release(i, profile, cfg)], config=cfg)}
    x1 = e_evidenced and bool(third_party - vendor_named)

    def trains(item: EvidenceItem) -> bool:
        """The vendor's own data-use practice: platform suppliers, affiliates and commentary do not count."""
        if item.tags.locus in THIRD_PARTY_LOCI or item.tags.u_class == "U8":
            return False
        text = _claim_text(item)
        return _cue(cfg, "training", text) and not _cue(cfg, "no_training", text)

    x2 = e_evidenced and any(trains(i) for i in citable)

    def acts_on_production(item: EvidenceItem) -> bool:
        text = _claim_text(item)
        agentic = item.tags.ai_type == "agentic" or privileged or _cue(cfg, "privileged", text)
        return item.action_level == "automated_action" and agentic and _cue(cfg, "production", text)

    x3 = any(acts_on_production(i) for i in paths)

    def k3_on_credit(item: EvidenceItem) -> bool:
        level = _decision(item, cfg)
        return level is not None and level.level == 3 and _cue(cfg, "credit_holds", _claim_text(item))

    x4 = inputs.k >= 3 and not inputs.k_assumed and any(k3_on_credit(i) for i in paths)

    def recent_incident(item: EvidenceItem) -> bool:
        """An AI incident of the vendor's own: not commentary, a supplier, an affiliate or another company."""
        t = item.tags
        if t.locus in THIRD_PARTY_LOCI or t.u_class == "U8" or _has(item, "M3"):
            return False
        if item.family == SourceFamily.IND and profile is not None and not _names_vendor(item, profile):
            return False
        day = _item_day(item)
        if day is None or _months_before(day, as_of_day) > X5_WINDOW_MONTHS:
            return False
        return any(_cue(cfg, "incident", s) and _cue(cfg, "ai_terms", s) and not _cue(cfg, "hypothetical", s)
                   for s in _sentences(_claim_text(item)))

    x5 = any(recent_incident(i) for i in citable)
    attributed = [*paths, *(i for i in citable if _names_its_providers(i) or _vendor_release(i, profile, cfg))]
    x6 = criticality.tier == Tier.CRITICAL and len(foundation_model_providers(attributed, config=cfg)) == 1
    return dict(zip(ESCALATOR_CODES, (x1, x2, x3, x4, x5, x6), strict=True))


# --------------------------------------------------------------------------- the ordered steps


@dataclass(frozen=True)
class _Values:
    e: int
    k: int
    tp: int
    tg: int
    e_assumed: bool
    k_assumed: bool

    @classmethod
    def of(cls, inputs: RiskInputs) -> _Values:
        return cls(inputs.e, inputs.k, inputs.tp, inputs.tg, inputs.e_assumed, inputs.k_assumed)


@dataclass(frozen=True)
class _Run:
    values: _Values
    arp: int
    base: str
    gate_met: bool
    gated: str
    true: tuple[str, ...]
    fired: tuple[str, ...]
    logged: tuple[str, ...]
    pre_cap: str
    cap: str
    final: str


def _run(values: _Values, label: str, escalators: Mapping[str, bool], cfg: RiskConfig) -> _Run:
    """Steps 1-5 in their fixed order: score, base class, materiality gate, escalator floors, verdict cap."""
    arp = 2 * values.e + 2 * values.k + values.tp + values.tg
    base = arp_class(arp)
    gate_met = ((values.e >= cfg.gate.min_e and not values.e_assumed)
                or (values.k >= cfg.gate.min_k and not values.k_assumed))
    gated = cfg.gate.lowered_to if _rank(base) >= _rank("High") and not gate_met else base
    true = tuple(code for code in ESCALATOR_CODES if escalators.get(code))
    applies = label in YES_LABELS and gate_met
    fired, logged = (true, ()) if applies else ((), true)
    pre_cap = cfg.floor_class if fired and _rank(gated) < _rank(cfg.floor_class) else gated
    cap = cfg.caps[label]
    if cap == "None identified":
        final = cap
    else:
        final = cap if cap and _rank(pre_cap) > _rank(cap) else pre_cap
    return _Run(values, arp, base, gate_met, gated, true, fired, logged, pre_cap, cap, final)


def _mask(escalators: Mapping[str, bool], values: _Values) -> dict[str, bool]:
    """Escalators under hypothetical inputs: X1 and X2 need evidenced E >= 2, X4 an evidenced K3."""
    e_ok = values.e >= 2 and not values.e_assumed
    out = dict(escalators)
    out["X1"] = bool(out.get("X1")) and e_ok
    out["X2"] = bool(out.get("X2")) and e_ok
    out["X4"] = bool(out.get("X4")) and values.k >= 3 and not values.k_assumed
    return out


def score(inputs: RiskInputs, verdict: UsageVerdict, *, tier: Tier | str,
          escalators: Mapping[str, bool] | None = None, items: Sequence[EvidenceItem] = (),
          criticality: CriticalityResult | None = None, config: RiskConfig | None = None) -> RiskResult:
    """Run the fixed order on given inputs (design 2.8) and log every step.

    ``escalators`` maps X1..X6 to whether the condition is evidenced (``evaluate_escalators``). ``items`` (the
    vendor's citable items) and ``criticality`` are optional context for the flip condition and the themes;
    ``assess`` passes them, and the V-000 calibration runs without them.
    """
    cfg = config or load_risk_config()
    tier = Tier(tier)
    if inputs.tp != tier.points:
        raise ValueError(f"tier points {inputs.tp} do not match the {tier.value} tier ({tier.points} points)")
    given = dict(escalators or {})
    if unknown := sorted(set(given) - set(ESCALATOR_CODES)):
        raise ValueError(f"unknown escalator codes: {', '.join(unknown)}")
    esc = {code: bool(given.get(code)) for code in ESCALATOR_CODES}
    vendor_id = criticality.vendor_id if criticality is not None else None
    pool = _citable(items, vendor_id)
    label = verdict.label
    run = _run(_Values.of(inputs), label, esc, cfg)
    ceiling_run: _Run | None = None
    ceiling = ""
    if label not in NO_LABELS:
        values = replace(_Values.of(inputs), e=max(inputs.e, inputs.e_if_confirmed or 0), e_assumed=False,
                         k_assumed=False)
        ceiling_run = _run(values, "Confirmed", esc, cfg)
        ceiling = ceiling_run.pre_cap if ceiling_run.pre_cap != run.final else ""
    flip = _flip(inputs, verdict, esc, run, ceiling, pool, criticality, cfg)
    return RiskResult(
        inputs=inputs, arp=run.arp, base_class=run.base, gate_met=run.gate_met,
        escalators_fired=list(run.fired), escalators_logged_not_applied=list(run.logged), cap=run.cap,
        pre_cap_class=run.pre_cap, final_class=run.final, provisional=run.cap == "Medium", ceiling_class=ceiling,
        flip_condition=flip, themes=_themes(inputs, verdict, esc, pool, cfg),
        steps=_steps(inputs, verdict, run, ceiling, ceiling_run, flip, cfg),
    )


def assess(verdict: UsageVerdict, items: Sequence[EvidenceItem], criticality: CriticalityResult,
           profile: VendorProfile, plan: DepthPlan, *, as_of: str, overrides: OverrideStore | None = None,
           config: RiskConfig | None = None) -> RiskResult:
    """derive_inputs, then evaluate_escalators, then score at the vendor's tier (contracts_p3 section 8)."""
    cfg = config or load_risk_config()
    inputs = derive_inputs(verdict, items, criticality, profile, plan, overrides=overrides, config=cfg)
    escalators = evaluate_escalators(verdict, items, inputs, criticality, as_of=as_of, profile=profile, config=cfg)
    return score(inputs, verdict, tier=criticality.tier, escalators=escalators, items=items,
                 criticality=criticality, config=cfg)


# --------------------------------------------------------------------------- flip condition and themes


def _flip(inputs: RiskInputs, verdict: UsageVerdict, esc: Mapping[str, bool], run: _Run, ceiling: str,
          items: Sequence[EvidenceItem], criticality: CriticalityResult | None, cfg: RiskConfig) -> str:
    """One plain sentence naming the single confirmation or change that would move the class ('' if none)."""
    flip = cfg.flip
    label = verdict.label
    if label in NO_LABELS:
        platforms = any(i.tags.strength == "Context - platform supplier" for i in items)
        return flip.none_identified_platforms if platforms else flip.none_identified
    if label not in YES_LABELS:
        return flip.provisional.format(subject=_subject(verdict, items, cfg), ceiling=ceiling) if ceiling else ""
    base = _Values.of(inputs)
    final = run.final

    def moved(values: _Values, as_label: str) -> str:
        return _run(values, as_label, _mask(esc, values), cfg).final

    if label == "Probable":
        if _rank(run.pre_cap) > _rank(final):
            return flip.probable_cap.format(cls=run.pre_cap)
        service = 0
        if criticality is not None:
            service = _exposure_from_data(*_data_sensitivity(criticality))
        e_confirmed = max(inputs.e, inputs.e_if_confirmed or 0, service)
        if e_confirmed > inputs.e:
            cls = moved(replace(base, e=e_confirmed, e_assumed=False), "Confirmed")
            if _rank(cls) > _rank(final):
                return flip.probable_confirm.format(e=e_confirmed, cls=cls)
    ups: list[tuple[str, str]] = []
    if inputs.e < 3 and _exposure_place(inputs, items) is not None:
        ups.append(("e", moved(replace(base, e=3, e_assumed=False), label)))
    if inputs.k < 3:
        ups.append(("k", moved(replace(base, k=3, k_assumed=False), label)))
    ups = [(which, cls) for which, cls in ups if _rank(cls) > _rank(final)]
    if ups:
        best = max((cls for _, cls in ups), key=_rank)
        which = [w for w, cls in ups if cls == best]
        e3 = flip.e3[_exposure_place(inputs, items) or "service"]
        if which == ["e", "k"]:
            return flip.raise_both.format(e3=e3, k3=flip.k3, cls=best)
        if which == ["e"]:
            return flip.raise_e.format(e3=e3, cls=best)
        return flip.raise_k.format(k3=flip.k3, cls=best)
    downs: list[tuple[str, str]] = []
    if inputs.e >= 2:
        downs.append(("e", moved(replace(base, e=1, e_assumed=False), label)))
    if inputs.k >= 2:
        downs.append(("k", moved(replace(base, k=1, k_assumed=False), label)))
    downs = [(which, cls) for which, cls in downs if _rank(cls) < _rank(final)]
    if downs:
        lowest = min((cls for _, cls in downs), key=_rank)
        which = next(w for w, cls in downs if cls == lowest)
        return (flip.lower_e if which == "e" else flip.lower_k).format(cls=lowest)
    if label == "Probable":
        # At the Probable cap, neither exposure 3 nor decision impact 3 moves the class on its own: name the
        # confirmation that would lift the cap together with it.
        confirmed: list[tuple[str, str]] = []
        if inputs.e < 3 and _exposure_place(inputs, items) is not None:
            confirmed.append(("e", moved(replace(base, e=3, e_assumed=False), "Confirmed")))
        if inputs.k < 3:
            confirmed.append(("k", moved(replace(base, k=3, k_assumed=False), "Confirmed")))
        confirmed = [(which, cls) for which, cls in confirmed if _rank(cls) > _rank(final)]
        if confirmed:
            best = max((cls for _, cls in confirmed), key=_rank)
            which = [w for w, cls in confirmed if cls == best]
            e3 = flip.e3[_exposure_place(inputs, items) or "service"]
            parts = {"e": flip.what_e.format(e3=e3), "k": flip.what_k.format(k3=flip.k3)}
            return flip.probable_raise.format(what=flip.what_or.join(parts[w] for w in which), cls=best)
    return ""


def _exposure_place(inputs: RiskInputs, items: Sequence[EvidenceItem]) -> str | None:
    """Where exposure 3 could arise: 'service', 'delivery', or None when the pathways cannot reach it (sdlc,
    corporate). With no evidenced pathway (assumed or calibration inputs) it is the service itself."""
    by_key = {i.item_key: i for i in items}
    loci = {by_key[k].tags.locus for k in inputs.e_items if k in by_key}
    if not loci:
        return "service"
    if loci & SERVICE_LOCI:
        return "service"
    if loci & DELIVERY_LOCI:
        return "delivery"
    return None


def _subject(verdict: UsageVerdict, items: Sequence[EvidenceItem], cfg: RiskConfig) -> str:
    """What would have to be confirmed, from the first decisive item's strength label."""
    by_key = {i.item_key: i for i in items}
    first = next((by_key[k] for k in verdict.decisive if k in by_key), None)
    label = first.tags.strength if first is not None else "default"
    template = cfg.flip.subjects.get(label, cfg.flip.subjects["default"])
    if "{name}" not in template:
        return template
    name = ""
    if first is not None and label == "Context - inferred affiliate":
        name = first.publisher.strip()
    elif label == "Context - relationship only":
        name = _join(provider_names([i for i in items if i.tags.strength == label], config=cfg))
    if name:
        return template.format(name=name)
    return cfg.flip.subjects_unnamed.get(label, cfg.flip.subjects["default"])


def _themes(inputs: RiskInputs, verdict: UsageVerdict, esc: Mapping[str, bool], items: Sequence[EvidenceItem],
            cfg: RiskConfig) -> list[str]:
    """Risk themes RT1..RT7 (NIST AI 600-1, OWASP LLM Top 10). A No verdict has none."""
    if verdict.label in NO_LABELS:
        return []
    missing = set(inputs.missing_gaps) if inputs.gaps else set(GAP_KEYS)
    pool = [i for i in items if i.tags.u_class != "U8"]
    types = {i.tags.ai_type for i in pool}
    found: set[str] = set()
    if inputs.e >= 2:
        found.add("RT1")
    if "t2" in missing or esc.get("X1") or esc.get("X6") or any(i.tags.strength in CONTEXT_STRENGTHS for i in pool):
        found.add("RT2")
    if inputs.k >= 2 or "t4" in missing:
        found.add("RT3")
    if inputs.k >= 3 or esc.get("X3") or any(i.action_level == "automated_action" for i in pool):
        found.add("RT4")
    for code, spec in cfg.themes.items():
        if types & set(spec.ai_types):
            found.add(code)
    return [code for code in THEME_CODES if code in found]


# --------------------------------------------------------------------------- steps log (the UI's why trace)


def _band_range(cls: str, cfg: RiskConfig) -> tuple[int, int]:
    low = cfg.bands[cls]
    higher = [cfg.bands[c] for c in RISK_CLASS_ORDER[_rank(cls) + 1:]]
    return low, (min(higher) - 1 if higher else 18)


def _steps(inputs: RiskInputs, verdict: UsageVerdict, run: _Run, ceiling: str, ceiling_run: _Run | None,
           flip: str, cfg: RiskConfig) -> list[str]:
    def basis(assumed: bool) -> str:
        return "assumed" if assumed else "evidenced"

    label = verdict.label
    gaps = f" ({len(inputs.missing_gaps)} of {len(GAP_KEYS)} checks missing)" if inputs.gaps else ""
    steps = [
        f"1. Score: exposure {inputs.e} of 3 ({basis(inputs.e_assumed)}), decision impact {inputs.k} of 3 "
        f"({basis(inputs.k_assumed)}), tier points {inputs.tp}, transparency gap {inputs.tg}{gaps}; "
        f"ARP = 2x{inputs.e} + 2x{inputs.k} + {inputs.tp} + {inputs.tg} = {run.arp} of 18.",
        "2. Base class: {0} falls in the {1} band ({2}-{3}).".format(run.arp, run.base, *_band_range(run.base, cfg)),
    ]
    if run.gate_met:
        met = [f"exposure {inputs.e}" if inputs.e >= cfg.gate.min_e and not inputs.e_assumed else "",
               f"decision impact {inputs.k}" if inputs.k >= cfg.gate.min_k and not inputs.k_assumed else ""]
        steps.append(f"3. Materiality gate: met by evidenced {_join(met)}.")
    elif run.gated != run.base:
        steps.append(f"3. Materiality gate: not met (neither exposure of {cfg.gate.min_e} or more nor decision "
                     f"impact of {cfg.gate.min_k} or more is evidenced), so {run.base} is lowered to {run.gated}.")
    else:
        steps.append(f"3. Materiality gate: not met, but the base class {run.base} is below High, so nothing "
                     f"changes.")

    def named(codes: Sequence[str]) -> str:
        return _join([f"{c} ({cfg.escalators[c].name})" for c in codes])

    if not run.true:
        steps.append("4. Escalators: none apply.")
    elif run.fired:
        verb = "sets" if len(run.fired) == 1 else "set"
        change = (f", so {run.gated} rises to {run.pre_cap}" if run.pre_cap != run.gated
                  else f"; the class is already {run.gated}")
        steps.append(f"4. Escalators: {named(run.fired)} {verb} a {cfg.floor_class} floor{change}.")
    else:
        why = f"the verdict is {label}" if label not in YES_LABELS else "the materiality gate is not met"
        verb = "is" if len(run.logged) == 1 else "are"
        steps.append(f"4. Escalators: {named(run.logged)} {verb} logged, not applied, because {why}.")
    if run.cap == "":
        steps.append(f"5. Verdict cap: {label} carries no cap, so the class is {run.final}.")
    elif run.cap == "None identified":
        steps.append(f"5. Verdict cap: a No verdict ({label}) gives None identified.")
    else:
        marked = " and marks it Provisional" if run.cap == "Medium" else ""
        outcome = (f", so {run.pre_cap} becomes {run.final}" if run.final != run.pre_cap
                   else f"; {run.pre_cap} is within the cap")
        steps.append(f"5. Verdict cap: {label} caps the class at {run.cap}{marked}{outcome}.")
    if ceiling_run is None:
        steps.append("6. Ceiling if confirmed: not applicable to a No verdict.")
    elif ceiling:
        v = ceiling_run.values
        steps.append(f"6. Ceiling if confirmed: {ceiling} (assumed inputs treated as evidenced: exposure {v.e}, "
                     f"decision impact {v.k}; {ceiling_run.arp} of 18).")
    else:
        steps.append("6. Ceiling if confirmed: none above the final class.")
    steps.append(f"7. Flip condition: {flip}" if flip else
                 "7. Flip condition: none; no single confirmation or change moves the class.")
    return steps


# --------------------------------------------------------------------------- V-000 calibration and legend


def calibration_inputs(config: RiskConfig | None = None) -> RiskInputs:
    """The V-000 calibration inputs from config/risk.toml (E3 + K3 + TP3 + TG2, all evidenced)."""
    cfg = config or load_risk_config()
    cal = cfg.calibration
    return RiskInputs(e=cal.e, k=cal.k, tp=cal.tier.points, e_reason=cal.e_reason, k_reason=cal.k_reason,
                      gaps=dict(cal.gaps), gap_reasons={g: cfg.reasons["gap"] for g in GAP_KEYS if cal.gaps[g]})


def calibration_verdict() -> UsageVerdict:
    """The Confirmed verdict of the V-000 worked example (it cites no evidence items)."""
    return UsageVerdict(
        rule="b", likelihood="very likely", confidence="High",
        confidence_reason="the worked example rests on two consistent first-party sources and complete coverage",
        coverage_complete=True,
        trace=["b) Confirmed: the V-000 worked example is the calibration case; no evidence items are scored."],
    )


def calibrate(config: RiskConfig | None = None) -> RiskResult:
    """``score`` on the V-000 calibration inputs with a Confirmed verdict: 17 of 18, Critical (design 2.8)."""
    cfg = config or load_risk_config()
    return score(calibration_inputs(cfg), calibration_verdict(), tier=cfg.calibration.tier, config=cfg)


def legend(config: RiskConfig | None = None) -> list[tuple[str, str]]:
    """(term, meaning) rows of the risk matrix for the Method & Legend sheet."""
    cfg = config or load_risk_config()
    bands = ", ".join("{0} {1}-{2}".format(c, *_band_range(c, cfg)) for c in reversed(RISK_CLASS_ORDER))
    rows = [
        ("ARP", "2 x exposure + 2 x decision impact + tier points + transparency gap, 0-18; " + bands + "."),
        ("Exposure (E)", "0-3, the maximum over the qualifying and corroborating pathways by where the AI sits; "
                         "exposure 3 needs an exact-service (R3) item, and a product-family (R2) item counts as 2."),
        ("Decision impact (K)", "3 automated action on customers, funds, regulatory outputs or production without "
                                "per-case review; 2 human-reviewed, or autonomous on internal operations; "
                                "1 advisory; 0 none."),
        ("Tier points (TP)", "Critical 3, High 2, Medium 1, Low 0."),
        ("Transparency gap (TG)", "missing checks 0-1 give 0, 2-3 give 1, 4-5 give 2, all 6 give 3."),
        *((f"Check {g}", cfg.gaps[g]) for g in GAP_KEYS),
        ("Closing a check", "only the vendor's own citable disclosure closes a check, never a third party's page, a "
                            "DNS token, a platform supplier, an inferred affiliate or commentary; checks t3-t6 also "
                            "never close on a marketing claim or a job posting, and t4-t6 never on a description of "
                            "a law, a risk factor or a forward-looking statement."),
        ("Unknowns rule", "an Inconclusive verdict, or an input a Yes verdict leaves unknown, is assumed: exposure "
                          "from data sensitivity, decision impact from the vendor's role; a pathway in the service "
                          "or its delivery that does not state what the AI's output does counts at the role's "
                          "decision impact, so added evidence never lowers it; assumed values never meet the "
                          "materiality gate."),
        ("Materiality gate", f"High or above needs evidenced exposure of {cfg.gate.min_e}+ or decision impact of "
                             f"{cfg.gate.min_k}+; otherwise the class is lowered to {cfg.gate.lowered_to}."),
        *((f"Escalator {c}", f"{spec.description} Sets a {cfg.floor_class} floor for Confirmed or Probable verdicts "
                             f"with the gate met.") for c, spec in cfg.escalators.items()),
        ("Verdict cap", "applied last: Probable at most High; Inconclusive at most Medium (Provisional); a No "
                        "verdict gives None identified; Confirmed is not capped."),
        ("Ceiling if confirmed", "the class after the first four steps with assumed inputs treated as evidenced."),
        *((f"Theme {c}", f"{spec.name} (NIST AI 600-1: {spec.nist}; OWASP: {spec.owasp})")
          for c, spec in cfg.themes.items()),
    ]
    return rows
