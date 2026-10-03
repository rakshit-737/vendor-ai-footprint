# P3/P4 contracts (signals, AI layer, verdict, risk, cells, app API)

Read this before writing any P3/P4 module. The data models live in `src/footprint/models.py` (section "P3/P4:
signals, verdict, risk"). Their validators enforce the invariants in §1, and `tests/unit/test_models_p3.py` pins
them. Offline sample builders for tests are in `tests/fixtures/p3_samples.py` (fictional vendor V-901); load the
file with importlib, as the tests do with `collectors/_fakes.py`.

Design references: `docs/design.md` Appendix A §2.3 (sources, tags, strength labels), §2.6 (AI roles, claim schema,
payload guard, V1–V9), §2.7 (verdict), §2.8 (risk, cell templates, actions), §2.11 (workbook I/O), and the main
plan's "Traceability & workbook I/O". The P2 contracts are in `docs/contracts_p2.md`.

The design decides policy. This file decides names, signatures and data shapes. Items marked *(interpretation)*
fill a gap in the design. Change one only together with the test that pins it.

## 0. Conventions for every P3/P4 module

- **No clock in decisions.** Every function that dates anything takes `as_of: str` (an ISO date) explicitly.
  Wall-clock time appears only in record metadata (`recorded_at`, `created_at`, the audit `ts`).
- **Defined order.** Never depend on set order or on the order of external data. Each function states its output
  order, and ties break on `item_key`.
- **Identity.** Until `compose.assign_evidence_ids` runs, the only item id is `EvidenceItem.item_key`
  (sha256 of `url_final|excerpt_sha256`). Verdicts, risk inputs, reviews, screenshots and caches all use it.
  Cells and sheets cite `evidence_id` (`V-00x-E-0001`).
- **What counts.** Only `item.citable` items feed the verdict, risk, actions and cells. An item is citable when it is
  not rejected, not a definition-test trap (`ai_type == "not_ai"`) and not a pending LLM proposal. Every other item
  stays in the Evidence Log only.
- **Updating items.** `model_copy(update=...)` skips validation. Use it only for tags, labels, role, review
  fields, cluster and ids. When the excerpt, its offsets or the URLs change, build the item again with
  `EvidenceItem.model_validate({...})`.
- **Seeds.** You may use `aliases`, `legal_names`, `domains`, `sec_cik`, `collisions`, `person_scrub`,
  `service_term`, `family_term`, `affiliate`, `platform_supplier` and `ats`. Never read a seed's `expect`,
  `relevance` or `note`. They are gold labels for `evaluate` only, never evidence.
- **Files.** A JSON line is `json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n"`, written as UTF-8 with LF.
  JSONL files are append-only unless a section says otherwise. Config is TOML read with `tomllib`, cached per path,
  and hashed into the run manifest.
- **Failures.** "Nothing found" is a result, not an exception: an empty list, rule e or f, or "None identified".
  Raise `ValueError` only when a contract is broken. One vendor's failure never stops a run: the pipeline records a
  note and continues.
- **Tests.** Unit tests are offline: no sockets, no Gemini, no Playwright. Inject fakes and use `tmp_path`. Live
  tests carry `@pytest.mark.network`. No test may need `.env`.
- **Confidentiality.** Only `footprint.ai` talks to Gemini. Text reaches Gemini only inside a `PublicPayload`, and
  only after `PayloadGuard.check`. No prompt, schema, cache entry or log line may contain any of these:
  "Meridian", the team name, a profile free-text value (columns D, E, H, J, K), a tier, a verdict or a finding.
- **Import direction.** This direction prevents import cycles:
  - `rules` imports `models` only, plus helpers from `extract`, `collectors.base` and `review`.
  - `verify` imports `rules`.
  - `cluster` imports `rules`. `verdict` imports `rules`, `cluster` and `depth`.
  - `risk` and `actions` may import `rules`.
  - `ai` imports nothing from `rules`, `verdict`, `risk`, `compose`, `review`, `criticality` or `depth`.
  - `compose` may import any analysis module.
  - Only `pipeline`, `cli` and `app` import `pipeline`.

## 1. Data models (models.py)

### Invariants

The validators enforce every rule in this table.

| Model | Purpose | Enforced |
|---|---|---|
| `Indicator(code, span)` | one G/M indicator hit and the words that show it | <br>• `code` ∈ `INDICATOR_CODES` (G1..G11, M1..M7). <br>• `UNDEFINED_INDICATORS` = {G9, G10}: in the schema range but not defined in §2.3, so V4 drops them. <br>• `SOURCE_TYPE_INDICATORS` = {G6, G7}: the rules set them from the source type, and V4 drops any the LLM proposes. |
| `SignalTags` | tag card | <br>• `u_class` U1..U8, `sr` A..F, `sp` S0..S3, `rl` R0..R3, `rc` T0..T3, `ic` 1..6. <br>• `locus` has 10 values. `ai_type` has 8 values, including `not_ai`. `strength` has 8 labels written with an ASCII " - ". <br>• `tag_string()` returns `"U3 · SR:B · SP:S2 · RL:R2 · RC:T3 · IC:2 · locus=delivery_ops"`. <br>• `u_number`, `sp_level`, `rl_level` and `rc_level` return ints. <br>• Unknown fields are rejected. |
| `Claim`, `ClaimBatch` | the Gemini `extract_v1` response schema (§2.6), unverified | <br>• All ten fields are required, with the enums exactly as in §2.6. <br>• Unknown keys are ignored on purpose (lenient). <br>• The JSON-schema descriptions are short text written for the model. A test forbids internal words in them, such as Meridian, tier and verdict. |
| `VerifyResult` | outcome of `verify.verify_claim` | <br>• `failures` are unique and in gate order. <br>• `ok` is True exactly when no blocking gate failed. Blocking gates (`BLOCKING_VERIFY_CODES`) are V1, V2, V3, V6 and V9. V4 and V5 (`REPAIR_VERIFY_CODES`) only repair the claim. <br>• `ok` requires a located excerpt. <br>• `end - start == len(excerpt)`. |
| `EvidenceItem` | one verified excerpt with its tags: one Evidence Log row | <br>• `excerpt` is not empty, and `end - start == len(excerpt)`. <br>• These fields are derived when omitted and checked when given: `excerpt_sha256`; `url_final` (defaults to `url`); `item_key`; `capture_sha256` (= `capture_id`); `text_sha256` (= `doc_id`). <br>• `evidence_id` is "" or `{vendor_id}-E-dddd`. <br>• Properties: `strength`, `tag_string`, `proposed`, `citable`, and `label_disagreements` (checked over `V8_LABEL_KEYS`: temporal, action_level, sp). <br>• Helpers: `EvidenceItem.make_key(url_final, excerpt_sha256)` and `sha256_text(text)`. |
| `UsageVerdict` | column O | <br>• `label`, `column_o` and `conflict` are derived from `rule` when omitted and checked when given (`RULE_LABEL`, `LABEL_COLUMN_O`). `conflict` is True exactly for rule a. <br>• `likelihood` ∈ `ICD203_LIKELIHOOD`. `confidence` is High, Moderate or Low. <br>• The item lists hold item keys. |
| `RiskInputs` | E, K, TP and TG with their provenance | <br>• Each input is in the range 0..3. <br>• `gaps` either has all six keys t1..t6 (True = missing) or is empty. <br>• `tg` is derived when omitted and must equal `tg_for_missing(n_missing)`. <br>• `missing_gaps` property. <br>• `e_if_confirmed` is optional, 0..3. |
| `RiskResult` | the ordered risk steps | <br>• `arp == 2E + 2K + TP + TG`, and `base_class == arp_class(arp)`. <br>• `cap` ∈ {"", "High", "Medium", "None identified"}. <br>• `provisional` is True exactly when `cap == "Medium"`. <br>• `final_class == "None identified"` exactly when `cap == "None identified"`. <br>• When `pre_cap_class` is given, `final_class == min(pre_cap_class, cap)`. <br>• No escalator is both fired and logged as not applied. <br>• `ceiling_class` ∈ {"", Critical, High, Medium, Low}. <br>• `themes` are RT1..RT7. |
| `ActionPlan` | column U | <br>• `gap_blocks` ∈ `GAP_BLOCK_CODES`: SUB, TRAIN, LOC, EXPL, AGENT, PI, GOV, INC, CONC, MRM. <br>• `questionnaire_items` are Q1..Q15. <br>• `contract_clauses` are C1..C11. |
| `VendorFindings` | one vendor end to end | <br>• Profile, criticality, depth, coverage and evidence all belong to one vendor. <br>• Item keys and evidence ids are unique. <br>• Every key cited by the verdict, the risk inputs (`e_items`, `k_items`, `gap_items`) or an item's `corroborates` exists in `evidence`. <br>• `cells` defaults to an empty `StudentCells`. <br>• Helpers: `vendor_id`, `item(ref)` (by item key or E-ID), `cited()`. |
| `AssessmentResult` | the output of `run_assessment` | <br>• `mode` is replay, live_rules or live_ai. <br>• `as_of` is YYYY-MM-DD. <br>• `input_sha256` is 64 lower-case hex characters. <br>• Vendor ids are unique, and the example is not one of the vendors. <br>• Helpers: `vendor(id)`, and `cells()`, which returns the mapping `write_workbook` takes. |

### Constants, functions and type aliases

- **Constants:** `TAG_SEPARATOR`, `GENUINE_INDICATORS`, `MARKETING_INDICATORS`, `STRENGTH_ORDER`,
  `CONTEXT_STRENGTHS`, `QUALIFYING_LOCI`, `ROLE_ORDER`, `CITED_ROLES`, `GAP_KEYS`, `RISK_CLASS_ORDER`,
  `GAP_BLOCK_CODES`.
- **Functions:** `tg_for_missing`, `arp_class`, `sha256_text`.
- **Literal aliases:**
  - Tags: `IndicatorCode`, `UClass`, `SourceReliability`, `SpecificityGrade`, `RelevanceGrade`, `RecencyGrade`,
    `Locus`, `AiType`, `Strength`.
  - Claims and verification: `ClaimKind`, `ClaimSubject`, `ClaimAiType`, `Temporal`, `ActionLevel`, `VerifyCode`.
  - Evidence: `EvidenceMethod`, `EvidenceRole`, `ReviewStatus`.
  - Verdict: `VerdictRule`, `VerdictLabel`, `ColumnO`, `Likelihood`, `ConfidenceLevel`.
  - Risk and actions: `GapKey`, `RiskClass`, `FinalRiskClass`, `VerdictCap`, `CeilingClass`, `EscalatorCode`,
    `RiskTheme`, `GapBlock`, `QuestionCode`, `ClauseCode`.
  - Runs: `AssessmentMode`.

### Field meanings that more than one module depends on

- **`url` and `url_final`.** `url` is the cited URL: `Document.url`, which is the original URL for a Wayback copy.
  `url_final` is the URL actually retrieved: `Capture.url_final or Capture.url_requested`, which is the replay URL
  for a Wayback copy.
- **`source_type` and `publisher`.** `source_type` is the phrase column P uses, for example "Product page",
  "SEC Form 10-K", "Privacy notice", "Job posting", "DNS TXT record" or "Trade press interview". `publisher` is the
  vendor's short name for a first-party source, and otherwise the outlet or provider.
- **`published` and `date_basis`.** Both come from the `Document`. An item dated by retrieval has
  `date_basis = "retrieval"` and `published = ""`.
- **`method`:**
  - `rule`: found by the rules only. Also used when an overlapping LLM label disagrees: the rule value stands,
    `label_disagreements` is not empty, and the item goes to the HC2 queue.
  - `rule+llm_agree`: an overlapping verified LLM claim agrees on the V8 labels.
  - `llm_proposed_accepted`: an LLM claim that no rule item covers. It stays `proposed` until an analyst accepts it.
  - `adjudicated`: an analyst changed its labels in HC2.
- **`rule_labels` and `llm_labels`.** String dicts. Keys come from: temporal, action_level, sp, rl, locus, u_class,
  ai_type, claim_kind, subject, providers. Lists are joined with "; ".
- **`role`** (set by `verdict.assign_roles`):
  - Primary: the first decisive item of a Yes verdict.
  - Supporting: the other Q and K items of a Yes verdict.
  - Indicator: the decisive items of an Inconclusive verdict.
  - Negative: U8 limiting statements.
  - Counter-evidence: the decisive non-U8 items of a No verdict.
  - Context: any other item with a Context label.
  - Logged: everything else, including rejected items and traps.
- **`review_status`, `review_reason`, `reviewer`.** These come from HC2 records. `reviewer` is
  "{analyst} {YYYY-MM-DD}".
- **`ic`** *(interpretation: §2.3 does not define 4)*. `rules` sets 2, 3, 4 and 6;
  `cluster.link_corroboration` sets 1 and 5.

  | IC | Meaning |
  |---|---|
  | 1 | Corroborated by an independent K. |
  | 2 | Consistent but uncorroborated, SR A or B. |
  | 3 | S0 or S1 only. |
  | 4 | Uncorroborated SR C or D at S2 or above (Admiralty "doubtful"). |
  | 5 | Contradicted by an A/B U8 at R3 dated the same day or later. |
  | 6 | Cannot be judged: T0 or undated, or SR E or F. |

## 2. Stage order

`pipeline.run_assessment` validates the workbook, then runs these stages for each vendor in sheet order:

1. `assess_profile` (criticality plus any HC1 override), then `depth.plan_depth`.
2. Collection gives a `CollectionRun`: captures, documents, passages and coverage. Replay makes no network calls.
3. `rules.tag_passage` runs on every passage in document order. `rules.tag_document` handles kinds that have no
   passages (DNS). The result is the rule items.
4. The AI path runs in live_ai, runs from the cache in replay, and uses NullLLM in live_rules:
   1. `ai.extract_claims` returns the claims.
   2. `verify.verify_claim` checks each claim.
   3. `rules.claim_item` builds an item for each `ok` result.
   4. `rules.merge_claims(rule_items, claim_items)` merges them.
5. `cluster.dedupe` (V9), then `cluster.assign_clusters`, then `cluster.link_corroboration`.
6. `rules.apply_reviews` applies the HC2 decisions from `review/reviews.jsonl`.
7. `verdict.decide`, then `verdict.assign_roles`.
8. `risk.assess`, then `actions.plan_actions`.
9. Screenshots:
   - Live modes: `capture.shots.screenshot_excerpt`. Every Primary item gets one; other cited items get one while
     the budget allows.
   - Replay: `capture.shots.find_shot`.
10. Cells:
    1. `compose.assign_evidence_ids`.
    2. Build the `VendorFindings`.
    3. `compose.build_cells`.
    4. Store the cells with `findings.model_copy(update={"cells": ...})`.
    5. Apply `cell_edit` overrides.

`pipeline.rescore_vendor` repeats stages 5–10 after a review or override changes. It skips collection, the LLM and
screenshots.

## 3. rules.py: rules tagger

Config this module owns:
- `config/sources.toml` (new). It is the only thing that sets SR.
- New tables appended to `config/lexicon.toml`. Do not change `[core]`, `[guards]` or `[suppressors]`, because P2
  passages depend on them.

```python
class SourceInfo(BaseModel):          # frozen
    source_type: str
    publisher: str
    sr: SourceReliability
    first_party: bool
    dated_by: Literal["publication", "retrieval"]
    legal: bool = False               # gives G6
    filing: bool = False              # gives G7

def load_signals(path: str | Path | None = None) -> Signals           # lexicon tables, cached per path
def load_sources(path: str | Path | None = None) -> SourceRegister    # config/sources.toml, cached per path
def classify_source(document: Document, capture: Capture, seeds: dict, profile: VendorProfile, *,
                    sources: SourceRegister | None = None) -> SourceInfo
def entity_ok(document: Document, capture: Capture, seeds: dict, profile: VendorProfile, *, text: str = "",
              log: list[str] | None = None) -> bool
def find_indicators(text: str, *, source: SourceInfo | None = None,
                    signals: Signals | None = None) -> list[Indicator]
def indicator_ok(code: str, span: str, *, signals: Signals | None = None) -> bool
def specificity(indicators: Sequence[Indicator], *, named_target: bool,
                temporal: Temporal = "unclear") -> SpecificityGrade
def relevance(claim_text: str, seeds: dict, locus: Locus, *, title: str = "", heading: str = "") -> RelevanceGrade
def recency(published: str, retrieved_at: str, as_of: str, *,
            dated_by: Literal["publication", "retrieval"]) -> RecencyGrade
def strength_label(u_class: UClass, sp: SpecificityGrade, rl: RelevanceGrade, locus: Locus, *,
                   is_q: bool, is_k: bool) -> Strength
def is_qualifying(item: EvidenceItem) -> bool
def is_corroborating(item: EvidenceItem) -> bool
def tag_passage(passage: Passage, document: Document, capture: Capture, seeds: dict, profile: VendorProfile,
                plan: DepthPlan, *, as_of: str, doc_text: str | None = None, signals: Signals | None = None,
                sources: SourceRegister | None = None, log: list[str] | None = None) -> EvidenceItem | None
def tag_document(document: Document, doc_text: str, capture: Capture, seeds: dict, profile: VendorProfile,
                 plan: DepthPlan, *, as_of: str, signals: Signals | None = None,
                 sources: SourceRegister | None = None, log: list[str] | None = None) -> list[EvidenceItem]
def claim_item(claim: Claim, result: VerifyResult, passage: Passage, document: Document, capture: Capture,
               seeds: dict, profile: VendorProfile, plan: DepthPlan, *, as_of: str, doc_text: str,
               llm_model: str, prompt_sha256: str, signals: Signals | None = None,
               sources: SourceRegister | None = None) -> EvidenceItem
def merge_claims(rule_items: Sequence[EvidenceItem], claim_items: Sequence[EvidenceItem]) -> list[EvidenceItem]
def apply_reviews(items: Sequence[EvidenceItem], store: OverrideStore) -> list[EvidenceItem]
def retag(item: EvidenceItem) -> EvidenceItem
```

### Source, entity and indicator checks

- **`entity_ok`** (the §2.3 entity guard, also used for V6). It returns True when either:
  - the source is a first-party or confirmed-affiliate domain (`seeds.domains`, `profile.domain`, or an `affiliate`
    whose status is confirmed); or
  - the document carries the vendor's legal name or an alias **and** a link to a vendor domain or the SEC CIK that
    corroborates it.

  It returns False when `text` or the title matches a seeded collision (`seeds.collisions`): for example
  "Claude Reumert", Telik's "Terrapin Technologies", Terrapinn, Autoworx, Packet Clearing House, or the GSA programme
  "FSSI". On False it appends a short reason to `log`.
- **`find_indicators`** returns the G/M hits whose spans are exact substrings of `text`, one `Indicator` per
  (code, span), in text order. It adds G6 when `source.legal` is set and G7 when `source.filing` is set; the span is
  the AI term of the AI sentence.
- **`indicator_ok`** (used by V4) is True when the span passes the lexical test for its code: G1 needs a provider or
  model name, G3 the data-flow lexicon, M2 the aspirational or modal lexicon, and so on. A code with no lexical test
  passes. G6, G7, G9 and G10 always fail.

### Tag functions

- **`specificity`** (§2.3). The first rule that matches wins. Rule 1 defines "M2/M3 dominate"
  *(interpretation)*.
  1. S0 when `temporal == "planned"`, or when M2 or M3 is present and none of G2, G3, G4, G8 and G11 is.
  2. S3 when any of G1, G5, G6, G7 is present together with any of G2, G3.
  3. S2 when any of G2, G4, G5, G8, G11 is present, `named_target` is True, and M4 is absent.
  4. S1 otherwise. This covers generic legal or filing language, M1/M5/M6/M7 only, G6 or G7 without G2 or G3,
     and no indicators at all.

  `tag_passage` caps JOB items at S2. Fiserv-style filing statements end up at S1 under rule 4.
- **`relevance`** (§2.3; computed locally only, which is V7):
  - R0 when the locus is `commentary`.
  - R1 when the locus is `platform_supplier` or `affiliate_inferred`.
  - R2 when the locus is `relationship`.
  - R3 when a `service_term` occurs (case-insensitive, whole words) in the claim sentence, its heading or the page
    title, and the locus is service_feature, vendor_addon or delivery_ops.
  - R2 when a `family_term` occurs there, or the locus is delivery_ops or sdlc (a company-wide delivery practice).
  - R1 otherwise.
- **`recency`:**
  - Date: `published` when `dated_by == "publication"` and `published` is set; otherwise the retrieval date
    (`retrieved_at[:10]`).
  - Age: whole calendar months up to `as_of`. A date after `as_of` counts as 0 months.
  - Grade: 12 months or less is T3, 24 or less T2, 36 or less T1, older T0. No date at all is T0.
- **`strength_label`** (the §2.3 fixed order; the first rule that matches wins):

  | Condition | Label |
  |---|---|
  | U8 | Negative |
  | `is_q` and R3 | Strong |
  | `is_q` or `is_k` | Moderate |
  | locus `relationship` | "Context - relationship only" |
  | locus `platform_supplier` | "Context - platform supplier" |
  | locus `affiliate_inferred` | "Context - inferred affiliate" |
  | U7 or S0 | "Marketing only" |
  | anything else | Weak |

  A DNS token (U4, S1, R2, locus relationship) is therefore always "Context - relationship only".
- **`is_qualifying`** (Q, §2.7) needs all of: `citable`; SR A or B; SP S2 or higher; RL R2 or higher; RC T1 or
  newer; class U1–U4; and a locus in `QUALIFYING_LOCI`.
- **`is_corroborating`** (the item's own part of K, §2.7) needs all of: `citable`; SR A–C; SP S2 or higher; RL R2 or
  higher; RC T1 or newer; and class U1–U4 *(interpretation: a K corroborates use, so U5–U8 never count)*.
  `cluster.independent` checks independence.

### Building items

- **`tag_passage`** returns one item per passage, or None. `plan` supplies the tier, the modifiers (for example the
  M-D3P delivery-AI cues) and the JOB rules. Steps, in order:
  1. Return None, with the reason in `log`, when any of these holds:
     - `entity_ok` fails;
     - no AI term survives the provider guards and suppressors;
     - the subject test fails (§2.3 test 2: a third-party source must name the vendor, an alias or a product in
       the sentence or the title; on a first-party page the product is the implied subject).
  2. Excerpt: the smallest run of whole sentences of the passage that holds the AI claim. It is 25–600 characters
     and an exact slice; its document offsets are `passage.start` plus the local offsets.
  3. When the definition test fails, the item becomes a visible trap. The definition test is EU AI Act Art. 3(1):
     scheduling, RPA, rules engines, templated composition, IMb, BI, or automation relabelled as AI with no ML or LLM
     evidenced. The trap has `ai_type="not_ai"`, `u_class="U7"`, an M4 indicator and `strength="Marketing only"`.
     It shows in the Evidence Log as a rejected trap and is never citable.
  4. Otherwise the item gets:
     - SR, `source_type` and `publisher` from `classify_source`;
     - indicators;
     - temporal from the use-state test: deployed cues give in_production, pilot or beta gives pilot_or_beta, M2
       cues give planned, anything else gives unclear;
     - action_level from the autonomy lexicon, or unknown when the text is silent;
     - ai_type;
     - locus and U class *(precedence guidance: U8 > U1 > U2 > U3 > U4 > U6 > U5 > U7)*;
     - SP (JOB capped at S2), RL, RC and the initial IC;
     - strength, with `is_q` and `is_k` taken from the item-local predicates;
     - `rule_labels`, `method="rule"` and `role="Logged"`.
- **`tag_document`** handles `kind == "dns"` documents. It returns one item per `AI_TOKEN\t{provider}\t{record}`
  line, with:
  - excerpt = the record text, and `passage_id=""`;
  - U4, SR B, S1, R2, RC by retrieval, IC 2;
  - locus relationship, ai_type unspecified, providers [provider];
  - source_type "DNS TXT record", publisher = the vendor's short name;
  - strength "Context - relationship only".

  Other kinds return [].
- **`claim_item`** builds an item over the verified excerpt (`result.start`, `result.end`, `result.excerpt`):
  - It is tagged by the same rules as `tag_passage`. RL and locus are computed locally (V7); temporal, action_level
    and SP come from the rules (V8).
  - `indicators` = the rule indicators plus `result.indicators`. `providers` = the providers the rules detect plus
    `result.providers`.
  - `llm_labels` holds the claim's claim_kind, subject, ai_type, temporal, action_level and providers, plus the SP
    its surviving indicators would give.
  - It sets `method="llm_proposed_accepted"`, `llm_model` and `prompt_sha256`.
- **`merge_claims`.** A claim item whose span overlaps a rule item from the same `doc_id` merges into the best such
  rule item: first by STRENGTH_ORDER, then by the longer overlap.
  - The rule item gains `llm_labels`, `llm_model`, `prompt_sha256`, and those of the claim's providers that appear
    in its own excerpt or title.
  - Its `method` becomes `rule+llm_agree` when `label_disagreements` is empty, and stays `rule` otherwise.
  - Claim items that match no rule item stay as proposals.
  - Output is sorted by (doc_id, start, end, item_key).
- **`apply_reviews`** reads, for each item, the latest `evidence_review` record keyed by its `item_key` (§10):
  - The record sets `review_status`, `review_reason` and `reviewer`.
  - `extra["labels"]` overrides the labels it names. Allowed keys: temporal, action_level, sp, rl, locus, u_class,
    ai_type. It then sets `method="adjudicated"` and calls `retag`.
  - Items without a record are unchanged, and order is preserved.
- **`retag`** recomputes `strength` and the initial IC from the current tags.

## 4. ai.py: Gemini layer and payload guard

This is the only module that talks to Gemini. Its functions accept public material only: passages, documents, the
company name, triage rows and names. They never take a VendorProfile, CriticalityResult, DepthPlan, UsageVerdict,
RiskResult, review record or seed service term.

The prompts are `prompts/expand_v1.txt`, `prompts/triage_v1.txt` and `prompts/extract_v1.txt`. They are frozen,
hashed and never contain the word "Meridian". `extract_v1` lists only the defined indicator codes.

```python
MODEL_CHAIN = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
SEED = 1234
CACHE_DIR = Path("evidence/llm_cache")
BATCH_SIZE = 12
MIN_INTERVAL_S = 6.0
LLMRole = Literal["expand", "triage", "extract"]
WithheldReason = Literal["hard_block_meridian", "hard_block_team", "profile_shingle", "volume_phrase",
                         "host_ai_disallowed", "robots_google_extended", "content_signal"]

class PayloadItem(BaseModel):    # frozen, extra="forbid": the only shape public text crosses in
    id: str
    kind: str
    text: str

class PublicPayload(BaseModel):  # frozen, extra="forbid"
    company: str
    items: tuple[PayloadItem, ...]
    def sha256(self) -> str      # canonical JSON: sort_keys, compact separators, ensure_ascii=False

class GuardInput(BaseModel):     # frozen: a candidate before the guard
    id: str
    kind: str
    text: str
    url: str = ""

class Withheld(BaseModel):       # frozen; logged without text
    id: str
    reason: WithheldReason

class LLMReply(BaseModel):
    data: dict | None
    model: str = ""
    status: Literal["ok", "cache_hit", "cache_miss", "error", "quota", "null", "budget"]
    cache_key: str = ""
    attempts: int = 0

class TriageRow(BaseModel):
    id: str
    path: str
    title: str = ""
    date: str = ""
    family: str = ""

class LLM(Protocol):
    name: str
    def generate(self, *, role: LLMRole, prompt: str, payload: PublicPayload,
                 schema: dict[str, Any]) -> LLMReply: ...

class GeminiLLM:   # __init__(api_key=None, *, models=MODEL_CHAIN, client=None, min_interval_s=MIN_INTERVAL_S,
                   #          max_attempts=5, sleep=time.sleep, clock=time.monotonic); .switches: list[dict]
class CachedLLM:   # __init__(inner: LLM | None, cache_dir=CACHE_DIR, *, models=MODEL_CHAIN, seed=SEED)
class NullLLM:     # name = "null"; generate() returns LLMReply(data=None, status="null")
def make_llm(mode: AssessmentMode, *, cache_dir: str | Path = CACHE_DIR) -> LLM

class PayloadGuard:
    def __init__(self, profile_texts: Sequence[str], team_name: str, scrub_names: Sequence[str] = (), *,
                 host_policy: Callable[[str], tuple[bool, str]] | None = None,
                 allow_shingles: Mapping[str, str] | None = None) -> None
    def check(self, items: Sequence[GuardInput]) -> tuple[list[PayloadItem], list[Withheld]]
    def placeholders(self, item_id: str) -> dict[str, str]
    def restore(self, item_id: str, text: str) -> str

class HostAiPolicy:                # a host_policy callable
    def __init__(self, tou: TouRegister, decisions_path: str | Path = "evidence/ai_policy.jsonl", *,
                 robots: RobotsCache | None = None) -> None
    def __call__(self, url: str) -> tuple[bool, str]

class AuditLog:
    def __init__(self, path: str | Path | None) -> None   # None keeps records in memory only
    def record(self, *, vendor_id: str, role: LLMRole, reply: LLMReply, prompt_sha256: str, schema_sha256: str,
               payload_sha256: str, items_sent: Sequence[str], withheld: Sequence[Withheld], n_out: int) -> None
    def model_for(self, item_id: str) -> str
    records: list[dict]

def prompt_text(name: str) -> str
def prompt_sha256(name: str) -> str
def schema_sha256(schema: dict[str, Any]) -> str
def cache_key(model: str, prompt_sha256: str, schema_sha256: str, payload_sha256: str, seed: int = SEED) -> str
def expand_names(llm: LLM, company: str, texts: Sequence[str], *, guard: PayloadGuard,
                 audit: AuditLog | None = None, vendor_id: str) -> list[str]
def triage(llm: LLM, rows: Sequence[TriageRow], *, company: str, guard: PayloadGuard,
           audit: AuditLog | None = None, vendor_id: str) -> list[str]
def extract_claims(llm: LLM, passages: Sequence[Passage], *, company: str, guard: PayloadGuard,
                   docs: Mapping[str, Document], audit: AuditLog | None = None, vendor_id: str,
                   batch_size: int = BATCH_SIZE, max_calls: int | None = None) -> list[Claim]
def check_audit(path: str | Path) -> list[str]
```

### LLM back ends

- **`GeminiLLM`** uses google-genai, and only stateless `generate_content`. It never uses the Files API, context
  caching, the Interactions API, tools or grounding. The call is:

  ```python
  client.models.generate_content(
      model=model,
      contents=rendered_payload,
      config=types.GenerateContentConfig(
          system_instruction=prompt,
          response_mime_type="application/json",
          response_json_schema=schema,          # e.g. ClaimBatch.model_json_schema()
          seed=1234,
          thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
          automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
      ),
  )
  ```

  - No temperature is set.
  - Calls are at least 6 s apart.
  - It makes 5 attempts, with 2–60 s backoff, on HTTP 408, 429 and 5xx.
  - A daily-quota error or a 403 switches to the next model. After the last model it returns status "quota" for the
    rest of the run, and callers fall back to the rules. Every switch is appended to `switches` for the manifest.
  - The API key comes from `GEMINI_API_KEY`, loaded by `pipeline.load_dotenv`. It is never logged or stored.
  - `client`, `sleep` and `clock` can be injected for offline tests.
  - A `@pytest.mark.network` test confirms the API accepts `ClaimBatch.model_json_schema()` (which has `$defs`).
    If it does not, inline the definitions.
- **`CachedLLM`:**
  - Key: `cache_key(...)`, the sha256 of `"{model}|{prompt_sha}|{schema_sha}|{payload_sha}|{seed}"`.
  - File: `cache_dir/<key[:2]>/<key>.json`, written atomically with sorted keys. It holds key, model, role,
    prompt_sha256, schema_sha256, payload_sha256, seed and response, and no secrets.
  - Lookup tries each model of the chain in order, so an answer from the fallback model replays too.
  - A miss with `inner=None` (replay) returns `status="cache_miss"` and `data=None`.
  - A miss with an inner LLM calls it and stores an `ok` response. Errors are never stored.
- **`make_llm`** returns, by mode:
  - replay: `CachedLLM(None)`;
  - live_rules: `NullLLM()`;
  - live_ai: `CachedLLM(GeminiLLM())`, or `NullLLM()` when no API key is set. The run then carries the note
    "Gemini unavailable – rules + local model".

### Payload guard and host policy

- **`PayloadGuard.check`** (§2.6) works through the items in input order.
  - It withholds an item, logging no text, on any of these:

    | Reason | When |
    |---|---|
    | `hard_block_meridian` | the whole word "Meridian" appears |
    | `hard_block_team` | the team name appears as whole words, with or without a leading "Team " |
    | `profile_shingle` | a normalised 6-word shingle of a `profile_texts` value with 6 or more words appears, or the whole value of a 4–5-word value |
    | `volume_phrase` | a distinctive volume phrase appears, with numbers normalised, such as "1.4 million customers" |
    | `host_ai_disallowed`, `robots_google_extended`, `content_signal` | `host_policy(url)` refuses the host |

  - `allow_shingles` maps a shingle to the written reason it may still be sent, for example because it appears on
    the vendor's own public pages.
  - It redacts the texts it sends: emails become `[EMAIL_n]`, phone numbers `[PHONE_n]`, and `scrub_names`
    `[PERSON_n]`; n counts from 1 within each item. The map is kept per item for `placeholders` and `restore`.
  - The pipeline builds `profile_texts` from the D, E, H, J and K values of every workbook row, including V-000. The
    enumerated columns F and I, the website in G and the vendor name in C are not blocked.
- **`HostAiPolicy`** *(interpretation, needed for replay)*:
  - It refuses a host whose ToS entry has `ai_processing_allowed=false`. The default entry counts, so unlisted hosts
    are refused.
  - With `robots` (live modes) it also refuses a host whose robots.txt disallows `Google-Extended`, or sets a
    `Content-Signal` with `ai-train=no` or `ai-input=no`. It appends each decision
    {host, allowed, reason, robots_sha256, decided_on} to `decisions_path`.
  - Without `robots` (replay) it reads the recorded decisions, so replay withholds exactly what live withheld.

### Role functions

- **`expand_names`.** Input items are public homepage title, meta and navigation texts. The output is names only
  (aliases, products, AI programmes, providers, affiliates), used as search keys and never as evidence. It drops
  anything URL-like: `://`, `www.` or a dotted host. With NullLLM it returns the seed aliases plus capitalised
  n-grams next to AI terms (the §2.6 keyless fallback).
- **`triage`.** Rows are rendered `id | path | title | date | family`. It returns only ids that were sent: high
  priority first, then medium, each in input order. With NullLLM it orders by a slug and title keyword score.
- **`extract_claims`:**
  1. It sorts passages by (doc_id, start) and runs the guard.
  2. Each sendable item is rendered `[{id}] ({kind}, {date}) {text}`, where kind is `"{family} {doc kind}"` and date
     is `Document.published` or "".
  3. It sends batches of `batch_size`. `max_calls` is the vendor's remaining Gemini budget; once it is used up,
     passages are not sent (audit status "budget") and stay with the rules.
  4. It returns every schema-valid claim in batch order. It does not verify quotes and does not drop unknown
     `passage_id`s: V1 rejects those, so the rejection is logged.

### Audit log

- **`AuditLog`** is written to `runs/<run_id>/llm_calls.jsonl`.
  - One line per call or cache lookup, and one per withheld item.
  - Fields: ts, vendor_id, role, model, status, cache_key, prompt_sha256, schema_sha256, payload_sha256,
    items_sent, withheld [{id, reason}], n_out, attempts.
  - Never any passage text, quote, prompt text, API key or profile text.
  - `model_for(item_id)` returns the model of the latest successful call that sent the item, or "" if none.
  - The manifest reports the withholding rate per vendor.
- **`check_audit`** is a release gate. It returns the problems it finds; an empty list means clean. Problems include:
  - a line with a text-like field;
  - a sent item whose host the ToS register refuses;
  - a withholding with no reason;
  - the word "Meridian".

## 5. verify.py: verification gates

```python
MIN_EXCERPT = 25
MAX_EXCERPT = 600
def normalise(text: str) -> tuple[str, list[int]]
def locate(quote: str, text: str, lo: int = 0, hi: int | None = None) -> tuple[int, int] | None
def verify_claim(claim: Claim, passage: Passage | None, doc_text: str, *, title: str = "",
                 placeholders: Mapping[str, str] | None = None, entity_ok: bool = True) -> VerifyResult
def reverify_item(item: EvidenceItem, store: EvidenceStore) -> VerifyResult
```

- **`normalise`** applies NFKC, collapses each whitespace run to one space, turns curly quotes straight and turns
  dashes (‐ – — −) into "-". It also returns the index map back to the original offsets.
- **`locate`** tries an exact `text.find(quote, lo, hi)` first, then the normalised match mapped back. The span it
  returns is in `text` coordinates, and that slice is what gets stored.
- **`verify_claim`** checks the gates in this order:
  - **V1:** fails, and stops, when `passage` is None or `passage.passage_id != claim.passage_id`.
  - **V2:** after the `placeholders` are restored, the quote must be found inside
    `doc_text[passage.start:passage.end]`. Found: `start`, `end` and `excerpt` are set from the source. Not found:
    fails and stops.
  - **V3:** fails when the quote holds "..." or "…" that the excerpt does not, or when `len(excerpt)` is outside
    25–600.
  - **V4 (repair):** drops each indicator that is G6, G7, G9 or G10, whose span is not inside the excerpt
    (normalised), or that fails `rules.indicator_ok`. Any drop records V4.
  - **V5 (repair):** drops each named provider not found in the excerpt or `title` (case-insensitive, whole words,
    normalised). Any drop records V5.
  - **V6:** fails when `entity_ok` is False. The caller runs `rules.entity_ok` to get it.
  - V7 and V8 are not judged here, because the labels come from the rules in `rules.claim_item`. V9 is
    `cluster.dedupe`.

  `details` gets one plain line per failure, quoting at most 80 characters of source text.
- **`reverify_item`** backs the demo's "Re-verify" button. It recomputes four checks:
  - sha256 of the stored raw blob = `capture_sha256`;
  - sha256 of the stored text = `text_sha256`;
  - `text[start:end] == excerpt`;
  - `sha256_text(excerpt)` = `excerpt_sha256`.

  Any mismatch gives `ok=False`, `failures=["V2"]` and one detail line per mismatch.

## 6. cluster.py: dedupe, origin clusters, corroboration

```python
SIMILARITY = 90
def dedupe(items: Sequence[EvidenceItem], *, log: list[str] | None = None) -> list[EvidenceItem]
def same_origin(a: EvidenceItem, b: EvidenceItem) -> bool
def assign_clusters(items: Sequence[EvidenceItem]) -> list[EvidenceItem]
def independent(a: EvidenceItem, b: EvidenceItem) -> bool
def link_corroboration(items: Sequence[EvidenceItem]) -> list[EvidenceItem]
```

- **`dedupe`** (V9) keeps one item per `item_key`, and one per overlapping span within the same `doc_id`. Among
  duplicates it keeps the best item, compared in this order:
  1. STRENGTH_ORDER;
  2. method, ranked rule+llm_agree > adjudicated > rule > llm_proposed_accepted;
  3. the longer excerpt;
  4. item_key.

  It logs `V9:<item_key>` for each removal and preserves order.
- **`same_origin`** (§2.7) is True when either:
  - rapidfuzz `fuzz.token_set_ratio` of the normalised excerpts is 90 or more; or
  - the normalised titles are equal and at least 4 words long *(interpretation: this guards against generic
    titles)*.

  Items of different vendors are never the same origin.
- **`assign_clusters`** runs union-find over `same_origin`, processing items in item_key order. `cluster_id` is
  `min(item_key of the members)[:12]`. Partner mirrors of one release form one cluster.
- **`independent`** (§2.7) needs a different `cluster_id` and either a different publisher (case-folded) or a
  different family.
- **`link_corroboration`:**
  - For each K-candidate item k, `k.corroborates` = the sorted keys of the Q and K-candidate items that are
    independent of k.
  - IC becomes 1 for any item that some K-candidate corroborates.
  - IC becomes 5 for a U1–U4 item contradicted by a citable A/B U8 item at R3 dated the same day or later.
  - Every other IC is unchanged.

## 7. verdict.py: column O

```python
def decide(items: Sequence[EvidenceItem], plan: DepthPlan, coverage: Sequence[CoverageEntry]) -> UsageVerdict
def assign_roles(items: Sequence[EvidenceItem], verdict: UsageVerdict) -> list[EvidenceItem]
```

### Rules

`decide` uses only the citable items of `plan.vendor_id`:
- Q = `rules.is_qualifying`, and K = `rules.is_corroborating`.
- `coverage_complete = depth.coverage_complete(plan, coverage)`.
- An item's date is `published`, or else `retrieved_at[:10]`.

The rules apply in order, and the first match wins:
- **a) Conflict.** A Q, plus a citable U8 with SR A/B and RL R3 dated the same day or later. Inconclusive, with
  `conflict` set; decisive = [the Q, the U8]. An older U8 is a scope question: a trace line, not a conflict.
- **b) Confirmed.** A Q at R3, plus a K that is a different item and independent of it (`cluster.independent`).
- **c) Probable.** Any Q, or two K that are independent of each other.
- **d) Affirmed negative.** A citable U8 with SR A/B at RL R2 or higher, with no Q and no K.
- **e) Not detected.** `coverage_complete`, and no citable item has all of: SP S1 or higher, RL R2 or higher,
  RC T1 or newer, and class U1–U4 or U7.
