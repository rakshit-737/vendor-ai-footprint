# Design — footprint (Optiv case study 1, Meridian vendor AI-usage analysis)

Status: approved 2 Oct 2026. Source: plan approved by the user, built from an 18-agent research and design workflow.


## Context
Optiv Consulting case study (released Sept 2026; team submission ~3 weeks from release; today Fri 2 Oct 2026;
exact deadline unknown → plan targets **ready-to-submit Tue 13 Oct**, Wed 14 Oct buffer).
Meridian's TPRM team needs a repeatable, evidence-driven way to tell whether a vendor *genuinely* uses AI in
the service it provides (vs. AI marketing), from the vendor's **public footprint only**, and to surface the
resulting risk (data exposed to AI models, undisclosed AI sub-processors, opaque decisions).
Inputs in `D:\Optiv`: `Case Study Brief.pdf`, `Meridian_Vendor_Input.xlsx` (6 real vendors V-001..V-006 +
fictional worked example V-000; student cream cells L–V; V-000 cites an "Evidence Log" sheet that is absent).

Required outcomes (10–15 min PPTX walkthrough): 01 Functional Design Diagram · 02 Architecture Diagram ·
03 Source Strategy + evidentiary-weight logic · 04 Criticality Ratings + criteria + depth per rating ·
05 Working demo (upload vendor list → analyze → AI security risk per vendor). Findings live in the Excel workbook.
Confidentiality (brief + workbook): **no public repo, no published presentation, no vendor contact.**

This plan was produced from a read-only research + design workflow (18 agents: Gemini API status, keyless
OSINT source probes, TPRM/evidence frameworks, live scouting of all 6 vendors, 3 competing architectures,
3 judges, synthesis, completeness critic). Full detailed spec is in the session scratchpad and will be
committed as `docs/design.md` in P0, trimmed to match this plan's scope.

## Locked decisions (user, 2 Oct)
- Case Study 1 only (Cadence CS2 files ignored).
- **Private** GitHub repo `rakshit-737/vendor-ai-footprint` at `D:\Optiv\vendor-ai-footprint` (subfolder —
  keeps brief PDF, CS2 files, `.opencode` out). Commits code, input workbook, completed workbook, evidence pack, deck.
- LLM = Google Gemini key only (free tier assumed). Keyless rules path must always work.
- Demo = Streamlit app + Colab/Jupyter notebook over one engine.

## Key research facts that shape the design
- Gemini free tier: `gemini-3.5-flash-lite` (~500 req/day, unverified) for extraction/triage; fallback
  `gemini-3.1-flash-lite`. SDK `google-genai>=2.27,<3` (supports Py 3.14). Temperature deprecated on 3.x → use
  `seed`, minimal thinking, schema output, caching. **Search grounding is paid-only on 3.x and its terms forbid
  harvesting links** → discovery must be our own OSINT. **Free-tier prompts may be used for training / human
  review** → only public vendor text goes to Gemini; Meridian-internal fields never do.
- Keyless sources verified live 2 Oct: DNS TXT via DoH (OpenAI+Anthropic tokens at Fiserv, BNY, Terrapin;
  OpenAI at TCH; none at FSSI/AutomWorx) · SEC EDGAR full-text + submissions (Fiserv CIK 798354, BNY CIK 1390777)
  · ATS APIs (TCH Workday wd108, BNY Oracle Recruiting Cloud, FSSI Workable; Fiserv Phenom→Workday) · WordPress
  REST (AutomWorx, FSSI, Terrapin) · Wayback availability/CDX (for Akamai/Cloudflare-blocked pages).
- **fiserv.com Terms §7 forbid spiders/data mining** → fiserv.com is manual-capture only, never sent to Gemini.
  BNY robots disallows `/careers/`, `/insights/`, `/about-us/newsroom/` (root-anchored); TCH disallows `*/media/`.
  `urllib.robotparser` mis-parses both on 3.14.3 → use **Protego**.

## Design

### Principles
1. **Criticality first, from profile only** (cols B–K) — deterministic rubric; OSINT never changes the tier.
2. **AI proposes, code verifies, analyst approves.** Gemini expands names, triages URLs (by ID), extracts
   claims; code verifies every quote is an exact slice of a captured source; rules assign final tags/verdicts;
   analyst accepts/rejects evidence and records overrides with reasons. LLM never outputs URLs or final labels.
3. **Capture before cite.** Nothing is cited unless captured (raw bytes + SHA-256 + UTC + URL), text-extracted,
   and quote-verified. Screenshot for every Primary excerpt.
4. **Replayable.** Frozen evidence pack + LLM cache → offline replay reproduces identical L–V cells; the demo
   never depends on network or quota.
5. **Practice what we assess.** Public text only to Gemini; payload guard; ToS + robots register; no vendor contact.

### Functional flow (Outcome 01)
```
Upload xlsx → C1 Intake/validate (header aliases, cream-cell map, skip V-000)
→ C2 Criticality (rubric on B–K) ── HC1 analyst confirms tier (override needs reason)
→ C3 Depth planner (tier + modifiers → mandatory source families, budgets, stop rules)
→ C4 Discovery (seeds, DNS, sitemap/WP-REST, SEC, ATS, provider stories, Wayback) ⇄ Gemini expand/triage
→ C5 Compliant capture (ToS register → robots/Protego → rate limit → GET | Playwright shot | manual import)
     → content-addressed store (raw, SHA-256, UTC, headers)
→ C6 Extraction (trafilatura/pypdf → dated text → sentences+offsets → lexicon passages)
→ C7 Signal analysis (rules tagger ∥ Gemini claim extraction via payload guard) → verifier V1–V9 → tags
     ── HC2 evidence review (accept/reject+reason)
→ C8 AI-usage verdict (rules a–f, ICD-203 wording) → C9 Risk engine (E,K,TP,TG → class) + action playbook
→ C10 Compose L–V + Evidence Log/Coverage Log/Criticality Workings/Method sheets → export + run manifest
```

### Architecture (Outcome 02)
```
vendor-ai-footprint/
  pyproject.toml uv.lock README.md CLAUDE.md .gitignore .gitattributes(eol=lf; evidence -text) .env.example
  config/  rubric depth sources lexicon risk actions tou settings (.toml, versioned, hashed into manifest)
  prompts/ expand_v1 triage_v1 extract_v1          seeds/ V-001..V-006.toml (aliases, CIK, ATS ids,
  data/input/Meridian_Vendor_Input.xlsx (SHA pinned)        curated URLs + discovery query/date, collisions)
  src/footprint/
    models.py  workbook.py  criticality.py  depth.py  pipeline.py  cli.py (typer)  review.py
    net/      fetcher.py (Live|Replay)  robots.py (Protego)  tou.py  ratelimit.py
    capture/  store.py (blobs+SHA)  shots.py (Playwright, excerpt-anchored)  manual.py (import)
    collectors/ dns.py site.py wordpress.py sec.py jobs.py (Workday|Oracle|Workable) wayback.py providers.py seeds.py
    extract.py  rules.py (lexicon, suppressors, entity guard, G/M indicators, tags)
    ai.py (Gemini|Cached|Null, payload guard)  verify.py (V1–V9)  verdict.py  risk.py  compose.py
  app/ streamlit_app.py + pages (Assess · Evidence · Findings & Risk · Export)
  notebooks/footprint_demo.ipynb      deck/build_deck.py (python-pptx, native editable shapes)
  evidence/{blobs,text,shots,llm_cache}  runs/<run_id>/{manifest.json,evidence.jsonl,coverage.jsonl}
  review/{reviews.jsonl,overrides.jsonl}  submission/  tests/{unit,fixtures,gold}  docs/design.md
```
Libraries: installed (openpyxl, pydantic, requests, lxml, rapidfuzz, typer, rich, playwright, python-pptx,
streamlit, pandas, pytest, pypdf) + install `google-genai`, `trafilatura`, `protego`, `ipykernel`/`notebook`.
`requires-python >=3.11` (Colab ~3.12). Heavy deps as extras so Colab installs core only.
Run modes: `replay` (default; from frozen pack + caches, no network) · `live_rules` · `live_ai`.
Single entry point: `run_assessment(xlsx, mode) -> AssessmentResult` used by CLI, app, notebook.

### Source strategy & evidentiary weight (Outcome 03)
| Family | Method (GET only; ToS→robots→rate checks) | SR start | C / H / M / L depth |
|---|---|---|---|
| LEG privacy, terms, DPA, sub-processors, AI policy, trust | footer/sitemap/slug discovery; trust-platform CNAME | A | all tiers |
| REG SEC 10-K/10-Q/8-K/DEFA14A | submissions JSON + EFTS full-text (AI + product terms, 24 mo) | A | registrants: M/M/1 query/– |
| PRD product, docs, release notes, whitepapers, IR | homepage links depth 2, sitemaps, WP-REST sweep, PDFs | B (blog C) | full/full/keyword/homepage |
| JOB vendor's own ATS | Workday sitemap+job JSON, Oracle ORC REST, Workable API | B (S≤2) | ≤120/≤60/keyword/– |
| DNS AI-vendor TXT tokens, SPF, CNAME | DoH (dns.google + Cloudflare agree) | B, relationship only | all tiers |
| IND provider stories, partner releases, trade press | provider sitemaps filtered by alias; seeds | C (corroboration) | M/M/–/– |
| HIST Wayback first-seen / blocked-page copies | availability + CDX, read-only, never Save Page Now | dating | M/lead/–/– |
| EXEC executive posts/talks | manual capture only (optional) | B | M/lead/–/– |
Excluded with reasons (slide 6): search-engine scraping/ddgs/Google News RSS (ToS, irreproducible), Gemini
grounding (paid + link-harvest ban), automated LinkedIn/Glassdoor (ToS), Save Page Now (public trace),
patents (capability ≠ use), GDELT/HN/OpenAlex (low yield/reliability), logins/NDA portals.
**Tags** per item: `U(class 1–8) · SR(A–F) · SP(S0–S3) · RL(R0–R3) · RC(T0–T3) · IC(1–6)`.
Genuine-vs-marketing: 4 ordered tests — definition (EU AI Act Art 3(1): scheduling/RPA/rules/templating/IMb
≠ AI), subject, use-state (deployed vs "will/exploring"), specificity (G1–G11 genuine vs M1–M7 marketing
indicators → S3/S2/S1/S0). Strength label order: Negative > Strong (Q at R3) > Moderate > Context (DNS token,
platform supplier, inferred affiliate) > Marketing-only > Weak. Entity guard + collision lists (Telik
"Terrapin", Autoworx, "FSSI" GSA programme, "Claude Reumert", MCP=Microsoft Certified Professional).

