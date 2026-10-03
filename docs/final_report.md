# Final report — Meridian Vendor AI Footprint (Team Osprey)

Run: `A-20261003-9e73747c` (replay mode) · Date: 2026-10-03

## Deliverables
| File | What it is |
|---|---|
| `submission/Meridian_Vendor_Assessment_FINAL.xlsx` | Final workbook (columns L–V, Evidence Log, Coverage Log, workings). SHA-256 `7d956668d43b26a9f457d04c74875bb886459be2fd624049ac90f1bf1f774abd` |
| `submission/Team_Osprey_Vendor_AI_Footprint.pptx` | 25-slide deck (13 main + 12 appendix), numbers match the workbook |
| `notebooks/footprint_demo.ipynb` | End-to-end replay notebook (local Jupyter / Colab) |
| `footprint_bundle.zip` | Reproducible bundle (untracked, secret-scanned) |
| `docs/p5_review_log.md`, `review/*.jsonl` | Evidence review decisions and overrides |
| `docs/demo_runbook.md` | Demo plan A/B/C and doctor checks |

## Results
| Vendor | Tier (L) | AI usage (O) | Risk class (S) |
|---|---|---|---|
| V-001 AutomWorx | High | Inconclusive | Medium (Provisional) |
| V-002 Fiserv | Critical | Yes | High |
| V-003 FSSI | High | No | None identified |
| V-004 Terrapin | High | Inconclusive | Medium (Provisional) |
| V-005 BNY | Critical | Yes | High |
| V-006 The Clearing House | Critical | Yes | High |

Column V reads `Team Osprey / 03-10-2026` for all six vendors.

## Verification
- Full test suite: green (`uv run pytest -q`, exit 0).
- Two replay runs of `footprint assess ... --mode replay` produce a cell-identical workbook (0 differences across all 9 sheets).
- `uv run footprint verify`: "6 vendors replayed, 107 cited items re-verified, audit clean" / "PASS: replay reproduces the stored cells".
- Coverage Log: the 6 pending fiserv.com / bny.com family rows (V-002 and V-005 LEG, PRD, JOB) now say "not captured: host terms bar automated access; manual capture not performed". This is done when the sheet is written (`src/footprint/sheets.py`, `_not_captured`), so stored runs and verify are not affected.

## Limitations
- **No manual captures.** Fiserv and BNY sites, product pages and job boards have terms that bar automated access. They were not crawled and not captured by hand. Those families are logged as gaps.
- **BNY and Fiserv** therefore rest on SEC filings, partner/industry sources and DNS evidence. BNY has no first-party website evidence.
- **The evidence review was AI-assisted** (`claude-ai-assisted`, 72 items: 42 accepted, 30 rejected). It is not an independent human review. A human should skim `docs/p5_review_log.md` before submission, especially the AutomWorx rejections and the K = 3 overrides and manual T-cell edits for BNY, Fiserv and TCH.
- AutomWorx and Terrapin are Provisional. Their verdicts rest on weak indicators (AutomWorx) and bot-blocked sites (Terrapin).
- Slide 6 still describes executive posts as "captured by hand". That is the policy in `config/depth.toml`; it was not done in this run.

## How to reproduce
```
uv sync
uv run pytest -q
uv run footprint assess data/input/Meridian_Vendor_Input.xlsx --mode replay --out submission/Meridian_Vendor_Assessment_FINAL.xlsx --force
uv run footprint verify
```
Replay mode uses only cached collection and cached Gemini outputs. It makes no network calls and needs no API key.