- **f) Inconclusive** otherwise. This covers marketing only, capability only, relationship only, an inferred
  affiliate and incomplete coverage.

### Item lists

All lists hold item keys, best first: by STRENGTH_ORDER, then SR, RL and RC, then item_key.
- `qualifying` = the Q items.
- `corroborating` = the K items independent of the decisive Q (rule b), or of each other (rule c).
- `decisive` depends on the verdict:
  - Yes: the best R3 Q (rule b) or the best Q (rule c) first, then up to two more Q or K, independent ones first.
  - Inconclusive (rule f): up to two items, ranked by label order, then SR, RL and RC.
  - Affirmed negative: the U8 items.
  - Not detected: the strongest counter-evidence item (an S0 statement of planned use), if there is one.

### Wording

- **Likelihood** (§2.7):

  | Rule | Likelihood |
  |---|---|
  | b | "very likely"; "almost certain" with two or more independent SR A sources |
  | c | "likely" |
  | a, f | "roughly even chance" if a citable item has SP S1 or higher at RL R2 or higher; otherwise "unlikely" |
  | e | "unlikely" |
  | d | "very unlikely" |

- **Confidence** (§2.7), checked in this order:
  1. Low on a conflict, on incomplete coverage, or when every item is SR D–F.
  2. High with two or more independent A/B items consistent with the verdict and complete coverage.
  3. Moderate with one A/B item, two or more SR C items, or complete coverage with nothing found.
  4. Low otherwise.