### Criticality rubric v1.0 (Outcome 04) — profile fields only, anchored on OCC 2023-17
Factors 0–4: **O** operational dependency (×7), **D** data sensitivity (×6; D3-P = privileged prod access/
credentials), **P** payment-flow involvement (×5), **R** regulatory exposure (×4), **V** customer-data volume (×3).
Score = 7O+6D+5P+4R+3V (0–100): Critical ≥80 · High 50–79 · Medium 25–49 · Low ≤24.
Floors: F1 O=4→Critical · F2 P≥3∧O≥3→Critical · F3 D=4∧V≥3→≥High · F4 D3-P→≥High · F5 D≥3→≥Medium ·
F6 D=4∧V≥3∧O≥3→Critical. Each factor logs level + anchor + verbatim trigger phrase; one-step sensitivity
and ±1 weight perturbation reported (all tiers stable across 70 perturbations).
| Vendor | O D P R V | Score | Floors | Tier |
|---|---|---|---|---|
| V-000 (calibration) | 3 4 2 4 3 | 80 | F3 F5 F6 | Critical ✔ matches example |
| V-001 AutomWorx | 3 3P 2 2 0 | 57 | F4 F5 | **High** |
| V-002 Fiserv | 4 4 4 4 4 | 100 | F1 F2 F3 F5 F6 | **Critical** |
| V-003 FSSI | 2 4 1 4 4 | 71 | F3 F5 | **High** |
| V-004 Terrapin | 2 3 2 3 2 | 60 | F5 | **High** |
| V-005 BNY | 3 3 3 4 3 | 79 | **F2** F5 | **Critical** (payment-path floor) |
| V-006 TCH | 4 3 4 4 4 | 94 | F1 F2 F5 | **Critical** |
Column M = plain-language rationale in V-000's style (codes only in the Criticality Workings sheet).

### Depth policy
| Tier → col N label | Mandatory families | Budget (fetches / Gemini calls / analyst time) |
|---|---|---|
| Critical → "Full review – tier-driven" | LEG REG* PRD JOB DNS IND HIST (+EXEC manual) | 100 / 40 / 3 h |
| High → "Standard review – tier-driven" | LEG PRD JOB DNS IND REG* | 50 / 20 / 1.5 h |
| Medium → "Focused review" | LEG, PRD keyword scan, JOB keyword, DNS, REG* 1 query | 0 / 6 / 30 min |
| Low → "Screen" | homepage, privacy, sub-processor/trust page, DNS | 0 / 2 / 15 min |
Modifiers: M-D4 (FSSI: LEG+HIST at Critical depth), M-D3P (AutomWorx: delivery-AI module — embedded tenants/
affiliates, platform-maker AI terms), M-Private (no SEC → deeper LEG/HIST). Stop when usage/sub-processor/
data-use questions answered or logged as gaps, or saturation (no new S≥2/R≥2 cluster in last 10 (C) / 6 (H)
actions), or budget spent. Every mandatory family ends with a status (done, done_manual, not_applicable,
stopped, blocked_robots, blocked_tou, blocked_bot, error) → Coverage Log = negative evidence. Non-OSINT steps
(questionnaire, contract review, SOC2/ISO 42001 validation) are "reserved for Meridian" in col N.

### AI-usage verdict (col O) & AI security risk (cols S–U)
Verdict rules in order: a) conflict → Inconclusive · b) ≥1 qualifying signal at R3 + independent corroboration
→ **Yes (Confirmed)** · c) qualifying at R2+ or ≥2 independent corroborations → **Yes (Probable)** · d) A/B
limiting statement, no signal → No (Affirmed) · e) all mandatory families complete, nothing found → **No (Not
detected)** · f) else **Inconclusive**. ICD-203 likelihood + separate confidence sentence in T.
Risk: **ARP = 2E + 2K + TP + TG** (0–18) where E = Meridian data exposed to AI (0–3, E3 needs R3 evidence),
K = decision impact (0–3), TP = tier points (C3/H2/M1/L0), TG = transparency gaps (6 checks → 0–3).
Class: Critical 14–18 · High 10–13 · Medium 6–9 · Low 0–5. Fixed order: score → base class → materiality gate
(High+ needs evidenced E≥2 or K≥2) → escalator floors X1–X6 (Confirmed/Probable only) → verdict cap last
(Probable ≤High; Inconclusive ≤Medium "Provisional" + "ceiling if confirmed" in T; No → None identified).
Calibration: V-000 = 3·3·3·2 → 17 Critical ✔. Actions: playbook by class (V-000's 4-sentence pattern:
questionnaire within 15/30 business days, AI sub-processor inventory, contract AI clauses, escalation) + gap
blocks (SUB, TRAIN, LOC, EXPL, AGENT, GOV, INC, CONC) → questionnaire items Q1–Q15. All actions addressed to Meridian.

**Expected outcomes (hypotheses from scouting; pipeline + review decide):**
| Vendor | Tier | AI usage | Risk | Flip condition |
|---|---|---|---|---|
| AutomWorx | High | Inconclusive (affiliate Labarum AI "NEO" LLM/RAG claims; Automic BYO-LLM context) | Medium – Provisional (ceiling Critical) | NEO confirmed on Meridian work |
| Fiserv | Critical | Yes – Probable (10-K "embedding AI… account processing"; agentOS/Bedrock; OpenAI) | High | AI confirmed on DNA → Critical |
| FSSI | High | No – Not detected (marketing blogs only) | None identified | GenAI in composition tools on Meridian data |
| Terrapin | High | Inconclusive (OpenAI+Anthropic DNS tokens; silent disclosures) | Medium – Provisional (ceiling High) | staff GenAI on Meridian data |
| BNY | Critical | Yes – Confirmed (RTP page "AI-enabled anomaly detection"; ~70% payment screening reviewed by AI) | High | external models on RTP data / no per-case review → Critical |
| TCH | Critical | Yes – Probable (NOC job post: Copilot/ChatGPT/ServiceNow AI; AI-assisted SDLC) | High | isolated GenAI, no payment data → Medium |

### Confidentiality & ethics controls
- Repo private; pre-push hook + every gate checks `gh repo view --json visibility` = PRIVATE; no Pages/forks/gists;
  deck and workbook never published (no Artifact/Docs publishing). Secrets only in `.env` / Colab userdata;
  secret-scan test (AIza, gho_, ghp_, github_pat_…).
- Gemini payload guard: closed `PublicPayload` type (company + public passages only); withhold passages
  containing "Meridian"/team name/distinctive profile free-text shingles; redact emails/phones; skip hosts with
  `ai_processing_allowed=false` (fiserv.com, LinkedIn); audit log `llm_calls.jsonl` is a release gate.
- Collection: GET only; ToS register `tou.toml` per host class; Protego robots with honest UA `footprint-osint/1.0`;
  1 req/s/host (SEC 5/s with contact email it requires, CDX 0.4/s, FSSI ≤25 req/run); no bot evasion, no
  logins/forms; blocked pages → manual capture or logged gap. Playwright blocks third-party trackers.

### Traceability & workbook I/O
- Store: `evidence/blobs/<sha256>.gz` + meta (URLs, status, headers, UTC, robots/ToS decision, collector);
  text store with offsets; excerpt-anchored screenshots (Chromium clips; PDFs via pypdfium2).
- Writer: only cream cells L–V rows 6–11; never touches V-000/provided cells; never reads/writes column
  dimensions M–O / S–T (single `<col>` spans L–O, R–T → Excel repair prompt); strings forced (no formula
  injection); per-column length budgets; raise row heights 6–11. Appends sheets: **Evidence Log**,
  **Coverage Log**, **Criticality Workings**, **Method & Legend**, **Run Info**. Fidelity test = semantic
  diff of all provided cells/styles/merges/sheets + manual open in Excel.
