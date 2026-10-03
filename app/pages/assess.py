"""Assess (home): load the vendor workbook, check it, rate criticality (HC1), plan the depth, run the assessment."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import ui


def criticality_card(a, stores: ui.ReviewStores) -> None:
    """One vendor: score, factor levels with their trigger phrases, floors, sensitivity and the HC1 form."""
    c, p = a.criticality, a.profile
    head, tier = st.columns([5, 1], vertical_alignment="center")
    head.markdown(f"**{ui.md_escape(p.name)}**")
    if p.service:
        head.caption(ui.md_escape(ui.clip(p.service, 220)))
    with tier:
        ui.badge(c.tier.value)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Score", f"{c.score} / 100")
    m2.metric("Tier by score", c.score_tier.value)
    m3.metric("Floors fired", ", ".join(c.floors_fired) or "none")
    m4.metric("Final tier", c.tier.value, help="The higher of the score tier and every floor, or the HC1 override.")
    st.dataframe(pd.DataFrame(ui.factor_rows(c)), hide_index=True)
    floors = ui.floor_rows(c)
    if floors:
        st.markdown("**Floors**\n\n" + "\n".join(
            f"- **{r['Floor']}** {ui.md_escape(r['Name'])} (at least {r['At least']}): {ui.md_escape(r['Rule'])}"
            for r in floors))
    if c.sensitivity:
        st.markdown("**Sensitivity**\n\n" + "\n".join(f"- {ui.md_escape(s)}" for s in c.sensitivity))
    st.caption("Tier unchanged under every ±1 weight change." if c.perturbation_stable
               else "The tier moves under at least one ±1 weight change.")
    if c.override is not None:
        o = c.override
        st.info(f"HC1 override in force: computed {c.computed_tier.value}, now {o.tier.value} "
                f"({ui.md_escape(o.analyst)}, {ui.dmy(o.date)}): {ui.md_escape(o.reason)}")
    with st.expander("HC1 · confirm or override this tier"):
        hc1_form(a, stores)
    with st.expander("Column M text"):
        st.text(ui.rationale(p, c))


def hc1_form(a, stores: ui.ReviewStores) -> None:
    c, vid = a.criticality, a.profile.vendor_id
    st.caption(f"Computed tier {c.computed_tier.value}. Confirming it needs no record. An override needs a reason "
               f"of {ui.MIN_REASON} or more characters and is appended to the {stores.label}.")
    with st.form(f"fp_hc1_{vid}"):
        tier = st.selectbox("Tier", ui.TIER_NAMES, index=ui.TIER_NAMES.index(c.tier.value), key=f"fp_hc1_tier_{vid}")
        reason = st.text_area("Reason", key=f"fp_hc1_reason_{vid}", height=80,
                              placeholder="Why the computed tier does not fit, e.g. what the business owner confirmed")
        submitted = st.form_submit_button("Record override", type="primary", key=f"fp_hc1_submit_{vid}")
    if submitted:
        if tier == c.tier.value:
            st.info(f"{tier} is already the tier in force; nothing was recorded.")
            return
        try:
            ui.record_tier_override(stores.overrides, vid, tier, reason, ui.session_analyst())
        except ValueError as exc:
            st.error(ui.scrub(exc))
            return
        ui.flash(f"{vid}: tier override to {tier} recorded in the {stores.label}. Run the assessment again to "
                 "apply it to the evidence depth.")
        st.rerun()


def start_run(inp: ui.InputFile, data, mode: str, stores: ui.ReviewStores, api: ui.PipelineApi,
              subset: list[str], as_of_text: str) -> None:
    """Run (or fall back to P1), cache the record under (input SHA-256, mode) and rerun to show it."""
    try:
        as_of = ui.parse_as_of(as_of_text)
    except ValueError as exc:
        st.error(ui.md_escape(exc))
        return
    bar = st.progress(0.0, text="Starting")

    def progress(message: str, fraction: float) -> None:
        bar.progress(min(1.0, max(0.0, float(fraction))), text=ui.clip(message, 120))

    with st.spinner("Assessing…"):
        record = ui.execute_run(inp, data, mode, stores, team=ui.session_team(), vendors=subset or None,
                                progress=progress, api=api, as_of=as_of)
    bar.empty()
    ui.store_run(record)
    ui.flash(f"{ui.MODE_LABELS[mode]} run finished." if record.full else f"{ui.MODE_LABELS[mode]} run finished: "
             "criticality and depth only.", "success" if record.full and not record.error else "info")
    st.rerun()


def run_summary(record: ui.RunRecord, stores: ui.ReviewStores, data) -> None:
    if record.error:
        st.error(ui.md_escape(record.error))
        if record.issues:
            st.dataframe(pd.DataFrame(ui.issue_rows(record.issues)), hide_index=True)
    for note in record.notes:
        st.info(note)
    if ui.is_stale(record, stores, data):
        st.warning("A tier override or the review store changed since this run. Run it again to apply the change.")
    if record.full and record.result is not None:
        r = record.result
        st.success(f"Run {r.run_id} · {ui.MODE_LABELS[record.mode]} · as of {ui.dmy(r.as_of)} · "
                   f"{len(r.vendors)} vendors assessed.")
    st.dataframe(pd.DataFrame(ui.run_summary_rows(record)), hide_index=True)
    if record.full:
        c1, c2, c3 = st.columns(3)
        c1.page_link("pages/evidence.py", label="Review the evidence", icon=":material/find_in_page:")
        c2.page_link("pages/findings.py", label="Findings and risk", icon=":material/shield:")
        c3.page_link("pages/export.py", label="Export the workbook", icon=":material/download:")


# --------------------------------------------------------------------------- page

ui.page_setup("Assess")
st.title("Vendor AI footprint")
st.caption("Does each vendor genuinely use AI in the service it provides, and what AI security risk follows? "
           "Public footprint only: every finding traces to a captured, hashed source.")
ui.show_flash()

st.subheader("1. Vendor workbook", divider="gray")
left, right = st.columns([3, 2], vertical_alignment="bottom")
uploaded = left.file_uploader("Upload the vendor inventory (.xlsx)", type=["xlsx"], key="fp_upload")
use_bundled = right.button("Use the bundled input workbook", key="fp_use_bundled", icon=":material/inventory_2:",
                           disabled=not ui.BUNDLED_INPUT.exists(),
                           help="data/input/Meridian_Vendor_Input.xlsx from the repository")
ui.take_input(uploaded, use_bundled)
inp = ui.session_input()
if inp is None:
    st.info("Upload the vendor inventory, or use the bundled input workbook, to start.")
    st.stop()

data = ui.session_workbook(inp)
errors = [i for i in data.issues if i.severity == "error"]
warnings = [i for i in data.issues if i.severity != "error"]
st.caption(f"{inp.name} · SHA-256 {inp.short_sha}…")
if errors:
    st.error(f"The workbook cannot be assessed: {len(errors)} error(s). Fix them and upload it again.")
elif warnings:
    st.warning(f"The workbook can be assessed, with {len(warnings)} warning(s).")
else:
    example = " The worked example V-000 is used for calibration only." if data.example else ""
    st.success(f'Workbook checked: sheet "{data.sheet_name}", headers in row {data.header_row}, '
               f"{len(data.vendors)} vendors.{example}")
if data.issues:
    st.dataframe(pd.DataFrame(ui.issue_rows(data.issues)), hide_index=True)
if errors:
    st.stop()
if not data.vendors:
    st.warning("The workbook has no vendor rows to assess.")
    st.stop()

stores = ui.session_stores()
try:
    assessments, example = ui.session_assessments(inp, data, stores)
except ValueError as exc:
    st.error(f"The review store could not be read: {ui.md_escape(ui.scrub(exc))}")
    st.stop()

st.subheader("2. Criticality", divider="gray")
st.caption("Rubric on the profile fields only (columns B-K): score = 7·O + 6·D + 5·P + 4·R + 3·V out of 100; "
           "floors can raise the tier. Public evidence never changes it. Analysts confirm or override it (HC1).")
st.dataframe(pd.DataFrame(ui.tier_summary_rows(assessments)), hide_index=True)
tabs = st.tabs([f"{a.profile.vendor_id} · {ui.short_name(a.profile.name)}" for a in assessments])
for tab, assessment in zip(tabs, assessments):
    with tab:
        criticality_card(assessment, stores)
if example is not None:
    with st.expander(f"Calibration: {example.profile.vendor_id} worked example (never written)"):
        given = ui.session_example_cells(inp, data).get("criticality_tier", "")
        computed = example.criticality.tier.value
        verdict = "matches" if given == computed else "differs from"
        st.markdown(f"The rubric gives the worked example **{computed}** (score {example.criticality.score}), "
                    f"which {verdict} the example's own tier ({given or 'blank'}).")
        st.dataframe(pd.DataFrame(ui.factor_rows(example.criticality)), hide_index=True)

st.subheader("3. Assessment depth", divider="gray")
st.caption("The tier sets how deep the public-footprint review goes; modifiers raise single families where the "
           "profile warrants it. Non-OSINT steps stay reserved for Meridian.")
st.dataframe(pd.DataFrame(ui.depth_rows(assessments)), hide_index=True)
by_id = {a.profile.vendor_id: a for a in assessments}
chosen = st.selectbox("Depth plan for", list(by_id),
                      key=ui.keep("fp_depth_vendor", next(iter(by_id)), list(by_id)),
                      format_func=lambda v: f"{v} · {by_id[v].profile.name}")
plan = by_id[chosen].depth
st.markdown(f"**{ui.md_escape(plan.label)}** · fetch budget {plan.discretionary_fetches} · Gemini calls "
            f"{plan.gemini_calls} · analyst time {plan.analyst_minutes} min · saturation window "
            f"{plan.saturation_window}")
st.dataframe(pd.DataFrame(ui.family_rows(plan)), hide_index=True)
for line in ui.modifier_lines(plan):
    st.markdown(f"- {ui.md_escape(line)}")
st.caption("Reserved for Meridian (non-OSINT): " + ", ".join(plan.reserved_for_meridian))
coverage = ui.coverage_for(chosen, ui.active_run())
if coverage:
    with st.expander(f"Coverage Log ({len(coverage)} searches)"):
        st.dataframe(pd.DataFrame(ui.coverage_rows(coverage)), hide_index=True)
with st.expander("Column N text"):
    st.text(ui.depth_cell(plan, coverage))

st.subheader("4. Run the assessment", divider="gray")
mode = st.radio("Mode", ui.MODES, format_func=ui.MODE_LABELS.get, captions=[ui.MODE_CAPTIONS[m] for m in ui.MODES],
                horizontal=True, key=ui.keep("fp_mode_widget", ui.session_mode(), ui.MODES))
ui.set_mode(mode)
api = ui.pipeline_api()
record = ui.active_run()
if not api.full and record is None:
    st.info(ui.P1_NOTE)
if mode != "replay":
    st.warning("Live modes make polite GET requests only, each through the terms register, robots.txt and a "
               "per-host rate limit; fiserv.com and LinkedIn stay manual. Live AI sends public vendor text only, "
               "through the payload guard. Evidence captured now is added to the evidence store.")
scope, when = st.columns([3, 1])
subset = scope.multiselect("Limit the run to (optional)", list(by_id),
                           key=ui.keep("fp_run_vendors", [], list(by_id), multi=True),
                           format_func=lambda v: f"{v} · {by_id[v].profile.name}",
                           placeholder="All vendors in the workbook")
as_of_text = when.text_input("As of (optional)", key=ui.keep("fp_as_of", ""), placeholder="YYYY-MM-DD",
                             help="The date every recency decision uses. Empty: replay takes the date of the frozen "
                                  "collection runs; live modes take today's date.")
if st.button("Run assessment", type="primary", icon=":material/play_arrow:", key="fp_run"):
    start_run(inp, data, mode, stores, api, subset, as_of_text)
if record is None:
    st.caption(f"No {ui.MODE_LABELS[mode]} run for this workbook yet.")
else:
    run_summary(record, stores, data)