- **`confidence_reason`** is a lower-case clause with no codes that completes "Confidence is {level} because …".
- **`trace`** has one plain line per rule tested.

`assign_roles` sets roles as described under "role" in §1 and returns copies in input order.

## 8. risk.py: inputs for columns S and T

This module owns `config/risk.toml`. It holds:
- E and K anchors and their cue lexicons;
- the TG mapping and the class bands;
- escalator definitions and the theme mapping;
- flip-condition templates;
- the V-000 calibration inputs.

```python
def load_risk_config(path: str | Path | None = None) -> RiskConfig
def derive_inputs(verdict: UsageVerdict, items: Sequence[EvidenceItem], criticality: CriticalityResult,
                  profile: VendorProfile, plan: DepthPlan, *, overrides: OverrideStore | None = None,
                  config: RiskConfig | None = None) -> RiskInputs
def evaluate_escalators(verdict: UsageVerdict, items: Sequence[EvidenceItem], inputs: RiskInputs,
                        criticality: CriticalityResult, *, as_of: str,
                        config: RiskConfig | None = None) -> dict[str, bool]
def score(inputs: RiskInputs, verdict: UsageVerdict, *, tier: Tier, escalators: Mapping[str, bool] | None = None,
          config: RiskConfig | None = None) -> RiskResult
def assess(verdict: UsageVerdict, items: Sequence[EvidenceItem], criticality: CriticalityResult,
           profile: VendorProfile, plan: DepthPlan, *, as_of: str, overrides: OverrideStore | None = None,
           config: RiskConfig | None = None) -> RiskResult
```