- Col P format: `"Primary source – {type}, {publisher}, {date}, {URL} (retrieved {date}; Evidence Log {E-ID}): "{verbatim}""`
  + negative findings + "Entries are recorded in the Evidence Log sheet with retrieval dates and screenshots."
  Col V: `Team {name} / {DD-MM-YYYY}` (template's format).

### Demo (Outcome 05)
- Streamlit (localhost, usage stats off): Assess (upload, validation, criticality cards, HC1 confirm, depth plan,
  mode) → Evidence (excerpt in context + screenshot, rule vs Gemini labels, accept/reject) → Findings & Risk
  ("why" trace, E/K/gap editors with reason, live class) → Export (validated xlsx download).
- Notebook (Colab + local Jupyter): upload → criticality → depth → run (replay; optional live one vendor) →
  tables → export/download → verify. Colab gets code + frozen pack from `footprint_bundle.zip` (private release
  asset, not in git); Gemini key via `userdata`.
- Live mode works for any new vendor list (criticality instant; time-boxed collection). Demo default = offline
  replay (<60 s upload→export) + one live DNS lookup. Plan B local Jupyter; Plan C recording.

## Deck outline (13 slides, ~13:15; python-pptx native editable diagrams; team branding, not Optiv's)
1 Title · 2 Answer first (tier × usage × risk × next action) · 3 Approach ("AI proposes, code verifies,
analyst approves") · 4 Functional design (O1) · 5 Architecture (O2) · 6 Sources + why each thins below
Critical + exclusions (O3) · 7 Evidentiary weight: tag card, 4 tests, FSSI trap vs BNY strong (O3) ·
8 AI layer + payload guard + verification + Gemini-vs-rules comparison · 9 Criticality rubric + 6 tiers +
V-000 calibration + sensitivity (O4) · 10 Depth ladder + stop rules (O4) · 11 Findings & risk: BNY chain
excerpt→tags→verdict→ARP→action; fourth-party AI concentration · 12 Live demo 3:00 (O5) · 13 Recommendations,
controls, OSINT limits. Appendix: rubric anchors, tag legend, per-vendor slides, demo screenshots.

## Phases (each gate: tests green, push + tag, repo PRIVATE)
| Phase | Target | Build | Gate |
|---|---|---|---|
| **P0 Setup** | Fri 2–Sat 3 Oct | Repo subfolder, git, private `gh repo create`, `.gitignore` allowlist, `.gitattributes`, pre-push hook, `CLAUDE.md` invariants, uv env (3.14), deps, `docs/design.md`; Gemini smoke test (models + one schema call; record quota); workbook round-trip probe; `tou.toml` draft | PRIVATE; pytest green; Gemini OK (else rules-only noted); round-trip leaves provided cells identical |
| **P1 Deterministic core** | Mon 5 Oct | models, workbook reader/writer + fidelity test, criticality (floors, sensitivity), depth planner, M/N templates, Criticality Workings + Method sheets, HC1 overrides | 7 tier pins pass (incl. V-000); Excel opens without repair; you sanity-check tiers |
| **P2 Collection** | Tue 6–Wed 7 Oct | fetcher (Live/Replay), Protego, ToS, rate limits, store, manual import; collectors DNS, site+WP, SEC, jobs ×3, Wayback, providers, seeds; extraction → passages; Coverage Log; first `live_rules` run. **You:** manual captures from my checklist (fiserv.com sampling ~15–20 pages; optional exec posts) | 0 robots/ToS violations; every mandatory family has a status; ≥90% of scouting gold URLs captured or explained |
| **P3 Classification + AI** | Thu 8 Oct | lexicon + traps, entity guard, rules tagger, Gemini expand/triage/extract with payload guard, V1–V9, clusters, verdict rules, review store; cached `live_ai` run; Gemini-vs-rules comparison | traps 100% rejected; 0 unverified LLM quotes/URLs; payload audit clean; verdict diffs vs expected table reviewed with you |
| **P4 Risk, cells, app** | Fri 9 Oct | risk engine (ordered), playbook, O–V templates, all sheets, screenshots, V-000 end-to-end test, Streamlit pages | V-000 → Yes/Critical; all L–V valid; every P item → Evidence Log row with URL/UTC/SHA; length budgets met; opens clean |
| **P5 Review + notebook + deck** | Sat 10–Mon 12 Oct | **You/team:** HC2 review of cited evidence + overrides. Me: notebook + Colab bundle, replay hardening, `doctor`, deck build (slides 3–10 first) | all cited items reviewed; offline replay = identical cells; notebook runs local + Colab |
| **P6 Freeze & release** | Tue 13 Oct | freeze evidence-v1, final export, `verify`, deck numbers injected from frozen run, speaker notes, 2 timed rehearsals, recording, `v1.0` | verify REPRODUCIBLE; deck ≤15 min; PPTX/XLSX open clean; repo PRIVATE |
| Buffer | Wed 14 Oct | fixes / submission | – |

**Execution method (ultracode):** per phase one Workflow — parallel implementer agents on independent modules
(TDD; worktree isolation when edits overlap) → adversarial reviewers (correctness, brief-compliance,
security/confidentiality) → fix pass → I run the gate, commit, push. You review at P1, P3, P5, P6.
**Cut order if late:** local embedding ranker (never built unless spare time) → provider-sitemap collector
(seeds only) → Wayback CDX gap-fill → screenshots for non-Primary items → embedded images → notebook polish.
**Never cut:** L–V + Evidence Log, review, deck, offline replay demo, confidentiality controls, Gemini path
(cached) + rules fallback.
**Trimmed from design-panel spec (YAGNI):** four-eyes approval bound to assessment hashes, socket-level offline
guard (Replay fetcher suffices), XML part-inventory diff gate, Py3.12 determinism matrix, seedless ablation /
inter-rater κ, `screen` mode as separate mode (live mode with small budget instead).

## Inputs needed from you
1. Team name (col V + deck) and exact deadline / submission channel / presentation date.
2. `GEMINI_API_KEY` placed in `D:\Optiv\vendor-ai-footprint\.env` yourself (never paste in chat); confirm free tier.
3. A contact email for the SEC-required User-Agent (I won't use your account email unless you say so).
4. ~45 min in P2 for manual captures (checklist provided) and ~2 h in P5 for evidence review.

## Verification (end-to-end)
- `uv run pytest` (rubric pins, sensitivity, traps, verdict/risk truth tables, payload guard, workbook fidelity,
  V-000 end-to-end, secret scan, replay determinism).
- `uv run footprint assess data/input/Meridian_Vendor_Input.xlsx --mode live_ai` → review →
  `footprint export` → `footprint verify --scope full` prints `REPRODUCIBLE: yes`.
- Fresh clone, Wi-Fi off: `footprint assess … --mode replay` → byte-identical L–V cells to submitted workbook.
- `uv run streamlit run app/streamlit_app.py` → upload → export in <60 s; notebook top-to-bottom locally + Colab.
- Open xlsx in Excel (no repair prompt; V-000 + provided cells untouched); open pptx in PowerPoint; timed rehearsal.
- `gh repo view rakshit-737/vendor-ai-footprint --json visibility` → PRIVATE.

## Top risks
| Risk | Mitigation |
|---|---|
| Gemini free-tier quota cuts / 403 / 429 | P0 quota check; batch 12–25 passages/call; flash-lite fallback; cache; NullLLM rules path; replay |
| Bot walls / ToS-barred sites (Fiserv, TCH PDFs) | manual capture lane; SEC + partner mirrors + Wayback; gaps logged → questionnaire items |
| Marketing over-claimed or relevance swings | 4 tests, G6/G7-alone = S1, R3 same-sentence rule, caps, Provisional ceilings, flip conditions in T |
| Graders expect BNY=High / FSSI=Critical | explicit floors + sensitivity analysis on slide 9 |
| openpyxl breaks template | cream-only writer, column-span rule, semantic fidelity test, Excel open check at P1/P4 |
| Findings leak | private repo checks, pre-push hook, no publishing, bundle outside git |

---

# Appendix A — Detailed rule reference (design panel)

This appendix is the design panel's detailed reference for lexicons, tag definitions, verdict and risk rules,
cell templates, the deck script and per-vendor calibration notes. **The plan above wins wherever they differ.**
Descoped from this reference (YAGNI, see plan): four-eyes approval / HC3 approvals bound to assessment hashes
(column V is simply `Team {name} / {DD-MM-YYYY}`), stale-adjudication hash machinery (overrides live in
`review/overrides.jsonl` with analyst + reason + date), the socket-level offline guard (replay uses the
`ReplayFetcher`), the XML part-inventory fidelity gate (semantic diff + manual Excel open instead), the local
embedding ranker (stretch only), a separate `screen` mode (live mode with a small budget), the Python 3.12
determinism matrix, seedless ablation and inter-rater kappa. Section numbers below are the reference's own.

Workbook facts verified on the real file (2 Oct): row 5 (V-000) is also cream (FFFFF6E0) but must never be
written; rows 6-11 have custom height 55; `<col>` spans are L-O (12-15) and R-T (18-20); an openpyxl
round-trip with L-V written and a sheet appended left every provided cell, style, merge, hyperlink and column
width unchanged (only the SharePoint `customXml` parts are dropped).

#### 2.3 Source strategy and evidentiary weight (Outcome 03)

**Source families.** In the "Depth by tier" column, M = mandatory, C/H/M/L = Critical/High/Medium/Low, and "–" = not used at that tier.

| Code | Family | How it is collected (GET only; every request passes the terms, robots and rate-limit checks) | Depth by tier (C/H/M/L) | What it can prove |
|---|---|---|---|---|
| LEG | Privacy policy, terms, data processing agreement (DPA), sub-processor list, AI policy, trust/security pages | Footer, sitemap and URL-slug discovery; DNS CNAME check for trust platforms | M/M/M/M | Named sub-processors, automated decision-making, AI terms |
| REG | SEC filings with all exhibits, 8-K EX-99, DEFA14A | company_tickers → submissions JSON → EFTS full-text search over 24 months (AI terms plus product terms found along the way) | M\*/M\*/one query/– | Legally accountable use |
| PRD | Product pages, docs, release notes, whitepapers, blogs, investor relations (IR) | Homepage links to depth 2; sitemaps; WordPress REST sweep when a `wp-json` Link header is present; PDFs read page by page | M/M/keyword scan/homepage | Features, data flows |
| JOB | The vendor's own applicant tracking system (ATS) | <br>• Workday siteMap + per-job JSON, Oracle ORC REST, Workable widget API. <br>• Native careers pages are read through PRD. <br>• Fiserv's Phenom site is manual only (sampling protocol, §2.5). | Listing pages + detail pages for matched titles, at most 120 / at most 60 / keyword search / – | AI duties, named tools |
| DNS | TXT records with AI-vendor verification tokens, SPF/MX, CNAME | DNS-over-HTTPS (DoH) from dns.google and Cloudflare, which must agree | M/M/M/M | A relationship only |
| EXEC | Named executives' posts, talks, podcasts, earnings remarks | Manual capture by a team member only, under the executive-channel rules in §2.9 | M / on a lead / – / – | First-party statements of deployed use |
| IND | Provider customer stories, partner releases, trade press | Provider sitemaps filtered by vendor alias (an early cut); seeds | M/M/–/– | Corroboration (K) |
| NEWS | Newsroom and IR releases | Found via PRD; a blocked IR site falls back to the SEC copy or a partner mirror | M/M/scan/– | Partnerships, metrics |
| HIST | Wayback availability and CDX first-seen dates | Read-only; never Save Page Now; never for paths barred by robots or terms | M / on a lead / – / – | Dating, withdrawn claims |
| DEV | Developer portal; verified GitHub org | DoH lookups of `developer.` and `api.`; GitHub API (anything beyond a minimal check is an early cut) | M / if the service has APIs / – / – | AI endpoints |
| INC | AI incidents and enforcement | 8-K Item 1.05; scan of captured news (anything beyond a minimal check is an early cut) | M/–/–/– | Escalator X5 |

\*Only if the vendor files with the SEC.

**Why each family thins out below Critical.** These reasons come from the one-line `why_not` field in `sources.toml` and are shown on slides 6 and 10.

| Family | Reason |
|---|---|
| LEG | Never dropped. It is cheap, legally accountable (SR A), and the first place AI sub-processors appear. |
| REG | One query at Medium, none at Low. Filings rarely describe a non-critical service, and most smaller vendors do not file. |
| PRD | Keyword scan at Medium, homepage at Low. A full crawl mostly adds marketing (U7) for a vendor whose failure would not seriously affect Meridian. |
| JOB | Keyword search at Medium, none at Low. Postings show intent and staff skills, and their specificity is capped at S2. The volume is worth reading only where data or dependency is material. |
| DNS | Never dropped. It is one cheap lookup, although it proves only a relationship. |
| EXEC | On a lead at High, none below. Executive channels rarely give SR A/B evidence of use within the service at lower tiers, and capture is manual. |
| IND | None below High. Provider stories and trade press only corroborate (SR C). Below High, the questionnaire is a cheaper way to separate Probable from Confirmed. |
| NEWS | Scan at Medium, none at Low. Releases are promotional (SR C) and rarely specific to the service. |
| HIST | On a lead at High, none below. History dates claims and finds withdrawn ones, but it rarely gives SR A/B evidence of use within the service. |
| DEV | Only where the service has APIs. AI endpoints matter only where Meridian integrates with the vendor in code. |
| INC | A separate search at Critical only. At other tiers, incidents surface through the captured news and SEC filings. |

**Excluded by design, with reasons:**
- **Search-engine result pages, Google News RSS and ddgs:** their terms bar automated querying, and the results cannot be reproduced.
- **Gemini grounding:** it is paid, and its terms forbid harvesting the links.
- **Automated LinkedIn or Glassdoor access:** their terms forbid it. Executive posts are captured by hand under §2.9.
- **Wayback's Save Page Now:** it would create new public archive records of the research.
- **Logins, forms and NDA-gated portals:** they are not part of the public footprint.
- **Patents:** they show capability, not use.
- **GDELT:** a machine-built news index. It adds only aggregator copies (SR D) of releases that NEWS already captures.
- **OpenAlex:** research papers show research capability, not use within the service.
- **Hacker News:** user-generated content (SR E).
- **crt.sh:** certificate subdomains such as `ai.` or `copilot.` show capability or plans, not use, and cannot be tied to the service. A hit seen during manual work may be logged as a lead.

**Seeds.** Third-party pages enter only as seeds. Each seed records the manual query that found it, the date and the analyst. The query templates are published in the Method & Legend sheet so the discovery can be repeated.

**Source reliability (SR).** `sources.toml` is the only thing that determines SR.

| SR | Source types |
|---|---|
| A, legally accountable | SEC filings and exhibits, including DEFA14A transcripts; regulator publications; privacy notices; DPAs; sub-processor lists; contract and product terms (for example, Broadcom's Specific Program Documentation, SPD) |
| B, first-party accountable | Vendor product and solution pages; technical docs; release notes; trust pages; IR decks; the vendor's own ATS postings; statements attributed to a named executive (including manual captures); DNS records; verified repos |
| C, promotional or second-party | Unattributed blogs and whitepapers; press releases and their mirrors; provider customer stories; partner pages; trade press; third-party podcasts |
| D | Aggregators and syndicated copies |
| E–F | User-generated content; content of unknown origin, including AI-generated blogs (excluded) |

**Tags.** Each evidence item carries a tag string such as `U3 · SR:B · SP:S2 · RL:R2 · RC:T3 · IC:2 · locus=delivery_ops`. The tags map to a strength label using the fixed order below.

**U, the signal class:**
- U1: AI in the exact service.
- U2: AI in the vendor's platform or an add-on.
- U3: AI in operations that touch the service (support, network operations centre (NOC), software development lifecycle (SDLC), delivery).
- U4: a named AI provider or relationship, including a DNS token.
- U5: building AI capability.
- U6: AI governance.
- U7: a marketing claim.
- U8: a negative or limiting statement.

A job posting is U3 when it assigns duties that use AI tools in production services, and U5 when it only asks for AI skills. Postings describe intentions, so their specificity is capped at S2.

**SP, specificity,** is built from stored indicator hits.
- Genuine-use indicators:
  - G1: a named model or provider.
  - G2: a named feature with observable behaviour.
  - G3: a statement about data flow, retention, training or human review.
  - G4: a release note for a generally available feature.
  - G5: a developer artifact.
  - G6: a legal artifact.
  - G7: a filing that ties AI to products.
  - G8: a job posting with operational AI duties.
  - G11: a quantified outcome.
- Marketing indicators:
  - M1: buzzword.
  - M2: aspirational or capability wording: "will", "plan", "exploring", "roadmap". In statements about what a product can do, the hedges "may", "might", "could" and "can" also count.
  - M3: commentary about other companies or the industry.
  - M4: automation relabelled as AI.
  - M5: superlative.
  - M6: no product named.
  - M7: a partnership shown only by logo.

The specificity grades:
- **S3:** (G1, G5, G6 or G7) together with (G2 or G3).
- **S2:** any of G2, G4, G5, G8 or G11, tied to a named product or function, with no M4. G6 and G7 raise an item above S1 only together with G2 or G3, which already gives S3.
- **S1:** only M1, M5, M6 or M7; or G6 or G7 without G2 or G3. Generic legal or filing language such as "we use artificial intelligence to improve our services" is S1.
- **S0:** M2 or M3 dominates.

A pilot or beta counts as limited deployment and is flagged; it is not treated as M2.

**RL, relevance,** is computed locally only.
- **R3:** an exact-service term appears in the sentence making the AI claim, its heading or the page title, and the locus is service_feature, vendor_addon or delivery_ops.
- **R2:** a product-family term, a company-wide delivery practice, or a relationship proven by DNS.
- **R1:** another business line, an affiliate that is inferred but not confirmed, or a platform supplier.
- **R0:** commentary.

**RC, recency,** is measured against `as_of`: T3 is 12 months or less, T2 is 24 or less, T1 is 36 or less, and T0 is older or undated.
- Articles, releases, posts and filings are dated by publication. The date source is chosen in this order: EDGAR filing date, JSON-LD `datePublished`, `article:published_time`, the WordPress REST date, the ATS posting date, PDF CreationDate, htmldate, the Last-Modified header, then the Wayback first-seen date.
- Live product, docs, policy and trust pages are dated by retrieval, because they describe what is offered today.

**IC, corroboration:**
- 1: corroborated by an independent K signal.
- 2: consistent but uncorroborated A/B.
- 3: S1 only.
- 5: contradicted by an A/B U8 statement.
- 6: cannot be judged.

**Strength labels.** The first rule that matches wins, in this fixed order:
1. **Negative:** U8.
2. **Strong:** a qualifying signal (Q) at R3.
3. **Moderate:** a Q at R2, or a corroborating signal (K).
4. **Context – not scored.** One of three kinds:
   - *relationship only:* a DNS token or a provider listing;
   - *platform supplier:* a platform supplier's capability;
   - *inferred affiliate:* an affiliate that is inferred but not confirmed.
5. **Marketing only:** U7 or S0.
6. **Weak:** anything else verified (S1 or R1).

So a DNS verification token (U4, S1, R2) is always "Context – relationship only", never "Weak".

When these items are cited in column P for an Inconclusive or No verdict, the label is shown as a prefix:

| Label | Prefix in column P |
|---|---|
| Context – relationship only | "Indicator – relationship only (DNS TXT record)" |
| Context – platform supplier | "Indicator – platform supplier capability" |
| Context – inferred affiliate | "Indicator – inferred affiliate, not confirmed" |
| Marketing only | "Marketing statement – not confirmatory" |
| Weak | "Indicator – weak" |
| Negative | "Limiting statement" |

**Telling genuine use from marketing.** Four tests run in order, and every hit is stored.
1. **Definition test** (EU AI Act Art. 3(1)). Workload automation, batch scheduling, RPA, rules engines, templated composition, Intelligent Mail barcodes and BI dashboards are not AI unless an ML or LLM component is evidenced.
2. **Subject test.** The sentence must name the vendor, an alias or a product, or use the first person. On first-party product pages the product is the implied subject. A third-party article must name the vendor in the sentence or the title.
3. **Use-state test.** Look for deployed cues ("live", "launched", "generally available", "uses", "reviewed by AI", "approved AI tools", a percentage) rather than M2 wording.
4. **Specificity test,** which produces SP.

**Entity guard.** A source must be a first-party or confirmed-affiliate domain, or carry the vendor's legal name or alias plus a link or SEC CIK that corroborates it. Seeded collision lists exclude Telik's "Terrapin Technologies", Terrapinn, Autoworx, Packet Clearing House and the GSA programme called "FSSI".

#### 2.4 Criticality rubric (Outcome 04)

**Basis.** Rubric v1.0 uses profile fields B–K only and runs before any OSINT. It is anchored on OCC Bulletin 2023-17. That bulletin treats an activity as critical when the third party's failure would create significant risk, significant customer impact, or a significant impact on operations.

| Factor (weight) | 4 | 3 | 2 | 1 | 0 |
|---|---|---|---|---|---|
| O, operational dependency (×7), col I | Total or Critical | High | Moderate or Medium | Low | none |
| D, data sensitivity (×6), col J | Tax IDs or SSNs; health data; card numbers (PAN); customer authentication secrets; the full customer master | **D3:** non-public personal information (NPI): account identifiers, balances, holdings, payment instructions or messages. **D3-P:** privileged production access or service credentials. | Confidential data that is not customer data | Internal or aggregated | none |
| P, payment-flow involvement (×5), cols E and H | Clearing or settlement; core account or transaction processing; system of record | Transmitting payment messages or files | Scheduling jobs on posting systems; commission or payout calculation; payment arrangements | Statements or reporting after the fact | none |
| R, regulatory exposure (×4) | The service *is* the regulated mechanism: system of record, clearing or transmission, mandated statements, tax forms, notices, servicing | NPI for 10,000 or more customers, or financial-reporting records | Privileged access to regulated systems without NPI | Confidential data that is not customer data | none |
| V, volume (×3), cols J and K | 1M or more customers, the entire customer base, or 10M or more records a year | 100k–999k customers, or 1M–9.99M records | 10k–99k, or 100k–999k | Below 10k or below 100k | not applicable; frequencies are ignored |

**Score and tiers.** Score = 7O + 6D + 5P + 4R + 3V, out of 100. Critical is 80 or more, High 50–79, Medium 25–49 and Low 24 or less.

**Floors.** The final tier is the higher of the score tier and any floor that applies.
- F1: O = 4 gives Critical.
- F2: P ≥ 3 and O ≥ 3 gives Critical.
- F3: D = 4 and V ≥ 3 gives at least High.
- F4: D3-P gives at least High.
- F5: D ≥ 3 gives at least Medium.
- F6: D = 4, V ≥ 3 and O ≥ 3 gives Critical. This encodes V-000's two drivers: sensitive data at population scale, and an outage that takes away a primary channel.

**Scoring rules:**
- Use profile fields only.
- When two adjacent levels fit, take the higher one and log the choice.
- Record each factor's level, anchor and verbatim trigger phrase.
- Sensitivity is the tier after moving each factor one level up and one level down, with floors applied. It is rendered in plain words and checked against a brute-force recomputation.
- Also report a ±1 weight perturbation.
- Any override needs a stated reason, and any rule change bumps the rubric version.

| Vendor | O | D | P | R | V | Score | Floors | **Tier** | Sensitivity |
|---|---|---|---|---|---|---|---|---|---|
| V-000 (calibration only) | 3 | 4 | 2 | 4 | 3 | 80 | F3, F5, F6 | **Critical** | Drops to High if D, O or V were one level lower |
| V-001 AutomWorx | 3 | 3-P | 2 | 2 | 0 | 57 | F4, F5 | **High** | Becomes Critical if dependency were Critical/Total (F1) or the service transmitted payment files (F2); High for every one-step decrease (scores 50–53) |
| V-002 Fiserv | 4 | 4 | 4 | 4 | 4 | 100 | F1, F2, F3, F5, F6 | **Critical** | Never below 93 |
| V-003 FSSI | 2 | 4 | 1 | 4 | 4 | 71 | F3, F5 | **High** | Becomes Critical if dependency were High (F6); High for every one-step decrease (scores 64–68) |
| V-004 Terrapin | 2 | 3 | 2 | 3 | 2 | 60 | F5 | **High** | Score range 53–67; tier stable |
| V-005 BNY | 3 | 3 | 3 | 4 | 3 | 79 | **F2**, F5 | **Critical** | Drops to High if O or P were one level lower |
| V-006 TCH | 4 | 3 | 4 | 4 | 4 | 94 | F1, F2, F5 | **Critical** | Tier stable |

All seven tiers stay the same when any single weight changes by ±1 (70 perturbations tested).

**Column M template.** Maximum 1,050 characters, in plain language in V-000's style. Factor codes (O, D, P, R, V, F1–F6) appear only in the Criticality Workings sheet.

`{Tier}. {Opening sentence of about 150–300 characters naming the two strongest drivers in the profile's own words}. How the tier was reached (rubric v1.0, profile only): operational dependency {High} ({3} of 4, "{trigger}"); data sensitivity {…} ({n} of 4); payment-flow involvement {…} ({n} of 4); regulatory exposure {…} ({n} of 4); volume {…} ({n} of 4). Score {s} of 100{; raised to Critical because {floor in plain words}}. Sensitivity: {plain sentence}.`

Example opening for V-005: "Critical. BNY transmits real-time payment files from other banks to Meridian, so an outage stops inbound instant payments, and it handles payment instructions, counterparty names and account identifiers."

#### 2.5 Depth policy and stopping rules

| Tier → label in column N | Mandatory families (run until complete or until their caps; status logged) | Discretionary | Budget: discretionary fetches / Gemini calls / analyst time |
|---|---|---|---|
| Critical → "Full review – tier-driven" | LEG, REG\*, PRD, JOB, DNS, EXEC, IND, NEWS, HIST, DEV, INC | Seeds; 2 snowball rounds, in which newly verified product or provider names are queried again | 100 / 40 / 3 h |
| High → "Standard review – tier-driven" | LEG, PRD, JOB, DNS, IND, NEWS, REG\* | 1 snowball round; HIST and EXEC on a lead; DEV if the service has APIs | 50 / 20 / 1.5 h |
| Medium → "Focused review" | LEG (privacy and sub-processors), PRD keyword scan, JOB keyword search, NEWS scan, DNS, REG\* (one query) | none | 0 / 6 / 30 min |
| Low → "Screen" | Homepage, privacy notice, sub-processor or trust page, DNS | none | 0 / 2 / 15 min |

\*Only if the vendor files with the SEC.

How the budgets apply:
- The fetch budget limits discretionary work only. It never cuts a mandatory family short.
- Gemini calls and analyst time are per-vendor ceilings across all work. When a vendor's Gemini calls run out, its remaining passages go to the rules path (logged), and collection continues.

**Mandatory family caps.** Caps count automated requests per vendor and are set in `depth.toml`. Robots.txt and terms checks are not counted. Actual use is logged against each cap in the Coverage Log.

| Family | Critical | High | Medium | Low |
|---|---|---|---|---|
| LEG | 40 | 40 | 15 | 3 |
| REG\* | 80 | 40 | 5 (one full-text query) | – |
| PRD | 150 (depth 2) | 100 (depth 2) | 20 (keyword scan) | 1 (homepage) |
| JOB | Listing pages (counted separately) + at most 120 detail fetches | Listing pages + at most 60 | Listing pages + at most 15 | – |
| DNS (DoH queries) | 40 | 40 | 20 | 10 |
| IND | 30 | 20 | – | – |
| NEWS | 40 | 25 | 10 (scan) | – |
| HIST (CDX) | 30 | On a lead (discretionary) | – | – |
| DEV | 10 | If the service has APIs (discretionary) | – | – |
| INC | 5 | – | – | – |
| EXEC | Manual only (analyst time) | On a lead (discretionary, manual) | – | – |

JOB detail fetches are made only for titles that match the role keyword list, or that the ATS's own keyword search returns for AI terms. The role keyword list holds:
- AI terms;
- service terms from columns E and H;
- delivery roles such as NOC, SRE, operations, engineering, data, fraud and support.

When a family reaches its cap, it ends as `stopped(rule: cap)` and the unread remainder is logged. Modifiers raise a family to its Critical cap. Per-host ceilings from the terms register apply on top; for example, FSSI's host gets about 25 automated requests per run in total.

**Modifiers:**
- **M-D4, for FSSI** (top data sensitivity below Critical tier): run LEG and HIST at Critical depth, and check the AI features of platforms the vendor names.
- **M-D3P, for AutomWorx** (privileged access): run a delivery-AI module that checks three things: embedded third-party tenants and affiliates, the platform maker's AI terms, and signals that staff use AI tools. Escalate early.
- **M-Private, for AutomWorx, FSSI, Terrapin and TCH** (no SEC filings): log REG as not applicable with evidence of the EDGAR check, and go deeper on LEG and HIST.

**Stopping rules.** Discretionary work stops at whichever of these comes first:
- **All three questions are answered or logged as gaps:**
  - **U (usage):** usage is Confirmed for the exact service, or the search is saturated. Saturation means no new evidence cluster at SP ≥ S2 and RL ≥ R2 in the last 10 actions for a Critical vendor, or the last 6 for a High vendor.
  - **S (sub-processors):** the vendor names its sub-processors, or it is confirmed that no register exists.
  - **D (data use):** data-use terms are found, or the gap is logged.
- **The discretionary fetch budget runs out.**

**Where effort stops paying:**
- Once usage is Confirmed, the remaining budget goes to the exposure (E), decision-impact (K) and transparency questions.
- Syndicated copies count once.
- Stop a family after 3 consecutive R1 items.
- Skip job postings older than 24 months.
- Patents and forums are leads only.
- For consultancies and network operators, escalate to a questionnaire rather than over-search.

**Enforcement:**
- A `DepthPlan` cannot exist without a `CriticalityResult`.
- Collectors that are not in the plan are refused.
- Every mandatory family ends with one of these statuses: done, done_manual, not_applicable(reason), stopped(rule) (which covers caps and the 3-consecutive-R1 rule), blocked_robots, blocked_tou, blocked_bot, error or descoped.
- **Complete coverage** = {done, done_manual, not_applicable, stopped(rule)}.
- **Incomplete coverage** = {blocked_robots, blocked_tou, blocked_bot, error, descoped}.
- Verdict rule e) and the confidence rule (§2.7) use these two sets.
- Column N is rendered from the plan plus the Coverage Log. It lists "Reserved for Meridian (non-OSINT): AI due-diligence questionnaire, contract and AI-clause review, SOC 2 scope and ISO/IEC 42001 validation" and closes with: "Depth followed from the assigned tier rather than from the volume of material the vendor had published."
- Synthetic Medium and Low vendors, used in tests only, exercise the unused tiers.

**Manual sampling protocol** for domains whose terms entry is `automation=none`, such as fiserv.com and careers.fiserv.com. Claude Code prepares a checklist for each family, and a team member captures each item with `footprint capture import`.
- **LEG:** the privacy notice, the terms, and every sub-processor, AI, trust or security page linked from the footer.
- **PRD and NEWS:** the seeded pages, plus the first results page of the site's own search for each core term group (AI, machine learning, generative AI, agentic).
- **JOB:** the careers site's keyword search for the same terms. Capture the result counts and the 10 most recent matching postings, preferring the service's business area.

Completing the checklist counts as done_manual. The checklist, the counts and the capturing analyst are recorded in the Coverage Log.

#### 2.6 Signal classification, AI layer and anti-hallucination controls

**Passages**
- Boilerplate is removed: any line that appears on at least 40% of a host's pages (minimum 5 pages).
- A deterministic sentence splitter guards abbreviations (Inc., Corp., L.L.C., U.S., e.g.).
- A passage is the sentence with the lexicon hit plus one sentence either side, up to 700 characters, with no ellipses.
- Pages that triage marks high-priority are also split into whole-page chunks of up to 30k characters for Gemini extraction.

**Lexicon** (`lexicon.toml`, word-boundary matching)

*Core terms:*
- artificial intelligence, machine learning, deep learning, neural, LLM, generative AI, agentic, AI agent, digital employee, NLP, RAG, model context protocol, foundation model, fine-tuning, chatbot, virtual assistant, predictive model, intelligent document processing;
- case-sensitive whole words: `AI|ML|LLMs?|GenAI|MCP`. `MCP` counts only when "Model Context Protocol" appears in the document or another AI term appears in the same passage. This blocks "Microsoft Certified Professional".

*Provider guards:*
- "Claude" counts only next to Anthropic or model names. This blocks "Claude Reumert".
- "Gemini" needs Google or model context.
- "Copilot" needs Microsoft, GitHub or M365.
- "Devin" needs Cognition.

*Suppressors (never counted as AI):*
- Intelligent Mail barcode / IMb, intelligent inserting, "intelligent tools" with no AI term;
- Automic agents, Automation Analytics & Intelligence, Oracle 23ai, business intelligence;
- "in our DNA", operating model, user agent;
- Yoast-generated llms.txt, navigation topic links, "recognition";
- Microsoft Certified Professional (MCP).

*Separate lexicons:* governance (U6), limiting statements (U8), data flow (G3), aspirational and capability wording including the modal hedges (M2), and autonomy and human review (used to set K).

**AI roles**

| Role | Engine | Input (public only) | Output | Calls per run |
|---|---|---|---|---|
| expand | gemini-3.5-flash-lite, falling back to 3.1-flash-lite | Public vendor name, homepage title and meta, navigation link text | Names only (aliases, products, AI programmes, providers, affiliates), used as search keys and never as evidence | 6 |
| triage | same | Rows of `id \| path \| title \| date \| family`, plus job titles | A sparse list of `{id, priority: high\|medium, reason enum}`; IDs only | ~20 |
| extract | same | `[P#] (kind, date) text`, 12 passages per call (25 if the P0 quota gate requires it) | ClaimBatch (schema below) | ~70–100 (about 35–50 at 25 per call) |
| rank (keyless) | local all-MiniLM-L6-v2, pinned revision, CPU, cached | The same rows and passages, processed on the machine | Cosine similarity to 12 prototype statements, used to order triage | 0 network |

**Gemini call settings**
- Stateless `generateContent` only: no Files API, no context caching, no Interactions API.
- JSON-schema output, `seed=1234`, and no temperature (deprecated for Gemini 3.x). Thinking is set to minimal.
- Retries: 5 attempts, 2–60 s backoff, on HTTP 408, 429 and 5xx. Calls are paced at least 6 s apart.
- A daily-quota error or 403 switches to the next model, then to `NullLLM`; the switch is recorded in the manifest.
- Cache key: `sha256(model|prompt_sha|schema_sha|payload_sha|seed)`.
- Planned and actual calls per day and per vendor are recorded in the manifest.

**Claim extraction schema**

```python
class Claim(BaseModel):
    passage_id: str; quote: str                  # verbatim, 1–3 sentences
    claim_kind: Literal["uses_ai","offers_ai_feature","ai_partnership","names_ai_provider","ai_hiring",
                        "ai_governance","generic_ai_marketing","negative_or_limiting","industry_commentary","not_ai"]
    subject: Literal["vendor_product","vendor_operations","vendor_staff_tools","third_party_product","other_or_industry"]
    ai_type: Literal["predictive_ml","genai_llm","agentic","document_ai","conversational","aiops","unspecified"]
    temporal: Literal["in_production","pilot_or_beta","planned","unclear"]
    action_level: Literal["none","advisory","human_reviewed_decision","automated_action","unknown"]
    named_providers: list[str]; data_mentioned: list[str]
    indicators: list[Indicator]                  # {code: G1..G11|M1..M7, span: str}
```

**Prompt outline for `extract_v1`** (frozen and hashed). The system message frames the task as labelling public passages for an evidence log. Rules:
1. Use the passage text only.
2. The quote must be one continuous span, copied character for character, with no ellipses.
3. Return nothing for passages that are not relevant.
4. Advice to readers and industry trends are `industry_commentary`. Other companies' products are `third_party_product`.
5. Automation, rules engines, scheduling, templated composition, barcodes and BI are not AI unless ML or an LLM is named.
6. Every indicator span must be copied from the quote.
7. Never output URLs.

The prompt includes two fictional few-shot examples: one marketing claim and one genuine use.

**Other prompts:**
- `triage_v1`: "Return IDs of items likely to hold the company's own statements on AI in products or operations, AI policy, sub-processors or data processing, AI-related filings, or job duties using AI. IDs only."
- `expand_v1`: "Names only; used solely as search keys."

**Payload guard.** `PublicPayload` is a closed type that holds only `company` and `items[{id, kind, text}]`. The code that builds it cannot see the profile, tier, verdict, review or service-term objects. Before each call the guard checks every passage:
- **Hard blocks.** The word "Meridian" or the team name, matched as whole words, withholds the passage.
- **Profile blocklist.** A hit withholds the passage. The list is built only from the free-text profile columns: D (description), E (service), H (business process), J (data accessed) and K (volume).
  - Values of 4 or more words are matched as normalised 6-word shingles, or as the whole value when it has 4–5 words.
  - Distinctive volume phrases are also blocked, with numbers normalised. Examples: "1.4 million customers", "38,000 wealth clients", "2.1 million messages".
- **Not blocked.** The enumerated columns F (category) and I (dependency), the public website in G and the vendor name in C. Values such as "Payments", "Core Platform", "Professional Services", "High", "Critical", "Moderate", "Not applicable" and "www.bny.com" occur naturally in public text.
- **Redacted, not blocked.**
  - Email addresses become `[EMAIL_n]` and phone numbers become `[PHONE_n]`.
  - Names on the seeded scrub list become `[PERSON_n]`.
  - Each passage keeps a map of its placeholders, and returned quotes are restored from that map before check V2.
- **What happens to a withheld passage.**
  - It goes to the rules and the local ranker only. The rest of the batch is still sent.
  - Each withholding is logged in `llm_calls.jsonl` with its reason code and no text, and the withholding rate is reported per vendor.
  - A shingle that also appears on the vendor's own public pages may be removed from the blocklist with a written reason.
- **Skipped hosts.** Passages are also skipped when:
  - the host's terms entry sets `ai_processing_allowed=false` (fiserv.com and similar);
  - its robots.txt disallows `Google-Extended`;
  - its Content-Signal sets `ai-train=no` or `ai-input=no`.

**Verification gates V1–V9.** Every rejection is logged with a reason code.
- **V1:** the passage ID exists.
- **V2:** after placeholders are restored from the passage's map, the quote must be an exact substring of the source, or match after NFKC, whitespace, quote and dash normalisation. The stored excerpt is then the exact source slice with its offsets.
- **V3:** no inserted ellipsis; length 25–600 characters.
- **V4:** each indicator span lies inside the quote and passes its lexical test. G6 and G7 come from source type only, and on their own they grade S1.
- **V5:** named providers and products appear verbatim in the quote or the page title.
- **V6:** the entity guard passes.
- **V7:** relevance (RL) and locus are computed locally.
- **V8:** final temporal, action and SP labels come from the rules, and Gemini's labels are shown beside them. A disagreement goes to the review queue, and the rule value stands until an analyst adjudicates.
- **V9:** duplicates are removed.

A claim found by the LLM with no lexicon hit is marked "proposed". It can only be cited after an analyst accepts it.

**Keyless fallback (`NullLLM`)**
- Expansion uses seeds plus capitalised n-grams near AI terms on the homepage.
- Triage uses the local ranker, or a score from URL slug and title rules if the ranker is absent.
- Extraction is rules only.
- Verification, tagging and verdicts use the same code as the AI path.
- The UI shows a banner: "Gemini unavailable – rules + local model".
- Replay serves cached AI outputs, so the AI layer can still be demonstrated offline.

#### 2.7 AI-usage aggregation (column O)

**Definitions**
- **Q (qualifying signal):** verified, SR A or B, SP S2 or higher, RL R2 or higher, RC T1 or newer, class U1–U4, and locus service_feature, vendor_addon, delivery_ops or sdlc.
- **K (corroborating signal):** an item from an independent cluster, SR A–C, SP S2 or higher, RL R2 or higher, RC T1 or newer. Items older than 36 months (T0) appear only as historical context.
- **Independent:** a different origin cluster AND a different publisher or source family. Two items share an origin cluster if their rapidfuzz `token_set_ratio` is 90 or more, or their titles match. Partner mirrors of the same release form one cluster.

**Verdict rules,** applied in order:
- **a) Conflict.** A Q is explicitly contradicted, for the same service, by an A/B U8 dated the same or later. The verdict is Inconclusive, and the conflict becomes a questionnaire item. An older statement that only limits scope is a "scope question", not a conflict.
- **b) Confirmed:** at least one Q at R3 plus at least one independent K. Column O = Yes.
- **c) Probable:** at least one Q at R2 or higher, or at least two independent K. Column O = Yes.
- **d) Affirmed negative:** an A/B U8 covers the service and there is no Q or K. Column O = No.
- **e) Not detected:** every mandatory family has complete coverage, and no item has all of SP S1 or higher, RL R2 or higher, RC T1 or newer and class U1–U4 or U7. Complete coverage means done, done_manual, not_applicable or stopped(rule). Column O = No.
- **f) Otherwise Inconclusive.** This covers marketing only, capability only, relationship only, an inferred affiliate, or incomplete coverage.

