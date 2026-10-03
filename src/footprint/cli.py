"""Command-line interface for footprint."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from footprint import __version__
from footprint.criticality import FACTORS
from footprint.models import Tier, TierOverride
from footprint.review import OVERRIDES_ENV, OverrideStore, default_overrides_path

app = typer.Typer(no_args_is_help=True, help="Vendor AI public-footprint analysis (Optiv case study 1).")
MAX_PROBLEMS_SHOWN = 5


def _force_utf8() -> None:
    """Windows consoles default to a legacy code page; the cells use en dashes and quotes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def _fail(message: str, details: list[str] | None = None, code: int = 2) -> typer.Exit:
    """Print a short error (and the first few details) to stderr; the caller raises the returned Exit."""
    typer.echo(f"Error: {message}", err=True)
    for detail in (details or [])[:MAX_PROBLEMS_SHOWN]:
        typer.echo(f"  - {detail}", err=True)
    if details and len(details) > MAX_PROBLEMS_SHOWN:
        typer.echo(f"  ... and {len(details) - MAX_PROBLEMS_SHOWN} more", err=True)
    return typer.Exit(code=code)


@app.callback()
def main() -> None:
    """Vendor AI public-footprint analysis (Optiv case study 1)."""
    _force_utf8()


@app.command()
def version() -> None:
    """Print the footprint version."""
    typer.echo(__version__)