### derive_inputs (§2.8)

The pathways are the citable Q and K items.

**E per item, by locus:**

| Locus | E3 (needs an R3 item) | E2 | E1 | E0 |
|---|---|---|---|---|
| service_feature, vendor_addon | D = 4, D3-P, or a named external model processes the data | D3; or an R2 item that would give E3 (then `e_if_confirmed=3`) | otherwise | – |
| delivery_ops | the excerpt states that customer data or credentials are processed | AI works on artefacts that carry customer data: tickets, cases, incidents, exceptions, transactions, production logs, communications | otherwise | – |
| sdlc | – | production data is named | otherwise | – |
| corporate_internal | – | – | – | always |

E = the maximum, and `e_items` = the keys that reach it.

**K from action levels:**
- K3: automated action without per-case human review on customers, funds, regulatory outputs or production.
- K2: human-reviewed decisions, or autonomous action on operational items that do not affect customers.
- K1: advisory output only.
- K0: none.

**Unknowns rule.** This applies to an Inconclusive verdict, and to a Yes verdict that leaves E or K unknown. The
values are flagged assumed.
- E from data sensitivity: D4 or D3-P gives 3, D3 gives 2, anything else 1.
- K from the vendor's role: P ≥ 3 or D3-P gives 3, pay or customer-facing outputs give 2, anything else 1.