**Wording** follows ICD 203. Likelihood and confidence go in separate sentences, written into T's strength clause.

| Verdict | Likelihood wording |
|---|---|
| Confirmed | "very likely"; "almost certain" with two or more independent A sources |
| Probable | "likely" |
| Inconclusive | "roughly even chance" if an S1+ item exists at R2+, otherwise "unlikely" |
| Not detected | "unlikely" |
| Affirmed negative | "very unlikely" |

| Confidence | When it applies |
|---|---|
| High | Two or more consistent A/B sources and complete coverage |
| Moderate | One A/B source, two or more C sources, or complete coverage with nothing found |
| Low | A conflict; incomplete coverage of a mandatory family (blocked_*, error or descoped); or D-grade sources only |

#### 2.8 AI risk matrix, rationale and actions (columns Q–V)

**Inputs.** Each input cites evidence IDs (E-IDs) or is marked "assumed".

**E, exposure.** Take the maximum over the Q and K pathways. E3 always needs an R3 item. An R2 (product-family) item is capped at E2; the E3 it would otherwise give is carried only as an assumed "if confirmed" input for the ceiling and the flip condition.

| Where the AI sits | E3 (needs an R3 item) | E2 | E1 | E0 |
|---|---|---|---|---|
| service_feature or vendor_addon | Data sensitivity D = 4 or D3-P, or a named external model processes the data | D3; or an R2 item where the R3 condition would give E3 | otherwise | – |
| delivery_ops | The excerpt states customer data or credentials are processed by AI | AI works on artefacts that carry customer data (tickets, cases, incidents, exceptions, transactions, production logs, communications) | otherwise | – |
| sdlc | – | Production data is named | otherwise | – |
| corporate_internal | – | – | – | always |

