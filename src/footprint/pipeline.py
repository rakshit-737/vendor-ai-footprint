"""End-to-end pipeline (P1 subset): score each vendor, plan its depth, and export columns L-N.

Later phases extend VendorAssessment (evidence, verdicts, risk) and build_cells (columns O-V).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from footprint.criticality import Rubric, load_rubric, render_rationale, score_profile
from footprint.depth import DepthConfig, load_depth_config, plan_depth, render_depth_cell
from footprint.models import Capture, CoverageEntry, CoverageStatus, Document, Passage, SourceFamily
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


# =========================================================================== P2: collection

CollectMode = Literal["live_rules", "replay"]


class CollectionRun(BaseModel):
    """Everything one collection run produced for one vendor (written to runs/<run_id>/)."""

    run_id: str
    vendor_id: str
    mode: str
    as_of: str
    captures: list[Capture] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    passages: list[Passage] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    leads: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    run_dir: str = ""


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); existing environment variables win. Values are never printed."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_seeds(vendor_id: str, seeds_dir: str | Path = "seeds") -> dict:
    p = Path(seeds_dir) / f"{vendor_id}.toml"
    if not p.exists():
        return {}
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def default_collectors() -> list[Any]:
    from footprint.collectors.dns import DnsCollector
    from footprint.collectors.jobs import JobsCollector
    from footprint.collectors.sec import SecCollector
    from footprint.collectors.seeds import SeedsCollector
    from footprint.collectors.site import SiteCollector
    from footprint.collectors.wayback import WaybackCollector
    from footprint.collectors.wordpress import WordPressCollector

    return [SeedsCollector(), DnsCollector(), SiteCollector(), WordPressCollector(), SecCollector(),
            JobsCollector(), WaybackCollector()]


def order_collectors(collectors: list[Any], plan: DepthPlan) -> list[Any]:
    """Seeds first (others reuse its captures), then mandatory families, then the rest; Wayback (HIST) last."""

    def key(c: Any) -> int:
        if getattr(c, "seeded", False):
            return 0
        if c.family == SourceFamily.HIST:
            return 3
        fp = plan.family(c.family)
        return 1 if fp and fp.mandatory else 2

    return sorted(collectors, key=key)


def make_run_id(vendor_id: str, mode: str, as_of: str, seeds: dict, plan: DepthPlan) -> str:
    """Short, reproducible id: vendor + as-of date + hash of (mode, seeds, depth plan)."""
    blob = json.dumps({"v": vendor_id, "mode": mode, "as_of": as_of, "seeds": seeds,
                       "plan": plan.model_dump(mode="json")}, sort_keys=True, default=str)
    return f"{vendor_id}-{as_of.replace('-', '')}-{hashlib.sha256(blob.encode()).hexdigest()[:8]}"


def make_fetcher(mode: str, store: Any, *, tou_path: str | Path | None = None) -> Any:
    from footprint.net.fetcher import LiveFetcher, ReplayFetcher

    if mode == "replay":
        return ReplayFetcher(store)
    if mode != "live_rules":
        raise ValueError(f"unknown mode {mode!r}")
    from footprint.net.tou import load_tou

    load_dotenv()
    return LiveFetcher(store, load_tou(tou_path), sec_contact=os.environ.get("FOOTPRINT_SEC_CONTACT", ""))


def collect_vendor(
    assessment: VendorAssessment,
    mode: CollectMode = "live_rules",
    seeds_dir: str | Path = "seeds",
    store: Any = None,
    *,
    fetcher: Any = None,
    collectors: list[Any] | None = None,
    runs_dir: str | Path = "runs",
    as_of: str | None = None,
    write: bool = True,
) -> CollectionRun:
    """Run the collectors the depth plan calls for (mandatory first, caps via CollectContext, saturation stop
    for optional families), extract AI passages from every new document, and write runs/<run_id>/."""
    from footprint.capture.store import EvidenceStore
    from footprint.collectors.base import CollectContext
    from footprint.extract import find_passages, load_core_lexicon

    store = store if store is not None else EvidenceStore()
    fetcher = fetcher if fetcher is not None else make_fetcher(mode, store)
    profile, plan = assessment.profile, assessment.depth
    vid = profile.vendor_id
    seeds = load_seeds(vid, seeds_dir)
    as_of = as_of or dt.date.today().isoformat()
    run = CollectionRun(run_id=make_run_id(vid, mode, as_of, seeds, plan), vendor_id=vid, mode=mode, as_of=as_of)
    lexicon = load_core_lexicon()
    ctx = CollectContext(profile=profile, seeds=seeds, plan=plan, fetcher=fetcher, store=store)
    seen_docs: set[str] = set()
    seen_passages: set[str] = set()
    idle = 0
    for c in order_collectors(collectors if collectors is not None else default_collectors(), plan):
        seeded = bool(getattr(c, "seeded", False))
        fp = plan.family(c.family)
        if not seeded and not (fp and fp.mandatory) and idle >= plan.saturation_window:
            run.coverage.append(ctx.entry(c.family, CoverageStatus.STOPPED, c.name,
                                          note=f"saturation: {idle} collectors in a row found no new AI passage"))
            continue
        try:
            if not c.applies(profile, seeds, plan):
                run.coverage.append(ctx.entry(c.family, CoverageStatus.NOT_APPLICABLE, c.name,
                                              note="collector does not apply to this vendor"))
                continue
            res = c.collect(ctx)
        except Exception as exc:  # noqa: BLE001 - one collector must not sink the run
            run.coverage.append(ctx.entry(c.family, CoverageStatus.ERROR, c.name,
                                          note=f"collector crashed: {type(exc).__name__}: {exc}"[:300]))
            continue
        run.captures.extend(res.captures)
        run.leads.extend(u for u in res.leads if u not in run.leads)
        run.notes.extend(res.notes)
        new = 0
        per_family: dict[SourceFamily, int] = {}
        for doc in res.documents:
            if doc.doc_id in seen_docs:
                continue
            seen_docs.add(doc.doc_id)
            run.documents.append(doc)
            try:
                text = store.get_text(doc.doc_id)
            except Exception:  # noqa: BLE001
                continue
            for start, end, hits in find_passages(text, lexicon):
                pid = Passage.make_id(doc.doc_id, start, end)
                if pid in seen_passages:
                    continue
                seen_passages.add(pid)
                run.passages.append(Passage(passage_id=pid, doc_id=doc.doc_id, vendor_id=vid, start=start, end=end,
                                            text=text[start:end], hits=hits))
                per_family[doc.family] = per_family.get(doc.family, 0) + 1
                new += 1
        for e in res.coverage:
            if e.ai_passages == 0 and per_family.get(e.family):
                e = e.model_copy(update={"ai_passages": per_family.pop(e.family)})
            run.coverage.append(e)
        if not seeded:
            idle = 0 if new else idle + 1

    covered = {e.family for e in run.coverage}
    for fp in plan.families:
        if fp.family in covered:
            continue
        if not fp.mandatory:
            status, note = CoverageStatus.NOT_APPLICABLE, f"not required at this tier (mode {fp.mode})"
        elif fp.mode == "manual" or fp.cap == 0:
            status, note = CoverageStatus.PENDING, "manual capture only (footprint capture import)"
        else:
            status, note = CoverageStatus.PENDING, "no automated collector for this family yet"
        run.coverage.append(ctx.entry(fp.family, status, "pipeline", note=note))
    if write:
        run.run_dir = write_run(run, runs_dir, assessment)
    return run


def write_run(run: CollectionRun, runs_dir: str | Path = "runs", assessment: VendorAssessment | None = None) -> str:
    """Write manifest.json, coverage.jsonl, captures.jsonl and passages.jsonl under runs/<run_id>/."""
    d = Path(runs_dir) / run.run_id
    d.mkdir(parents=True, exist_ok=True)

    def jl(name: str, items: list[Any]) -> None:
        with open(d / name, "w", encoding="utf-8", newline="\n") as fh:
            for it in items:
                fh.write(json.dumps(it.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n")

    jl("coverage.jsonl", run.coverage)
    jl("captures.jsonl", run.captures)
    jl("passages.jsonl", run.passages)
    manifest = {
        "run_id": run.run_id, "vendor_id": run.vendor_id, "mode": run.mode, "as_of": run.as_of,
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tier": assessment.depth.tier.value if assessment else "",
        "counts": {"captures": len(run.captures), "documents": len(run.documents), "passages": len(run.passages),
                   "coverage": len(run.coverage), "leads": len(run.leads)},
        "documents": [doc.model_dump(mode="json") for doc in run.documents],
        "leads": run.leads,
        "notes": run.notes,
    }
    (d / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=1, ensure_ascii=False),
                                     encoding="utf-8")
    return str(d)


def load_run_coverage(run_id: str, runs_dir: str | Path = "runs") -> list[CoverageEntry]:
    p = Path(runs_dir) / run_id / "coverage.jsonl"
    return [CoverageEntry.model_validate_json(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
