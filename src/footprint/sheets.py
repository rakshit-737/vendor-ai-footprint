"""Extra worksheets appended to the output workbook: Criticality Workings and Method & Legend.

Both are plain SheetSpecs built from the scoring results and the policy files, so a reviewer can rebuild every
tier by hand. Later phases append their own Method & Legend sections (tag legend, verdict rules, risk matrix)
with add_section().
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from footprint.criticality import FACTORS, Rubric, factor_label, floor_description, rubric_table
from footprint.depth import DepthConfig, depth_table, excluded_sources
from footprint.models import CoverageEntry, CriticalityResult, SheetSpec, Tier, VendorProfile

WORKINGS_TITLE = "Criticality Workings"
METHOD_LEGEND_TITLE = "Method & Legend"
EXAMPLE_LABEL = "V-000 (calibration only, not written to the inventory)"

WORKINGS_HEADERS: list[str] = [
    "Vendor ID", "Vendor", "Factor", "Level (0-4)", "Anchor", "Trigger phrase", "Weight", "Points",
    "Score", "Score tier", "Floors fired", "Final tier", "Override", "Sensitivity", "Perturbation stable",
    "Rubric version",
]
WORKINGS_WIDTHS: list[float] = [14, 24, 24, 10, 10, 50, 8, 8, 8, 10, 50, 10, 40, 60, 12, 10]
LEGEND_HEADERS: list[str] = ["Section", "Item", "Detail"]
LEGEND_WIDTHS: list[float] = [24, 30, 100]

Scored = tuple[VendorProfile, CriticalityResult]
Cell = str | int | float | None


# --------------------------------------------------------------------------- Criticality Workings


def criticality_workings_sheet(items: Sequence[Scored], example: Scored | None = None,
                               rubric: Rubric | None = None) -> SheetSpec:
    """One row per vendor x factor, then one summary row per vendor; the V-000 calibration block comes first."""
    rows: list[list[Cell]] = []
    if example is not None:
        rows += _vendor_rows(*example, rubric, label=EXAMPLE_LABEL)
    for profile, result in items:
        rows += _vendor_rows(profile, result, rubric)
    return SheetSpec(
        title=WORKINGS_TITLE,
        headers=list(WORKINGS_HEADERS),
        rows=rows,
        column_widths=list(WORKINGS_WIDTHS),
        note="Score = sum of weight x level per factor (0-100); final tier = max(score tier, floors), "
             "or the analyst override (HC1).",
    )


def _vendor_rows(profile: VendorProfile, result: CriticalityResult, rubric: Rubric | None,
                 label: str | None = None) -> list[list[Cell]]:
    vendor_id = label or profile.vendor_id
    blank: list[Cell] = [None] * 8
    rows: list[list[Cell]] = []
    for code in FACTORS:
        fs = result.factors[code]
        name = rubric.factors[code].name if rubric else _factor_name(code)
        trigger = fs.trigger or (f"(none - {fs.note})" if fs.note else "")
        rows.append([vendor_id, profile.name, name, fs.level, fs.anchor, trigger, fs.weight, fs.points] + blank)
    rows.append([
        vendor_id, profile.name, "Summary", None, None, None, None, None,
        result.score, result.score_tier.value, _floors_text(result.floors_fired, rubric), result.tier.value,
        _override_text(result), " ".join(result.sensitivity), "yes" if result.perturbation_stable else "no",
        result.rubric_version,
    ])
    return rows


def _factor_name(code: str) -> str:
    return factor_label(code)  # default rubric


def _floors_text(floor_ids: Iterable[str], rubric: Rubric | None) -> str:
    parts = [f"{fid}: {rubric.floor(fid).description if rubric else floor_description(fid)}" for fid in floor_ids]
    return "; ".join(parts) or "none"


def _override_text(result: CriticalityResult) -> str:
    o = result.override
    if o is None:
        return ""
    return f"{result.computed_tier.value} -> {o.tier.value} by {o.analyst} on {o.date}: {o.reason}"


# --------------------------------------------------------------------------- Method & Legend


def add_section(spec: SheetSpec, section: str, items: Iterable[tuple[str, str]]) -> SheetSpec:
    """Append (item, detail) pairs as rows of a named section; returns the same spec for chaining."""
    spec.rows.extend([section, item, detail] for item, detail in items)
    return spec


def method_legend_sheet(rubric: Rubric, depth_config: DepthConfig) -> SheetSpec:
    """The method in words: rubric, floors, anchors, depth ladder, budgets, modifiers and exclusions."""
    spec = SheetSpec(title=METHOD_LEGEND_TITLE, headers=list(LEGEND_HEADERS), column_widths=list(LEGEND_WIDTHS),
                     note="How every tier and every depth decision in this workbook was reached.")
    formula = " + ".join(f"{w} x {code}" for code, w in rubric.weights.items())
    add_section(spec, "Rubric formula", [
        ("Score", f"score = {formula}; each factor is levelled 0-4, so the score runs 0-100"),
        ("Factors", "; ".join(f"{code} = {rubric.factors[code].name}" for code in FACTORS)),
        ("Final tier", "max(score tier, highest floor fired); an analyst override (HC1) replaces it, with a reason"),
        ("Rubric version", rubric.version),
    ])
    thresholds = [(t.value, f"score >= {rubric.score_tiers[t]}") for t in (Tier.CRITICAL, Tier.HIGH, Tier.MEDIUM)]
    add_section(spec, "Tier thresholds", thresholds + [(Tier.LOW.value, f"score < {rubric.score_tiers[Tier.MEDIUM]}")])
    add_section(spec, "Floors", [(f.id, f"{f.name} (at least {f.tier.value}): {f.description}") for f in rubric.floors])
    add_section(spec, "Factor anchors", [
        (f"{r['factor']} {r['anchor']} (level {r['level']})",
         f"{r['factor_name']}, weight {r['weight']}: {r['label']} - {r['description']}") for r in rubric_table(rubric)
    ])
    add_section(spec, "Depth ladder", [
        (f"{r['tier']} / {r['family']}",
         f"{'mandatory' if r['mandatory'] else 'optional'}, mode {r['mode']}, cap {r['cap']}: {r['reason']}")
        for r in depth_table(depth_config)
    ])
    by_rank = sorted(depth_config.tiers.items(), key=lambda kv: kv[0].rank, reverse=True)
    add_section(spec, "Budgets and stop rules", [
        (f"{tier.value} ({p.label})",
         f"{p.discretionary_fetches} discretionary fetches, {p.gemini_calls} Gemini calls, {p.analyst_minutes} "
         f"analyst minutes; stop after {p.saturation_window} actions without a new qualifying cluster; a family "
         "that reaches its cap ends as stopped") for tier, p in by_rank
    ])
    add_section(spec, "Depth modifiers", [(mid, f"{m.title}: {m.note}") for mid, m in depth_config.modifiers.items()])
    add_section(spec, "Reserved for Meridian", [
        (tier.value, "; ".join(p.reserved_for_meridian)) for tier, p in by_rank
    ])
    add_section(spec, "Excluded sources", excluded_sources(depth_config))
    return spec


COVERAGE_LOG_TITLE = "Coverage Log"
COVERAGE_HEADERS: list[str] = ["Vendor ID", "Family", "Mandatory", "Status", "Collector", "Endpoint / query",
                               "Requests used", "Cap", "Documents", "AI passages", "Note"]


FAMILY_SUMMARY_LABEL = "family (summary)"


def coverage_log_sheet(entries: Iterable[CoverageEntry], summary: Iterable[CoverageEntry] | None = None) -> SheetSpec:
    """Coverage Log: one row per (vendor, family, collector) search, negative evidence included.

    With ``summary`` (``pipeline.aggregate_coverage`` rows), each vendor's block starts with one row per family
    giving the family's single status (collector "family (summary)"), followed by the per-collector detail rows.
    """
    def row(e: CoverageEntry, collector: str) -> list[Cell]:
        return [e.vendor_id, e.family.value, "Yes" if e.mandatory else "No", e.status.value, collector, e.endpoint,
                e.requests_used, e.cap, e.documents, e.ai_passages, e.note]

    details = list(entries)
    rows: list[list[Cell]] = []
    if summary is None:
        rows = [row(e, e.collector) for e in details]
    else:
        summ = list(summary)
        vendors = list(dict.fromkeys([e.vendor_id for e in summ] + [e.vendor_id for e in details]))
        for vid in vendors:
            rows += [row(e, FAMILY_SUMMARY_LABEL) for e in summ if e.vendor_id == vid]
            rows += [row(e, e.collector) for e in details if e.vendor_id == vid]
    note = "What was searched, how, and the result; a done row with 0 AI passages is negative evidence."
    if summary is not None:
        note += (" Family rows give one status per family: done_manual > stopped > done when any search completed, "
                 "else pending > blocked_tou > blocked_robots > blocked_bot > error > descoped; not_applicable only "
                 "when every search was not applicable.")
    return SheetSpec(title=COVERAGE_LOG_TITLE, headers=COVERAGE_HEADERS, rows=rows,
                     column_widths=[10, 8, 10, 15, 12, 40, 9, 6, 9, 9, 60], note=note)