**K, decision impact:**
- K3: action without per-case human review on customers, funds, regulatory outputs or production.
- K2: action with human review or approval gates, or autonomous action on operational items that do not affect customers.
- K1: advisory output only.
- K0: none.

**TP, tier points:** Critical 3, High 2, Medium 1, Low 0.

**TG, transparency gap.** Only verified items close a gap. The six checks:
- t1: AI use disclosed for the service (an R3 A/B source).
- t2: AI providers named by the vendor.
- t3: data-use terms.
- t4: human oversight.
- t5: AI governance attestation (NIST AI RMF, ISO/IEC 42001 or an AI policy).
- t6: AI incident or change notification.

Missing 0–1 gives TG 0; missing 2–3 gives 1; missing 4–5 gives 2; missing all 6 gives 3. Each gap is worded "not publicly disclosed; contractual disclosure unknown".

**Unknowns rule.** This applies to Inconclusive verdicts, and to any input that a Confirmed or Probable verdict leaves unknown. Assumed values are marked "a" and never satisfy the materiality gate.
- E from data sensitivity: D4 or D3-P gives 3; D3 gives 2; D ≤ 2 gives 1.
- K from the vendor's role: P ≥ 3 or D3-P gives 3; pay or customer-facing outputs give 2; any other role gives 1.
- If the action level is unknown under a Confirmed or Probable verdict, it is assumed from the vendor's role using the same K mapping and marked as assumed.

