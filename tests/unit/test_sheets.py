"""Tests for the Criticality Workings and Method & Legend sheets."""

from __future__ import annotations

from pathlib import Path

import pytest

from footprint.criticality import FACTORS, load_rubric, rubric_table, score_profile
from footprint.depth import depth_table, excluded_sources, load_depth_config
from footprint.models import CriticalityResult, SheetSpec, Tier, TierOverride, VendorProfile
from footprint.sheets import (
    EXAMPLE_LABEL,
    LEGEND_HEADERS,
    METHOD_LEGEND_TITLE,
    WORKINGS_HEADERS,
    WORKINGS_TITLE,
    add_section,
    criticality_workings_sheet,
    method_legend_sheet,
)
from footprint.workbook import read_workbook

INPUT = Path(__file__).resolve().parents[2] / "data" / "input" / "Meridian_Vendor_Input.xlsx"


@pytest.fixture(scope="module")
def scored() -> tuple[list[tuple[VendorProfile, CriticalityResult]], tuple[VendorProfile, CriticalityResult]]:
    data = read_workbook(INPUT)
    assert data.example is not None
    items = [(p, score_profile(p)) for p in data.vendors]
    return items, (data.example, score_profile(data.example))


def test_workings_title_and_headers(scored) -> None:
    items, example = scored
    spec = criticality_workings_sheet(items, example)
    assert isinstance(spec, SheetSpec)
    assert spec.title == WORKINGS_TITLE == "Criticality Workings"
    assert spec.headers == WORKINGS_HEADERS
    assert all(len(row) == len(spec.headers) for row in spec.rows)


def test_workings_row_count_and_calibration(scored) -> None:
    items, example = scored
    spec = criticality_workings_sheet(items, example)
    assert len(spec.rows) == (len(items) + 1) * (len(FACTORS) + 1)
    calibration = [row for row in spec.rows if row[0] == EXAMPLE_LABEL]
    assert len(calibration) == len(FACTORS) + 1
    assert EXAMPLE_LABEL == "V-000 (calibration only, not written to the inventory)"
    assert len(criticality_workings_sheet(items, None).rows) == len(items) * (len(FACTORS) + 1)


def test_workings_factor_and_summary_rows(scored) -> None:
    items, _ = scored
    profile, result = items[0]
    spec = criticality_workings_sheet([(profile, result)], None)
    col = {h: i for i, h in enumerate(spec.headers)}
    factor_rows, summary = spec.rows[:5], spec.rows[5]
    assert [r[col["Factor"]] for r in factor_rows][0] == "operational dependency"
    assert sum(int(r[col["Points"]]) for r in factor_rows) == result.score
    assert summary[col["Score"]] == result.score
    assert summary[col["Final tier"]] == result.tier.value
    assert summary[col["Rubric version"]] == result.rubric_version
    for fid in result.floors_fired:
        assert fid in str(summary[col["Floors fired"]])


def test_workings_shows_override(scored) -> None:
    items, _ = scored
    profile, _ = items[0]
    override = TierOverride(vendor_id=profile.vendor_id, tier=Tier.LOW, reason="analyst judgement for test",
                            analyst="Team", date="2026-10-02")
    spec = criticality_workings_sheet([(profile, score_profile(profile, override=override))], None)
    summary = spec.rows[-1]
    assert "Low" in str(summary[spec.headers.index("Override")])
    assert "analyst judgement for test" in str(summary[spec.headers.index("Override")])


def test_method_legend_sections() -> None:
    rubric, cfg = load_rubric(), load_depth_config()
    spec = method_legend_sheet(rubric, cfg)
    assert spec.title == METHOD_LEGEND_TITLE == "Method & Legend"
    assert spec.headers == LEGEND_HEADERS == ["Section", "Item", "Detail"]
    sections = {row[0] for row in spec.rows}
    for name in ("Rubric formula", "Tier thresholds", "Floors", "Factor anchors", "Depth ladder",
                 "Budgets and stop rules", "Depth modifiers", "Reserved for Meridian", "Excluded sources"):
        assert name in sections
    items = [row[1] for row in spec.rows if row[0] == "Floors"]
    assert items == ["F1", "F2", "F3", "F4", "F5", "F6"]
    assert sum(1 for r in spec.rows if r[0] == "Factor anchors") == len(rubric_table(rubric))
    assert sum(1 for r in spec.rows if r[0] == "Depth ladder") == len(depth_table(cfg))
    assert sum(1 for r in spec.rows if r[0] == "Excluded sources") == len(excluded_sources(cfg))
    assert all(len(row) == 3 for row in spec.rows)


def test_add_section_appends() -> None:
    spec = method_legend_sheet(load_rubric(), load_depth_config())
    before = len(spec.rows)
    out = add_section(spec, "Tag legend", [("T1", "first"), ("T2", "second")])
    assert out is spec
    assert spec.rows[before:] == [["Tag legend", "T1", "first"], ["Tag legend", "T2", "second"]]


def test_coverage_log_with_family_summary_rows() -> None:
    from footprint.models import CoverageEntry, CoverageStatus, SourceFamily
    from footprint.pipeline import aggregate_coverage
    from footprint.sheets import FAMILY_SUMMARY_LABEL, coverage_log_sheet

    rows = [CoverageEntry(vendor_id=v, family=SourceFamily.DNS, mandatory=True, status=s, collector="dns",
                          requests_used=6) for v, s in (("V-005", CoverageStatus.DONE), ("V-005", CoverageStatus.ERROR),
                                                        ("V-006", CoverageStatus.DONE))]
    plain = coverage_log_sheet(rows)
    assert len(plain.rows) == 3 and [r[3] for r in plain.rows] == ["done", "error", "done"]
    spec = coverage_log_sheet(rows, summary=aggregate_coverage(rows))
    assert [(r[0], r[4], r[3]) for r in spec.rows] == [
        ("V-005", FAMILY_SUMMARY_LABEL, "done"), ("V-005", "dns", "done"), ("V-005", "dns", "error"),
        ("V-006", FAMILY_SUMMARY_LABEL, "done"), ("V-006", "dns", "done")]
    assert "done_manual > stopped > done" in spec.note