@app.command()
def criticality(
    input_xlsx: Path = typer.Argument(..., exists=True, dir_okay=False, help="Vendor input workbook"),
    out: Path | None = typer.Option(None, "--out", help="Write the assessed workbook here (a new file)"),
    overrides: Path | None = typer.Option(
        None, "--overrides", help=f"HC1 override store (default ${OVERRIDES_ENV}, else the repo review store)"
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace values already in the source's student cells"),
    force: bool = typer.Option(False, "--force", help="Replace the --out file if it already exists"),
    team: str | None = typer.Option(None, "--team", help="Name recorded as the workbook's last editor"),
) -> None:
    """Score criticality and plan depth for every vendor; optionally write columns L-N."""
    from footprint.pipeline import FidelityError, assess_example, assess_profiles, export_workbook
    from footprint.workbook import WorkbookWriteError, read_workbook

    if out is not None and out.exists() and not force:
        raise _fail(f"{out} already exists; pass --force to replace it.")
    try:
        data = read_workbook(input_xlsx)
    except OSError as exc:
        raise _fail(f"cannot read {input_xlsx}: {exc}") from exc
    for issue in data.issues:
        typer.echo(f"[{issue.severity}] {issue.code} {issue.cell}: {issue.message}", err=True)
    if not data.ok:
        raise typer.Exit(code=1)
    store_path = overrides or default_overrides_path()
    store = OverrideStore(store_path) if store_path.exists() else None
    typer.echo(f"Overrides: {store_path}" if store else f"Overrides: none ({store_path} not found)", err=True)
    assessments = assess_profiles(data, store)

    table = Table(title=f"Criticality ({input_xlsx.name})")
    for col in ["Vendor ID", "Vendor", *FACTORS, "Score", "Floors", "Tier", "Depth label"]:
        table.add_column(col)
    for a in assessments:
        c = a.criticality
        tier = c.tier.value + (" (override)" if c.override else "")
        table.add_row(a.profile.vendor_id, a.profile.name, *(str(c.factors[f].level) for f in FACTORS),
                      str(c.score), ", ".join(c.floors_fired) or "-", tier, a.depth.label)
    Console(width=200).print(table)

    if out is not None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            report = export_workbook(input_xlsx, out, assessments, example=assess_example(data), team=team,
                                     overwrite=overwrite)
        except WorkbookWriteError as exc:
            raise _fail("the workbook was not written.", [f"{i.code} {i.message}" for i in exc.issues]) from exc
        except FidelityError as exc:
            raise _fail("the output failed the fidelity check.", exc.problems) from exc
        except OSError as exc:
            raise _fail(f"cannot write {out}: {exc}") from exc
        typer.echo(f"Wrote {len(report.written)} cells and sheets {', '.join(report.sheets_added)} to {out}")


@app.command("override-tier")
def override_tier(
    vendor_id: str = typer.Argument(..., help="e.g. V-003"),
    tier: Tier = typer.Argument(..., help="Critical, High, Medium or Low"),
    reason: str = typer.Option(..., "--reason", help="Why the computed tier is overridden (10+ characters)"),
    analyst: str = typer.Option(..., "--analyst", help="Who made the decision"),
    overrides: Path | None = typer.Option(
        None, "--overrides", help=f"Override store to append to (default ${OVERRIDES_ENV}, else the repo's)"
    ),
) -> None:
    """HC1: record an analyst tier override (appended to the review store)."""
    store_path = overrides or default_overrides_path()
    try:
        record = TierOverride(vendor_id=vendor_id, tier=tier, reason=reason, analyst=analyst,
                              date=dt.date.today().isoformat())
        OverrideStore(store_path).add_tier_override(record)
    except ValueError as exc:
        raise _fail(f"override rejected: {exc}") from exc
    typer.echo(f"Recorded {vendor_id} -> {tier.value} in {store_path}")


DEFAULT_INPUT = Path("data/input/Meridian_Vendor_Input.xlsx")
capture_app = typer.Typer(no_args_is_help=True, help="Manual evidence capture.")
app.add_typer(capture_app, name="capture")


@app.command()
def collect(
    vendor: list[str] = typer.Option([], "--vendor", help="Vendor id (repeatable), e.g. V-001"),
    all_: bool = typer.Option(False, "--all", help="Collect every vendor in the input workbook"),
    mode: str = typer.Option("live_rules", "--mode", help="live_rules (polite network) or replay (evidence store)"),
    input_xlsx: Path = typer.Option(DEFAULT_INPUT, "--input", help="Vendor input workbook"),
    seeds_dir: Path = typer.Option(Path("seeds"), "--seeds"),
    evidence: Path = typer.Option(Path("evidence"), "--evidence", help="Evidence store root"),
    runs_dir: Path = typer.Option(Path("runs"), "--runs"),
    overrides: Path | None = typer.Option(None, "--overrides"),
    as_of: str | None = typer.Option(None, "--as-of", help="As-of date YYYY-MM-DD (default: today)"),
) -> None:
    """P2: run the collectors each vendor's depth plan calls for and write runs/<run_id>/."""
    from footprint.capture.store import EvidenceStore
    from footprint.models import COMPLETE_STATUSES
    from footprint.pipeline import assess_profiles, collect_vendor, make_fetcher, status_label
    from footprint.workbook import read_workbook

    if mode not in ("live_rules", "replay"):
        raise _fail(f"unknown --mode {mode!r} (live_rules or replay)")
    if not all_ and not vendor:
        raise _fail("pass --vendor V-00x or --all")
    if as_of is not None:
        try:
            dt.date.fromisoformat(as_of)
        except ValueError as exc:
            raise _fail(f"--as-of must be YYYY-MM-DD, got {as_of!r}") from exc
    data = read_workbook(input_xlsx)
    if not data.ok:
        raise _fail("input workbook has errors", [i.message for i in data.issues])
    store_path = overrides or default_overrides_path()
    assessments = assess_profiles(data, OverrideStore(store_path) if store_path.exists() else None)
    if not all_:
        wanted = set(vendor)
        assessments = [a for a in assessments if a.profile.vendor_id in wanted]
        missing = wanted - {a.profile.vendor_id for a in assessments}
        if missing:
            raise _fail(f"unknown vendor id(s): {', '.join(sorted(missing))}")
    store = EvidenceStore(evidence)
    try:
        fetcher = make_fetcher(mode, store)  # one fetcher per invocation: per-run host counters span all vendors
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    table = Table(title=f"Collection ({mode})")
    for col in ["Vendor", "Tier", "Run id", "Captures", "Docs", "Passages", "Family status", "Not complete"]:
        table.add_column(col)
    for a in assessments:
        run = collect_vendor(a, mode, seeds_dir, store, fetcher=fetcher, runs_dir=runs_dir,  # type: ignore[arg-type]
                             as_of=as_of)
        fams = " ".join(f"{e.family.value}:{status_label(e)}" for e in run.family_status if e.mandatory)
        open_fams = [f"{e.family.value}:{status_label(e)}" for e in run.family_status
                     if e.mandatory and e.status not in COMPLETE_STATUSES]
        table.add_row(a.profile.vendor_id, a.depth.tier.value, run.run_id, str(len(run.captures)),
                      str(len(run.documents)), str(len(run.passages)), fams, ", ".join(open_fams) or "-")
    Console(width=220).print(table)


@app.command()
def coverage(
    run_id: str = typer.Argument(...),
    runs_dir: Path = typer.Option(Path("runs"), "--runs"),
    details: bool = typer.Option(True, "--details/--families-only", help="Show per-collector rows under each family"),
) -> None:
    """Print the Coverage Log of one run: one status per family, then the per-collector rows, then the fetch
    log's gate decisions (ToS register, robots.txt, refusals)."""
    from footprint.pipeline import load_run_coverage, load_run_family_status, load_run_fetch_log
    from footprint.sheets import coverage_log_sheet

    try:
        detail_rows = load_run_coverage(run_id, runs_dir)
        summary = load_run_family_status(run_id, runs_dir)
        log = load_run_fetch_log(run_id, runs_dir)
    except OSError as exc:
        raise _fail(f"cannot read run {run_id}: {exc}") from exc
    spec = coverage_log_sheet(detail_rows if details else [], summary=summary)
    table = Table(title=f"Coverage Log {run_id}")
    for h in spec.headers:
        table.add_column(h)
    for row in spec.rows:
        table.add_row(*("" if v is None else str(v) for v in row))
    Console(width=220).print(table)
    if log:
        counts: dict[str, int] = {}
        for ev in log:
            k = f"{ev.get('gate', '')}:{ev.get('decision', '')}"
            counts[k] = counts.get(k, 0) + 1
        typer.echo("Fetch log: " + ", ".join(f"{k} {n}" for k, n in sorted(counts.items())))
        for ev in log:
            if ev.get("decision") in ("blocked_tou", "blocked_robots", "blocked_bot"):
                typer.echo(f"  {ev['decision']}: {ev.get('url', '')} {ev.get('why', '')}".rstrip())


@capture_app.command("import")
def capture_import(
    inbox: Path = typer.Option(Path("evidence/manual_inbox"), "--inbox"),
    evidence: Path = typer.Option(Path("evidence"), "--evidence"),
    analyst: str | None = typer.Option(None, "--analyst", help="Initials recorded as human:<initials>"),
) -> None:
    """Import manual captures from <inbox>/<vendor>/captures.csv into the evidence store."""
    from footprint.capture.manual import import_inbox_report
    from footprint.capture.store import EvidenceStore

    rep = import_inbox_report(EvidenceStore(evidence), inbox, analyst=analyst)
    typer.echo(f"Imported {len(rep.captures)} captures ({rep.duplicates} already imported, "
               f"{len(rep.skipped)} skipped)")
    for s in rep.skipped:
        typer.echo(f"  skipped {s.vendor_id} #{s.nn} {s.url}: {s.note}", err=True)


if __name__ == "__main__":
    app()
