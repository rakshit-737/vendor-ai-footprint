"""Evidence: filter the evidence items, read each excerpt in its source context, compare the rule and Gemini labels,
re-verify it against the evidence store, and accept or reject it with a reason code (HC2)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import ui
from footprint.models import STRENGTH_ORDER


def item_details(findings, item) -> None:
    head, role = st.columns([5, 2], vertical_alignment="center")
    head.markdown(f"#### {ui.item_ref(item)} · {ui.md_escape(item.strength)}")
    head.caption(f"Counts toward the verdict: {ui.counts_label(item)}")
    with role:
        st.badge(item.role, color="violet" if item.role == "Primary" else "gray")
        st.badge(ui.REVIEW_LABELS.get(item.review_status, item.review_status),
                 color={"accepted": "green", "rejected": "red"}.get(item.review_status, "gray"))
    st.code(item.tag_string, language=None, wrap_lines=True)
    meta = pd.DataFrame([
        {"Field": "Source", "Value": f"{item.source_type} · {item.publisher}"},
        {"Field": "Title", "Value": item.title or "-"},
        {"Field": "URL", "Value": item.url},
        {"Field": "Retrieved from", "Value": item.url_final if item.url_final != item.url else "same as URL"},
        {"Field": "Published", "Value": f"{item.published or 'undated'} ({item.date_basis or 'no date basis'})"},
        {"Field": "Retrieved (UTC)", "Value": item.retrieved_at},
        {"Field": "Method", "Value": ui.METHOD_LABELS.get(item.method, item.method)
         + (f" · {item.llm_model}" if item.llm_model else "")},
        {"Field": "Providers", "Value": ", ".join(item.providers) or "-"},
        {"Field": "Offsets", "Value": f"{item.start}-{item.end}"},
        {"Field": "Excerpt SHA-256", "Value": item.excerpt_sha256},
        {"Field": "Capture SHA-256", "Value": item.capture_sha256},
        {"Field": "Text SHA-256", "Value": item.text_sha256},
    ])
    st.dataframe(meta, hide_index=True)

    st.markdown("**Excerpt in context**")
    ctx = ui.load_context(item)
    if ctx is None:
        st.warning("The source text is not in the local evidence store; showing the stored excerpt only.")
        st.html(ui.context_html(ui.excerpt_context(item.excerpt, 0, len(item.excerpt), item.excerpt)))
    else:
        if not ctx.matches:
            st.error("The stored text no longer holds this excerpt at its offsets. Re-verify it.")
        st.html(ui.context_html(ctx))

    shot = ui.screenshot_file(item)
    if shot is not None:
        visible = {True: "excerpt visible in the render", False: "excerpt not found in the render",
                   None: "visibility not checked"}[item.visible_in_render]
        st.image(str(shot), caption=f"Screenshot · SHA-256 {item.screenshot_sha256[:16]}… · {visible}")
    elif item.screenshot_path:
        st.caption(f"Screenshot recorded at {item.screenshot_path}, but the file is not in this checkout.")
    else:
        st.caption("No screenshot for this item.")

    if st.button("Re-verify against the evidence store", key=f"fp_reverify_{item.item_key[:16]}",
                 icon=":material/verified:"):
        outcome = ui.reverify(item)
        if outcome is None:
            st.info("Re-verification needs footprint.verify, which is not available in this build.")
        elif outcome[0]:
            st.success("Verified: capture, text and excerpt hashes match, and the excerpt is the exact source slice.")
        else:
            st.error("Verification failed:\n\n" + "\n".join(f"- {ui.md_escape(d)}" for d in outcome[1]))

    st.markdown("**Rules vs Gemini**")
    rows = ui.label_rows(item)
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True)
    else:
        st.caption("No labels recorded.")
    if item.label_disagreements:
        st.warning("Rules and Gemini disagree on " + ", ".join(item.label_disagreements)
                   + ". The rule values stand until an analyst adjudicates (below).")
    if item.review_status != "unreviewed":
        st.caption(f"Last decision: {item.review_status} by {ui.md_escape(item.reviewer or 'unknown')}: "
                   f"{ui.md_escape(item.review_reason)}")

    review_panel(findings, item)


def review_panel(findings, item) -> None:
    stores = ui.session_stores()
    st.markdown("**Analyst decision (HC2)**")
    st.caption(f"Recorded in the {stores.label}; the vendor's verdict, risk and cells are recomputed at once.")
    key = item.item_key[:16]
    decision = st.radio("Decision", ["accepted", "rejected"], horizontal=True, key=f"fp_dec_{key}",
                        format_func=lambda d: "Accept" if d == "accepted" else "Reject")
    codes = dict(ui.ACCEPT_CODES if decision == "accepted" else ui.REJECT_CODES)
    codes[ui.OTHER_CODE] = ui.OTHER_WORDING
    code = st.selectbox("Reason code", list(codes), key=f"fp_code_{key}_{decision}",
                        format_func=lambda c: f"{c}: {codes[c]}")
    note = st.text_input("Note (optional; required for OTHER)", key=f"fp_note_{key}")
    labels: dict[str, str] = {}
    if decision == "accepted" and item.label_disagreements:
        st.caption("Adjudicate the disagreeing labels (the rule value is kept unless you pick Gemini's):")
        for label in item.label_disagreements:
            rule, llm = item.rule_labels[label], item.llm_labels[label]
            pick = st.radio(label, [rule, llm], horizontal=True, key=f"fp_adj_{key}_{label}",
                            format_func=lambda v, r=rule: f"{v} ({'rules' if v == r else 'Gemini'})")
            if pick != rule:
                labels[label] = pick
    if st.button("Record decision", type="primary", key=f"fp_record_{key}", icon=":material/gavel:"):
        record = ui.active_run()
        try:
            rec = ui.evidence_review_record(findings.vendor_id, item.item_key, decision, code, note,
                                            ui.session_analyst(), labels=labels or None)
            ui.store_review(stores, rec)
        except ValueError as exc:
            st.error(ui.scrub(exc))
            return
        message = f"{ui.item_ref(item)} {decision} ({code})."
        if record is None:
            ui.flash(f"{message} Run the assessment again to apply it.", "info")
        else:
            try:
                updated = ui.rescore(record, findings.vendor_id, stores, team=ui.session_team())
            except Exception as exc:  # noqa: BLE001 - the decision is recorded; the rescore failure is shown
                message += (" The decision is recorded, but the rescore failed: "
                            f"{ui.md_escape(ui.clip(ui.scrub(exc), 200))}")
                ui.flash(message, "warning")
            else:
                st.session_state[ui.K_PREV_CLASS][findings.vendor_id] = findings.risk.final_class
                ui.flash(f"{message} {findings.vendor_id}: {updated.verdict.column_o} ({updated.verdict.label}); "
                         f"{ui.class_change(findings.risk, updated.risk)}")
        st.rerun()


def dns_panel(record: ui.RunRecord) -> None:
    st.caption("Repeats the vendor's DNS TXT lookup live, through the two allowlisted DNS-over-HTTPS resolvers only "
               "(terms register, robots.txt and rate limit apply), and compares the AI-provider verification tokens "
               "with the captured result. Nothing is added to the evidence pack.")
    vendor_id = ui.vendor_picker(record, key="fp_dns_vendor")
    findings = record.findings(vendor_id)
    if st.button("Run live DNS check", key="fp_dns_run", icon=":material/dns:") and findings is not None:
        with st.spinner("Asking dns.google and cloudflare-dns.com…"):
            try:
                check = ui.dns_check(findings.profile, findings.depth)
            except Exception as exc:  # noqa: BLE001 - a network problem must not break the page
                st.error(f"The live DNS check failed: {ui.md_escape(ui.clip(ui.scrub(exc), 200))}")
                return
        captured = ", ".join(check.captured_providers) or "none"
        live = ", ".join(check.live_providers) or "none"
        (st.success if check.unchanged else st.warning)(
            f"{', '.join(check.domains)}: captured {ui.dmy(check.captured_at[:10]) or 'never'} → {captured}; "
            f"live now → {live}. " + ("Unchanged." if check.unchanged else "Changed since capture."))
        with st.expander("Lookup notes"):
            for note in check.notes:
                st.text(note)


# --------------------------------------------------------------------------- page

ui.page_setup("Evidence")
st.title("Evidence")
st.caption("Every item is an exact slice of a captured, hashed source. Rules set the final tags; Gemini only "
           "proposes. Only accepted or unreviewed items that are not traps or pending proposals count.")
ui.show_flash()
record = ui.require_full_run()
assert record.result is not None
findings_list = record.result.vendors

with st.container(border=True):
    c1, c2, c3, c4 = st.columns(4)
    ids = [f.vendor_id for f in findings_list]
    vendors = c1.multiselect("Vendor", ids, key=ui.keep("fp_ev_vendors", [], ids, multi=True),
                             format_func=lambda v: f"{v} · {ui.short_name(record.findings(v).profile.name)}")
    options = ui.present_values(findings_list, "family", list(ui.FAMILY_NAMES))
    families = c2.multiselect("Family", options, key=ui.keep("fp_ev_families", [], options, multi=True))
    options = ui.present_values(findings_list, "strength", STRENGTH_ORDER)
    strengths = c3.multiselect("Strength", options, key=ui.keep("fp_ev_strengths", [], options, multi=True))
    options = ui.present_values(findings_list, "method", list(ui.METHOD_LABELS))
    methods = c4.multiselect("Method", options, key=ui.keep("fp_ev_methods", [], options, multi=True),
                             format_func=lambda m: ui.METHOD_LABELS.get(m, m))
    c5, c6, c7 = st.columns([2, 2, 1], vertical_alignment="bottom")
    statuses = c5.multiselect("Review", list(ui.REVIEW_LABELS), format_func=ui.REVIEW_LABELS.get,
                              key=ui.keep("fp_ev_statuses", [], list(ui.REVIEW_LABELS), multi=True))
    text = c6.text_input("Search excerpt, title or URL", key=ui.keep("fp_ev_text", ""))
    cited_only = c7.toggle("Cited only", key=ui.keep("fp_ev_cited", False))
flt = ui.EvidenceFilter(vendors=tuple(vendors), families=tuple(families), strengths=tuple(strengths),
                        methods=tuple(methods), statuses=tuple(statuses), cited_only=cited_only, text=text)
pairs = ui.filter_items(findings_list, flt)
total = sum(len(f.evidence) for f in findings_list)
st.caption(f"{len(pairs)} of {total} items.")
if not pairs:
    st.info("No evidence item matches these filters.")
else:
    st.dataframe(pd.DataFrame(ui.evidence_rows(pairs)), hide_index=True, height=min(420, 38 + 35 * len(pairs)))
    by_key = {item.item_key: (f, item) for f, item in pairs}
    selected = st.selectbox("Item", list(by_key), key=ui.keep("fp_ev_item", next(iter(by_key)), list(by_key)),
                            format_func=lambda k: ui.item_option_label(by_key[k][1]))
    with st.container(border=True):
        item_details(*by_key[selected])

with st.expander("Live DNS check (allowlisted resolvers)"):
    dns_panel(record)
