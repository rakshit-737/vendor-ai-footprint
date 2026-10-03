"""End-to-end pipeline: criticality and depth (P1), collection (P2), and the full assessment (P3/P4).

- P1: ``assess_profiles`` / ``export_workbook`` score each vendor, plan its depth and export columns L-N.
- P2: ``collect_vendor`` runs the collectors a depth plan calls for and writes runs/<vendor>-<date>-<sha8>/.
- P3/P4: ``run_assessment`` (rules, the guarded AI path, verification, clusters, reviews, verdict, risk, actions,
  screenshots, cells), ``rescore_vendor`` (stages 5-10 after an analyst decision) and ``export_assessment`` (L-V plus
  the six appended sheets, fidelity-checked). Contract: docs/contracts_p3.md sections 2 and 15.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass
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
    """Everything one collection run produced for one vendor (written to runs/<run_id>/).

    ``coverage`` holds the per-collector detail rows (the Coverage Log); ``family_status`` one aggregated row per
    source family (``aggregate_coverage``); ``fetch_log`` every gate decision of the fetcher (ToS register,
    robots.txt, rate-limited GET, refusals), which is what the robots/ToS audit reads.
    """

    run_id: str
    vendor_id: str
    mode: str
    as_of: str
    captures: list[Capture] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    passages: list[Passage] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    family_status: list[CoverageEntry] = Field(default_factory=list)
    fetch_log: list[dict[str, Any]] = Field(default_factory=list)
    leads: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    run_dir: str = ""


# Family status precedence (one status per source family per vendor, from its per-collector rows):
#   1. complete statuses, first match wins: done_manual > stopped > done. Any complete row makes the family
#      complete (as depth.coverage_complete reads the log): done_manual because the manual protocol covers the
#      whole family; stopped before done because a cap or saturation rule left part of the family unread.
#   2. otherwise the incomplete status, first match wins: pending (awaiting manual capture, the planned next
#      step) > blocked_tou > blocked_robots > blocked_bot (policy blocks before technical ones) > error > descoped.
#   3. not_applicable only when every row is not_applicable (a secondary collector that does not apply, such as
#      the WordPress sweep on a non-WordPress site, never decides the family on its own).
FAMILY_STATUS_PRECEDENCE: tuple[CoverageStatus, ...] = (
    CoverageStatus.DONE_MANUAL, CoverageStatus.STOPPED, CoverageStatus.DONE,
    CoverageStatus.PENDING, CoverageStatus.BLOCKED_TOU, CoverageStatus.BLOCKED_ROBOTS, CoverageStatus.BLOCKED_BOT,
    CoverageStatus.ERROR, CoverageStatus.DESCOPED, CoverageStatus.NOT_APPLICABLE,
)
FAMILY_COLLECTOR = "family"
AWAITING_MANUAL = "awaiting manual capture"


def family_status(statuses: list[CoverageStatus]) -> CoverageStatus | None:
    """The family's single status under FAMILY_STATUS_PRECEDENCE (None when there are no rows)."""
    present = set(statuses)
    for s in FAMILY_STATUS_PRECEDENCE:
        if s in present:
            return s
    return None


def aggregate_coverage(entries: list[CoverageEntry], plan: DepthPlan | None = None) -> list[CoverageEntry]:
    """One Coverage Log row per (vendor, family): status by FAMILY_STATUS_PRECEDENCE, requests/documents/AI
    passages summed, the plan's cap and mandatory flag, and a note listing every collector's status.

    Detail rows are kept as they are; these rows are a summary (collector ``"family"``) and are never fed back
    into the detail log, so request totals are not double counted.
    """
    order = [f.family for f in plan.families] if plan else []
    by_key: dict[tuple[str, SourceFamily], list[CoverageEntry]] = {}
    for e in entries:
        by_key.setdefault((e.vendor_id, e.family), []).append(e)
    rank = {f: i for i, f in enumerate(order)}
    out: list[CoverageEntry] = []
    for (vid, fam), rows in sorted(by_key.items(), key=lambda kv: (kv[0][0], rank.get(kv[0][1], 99),
                                                                  kv[0][1].value)):
        status = family_status([r.status for r in rows])
        assert status is not None
        fp = plan.family(fam) if plan and plan.vendor_id == vid else None
        parts = []
        for r in rows:
            label = r.status.value
            if r.status == CoverageStatus.PENDING and AWAITING_MANUAL in r.note:
                label = "awaiting manual capture"
            parts.append(f"{r.collector or 'coverage'} {label}")
        note = "; ".join(dict.fromkeys(parts))
        deciding = sorted((r for r in rows if r.status == status and r.note), key=lambda r: -r.requests_used)
        why = deciding[0].note if deciding else ""
        if why and len(rows) > 1:
            note += f" | decided by: {why}"
        elif why:
            note = why
        out.append(CoverageEntry(
            vendor_id=vid, family=fam, mandatory=fp.mandatory if fp else any(r.mandatory for r in rows),
            status=status, collector=FAMILY_COLLECTOR,
            endpoint=", ".join(dict.fromkeys(r.collector for r in rows if r.collector)),
            requests_used=sum(r.requests_used for r in rows), cap=fp.cap if fp else max(r.cap for r in rows),
            documents=sum(r.documents for r in rows), ai_passages=sum(r.ai_passages for r in rows),
            note=note[:1000],
        ))
    return out


def status_label(entry: CoverageEntry) -> str:
    """Display label of a (family) status: 'awaiting manual capture' for a pending manual step."""
    if entry.status == CoverageStatus.PENDING and AWAITING_MANUAL in entry.note:
        return "awaiting_manual"
    return entry.status.value


LIVE_MODULES: tuple[str, ...] = ("trafilatura", "protego", "pypdf")


