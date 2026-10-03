# Demo runbook

How to run the live demo of `footprint` for the Optiv case-study panel. Everything below runs in **replay mode**:
offline, from the frozen evidence pack, the LLM cache and the review stores. Replay makes no network calls and no
new Gemini calls, and it reproduces the same L-V cells on every run.

Confidential: never share or publish the bundle, the notebook outputs, the exported workbook or screenshots
outside the panel. Do not screen-share `.env`.

## 0. Doctor checks (T-30 min, on the demo laptop)

Run these from the repo root (`D:/Optiv/vendor-ai-footprint`) in Git Bash or PowerShell. Set `PYTHONUTF8=1`
first (PowerShell: `$env:PYTHONUTF8 = "1"`).

| # | Command | Expected |
|---|---------|----------|
| 1 | `uv sync --all-extras` | completes with no errors |
| 2 | `uv run footprint version` | prints `0.1.0` |
| 3 | `uv run pytest -q` | all pass (network tests are deselected by default) |
| 4 | `uv run pytest tests/unit/test_app.py -q` | 42 passed (Streamlit AppTest of every page) |
| 5 | `uv run footprint verify` | `6 vendors replayed, 107 cited items re-verified, audit clean` and `PASS: replay reproduces the stored cells` |
| 6 | `uv run footprint coverage <collection run id>` (ids under `runs/`) | Coverage Log prints, one status per family. Check the Fiserv and BNY host families against §5 |
| 7 | `footprint_bundle.zip` exists in the repo root (about 43 MB) | needed only for Plan B on Colab |
| 8 | Wi-Fi may be off | replay needs no network |

If check 5 fails, do not demo live; go to Plan C.

## Plan A: Streamlit app, replay (primary, about 8 min)

1. `uv run streamlit run app/streamlit_app.py`. It serves on `http://localhost:8501` only and sends no usage
   statistics.
2. In the sidebar, enter the analyst initials and the team name.
3. **Assess** page, section 1: upload `data/input/Meridian_Vendor_Input.xlsx`, or click the bundled-workbook
   button. Expect "Workbook checked: sheet "Vendor Inventory", headers in row 4, 6 vendors".
4. Show the criticality cards (columns L-M): score, factors, floors, sensitivity. You can show the HC1 override
   form. Overrides made in the app go to a sandbox copy of the review store, never the project store.
5. Leave mode on **Replay** and click **Run**. The run takes about 30-60 s. Expect "Replay run finished" and a
   summary table of 6 vendors:
   - V-001 AutomWorx: High, Inconclusive, Medium (provisional)
   - V-002 Fiserv: Critical, Yes (Probable), High
   - V-003 FSSI: High, No, None identified
   - V-004 Terrapin: High, Inconclusive, Medium (provisional)
   - V-005 BNY: Critical, Yes (Probable), High
   - V-006 TCH: Critical, Yes (Probable), High
6. **Evidence** page: open one cited item and show its SHA-256, exact quote and screenshot. You can show an
   HC2 accept or reject; the vendor is rescored live.
7. **Findings & Risk** page: show the why-trace (AI usage, then AI risk) and the L-V cells.
8. **Export** page: click **Build**. Expect "Integrity check passed". Then **Download the assessed workbook** and
   open the appended sheets: Evidence Log, Coverage Log, Criticality Workings, Method & Legend, Evidence Images
   and Run Info.

The full flow was verified on 2026-10-03 with an AppTest against the real replay pipeline (upload, run,
evidence, findings, export, download).

## Plan B: Jupyter notebook (fallback, about 5 min)

### Local

1. `uv run --with jupyterlab jupyter lab notebooks/footprint_demo.ipynb`
2. In the parameters cell, set `ASK_UPLOAD = False` to skip the upload widget and use the bundled workbook. Keep
   `MODE = "replay"`.
3. Choose **Run > Run All Cells**. It takes about 45 s. The final **Verify** table must show 5 PASS rows,
   including "Cited evidence re-verifies: 107 of 107 items".
4. The exported workbook lands in `scratch/notebook/`, which is not tracked.
5. Before any commit, clear the outputs with **Edit > Clear Outputs of All Cells** and save. The committed
   notebook carries no outputs.

Headless check: this command runs the notebook with nbclient, as the 2026-10-03 run did, and passed:
`uv run --with nbclient --with ipykernel python -c "import nbformat; from nbclient import NotebookClient; nb=nbformat.read('notebooks/footprint_demo.ipynb',4); NotebookClient(nb, timeout=900, resources={'metadata':{'path':'.'}}).execute(); print('OK')"`

### Google Colab (for the user)

1. Build the bundle on the dev machine:
   `uv run python -c "from footprint.bundle import build_bundle; print(build_bundle())"`
   This writes `footprint_bundle.zip` (about 43 MB, 3,160 files, SHA-256 manifest inside). The build refuses if
   it finds any secret, and the zip is git-ignored. Never commit or publish it.
2. Open https://colab.research.google.com and choose **File > Upload notebook**. Upload
   `notebooks/footprint_demo.ipynb`, the stripped copy with no outputs.
3. Choose **Runtime > Run all**. In cell 1 (Setup), choose `footprint_bundle.zip` when the file picker asks. The
   cell unpacks the bundle to `/content/footprint`, installs the bundled wheel and checks every file against the
   manifest.
   - If Setup asks you to **restart the session** (the kernel already had an older pydantic or extraction
     package), choose **Runtime > Restart session**, then **Run all** again. The bundle is not uploaded a second
     time.
   - If it warns that the LLM schema hashes differ, replay may miss the cache. Stop and use Plan A.
4. In cell 2, upload the workbook or leave it empty to use the bundled one.
5. In section 7, the download cell offers the exported workbook.
6. When you finish, choose **Runtime > Disconnect and delete runtime**. Do not save the executed notebook to Drive
   or GitHub, and do not share the Colab link.
7. Do not enter a Gemini key in Colab. Replay does not need one, and live modes are out of scope for the demo.

## Plan C: screenshots (last resort)

Use this plan if neither Python environment works on the day.

- Before the demo, capture screenshots of a successful Plan A run, in this order: Assess (workbook checked), the
  run summary table, Evidence (one item with its quote and screenshot), Findings (why-trace), Export (integrity
  passed), and the exported workbook's Evidence Log and Coverage Log sheets.
- Keep them in `scratch/demo_screens/` (untracked) and in the private deck appendix only.
- Show the exported workbook from the last good run (`scratch/notebook/Meridian_Vendor_Assessment_*.xlsx`) in
  Excel.

## 5. Known coverage notes to say out loud

- **fiserv.com and bny.com families**: not captured, because the host terms bar automated access and no manual
  capture was performed. These families appear in the Coverage Log as negative-evidence rows, not as failures.
- **BNY (V-005)**: the finding rests on SEC filings, partner pages and DNS evidence.
- Verdicts marked "provisional" (V-001, V-004) mean the public footprint is inconclusive; the follow-up actions
  (questionnaire, contract review) are reserved for Meridian.
