# P3/P4 run report

Run `A-20261003-46a58030`, mode `live_ai`, as of 2026-10-03 (UTC). Collection reused the same-day collection runs
(`V-00x-20261003-*`); no site was requested again. Exported to
`submission/Meridian_Vendor_Assessment_P4_draft.xlsx` (check_fidelity clean). Deck built from `runs/findings.json`
(`submission/Team_Osprey_Vendor_AI_Footprint.pptx`, 25 slides).

Determinism: two replay runs (offline, cache only) gave identical L-V cells, and both match the live_ai cells for all
six vendors. `footprint verify` passes: 125 cited items re-verify against the evidence store, and the audit log is clean.
Test suite: 1678 passed, 1 skipped, 9 deselected (network).

## Summary

| Vendor | Tier | O | Rule | Likelihood | Confidence | E·K·TP·TG = ARP | Gate | Base | Escalators | Cap | Final S | Ceiling |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| V-001 AutomWorx | High | Inconclusive | f | roughly even chance | Moderate | 3a·3a·2·3 = 17 | no | Critical | – | Medium | Medium (Provisional) | Critical |
| V-002 Fiserv | Critical | Yes | c Probable | likely | Low | 2·3a·3·2 = 15 | yes | Critical | X1 | High | High | Critical |
| V-003 FSSI | High | No | e Not detected | unlikely | Moderate | 0·0·2·3 = 5 | no | Low | – | None identified | None identified | – |
| V-004 Terrapin | High | Inconclusive | f | roughly even chance | Low | 2a·2a·2·3 = 13 | no | High | – | Medium | Medium (Provisional) | High |
| V-005 BNY | Critical | Yes | c Probable | likely | Low | 2·3a·3·2 = 15 | yes | Critical | – | High | High | Critical |
| V-006 TCH | Critical | Yes | c Probable | likely | Moderate | 2·3a·3·2 = 15 | yes | Critical | – | High | High | Critical |

"a" means assumed under the unknowns rule.

Flip conditions (column T):
- V-001: confirmation that the AI platform of the inferred affiliate Labarum AI delivers Meridian work would make the
  verdict Yes and could raise the class to Critical.
- V-002, V-005, V-006: independent confirmation of AI use in the service itself would lift the Probable cap and raise
  the class to Critical.
- V-003: evidence that AI features of the platforms the vendor uses, or AI tools its staff use, touch Meridian data
  would require a reassessment.
- V-004: confirmation that the AI services indicated only by DNS records (Anthropic, OpenAI) are used on Meridian data
  would make the verdict Yes and could raise the class to High.

## Top evidence (decisive items)

- **V-001** (Indicator): `https://labarum.ai/`: "Twenty years of platform depth across 250+ Fortune 1000 accounts gets
  extended by AI infrastructure built to run where customer data runs." `https://labarum.ai/public-sector/`: "Agencies
  engage Labarum AI for enterprise workload automation migrations, managed support of production scheduling
  environments, ..."
- **V-002** (Primary): SEC DEFA14A
  `https://www.sec.gov/Archives/edgar/data/798354/000119312526226797/d150338ddefa14a.htm`: "Two weeks ago, we
  re-architected our client service portal to drive more self-service and agentic AI capabilities to resolve our
  tickets." (Supporting) Mondo Visione: "GitHub Copilot has been deployed to more than 8,000 software engineers across
  Fiserv, ..."
- **V-003**: none. Negative findings for the legal/trust, EDGAR, jobs and DNS families. OpenText AI options are logged
  as a platform-supplier Context item only.
- **V-004** (Indicator): DNS TXT `terrapintech.com`: `anthropic-domain-verification-...` and
  `openai-domain-verification=...`.
- **V-005** (Primary): SEC 8-K exhibit 99.3
  `https://www.sec.gov/Archives/edgar/data/1390777/000139077726000042/ex993_quarterlyupdatepre.htm`: "~220 enterprise
  AI solutions in production, and ~140 digital employees ... embedding AI to automate at scale, e.g., multi-currency
  payments processing ...". (Supporting) The Asian Banker: "In the payments environment, AI supports anomaly detection
  and exception management across both cross-border and real-time rails."
- **V-006** (Primary): TCH Workday posting "Manager, NOC Operations": "Utilize AI-powered tools such as Microsoft
  Copilot, ChatGPT, ServiceNow AI, or similar technologies to enhance documentation, reporting, knowledge management,
  and incident response ...". (Supporting) "Sr Software Engineer": "... introducing modern automation and AI-assisted
  engineering practices ...".

## Diff against the expected outcomes (design.md)