**Score.** ARP = 2E + 2K + TP + TG, range 0–18. Critical is 14–18, High 10–13, Medium 6–9 and Low 0–5.

**Order of adjustments.** The order is fixed, and every step is logged.
1. **Risk score:** ARP = 2E + 2K + TP + TG.
2. **Base class** from the score.
3. **Materiality gate:** High or above needs evidenced (not assumed) E ≥ 2 or K ≥ 2. If the gate is not met, the class is lowered to Medium.
4. **Escalator floors:** these apply to Confirmed and Probable only, and only when the gate is met. Otherwise, any escalator that would have fired is logged and not applied. Each escalator sets a High floor:
   - X1: a provider named by a third party but not by the vendor, with E ≥ 2.
   - X2: training or retention without an opt-out, with E ≥ 2.
   - X3: agentic or privileged AI acting on Meridian production without approval.
   - X4: K3 on credit, account access or payment holds.
   - X5: an AI incident in the last 24 months.
   - X6: a single foundation-model provider behind a Critical vendor.
5. **Verdict cap,** always last:
   - Probable is capped at High.
   - Inconclusive is capped at Medium, and T states "Provisional".
   - A "No" verdict gives "None identified".

**Ceiling.** The class after steps 1–4 but before the cap, computed with assumed inputs treated as confirmed. An escalator counts toward the ceiling only if its own condition is evidenced. T labels the result "if confirmed", for example "Provisional; ceiling Critical if confirmed".

**Calibration test:** V-000 scores E3 + K3 + TP3 + TG2 = 17. Its evidenced E3 meets the gate, and Confirmed is not capped, so the result is Critical.

