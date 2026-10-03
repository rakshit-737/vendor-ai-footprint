"""footprint demo app (Outcome 05): upload the vendor inventory, assess it, review the evidence, export.

Run: ``uv run streamlit run app/streamlit_app.py``. The server listens on localhost only and sends no usage
statistics: app/.streamlit/config.toml sits next to this script, so Streamlit applies it from any working directory
(the repo root's .streamlit/config.toml holds the same settings). To state it on the command line as well, add
``--server.address localhost --browser.gatherUsageStats false``.
Pages: Assess (home) · Evidence · Findings & Risk · Export.
Shared helpers live in app/ui.py; the engine is footprint.pipeline (contracts: docs/contracts_p3.md section 15).
Each page calls ui.page_setup() itself (sidebar included), so it also works when Streamlit runs it directly.
"""

from __future__ import annotations

import streamlit as st

import ui

st.set_page_config(page_title="footprint · vendor AI footprint", page_icon=ui.PAGE_ICON, layout="wide",
                   menu_items={"Get help": None, "Report a bug": None,
                               "About": "footprint: vendor AI public-footprint analysis (local demo)."})
ui.init_session()

page = st.navigation([
    st.Page("pages/assess.py", title="Assess", icon=":material/fact_check:", default=True),
    st.Page("pages/evidence.py", title="Evidence", icon=":material/find_in_page:"),
    st.Page("pages/findings.py", title="Findings & Risk", icon=":material/shield:"),
    st.Page("pages/export.py", title="Export", icon=":material/download:"),
])
page.run()