**TP** = `criticality.tier.points`.

**Gaps.** Only citable items close a check:
- t1: R3 A/B disclosure of AI use in the service.
- t2: AI providers named by the vendor itself (a DNS token does not count).
- t3: data-use terms (G3 in a LEG item).
- t4: human oversight.
- t5: an AI governance attestation (U6: NIST AI RMF, ISO/IEC 42001 or an AI policy).
- t6: AI incident or change notification.

`gap_reasons` reads "not publicly disclosed; contractual disclosure unknown", and `gap_items` holds the keys that
closed each check.

**Overrides.** A `risk_input_override` record (§10) replaces a value and its reason, written
"analyst {name} {date}: {reason}".

### Escalators and scoring

- **`evaluate_escalators`** maps X1..X6 to whether citable items evidence the condition. X5 needs an incident dated
  within 24 months of `as_of`. X6 needs a Critical tier and exactly one foundation-model provider across the Q and K
  items.
- **`score`** follows the fixed order and appends each step to `steps`:
  1. ARP.
  2. `base_class = arp_class(arp)`.
  3. Gate: `gate_met` = (E ≥ 2 and not assumed) or (K ≥ 2 and not assumed). A High or Critical base class without
     the gate becomes Medium.
  4. Escalators: for a Confirmed or Probable verdict with the gate met, each true escalator goes to
     `escalators_fired` and sets a High floor. Otherwise each true escalator goes to
     `escalators_logged_not_applied`. The class at this point is `pre_cap_class`.
  5. Cap: Confirmed "" · Probable "High" · Inconclusive "Medium" (`provisional`) · Affirmed negative and Not detected
     "None identified". `final_class = min(pre_cap_class, cap)`.

  After the cap:
  - **Ceiling.** Rerun steps 1–4 with assumed inputs treated as evidenced, `e_if_confirmed` used for E, and
    escalators counted only when evidenced. `ceiling_class` is that class when it differs from `final_class`, and ""
    otherwise.
  - **`flip_condition`** is one plain sentence from the config templates naming the single confirmation or change
    that would move the class, or "" if there is none.
  - **`themes`** use the NIST AI 600-1 and OWASP LLM Top 10 mapping in the config: RT1 data exposure, RT2
    undisclosed sub-processors and concentration, RT3 decision transparency, RT4 excessive agency, RT5 injection and
    leakage, RT6 output integrity, RT7 predictive-model risk.