def check_live_dependencies(modules: tuple[str, ...] = LIVE_MODULES) -> list[str]:
    """Names of the extraction/policy modules a live run needs but cannot import ([] when all are present).

    The first live run (2 Oct 2026) silently fell back to lxml text for every HTML page; a live collection now
    refuses to start without them (``uv sync --all-extras``).
    """
    import importlib

    missing = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - any import failure means the extra is unusable
            missing.append(name)
    return missing


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
    from footprint.collectors.manual import ManualCollector
    from footprint.collectors.sec import SecCollector
    from footprint.collectors.seeds import SeedsCollector
    from footprint.collectors.site import SiteCollector
    from footprint.collectors.wayback import WaybackCollector
    from footprint.collectors.wordpress import WordPressCollector

    return [ManualCollector(), SeedsCollector(), DnsCollector(), SiteCollector(), WordPressCollector(),
            SecCollector(), JobsCollector(), WaybackCollector()]


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
    missing = check_live_dependencies()
    if missing:
        raise RuntimeError(f"live collection needs {', '.join(missing)} (uv sync --all-extras)")
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
    ctx = CollectContext(profile=profile, seeds=seeds, plan=plan, fetcher=fetcher, store=store, as_of=as_of)
    seen_docs: set[str] = set()
    seen_passages: set[str] = set()
    seen_captures: set[tuple[str, str, str]] = set()
    idle = 0
    for c in order_collectors(collectors if collectors is not None else default_collectors(), plan):
        seeded = bool(getattr(c, "seeded", False))
        fp = plan.family(c.family)
        lead = bool(getattr(c, "lead_driven", False) and ctx.blocked)
        if not seeded and not lead and not (fp and fp.mandatory) and idle >= plan.saturation_window:
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
        for cap in res.captures:  # a URL reused from an earlier collector is listed once
            key = (cap.capture_id, cap.url_requested, cap.retrieved_at)
            if key not in seen_captures:
                seen_captures.add(key)
                run.captures.append(cap)
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
            status, note = CoverageStatus.PENDING, f"{AWAITING_MANUAL}: manual capture only (footprint capture import)"
        else:
            status, note = CoverageStatus.PENDING, "no automated collector for this family yet"
        run.coverage.append(ctx.entry(fp.family, status, "pipeline", note=note))
    run.family_status = aggregate_coverage(run.coverage, plan)
    drain = getattr(fetcher, "drain_events", None)
    if callable(drain):
        run.fetch_log = [e for e in drain() if e.get("vendor_id", vid) == vid]
    if ctx.discretionary_used:
        run.notes.append(f"discretionary fetches used: {ctx.discretionary_used}/{plan.discretionary_fetches}")
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
    jl("family_status.jsonl", run.family_status)
    jl("captures.jsonl", run.captures)
    jl("passages.jsonl", run.passages)
    with open(d / "fetch_log.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for ev in run.fetch_log:
            fh.write(json.dumps(ev, sort_keys=True, ensure_ascii=False, default=str) + "\n")
    decisions: dict[str, int] = {}
    for ev in run.fetch_log:
        k = f"{ev.get('gate', '')}:{ev.get('decision', '')}"
        decisions[k] = decisions.get(k, 0) + 1
    manifest = {
        "run_id": run.run_id, "vendor_id": run.vendor_id, "mode": run.mode, "as_of": run.as_of,
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tier": assessment.depth.tier.value if assessment else "",
        "counts": {"captures": len(run.captures), "documents": len(run.documents), "passages": len(run.passages),
                   "coverage": len(run.coverage), "leads": len(run.leads), "fetch_log": len(run.fetch_log)},
        "family_status": {e.family.value: status_label(e) for e in run.family_status},
        "fetch_decisions": dict(sorted(decisions.items())),
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


def load_run_family_status(run_id: str, runs_dir: str | Path = "runs") -> list[CoverageEntry]:
    """The run's one-row-per-family summary (recomputed from coverage.jsonl for runs written before it existed)."""
    p = Path(runs_dir) / run_id / "family_status.jsonl"
    if p.exists():
        return [CoverageEntry.model_validate_json(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return aggregate_coverage(load_run_coverage(run_id, runs_dir))


def load_run_fetch_log(run_id: str, runs_dir: str | Path = "runs") -> list[dict[str, Any]]:
    p = Path(runs_dir) / run_id / "fetch_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# =========================================================================== P3/P4: the assessment
#
# Contract: docs/contracts_p3.md sections 2 and 15. One entry point (run_assessment) serves the CLI, the Streamlit
# app and the notebook; rescore_vendor repeats stages 5-10 after an analyst review; export_assessment writes L-V and
# the six appended sheets and proves nothing else changed.

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
CONFIG_FILES: dict[str, str] = {
    "rubric": "rubric.toml", "depth": "depth.toml", "lexicon": "lexicon.toml", "sources": "sources.toml",
    "risk": "risk.toml", "actions": "actions.toml", "tou": "tou.toml",
}
WHOLE_PAGE_CHARS = 30_000
"""A triaged page without lexicon passages goes to extraction whole, as one chunk of at most this many characters."""
MAX_WHOLE_PAGES = 3
"""Whole-page chunks per vendor (the first triage picks that have no lexicon passage)."""
EXPAND_TEXT_CHARS = 1_200
"""Characters of homepage text the name expansion sees (with the page title)."""
SHOT_BUDGET = 4
"""Live screenshots per vendor: the Primary item always, then other cited items in Evidence ID order."""
MANUAL_INBOX = "manual_inbox"
"""Analyst inbox under the evidence store root, imported at the start of a live run."""
FINDINGS_FILE = "findings.json"
"""runs/findings.json: the latest written assessment, for the deck builder."""
CELL_FIELDS: tuple[str, ...] = tuple(StudentCells.model_fields)


class InputError(ValueError):
    """The input workbook failed validation; ``issues`` lists every problem and nothing was assessed."""

    def __init__(self, issues: list[Any]) -> None:
        self.issues = list(issues)
        shown = "; ".join(f"{getattr(i, 'code', '')} {getattr(i, 'message', i)}".strip() for i in self.issues[:5])
        super().__init__(f"the input workbook failed validation: {shown}")


def _input_bytes(xlsx: Any) -> bytes:
    """The workbook's raw bytes from a path, bytes or a binary stream (rewound when it can be)."""
    if isinstance(xlsx, (bytes, bytearray, memoryview)):
        return bytes(xlsx)
    if isinstance(xlsx, (str, os.PathLike)):
        return Path(xlsx).read_bytes()
    read = getattr(xlsx, "read", None)
    if callable(read):
        seek = getattr(xlsx, "seek", None)
        if callable(seek):
            try:
                seek(0)
            except (OSError, ValueError):
                pass
        data = read()
        if callable(seek):
            try:
                seek(0)
            except (OSError, ValueError):
                pass
        return bytes(data)
    raise TypeError("xlsx must be a path, bytes or a binary stream")


def config_hashes(config_dir: str | Path = CONFIG_DIR) -> dict[str, str]:
    """sha256 of each config file's raw bytes ("" when the file is missing), keyed as in the manifest."""
    out: dict[str, str] = {}
    for key, name in CONFIG_FILES.items():
        p = Path(config_dir) / name
        out[key] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""
    return out


def prompt_hashes() -> dict[str, str]:
    from footprint import ai

    return {name: ai.prompt_sha256(name) for name in ai.PROMPT_NAMES}


def code_version() -> str:
    """'{package version}+{12 hex}': the hash runs over every module of the footprint package (LF-normalised)."""
    from footprint import __version__

    pkg = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(pkg.rglob("*.py")):
        h.update(p.relative_to(pkg).as_posix().encode("utf-8") + b"\0")
        h.update(p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return f"{__version__}+{h.hexdigest()[:12]}"


def assessment_run_id(as_of: str, input_sha256: str, mode: str, vendor_ids: list[str],
                      config_sha256: dict[str, str], prompts_sha256: dict[str, str]) -> str:
    """'A-{as_of without dashes}-{sha8}' over the input, mode, vendors, config and prompt hashes."""
    blob = json.dumps({"input": input_sha256, "mode": mode, "vendors": vendor_ids, "config": config_sha256,
                       "prompts": prompts_sha256}, sort_keys=True)
    return f"A-{as_of.replace('-', '')}-{hashlib.sha256(blob.encode()).hexdigest()[:8]}"


# --------------------------------------------------------------------------- collection runs on disk


def collection_runs(vendor_id: str, runs_dir: str | Path = "runs") -> list[tuple[str, str, str, Path]]:
    """The vendor's collection runs as (as_of, created_at, run_id, dir), oldest first (the order evaluate uses)."""
    root = Path(runs_dir)
    out: list[tuple[str, str, str, Path]] = []
    if not root.is_dir():
        return out
    for d in root.glob(f"{vendor_id}-*"):
        m = d / "manifest.json"
        if not d.is_dir() or not m.is_file() or not (d / "captures.jsonl").is_file():
            continue
        try:
            man = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(man.get("vendor_id", "")) != vendor_id:
            continue
        out.append((str(man.get("as_of", "")), str(man.get("created_at", "")), str(man.get("run_id") or d.name), d))
    return sorted(out)


def latest_collection_run(vendor_id: str, runs_dir: str | Path = "runs", as_of: str | None = None) -> Path | None:
    """Directory of the vendor's newest collection run dated on or before ``as_of`` (any date when None)."""
    runs = [r for r in collection_runs(vendor_id, runs_dir) if as_of is None or (r[0] and r[0] <= as_of)]
    return runs[-1][3] if runs else None


def load_collection_run(run_dir: str | Path) -> CollectionRun:
    """Read back what ``write_run`` stored (documents come from the manifest; text stays in the evidence store)."""
    d = Path(run_dir)
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))

    def jl(name: str, model: Any) -> list[Any]:
        p = d / name
        if not p.is_file():
            return []
        return [model.model_validate_json(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    coverage = jl("coverage.jsonl", CoverageEntry)
    fam = jl("family_status.jsonl", CoverageEntry) or aggregate_coverage(coverage)
    fetch_log: list[dict[str, Any]] = []
    if (d / "fetch_log.jsonl").is_file():
        fetch_log = [json.loads(x) for x in (d / "fetch_log.jsonl").read_text(encoding="utf-8").splitlines()
                     if x.strip()]
    return CollectionRun(
        run_id=str(man.get("run_id") or d.name), vendor_id=str(man["vendor_id"]), mode=str(man.get("mode", "")),
        as_of=str(man.get("as_of", "")), captures=jl("captures.jsonl", Capture),
        documents=[Document.model_validate(x) for x in man.get("documents", [])],
        passages=jl("passages.jsonl", Passage), coverage=coverage, family_status=fam, fetch_log=fetch_log,
        leads=[str(u) for u in man.get("leads", [])], notes=[str(n) for n in man.get("notes", [])], run_dir=str(d),
    )


def replay_as_of(vendor_ids: list[str], runs_dir: str | Path = "runs") -> str:
    """The as_of of the newest collection run of each vendor; they must agree (ValueError otherwise)."""
    dates: dict[str, str] = {}
    for vid in vendor_ids:
        runs = collection_runs(vid, runs_dir)
        if runs:
            dates[vid] = runs[-1][0]
    found = sorted(set(dates.values()))
    if not found:
        raise ValueError(f"no collection run under {runs_dir} for {', '.join(vendor_ids)}; run a live collection "
                         "first or pass as_of")
    if len(found) > 1:
        detail = ", ".join(f"{v} {d}" for v, d in sorted(dates.items()))
        raise ValueError(f"the newest collection runs have different dates ({detail}); pass as_of to choose one")
    return found[0]


class _Pack:
    """One vendor's collection run with lookups: documents by id, captures by id, text by doc id (cached)."""

    def __init__(self, run: CollectionRun, store: Any, index: dict[str, Capture]) -> None:
        self.run = run
        self.store = store
        self.documents = list(run.documents)
        self.docs = {d.doc_id: d for d in self.documents}
        self.caps = dict(index)
        self.caps.update({c.capture_id: c for c in run.captures})
        order = {d.doc_id: n for n, d in enumerate(self.documents)}
        self.passages = sorted((p for p in run.passages if p.doc_id in self.docs),
                               key=lambda p: (order[p.doc_id], p.start, p.end, p.passage_id))
        self._texts: dict[str, str | None] = {}

    def capture(self, capture_id: str) -> Capture | None:
        return self.caps.get(capture_id)

    def text(self, doc_id: str) -> str | None:
        if doc_id not in self._texts:
            try:
                self._texts[doc_id] = self.store.get_text(doc_id)
            except (KeyError, OSError, UnicodeDecodeError):
                self._texts[doc_id] = None
        return self._texts[doc_id]

    def image(self, doc: Document) -> bool:
        """A capture whose bytes are an image: its decoded 'text' is noise and never carries a claim."""
        cap = self.capture(doc.capture_id)
        return bool(cap and cap.content_type.lower().startswith("image/"))


def _manual_additions(run: CollectionRun, store: Any, plan: DepthPlan, as_of: str) -> CollectionRun:
    """Add the vendor's manual captures (retrieved on or before ``as_of``) that the collection run does not hold yet:
    they were imported after the run. Each becomes a document with its lexicon passages and a done_manual row."""
    finder = getattr(store, "manual_captures", None)
    if not callable(finder):
        return run
    held = {c.capture_id for c in run.captures}
    latest: dict[str, Capture] = {}
    for c in finder(run.vendor_id):
        if c.retrieved_at[:10] <= as_of and c.capture_id not in held:
            latest[c.url_requested] = c
    if not latest:
        return run
    from footprint.extract import extract_document, find_passages, load_core_lexicon

    lexicon = load_core_lexicon()
    run = run.model_copy(deep=True)
    seen_docs = {d.doc_id for d in run.documents}
    per: dict[SourceFamily, list[int]] = {}
    for c in sorted(latest.values(), key=lambda c: (c.retrieved_at, c.url_requested)):
        counts = per.setdefault(c.family, [0, 0, 0])
        counts[0] += 1
        run.captures.append(c)
        try:
            doc = extract_document(c, store.get_raw(c.capture_id), store)
        except Exception:  # noqa: BLE001 - an unreadable capture is counted, not fatal
            continue
        doc = doc.model_copy(update={"url": c.url_requested, "family": c.family})
        if doc.doc_id in seen_docs:
            continue
        seen_docs.add(doc.doc_id)
        run.documents.append(doc)
        counts[1] += 1
        text = store.get_text(doc.doc_id)
        for start, end, hits in find_passages(text, lexicon):
            run.passages.append(Passage(passage_id=Passage.make_id(doc.doc_id, start, end), doc_id=doc.doc_id,
                                        vendor_id=run.vendor_id, start=start, end=end, text=text[start:end],
                                        hits=hits))
            counts[2] += 1
    for fam, (n, docs, passages) in sorted(per.items(), key=lambda kv: kv[0].value):
        fp = plan.family(fam)
        run.coverage.append(CoverageEntry(
            vendor_id=run.vendor_id, family=fam, mandatory=bool(fp and fp.mandatory), status=CoverageStatus.DONE_MANUAL,
            collector="manual", endpoint="evidence/index.jsonl (manual=true)", cap=fp.cap if fp else 0,
            documents=docs, ai_passages=passages,
            note=f"{n} manual captures imported after collection run {run.run_id}; {docs} documents extracted",
        ))
    run.notes.append(f"manual: {len(latest)} manual captures added after the collection run")
    return run


# --------------------------------------------------------------------------- run context


@dataclass
class _RunContext:
    mode: str
    as_of: str
    store: Any
    seeds_dir: Path
    runs_dir: Path
    overrides: OverrideStore | None
    reviews: OverrideStore | None
    llm: Any
    fetcher: Any
    team: str | None
    guard_texts: list[str]
    guard_team: str
    host_policy: Any
    audit: Any
    shots: bool
    recollect: bool
    write: bool
    stack: ExitStack
    browser: Any = None
    browser_note: str = ""
    _index: dict[str, Capture] | None = None

    @property
    def live(self) -> bool:
        return self.mode != "replay"

    def capture_index(self) -> dict[str, Capture]:
        if self._index is None:
            finder = getattr(self.store, "captures", None)
            self._index = {c.capture_id: c for c in finder()} if callable(finder) else {}
        return self._index


def _collection_for(rc: _RunContext, assessment: VendorAssessment, seeds: dict, notes: list[str]) -> CollectionRun:
    """Stage 2. Replay: the newest stored collection run dated on or before as_of (no network). Live: the
    same-day run with the same id when one exists (unless ``recollect``), otherwise a new polite collection."""
    vid = assessment.profile.vendor_id
    plan = assessment.depth
    if rc.live:
        run_id = make_run_id(vid, "live_rules", rc.as_of, seeds, plan)
        existing = rc.runs_dir / run_id
        if (existing / "manifest.json").is_file() and (existing / "captures.jsonl").is_file() and not rc.recollect:
            notes.append(f"collection: reused today's collection run {run_id} (same vendor, date, seeds and depth "
                         "plan); no site was requested again")
            run = load_collection_run(existing)
        else:
            run = collect_vendor(assessment, "live_rules", rc.seeds_dir, rc.store, fetcher=rc.fetcher,
                                 runs_dir=rc.runs_dir, as_of=rc.as_of, write=True)
            rc._index = None
            notes.append(f"collection: new collection run {run.run_id}")
    else:
        d = latest_collection_run(vid, rc.runs_dir, rc.as_of)
        if d is None:
            notes.append(f"collection: no collection run on or before {rc.as_of}; nothing to replay")
            return CollectionRun(run_id="", vendor_id=vid, mode="replay", as_of=rc.as_of)
        run = load_collection_run(d)
        notes.append(f"collection: replayed collection run {run.run_id}")
    return _manual_additions(run, rc.store, plan, rc.as_of)


# --------------------------------------------------------------------------- stages 3-4: rules and the AI path


def _rule_items(pack: _Pack, seeds: dict, profile: VendorProfile, plan: DepthPlan, as_of: str,
                log: list[str]) -> list[Any]:
    """Stage 3: rules.tag_document for DNS documents, then rules.tag_passage for every passage in document order."""
    from footprint import rules

    items: list[Any] = []
    for doc in pack.documents:
        if doc.kind != "dns":
            continue
        cap, text = pack.capture(doc.capture_id), pack.text(doc.doc_id)
        if cap is None or text is None:
            log.append(f"{doc.doc_id[:12]}: capture or text missing from the evidence store")
            continue
        items += rules.tag_document(doc, text, cap, seeds, profile, plan, as_of=as_of, log=log)
    for p in pack.passages:
        doc = pack.docs[p.doc_id]
        cap, text = pack.capture(doc.capture_id), pack.text(doc.doc_id)
        if cap is None or text is None:
            log.append(f"{p.passage_id}: capture or text missing from the evidence store")
            continue
        if pack.image(doc):
            log.append(f"{p.passage_id}: image capture, never a claim")
            continue
        try:
            item = rules.tag_passage(p, doc, cap, seeds, profile, plan, as_of=as_of, doc_text=text, log=log)
        except ValueError as exc:
            log.append(f"{p.passage_id}: {exc}")
            continue
        if item is not None:
            items.append(item)
    return items


def _llm_calls(audit: Any, vendor_id: str) -> int:
    """LLM lookups made for the vendor so far (withholdings and budget stops are not calls)."""
    return sum(1 for r in audit.records if r.get("vendor_id") == vendor_id
               and r.get("status") not in ("withheld", "budget"))


def _homepage(pack: _Pack, profile: VendorProfile) -> Document | None:
    from urllib.parse import urlsplit

    domain = profile.domain
    for doc in pack.documents:
        if doc.kind != "html" or not domain:
            continue
        parts = urlsplit(doc.url)
        host = (parts.hostname or "").lower()
        if (host == domain or host.endswith("." + domain)) and parts.path in ("", "/") and not parts.query:
            return doc
    return None


def _ai_items(rc: _RunContext, pack: _Pack, seeds: dict, assessment: VendorAssessment, log: list[str],
              notes: list[str]) -> tuple[list[Any], dict[str, Any]]:
    """Stage 4: name expansion, triage, claim extraction (all through the payload guard and the cache), then
    verify.verify_claim and rules.claim_item. Gemini calls stay within the vendor's plan.gemini_calls."""
    from urllib.parse import urlsplit

    from footprint import ai, rules, verify

    stats: dict[str, Any] = {"claims": 0, "verified": 0, "failures": {}, "whole_pages": 0, "names": 0}
    llm = rc.llm
    if isinstance(llm, ai.NullLLM) or getattr(llm, "name", "") == "null":
        return [], stats
    profile, plan = assessment.profile, assessment.depth
    vid = profile.vendor_id
    company = profile.name
    guard = ai.PayloadGuard(rc.guard_texts, rc.guard_team,
                            scrub_names=[str(x) for x in (seeds.get("person_scrub") or [])],
                            host_policy=rc.host_policy)
    budget = max(0, int(plan.gemini_calls))

    home = _homepage(pack, profile)
    if home is not None and budget - _llm_calls(rc.audit, vid) >= 3:
        text = pack.text(home.doc_id) or ""
        names = ai.expand_names(llm, company, [home.title, text[:EXPAND_TEXT_CHARS]], guard=guard, audit=rc.audit,
                                vendor_id=vid, url=home.url, aliases=[str(a) for a in seeds.get("aliases", []) or []],
                                max_calls=1)
        stats["names"] = len(names)
        if names:
            notes.append("name expansion (search keys only, never evidence): " + ", ".join(names[:20]))

    with_passages = {p.doc_id for p in pack.passages}
    rows = [ai.TriageRow(id=d.doc_id[:16], path=(urlsplit(d.url).path or "/")[:200], title=(d.title or "")[:200],
                         date=d.published, family=d.family.value, url=d.url)
            for d in pack.documents if d.kind in ("html", "pdf") and not pack.image(d)]
    chunks: list[Passage] = []
    left = budget - _llm_calls(rc.audit, vid)
    if rows and left >= 2:
        picks = ai.triage(llm, rows, company=company, guard=guard, audit=rc.audit, vendor_id=vid,
                          max_calls=min(2, left - 1))
        by_short = {d.doc_id[:16]: d for d in pack.documents}
        for short in picks:
            doc = by_short.get(short)
            if doc is None or doc.doc_id in with_passages:
                continue
            text = pack.text(doc.doc_id)
            if not text or not text.strip():
                continue
            end = min(len(text), WHOLE_PAGE_CHARS)
            chunks.append(Passage(passage_id=Passage.make_id(doc.doc_id, 0, end), doc_id=doc.doc_id, vendor_id=vid,
                                  start=0, end=end, text=text[:end], hits=[]))
            if len(chunks) >= MAX_WHOLE_PAGES:
                break
    stats["whole_pages"] = len(chunks)

    sendable = [*(p for p in pack.passages if not pack.image(pack.docs[p.doc_id])), *chunks]
    left = budget - _llm_calls(rc.audit, vid)
    claims = ai.extract_claims(llm, sendable, company=company, guard=guard, docs=pack.docs, audit=rc.audit,
                               vendor_id=vid, batch_size=None, max_calls=max(0, left)) if sendable else []
    stats["claims"] = len(claims)
    lookup = {p.passage_id: p for p in sendable}
    psha = ai.prompt_sha256("extract_v1")
    seen: set[str] = set()
    failures: dict[str, int] = {}
    out: list[Any] = []
    for claim in claims:
        p = lookup.get(claim.passage_id)
        doc = pack.docs.get(p.doc_id) if p is not None else None
        cap = pack.capture(doc.capture_id) if doc is not None else None
        text = pack.text(doc.doc_id) if doc is not None else None
        if p is None or doc is None or cap is None or text is None:
            result = verify.verify_claim(claim, None, "")
        else:
            result = verify.verify_claim(claim, p, text, placeholders=guard.placeholders(p.passage_id),
                                         document=doc, capture=cap, seeds=seeds, profile=profile, seen=seen, log=log)
        for code in result.failures:
            failures[code] = failures.get(code, 0) + 1
        if not result.ok or p is None or doc is None or cap is None or text is None:
            continue
        try:
            item = rules.claim_item(claim, result, p, doc, cap, seeds, profile, plan, as_of=rc.as_of, doc_text=text,
                                    llm_model=rc.audit.model_for(p.passage_id), prompt_sha256=psha)
        except ValueError as exc:
            log.append(f"claim {claim.passage_id}: {exc}")
            continue
        out.append(item)
    stats["verified"] = len(out)
    stats["failures"] = dict(sorted(failures.items()))
    return out, stats


# --------------------------------------------------------------------------- stages 5-10


def _cell_edits(cells: StudentCells, overrides: OverrideStore | None, vendor_id: str) -> StudentCells:
    """Apply the latest ``cell_edit`` record of each student field (an analyst's edited cell text)."""
    if overrides is None:
        return cells
    upd: dict[str, str] = {}
    for rec in overrides.records("cell_edit", vendor_id):
        if rec.key in CELL_FIELDS:
            upd[rec.key] = rec.value
    return cells.model_copy(update=upd) if upd else cells


def _decide(profile: VendorProfile, criticality: CriticalityResult, plan: DepthPlan, coverage: list[CoverageEntry],
            items: list[Any], *, as_of: str, team: str | None, overrides: OverrideStore | None,
            reviews: OverrideStore | None, notes: list[str], shots: Any = None) -> Any:
    """Stages 5-10: dedupe (V9) and origin clusters, analyst reviews (HC2), corroboration, verdict and roles, risk
    and actions, screenshots (``shots(items) -> items``), evidence ids, findings and cells (with cell edits).

    Reviews are applied before corroboration is linked, so a rejected item never corroborates and an accepted
    LLM proposal is linked like any other item *(interpretation of the stage order)*.
    """
    from footprint import actions as actions_mod
    from footprint import cluster, compose, rules
    from footprint import risk as risk_mod
    from footprint import verdict as verdict_mod
    from footprint.models import VendorFindings

    vid = profile.vendor_id
    log: list[str] = []
    items = cluster.dedupe(items, log=log)
    items = cluster.assign_clusters(items)
    if reviews is not None:
        items = rules.apply_reviews(items, reviews)
    items = cluster.link_corroboration(items)
    verdict = verdict_mod.decide(items, plan, coverage)
    items = verdict_mod.assign_roles(items, verdict)
    risk = risk_mod.assess(verdict, items, criticality, profile, plan, as_of=as_of, overrides=overrides)
    acts = actions_mod.plan_actions(risk, verdict, items, criticality.tier)
    if shots is not None:
        items = shots(items)
    items = compose.assign_evidence_ids(items, vid)
    findings = VendorFindings(profile=profile, criticality=criticality, depth=plan, coverage=coverage,
                              evidence=items, verdict=verdict, risk=risk, actions=acts, notes=notes)
    cells = _cell_edits(compose.build_cells(findings, team, as_of), overrides, vid)
    return findings.model_copy(update={"cells": cells})


def _browser(rc: _RunContext) -> Any:
    """One Playwright Chromium per run, opened on first use; None (with a note) when it cannot start."""
    if rc.browser is None and not rc.browser_note:
        try:
            from footprint.capture.shots import chromium

            rc.browser = rc.stack.enter_context(chromium())
        except Exception as exc:  # noqa: BLE001 - screenshots are optional evidence
            rc.browser_note = f"screenshots skipped: Chromium unavailable ({type(exc).__name__})"
    return rc.browser


def _shots_for(rc: _RunContext, pack: _Pack, seeds: dict, notes: list[str]) -> Any:
    """``items -> items``: live runs take excerpt-anchored screenshots of the Primary item and other cited items
    (at most SHOT_BUDGET, each page loaded once through the run's ToS -> robots -> rate-limit gate); every run then
    reads the latest recorded shot of each cited item from the shots index, so replay cites the same files."""
    from footprint.capture import shots as shots_mod
    from footprint.models import CITED_ROLES, ROLE_ORDER, STRENGTH_ORDER

    def attach(items: list[Any]) -> list[Any]:
        cited = [i for i in items if i.role in CITED_ROLES and i.family != SourceFamily.DNS]
        if rc.live and rc.shots and cited:
            ranked = sorted(cited, key=lambda i: (ROLE_ORDER.index(i.role), STRENGTH_ORDER.index(i.strength),
                                                  i.url, i.start, i.item_key))[:SHOT_BUDGET]
            browser = _browser(rc)
            if browser is None:
                if rc.browser_note and rc.browser_note not in notes:
                    notes.append(rc.browser_note)
            else:
                seeded = {str(s.get("url", "")) for s in seeds.get("seed", []) or [] if isinstance(s, dict)}
                seeded |= {c.url_final or c.url_requested for c in pack.run.captures}
                policy = shots_mod.GatePolicy.from_fetcher(rc.fetcher, seeded=seeded)
                by_cap: dict[str, list[Any]] = {}
                for item in ranked:
                    by_cap.setdefault(item.capture_id, []).append(item)
                for cap_id, group in by_cap.items():
                    cap = pack.capture(cap_id)
                    if cap is None:
                        continue
                    try:
                        shots_mod.screenshot_excerpts(cap, [(i.excerpt, i.start, i.end) for i in group], rc.store,
                                                      policy, document=pack.docs.get(group[0].doc_id),
                                                      browser=browser)
                    except Exception as exc:  # noqa: BLE001 - a failed shot never fails the run
                        notes.append(f"screenshot failed for {group[0].url}: {type(exc).__name__}")
        out = []
        for item in items:
            if item.role in CITED_ROLES:
                found = shots_mod.find_shot(item.item_key, rc.store)
                if found is not None and (found.path or found.visible is not None):
                    item = item.model_copy(update={"screenshot_path": found.path, "screenshot_sha256": found.sha256,
                                                   "visible_in_render": found.visible})
            out.append(item)
        return out

    return attach


def _failed_findings(assessment: VendorAssessment, coverage: list[CoverageEntry], *, as_of: str, team: str | None,
                     overrides: OverrideStore | None, notes: list[str]) -> Any:
    """Findings for a vendor whose analysis failed: no evidence and every mandatory family marked as an error (so
    the verdict is Inconclusive, never a false 'Not detected'), with the failure in the notes."""
    vid = assessment.profile.vendor_id
    blocked = [CoverageEntry(vendor_id=vid, family=f.family, mandatory=f.mandatory, status=CoverageStatus.ERROR,
                             cap=f.cap, note="analysis failed; see the run notes")
               for f in assessment.depth.families if f.mandatory]
    return _decide(assessment.profile, assessment.criticality, assessment.depth, blocked, [], as_of=as_of,
                   team=team, overrides=overrides, reviews=None, notes=notes)


def _example_findings(data: WorkbookData, *, as_of: str, team: str | None) -> Any:
    """The V-000 calibration case: its criticality and depth, the calibration risk inputs with a Confirmed verdict
    (17 of 18, Critical), no evidence; cells are composed for comparison only and never written."""
    from footprint import actions as actions_mod
    from footprint import compose
    from footprint import risk as risk_mod
    from footprint.models import VendorFindings

    ex = assess_example(data)
    if ex is None:
        return None
    verdict = risk_mod.calibration_verdict()
    risk = risk_mod.calibrate()
    acts = actions_mod.plan_actions(risk, verdict, [], ex.criticality.tier)
    findings = VendorFindings(profile=ex.profile, criticality=ex.criticality, depth=ex.depth, coverage=[],
                              evidence=[], verdict=verdict, risk=risk, actions=acts,
                              notes=["calibration example: never written to the inventory"])
    try:
        cells = compose.build_cells(findings, team, as_of)
    except ValueError:
        return findings
    return findings.model_copy(update={"cells": cells})


# --------------------------------------------------------------------------- run_assessment


def run_assessment(
    xlsx: Any,
    mode: str = "replay",
    *,
    vendors: list[str] | tuple[str, ...] | None = None,
    as_of: str | None = None,
    store: Any = None,
    seeds_dir: str | Path = "seeds",
    runs_dir: str | Path = "runs",
    overrides: OverrideStore | None = None,
    reviews: OverrideStore | None = None,
    llm: Any = None,
    fetcher: Any = None,
    team: str | None = None,
    write: bool | None = None,
    progress: Any = None,
    shots: bool | None = None,
    recollect: bool = False,
) -> Any:
    """The whole assessment (contracts_p3 sections 2 and 15) for every vendor of the workbook, in sheet order.

    - replay never touches the network: the newest stored collection run of each vendor (dated on or before
      ``as_of``), the LLM cache only (``ai.CachedLLM(None)``), the recorded AI host decisions and the recorded
      screenshots.
    - live_rules collects politely, or reuses today's collection run with the same id unless ``recollect`` (a second
      crawl on the same day would only repeat the requests), and uses no LLM.
    - live_ai adds Gemini through the payload guard and the cache (NullLLM with a note when no key is set).

    ``llm`` and ``fetcher`` override the mode's choice (tests); ``shots`` (default: live modes with a LiveFetcher)
    takes excerpt screenshots. ``write`` (default: live modes) stores runs/<run_id>/ and runs/findings.json.
    One vendor's failure is recorded in its notes and never stops the run.
    """
    from footprint import ai
    from footprint.capture.store import EvidenceStore
    from footprint.models import AssessmentResult
    from footprint.net.tou import load_tou
    from footprint.workbook import read_workbook

    if mode not in ("replay", "live_rules", "live_ai"):
        raise ValueError(f"unknown mode {mode!r} (replay, live_rules or live_ai)")
    raw = _input_bytes(xlsx)
    input_sha = hashlib.sha256(raw).hexdigest()
    data = read_workbook(raw)
    if not data.ok:
        raise InputError([i for i in data.issues if i.severity == "error"])
    profiles = list(data.vendors)
    if vendors:
        wanted = list(dict.fromkeys(vendors))
        unknown = sorted(set(wanted) - {p.vendor_id for p in profiles})
        if unknown:
            raise ValueError(f"unknown vendor id(s): {', '.join(unknown)}")
        profiles = [p for p in profiles if p.vendor_id in wanted]
    vendor_ids = [p.vendor_id for p in profiles]
    live = mode != "replay"
    if live or team is None:
        load_dotenv()
    team = team if team is not None else (os.environ.get("FOOTPRINT_TEAM_NAME") or None)
    runs_path = Path(runs_dir)
    if as_of is None:
        as_of = dt.datetime.now(dt.timezone.utc).date().isoformat() if live else replay_as_of(vendor_ids, runs_path)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        raise ValueError(f"as_of must be YYYY-MM-DD, got {as_of!r}")
    dt.date.fromisoformat(as_of)
    store = store if store is not None else EvidenceStore()
    write = live if write is None else write
    cfg_sha, prm_sha = config_hashes(), prompt_hashes()
    run_id = assessment_run_id(as_of, input_sha, mode, vendor_ids, cfg_sha, prm_sha)
    run_dir = runs_path / run_id

    if live and fetcher is None:
        fetcher = make_fetcher("live_rules", store)
    if live:
        inbox = Path(store.root) / MANUAL_INBOX
        if inbox.is_dir():
            from footprint.capture.manual import import_inbox

            import_inbox(store, inbox)
    if llm is None:
        llm = ai.make_llm(mode, cache_dir=Path(store.root) / "llm_cache")  # type: ignore[arg-type]
    if write:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "llm_calls.jsonl").unlink(missing_ok=True)
    audit = ai.AuditLog(run_dir / "llm_calls.jsonl" if write else None)
    guard_team = team or os.environ.get("FOOTPRINT_TEAM_NAME", "") or ""
    rows = [*data.vendors, *([data.example] if data.example else [])]
    guard_texts = [str(getattr(p, f)) for p in rows for f in ai.PROFILE_GUARD_FIELDS
                   if str(getattr(p, f, "") or "").strip()]
    robots = getattr(fetcher, "robots", None) if live else None
    host_policy = ai.HostAiPolicy(load_tou(), Path(store.root) / "ai_policy.jsonl", robots=robots, as_of=as_of)
    use_shots = bool(shots if shots is not None else live) and live and hasattr(fetcher, "request_counts")

    findings: list[Any] = []
    stats: dict[str, dict[str, Any]] = {}
    collection: dict[str, str] = {}
    total = max(1, len(profiles))
    with ExitStack() as stack:
        rc = _RunContext(mode=mode, as_of=as_of, store=store, seeds_dir=Path(seeds_dir), runs_dir=runs_path,
                         overrides=overrides, reviews=reviews, llm=llm, fetcher=fetcher, team=team,
                         guard_texts=guard_texts, guard_team=guard_team, host_policy=host_policy, audit=audit,
                         shots=use_shots, recollect=recollect, write=write, stack=stack)
        by_id = {a.profile.vendor_id: a for a in assess_profiles(data, overrides)}
        for n, profile in enumerate(profiles):
            assessment = by_id[profile.vendor_id]
            vid = profile.vendor_id

            def step(stage: str, k: float, _n: int = n, _vid: str = vid) -> None:
                if progress is not None:
                    progress(f"{_vid}: {stage}", min(1.0, (_n + k) / total))

            notes: list[str] = []
            try:
                seeds = load_seeds(vid, seeds_dir)
                run = _collection_for(rc, assessment, seeds, notes)
                collection[vid] = run.run_id
                coverage = aggregate_coverage(run.coverage, assessment.depth)
                step("collection", 0.3)
                pack = _Pack(run, store, rc.capture_index())
                log: list[str] = []
                items = _rule_items(pack, seeds, profile, assessment.depth, as_of, log)
                n_rule = len(items)
                step("rules", 0.5)
                claim_items, ai_stats = _ai_items(rc, pack, seeds, assessment, log, notes)
                if claim_items:
                    from footprint import rules

                    items = rules.merge_claims(items, claim_items)
                step("ai", 0.7)
                f = _decide(profile, assessment.criticality, assessment.depth, coverage, items, as_of=as_of,
                            team=team, overrides=overrides, reviews=reviews, notes=notes,
                            shots=_shots_for(rc, pack, seeds, notes))
                step("cells", 1.0)
                stats[vid] = {"documents": len(pack.documents), "passages": len(pack.passages),
                              "rule_items": n_rule, **{f"llm_{k}": v for k, v in ai_stats.items()}}
            except Exception as exc:  # noqa: BLE001 - one vendor's failure never stops the run
                notes.append(f"assessment failed: {type(exc).__name__}: {exc}"[:500])
                f = _failed_findings(assessment, [], as_of=as_of, team=team, overrides=overrides, notes=notes)
                stats[vid] = {"failed": True}
            findings.append(f)

    example = _example_findings(data, as_of=as_of, team=team)
    manifest = _manifest(run_id=run_id, mode=mode, as_of=as_of, input_sha=input_sha, findings=findings, llm=llm,
                         audit=audit, collection=collection, stats=stats, cfg_sha=cfg_sha, prm_sha=prm_sha)
    if write and audit.path is not None and audit.path.exists():
        manifest["llm"]["audit_problems"] = ai.check_audit(audit.path, tou=load_tou(), team_name=guard_team)
    result = AssessmentResult(run_id=run_id, mode=mode, as_of=as_of, input_sha256=input_sha,  # type: ignore[arg-type]
                              vendors=findings, example=example, manifest=manifest)
    if write:
        write_assessment(result, runs_path)
    return result


def _manifest(*, run_id: str, mode: str, as_of: str, input_sha: str, findings: list[Any], llm: Any, audit: Any,
              collection: dict[str, str], stats: dict[str, dict[str, Any]], cfg_sha: dict[str, str],
              prm_sha: dict[str, str]) -> dict[str, Any]:
    from footprint import ai

    audit_stats = audit.stats()
    calls: dict[str, dict[str, Any]] = {}
    withheld: dict[str, dict[str, Any]] = {}
    counts: dict[str, dict[str, Any]] = {}
    for f in findings:
        vid = f.vendor_id
        s = audit_stats.get(vid, {})
        calls[vid] = {"planned": f.depth.gemini_calls, "actual": int(s.get("calls", 0)),
                      "api_calls": int(s.get("api_calls", 0)), "cache_hits": int(s.get("cache_hits", 0)),
                      "errors": int(s.get("errors", 0)), "quota": int(s.get("quota", 0)),
                      "budget_stops": int(s.get("budget", 0))}
        withheld[vid] = {"count": int(s.get("withheld", 0)), "sent": int(s.get("sent", 0)),
                         "rate": float(s.get("rate", 0.0))}
        ev = f.evidence
        counts[vid] = {"items": len(ev), "cited": sum(1 for i in ev if i.role != "Logged"),
                       "citable": sum(1 for i in ev if i.citable), "proposed": sum(1 for i in ev if i.proposed),
                       "rejected": sum(1 for i in ev if i.review_status == "rejected"),
                       "traps": sum(1 for i in ev if i.tags.ai_type == "not_ai"),
                       "with_llm_labels": sum(1 for i in ev if i.llm_model), **stats.get(vid, {})}
    used_models = sorted({str(r.get("model")) for r in audit.records if r.get("model")})
    return {
        "run_id": run_id, "mode": mode, "as_of": as_of, "input_sha256": input_sha, "code_version": code_version(),
        "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_sha256": cfg_sha, "prompts_sha256": prm_sha,
        "llm": {"backend": str(getattr(llm, "name", type(llm).__name__)), "chain": list(ai.MODEL_CHAIN),
                "models": used_models, "note": str(getattr(llm, "note", "") or ""),
                "switches": list(getattr(llm, "switches", []) or []), "calls": calls, "withheld": withheld},
        "collection_runs": collection,
        "counts": counts,
        "notes": {f.vendor_id: list(f.notes) for f in findings if f.notes},
    }


def write_assessment(result: Any, runs_dir: str | Path = "runs") -> str:
    """runs/<run_id>/: assessment.json, evidence.jsonl, coverage.jsonl and manifest.json (llm_calls.jsonl is written
    during the run), plus runs/findings.json, the copy the deck builder reads."""
    d = Path(runs_dir) / result.run_id
    d.mkdir(parents=True, exist_ok=True)
    body = json.dumps(result.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, indent=1) + "\n"
    for target in (d / "assessment.json", Path(runs_dir) / FINDINGS_FILE):
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)

    def jl(name: str, rows: list[Any]) -> None:
        with open(d / name, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n")

    jl("evidence.jsonl", [i for f in result.vendors for i in f.evidence])
    jl("coverage.jsonl", [e for f in result.vendors for e in f.coverage])
    with open(d / "manifest.json", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(result.manifest, sort_keys=True, ensure_ascii=False, indent=1) + "\n")
    return str(d)


def load_assessment(path: str | Path) -> Any:
    """An AssessmentResult from runs/<run_id>/ (its assessment.json) or from a JSON file."""
    from footprint.models import AssessmentResult

    p = Path(path)
    if p.is_dir():
        p = p / "assessment.json"
    return AssessmentResult.model_validate_json(p.read_text(encoding="utf-8"))


def latest_assessment_dir(runs_dir: str | Path = "runs") -> Path | None:
    """The newest assessment run (runs/A-*/ with an assessment.json) by its manifest's created_at, then name."""
    best: tuple[str, str, Path] | None = None
    root = Path(runs_dir)
    if not root.is_dir():
        return None
    for d in root.glob("A-*"):
        if not (d / "assessment.json").is_file():
            continue
        stamp = ""
        try:
            stamp = str(json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("created_at", ""))
        except (OSError, ValueError):
            pass
        if best is None or (stamp, d.name) > (best[0], best[1]):
            best = (stamp, d.name, d)
    return best[2] if best else None


# --------------------------------------------------------------------------- rescore_vendor


def rescore_vendor(findings: Any, *, as_of: str, team: str, overrides: OverrideStore | None = None,
                   reviews: OverrideStore | None = None) -> Any:
    """Stages 5-10 again after an analyst review or override: no collection, no LLM, no screenshots.

    Items keep their tags, labels, screenshots and LLM labels; roles, evidence ids, clusters, corroboration and
    review fields are recomputed from the current review store (the latest record per item wins). A changed HC1
    tier override in ``overrides`` re-scores criticality and the depth plan first.
    """
    from footprint import rules

    profile = findings.profile
    criticality, plan = findings.criticality, findings.depth
    coverage = list(findings.coverage)
    if overrides is not None:
        ov = overrides.tier_override(profile.vendor_id)
        if ov != criticality.override:
            a = assess_profile(profile, ov)
            criticality, plan = a.criticality, a.depth
            coverage = aggregate_coverage(coverage, plan)
    items = []
    for item in findings.evidence:
        item = item.model_copy(update={"role": "Logged", "evidence_id": "", "corroborates": [], "cluster_id": "",
                                       "review_status": "unreviewed", "review_reason": "", "reviewer": ""})
        items.append(rules.retag(item))
    return _decide(profile, criticality, plan, coverage, items, as_of=as_of, team=team or None, overrides=overrides,
                   reviews=reviews, notes=list(findings.notes))


# --------------------------------------------------------------------------- export_assessment

QUERY_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("Seeds", "manual web searches '{vendor} AI', '{vendor} machine learning', '{vendor} generative AI', "
              "'{product} AI' and '{executive} AI'; each seed records how it was found in seeds/<vendor id>.toml"),
    ("SEC EDGAR full-text search", "the vendor's CIK with each AI term: artificial intelligence, machine learning, "
                                   "generative AI, LLM, AI"),
    ("WordPress search", "/wp-json/wp/v2/posts?search={AI term} on vendor-owned WordPress sites"),
    ("Job boards", "the vendor's own applicant tracking system: titles matching AI or delivery-role keywords, then "
                   "AI terms in the posting text"),
    ("DNS", "TXT records of the vendor's domains through DNS-over-HTTPS, read for AI provider verification tokens"),
    ("Wayback", "CDX listing of seeded and refused URLs; archived copies only, never Save Page Now"),
)


def export_assessment(result: Any, xlsx_in: Any, out_path: Any, team: str, *, overwrite: bool = False,
                      seeds_dir: str | Path = "seeds") -> Any:
    """Write every vendor's L-V and append Evidence Log, Coverage Log, Criticality Workings, Method & Legend (the
    P1 sections plus the P3/P4 legend), Evidence Images and Run Info; then prove with check_fidelity that nothing
    else changed (FidelityError). ``out_path`` may be a path or a binary stream. The file's modified time is pinned
    to ``result.as_of``, so the same result exports byte-identical workbooks. Refuses (ValueError) a workbook other
    than the assessed one, and a run whose LLM audit log failed the release gate.
    """
    from footprint import compose
    from footprint.sheets import criticality_workings_sheet, method_legend_sheet
    from footprint.workbook import check_fidelity, write_workbook

    raw = _input_bytes(xlsx_in)
    if hashlib.sha256(raw).hexdigest() != result.input_sha256:
        raise ValueError("this workbook is not the one the assessment was run on (its SHA-256 differs)")
    llm_info = result.manifest.get("llm") if isinstance(result.manifest, dict) else None
    problems = llm_info.get("audit_problems") if isinstance(llm_info, dict) else None
    if problems:
        raise ValueError(f"release gate: the LLM audit log has {len(problems)} problem(s), e.g. {problems[0]}")
    if isinstance(out_path, (str, os.PathLike)) and isinstance(xlsx_in, (str, os.PathLike)):
        if os.path.normcase(Path(out_path).resolve()) == os.path.normcase(Path(xlsx_in).resolve()):
            raise ValueError("the output must not overwrite the input workbook")
    rubric, depth_config = load_rubric(), load_depth_config()
    assessed_by = compose.render_assessed_by(team, result.as_of) if (team or "").strip() else None
    cells = {f.vendor_id: (f.cells.model_copy(update={"assessed_by": assessed_by}) if assessed_by else f.cells)
             for f in result.vendors}
    example = result.example
    sdir = Path(seeds_dir)
    if not sdir.is_dir():
        sdir = REPO_ROOT / "seeds"
    seeds = {f.vendor_id: load_seeds(f.vendor_id, sdir) for f in result.vendors}
    legend = compose.method_legend_p3(method_legend_sheet(rubric, depth_config), seeds=seeds,
                                      query_templates=list(QUERY_TEMPLATES))
    sheets = [
        compose.evidence_log_sheet(result.vendors),
        compose.coverage_log_sheet(result.vendors),
        criticality_workings_sheet([(f.profile, f.criticality) for f in result.vendors],
                                   (example.profile, example.criticality) if example else None, rubric=rubric),
        legend,
        compose.evidence_images_sheet(result.vendors),
        compose.run_info_sheet(result),
    ]
    modified = dt.datetime.fromisoformat(result.as_of).replace(tzinfo=dt.timezone.utc)
    buffer = io.BytesIO()
    report = write_workbook(raw, buffer, cells, sheets, overwrite=overwrite, last_modified_by=team or None,
                            modified=modified)
    out = buffer.getvalue()
    issues = check_fidelity(raw, out)
    if issues:
        raise FidelityError(issues)
    if isinstance(out_path, (str, os.PathLike)):
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(out)
        os.replace(tmp, target)
    else:
        out_path.write(out)
    return report
