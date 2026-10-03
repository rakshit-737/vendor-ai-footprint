"""Recommended actions (column U): the playbook for the AI risk class plus gap blocks.

Design Appendix A 2.8 ('U'); docs/contracts_p3.md section 9. The playbook follows V-000's four sentences for a
Critical or High class (questionnaire, AI sub-processor inventory, AI clauses, escalation), asks questionnaire items
Q1-Q4 for a Provisional class, and asks for a no-AI attestation when no AI risk is identified. Gap blocks (SUB,
TRAIN, LOC, EXPL, AGENT, PI, GOV, INC, CONC, MRM) come from the missing transparency checks, the escalators and the
cited items, and map to questionnaire items Q1-Q15 and contract clauses C1-C11.

Every sentence is an instruction to Meridian, in the imperative as in V-000's column U: Meridian issues the
questionnaire, negotiates the clauses and escalates. The assessment team never contacts a vendor, and the loader
refuses first-person or team wording. The rendered text stays within the column U budget (1,480 characters).
Policy and wording live in config/actions.toml.
"""

from __future__ import annotations

import functools
import re
import string
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from footprint import risk as risk_engine
from footprint.models import (
    GAP_BLOCK_CODES,
    GAP_KEYS,
    ActionPlan,
    ClauseCode,
    EvidenceItem,
    QuestionCode,
    RiskResult,
    SourceFamily,
    Tier,
    UsageVerdict,
)
from footprint.workbook import DEFAULT_LENGTH_BUDGETS

__all__ = [
    "DEFAULT_CONFIG_PATH", "PLAYBOOK_KEYS", "ActionsConfig", "clause_table", "gap_block_table", "gap_blocks",
    "load_actions", "plan_actions", "playbook_key", "questionnaire_table",
]

DEFAULT_CONFIG_PATH: Path = Path(__file__).resolve().parents[2] / "config" / "actions.toml"
PLAYBOOK_KEYS: tuple[str, ...] = ("critical", "high", "provisional", "standard", "none_high", "none_low")
QUESTION_CODES: tuple[str, ...] = tuple(f"Q{n}" for n in range(1, 16))
CLAUSE_CODES: tuple[str, ...] = tuple(f"C{n}" for n in range(1, 12))
PLACEHOLDERS: frozenset[str] = frozenset({"days", "topics", "provider_flag", "ceiling_clause", "platform_clause"})
CUE_KEYS: tuple[str, ...] = ("region", "register")
COLUMN_BUDGET: int = DEFAULT_LENGTH_BUDGETS["recommended_action"]
PLATFORM_LABEL = "Context - platform supplier"

_FIRST_PERSON_WORDS = re.compile(r"\b(?:we|our|ours|us|team)\b", re.IGNORECASE)
_FIRST_PERSON_I = re.compile(r"\bI\b")


# --------------------------------------------------------------------------- config (config/actions.toml)


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Deadlines(_Spec):
    """Business days within which Meridian issues the questionnaire."""

    critical: int = Field(ge=1)
    high_critical_tier: int = Field(ge=1)
    high: int = Field(ge=1)
    provisional_high_ceiling: int = Field(ge=1)
    provisional: int = Field(ge=1)
    standard_critical_tier: int = Field(ge=1)
    standard: int = Field(ge=1)


class TextSpec(_Spec):
    provider_unresolved: str
    provider_named: str
    ceiling_with: str
    ceiling_without: str
    platform_clause: str
    conflict: str
    codes_both: str
    codes_questions: str
    codes_clauses: str
    question_noun: str
    question_nouns: str
    clause_noun: str
    clause_nouns: str


class Playbook(_Spec):
    sentences: tuple[str, ...] = Field(min_length=1)
    questions: tuple[QuestionCode, ...] = ()
    clauses: tuple[ClauseCode, ...] = ()
    monitoring: str
    base_topics: bool = Field(default=True, description="the questionnaire sentence names the playbook's own topics")
    gap_blocks: bool = Field(default=True, description="False: no gap blocks (a None identified class)")


class GapBlockSpec(_Spec):
    topic: str = Field(min_length=1)
    short: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    questions: tuple[QuestionCode, ...] = Field(min_length=1)
    clauses: tuple[ClauseCode, ...] = ()