- **`assess`** = `derive_inputs`, then `evaluate_escalators`, then `score(tier=criticality.tier)`.
- **Calibration test:** V-000's inputs E3 (evidenced), K3, TP3 and TG2 with a Confirmed verdict give ARP 17, the gate
  met, no cap, and Critical.

## 9. actions.py: column U

This module owns `config/actions.toml`: playbook sentences per class; gap blocks with their triggers, Q codes,
C codes and wording; the text of Q1..Q15 and C1..C11; and the monitoring lines.

```python
def load_actions(path: str | Path | None = None) -> ActionsConfig
def plan_actions(risk: RiskResult, verdict: UsageVerdict, items: Sequence[EvidenceItem], tier: Tier, *,
                 config: ActionsConfig | None = None) -> ActionPlan
```

### Playbook by class (§2.8)

| Class | Playbook |
|---|---|
| Critical | V-000's four sentences: <br>1. a questionnaire within 15 business days covering the open gap blocks; <br>2. registration on Meridian's AI sub-processor inventory, with an unresolved-provider flag when no provider is named; <br>3. AI clauses at the next contract review: training restrictions, sub-processor change notification, human-oversight thresholds, rights to review attestations; <br>4. escalation to the Third-Party Risk Committee if the vendor does not confirm. |
| High | The same four sentences, with the questionnaire due within 15 business days for a Critical-tier vendor and 30 otherwise. |
| Provisional | Q1–Q4 within 15 business days if `ceiling_class` is High or Critical, otherwise 30; reclassify on response. |
| Medium or Low, not provisional | From the config: questionnaire items within 30 business days, and an annual re-scan. |
| None identified, Critical or High tier | A written no-AI attestation, plus an AI change-notification clause at renewal; annual re-scan. |
| None identified, Medium or Low tier | Annual re-scan. |

### Gap blocks

Gap blocks come from the missing checks, the verdict and the items, and are listed in `GAP_BLOCK_CODES` order:

| Block | Trigger |
|---|---|
| SUB | t2 missing, or no sub-processor register |
| TRAIN | t3 missing |
| LOC | hosting region unknown |
| EXPL | t4 missing |
| AGENT | agentic or automated_action items |
| PI | genai_llm or conversational items |
| GOV | t5 missing |
| INC | t6 missing |
| CONC | X6, or a single provider |
| MRM | predictive_ml at K ≥ 2 |

The Q and C codes are the union of the blocks' codes, in numeric order.

### Rendered text

`text` = the playbook, then one sentence listing the gap-block topics, then monitoring. It stays within 1,480
characters, and every sentence is addressed to Meridian. No wording may have the team contacting a vendor.

## 10. Review records used by P3/P4

These records use the existing `OverrideStore` in review.py:

| kind | key | value | extra |
|---|---|---|---|
| `evidence_review` (HC2) | item_key | "accepted" or "rejected" | optional `labels`: {temporal, action_level, sp, rl, locus, u_class, ai_type} |
| `risk_input_override` | "e", "k", "t1".."t6" | "0".."3" for e and k; "missing" or "closed" for a check | optional `evidence`: [item keys], which makes the value not assumed |
| `cell_edit` | a `StudentCells` field name | the edited cell text | – |

- Evidence reviews live in `review/reviews.jsonl`, and everything else in `review/overrides.jsonl` (paths as defined
  in `review.py`).
- A reason needs at least 10 characters, and the last record wins.
- The demo uses a sandbox copy of `review/`, passed in through the `reviews` and `overrides` parameters.

## 11. compose.py: cells and sheets

```python
def assign_evidence_ids(items: Sequence[EvidenceItem], vendor_id: str) -> list[EvidenceItem]
def coverage_ids(coverage: Sequence[CoverageEntry]) -> list[str]
def build_cells(findings: VendorFindings, team: str, assessed_on: str, *, rubric: Rubric | None = None,
                depth_config: DepthConfig | None = None) -> StudentCells
def evidence_log_sheet(findings_list: Sequence[VendorFindings]) -> SheetSpec
def coverage_log_sheet(findings_list: Sequence[VendorFindings]) -> SheetSpec
def evidence_images_sheet(findings_list: Sequence[VendorFindings]) -> SheetSpec
def run_info_sheet(result: AssessmentResult) -> SheetSpec
def method_legend_p3(spec: SheetSpec) -> SheetSpec
```

### IDs

- **`assign_evidence_ids`** numbers every item of the vendor `{vendor_id}-E-0001`, `-E-0002`, … and returns the
  items in that order. The sort key is ROLE_ORDER, STRENGTH_ORDER, SR, RL descending, RC descending, url, start,
  item_key, so E-0001 is the Primary item.
- **`coverage_ids`** gives `{vendor_id}-C-01`, `-C-02`, … by position in `findings.coverage`.

