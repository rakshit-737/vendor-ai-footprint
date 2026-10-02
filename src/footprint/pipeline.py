"""End-to-end pipeline (P1 subset): score each vendor, plan its depth, and export columns L-N.

Later phases extend VendorAssessment (evidence, verdicts, risk) and build_cells (columns O-V).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from footprint.criticality import Rubric, load_rubric, render_rationale, score_profile
from footprint.depth import DepthConfig, load_depth_config, plan_depth, render_depth_cell
from footprint.models import CriticalityResult, DepthPlan, StudentCells, TierOverride, VendorProfile, WorkbookData
from footprint.review import OverrideStore
from footprint.sheets import criticality_workings_sheet, method_legend_sheet
from footprint.workbook import WorkbookSource, WriteReport, check_fidelity, write_workbook


class VendorAssessment(BaseModel):
    """Everything known about one vendor so far."""

    profile: VendorProfile
    criticality: CriticalityResult
    depth: DepthPlan


class FidelityError(RuntimeError):
    """The exported workbook changed something outside the student cells and appended sheets."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("output workbook failed the fidelity check: " + "; ".join(problems[:5]))
        self.problems = problems


def assess_profile(profile: VendorProfile, override: TierOverride | None = None, *, rubric: Rubric | None = None,
                   depth_config: DepthConfig | None = None) -> VendorAssessment:
    """Score one vendor (applying an HC1 override, if any) and plan its depth from the final tier.

    rubric and depth_config default to config/rubric.toml and config/depth.toml.
    """
    result = score_profile(profile, rubric, override=override)
    return VendorAssessment(profile=profile, criticality=result,
                            depth=plan_depth(result, profile, config=depth_config))


def assess_profiles(workbook: WorkbookData, overrides: OverrideStore | None = None, *, rubric: Rubric | None = None,
                    depth_config: DepthConfig | None = None) -> list[VendorAssessment]:
    """Assess every real vendor in sheet order; the example row is left out (see assess_example)."""
    return [
        assess_profile(p, overrides.tier_override(p.vendor_id) if overrides else None, rubric=rubric,
                       depth_config=depth_config)
        for p in workbook.vendors
    ]


def assess_example(workbook: WorkbookData, *, rubric: Rubric | None = None,
                   depth_config: DepthConfig | None = None) -> VendorAssessment | None:
    """The V-000 worked example, assessed for calibration only (never written to the inventory)."""
    if workbook.example is None:
        return None
    return assess_profile(workbook.example, rubric=rubric, depth_config=depth_config)


def build_cells(assessment: VendorAssessment, *, rubric: Rubric | None = None,
                depth_config: DepthConfig | None = None) -> StudentCells:
    """Columns L-N for one vendor; the other student columns stay untouched in P1."""
    return StudentCells(
        criticality_tier=assessment.criticality.tier.value,
        criticality_rationale=render_rationale(assessment.profile, assessment.criticality, rubric),
        assessment_depth=render_depth_cell(assessment.depth, config=depth_config),
    )


def export_workbook(
    source: WorkbookSource,
    destination: str | Path,
    assessments: list[VendorAssessment],
    *,
    example: VendorAssessment | None = None,
    team: str | None = None,
    overwrite: bool = False,
    rubric: Rubric | None = None,
    depth_config: DepthConfig | None = None,
) -> WriteReport:
    """Write L-N plus the Criticality Workings and Method & Legend sheets, then prove nothing else changed.

    rubric and depth_config must be the ones the assessments were made with (default: the config files); a
    result scored under another rubric version is refused, so the sheets never describe a different rubric.
    """
    rubric = rubric or load_rubric()
    depth_config = depth_config or load_depth_config()
    scored = [a.criticality for a in assessments] + ([example.criticality] if example else [])
    stale = sorted({r.vendor_id for r in scored if r.rubric_version != rubric.version})
    if stale:
        raise ValueError(f"{', '.join(stale)} were scored with another rubric version than v{rubric.version}")
    cells = {a.profile.vendor_id: build_cells(a, rubric=rubric, depth_config=depth_config) for a in assessments}
    sheets = [
        criticality_workings_sheet(
            [(a.profile, a.criticality) for a in assessments],
            (example.profile, example.criticality) if example else None,
            rubric=rubric,
        ),
        method_legend_sheet(rubric, depth_config),
    ]
    report = write_workbook(source, destination, cells, sheets, overwrite=overwrite, last_modified_by=team)
    problems = check_fidelity(source, destination)
    if problems:
        raise FidelityError(problems)
    return report
