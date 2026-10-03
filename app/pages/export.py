"""Export: build the assessed workbook in memory, prove that nothing outside the student cells changed, download it."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import ui

ui.page_setup("Export")
st.title("Export")
st.caption("Writes the student cells L-V of the vendor rows and appends the evidence sheets. The worked example "
           "V-000 and every provided cell stay untouched; the fidelity check proves it before the download.")
ui.show_flash()
inp, data = ui.require_input()
if not data.ok:
    st.error("The workbook failed validation; fix it on the Assess page first.")
    st.stop()

stores = ui.session_stores()
record = ui.active_run()
api = ui.pipeline_api()
if record is not None and record.full and api.export_assessment is not None:
    assert record.result is not None
    st.markdown(f"Exporting run **{record.result.run_id}** ({ui.MODE_LABELS[record.mode]}, as of "
                f"{ui.dmy(record.result.as_of)}): columns L-V for {len(record.result.vendors)} vendors, plus the "
                "Evidence Log, Coverage Log, Criticality Workings, Method & Legend, Evidence Images and Run Info "
                "sheets.")
    if ui.is_stale(record, stores, data):
        st.warning("A tier override or the review store changed since this run; run it again first to include it.")
elif record is not None and record.full:
    st.info(ui.EXPORT_P1_NOTE)
else:
    st.info("Exporting criticality and depth only (columns L-N, plus the Criticality Workings and Method & Legend "
            "sheets): " + ("this run has no evidence, verdict or risk results." if record is not None
                           else "no full assessment has been run for this workbook and mode."))

team = ui.session_team()
if not team:
    st.warning("Set the team name in the sidebar: column V reads 'Team <name> / <date>'.")
overwrite = st.checkbox("Replace values already in the student cells", key="fp_export_overwrite",
                        help="Needed only when the uploaded workbook already has filled student cells.")
key = ui.export_key(inp, record, team, overwrite)
if st.button("Build the workbook", type="primary", icon=":material/build:", key="fp_export_build"):
    with st.spinner("Writing and checking the workbook…"):
        try:
            outcome = ui.export_run(record, inp, data, stores, team=team, overwrite=overwrite, api=api)
        except Exception as exc:  # noqa: BLE001 - WorkbookWriteError, FidelityError or a pipeline problem
            issues = getattr(exc, "issues", None) or []
            problems = getattr(exc, "problems", None) or []
            st.error(f"The workbook was not written: {ui.md_escape(ui.clip(ui.scrub(exc), 400))}")
            if issues:
                st.dataframe(pd.DataFrame(ui.issue_rows(issues)), hide_index=True)
            for problem in problems[:10]:
                st.text(problem)
            st.session_state.pop(ui.K_EXPORT, None)
        else:
            st.session_state[ui.K_EXPORT] = (key, outcome)

saved = st.session_state.get(ui.K_EXPORT)
if saved and saved[0] == key:
    outcome = saved[1]
    if outcome.note and api.export_assessment is not None:  # otherwise the note is already shown above
        st.info(outcome.note)
    if outcome.ok:
        st.success("Integrity check passed: only the student cells of the vendor rows changed, and sheets were "
                   "appended after the original ones." + (" The worked example row is unchanged."
                                                          if outcome.example_unchanged else ""))
    else:
        st.error("Integrity check failed:\n\n" + "\n".join(f"- {ui.md_escape(p)}" for p in outcome.fidelity[:10]))
        if outcome.example_unchanged is False:
            st.error("The worked example row changed.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Cells written", len(outcome.written))
    c2.metric("Sheets appended", len(outcome.sheets_added))
    c3.metric("Workbook", "L-V" if outcome.kind == "full" else "L-N")
    st.caption("Sheets: " + " · ".join(outcome.sheets))
    for warning in outcome.warnings:
        st.warning(ui.md_escape(warning))
    st.download_button("Download the assessed workbook", data=outcome.data, file_name=outcome.file_name,
                       mime=ui.XLSX_MIME, type="primary", icon=":material/download:", key="fp_export_download",
                       disabled=not outcome.ok)
    st.caption("The file stays on this machine. Do not publish the workbook or its findings.")