**Risk themes,** mapped to NIST AI 600-1 and the OWASP LLM Top 10:
- RT1 data exposure;
- RT2 undisclosed sub-processors and concentration;
- RT3 decision transparency;
- RT4 excessive agency;
- RT5 injection and leakage;
- RT6 output integrity;
- RT7 predictive-model risk.

**Expected outcomes.** These are hypotheses from the scouting work, recalculated under the rules above. The pipeline and the review decide the final values. "a" marks a value assumed under the unknowns rule.

| Vendor | Tier | O (verdict) | E·K·TP·TG = ARP | S | Flip condition (written in T) |
|---|---|---|---|---|---|
| V-001 AutomWorx | High | Inconclusive | 3a·3a·2·3 = 17 | Medium (Provisional; ceiling Critical if confirmed) | Confirmation that Labarum AI's NEO platform delivers Meridian work → Yes, High–Critical |
| V-002 Fiserv | Critical | Yes (Probable) | 2·2·3·1 = 12 | High | Confirmation that AI runs on DNA (Confirmed; E3, K3 → 16) → Critical |
| V-003 FSSI | High | No (Not detected) | – | None identified | Generative AI in the composition tools, or Copilot, touching Meridian data → reassess |
| V-004 Terrapin | High | Inconclusive | 2a·2a·2·3 = 13 | Medium (Provisional; ceiling High if confirmed) | Staff generative-AI use on Meridian data confirmed → High |
| V-005 BNY | Critical | Yes (Confirmed) | 2·2·3·1 = 12 | High | External models processing Meridian RTP data (E3) or holds without per-case review (K3) → Critical |
| V-006 TCH | Critical | Yes (Probable) | 2·1·3·2 = 11 | High | Isolated generative-AI tenant with no payment data in prompts (E1) → Medium |

Notes on the recalculation:
- **Fiserv:** the 10-K product-family item (R2) is capped at E2 and may grade only S1. Its E2 and K2 come from the Investor Day ticket-resolution pilot.
- **AutomWorx and Terrapin:** their assumed E and K fail the gate. Their ceilings exist only "if confirmed".
- **FSSI:** if coverage ends incomplete (for example, the site blocks the bot), the verdict becomes Inconclusive. The class is then Medium, Provisional, with a ceiling of Critical if confirmed (3a·2a·2·3 = 15).

**Cell templates**
- **P.** Maximum 1,390 characters; no hashes, no interpretation; every quote verbatim. What P holds depends on the verdict:
  - **Yes:** up to 3 Q and K items, each in the form `"{Primary|Supporting} source – {type}, {publisher}, {14 May 2026}, {URL} (retrieved 02 Oct 2026; Evidence Log {E-ID}): "{verbatim}""`. P contains no marketing-only or context items.
  - **Inconclusive:** up to 2 decisive indicator items, each prefixed with its strength label only. Example: `"Indicator – relationship only (DNS TXT record), {publisher}, {URL} (retrieved …; Evidence Log {E-ID}): "{verbatim}""`, or `"Marketing statement – not confirmatory, {type}, …: "{verbatim}""`. "Decisive" means the items that rule f) relies on, ranked by label order, then SR, RL and RC.
  - **No:** the strongest limiting or counter-evidence excerpt, prefixed "Limiting statement" or "Counter-evidence".
  - Every verdict then lists `"Negative finding – {what} not found on {where} (Coverage Log {C-ID})."` and ends with `"Entries are recorded in the Evidence Log sheet with retrieval dates and screenshots."`
- **Q:** `"{n} usage pathways are indicated. First, … (E-IDs). … The extent of {unknowns} is undetermined from public material and is carried forward to the vendor questionnaire."` DNS tokens and platform options appear here, labelled "relationship indicator, not a confirmed sub-processor".
- **R:** only providers the vendor itself names publicly, with role and source. If there are none: "None named by the vendor – {Inconclusive}. No public sub-processor register found (searched …). Provider identity, region and retention are raised with the vendor."
- **T:** plain sentences with no score codes, in this order:
  1. the class, with "Provisional" where capped;
  2. the data involved, in the profile's words;
  3. the AI evidence and what it touches (E-IDs);
  4. strength: the ICD 203 likelihood sentence ("It is {very likely} that the vendor uses AI in {service}."), then a separate confidence sentence ("Confidence is {level} because {reason}."). Where it applies, add that the evidence is reliable on the fact of use while data flows are inferred;
  5. dependency;
  6. transparency gaps;
  7. the score in words, for example "exposure 2/3, decision impact 2/3, tier 3, transparency gap 1 → 12 of 18 = High (exposure and decision impact count double)";
  8. the cap, ceiling or flip sentence.
- **U:** the playbook for the class, plus gap blocks. The gap blocks are GAP-SUB, TRAIN, LOC, EXPL, AGENT, PI, GOV, INC, CONC and MRM. They map to questionnaire items Q1–Q15 and contract clauses C1–C11 in `actions.toml`.

  | Class | Playbook |
  |---|---|
  | Critical | Follows V-000's U5 in four sentences: <br>1. Issue a targeted questionnaire within 15 business days covering the open gap blocks. <br>2. Register the vendor on Meridian's AI sub-processor inventory, with an unresolved-provider flag when no provider is named. <br>3. Raise AI clauses at the next contract review: training restrictions, sub-processor change notification, human-oversight thresholds, and rights to review attestations. <br>4. Escalate to the Third-Party Risk Committee if the vendor does not confirm. |
  | High | The same four sentences, with the questionnaire due within 15 business days for a Critical-tier vendor and 30 otherwise |
  | Provisional | Q1–Q4 within 15 business days if the ceiling is High or above, otherwise 30; reclassify on response |
  | None identified, Critical or High tier | Written no-AI attestation plus an AI change-notification clause at renewal; annual re-scan |

  Every action is addressed to Meridian. The team never contacts vendors.
- **V:** `"Team {name} / {HC3 approval date}"`, in the format confirmed at Q2. The suffix "UNREVIEWED DRAFT" is added until an HC3 approval is recorded for the current assessment hash.

#### 2.11 Workbook I/O

**Reader**
- Finds the "Vendor Inventory" sheet, or else the first sheet with "Vendor ID" in rows 1–10.
- Matches headers with an alias table plus rapidfuzz ≥ 85. This absorbs "AI Usage Detected (Y/N)", "Assessedd By / Date" and "Service / Product Provided [to Meridian]".
- Reads data rows until the Vendor ID is empty, and skips V-000, "fictional" and "do not edit" rows.
- Error codes:
  - E01 sheet, E02 header row, E03 header (shows the closest match);
  - E04 duplicate ID, E05 no vendors;
  - E06 cells already filled (needs `--overwrite`);
  - E07 no website (criticality only), E08 unknown dependency label;
  - E09 target cell not cream.

**Writer** (openpyxl, working on a copy)
- Writes only L–V of vendor rows, and only where the fill is `FFFFF6E0` (cream).
- Never inserts or deletes rows.
- Allowed values: L ∈ {Critical, High, Medium, Low}; O ∈ {Yes, No, Inconclusive}; S ∈ {Critical, High, Medium, Low, None identified}.
- Every text value is forced to string type, which blocks formula injection. XML-illegal characters are stripped and the change is logged.
- Length budgets come from column width × the 409-pt maximum row height: P about 1,390 characters, Q about 1,670, U about 1,480, others about 1,050.
- Heights of rows 6–11 are raised from line estimates, with user approval.
- `column_dimensions` for M–O and S–T is never read or written. The reason:
  - Columns L–O and R–T are each defined by a single `<col>` span (`min=12 max=15` and `min=18 max=20`). They are not outline-grouped.
  - openpyxl stores each span under its first letter (L and R).
  - Touching M–O or S–T would add a second `<col>` element that overlaps the span, which makes Excel show a repair prompt.
- Six sheets are appended: Evidence Log, Coverage Log, Criticality Workings, Method & Legend (tag legend, service-term dictionaries with reasons, query templates, `why_not` reasons), Evidence Images (JPEG crops) and Run Info.
  - With `export --no-images`, Evidence Images lists each crop's reference and SHA-256 instead of the image.
  - The appended sheets' headers use the cream fill, because blue-grey means "provided by Optiv".

**Fidelity gate.** Two checks, both required.
- **(a) Semantic comparison** of the provided cells and sheets: values, number formats, fonts, fills, borders, alignment, merges, column definitions (widths and spans), hyperlinks (G5, G10), freeze panes and sheet views (including the active tab), the Field Guide and Points to consider sheets, and sheet order. Allowed differences: L–V in rows 6–11, the heights of rows 6–11, and the appended sheets.
- **(b) Part-inventory diff** against an explicit list of the changes openpyxl is expected to make:
  - `workbook.xml` loses `x15ac:absPath` (the SharePoint path), `xr:revisionPtr`, and the calcFeatures and LibreOffice `extLst` entries;
  - `styles.xml` is renumbered;
  - `xr:uid` attributes are dropped;
  - the 9 SharePoint `customXml` parts are dropped;
  - shared strings are rewritten as inline strings;
  - `docProps` are rewritten;
  - the appended sheets add drawing and media parts.

  Any change to a part that is not on the list fails the gate.
- The output is opened manually in Excel at P1 and at P4.

### 3. Deck outline

**Format.** 13 slides, about 13:15 plus a Q&A buffer.
- **Build:** python-pptx with native, editable shapes and tables, and numbers injected from the frozen run.
  - Slides 3–10 do not depend on the final numbers and are complete by P4.
  - Slides 2 and 11 and the per-vendor appendix are generated from the frozen run after P6.
- **Labelling:** each slide carries an "Outcome 0X" corner tag, and the speaker notes cite an E-ID for every claim about a vendor.
- **Styling:** team branding only, no Optiv branding.

