# P4 gate report

Run `A-20261003-46a58030`, as of 2026-10-03. Checked 2026-10-03, offline replay only (no network, 0 Gemini API calls). Nothing committed.

## Gate checklist

| Phase | Gate | Result | Evidence |
|---|---|---|---|
| P2 | URL recall >= 90% of in-scope gold URLs captured or explained | PASS (100.0%, 73 in scope: 64 captured, 9 explained, 0 missed; 22 excluded on manual-only hosts) | `runs/eval/gold_report.md` |
| P2 | Collection is GET-only and respects ToS/robots | PASS | ToS register + robots decisions; 67 items withheld from Gemini by host policy |
| P3 | Every trap rejected, no citable item rests on a trap | PASS (5/5, 0 leaks) | `runs/eval/gold_report.md` |
| P3 | Cited quotes verified against captured text | PASS (0 unverified cited quotes; 125 cited items re-verified by `footprint verify`) | `footprint verify` output |
| P3 | Only public text to Gemini; payload audit clean | PASS (`check_audit` on `llm_calls.jsonl`: clean; 51 calls, gemini-3.5-flash-lite) | `footprint verify`: "audit clean" |
| P4 | Full test suite green | PASS: 1678 passed, 1 skipped, 9 deselected (network) in 211 s | `uv run pytest` |
| P4 | Deterministic replay | PASS: two `footprint assess --mode replay --force` runs give identical L-V cell values for all 6 vendors; `footprint verify`: "PASS: replay reproduces the stored cells" | openpyxl compare of both outputs |
| P4 | Workbook fidelity: V-000 example and provided cells untouched | PASS: `check_fidelity(input, draft)` returns [] (V-000 row 5 unchanged) | `workbook.check_fidelity` |
| P4 | All L-V filled; L/O/S use allowed values | PASS: 6 vendors x 11 fields, no empty cells, no disallowed values | `ALLOWED_VALUES` check |
| P4 | Every P excerpt is an exact slice of the evidence text store | PASS: 10 of 10 quoted excerpts in P equal `evidence/text/<doc_id>.txt[start:end]` and the stored excerpt | openpyxl + evidence.jsonl check |
| P4 | O and S match design.md expected outcomes | PASS for all 6 vendors (3 secondary differences below) | `docs/p3_p4_run_report.md` |

## Metrics

- Tests: 1678 passed, 1 skipped, 9 network tests deselected.
- Gold: URL recall 100%. Passage recall 84.3% overall; V-004 lowest at 28.6% because the site blocks the bot.
- Traps: 5/5 rejected or suppressed, 0 leaks.
- Claims: 346 of 364 passed verification. 0 unverified quotes cited in the workbook.
- Payload audit: clean. 67 items withheld (host terms/robots bar AI processing).
- Determinism: 2 replays plus the live cells are identical. 51 cache hits, 0 API calls.

## Results

| Vendor | O | S |
|---|---|---|
| V-001 | Inconclusive | Medium (provisional, ceiling Critical) |
| V-002 | Yes (Probable) | High (capped, ceiling Critical) |
| V-003 | No (Not detected) | None identified |
| V-004 | Inconclusive | Medium (provisional, ceiling High) |
| V-005 | Yes (Probable) | High (capped, ceiling Critical) |
| V-006 | Yes (Probable) | High (capped, ceiling Critical) |

## Remaining gaps

1. Manual captures pending. These hosts are manual-only and nothing from them has been imported yet:
   - fiserv.com: V-002, plus aboutamazon press.
   - bny.com: V-005.
   - Leads seeded but not imported: dir.texas.gov for V-001 and americanbanker.com for V-002.
   - See `docs/manual_capture_checklist.md`.
2. BNY terms: V-005 is Probable instead of the expected Confirmed. Its exact-service pages and its legal and trust pages are on bny.com, which is manual-only. T for V-005 shows "legal and trust pages ... still pending."
3. K (decision impact) is assumed at 3 for V-002, V-005 and V-006. The design expected an evidenced 1 or 2, and the excerpts that would set it are on the manual-only hosts.
4. V-002's transparency gap is 2 instead of 1, because its trust pages are manual-only.
5. V-004 passage recall is low because terrapintech.com blocks the bot. It is covered only by DNS TXT records and EDGAR/ATS negatives.

The plan for items 1 to 4: import the manual captures, then re-run `footprint assess` with `--mode live_ai` or replay, then `footprint verify`. No code change is indicated.

## Open questions

1. The P closing says "screenshots" when any non-Logged cited item has one. V-003 quotes no excerpt in P but its Context items have screenshots. Should the check be limited to the items quoted in P? That would be a one-line change in `compose.py`.
2. Should V-005 stay Probable/High in the submission, or should the BNY manual capture be completed first to reach Confirmed/Critical?
3. Should the K=3 assumption for the payment-path vendors stay as-is, flagged "(assumed)" in T, until the manual captures are imported?
4. Does the team accept V-004 being Inconclusive given the bot block, or should someone capture terrapintech.com manually?