### build_cells

`build_cells` is pure and fits `workbook.DEFAULT_LENGTH_BUDGETS`. Style follows V-000:
- It never truncates a quote. To fit a budget it drops lower-ranked items or negative findings first.
- Dates are DD-MM-YYYY, and URLs are written without the scheme.
- The separator is " — " (U+2014), as in V-000's N5, P5 and R5.

The simple cells:

| Cell | Content |
|---|---|
| L | `criticality.tier.value` |
| M | `criticality.render_rationale` |
| N | `depth.render_depth_cell(depth, coverage)` |
| O | `verdict.column_o` |
| S | `risk.final_class` |
| U | `actions.text` |
| V | `"{team} / {assessed_on as DD-MM-YYYY}"`, with "Team " added once when it is missing |

**P (§2.8).** Contains no hashes, no tag codes and no interpretation. What it lists first depends on the verdict:
- **Yes:** up to 3 decisive Q/K items, each written
  `"{Primary|Supporting} source — {source_type}, {publisher}, {published or 'undated'}, {url} (retrieved {date}; Evidence Log {E-ID}): "{excerpt}""`.
- **Inconclusive:** up to 2 decisive items, each prefixed by its label:

  | Label | Prefix |
  |---|---|
  | Context - relationship only | "Indicator — relationship only (DNS TXT record)" |
  | Context - platform supplier | "Indicator — platform supplier capability" |
  | Context - inferred affiliate | "Indicator — inferred affiliate, not confirmed" |
  | Marketing only | "Marketing statement — not confirmatory" |
  | Weak | "Indicator — weak" |

- **No:** the strongest "Limiting statement" or "Counter-evidence" item.

Every verdict then adds:
1. `"Negative finding — {what} not found on {where} (Coverage Log {C-ID})."` for each complete family that had no AI
   passage, and for each missing register;
2. the closing sentence `"Entries are recorded in the Evidence Log sheet with retrieval dates and screenshots."`

**Q:**
- The pattern is `"{n} usage pathways are indicated. First, … (E-IDs). … The extent of {unknowns} is undetermined from public material and is carried forward to the vendor questionnaire."`
- DNS tokens and platform options are labelled "relationship indicator, not a confirmed sub-processor".
- With no pathway, Q is a plain sentence: "No usage pathway is evidenced …".

**R** lists only providers the vendor itself names publicly: citable first-party SR A/B items that name providers,
each with its role and E-ID. With none:
`"None named by the vendor — {verdict label}. No public sub-processor register found (searched …). Provider identity, region and retention are raised with the vendor."`

**T** is plain sentences with no codes, in this order:
1. The class, with "Provisional" when capped.
2. The data involved, in the profile's words.
3. The AI evidence and what it touches (E-IDs).
4. "It is {likelihood} that the vendor uses AI in {service}." then "Confidence is {confidence} because
   {confidence_reason}."
5. Dependency.
6. Transparency gaps.
7. The score in words, for example "exposure 2/3, decision impact 2/3, tier 3, transparency gap 1 → 12 of 18 = High
   (exposure and decision impact count double)".
8. The cap, ceiling or flip sentence, for example "Provisional; ceiling Critical if confirmed".

### Sheets

- **`evidence_log_sheet`** ("Evidence Log") has one row per item, including rejected items and traps. Vendors appear
  in order, with each vendor's items ordered by E-ID. Headers: Evidence ID, Vendor ID, Role, Strength, Tags, Family,
  Source type, Publisher, Title, URL, Retrieved URL, Published, Date basis, Retrieved (UTC), Excerpt, Offsets,
  Excerpt SHA-256, Capture SHA-256, Text SHA-256, Screenshot, Screenshot SHA-256, Visible in render, Providers,
  Data mentioned, Temporal, Action level, Method, LLM model, Prompt SHA-256, Rule labels, LLM labels, Cluster,
  Corroborates (E-IDs), Review, Review reason, Reviewer, Item key.
- **`coverage_log_sheet`** ("Coverage Log") uses `sheets.COVERAGE_HEADERS` with a "Coverage ID" column added first.
- **`evidence_images_sheet`** ("Evidence Images") has the columns Evidence ID, Vendor ID, Screenshot, SHA-256,
  Visible in render, URL. Embedding the images needs a `workbook` extension; until then the sheet holds references
  only.
- **`run_info_sheet`** ("Run Info") has key/value rows from `result.manifest`: run id, mode, as_of, input SHA-256,
  config and prompt hashes, models and switches, call counts, withholding rates, code version.
- **`method_legend_p3`** appends sections through `sheets.add_section`:
  - the tag legend (U, SR, SP, RL, RC, IC, loci);
  - the strength label order and the P prefixes;
  - the four genuine-vs-marketing tests;
  - the G and M indicators;
  - verdict rules a–f and the ICD 203 wording;
  - the risk matrix: E, K, TP, TG, bands, gate, X1–X6, cap, ceiling;
  - Q1–Q15 and C1–C11;
  - the service-term dictionaries with their reasons;
  - the query templates.

## 12. capture/shots.py: excerpt-anchored screenshots

```python
SHOTS_INDEX = "shots/index.jsonl"   # under the store root
class ShotPolicy(Protocol):
    def check(self, url: str) -> tuple[bool, str]   # ToS register -> robots -> rate-limit wait; counts the request
def screenshot_excerpt(capture: Capture, excerpt: str, start: int, end: int, store: EvidenceStore,
                       policy: ShotPolicy, *, document: Document | None = None,
                       browser: Any = None) -> tuple[str, str, bool | None]
def find_shot(item_key: str, store: EvidenceStore) -> tuple[str, str, bool | None] | None
```

`screenshot_excerpt` returns (repo-relative path, sha256 of the PNG bytes, visible).
- **Manual-only hosts** (fiserv.com, linkedin.com) are never loaded. A manual capture's own `screenshot_path` is
  reused with its sha and visible None; with no such screenshot the result is ("", "", None).
- **HTML:**
  - Playwright Chromium with the honest UA `footprint-osint/1.0`.
  - Every page load passes `policy.check`; FSSI's budget of about 25 requests per host includes screenshot loads.
  - Third-party trackers are blocked. No stealth and no evasion.
  - It finds the excerpt text in the rendered DOM, scrolls to it and clips its bounding box with a margin, then
    stores the PNG with `store.put_shot(png, ".png")`; visible is True.
  - If the excerpt is not found, the result is ("", "", False).
- **PDF:** renders the page that holds `start` (found through `document.pages`) from the stored bytes with
  pypdfium2, offline; visible is True.
- **Index:** each result is appended to `SHOTS_INDEX` as {item_key, capture_id, url, path, sha256, visible,
  taken_at}. `find_shot` returns the latest entry for a key, which is what replay uses.
- **Item key** = `EvidenceItem.make_key(capture.url_final or capture.url_requested, sha256_text(excerpt))`.
- **Tests:** unit tests use a fake browser object; real Playwright tests are `@pytest.mark.network`.

## 13. evaluate.py: gold set and Gemini-vs-rules comparison

```python
def load_gold(path: str | Path = "tests/gold/gold_v1.json") -> list[dict]
def gold_report(runs: AssessmentResult | Sequence[VendorFindings], gold: Sequence[dict], *,
                captures: Sequence[Capture] | None = None) -> dict
```

- **Matching.** Gold rows match items by vendor and normalised URL (`url` or `url_final`). Normalising lower-cases the
  scheme and host and drops the fragment and any trailing slash.
- **Expected labels.**

  | Gold `expect` | Matching strength |
  |---|---|
  | strong | Strong |
  | moderate | Moderate |
  | weak | Weak or a Context label |
  | marketing-only | Marketing only |

- **Output.** A JSON-serialisable dict with sorted keys:
  - `gold_items`, `captured`, and `captured_pct` (needs `captures`);
  - `matched`, `label_agreement`, `confusion` {expect: {strength or "none": n}};
  - `by_vendor`, `unmatched` [{vendor_id, url, reason}];
  - `llm` {items_with_llm, agree, disagree, disagree_by_field};
  - `traps` {items, rejected}.

The function is pure and offline.

## 14. bundle.py: Colab bundle

```python
DEFAULT_INCLUDE = ("pyproject.toml", "uv.lock", "README.md", "CLAUDE.md", "config", "prompts", "seeds", "src", "app",
                   "notebooks", "data/input", "evidence", "runs", "review", "tests/gold")
def build_bundle(out_zip: str | Path, *, root: str | Path = ".", include: Sequence[str] = DEFAULT_INCLUDE) -> dict
```

- **Output.** A deterministic zip (sorted entries, fixed timestamps, deflate) plus `BUNDLE_MANIFEST.json`, which maps
  each path to its sha256. It returns {path, files, bytes, sha256}.
- **Excluded:** `.env`, `.git`, `.venv`, `__pycache__`, `scratch`, `submission`, and anything git-ignored as a
  secret.