| # | Slide | Content | Time |
|---|---|---|---|
| 1 | Title | "Reading the public footprint" | 0:15 |
| 2 | Answer first | Table: tier × usage × risk × flip condition × next action. The headline is generated from the frozen run. On the expected outcomes it would read: "AI runs in service delivery at 3 of 6 vendors (Fiserv, BNY, TCH). None reaches Critical AI risk on public evidence; AutomWorx's ceiling is Critical if confirmed, and Fiserv and BNY would become Critical on a single confirming answer." | 1:00 |
| 3 | Approach | Roles 01–05 mapped to C1–C10; three human checkpoints; "AI proposes, code verifies, analyst approves" | 0:30 |
| 4 | Outcome 01: functional design | The §2.1 flow, including the stop-rule loop and the freeze → render → approve order | 1:00 |
| 5 | Outcome 02: architecture | Layers, modules, stores, external services, Gemini trust boundary, replay switch and offline allowlist | 1:00 |
| 6 | Outcome 03: sources | Family-by-tier table with the reason each family thins out below Critical, the source reliability ladder, the terms register by host class, and the exclusions with reasons | 1:00 |
| 7 | Outcome 03: weight | Tag card, label order and the four tests; FSSI "intelligent tools" (not AI) vs BNY's RTP page (Strong) | 1:00 |
| 8 | AI where it adds recall, code where it adds trust | The 3 Gemini roles plus the local ranker (if shipped); payload guard; V1–V9; seeded Gemini-vs-rules comparison; zero invented URLs | 0:45 |
| 9 | Outcome 04: criticality | Rubric card, table of six tiers, floors, V-000 calibration, BNY floor, sensitivity notes rendered by the engine (FSSI, AutomWorx), two-rater tier check, OCC 2023-17 | 1:15 |
| 10 | Outcome 04: depth | Depth ladder with the reasons for thinner depth, mandatory caps vs discretionary budgets, modifiers, stop rules, manual channels, steps reserved for Meridian | 0:45 |
| 11 | Findings and risk | <br>• Verdict and strongest excerpt per vendor. <br>• The BNY chain: excerpt → tags → Confirmed → exposure 2/3, decision impact 2/3, tier 3, transparency gap 1 → 12 of 18 → High → flip condition. <br>• Fourth-party concentration, generated from the frozen run and split by basis, for example "OpenAI: named by N vendors; DNS verification token only at M more". | 1:15 |
| 12 | Outcome 05: live demo | Script below | 3:00 |
| 13 | Recommendations and controls | Actions for 0–30 and 30–90 days; confidentiality and reproducibility; limits of OSINT | 0:30 |

**Appendix:** rubric anchors, tag legend, verdict rules, risk anchors, one slide per vendor, assumptions, workbook fidelity notes (cream-cell guard, two-part fidelity gate, the L–O and R–T column spans), demo screenshots.

**Demo script (3:00).** Review actions in steps 4–5 use the sandbox copy of `review/`.
1. Upload the workbook; the header typos are tolerated. If "Live" is on, the screen run for a new vendor starts in the background. (0:15)
2. BNY scores 79, the payment-path floor makes it Critical, and FSSI's sensitivity is shown. (0:30)
3. Replay the run, then do the allowlisted live DNS check against the captured result. (0:25)
4. Show the BNY RTP excerpt with its screenshot and Re-verify, reject the FSSI trap, and compare Gemini with the rules. (0:40)
5. Show BNY's risk ("exposure 2/3, decision impact 2/3, tier 3, transparency gap 1 → 12 of 18 = High") with its flip condition, then approve. (0:30)
6. Export and open the workbook: V-000 untouched, Evidence Log and images present, integrity check passes. (0:30)
7. Spare time: show the screen result started at step 1, or the pre-run result. (0:10)

### 5. Per-vendor calibration notes (from scouting)

**V-001 AutomWorx.** Tier High: privileged production credentials over core batch, no customer data. It would become Critical only if dependency were Critical/Total or the service transmitted payment files. Expected: Inconclusive, Medium (Provisional; ceiling Critical if confirmed).
- **Sources and access:** automworx.com via the WordPress REST API, which gives true dates. Sitemap `lastmod` values reflect a May 2026 rebuild.
- **First-party AI signals (U7 or platform context):**
  - the blog line "Ours is Human-Led. AI-Enabled." is U7;
  - the V26 "Bring your own LLM" post is platform-supplier context.
- **Affiliate lead:** a Zoho Forms tenant named `labarum_ai` leads to labarum.ai/capabilities. That page describes NEO as local GPU inference, open-source LLMs, RAG over customer environments and model context protocol, with "Customer-side AI on every engagement". This is an inferred affiliate, graded R1, so it is context only.
- **Platform supplier (Broadcom), context for the questionnaire:**
  - Automic 24.4 and V26 blogs;
  - the October 2025 SPD PDF: Gemini is the default on SaaS; bring-your-own-model options include OpenAI, Claude and Ollama; no confidential data is allowed in prompts.
- **Column P (Inconclusive):**
  - the Labarum NEO capability line, prefixed "Indicator – inferred affiliate, not confirmed";
  - "Ours is Human-Led. AI-Enabled.", prefixed "Marketing statement – not confirmatory";
  - then the negative findings.

  The Broadcom items appear in Q, T and U and are logged in the Evidence Log.
- **Negatives:** no ATS, trust page, AI policy, sub-processor list, AI DNS tokens, SEC filings or GitHub.
- **Entity resolution:** AutomWorx, Inc. vs Honest Nerd dba AutomWorx (the Texas DIR page is behind Cloudflare, so capture it manually) vs NEXRY LLC dba Labarum AI.
- **Traps:** Automic "agents", "Analytics & Intelligence", 23ai, and "Autoworx" businesses.

**V-002 Fiserv.** Tier Critical: system of record for 1.4M customers. Expected: Yes (Probable), High. It becomes Critical on confirmation that AI runs on DNA (Confirmed; E3, K3 → 16).
- **Manual capture only** (fiserv.com and careers.fiserv.com), under the sampling protocol in §2.5. These passages never go to Gemini (`ai_processing_allowed=false`). Items to capture:
  - the agentOS landing page under `/en/lp/` (missing from the sitemap);
  - the agentic-AI insights article;
  - the CSR page;
  - the 2025 Sustainability & Impact Report (internal generative-AI policy, NIST AI RMF);
  - the DNA page and the privacy notice, as negative findings;
  - on the Phenom careers site, the keyword-search counts and the 10 most recent matching postings, preferring account processing or core banking.
- **Automated, SEC (CIK 798354):**
  - 10-K FY2025, "embedding … AI … in our account processing solutions, led by Finxact" (R2). As a filing statement it reaches S2 only if the same passage names a feature or a data flow; otherwise it is S1. Either way it is capped at E2. The 10-K also has a risk factor on third-party models;
  - 10-Q Q2 2026;
  - the DEFA14A Investor Day transcript: agentOS controls, AI-written code, and an agentic ticket-resolution pilot that gives E2 and K2. This pilot carries the Probable verdict.
- **Automated, partner mirrors and other sources:**
  - the AWS press centre (agentOS on Bedrock);
  - Mondo Visione copies of the OpenAI and Microsoft releases;
  - Finovate on Devin;
  - DNS tokens for OpenAI and Anthropic.
- **Column R** (providers Fiserv names): OpenAI, AWS, Microsoft, Cognition, Google (merchant business only), Personetics, Zafin.
- **Why High, not Critical:** no A or B source ties AI to DNA itself.
- **Checks and traps:** re-verify or drop the summarised American Banker and PYMNTS quotes. Traps: "recognition" and "in our DNA".

**V-003 FSSI.** Tier High: tax IDs for the entire customer base, but Moderate dependency. It would be Critical only if dependency were High. Expected: No (Not detected, moderate confidence), None identified.
- **Action:** a no-AI attestation, an AI change-notification clause, and composition-tool questions.
- **Sources:**
  - WordPress REST sweep of pages, posts, press releases and media PDFs;
  - Workable API: 22 postings, none about AI. The Software Engineer posting names OpenText Exstream and Quadient Inspire, whose generative-AI add-ons are platform-supplier context;
  - DNS: Microsoft 365, no AI tokens;
  - privacy notice dated April 2024;
  - the SOC 2 and HITRUST pages;
  - the piworld profile, which is counter-evidence. It is the likely column P excerpt for the No verdict, followed by the negative findings.
- **Modifier:** M-D4, so Wayback history of the policy pages.
- **Traps:** IMb, "intelligent inserting", the "intelligent tools" press release, reader-advice blogs, the 2017 first-person analytics blog (T0, R1), and acronym collisions.
- **Access:** the site backs off after about 30 requests.
  - The WordPress REST sweep uses `per_page=100` and `_fields`. All automated requests to the host, including screenshot loads, stay under about 25 per run.
  - If the site still blocks, coverage is incomplete. The verdict then becomes Inconclusive: Medium (Provisional; ceiling Critical if confirmed). Both outcomes are tested.

**V-004 Terrapin.** Tier High: holdings and compensation NPI for 38,000 wealth clients. Expected: Inconclusive, Medium (Provisional; ceiling High if confirmed).
- **Signals:**
  - DNS verification tokens for OpenAI and Anthropic. These are U4, S1, R2, so they block a "No" verdict, but they belong in Q and T, not R. In column P they appear as "Indicator – relationship only (DNS TXT record)", followed by the negative findings.
  - WordPress REST sweep (136 posts, 47 pages). The AI-readiness FAQ and "supports responsible AI later" are U7/M2 counter-evidence, cited in Q and T. The compensation and security pages are rules-based.
  - A native careers page with only one posting (Office Assistant).
  - The president's podcast has show notes only.
- **Traps:** Yoast llms.txt, the Telik EDGAR collision (CIK 1109196), Outward VC, Terrapinn, and an unverified GitHub org.

**V-005 BNY.** Tier Critical: the payment-path floor applies at a score of 79. Expected: Yes (Confirmed), High; flips to Critical with E3 or K3.
- **Primary and corroborating:**
  - Primary: the instant-payments page, "A single connection to the RTP® network … AI-enabled anomaly detection" (R3).
  - K: The Asian Banker's GP&T interview on "anomaly detection … real-time rails".
- **Supporting:**
  - the 1Q26 IR deck: "~70% of restricted party screening for payments reviewed by AI"; Eliza integrates OpenAI, Google and Anthropic;
  - the September 2026 whitepaper: 42% of sanctions touchpoints, with a human in the loop, which gives K2 and t4. It is a two-column PDF, so check it visually;
  - the 10-K exhibit `bk-20251231_d2.htm` (scan all exhibits);
  - the Responsible AI page (t5);
  - Oracle Recruiting Cloud API and DNS tokens.
- **Scope question, not a conflict:** the November 2025 validation fact sheet says it is rules-based and covers only originated wires and ACH.
- **Traps:** "Claude Reumert", and legacy robots prefixes (use exact prefix matching).

**V-006 TCH.** Tier Critical: operates ACH and RTP clearing and settlement. Expected: Yes (Probable), High; flips to Medium with E1.
- **Signals:**
  - Workday siteMap plus per-job JSON. Detail pages are fetched for titles on the role keyword list, which includes "NOC". NOC Manager JR100193-5 says "Utilize AI-powered tools such as Microsoft Copilot, ChatGPT, ServiceNow AI …" for incident response, which is U3, R2 and E2. The RTP engineering postings describe AI-assisted development (sdlc, E1).
  - DNS: OpenAI token.
  - The Sardine podcast: the fraud head describes computed signals and stays non-committal on AI, which is counter-evidence. It also shows data being analysed beyond transit, which becomes a data question in U.
- **Access:**
  - `*/media/` PDFs are barred by robots.txt, so they are never crawled. Capture them manually if they are publicly viewable.
  - The sitemap is stale, and soft 404s must be detected by page title.
- **Fourth party:** Mastercard, with no AI evidence.
