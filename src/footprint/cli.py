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


if __name__ == "__main__":
    app()
