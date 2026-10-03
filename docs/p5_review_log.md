# P5 evidence review log (AI-assisted)

Reviewer: `claude-ai-assisted`, dated 2026-10-03. This was an AI-assisted HC2 review, not an independent human
review. Each excerpt was read in its source context in the evidence text store (`evidence/text/<sha256>.txt`, about
400 to 700 characters either side of the offsets). All cited excerpts were checked against the stored text and
match it exactly. There was no network collection and no new Gemini calls: every assessment ran in replay.

- Starting assessment: `A-20261003-46a58030`. Final assessment: `A-20261003-9e73747c` (replay, as of 2026-10-03).
- Output workbook: `submission/Meridian_Vendor_Assessment_P5.xlsx`.
- `footprint verify`: 6 vendors replayed, 107 cited items re-verified, audit clean, replay reproduces the stored
  cells. `pytest`: all tests pass.
- Stores: `review/reviews.jsonl` (evidence reviews) and `review/overrides.jsonl` (risk-input overrides and cell
  edits).

## Scope and iteration

The scope was every item that was cited or decisive: roles Primary, Supporting and Indicator; verdict
decisive/qualifying/corroborating items; risk E, K and transparency-gap items; items cited in any L-V cell; and the
items behind Fiserv's X1 escalator. A rejection can move another item into the cited set, and Evidence IDs are
renumbered on every run. For both reasons the review was repeated after each replay until no cited or decisive item
was left unreviewed.

For V-001, the source pages of all Labarum AI items were checked. None of them names AutomWorx, so every Labarum AI
item was rejected in one pass, which stopped the cascade.

A simulation was also run in a scratch review store. It rejected every AutomWorx blog item (111). That would move
V-001 to O = No and S = None identified. A wholesale rejection of the vendor's own blog would be neither accurate
nor conservative, so it was not done. The two remaining AutomWorx blog Indicators were accepted as Indicators only.

## Counts

| Vendor | Accepted | Rejected |
|---|---|---|
| V-001 AutomWorx | 5 | 24 |
| V-002 Fiserv | 14 | 4 |
| V-003 FSSI | 3 | 1 |
| V-004 Terrapin | 2 | 0 |
| V-005 BNY | 11 | 1 |
| V-006 TCH | 7 | 0 |
| **Total** | **42** | **30** |

## Rejections (Evidence IDs as numbered in the final run)

| E-ID | Publisher | Reason |
|---|---|---|
| V-001-E-0118 to E-0139 (22 items) | Labarum AI | Misattributed entity: Labarum AI website copy. The captured pages never name AutomWorx and the affiliation is only inferred. It is marketing, not evidence of AI use in the vendor's service. |
| V-001-E-0143, V-001-E-0144 | AutomWorx | Marketing tagged as use: the blog sign-off advertises "AI pipeline orchestration" as a service AutomWorx sells. It does not show AI used to deliver Meridian's batch scheduling. |
| V-002-E-0015 | Finovate | A trade-press summary bullet, not a verbatim vendor statement. The naming of Devin/Cognition is not confirmed by a vendor or SEC source. |
| V-002-E-0016 | Finovate | Mixes the journalist's paraphrase with commentary by Cognition (the provider) about Fiserv. It is commentary about others from trade press only. |
| V-002-E-0107 | Finovate | A trade-press paraphrase of the Cognition announcement, not a vendor or SEC source. |
| V-002-E-0031 | Fiserv (DEFA14A transcript) | Misattributed voice: the words are a question from a KeyBanc sell-side analyst, not a statement by Fiserv. |
| V-003-E-0010 | OpenText | Commentary about others: a generic survey statistic on AI ROI. It says nothing about FSSI. |
| V-005-E-0008 | BNY (8-K exhibit) | A flattened chart and metrics fragment (axis labels and percentages). No verbatim claim about AI use can be attributed from it. |

The exact reason recorded for each item is in `review/reviews.jsonl` and in the Evidence Log columns "Review
reason" and "Reviewer".

