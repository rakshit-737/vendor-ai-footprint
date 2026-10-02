"""Command-line interface for footprint."""

import typer

from footprint import __version__

app = typer.Typer(no_args_is_help=True, help="Vendor AI public-footprint analysis (Optiv case study 1).")


@app.callback()
def main() -> None:
    """Vendor AI public-footprint analysis (Optiv case study 1)."""


@app.command()
def version() -> None:
    """Print the footprint version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