class QuestionSpec(_Spec):
    topic: str = Field(min_length=1)
    short: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ClauseSpec(_Spec):
    topic: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ActionsConfig(_Spec):
    """config/actions.toml after validation: every block, code and playbook is defined and addressed to Meridian."""

    version: str = Field(min_length=1)
    max_chars: int = Field(gt=0)
    deadlines: Deadlines
    text: TextSpec
    playbooks: dict[str, Playbook]
    monitoring: dict[str, str]
    blocks: dict[str, GapBlockSpec]
    questions: dict[str, QuestionSpec]
    clauses: dict[str, ClauseSpec]
    cues: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _complete(self) -> ActionsConfig:
        problems = _config_problems(self)
        if problems:
            raise ValueError("actions policy is invalid: " + "; ".join(problems))
        return self


def _config_problems(cfg: ActionsConfig) -> list[str]:
    problems: list[str] = []
    if cfg.max_chars > COLUMN_BUDGET:
        problems.append(f"max_chars must not exceed the column U budget ({COLUMN_BUDGET})")
    if set(cfg.playbooks) != set(PLAYBOOK_KEYS):
        problems.append(f"playbooks must be {', '.join(PLAYBOOK_KEYS)}")
    for key, playbook in cfg.playbooks.items():
        if playbook.monitoring not in cfg.monitoring:
            problems.append(f"playbooks.{key}.monitoring {playbook.monitoring!r} is not in [monitoring]")
        for sentence in playbook.sentences:
            fields = {name for _, name, _, _ in string.Formatter().parse(sentence) if name is not None}
            if unknown := sorted(fields - PLACEHOLDERS):
                problems.append(f"playbooks.{key} uses unknown placeholders {', '.join(unknown)}")
            if not sentence.rstrip().endswith("."):
                problems.append(f"playbooks.{key}: every sentence ends with a full stop")
    if tuple(cfg.blocks) != GAP_BLOCK_CODES:
        problems.append(f"blocks must be {', '.join(GAP_BLOCK_CODES)}, in order")
    if tuple(cfg.questions) != QUESTION_CODES:
        problems.append("questions must be Q1..Q15, in order")
    if tuple(cfg.clauses) != CLAUSE_CODES:
        problems.append("clauses must be C1..C11, in order")
    if missing := [k for k in CUE_KEYS if k not in cfg.cues]:
        problems.append(f"cues lacks {', '.join(missing)}")
    for pattern in (p for patterns in cfg.cues.values() for p in patterns):
        try:
            _rx(pattern)
        except re.error as exc:
            problems.append(f"bad pattern {pattern!r}: {exc}")
    for where, text in _prose(cfg):
        if _FIRST_PERSON_WORDS.search(text) or _FIRST_PERSON_I.search(text):
            problems.append(f"{where} uses first person or team wording; every action is addressed to Meridian")
    return problems


def _prose(cfg: ActionsConfig) -> list[tuple[str, str]]:
    """Every piece of wording that can reach column U or the Method & Legend sheet."""
    out = [(f"text.{k}", v) for k, v in cfg.text.model_dump().items()]
    out += [(f"monitoring.{k}", v) for k, v in cfg.monitoring.items()]
    out += [(f"playbooks.{k}", s) for k, p in cfg.playbooks.items() for s in p.sentences]
    out += [(f"blocks.{k}", t) for k, b in cfg.blocks.items() for t in (b.topic, b.short, b.trigger)]
    out += [(f"questions.{k}", t) for k, q in cfg.questions.items() for t in (q.topic, q.short, q.text)]
    out += [(f"clauses.{k}", t) for k, c in cfg.clauses.items() for t in (c.topic, c.text)]
    return out


@functools.lru_cache(maxsize=None)
def _load(path: str) -> ActionsConfig:
    with open(path, "rb") as fh:
        return ActionsConfig.model_validate(tomllib.load(fh))


def load_actions(path: str | Path | None = None) -> ActionsConfig:
    """Read and validate the actions policy (default: config/actions.toml next to the package). Cached per path."""
    return _load(str(Path(path or DEFAULT_CONFIG_PATH).resolve()))


# --------------------------------------------------------------------------- helpers


@functools.lru_cache(maxsize=None)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _hits(patterns: Iterable[str], text: str) -> bool:
    return any(_rx(p).search(text) for p in patterns)


