# footprint: vendor AI public-footprint analysis

> **Confidential case-study material.** This private repository holds work for an Optiv Consulting case study
> about real organisations. Do not make it public, do not publish the deck or workbook, and do not contact
> vendors.

`footprint` takes Meridian's third-party vendor workbook and, for each vendor:

1. rates **criticality** from the profile alone (deterministic rubric);
2. sets the **assessment depth** that tier warrants;
3. collects **public OSINT evidence** (filings, product and legal pages, job postings, DNS, provider stories)
   under robots.txt, terms-of-use and rate-limit controls;
4. **classifies each signal** with transparent tags (rules + Gemini, every quote verified against the captured
   source);
5. concludes whether AI is **genuinely used** in the service, and classifies the resulting **AI security risk**
   with recommended actions;
6. writes the results into the workbook's student columns (L-V), plus Evidence Log and Coverage Log sheets.

Every finding traces to a captured, hashed source. Runs can be replayed offline from the frozen evidence pack.

## Quick start

```bash
uv sync --all-extras
cp .env.example .env            # then add GEMINI_API_KEY (optional) and FOOTPRINT_SEC_CONTACT
uv run pytest
uv run footprint --help
uv run streamlit run app/streamlit_app.py
```

The notebook `notebooks/footprint_demo.ipynb` runs the same engine on Google Colab or local Jupyter.

Design and method: [docs/design.md](docs/design.md).
