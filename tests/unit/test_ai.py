"""Offline tests for footprint.ai: Gemini back ends, cache, payload guard, host policy, roles and audit log.

No network and no Gemini: the google-genai client is faked, and every other LLM is a scripted fake. The only live
test is marked ``network`` (deselected by default) and sends fictional text only.

The real-data tests read the profile free text (columns D, E, H, J, K) from data/input/Meridian_Vendor_Input.xlsx
and the public passages in runs/*/passages.jsonl. They never print either: failures report ids and reasons only.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from footprint import ai
from footprint.ai import (
    BATCH_SIZE,
    EXPAND_SCHEMA,
    EXTRACT_SCHEMA,
    GEMINI_UNAVAILABLE_NOTE,
    MODEL_CHAIN,
    PROFILE_GUARD_FIELDS,
    SEED,
    TRIAGE_SCHEMA,
    AuditLog,
    CachedLLM,
    GeminiLLM,
    GuardInput,
    HostAiPolicy,
    LLMReply,
    NullLLM,
    PayloadGuard,
    PayloadItem,
    PublicPayload,
    TriageRow,
    Withheld,
    backoff_delay,
    cache_key,
    check_audit,
    expand_names,
    extract_batch_size,
    extract_claims,
    keyword_triage,
    local_names,
    make_llm,
    parse_content_signals,
    prompt_sha256,
    prompt_text,
    render_payload,
    schema_sha256,
    triage,
)
from footprint.models import Claim, Document, Passage, SourceFamily
from footprint.net.robots import RobotsCache
from footprint.net.tou import TouRegister

REPO = Path(__file__).resolve().parents[2]
WORKBOOK = REPO / "data" / "input" / "Meridian_Vendor_Input.xlsx"
RUNS = REPO / "runs"
SEEDS = REPO / "seeds"
COMPANY = "Fernhill Ledger Ltd"   # fictional
TEAM = "Team Kestrel"             # stand-in; the real team name lives only in .env
PRIMARY, FALLBACK = MODEL_CHAIN

TOU = TouRegister({
    "default": {"automation": "limited", "ai_processing_allowed": False},
    "host": [
        {"match": "fernhill.example", "automation": "full", "ai_processing_allowed": True},
        {"match": "press.example", "automation": "full", "ai_processing_allowed": True},
        {"match": "closed.example", "automation": "full", "ai_processing_allowed": False},
    ],
})


# --------------------------------------------------------------------------- helpers


def guard(profile_texts=(), *, team=TEAM, scrub=(), **kw) -> PayloadGuard:
    return PayloadGuard(list(profile_texts), team, list(scrub), **kw)


def doc(doc_id: str, *, url: str = "https://www.fernhill.example/ai", published: str = "2026-05-01",
        family: SourceFamily = SourceFamily.PRD, kind: str = "html") -> Document:
    return Document(doc_id=doc_id, capture_id="c" * 64, vendor_id="V-901", family=family, url=url, title="t",
                    kind=kind, published=published)


def passage(doc_id: str, start: int, text: str) -> Passage:
    end = start + len(text)
    return Passage(passage_id=Passage.make_id(doc_id, start, end), doc_id=doc_id, vendor_id="V-901", start=start,
                   end=end, text=text, hits=["AI"])


def claim(pid: str, quote: str, **kw) -> dict:
    base = {"passage_id": pid, "quote": quote, "claim_kind": "uses_ai", "subject": "vendor_operations",
            "ai_type": "genai_llm", "temporal": "in_production", "action_level": "advisory",
            "named_providers": [], "data_mentioned": [], "indicators": []}
    return {**base, **kw}


class FakeLLM:
    """Scripted LLM: each step is a dict (an ok reply), an LLMReply, or a callable(payload) -> either."""

    name = "fake"

    def __init__(self, *steps, model: str = PRIMARY):
        self.steps = list(steps)
        self.model = model
        self.calls: list[dict] = []

    def generate(self, *, role, prompt, payload, schema):
        self.calls.append({"role": role, "prompt": prompt, "payload": payload, "schema": schema})
        step = self.steps.pop(0) if self.steps else {"claims": [], "items": [], "names": []}
        if callable(step):
            step = step(payload)
        if isinstance(step, LLMReply):
            return step
        return LLMReply(data=step, model=self.model, status="ok", attempts=1)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeModels:
    def __init__(self, script, clock: Clock):
        self.script = list(script)
        self.clock = clock
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config, "t": self.clock()})
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return SimpleNamespace(text=step)


def fake_gemini(script, **kw):
    pytest.importorskip("google.genai")
    clock = Clock()
    models = FakeModels(script, clock)
    llm = GeminiLLM(client=SimpleNamespace(models=models), sleep=clock.sleep, clock=clock, **kw)
    return llm, models, clock


def api_error(code: int, *, status: str = "", quota_id: str = "", retry_delay: str = ""):
    from google.genai import errors

    details: list[dict] = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id, "quotaMetric": "generate_requests"}]})
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    body = {"error": {"code": code, "message": "fake", "status": status, "details": details}}
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, body)


def released_payload(*texts: str) -> PublicPayload:
    """A payload whose items went through a guard (GeminiLLM refuses anything else)."""
    items, withheld = guard().check([GuardInput(id=f"p{i}", kind="PRD html", text=t)
                                     for i, t in enumerate(texts, 1)])
    assert not withheld
    return PublicPayload(company=COMPANY, items=tuple(items))


OK_CLAIMS = json.dumps({"claims": []})


# --------------------------------------------------------------------------- closed payload types


def test_public_payload_is_closed_and_frozen():
    item = PayloadItem(id="P1", kind="PRD html", text="Fernhill uses AI.")
    with pytest.raises(ValueError):
        PayloadItem(id="P1", kind="k", text="t", url="https://x")             # no extra fields
    with pytest.raises(ValueError):
        PublicPayload(company=COMPANY, items=(item,), tier="High")           # no profile, tier or verdict slot
    with pytest.raises(ValueError):
        PayloadItem(id=1, kind="k", text="t")                                # strings only, no coercion
    payload = PublicPayload(company=COMPANY, items=(item,))
    with pytest.raises(ValueError):
        payload.company = "x"
    assert set(PublicPayload.model_fields) == {"company", "items"}
    assert set(PayloadItem.model_fields) == {"id", "kind", "text"}


def test_payload_sha_is_canonical_json():
    a = PublicPayload(company=COMPANY, items=(PayloadItem(id="P1", kind="k", text="é AI"),))
    b = PublicPayload.model_validate({"items": [{"text": "é AI", "kind": "k", "id": "P1"}], "company": COMPANY})
    canon = json.dumps({"company": COMPANY, "items": [{"id": "P1", "kind": "k", "text": "é AI"}]},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert a.sha256() == b.sha256() == hashlib.sha256(canon.encode("utf-8")).hexdigest()


def test_cache_key_is_the_contract_formula():
    expect = hashlib.sha256(b"m|p|s|y|1234").hexdigest()
    assert cache_key("m", "p", "s", "y") == expect == cache_key("m", "p", "s", "y", SEED)
    assert cache_key("m", "p", "s", "y", 7) != expect


def test_schema_sha_is_order_independent():
    assert schema_sha256({"a": 1, "b": [1, 2]}) == schema_sha256({"b": [1, 2], "a": 1})
    assert schema_sha256(EXTRACT_SCHEMA) != schema_sha256(TRIAGE_SCHEMA) != schema_sha256(EXPAND_SCHEMA)


# --------------------------------------------------------------------------- frozen prompts


@pytest.mark.parametrize("name", ["expand_v1", "triage_v1", "extract_v1"])
def test_prompts_are_frozen_files_without_internal_words(name):
    raw = (REPO / "prompts" / f"{name}.txt").read_bytes()
    text = prompt_text(name)
    assert text and b"\r" not in raw, "prompt files are LF-only"
    assert prompt_sha256(name) == hashlib.sha256(raw).hexdigest()
    lowered = text.lower()
    for word in ("meridian", "tier", "tiers", "verdict", "criticality", "risk class", "finding", "findings",
                 "osprey", "kestrel"):
        assert not re.search(rf"\b{word}\b", lowered), f"{name} mentions {word!r}"
    team = os.environ.get("FOOTPRINT_TEAM_NAME", "")
    core = re.sub(r"(?i)^team\s+", "", team).strip()
    if core:
        assert core.lower() not in lowered


def test_extract_prompt_lists_only_defined_indicator_codes():
    codes = set(re.findall(r"\b([GM]\d{1,2})\b", prompt_text("extract_v1")))
    assert codes == {"G1", "G2", "G3", "G4", "G5", "G8", "G11", "M1", "M2", "M3", "M4", "M5", "M6", "M7"}


def test_extract_prompt_few_shot_output_matches_the_schema():
    text = prompt_text("extract_v1")
    example = text[text.rindex("Output:") + len("Output:"):].strip()
    batch = json.loads(example)
    claims = [Claim.model_validate(c) for c in batch["claims"]]
    assert [c.claim_kind for c in claims] == ["generic_ai_marketing", "uses_ai"]   # one marketing, one genuine
    for c in claims:
        assert c.quote in text
        assert all(i.span in c.quote for i in c.indicators)


def test_prompt_name_is_validated():
    with pytest.raises(ValueError):
        prompt_text("../CLAUDE")


# --------------------------------------------------------------------------- GeminiLLM (fake client)


def test_gemini_call_settings_follow_the_contract():
    llm, models, _ = fake_gemini([OK_CLAIMS])
    payload = released_payload("Fernhill Ledger uses machine learning to flag payments.")
    reply = llm.generate(role="extract", prompt="PROMPT", payload=payload, schema=EXTRACT_SCHEMA)
    assert reply.status == "ok" and reply.data == {"claims": []} and reply.model == PRIMARY
    call = models.calls[0]
    cfg = call["config"]
    from google.genai import types

    assert call["model"] == PRIMARY
    assert call["contents"] == render_payload("extract", payload)
    assert cfg.system_instruction == "PROMPT"
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == EXTRACT_SCHEMA
    assert cfg.seed == SEED == 1234
    assert cfg.temperature is None
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert cfg.automatic_function_calling.disable is True
    assert cfg.tools is None and cfg.cached_content is None


def test_gemini_paces_calls_at_least_six_seconds_apart():
    llm, models, _ = fake_gemini([OK_CLAIMS] * 3)
    payload = released_payload("Fernhill Ledger uses machine learning to flag payments.")
    for _ in range(3):
        assert llm.generate(role="extract", prompt="P", payload=payload, schema=EXTRACT_SCHEMA).status == "ok"
    times = [c["t"] for c in models.calls]
    assert all(b - a >= 6.0 for a, b in zip(times, times[1:]))


def test_backoff_schedule_is_2_to_60_seconds():
    assert [backoff_delay(n) for n in (1, 2, 3, 4, 5, 6)] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert backoff_delay(1, 41.0) == 41.0          # a server RetryInfo delay is honoured
    assert backoff_delay(2, 600.0) == 60.0         # but capped


def test_gemini_retries_408_429_and_5xx_then_succeeds():
    script = [api_error(429, status="RESOURCE_EXHAUSTED", quota_id="GenerateRequestsPerMinutePerProject",
                        retry_delay="7s"),
              api_error(503, status="UNAVAILABLE"), api_error(408), OK_CLAIMS]
    llm, models, _ = fake_gemini(script)
    reply = llm.generate(role="extract", prompt="P", payload=released_payload("We use AI."), schema=EXTRACT_SCHEMA)
    assert reply.status == "ok" and reply.attempts == 4 and reply.model == PRIMARY
    times = [c["t"] for c in models.calls]
    assert times[1] - times[0] >= 7.0              # RetryInfo delay
    assert times[3] - times[2] >= 8.0              # third backoff step
    assert llm.switches == []


def test_gemini_gives_up_after_five_attempts():
    llm, models, _ = fake_gemini([api_error(500)] * 5)
    reply = llm.generate(role="extract", prompt="P", payload=released_payload("We use AI."), schema=EXTRACT_SCHEMA)
    assert reply.status == "error" and reply.data is None and reply.attempts == 5 and len(models.calls) == 5


def test_gemini_does_not_retry_a_bad_request():
    llm, models, _ = fake_gemini([api_error(400, status="INVALID_ARGUMENT")])
    reply = llm.generate(role="extract", prompt="P", payload=released_payload("We use AI."), schema=EXTRACT_SCHEMA)
    assert reply.status == "error" and len(models.calls) == 1


def test_daily_quota_switches_to_the_fallback_model():
    script = [api_error(429, status="RESOURCE_EXHAUSTED", quota_id="GenerateRequestsPerDayPerProjectPerModel"),
              OK_CLAIMS, OK_CLAIMS]
    llm, models, _ = fake_gemini(script)
    payload = released_payload("We use AI.")
    reply = llm.generate(role="extract", prompt="P", payload=payload, schema=EXTRACT_SCHEMA)
    assert reply.status == "ok" and reply.model == FALLBACK
    assert llm.switches == [{"from": PRIMARY, "to": FALLBACK, "reason": "daily_quota", "role": "extract"}]
    llm.generate(role="extract", prompt="P", payload=payload, schema=EXTRACT_SCHEMA)
    assert [c["model"] for c in models.calls] == [PRIMARY, FALLBACK, FALLBACK]


@pytest.mark.parametrize("code,status,reason", [(400, "INVALID_ARGUMENT", "API_KEY_INVALID"),
                                                (401, "UNAUTHENTICATED", "")])
def test_an_invalid_key_stops_gemini_without_trying_the_fallback(code, status, reason):
    from google.genai import errors

    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}] if reason else []
    exc = errors.ClientError(code, {"error": {"code": code, "message": "fake", "status": status, "details": details}})
    llm, models, _ = fake_gemini([exc])
    payload = released_payload("We use AI.")
    assert llm.generate(role="extract", prompt="P", payload=payload, schema=EXTRACT_SCHEMA).status == "quota"
    assert llm.generate(role="extract", prompt="P", payload=payload, schema=EXTRACT_SCHEMA).status == "quota"
    assert len(models.calls) == 1
    assert llm.switches == [{"from": PRIMARY, "to": "null", "reason": "api_key_invalid", "role": "extract"}]
    assert llm.exhausted and llm.note == GEMINI_UNAVAILABLE_NOTE


def test_quota_on_every_model_returns_quota_for_the_rest_of_the_run():
    llm, models, _ = fake_gemini([api_error(403, status="PERMISSION_DENIED"),
                                  api_error(429, status="RESOURCE_EXHAUSTED", quota_id="RequestsPerDay")])
    payload = released_payload("We use AI.")
    first = llm.generate(role="extract", prompt="P", payload=payload, schema=EXTRACT_SCHEMA)
    second = llm.generate(role="triage", prompt="P", payload=payload, schema=TRIAGE_SCHEMA)
    assert first.status == second.status == "quota" and first.data is None
    assert len(models.calls) == 2, "no call after the chain is exhausted"
    assert [s["reason"] for s in llm.switches] == ["permission_denied", "daily_quota"]
    assert llm.switches[-1]["to"] == "null"
    assert llm.exhausted and llm.note == GEMINI_UNAVAILABLE_NOTE


def test_gemini_retries_unparseable_output_once_then_skips():
    llm, models, _ = fake_gemini(["not json", OK_CLAIMS])
    payload = released_payload("We use AI.")
    assert llm.generate(role="extract", prompt="P", payload=payload, schema=EXTRACT_SCHEMA).status == "ok"
    llm2, models2, _ = fake_gemini(['{"wrong": []}', "[]", OK_CLAIMS])
    reply = llm2.generate(role="extract", prompt="P", payload=payload, schema=EXTRACT_SCHEMA)
    assert reply.status == "error" and reply.data is None and len(models2.calls) == 2


def test_gemini_refuses_text_that_did_not_pass_the_guard():
    llm, models, _ = fake_gemini([OK_CLAIMS])
    raw = PublicPayload(company=COMPANY, items=(PayloadItem(id="P1", kind="k", text="never guarded text"),))
    with pytest.raises(ValueError, match="payload guard"):
        llm.generate(role="extract", prompt="P", payload=raw, schema=EXTRACT_SCHEMA)
    with pytest.raises(ValueError):
        llm.generate(role="extract", prompt="P", schema=EXTRACT_SCHEMA,
                     payload=PublicPayload(company="Meridian Bank", items=released_payload("We use AI.").items))
    assert models.calls == []


def test_gemini_needs_a_key_and_never_keeps_it(monkeypatch):
    pytest.importorskip("google.genai")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiLLM()
    fake_key = "AIza" + "X" * 35
    llm = GeminiLLM(api_key=fake_key)
    assert all(v != fake_key for v in vars(llm).values())
    assert fake_key not in repr(llm)


# --------------------------------------------------------------------------- CachedLLM, NullLLM, make_llm


def _gen(llm, payload, *, role="extract", prompt="PROMPT", schema=EXTRACT_SCHEMA):
    return llm.generate(role=role, prompt=prompt, payload=payload, schema=schema)


def test_cache_stores_ok_replies_and_replays_them(tmp_path):
    payload = released_payload("We use AI.")
    inner = FakeLLM({"claims": []})
    cached = CachedLLM(inner, tmp_path)
    first = _gen(cached, payload)
    second = _gen(cached, payload)
    assert first.status == "ok" and second.status == "cache_hit" and len(inner.calls) == 1
    key = cache_key(PRIMARY, hashlib.sha256(b"PROMPT").hexdigest(), schema_sha256(EXTRACT_SCHEMA),
                    payload.sha256())
    assert first.cache_key == second.cache_key == key
    path = tmp_path / key[:2] / f"{key}.json"
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert set(entry) == {"key", "model", "role", "prompt_sha256", "schema_sha256", "payload_sha256", "seed",
                          "response"}
    assert entry["response"] == {"claims": []} and entry["seed"] == SEED and entry["model"] == PRIMARY
    raw = path.read_bytes()
    assert b"\r" not in raw and raw == (json.dumps(entry, sort_keys=True, ensure_ascii=False, indent=1)
                                        + "\n").encode("utf-8")
    replay = CachedLLM(None, tmp_path)
    assert _gen(replay, payload).status == "cache_hit"


def test_replay_miss_never_calls_out(tmp_path):
    reply = _gen(CachedLLM(None, tmp_path), released_payload("We use AI."))
    assert reply.status == "cache_miss" and reply.data is None


def test_fallback_model_answers_replay(tmp_path):
    payload = released_payload("We use AI.")
    _gen(CachedLLM(FakeLLM({"claims": []}, model=FALLBACK), tmp_path), payload)
    hit = _gen(CachedLLM(None, tmp_path), payload)
    assert hit.status == "cache_hit" and hit.model == FALLBACK


def test_cache_never_stores_errors_or_forbidden_words(tmp_path):
    payload = released_payload("We use AI.")
    inner = FakeLLM(LLMReply(data=None, status="error", model=PRIMARY), {"claims": [{"quote": "Meridian"}]})
    cached = CachedLLM(inner, tmp_path)
    assert _gen(cached, payload).status == "error"
    assert _gen(cached, payload).status == "error"
    assert not list(tmp_path.rglob("*.json"))


def test_cache_key_covers_prompt_schema_and_payload(tmp_path):
    payload = released_payload("We use AI.")
    inner = FakeLLM({"claims": []}, {"items": []}, {"claims": []}, {"claims": []})
    cached = CachedLLM(inner, tmp_path)
    _gen(cached, payload)
    _gen(cached, payload, schema=TRIAGE_SCHEMA, role="triage")
    _gen(cached, payload, prompt="OTHER")
    _gen(cached, released_payload("We use AI too."))
    assert len(inner.calls) == 4 and len(list(tmp_path.rglob("*.json"))) == 4


def test_null_llm_returns_nothing():
    reply = NullLLM().generate(role="extract", prompt="P", payload=released_payload("x"), schema=EXTRACT_SCHEMA)
    assert reply.status == "null" and reply.data is None and NullLLM().name == "null"


def test_make_llm_by_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    replay = make_llm("replay", cache_dir=tmp_path)
    assert isinstance(replay, CachedLLM) and replay.inner is None
    assert isinstance(make_llm("live_rules"), NullLLM)
    keyless = make_llm("live_ai", cache_dir=tmp_path)
    assert isinstance(keyless, NullLLM) and keyless.note == GEMINI_UNAVAILABLE_NOTE == \
        "Gemini unavailable – rules + local model"
    pytest.importorskip("google.genai")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "Y" * 35)
    live = make_llm("live_ai", cache_dir=tmp_path)
    assert isinstance(live, CachedLLM) and isinstance(live.inner, GeminiLLM)
    with pytest.raises(ValueError):
        make_llm("bogus")


# --------------------------------------------------------------------------- payload guard: content blocks

PROFILE = [
    "Produces and distributes printed account statements and regulatory notices for the whole client base.",
    "Batch scheduling across core systems",           # 5 words: blocked as a whole value
    "Inbound real-time payments",                      # 3 words: never blocked
    "Full customer master and balances for 1.4 million customers.",
    "19 million documents",                            # short, but a volume phrase
    "Not applicable",
]


def _withheld(g: PayloadGuard, *texts: str, url: str = "") -> list[str]:
    sendable, withheld = g.check([GuardInput(id=f"x{i}", kind="PRD html", text=t, url=url)
                                  for i, t in enumerate(texts)])
    assert len(sendable) + len(withheld) == len(texts)
    return [w.reason for w in withheld]


@pytest.mark.parametrize("text", [
    "Meridian uses machine learning.", "We serve MERIDIAN's members.", "a meridian line", "Ｍｅｒｉｄｉａｎ AI",
    "see Meridian_Vendor_Input.xlsx", "Meridian2026 programme", "Meri​dian bank", "#meridian",
])
def test_hard_block_meridian(text):
    assert _withheld(guard(), text) == ["hard_block_meridian"]


@pytest.mark.parametrize("text", ["MeridianLink sells loan software.", "Meridians of longitude."])
def test_meridian_only_as_a_whole_word(text):
    assert _withheld(guard(), text) == []


def test_hard_block_team_name_with_or_without_team_prefix():
    g = guard(team="Team Kestrel")
    assert _withheld(g, "Prepared by Kestrel analysts.", "Team Kestrel slides", "kestrel") == ["hard_block_team"] * 3
    assert _withheld(g, "Kestrels hunt by day.") == []
    assert _withheld(guard(team="Kestrel"), "by Team Kestrel") == ["hard_block_team"]
    short = guard(team="Team A")       # a one-letter core only blocks with its "Team " prefix
    assert _withheld(short, "A model is a tool.", "From team a.") == ["hard_block_team"]


def test_profile_shingles_block_six_word_overlaps_after_normalisation():
    g = guard(PROFILE)
    hit = "Our firm PRODUCES and distributes printed   account statements, quickly."
    assert _withheld(g, hit) == ["profile_shingle"]
    near = "We produce and distribute printed account statements."     # stems match: still six words
    assert _withheld(g, near) == ["profile_shingle"]
    five = "It distributes printed account statements and more."       # only five words shared
    assert _withheld(g, five) == []


def test_profile_values_of_four_or_five_words_block_as_a_whole():
    g = guard(PROFILE)
    assert _withheld(g, "We run batch-scheduling across core systems for banks.") == ["profile_shingle"]
    assert _withheld(g, "We run batch scheduling across many core systems.") == []
    assert _withheld(g, "Inbound real-time payments are growing fast.") == []          # 3-word value
    assert _withheld(g, "This is not applicable to AI.") == []                         # 2-word value


@pytest.mark.parametrize("text", [
    "The bank has 1.4 million customers.", "It serves 1,400,000 customers.", "about 1.4M customers today",
    "for 1.4 million of our customers", "Volumes: 19,000,000 documents a year.", "19m documents were printed",
])
def test_volume_phrases_block_with_normalised_numbers(text):
    assert _withheld(guard(PROFILE), text) == ["volume_phrase"]


@pytest.mark.parametrize("text", [
    "Revenue rose by $1.4 million in 2025.", "1.4 million", "We printed 19 million pages.",
    "It has 14 million customers.", "Call 1-800-555-0199 for 19 million reasons.",
])
def test_numbers_alone_or_with_other_words_are_not_volume_phrases(text):
    assert _withheld(guard(PROFILE), text) == []


def test_enumerated_and_public_values_never_block():
    g = guard(PROFILE + ["Payments", "High", "Core Platform", "www.fiserv.com", "Professional Services"])
    texts = ["Payments are high on the agenda for our Core Platform.", "Visit www.fiserv.com today.",
             "Professional Services teams use AI.", "Fernhill Ledger Ltd uses AI."]
    assert _withheld(g, *texts) == []


def test_allow_shingles_need_a_written_reason():
    allowed = {"distributes printed account statements and regulatory notices":
               "appears on the vendor's own public services page"}
    g = guard(PROFILE, allow_shingles=allowed)
    assert _withheld(g, "It distributes printed account statements and regulatory notices.") == []
    assert _withheld(g, "Produces and distributes printed account statements.") == ["profile_shingle"]
    with pytest.raises(ValueError, match="reason"):
        guard(PROFILE, allow_shingles={"batch scheduling across core systems": " "})


def test_guard_rejects_anything_but_guard_inputs():
    with pytest.raises(TypeError):
        guard().check([PayloadItem(id="P1", kind="k", text="t")])


# --------------------------------------------------------------------------- payload guard: redaction


def test_emails_phones_and_people_are_redacted_and_restored():
    g = guard(scrub=["Ada Quill", "Bo Rinn"])
    text = ("Contact ada.quill@fernhill.example or +44 20 7946 0958. Ada Quill said the model is live; "
            "ADA QUILL and Bo  Rinn agreed. Call (212) 555-0147 or ada.quill@fernhill.example again.")
    (item,), withheld = g.check([GuardInput(id="P9", kind="PRD html", text=text)])
    assert not withheld
    assert item.text == ("Contact [EMAIL_1] or [PHONE_1]. [PERSON_1] said the model is live; "
                         "[PERSON_2] and [PERSON_3] agreed. Call [PHONE_2] or [EMAIL_1] again.")
    assert g.placeholders("P9") == {"[EMAIL_1]": "ada.quill@fernhill.example", "[PHONE_1]": "+44 20 7946 0958",
                                    "[PERSON_1]": "Ada Quill", "[PERSON_2]": "ADA QUILL", "[PERSON_3]": "Bo  Rinn",
                                    "[PHONE_2]": "(212) 555-0147"}
    quote = "[PERSON_1] said the model is live; [PERSON_2] and [PERSON_3] agreed."
    restored = g.restore("P9", quote)
    assert restored == "Ada Quill said the model is live; ADA QUILL and Bo  Rinn agreed." and restored in text
    assert g.restore("P9", "[EMAIL_7] unknown stays") == "[EMAIL_7] unknown stays"
    assert g.restore("nope", "[EMAIL_1]") == "[EMAIL_1]" and g.placeholders("nope") == {}


def test_placeholder_numbering_restarts_for_each_item():
    g = guard()
    items, _ = g.check([GuardInput(id="a", kind="k", text="mail x@y.example"),
                        GuardInput(id="b", kind="k", text="mail z@y.example")])
    assert [i.text for i in items] == ["mail [EMAIL_1]", "mail [EMAIL_1]"]
    assert g.restore("b", "[EMAIL_1]") == "z@y.example"


@pytest.mark.parametrize("text", [
    "Filed 2026-10-02 on Form 10-K (CIK 0000798354).", "Version 24.4.1 shipped in Q3 2025.",
    "Revenue was $1,234,567 and 12.5% growth.", "Call us 24/7.",
])
def test_redaction_leaves_dates_amounts_and_ids_alone(text):
    (item,), _ = guard().check([GuardInput(id="z", kind="k", text=text)])
    assert item.text == text


# --------------------------------------------------------------------------- host policy


class FakeRobots:
    def __init__(self, body: str):
        self.cache = RobotsCache(lambda url: (200, body.encode("utf-8")))
        self.body_bytes = body.encode("utf-8")
        self.asked: list[tuple[str, str]] = []

    def allowed(self, url, ua="footprint-osint"):
        self.asked.append((url, ua))
        return self.cache.allowed(url, ua)

    def body(self, url):
        return self.body_bytes


def test_host_policy_refuses_hosts_the_tou_register_refuses(tmp_path):
    robots = FakeRobots("User-agent: *\nAllow: /\n")
    policy = HostAiPolicy(TOU, tmp_path / "ai_policy.jsonl", robots=robots)
    assert policy("https://www.fernhill.example/a") == (True, "")
    assert policy("https://closed.example/a") == (False, "host_ai_disallowed")
    assert policy("https://unlisted.example/a") == (False, "host_ai_disallowed")        # default entry
    assert policy("https://www.fiserv.com/en/x.html") == (False, "host_ai_disallowed")  # hard manual host
    assert policy("not a url") == (False, "host_ai_disallowed")
    assert [u for u, _ in robots.asked] == ["https://www.fernhill.example/a"]


def test_host_policy_reads_google_extended_and_content_signals(tmp_path):
    path = tmp_path / "ai_policy.jsonl"
    ge = HostAiPolicy(TOU, path, robots=FakeRobots("User-agent: Google-Extended\nDisallow: /\n\n"
                                                    "User-agent: *\nAllow: /\n"))
    assert ge("https://www.fernhill.example/a") == (False, "robots_google_extended")
    cs = HostAiPolicy(TOU, path, robots=FakeRobots("User-agent: *\nContent-Signal: search=yes, ai-input=no\n"
                                                    "Allow: /\n"))
    assert cs("https://press.example/a") == (False, "content_signal")
    records = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [(r["host"], r["allowed"], r["reason"]) for r in records] == [
        ("www.fernhill.example", False, "robots_google_extended"), ("press.example", False, "content_signal")]
    assert all(set(r) == {"host", "url", "allowed", "reason", "robots_sha256", "decided_on"} for r in records)
    assert all(len(r["robots_sha256"]) == 64 for r in records)


def test_host_policy_falls_back_to_the_robots_fetch_for_content_signal(tmp_path):
    body = b"User-agent: *\nContent-Signal: ai-train=no\nAllow: /\n"
    fetched: list[str] = []

    def fetch_raw(url):
        fetched.append(url)
        return 200, body

    policy = HostAiPolicy(TOU, tmp_path / "p.jsonl", robots=RobotsCache(fetch_raw))
    assert policy("https://press.example/a") == (False, "content_signal")
    assert policy("https://press.example/b") == (False, "content_signal")
    assert fetched.count("https://press.example/robots.txt") <= 2


def test_replay_reproduces_recorded_decisions(tmp_path):
    path = tmp_path / "ai_policy.jsonl"
    live = HostAiPolicy(TOU, path, robots=FakeRobots("User-agent: Google-Extended\nDisallow: /private\n"))
    assert live("https://www.fernhill.example/private/x") == (False, "robots_google_extended")
    assert live("https://www.fernhill.example/public") == (True, "")
    replay = HostAiPolicy(TOU, path)
    assert replay("https://www.fernhill.example/private/x") == (False, "robots_google_extended")
    assert replay("https://www.fernhill.example/public") == (True, "")
    assert replay("https://www.fernhill.example/other") == (False, "robots_google_extended")  # mixed host: closed
    assert replay("https://press.example/a") == (True, "")                                  # no record: ToS only
    assert replay("https://closed.example/a") == (False, "host_ai_disallowed")


def test_replay_uses_the_decisions_in_force_on_as_of(tmp_path):
    """A later live run that records a new refusal never rewrites the replay of an earlier as_of: the payload, the
    cache key and the cells of that run stay identical (review finding: replay ignored as_of)."""
    path = tmp_path / "ai_policy.jsonl"
    url, other = "https://www.fernhill.example/blog/x", "https://www.fernhill.example/blog/y"
    records = [
        {"host": "www.fernhill.example", "url": url, "allowed": True, "reason": "", "robots_sha256": "a" * 64,
         "decided_on": "2026-10-03"},
        {"host": "www.fernhill.example", "url": url, "allowed": False, "reason": "content_signal",
         "robots_sha256": "b" * 64, "decided_on": "2026-10-20"},
        {"host": "www.fernhill.example", "url": other, "allowed": False, "reason": "robots_google_extended",
         "robots_sha256": "b" * 64, "decided_on": "2026-10-20"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    early = HostAiPolicy(TOU, path, as_of="2026-10-03")
    assert early(url) == (True, "")
    assert early(other) == (True, "")                       # its refusal came later: no record yet on 2026-10-03
    assert early("https://www.fernhill.example/z") == (True, "")
    late = HostAiPolicy(TOU, path, as_of="2026-10-20")
    assert late(url) == (False, "content_signal")
    assert late("https://www.fernhill.example/z")[0] is False                   # mixed host: closed
    assert HostAiPolicy(TOU, path)(url) == (False, "content_signal")             # no as_of: every record
    assert HostAiPolicy(TOU, path, as_of="2026-10-01")(url) == (True, "")        # before any record: ToS only


def test_live_decisions_are_recorded_once(tmp_path):
    path = tmp_path / "ai_policy.jsonl"
    for _ in range(2):
        policy = HostAiPolicy(TOU, path, robots=FakeRobots("User-agent: *\nAllow: /\n"))
        policy("https://www.fernhill.example/a")
        policy("https://www.fernhill.example/a")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_parse_content_signals():
    body = "# Content-Signal: ai-train=no\nUser-agent: *\ncontent-signal: search=yes,ai-train=yes, AI-Input = No\n"
    assert parse_content_signals(body.encode()) == {"search": "yes", "ai-train": "yes", "ai-input": "no"}
    assert parse_content_signals(b"\xef\xbb\xbfContent-Signal: ai-train=no") == {"ai-train": "no"}
    assert parse_content_signals(b"") == {}


def test_guard_applies_the_host_policy_and_fails_closed():
    calls: list[str] = []

    def policy(url):
        calls.append(url)
        return (url.startswith("https://ok.example"), "" if url.startswith("https://ok.example") else
                "robots_google_extended")

    g = guard(host_policy=policy)
    assert _withheld(g, "We use AI.", url="https://ok.example/a") == []
    assert _withheld(g, "We use AI.", url="https://bad.example/a") == ["robots_google_extended"]
    assert _withheld(g, "We use AI.", url="") == ["host_ai_disallowed"]           # no URL: closed
    odd = guard(host_policy=lambda url: (False, "something else"))
    assert _withheld(odd, "We use AI.", url="https://x.example/") == ["host_ai_disallowed"]
    # fiserv.com and linkedin.com are refused even without a host policy (CLAUDE.md hard rule)
    assert _withheld(guard(), "We use AI.", url="https://newsroom.fiserv.com/x") == ["host_ai_disallowed"]
    assert _withheld(guard(), "We use AI.", url="https://www.linkedin.com/posts/x") == ["host_ai_disallowed"]


def test_content_blocks_are_checked_before_the_host_policy():
    asked: list[str] = []
    g = guard(host_policy=lambda url: (asked.append(url) or True, ""))
    assert _withheld(g, "Meridian AI", url="https://ok.example/a") == ["hard_block_meridian"]
    assert asked == []


# --------------------------------------------------------------------------- real profile values vs real passages


def _real_profile_texts() -> list[str]:
    from footprint.workbook import read_workbook

    if not WORKBOOK.exists():
        pytest.skip("workbook not present")
    wb = read_workbook(WORKBOOK)
    rows = ([wb.example] if wb.example else []) + list(wb.vendors)
    return [getattr(p, f) for p in rows for f in PROFILE_GUARD_FIELDS if getattr(p, f).strip()]


def _real_rows():
    from footprint.workbook import read_workbook

    return read_workbook(WORKBOOK).vendors


def _real_passages() -> list[Passage]:
    files = sorted(RUNS.glob("*/passages.jsonl"))
    if not files:
        pytest.skip("no collection runs present")
    out: list[Passage] = []
    for f in files:
        out += [Passage.model_validate_json(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def _real_docs() -> dict[str, Document]:
    docs: dict[str, Document] = {}
    for f in sorted(RUNS.glob("*/manifest.json")):
        for d in json.loads(f.read_text(encoding="utf-8")).get("documents", []):
            docs[d["doc_id"]] = Document.model_validate(d)
    return docs


def _real_scrub_names() -> list[str]:
    import tomllib

    names: list[str] = []
    for f in sorted(SEEDS.glob("V-*.toml")):
        names += tomllib.loads(f.read_text(encoding="utf-8")).get("person_scrub", [])
    return names


def _team_name() -> str:
    return os.environ.get("FOOTPRINT_TEAM_NAME") or TEAM


def test_real_profile_values_withhold_no_real_public_passage():
    g = PayloadGuard(_real_profile_texts(), _team_name(), _real_scrub_names())
    passages = _real_passages()
    sendable, withheld = g.check([GuardInput(id=p.passage_id, kind="text", text=p.text) for p in passages])
    assert len(passages) > 500
    assert [(w.id, w.reason) for w in withheld] == [], "false withholdings (ids and reasons only)"
    assert len(sendable) == len(passages)


def test_real_profile_values_are_caught_inside_real_passages():
    texts = _real_profile_texts()
    g = PayloadGuard(texts, _team_name())
    base = _real_passages()[:50]
    long_values = [t for t in texts if len(t.split()) >= 4]
    assert len(long_values) >= 20
    probes = [GuardInput(id=f"s{i}", kind="text", text=f"{base[i % len(base)].text} {value}")
              for i, value in enumerate(long_values)]
    _, withheld = g.check(probes)
    assert len(withheld) == len(probes)
    assert {w.reason for w in withheld} <= {"profile_shingle", "hard_block_meridian", "volume_phrase"}


def test_real_profile_shingles_from_inside_a_value_are_caught_after_recasing():
    texts = _real_profile_texts()
    g = PayloadGuard(texts, _team_name())
    base = _real_passages()[:50]
    probes = []
    for i, value in enumerate(t for t in texts if len(t.split()) >= 6):
        words = value.split()
        mid = (len(words) - 6) // 2                    # a window near the middle, not the start of the value,
        starts = sorted(range(len(words) - 5), key=lambda k: (abs(k - mid), k))
        # ... that does not cut a number expression ("for 1.4" out of "for 1.4 million" is another number)
        k = next((k for k in starts if not any(ch.isdigit() for w in words[max(0, k - 1):k + 7] for ch in w)), None)
        if k is None:
            continue
        window = " ".join(words[k:k + 6])
        probes.append(GuardInput(id=f"m{i}", kind="text",
                                 text=f"{base[i % len(base)].text} ({window.upper()}), as stated."))
    assert len(probes) >= 15
    _, withheld = g.check(probes)
    assert len(withheld) == len(probes)
    assert {w.reason for w in withheld} <= {"profile_shingle", "hard_block_meridian", "volume_phrase"}


def test_real_volume_phrases_are_caught_with_reformatted_numbers():
    texts = _real_profile_texts()
    g = PayloadGuard(texts, _team_name())
    phrases = sorted({ph for t in texts for ph in ai._volume_phrases(t)})
    assert len(phrases) >= 8
    probes = []
    for i, (value, words) in enumerate(phrases):
        number = int(Decimal(value))
        probes.append(GuardInput(id=f"v{i}", kind="text", text=f"In total {number:,} {' '.join(words)} were seen."))
    _, withheld = g.check(probes)
    assert [w.reason for w in withheld] == ["volume_phrase"] * len(probes)


def test_real_enumerated_values_never_block():
    g = PayloadGuard(_real_profile_texts(), _team_name())
    probes = [GuardInput(id=p.vendor_id, kind="text",
                         text=f"{p.name} ({p.website}) offers {p.category}. Dependency: {p.operational_dependency}.")
              for p in _real_rows()]
    sendable, withheld = g.check(probes)
    assert withheld == [] and len(sendable) == len(probes) == 6


def test_real_tou_register_withholds_exactly_the_refused_hosts(tmp_path):
    from footprint.net.tou import load_tou

    tou = load_tou()
    docs = _real_docs()
    passages = [p for p in _real_passages() if p.doc_id in docs]
    policy = HostAiPolicy(tou, tmp_path / "none.jsonl")           # replay: ToS register only
    g = PayloadGuard(_real_profile_texts(), _team_name(), host_policy=policy)
    _, withheld = g.check([GuardInput(id=p.passage_id, kind="text", text=p.text, url=docs[p.doc_id].url)
                           for p in passages])
    expected = {p.passage_id for p in passages if not tou.entry_for(docs[p.doc_id].url).ai_processing_allowed}
    assert {w.id for w in withheld} == expected
    assert {w.reason for w in withheld} <= {"host_ai_disallowed"}
    assert 0 < len(expected) < len(passages)


# --------------------------------------------------------------------------- extract_claims


def _docs_and_passages(n: int = 30) -> tuple[dict[str, Document], list[Passage]]:
    docs = {f"d{k}": doc(f"d{k}") for k in range(3)}
    passages = [passage(f"d{i % 3}", 1000 * i, f"Fernhill Ledger uses AI model number {i} in support.")
                for i in range(n)]
    return docs, passages[::-1]         # reversed: extract_claims must sort


def test_extract_claims_batches_in_document_order_with_short_ids():
    docs, passages = _docs_and_passages(30)
    llm = FakeLLM()
    assert extract_claims(llm, passages, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901") == []
    sent = [[i.id for i in c["payload"].items] for c in llm.calls]
    assert [len(b) for b in sent] == [12, 12, 6]
    assert sent[0][:3] == ["P1", "P2", "P3"]
    texts = [i.text for c in llm.calls for i in c["payload"].items]
    expected = [p.text for p in sorted(passages, key=lambda p: (p.doc_id, p.start))]
    assert texts == expected
    first = llm.calls[0]
    assert first["role"] == "extract" and first["prompt"] == prompt_text("extract_v1")
    assert first["schema"] == EXTRACT_SCHEMA and first["payload"].company == COMPANY
    assert first["payload"].items[0].kind == "PRD html, 2026-05-01"


def test_extract_claims_honours_batch_size_and_char_budget():
    docs, passages = _docs_and_passages(30)
    llm = FakeLLM()
    extract_claims(llm, passages, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901", batch_size=25)
    assert [len(c["payload"].items) for c in llm.calls] == [25, 5]
    llm2 = FakeLLM()
    extract_claims(llm2, passages[:4], company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901",
                   max_batch_chars=110)
    assert [len(c["payload"].items) for c in llm2.calls] == [2, 2]


def test_extract_batch_size_is_12_or_25_when_the_budget_is_tight():
    assert ai.BATCH_SIZE == 12 and ai.BATCH_SIZE_TIGHT == 25
    assert extract_batch_size([500] * 30, None) == 12             # no budget: never tight
    assert extract_batch_size([500] * 30, 3) == 12                # 12+12+6 fits in 3 calls
    assert extract_batch_size([500] * 30, 2) == 25                # 3 calls at 12 > 2: go to 25
    assert extract_batch_size([500] * 60, 2) == 25                # still over: 25 is the ceiling
    assert extract_batch_size([], 0) == 12
    assert extract_batch_size([30_000] * 6, 3, max_chars=100_000) == 12   # 2 calls of 3 chunks: fits
    assert extract_batch_size([30_000] * 6, 1, max_chars=100_000) == 12   # chars decide; 25 would not save a call
    with pytest.raises(ValueError):
        extract_batch_size([1], -1)


def test_extract_claims_picks_the_batch_size_from_the_budget_when_asked():
    docs, passages = _docs_and_passages(30)
    llm = FakeLLM()
    extract_claims(llm, passages, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901", batch_size=None,
                   max_calls=2)
    assert [len(c["payload"].items) for c in llm.calls] == [25, 5]
    llm2 = FakeLLM()
    extract_claims(llm2, passages, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901", batch_size=None,
                   max_calls=5)
    assert [len(c["payload"].items) for c in llm2.calls] == [12, 12, 6]
    # the size is chosen on what the guard released, not on what came in
    leaks = [passage("d0", 50_000 + k, f"Meridian item {k} uses AI.") for k in range(20)]
    llm3 = FakeLLM()
    extract_claims(llm3, passages[:12] + leaks, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901",
                   batch_size=None, max_calls=1)
    assert [len(c["payload"].items) for c in llm3.calls] == [12]


def test_extract_claims_maps_ids_back_and_keeps_unknown_and_hallucinated_claims():
    docs, passages = _docs_and_passages(3)
    ordered = sorted(passages, key=lambda p: (p.doc_id, p.start))
    genuine = claim("P2", ordered[1].text[:40].rstrip(), named_providers=["OpenAI", "https://openai.com"])
    hallucinated = claim("P1", "Fernhill Ledger replaced every analyst with an autonomous agent.")
    unknown = claim("P99", ordered[0].text)
    bracketed = claim("[P3]", ordered[2].text)
    bad = claim("P1", ordered[0].text, temporal="someday")                  # schema-invalid claim
    llm = FakeLLM({"claims": [genuine, hallucinated, unknown, bad, bracketed]})
    audit = AuditLog(None)
    out = extract_claims(llm, passages, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901", audit=audit)
    assert [c.passage_id for c in out] == [ordered[1].passage_id, ordered[0].passage_id, "P99",
                                           ordered[2].passage_id]
    assert out[0].named_providers == ["OpenAI"]                              # URL-like names dropped
    by_id = {p.passage_id: p for p in passages}
    verified = [c.quote in by_id[c.passage_id].text if c.passage_id in by_id else False for c in out]
    assert verified == [True, False, False, True]                            # V1/V2 will reject the two
    (rec,) = [r for r in audit.records if r["status"] != "withheld"]
    assert rec["n_out"] == 4 and rec["n_dropped"] == 1 and rec["status"] == "ok"
    assert rec["items_sent"] == [p.passage_id for p in ordered]
    assert rec["hosts"] == ["www.fernhill.example"]
    assert audit.model_for(ordered[0].passage_id) == PRIMARY and audit.model_for("unknown") == ""


def test_extract_claims_skips_a_failed_batch_and_continues():
    docs, passages = _docs_and_passages(30)
    ordered = sorted(passages, key=lambda p: (p.doc_id, p.start))
    good = {"claims": [claim("P1", ordered[24].text)]}
    llm = FakeLLM(LLMReply(data=None, status="error", model=PRIMARY), {"wrong": 1}, good)
    audit = AuditLog(None)
    out = extract_claims(llm, passages, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901", audit=audit)
    assert [c.passage_id for c in out] == [ordered[24].passage_id]
    assert [r["status"] for r in audit.records] == ["error", "ok", "ok"]
    assert [r["n_out"] for r in audit.records] == [0, 0, 1]


def test_extract_claims_withholds_without_dropping_the_batch(tmp_path):
    docs, passages = _docs_and_passages(4)
    leak = passage("d0", 99_999, "Meridian Bank uses Fernhill Ledger AI for 1.4 million customers.")
    profile = passage("d1", 99_999, "We run batch scheduling across core systems with AI.")
    volume = passage("d2", 99_999, "Fernhill AI processes 1,400,000 customers.")
    llm = FakeLLM()
    audit = AuditLog(tmp_path / "llm_calls.jsonl", now=lambda: "2026-10-03T00:00:00Z")
    extract_claims(llm, passages + [leak, profile, volume], company=COMPANY, guard=guard(PROFILE), docs=docs,
                   vendor_id="V-901", audit=audit)
    sent_texts = [i.text for c in llm.calls for i in c["payload"].items]
    assert len(sent_texts) == 4 and not any("Meridian" in t or "1,400,000" in t for t in sent_texts)
    withheld = [r for r in audit.records if r["status"] == "withheld"]
    assert sorted((r["withheld"][0]["id"], r["withheld"][0]["reason"]) for r in withheld) == sorted([
        (leak.passage_id, "hard_block_meridian"), (profile.passage_id, "profile_shingle"),
        (volume.passage_id, "volume_phrase")])
    raw = (tmp_path / "llm_calls.jsonl").read_text(encoding="utf-8")
    assert "Fernhill" not in raw and "customers" not in raw and "Meridian" not in raw
    assert check_audit(tmp_path / "llm_calls.jsonl", tou=TOU) == []


def test_extract_claims_respects_the_call_budget():
    docs, passages = _docs_and_passages(30)
    llm = FakeLLM()
    audit = AuditLog(None)
    extract_claims(llm, passages, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901", audit=audit,
                   max_calls=1)
    assert len(llm.calls) == 1
    assert [r["status"] for r in audit.records] == ["ok", "budget", "budget"]
    assert [r["n_skipped"] for r in audit.records] == [0, 12, 6]
    assert audit.records[1]["items_sent"] == []


def test_extract_claims_redacts_and_the_guard_restores_quotes():
    docs = {"d0": doc("d0")}
    text = "Write to ai.desk@fernhill.example: Fernhill uses a chatbot that answers billing questions."
    p = passage("d0", 0, text)
    g = guard()
    llm = FakeLLM(lambda payload: {"claims": [claim("P1", payload.items[0].text)]})
    (c,) = extract_claims(llm, [p], company=COMPANY, guard=g, docs=docs, vendor_id="V-901")
    assert "[EMAIL_1]" in c.quote and "ai.desk@" not in llm.calls[0]["payload"].items[0].text
    assert g.placeholders(p.passage_id) == {"[EMAIL_1]": "ai.desk@fernhill.example"}
    assert g.restore(c.passage_id, c.quote) == text


def test_claims_feed_verify_hallucinations_and_unknown_ids_fail_redacted_quotes_pass():
    """End to end with the real verify gates: what extract_claims returns is what V1/V2 judge."""
    from footprint import verify

    docs = {"d0": doc("d0")}
    text = ("Questions go to ai.desk@fernhill.example. Fernhill Ledger uses a machine learning model to flag "
            "unusual payments, and Ada Quill's team reviews every flag before an account is frozen.")
    p = passage("d0", 0, text)
    g = guard(scrub=["Ada Quill"])

    def answer(payload):
        sent = payload.items[0].text
        genuine = sent[sent.index("Fernhill"):]
        return {"claims": [claim("P1", genuine), claim("P1", "Fernhill Ledger lets an autonomous agent freeze "
                                                          "accounts with no human review at all."),
                           claim("P7", genuine)]}

    out = extract_claims(FakeLLM(answer), [p], company=COMPANY, guard=g, docs=docs, vendor_id="V-901")
    assert "[PERSON_1]" in out[0].quote and "Ada Quill" not in out[0].quote
    by_id = {p.passage_id: p}
    results = [verify.verify_claim(c, by_id.get(c.passage_id), text, title="t",
                                   placeholders=g.placeholders(c.passage_id), indicator_ok=lambda code, span: True)
               for c in out]
    assert [r.ok for r in results] == [True, False, False]
    assert results[0].excerpt == text[text.index("Fernhill"):] and results[0].start == text.index("Fernhill")
    assert results[1].failures[0] == "V2"           # hallucinated quote
    assert results[2].failures == ["V1"]            # an id that was never sent


def test_extract_claims_with_null_llm_sends_nothing():
    docs, passages = _docs_and_passages(5)
    audit = AuditLog(None)
    assert extract_claims(NullLLM(), passages, company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901",
                          audit=audit) == []
    assert audit.records == []


def test_extract_claims_refuses_a_forbidden_company_name():
    docs, passages = _docs_and_passages(1)
    with pytest.raises(ValueError):
        extract_claims(FakeLLM(), passages, company="Meridian", guard=guard(), docs=docs, vendor_id="V-901")


def test_extract_claims_passage_without_document_is_refused_by_a_host_policy():
    docs, passages = _docs_and_passages(2)
    orphan = passage("missing", 0, "Fernhill uses AI to route tickets.")
    llm = FakeLLM()
    g = guard(host_policy=lambda url: (True, ""))
    extract_claims(llm, passages + [orphan], company=COMPANY, guard=g, docs=docs, vendor_id="V-901")
    assert [i.text for c in llm.calls for i in c["payload"].items] == [
        p.text for p in sorted(passages, key=lambda p: (p.doc_id, p.start))]


# --------------------------------------------------------------------------- triage


ROWS = [
    TriageRow(id="doc-a", path="/about/leadership", title="Leadership", date="2026-01-02", family="PRD",
              url="https://www.fernhill.example/about/leadership"),
    TriageRow(id="doc-b", path="/legal/subprocessors", title="Sub-processors", family="LEG",
              url="https://www.fernhill.example/legal/subprocessors"),
    TriageRow(id="doc-c", path="/blog/ai-assistant-launch", title="Our AI assistant is live", date="2026-05-01",
              family="PRD", url="https://www.fernhill.example/blog/ai-assistant-launch"),
    TriageRow(id="doc-d", path="/careers/ml-engineer", title="Machine Learning Engineer", family="JOB",
              url="https://www.fernhill.example/careers/ml-engineer"),
]


def test_triage_returns_sent_ids_high_first_then_medium_in_input_order():
    reply = {"items": [{"id": "R4", "priority": "medium", "reason": "ai_job_duties"},
                       {"id": "R3", "priority": "high", "reason": "ai_in_products"},
                       {"id": "R2", "priority": "high", "reason": "subprocessors_or_data"},
                       {"id": "R77", "priority": "high", "reason": "ai_policy"},
                       {"id": "R2", "priority": "medium", "reason": "ai_policy"},
                       {"id": "R1", "priority": "urgent", "reason": "ai_policy"}]}
    llm = FakeLLM(reply)
    audit = AuditLog(None)
    out = triage(llm, ROWS, company=COMPANY, guard=guard(), vendor_id="V-901", audit=audit)
    assert out == ["doc-b", "doc-c", "doc-d"]
    payload = llm.calls[0]["payload"]
    assert [i.id for i in payload.items] == ["R1", "R2", "R3", "R4"]
    assert payload.items[1].text == "/legal/subprocessors | Sub-processors |  | LEG"
    assert "fernhill.example" not in render_payload("triage", payload)
    assert llm.calls[0]["schema"] == TRIAGE_SCHEMA and llm.calls[0]["prompt"] == prompt_text("triage_v1")
    assert audit.records[0]["n_out"] == 3 and audit.records[0]["n_dropped"] == 2


def test_triage_never_sends_withheld_rows():
    rows = ROWS + [TriageRow(id="doc-e", path="/clients/meridian", title="Meridian case study", family="PRD",
                             url="https://www.fernhill.example/clients/meridian")]
    llm = FakeLLM({"items": [{"id": "R5", "priority": "high", "reason": "ai_in_products"}]})
    out = triage(llm, rows, company=COMPANY, guard=guard(), vendor_id="V-901")
    assert len(llm.calls[0]["payload"].items) == 4 and "doc-e" not in out


def test_triage_fallback_ranks_unsent_rows_locally_when_asked():
    llm = FakeLLM(LLMReply(data=None, status="quota"))
    assert triage(llm, ROWS, company=COMPANY, guard=guard(), vendor_id="V-901") == []
    llm2 = FakeLLM(LLMReply(data=None, status="quota"))
    assert triage(llm2, ROWS, company=COMPANY, guard=guard(), vendor_id="V-901", fallback=True) == \
        keyword_triage(ROWS)


def test_keyword_triage_without_gemini():
    out = triage(NullLLM(), ROWS, company=COMPANY, guard=guard(), vendor_id="V-901")
    assert out == keyword_triage(ROWS)
    assert out[0] == "doc-c" and "doc-a" not in out and set(out) == {"doc-b", "doc-c", "doc-d"}


# --------------------------------------------------------------------------- expand_names


def test_expand_names_returns_names_only():
    reply = {"names": [{"name": "Ledger Copilot", "kind": "ai_programme"},
                       {"name": "labarum.ai", "kind": "affiliate"},
                       {"name": "https://fernhill.example/ai", "kind": "product"},
                       {"name": "www.fernhill.example", "kind": "alias"},
                       {"name": "  Fernhill   Treasury ", "kind": "product"},
                       {"name": "fernhill treasury", "kind": "product"},
                       {"name": "x" * 200, "kind": "product"},
                       {"name": "Bad kind", "kind": "nickname"}]}
    llm = FakeLLM(reply)
    audit = AuditLog(None)
    out = expand_names(llm, COMPANY, ["Fernhill Ledger | AI for treasury", "Products: Ledger Copilot"],
                       guard=guard(), vendor_id="V-901", audit=audit, url="https://www.fernhill.example/",
                       aliases=["Fernhill", "Fernhill Ledger"])
    assert out == ["Fernhill", "Fernhill Ledger", "Ledger Copilot", "Fernhill Treasury"]
    assert llm.calls[0]["schema"] == EXPAND_SCHEMA and llm.calls[0]["role"] == "expand"
    assert audit.records[0]["hosts"] == ["www.fernhill.example"]


def test_expand_names_keyless_fallback_uses_aliases_and_ngrams_near_ai_terms():
    texts = ["Fernhill Ledger launches Ledger Copilot, a generative AI assistant for Treasury Teams.",
             "About Us | Careers | Contact"]
    out = expand_names(NullLLM(), COMPANY, texts, guard=guard(), vendor_id="V-901", aliases=["Fernhill"])
    assert out[0] == "Fernhill"
    assert "Ledger Copilot" in out and "Treasury Teams" in out
    assert "Careers" not in out and "AI" not in out


def test_local_names_ignores_text_without_ai_terms():
    assert local_names(["Welcome to Fernhill Ledger, Home Of Payments."]) == []


# --------------------------------------------------------------------------- audit log


def test_check_audit_flags_text_forbidden_words_reasons_and_refused_hosts(tmp_path):
    path = tmp_path / "llm_calls.jsonl"
    audit = AuditLog(path, now=lambda: "2026-10-03T00:00:00Z")
    ok = LLMReply(data={}, status="ok", model=PRIMARY)
    audit.record(vendor_id="V-901", role="extract", reply=ok, prompt_sha256="a" * 64, schema_sha256="b" * 64,
                 payload_sha256="c" * 64, items_sent=["p1"], withheld=[], n_out=0, hosts=["press.example"])
    audit.record_withheld(vendor_id="V-901", role="extract", withheld=[Withheld(id="p2", reason="volume_phrase")])
    assert check_audit(path, tou=TOU) == []
    lines = [
        {"ts": "t", "status": "ok", "text": "a passage"},
        {"ts": "t", "status": "ok", "quote": "x"},
        {"ts": "t", "status": "withheld", "withheld": [{"id": "p3"}]},
        {"ts": "t", "status": "ok", "items_sent": ["p4"], "hosts": ["closed.example"]},
        {"ts": "t", "status": "ok", "items_sent": ["p5"]},
        {"ts": "t", "status": "ok", "model": "Meridian"},
        {"ts": "t", "status": "ok", "model": "AIza" + "Z" * 35},
    ]
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for rec in lines:
            fh.write(json.dumps(rec) + "\n")
        fh.write("not json\n")
    problems = check_audit(path, tou=TOU)
    joined = "\n".join(problems)
    for n, needle in [(3, "text"), (4, "quote"), (5, "reason"), (6, "closed.example"), (7, "hosts"),
                      (8, "Meridian"), (9, "API key"), (10, "JSON")]:
        assert re.search(rf"line {n}: .*{re.escape(needle)}", joined), (n, needle, problems)
    assert check_audit(tmp_path / "absent.jsonl", tou=TOU) == []


def test_check_audit_catches_aq_keys_and_configured_secret_values(tmp_path, monkeypatch):
    """The release gate covers the AQ.-format Google key (bundle.SECRET_PATTERN) and the configured secret values,
    not only classic AIza keys (review finding). All values here are synthetic."""
    path = tmp_path / "llm_calls.jsonl"
    aq_key = "AQ." + "Ab1-_" * 10                                   # synthetic, AQ. format
    plain = "s3cr3t-synthetic-value-0042"                          # synthetic configured secret, no known format
    lines = [{"ts": "t", "status": "ok", "model": aq_key},
             {"ts": "t", "status": "ok", "model": f"x{plain}"},
             {"ts": "t", "status": "ok", "model": PRIMARY}]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    problems = check_audit(path, tou=TOU, secrets=[plain.encode()])
    assert [p for p in problems if "API key" in p] == ["line 1: contains an API key", "line 2: contains an API key"]
    assert all(aq_key not in p and plain not in p for p in problems)  # a problem never quotes the secret
    assert [p for p in check_audit(path, tou=TOU, secrets=[]) if "API key" in p] == ["line 1: contains an API key"]
    # by default the configured values come from the environment and .env (bundle.secret_values)
    monkeypatch.setenv("GEMINI_API_KEY", plain)
    assert "line 2: contains an API key" in check_audit(path, tou=TOU)


def test_audit_lines_have_the_contract_fields(tmp_path):
    audit = AuditLog(tmp_path / "a.jsonl", now=lambda: "2026-10-03T00:00:00Z")
    audit.record(vendor_id="V-901", role="triage", reply=LLMReply(data=None, status="cache_miss", cache_key="k"),
                 prompt_sha256="a" * 64, schema_sha256="b" * 64, payload_sha256="c" * 64, items_sent=["r1"],
                 withheld=[], n_out=0, hosts=["press.example"])
    rec = json.loads((tmp_path / "a.jsonl").read_text(encoding="utf-8"))
    assert {"ts", "vendor_id", "role", "model", "status", "cache_key", "prompt_sha256", "schema_sha256",
            "payload_sha256", "items_sent", "withheld", "n_out", "attempts"} <= set(rec)
    assert rec["status"] == "cache_miss" and rec["withheld"] == []


def test_audit_stats_report_the_withholding_rate():
    docs, passages = _docs_and_passages(3)
    audit = AuditLog(None)
    leak = passage("d0", 50_000, "Meridian uses AI.")
    extract_claims(FakeLLM(), passages + [leak], company=COMPANY, guard=guard(), docs=docs, vendor_id="V-901",
                   audit=audit)
    stats = audit.stats()["V-901"]
    assert stats["withheld"] == 1 and stats["sent"] == 3 and stats["rate"] == 0.25
    assert stats["calls"] == 1 and stats["api_calls"] == 1 and stats["cache_hits"] == 0


# --------------------------------------------------------------------------- confidentiality structure


def test_ai_module_never_imports_profile_tier_or_verdict_code():
    tree = ast.parse((REPO / "src" / "footprint" / "ai.py").read_text(encoding="utf-8"))
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            names |= {a.name for a in node.names}
    forbidden_modules = {f"footprint.{m}" for m in ("rules", "verdict", "risk", "compose", "review", "criticality",
                                                     "depth", "pipeline", "workbook")}
    assert not modules & forbidden_modules
    assert not names & {"VendorProfile", "CriticalityResult", "DepthPlan", "UsageVerdict", "RiskResult", "Tier",
                        "VendorFindings", "AssessmentResult", "WorkbookData", "StudentCells"}


def test_role_signatures_take_public_material_only():
    import inspect

    for fn in (expand_names, triage, extract_claims):
        params = inspect.signature(fn).parameters
        assert not {"profile", "tier", "verdict", "plan", "criticality", "risk", "seeds"} & set(params)


# --------------------------------------------------------------------------- live smoke test (fictional text)


@pytest.mark.network
def test_gemini_smoke_accepts_the_claim_schema(tmp_path):
    pytest.importorskip("google.genai")
    if not os.environ.get("GEMINI_API_KEY"):
        from footprint.pipeline import load_dotenv

        load_dotenv(REPO / ".env")
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")
    llm = GeminiLLM()
    docs = {"d0": doc("d0")}
    text = ("Fernhill Ledger uses a machine learning model from Azure OpenAI Service to flag unusual payments. "
            "Analysts review every flag before an account is frozen.")
    audit = AuditLog(None)
    claims = extract_claims(llm, [passage("d0", 0, text)], company=COMPANY, guard=guard(), docs=docs,
                            vendor_id="V-901", audit=audit)
    (rec,) = audit.records
    assert rec["status"] == "ok", (rec["status"], llm.switches)
    assert claims and any(c.quote in text for c in claims)