def _claim_text(item: EvidenceItem) -> str:
    return "\n".join([item.excerpt, *item.data_mentioned])


def _series(items: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b, and c' (the serial comma of V-000's column U)."""
    items = [i for i in items if i]
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _ordered(codes: Iterable[str]) -> list[str]:
    return sorted(set(codes), key=lambda code: int(code[1:]))


# --------------------------------------------------------------------------- playbook and gap blocks


def playbook_key(risk: RiskResult, tier: Tier | str) -> str:
    """Which playbook applies: by the final class, the Provisional flag and (for None identified) the tier."""
    tier = Tier(tier)
    if risk.final_class == "None identified":
        return "none_high" if tier in (Tier.CRITICAL, Tier.HIGH) else "none_low"
    if risk.provisional:
        return "provisional"
    if risk.final_class == "Critical":
        return "critical"
    if risk.final_class == "High":
        return "high"
    return "standard"


def gap_blocks(risk: RiskResult, verdict: UsageVerdict, items: Sequence[EvidenceItem], *,
               config: ActionsConfig | None = None) -> list[str]:
    """The open gap blocks, in GAP_BLOCK_CODES order (contracts_p3 section 9). None for a No verdict.

    SUB: t2 missing, or no sub-processor register among the cited legal items. TRAIN: t3 missing. LOC: no cited
    item states a hosting region. EXPL: t4 missing. AGENT: agentic or automated-action items. PI: generative or
    conversational items. GOV: t5 missing. INC: t6 missing. CONC: escalator X6 (fired or logged), or a single
    provider named. MRM: predictive models with decision impact 2 or more. Limiting statements (U8) never trigger.
    """
    cfg = config or load_actions()
    if risk.final_class == "None identified" or verdict.column_o == "No":
        return []
    citable = [i for i in items if i.citable]
    pool = [i for i in citable if i.tags.u_class != "U8"]
    missing = set(risk.inputs.missing_gaps) if risk.inputs.gaps else set(GAP_KEYS)
    escalators = {*risk.escalators_fired, *risk.escalators_logged_not_applied}
    register = any(i.family == SourceFamily.LEG and _hits(cfg.cues["register"], f"{i.source_type}\n{i.title}\n{i.url}")
                   for i in citable)
    region = any(_hits(cfg.cues["region"], _claim_text(i)) for i in citable)
    triggers = {
        "SUB": "t2" in missing or not register,
        "TRAIN": "t3" in missing,
        "LOC": not region,
        "EXPL": "t4" in missing,
        "AGENT": any(i.tags.ai_type == "agentic" or i.action_level == "automated_action" for i in pool),
        "PI": any(i.tags.ai_type in ("genai_llm", "conversational") for i in pool),
        "GOV": "t5" in missing,
        "INC": "t6" in missing,
        "CONC": "X6" in escalators or len(risk_engine.provider_names(pool)) == 1,
        "MRM": risk.inputs.k >= 2 and any(i.tags.ai_type == "predictive_ml" for i in pool),
    }
    return [code for code in GAP_BLOCK_CODES if triggers[code]]


def _deadline(key: str, risk: RiskResult, tier: Tier, cfg: ActionsConfig) -> int:
    d = cfg.deadlines
    if key == "critical":
        return d.critical
    if key == "high":
        return d.high_critical_tier if tier == Tier.CRITICAL else d.high
    if key == "provisional":
        return d.provisional_high_ceiling if risk.ceiling_class in ("High", "Critical") else d.provisional
    return d.standard_critical_tier if tier == Tier.CRITICAL else d.standard


def _topics(playbook: Playbook, blocks: Sequence[str], cfg: ActionsConfig, short: bool) -> list[str]:
    """Questionnaire topics in words: the playbook's own (if it names them), then each block not already asked."""
    attr = "short" if short else "topic"
    topics = [getattr(cfg.questions[q], attr) for q in playbook.questions] if playbook.base_topics else []
    for code in blocks:
        spec = cfg.blocks[code]
        if spec.questions[0] not in playbook.questions:
            topics.append(getattr(spec, attr))
    return list(dict.fromkeys(topics))


