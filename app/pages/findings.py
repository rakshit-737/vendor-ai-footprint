"""Findings & Risk: the why trace from evidence to verdict to risk class, the E/K/gap override editors (each with a
reason), the live class after every change, and the cells L-V that the export will write."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import ui
from footprint.models import GAP_KEYS


def apply_records(record: ui.RunRecord, findings, records, stores: ui.ReviewStores, what: str) -> None:
    """Append the override records, rescore the vendor and report the live class on the next run."""
    for rec in records:
        ui.store_review(stores, rec)
    try:
        updated = ui.rescore(record, findings.vendor_id, stores, team=ui.session_team())
    except Exception as exc:  # noqa: BLE001 - the records are written; the rescore failure is shown
        ui.flash(f"{what} recorded, but the rescore failed: {ui.md_escape(ui.clip(ui.scrub(exc), 200))}", "warning")
    else:
        st.session_state[ui.K_PREV_CLASS][findings.vendor_id] = findings.risk.final_class
        change = ui.class_change(findings.risk, updated.risk)
        ui.flash(f"{what} recorded for {findings.vendor_id}. Live class: {change}")
    st.rerun()


def value_editor(record: ui.RunRecord, findings, stores: ui.ReviewStores, key: str, title: str) -> None:
    inputs = findings.risk.inputs
    current = inputs.e if key == "e" else inputs.k
    assumed = inputs.e_assumed if key == "e" else inputs.k_assumed
    citable = [i for i in findings.evidence if i.citable]
    with st.form(f"fp_override_{key}_{findings.vendor_id}"):
        st.markdown(f"**{title}** · now {current}/3 ({'assumed' if assumed else 'evidenced'})")
        value = st.selectbox("New value", [0, 1, 2, 3], index=current, key=f"fp_ov_{key}_value_{findings.vendor_id}")
        evidence = st.multiselect("Evidence (makes the value evidenced, not assumed)",
                                  [i.item_key for i in citable], key=f"fp_ov_{key}_ev_{findings.vendor_id}",
                                  format_func=lambda k: ui.item_option_label(findings.item(k)))
        reason = st.text_input("Reason", key=f"fp_ov_{key}_reason_{findings.vendor_id}",
                               placeholder=f"At least {ui.MIN_REASON} characters")
        submitted = st.form_submit_button(f"Record {key.upper()}", type="primary")
    if submitted:
        if value == current and not evidence:
            st.info(f"{key.upper()} is already {current}; nothing was recorded.")
            return
        try:
            rec = ui.risk_override_record(findings.vendor_id, key, str(value), reason, ui.session_analyst(),
                                          evidence=evidence)
        except ValueError as exc:
            st.error(ui.scrub(exc))
            return
        apply_records(record, findings, [rec], stores, f"{key.upper()} = {value}")


def gap_editor(record: ui.RunRecord, findings, stores: ui.ReviewStores) -> None:
    current = ui.gap_values(findings)
    with st.form(f"fp_override_gaps_{findings.vendor_id}"):
        st.markdown("**Transparency checks** · a missing check adds to TG")
        values = {}
        cols = st.columns(3)
        for n, gap in enumerate(GAP_KEYS):
            values[gap] = cols[n % 3].selectbox(f"{gap} · {ui.GAP_LABELS[gap]}", ["missing", "closed"],
                                                index=0 if current[gap] == "missing" else 1,
                                                key=f"fp_ov_{gap}_{findings.vendor_id}")
        reason = st.text_input("Reason", key=f"fp_ov_gaps_reason_{findings.vendor_id}",
                               placeholder=f"At least {ui.MIN_REASON} characters")
        submitted = st.form_submit_button("Record checks", type="primary")
    if submitted:
        try:
            records = ui.changed_gap_records(findings, values, reason, ui.session_analyst())
        except ValueError as exc:
            st.error(ui.scrub(exc))
            return
        if not records:
            st.info("No check changed; nothing was recorded.")
            return
        apply_records(record, findings, records, stores, ", ".join(f"{r.key} {r.value}" for r in records))


def approve_panel(record: ui.RunRecord, findings, stores: ui.ReviewStores) -> None:
    pending = ui.cited_unreviewed(findings)
    if not pending:
        st.caption("Every cited item has an analyst decision.")
        return
    st.caption(f"{len(pending)} cited item(s) have no analyst decision yet: "
               + ", ".join(ui.item_ref(i) for i in pending) + ". Review single items on the Evidence page.")
    with st.form(f"fp_approve_{findings.vendor_id}"):
        code = st.selectbox("Reason code", list(ui.ACCEPT_CODES), key=f"fp_approve_code_{findings.vendor_id}",
                            format_func=lambda c: f"{c}: {ui.ACCEPT_CODES[c]}")
        confirm = st.checkbox("I have read each cited excerpt in its source context.",
                              key=f"fp_approve_confirm_{findings.vendor_id}")
        submitted = st.form_submit_button(f"Accept the {len(pending)} cited item(s)", type="primary")
    if submitted:
        if not confirm:
            st.error("Confirm that you have read the cited excerpts first.")
            return
        try:
            records = [ui.evidence_review_record(findings.vendor_id, i.item_key, "accepted", code, "",
                                                 ui.session_analyst()) for i in pending]
        except ValueError as exc:
            st.error(ui.scrub(exc))
            return
        apply_records(record, findings, records, stores, f"{len(records)} acceptance(s)")


# --------------------------------------------------------------------------- page

ui.page_setup("Findings & Risk")
st.title("Findings & Risk")
st.caption("Column O (AI usage) follows the first matching verdict rule; columns S-U follow the ordered risk "
           "steps: score, base class, materiality gate, escalator floors, then the verdict cap.")
ui.show_flash()
record = ui.require_full_run()
stores = ui.session_stores()
vendor_id = ui.vendor_picker(record, key="fp_fr_vendor")
findings = record.findings(vendor_id)
assert findings is not None
v, r = findings.verdict, findings.risk

m1, m2, m3, m4 = st.columns(4)
m1.metric("Criticality tier", findings.criticality.tier.value)
m2.metric("AI usage (column O)", v.column_o, help=f"{v.label}: rule {v.rule})")
previous = st.session_state[ui.K_PREV_CLASS].get(vendor_id)
m3.metric("AI security risk (live)", ui.risk_label(r),
          delta=f"was {previous}" if previous and previous != r.final_class else None, delta_color="off")
m4.metric("Score", f"{r.arp} / 18", help="ARP = 2E + 2K + TP + TG; exposure and decision impact count double")
st.caption(ui.score_sentence(r))
st.markdown(f"It is **{v.likelihood}** that the vendor uses AI in the service ({v.label}). Confidence is "
            f"**{v.confidence}** because {ui.md_escape(v.confidence_reason)}.")
if r.ceiling_class:
    st.markdown(f"Ceiling if confirmed: **{r.ceiling_class}**.")
if r.flip_condition:
    st.info(f"Flip condition: {ui.md_escape(r.flip_condition)}")

st.subheader("Why", divider="gray")
st.dataframe(pd.DataFrame(ui.why_trace(findings)), hide_index=True)
decisive = [findings.item(k) for k in v.decisive]
if decisive:
    st.markdown("**Decisive evidence**")
    for item in decisive:
        if item is not None:
            st.markdown(f"- **{ui.item_ref(item)}** {ui.md_escape(item.role)} · {ui.md_escape(item.strength)} · "
                        f"{ui.md_escape(item.source_type)}, {ui.md_escape(item.publisher)}: "
                        f"“{ui.md_escape(ui.clip(item.excerpt, 260))}”")

st.subheader("Risk inputs", divider="gray")
st.dataframe(pd.DataFrame(ui.risk_input_rows(findings)), hide_index=True)
st.dataframe(pd.DataFrame(ui.gap_rows(findings)), hide_index=True)
facts = [f"gate {'met' if r.gate_met else 'not met'}"]
if r.escalators_fired:
    facts.append("escalators applied: " + ", ".join(r.escalators_fired))
if r.escalators_logged_not_applied:
    facts.append("escalators logged, not applied: " + ", ".join(r.escalators_logged_not_applied))
if r.themes:
    facts.append("risk themes: " + ", ".join(r.themes))
st.caption("; ".join(facts) + ".")

st.subheader("Override the risk inputs", divider="gray")
st.caption(f"Each change needs a reason and is recorded in the {stores.label} as a risk input override; the class "
           "is recomputed at once.")
left, right = st.columns(2)
with left:
    value_editor(record, findings, stores, "e", "E · exposure of Meridian data to AI")
with right:
    value_editor(record, findings, stores, "k", "K · decision impact")
gap_editor(record, findings, stores)

st.subheader("Approve", divider="gray")
approve_panel(record, findings, stores)

st.subheader("Cells L-V", divider="gray")
inp, data = ui.require_input()
st.dataframe(pd.DataFrame(ui.cells_rows(findings.cells, data.column_map)), hide_index=True)
if findings.actions.text:
    with st.expander("Recommended action (column U)"):
        st.markdown(ui.md_escape(findings.actions.text))
if findings.notes:
    with st.expander("Run notes"):
        for note in findings.notes:
            st.text(note)
example = record.result.example if record.result is not None else None
if example is not None:
    with st.expander(f"Calibration: {example.vendor_id} worked example (never written)"):
        er = example.risk
        st.markdown(f"{example.verdict.label} · score {er.arp}/18 · {ui.risk_label(er)} "
                    f"(gate {'met' if er.gate_met else 'not met'}).")
        st.dataframe(pd.DataFrame(ui.why_trace(example)), hide_index=True)