- **Secret scan.** It refuses with a ValueError when a text file matches a secret pattern (`AIza…`, `gh[pousr]_…`,
  `github_pat_…`). The error names the files, never the values.
- **Never published.** It never uploads or publishes anything, and the zip stays outside git.

## 15. pipeline additions (UI agents code against these)

```python
class InputError(ValueError):   # the workbook failed validation
    issues: list[ValidationIssue]

def run_assessment(xlsx: WorkbookSource, mode: AssessmentMode = "replay", *, vendors: Sequence[str] | None = None,
                   as_of: str | None = None, store: EvidenceStore | None = None, seeds_dir: str | Path = "seeds",
                   runs_dir: str | Path = "runs", overrides: OverrideStore | None = None,
                   reviews: OverrideStore | None = None, llm: LLM | None = None, fetcher: Any = None,
                   team: str | None = None, write: bool | None = None,
                   progress: Callable[[str, float], None] | None = None) -> AssessmentResult
def rescore_vendor(findings: VendorFindings, *, as_of: str, team: str, overrides: OverrideStore | None = None,
                   reviews: OverrideStore | None = None) -> VendorFindings
def export_assessment(result: AssessmentResult, xlsx_in: WorkbookSource, out_path: str | Path | BinaryIO,
                      team: str, *, overwrite: bool = False) -> WriteReport
```

### run_assessment

**Input and scope**
- It reads and validates the workbook, raising `InputError` with the issues when it is not ok.
  `input_sha256` = sha256 of the input bytes.
- `vendors` limits the run to those ids, keeping sheet order. An unknown id raises ValueError.
- `as_of` defaults:
  - replay: the as_of in the newest collection-run manifests of the selected vendors. They must agree; otherwise it
    raises ValueError asking for one.
  - live modes: today's UTC date, taken once.

**Modes**
- replay: no network at all (`ReplayFetcher`, `make_llm("replay")`, `find_shot`).
- live_rules: live collection with `NullLLM`.
- live_ai: live collection with `CachedLLM(GeminiLLM())`, falling back to NullLLM without a key.
- Injecting `llm` or `fetcher` overrides the mode's choice, for tests.

**Per vendor**
- It follows §2.
- Gemini calls are capped by `plan.gemini_calls`.
- The guard is built from:
  - the D/E/H/J/K values of every workbook row;
  - `FOOTPRINT_TEAM_NAME`;
  - the vendor's `person_scrub` list.
- The vendor's manual captures (`Capture.manual`) are included as documents.
- High-priority triaged pages also go to extraction as whole-page chunks: `Passage` objects with `hits=[]`, up to
  30,000 characters, kept in the passage lookup used for V1.

**Calibration example.** `example` holds the V-000 calibration findings and is never written to the inventory:
- criticality and depth from its profile;
- `risk.score` on the calibration inputs in `config/risk.toml`, with a Confirmed calibration verdict;
- evidence [];
- cells composed for comparison only.

**Team.** `team` defaults to the env var `FOOTPRINT_TEAM_NAME`, loaded by `load_dotenv`. Without it column V stays
None.

**Writing.** `write` defaults to True for live modes and False for replay. It writes `runs/<run_id>/`: `assessment.json`,
`evidence.jsonl`, `coverage.jsonl`, `llm_calls.jsonl`, `manifest.json`. It never writes the input workbook.

**Run id.** `run_id` = `"A-{as_of without dashes}-{sha8}"`, where sha8 is taken over input_sha256, mode, vendor ids,
config hashes and prompt hashes.

**Manifest keys**
- run_id, mode, as_of, input_sha256, code_version.
- created_at (metadata only).
- config_sha256 {rubric, depth, lexicon, sources, risk, actions, tou}.
- prompts_sha256 {expand_v1, triage_v1, extract_v1}.
- llm {models, switches, calls {vendor: {planned, actual, cache_hits}}, withheld {vendor: {count, rate}}}.
- collection_runs {vendor: run_id}.
- counts {vendor: {items, cited, proposed, rejected}}.

**Progress.** `progress(message, fraction)` is called after each stage for each vendor; it drives the UI progress bar
and is optional.

**Determinism.** The same workbook, store, caches, reviews and as_of give identical `cells()`.

### rescore_vendor

It reapplies reviews and overrides and recomputes stages 5–10. It does no collection, no LLM calls and no
screenshots. The UI calls it after an accept, a reject, or an E/K/gap edit, to show the live class.

### export_assessment

1. It refuses (ValueError) when sha256(xlsx_in) ≠ `result.input_sha256`.
2. It writes every vendor's L–V through `write_workbook`, with column V rendered from `team` and `result.as_of`.
3. It appends the sheets in this order:
   1. Evidence Log
   2. Coverage Log
   3. Criticality Workings
   4. Method & Legend (the P1 sections plus `method_legend_p3`)
   5. Evidence Images
   6. Run Info
4. It sets `last_modified_by=team` and pins `modified` to `as_of`, so the output is byte-identical across runs.
5. It runs `check_fidelity`, raising `FidelityError` on any problem.

`out_path` may be a binary stream, for the Streamlit download.

### UI usage

- **Excerpt in context:** `store.get_text(item.doc_id)[max(0, item.start - 300):item.end + 300]`.
- **Screenshot:** `item.screenshot_path`.
- **Rules vs Gemini:** `item.rule_labels` vs `item.llm_labels`, with `item.label_disagreements`.
- **Accept or reject:** add
  `ReviewRecord(kind="evidence_review", vendor_id=..., key=item.item_key, value="accepted"|"rejected", reason=..., analyst=..., date=...)`
  to the reviews store, then call `rescore_vendor`.
- **E/K/gap editors:** add `risk_input_override` records, then call `rescore_vendor`.
- **Why trace:** `verdict.trace` plus `risk.steps`.
- **Re-verify:** `verify.reverify_item(item, store)`.
- **Download:** `export_assessment(result, uploaded_bytes, io.BytesIO(), team)`.

**CLI wiring (integrator):**
- `footprint assess <xlsx> --mode … [--vendor …] [--as-of …]`
- `footprint export …`
- `footprint verify` (replay reproduces the submitted cells)
- `footprint bundle`

## 16. Module map (suggested split)

| Module | Config or data it owns | Must not touch |
|---|---|---|
| `rules.py` | `config/sources.toml`; new `config/lexicon.toml` tables | `[core]`, `[guards]`, `[suppressors]` |
| `ai.py` | `prompts/*_v1.txt`; `evidence/llm_cache/`; `evidence/ai_policy.jsonl` | profile objects; seed service terms |
| `verify.py` | – | – |
| `cluster.py`, `verdict.py` | – | – |
| `risk.py`, `actions.py` | `config/risk.toml`; `config/actions.toml` | – |
| `compose.py` | – | the rules in `workbook.py` |
| `capture/shots.py` | `evidence/shots/` | – |
| `evaluate.py` | – | `tests/gold/gold_v1.json` (read only) |
| `bundle.py`, `pipeline.py`, `cli.py` | `runs/A-*` | – |
| `models.py`, this file | contracts | – |

Send a change request for a module you do not own to its owner, with the exact signature and the reason.

## 17. Interpretations and open points

1. **No §2.10 in the design.** The task names a §2.10 for traceability fields, but the design has none. The
   EvidenceItem traceability fields follow the main plan's "Traceability & workbook I/O" (URL, UTC time, SHA-256 of
   the capture, text and excerpt, screenshot) and the §2.8 P template.
2. **G9 and G10.** They are inside the schema's G1..G11 range but undefined in §2.3. V4 drops them, and `extract_v1`
   lists only the defined codes.
3. **IC 4.** It is undefined in §2.3, so the Admiralty meaning is used: "doubtful", that is, uncorroborated C/D.
4. **V4 and V5 repair rather than reject.** The verified quote survives without the unverified indicator or
   provider name. V1, V2, V3, V6 and V9 reject.
5. **"M2/M3 dominate" (S0)** is defined in the `specificity` rules in §3.
6. **Traps.** Definition-test traps are kept as `ai_type="not_ai"` items, never citable, so the demo can show the
   FSSI trap being rejected. Other non-AI passages (suppressors, collisions, entity failures) are dropped, with the
   reason in the log.
7. **K requires U1–U4,** because a corroborating signal must corroborate use.
8. **Dash style.** V-000 uses " — " (U+2014) in N5, P5 and R5, while the `config/depth.toml` labels use " – "
   (U+2013) in column N. Compose follows V-000. Whether to align depth.toml (a change to N cells, owned by depth) is
   an open decision for the user.
9. **Dates.** P and V use DD-MM-YYYY, as V-000 does. The template row supersedes the "14 May 2026" example in §2.8.
10. **Robots bodies** are not kept in the evidence store, so `HostAiPolicy` records the Google-Extended and
    Content-Signal decisions in `evidence/ai_policy.jsonl` during live runs, for replay to read.
11. **Evidence Images.** Embedding needs a `workbook.write_workbook` extension, because SheetSpec cannot hold images.
    Until then the sheet lists references and hashes (the design's `--no-images` form).