def _codes_sentence(questions: Sequence[str], clauses: Sequence[str], cfg: ActionsConfig) -> str:
    """'Use questionnaire items Q2, Q3 and contract clause C9 from the Method & Legend sheet.'"""
    t = cfg.text
    q = f"{t.question_noun if len(questions) == 1 else t.question_nouns} {', '.join(questions)}"
    c = f"{t.clause_noun if len(clauses) == 1 else t.clause_nouns} {', '.join(clauses)}"
    if questions and clauses:
        return cfg.text.codes_both.format(questions=q, clauses=c)
    if questions:
        return cfg.text.codes_questions.format(questions=q)
    if clauses:
        return cfg.text.codes_clauses.format(clauses=c)
    return ""


def plan_actions(risk: RiskResult, verdict: UsageVerdict, items: Sequence[EvidenceItem], tier: Tier | str, *,
                 config: ActionsConfig | None = None) -> ActionPlan:
    """Column U for one vendor: the playbook for its class, the gap blocks, their Q and C codes, and monitoring.

    ``text`` = the playbook sentences (the questionnaire sentence names the topics), a conflict sentence for rule
    a), one sentence listing the Q and C codes, then monitoring. It stays within ``max_chars``: short topic names
    and then dropping the codes sentence are tried before giving up (the codes stay in the plan itself).
    """
    cfg = config or load_actions()
    tier = Tier(tier)
    citable = sorted((i for i in items if i.citable), key=lambda i: i.item_key)
    key = playbook_key(risk, tier)
    playbook = cfg.playbooks[key]
    blocks = gap_blocks(risk, verdict, citable, config=cfg) if playbook.gap_blocks else []
    questions = _ordered([*playbook.questions, *(q for b in blocks for q in cfg.blocks[b].questions)])
    clauses = _ordered([*playbook.clauses, *(c for b in blocks for c in cfg.blocks[b].clauses)])
    text = cfg.text
    fills = {
        "days": _deadline(key, risk, tier, cfg),
        "provider_flag": text.provider_unresolved if risk.inputs.gaps.get("t2", True) else text.provider_named,
        "ceiling_clause": (text.ceiling_with.format(ceiling=risk.ceiling_class) if risk.ceiling_class
                           else text.ceiling_without),
        "platform_clause": text.platform_clause if any(i.strength == PLATFORM_LABEL for i in citable) else "",
    }
    conflict = [text.conflict] if verdict.conflict and (playbook.questions or blocks) else []
    codes = _codes_sentence(questions, clauses, cfg)
    monitoring = cfg.monitoring[playbook.monitoring]
    for short in (False, True):
        topics = _series(_topics(playbook, blocks, cfg, short))
        sentences = [s.format(topics=topics, **fills) for s in playbook.sentences if topics or "{topics}" not in s]
        for with_codes in (True, False):
            parts = [*sentences, *conflict, *([codes] if codes and with_codes else []), monitoring]
            rendered = " ".join(parts)
            if len(rendered) <= cfg.max_chars:
                return ActionPlan(class_playbook=sentences, gap_blocks=blocks, questionnaire_items=questions,
                                  contract_clauses=clauses, monitoring=monitoring, text=rendered)
    raise ValueError(f"column U for this plan does not fit its budget of {cfg.max_chars} characters")


# --------------------------------------------------------------------------- Method & Legend tables


def questionnaire_table(config: ActionsConfig | None = None) -> list[tuple[str, str, str]]:
    """(code, topic, question) for Q1..Q15."""
    cfg = config or load_actions()
    return [(code, q.topic, q.text) for code, q in cfg.questions.items()]


def clause_table(config: ActionsConfig | None = None) -> list[tuple[str, str, str]]:
    """(code, topic, clause) for C1..C11."""
    cfg = config or load_actions()
    return [(code, c.topic, c.text) for code, c in cfg.clauses.items()]


def gap_block_table(config: ActionsConfig | None = None) -> list[tuple[str, str, str]]:
    """('GAP-SUB', topic, 'Q3, Q14; C2 (trigger: ...)') for every gap block."""
    cfg = config or load_actions()
    return [(f"GAP-{code}", b.topic, f"{', '.join(b.questions)}; {', '.join(b.clauses)} (trigger: {b.trigger})")
            for code, b in cfg.blocks.items()]
