# footprint — project rules for Claude Code and its agents

Vendor AI public-footprint analysis (Optiv case study 1, client "Meridian"). Design: `docs/design.md`.
Approved plan + phases: `docs/design.md` §Phases. Read the relevant design section before changing a module.

## Confidentiality (hard rules, from the client brief)
- The GitHub repo `rakshit-737/vendor-ai-footprint` must stay PRIVATE. Never change visibility, never create
  gists, Pages, public forks, public Actions artifacts. `.githooks/pre-push` refuses non-private pushes.
- Never publish the deck, workbook or findings anywhere (no Artifact, Docs, Drive, paste sites).
- Never contact vendors: GET requests only, no forms, no logins, no account creation, no emails.
- Only PUBLIC vendor text may be sent to Gemini (free tier may train on prompts). Meridian-internal profile
  fields (columns D-K free text), tiers, verdicts, findings and the word "Meridian" never go in a prompt.
  All Gemini traffic goes through `footprint.ai` and its payload guard.

## Collection rules
- Every network request passes, in order: ToS register (`config/tou.toml`) -> robots.txt (Protego, UA token
  `footprint-osint`) -> per-host rate limit. Playwright page loads too.
- fiserv.com (Terms s.7) and linkedin.com are manual-capture only and `ai_processing_allowed=false`.
- Never use Wayback "Save Page Now". Never evade bot protection (no TLS impersonation, no stealth browsers).
- LLM output never supplies URLs. Evidence is cited only after capture + SHA-256 + exact-quote verification.

## Workbook rules (`data/input/Meridian_Vendor_Input.xlsx`, sheet "Vendor Inventory")
- Write ONLY student (cream, fill FFFFF6E0) cells L-V of vendor rows 6-11. Never edit row 5 (V-000 example)
  or any provided (blue-grey) cell. Never add/remove vendor rows.
- Never read or write `column_dimensions` for M, N, O, S, T (they live inside the `<col>` spans L-O and R-T;
  touching them makes Excel show a repair prompt).
- Force every written value to a string (no formulas). Respect per-column length budgets.

## Determinism
- Replay mode (default) must reproduce identical L-V cells from the frozen evidence pack + caches, offline.
- Hash raw bytes; write files as UTF-8 with LF. No wall-clock time in decision logic (use `as_of`).

## Dev
- `uv sync --all-extras` · `uv run pytest` · `uv run footprint --help` · `uv run streamlit run app/streamlit_app.py`
- Windows: set `PYTHONUTF8=1` (see `.env.example`). Python >=3.11 (dev box 3.14, Colab 3.12).
- Tests that need the network are marked `@pytest.mark.network` and are deselected by default.
- Keep modules single-purpose; config in `config/*.toml` (tomllib); pydantic models in `footprint/models.py`.