Accepted items fall into five groups:

- Verbatim SEC filing text in the vendor's own voice (Fiserv 10-K and DEFA14A; BNY 8-K exhibits).
- Vendor press releases.
- TCH's own job postings.
- DNS TXT verification tokens, accepted as relationship context only.
- Platform-supplier texts (Broadcom, OpenText), accepted as supplier context only.

## Judgment calls and overrides

1. **Fiserv X1 (provider named only by a third party).** X1 rested only on Finovate trade press naming Devin
   (Cognition). The decision was to keep the escalator off unless a vendor or SEC source names Devin. The three
   Finovate items were rejected with that reason. The Finovate page does quote a Fiserv executive, but trade press
   is not a vendor or SEC source.
   - Result: X1 changes from fired to off.
   - S is unchanged at High. Before the review, X1's High floor already sat below the pre-cap Critical, and the
     Probable cap set High.
   - Questionnaire item: "Does Fiserv use Cognition Devin or other autonomous coding agents on code or environments
     that process Meridian data? Name the provider and the controls."
   - No risk-input override key exists for escalators, so this decision is recorded only through the evidence
     rejections.
2. **K = 3 assumed for BNY (V-005), Fiserv (V-002) and TCH (V-006).** The conservative assumed value was kept.
   - A `risk_input_override` was recorded for each vendor: key `k`, value `3`, no evidence, so the value stays
     marked as assumed. The reason names the role in the payment path and the flip condition.
   - The flip condition: K falls if the vendor confirms in writing that AI outputs are advisory or human-reviewed.
   - A `cell_edit` on T was recorded for each of the three vendors. The generated T text was shortened, with the
     substance unchanged, so that a closing sentence fits within the 1,050-character budget: "Decision impact 3 is
     assumed (payment-path role); it falls if the vendor confirms AI outputs are advisory or human-reviewed."
   - Caveat: these cell edits freeze T for the three vendors. If a later run changes the computed rationale, the
     edits must be re-recorded.
3. **Fiserv and BNY coverage.** No manual captures were performed. For the fiserv.com and bny.com families, the
   coverage note is: "not captured: host terms bar automated access; manual capture not performed". BNY's
   assessment stays on its SEC, partner and DNS evidence. The edited T for Fiserv and BNY says the legal/trust,
   product/newsroom and job-posting searches were not completed. The Coverage Log rows themselves come from the
   collection runs and were not changed, because that would need new collection.

## Before and after, per vendor (O / S)

| Vendor | Before (A-20261003-46a58030) | After (A-20261003-9e73747c) | Escalators fired |
|---|---|---|---|
| V-001 AutomWorx | Inconclusive / Medium (Provisional) | Inconclusive / Medium (Provisional) | none to none |
| V-002 Fiserv | Yes / High | Yes / High | X1 to none |
| V-003 FSSI | No / None identified | No / None identified | none to none |
| V-004 Terrapin | Inconclusive / Medium (Provisional) | Inconclusive / Medium (Provisional) | none to none |
| V-005 BNY | Yes / High | Yes / High | none to none |
| V-006 TCH | Yes / High | Yes / High | none to none |

No O or S cell changed. The changes are in rationale and evidence:

- V-001 now rests on AutomWorx's own blog Indicators instead of Labarum AI copy.
- V-002 and V-005 cite renumbered, reviewed items.
- T for V-002, V-005 and V-006 now states the assumed K and its flip condition.

## Wording change for AI-assisted review

The README was not edited. In `src/footprint/compose.py`, the Evidence Log status for a rejection by an
AI-assisted reviewer now reads "rejected in AI-assisted review" (a human reviewer still gets "rejected by analyst").
The sheet note now says that reviewer `claude-ai-assisted` marks an AI-assisted review, not an independent human
review. The Reviewer column shows `claude-ai-assisted 2026-10-03` on all 72 reviewed rows.