| Vendor | Expected | Actual | Match | Root cause |
|---|---|---|---|---|
| V-001 | Inconclusive; 3a·3a·2·3 = 17; Medium Prov.; ceiling Critical | same | yes | – |
| V-002 | Yes (Probable); 2·2·3·1 = 12; High | Yes (Probable); 2·3a·3·2 = 15; High (capped) | O and S match; inputs differ | K: no captured item states what the AI output does, so K is assumed 3 from the payment-path role. The Investor Day pilot excerpt that set K2 is on fiserv.com (manual-only, not yet imported). TG 2 rather than 1: only t1/t2 are publicly evidenced, because the trust pages on fiserv.com are manual-only. The Probable cap holds S at High either way. |
| V-003 | No (Not detected); None identified | same (confidence Moderate) | yes | – |
| V-004 | Inconclusive; 2a·2a·2·3 = 13; Medium Prov.; ceiling High | same | yes | Legal/trust and product families are blocked_bot, so the verdict is f (Inconclusive) with Low confidence. |
| V-005 | Yes (**Confirmed**); 2·2·3·1 = 12; High | Yes (**Probable**); 2·3a·3·2 = 15; High (capped) | O and S match; rule differs | No Q at R3: the exact-service pages (bny.com instant-payments, payables, validation) are on a manual-only host and were not imported, so rule b cannot fire. The 8-K exhibit and trade press reach R2 only. K is assumed 3 because only some pathways state their output role (the pathways that do show K1). |
| V-006 | Yes (Probable); 2·1·3·2 = 11; High | Yes (Probable); 2·3a·3·2 = 15; High (capped) | O and S match; K differs | The job postings do not state what the AI output does, so K is assumed 3 from the payment-path role, not K1. The Probable cap keeps S at High. Under the expected inputs the flip would be E1 → Medium; with the assumed K3 it is not. |

All six O and S values match the expected table. The remaining differences are in the inputs (K assumed where design
expected an evidenced K1/K2) and in V-005's rule (Probable rather than Confirmed). Each traces to exact-service pages
on manual-only hosts (fiserv.com, bny.com) that have not been imported yet. Importing them through
`evidence/manual_inbox` and rerunning `footprint assess` is the remedy. No code change is indicated.

## Gemini usage

- Model: `gemini-3.5-flash-lite` (first in the chain); no switches, no quota or error responses.
- Calls: 51 API calls in live_ai, 0 cache hits. Replay: 51 cache hits, 0 API calls.
- Per vendor (actual / planned): V-001 18/20, V-002 14/40, V-003 3/20, V-004 2/20, V-005 9/40, V-006 5/40. No budget
  stops.
- Withheld: 67 items, all `host_ai_disallowed` (ToS register or robots policy). Withholding rates: V-001 0%,
  V-002 2.9%, V-003 42.6%, V-004 58.8%, V-005 12.0%, V-006 8.0%. No Meridian, team or profile-shingle blocks.
- Verification: 364 claims, 346 verified into items. V4 repaired 271 claims (dropped indicators). V2 and V3 rejected
  13 and 5 claims.
- Audit release gate (`check_audit`): clean.
- Gemini vs rules (gold report): 340 of 527 items carry Gemini labels; 89 agree and 251 disagree (temporal 193,
  action_level 67, sp 149). 113 proposals are pending HC2 review and are not citable until accepted.

## Gold report summary (`runs/eval/gold_report.md`)

- P2 URL recall gate: PASS, 100% (73 in scope: 64 captured, 9 explained; 22 excluded as manual-only or SKIP).
- P3 trap gate: PASS. 5 of 5 gold traps were rejected or suppressed, and no item leaked.
- Passage recall: 84.3% (74 of 89). Strong 100%, moderate 93.3%, weak 75.9%, marketing-only 72.2%. V-004 is low
  (28.6%) because its site is behind bot protection.

## Integration changes in this pass

- `verdict.py`: rule b counts, and `verdict.corroborating`, now use `cluster.distinct_sources`, so syndicated copies
  count once.
- `compose.py`: the column P closing sentence claims screenshots only when a cited item has one. Otherwise it reads
  "... with retrieval dates and SHA-256 hashes." (V-006). The legend's "Independent" row now mirrors
  `cluster.independent`, including the vendor-copy rule.
- `pipeline.py`:
  - `utc_today()` is the single default for `as_of` (collection and assessment).
  - `assessment_run_id` now hashes `as_of`.
  - New `review_stores()` helper.
  - `export_assessment` re-runs `check_audit` on `runs/<run_id>/llm_calls.jsonl` as a release gate.
- `cli.py`: new commands `assess`, `review accept|reject`, `verify` and `eval`.
- `.gitignore`: adds `**/.streamlit/secrets.toml`.
- Notebook: passes the override and review stores to `run_assessment`.
