"""notebooks/footprint_demo.ipynb: structure (stripped outputs, calls only, no secret handling) and execution.

The execution tests start a local ipykernel through nbclient and never touch the network.
- One runs every cell. Setup and the P1 cells (criticality, depth) run for real; a stand-in replaces the P3/P4
  pipeline calls (run_assessment, export_assessment), so the results, export, download and verify cells are
  exercised before that pipeline exists.
- The other runs the real pipeline in replay mode, top to bottom. It is skipped until
  footprint.pipeline.run_assessment exists.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from footprint.bundle import holds_secret

nbformat = pytest.importorskip("nbformat")

REPO = Path(__file__).resolve().parents[2]
NOTEBOOK = REPO / "notebooks" / "footprint_demo.ipynb"
EXPECTED_TIERS = ["High", "Critical", "High", "High", "Critical", "Critical"]
STEP_TAGS = ["parameters", "setup", "upload", "p1", "p1", "p1", "run", "results", "results", "results", "results",
             "results", "export", "verify"]
CELL_TIMEOUT_S = 1800
PROBE = "PROBE "
KERNEL = "footprint-tests"
# pyzmq on Windows: the Proactor loop lacks add_reader, so tornado adds a selector thread (harmless).
pytestmark = pytest.mark.filterwarnings("ignore:Proactor event loop does not implement add_reader:RuntimeWarning")

FORBIDDEN_NODES = (
    ast.For, ast.AsyncFor, ast.While, ast.If, ast.With, ast.AsyncWith, ast.Try, ast.FunctionDef, ast.AsyncFunctionDef,
    ast.ClassDef, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.IfExp, ast.BoolOp,
    ast.Compare, ast.BinOp, ast.Subscript, ast.Raise, ast.Assert, ast.Delete, ast.Global, ast.Nonlocal,
)


def load():
    return nbformat.read(NOTEBOOK, as_version=4)


def tag(cell) -> str:
    tags = cell.metadata.get("tags", [])
    return tags[0] if tags else ""


def code_cells(nb) -> list:
    return [c for c in nb.cells if c.cell_type == "code"]


# --------------------------------------------------------------------------- structure


def test_notebook_is_valid_utf8_lf_and_outputs_are_stripped() -> None:
    raw = NOTEBOOK.read_bytes()
    raw.decode("utf-8")
    assert b"\r\n" not in raw
    nb = load()
    nbformat.validate(nb)
    assert nb.metadata["kernelspec"]["name"] == "python3"
    assert "widgets" not in nb.metadata
    for cell in code_cells(nb):
        assert cell.outputs == [] and cell.execution_count is None, cell.id


def test_notebook_walks_every_demo_step_in_order() -> None:
    assert [tag(c) for c in code_cells(load())] == STEP_TAGS


def test_cells_are_calls_only() -> None:
    """Logic lives in footprint.bundle and footprint.pipeline; only the setup bootstrap may branch (Colab)."""
    for cell in code_cells(load()):
        if tag(cell) == "setup":
            continue
        tree = ast.parse(cell.source)
        bad = sorted({type(node).__name__ for node in ast.walk(tree) if isinstance(node, FORBIDDEN_NODES)})
        assert not bad, f"cell {cell.id} holds logic: {bad}"


def test_parameters_cell_only_assigns_constants() -> None:
    cell = next(c for c in code_cells(load()) if tag(c) == "parameters")
    for statement in ast.parse(cell.source).body:
        assert isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name)
        assert isinstance(statement.value, (ast.Constant, ast.List)), ast.dump(statement)
    assert "MODE = \"replay\"" in cell.source  # replay is the default: offline and reproducible


def test_no_secret_is_typed_read_or_printed_in_a_cell() -> None:
    nb = load()
    code = "\n".join(c.source for c in code_cells(nb))
    for forbidden in ("GEMINI_API_KEY", "environ", "getpass", "input(", "userdata", "print(env"):
        assert forbidden not in code, forbidden
    assert not holds_secret(NOTEBOOK.read_bytes())


def test_colab_setup_installs_the_bundled_wheel_without_tokens() -> None:
    setup = next(c for c in code_cells(load()) if tag(c) == "setup").source
    for needed in ("files.upload()", "%pip install -q /content/footprint/dist/footprint-", "sys.path.insert(0",
                   "notebook_setup(mode=MODE, vendors=VENDORS, team=TEAM, load_secrets=LOAD_SECRETS)"):
        assert needed in setup
    for forbidden in ("git clone", "github.com", "token", "GITHUB"):
        assert forbidden not in setup


# --------------------------------------------------------------------------- execution


def with_parameters(nb, **values) -> None:
    cell = next(c for c in code_cells(nb) if tag(c) == "parameters")
    cell.source += "\n" + "\n".join(f"{name} = {value!r}" for name, value in values.items())


def add_probe(nb, expression: str) -> None:
    nb.cells.append(nbformat.v4.new_code_cell(f"import json\nprint({PROBE!r} + json.dumps({expression}))"))


def pin_kernel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """A kernelspec that runs this interpreter (the project venv), ahead of any user-level "python3" spec."""
    spec = tmp_path / "jupyter" / "kernels" / KERNEL
    spec.mkdir(parents=True)
    kernel = {"argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
              "display_name": "footprint tests", "language": "python", "env": {"PYTHONUTF8": "1"}}
    (spec / "kernel.json").write_text(json.dumps(kernel), encoding="utf-8")
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "jupyter"))
    return KERNEL


def execute(nb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Run the notebook in a fresh local kernel started in notebooks/; return the probe's JSON."""
    nbclient = pytest.importorskip("nbclient")
    pytest.importorskip("ipykernel")
    monkeypatch.setenv("FOOTPRINT_OVERRIDES", str(tmp_path / "no_overrides.jsonl"))  # no analyst tier overrides
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)  # the kernel inherits the environment: replay needs no key
    client = nbclient.NotebookClient(nb, timeout=CELL_TIMEOUT_S, kernel_name=pin_kernel(monkeypatch, tmp_path),
                                     resources={"metadata": {"path": str(NOTEBOOK.parent)}})
    client.execute()
    for cell in code_cells(nb):
        errors = [o for o in cell.get("outputs", []) if o.get("output_type") == "error"]
        assert not errors, (cell.id, errors)
    lines = [line for o in nb.cells[-1].outputs if o.get("name") == "stdout" for line in o["text"].splitlines()]
    return json.loads(next(line for line in lines if line.startswith(PROBE))[len(PROBE):])


STAND_IN_PIPELINE = """
import hashlib, importlib.util
from pathlib import Path
import footprint.pipeline as _pipeline
from footprint.models import SheetSpec, StudentCells
from footprint.workbook import read_workbook, write_workbook

_spec = importlib.util.spec_from_file_location("p3_samples", {samples!r})
_samples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_samples)

def _run(xlsx, mode="replay", *, vendors=None, as_of=None, team=None, progress=None, **_):
    raw = xlsx if isinstance(xlsx, bytes) else Path(xlsx).read_bytes()
    found = []
    for a in _pipeline.assess_profiles(read_workbook(raw)):
        vid = a.profile.vendor_id
        if vendors and vid not in vendors:
            continue
        cells = StudentCells(criticality_tier=a.criticality.tier.value, ai_usage_detected="Inconclusive",
                             ai_risk_class="Medium", assessed_by=f"{{team}} / 02-10-2026")
        found.append(_samples.findings(
            profile=a.profile, criticality=a.criticality, coverage=[_samples.coverage(vendor_id=vid)], evidence=[],
            verdict=_samples.verdict(keys=[], rule="f", likelihood="unlikely", confidence="Low",
                                     confidence_reason="stand-in pipeline"),
            risk=_samples.risk_result(inputs=_samples.risk_inputs(e_items=[], k_items=[])), cells=cells))
        progress(vid, len(found) / 6)
    return _samples.assessment(vendors=found, input_sha256=hashlib.sha256(raw).hexdigest(), mode=mode)

def _export(result, xlsx_in, out_path, team, *, overwrite=False):
    sheets = [SheetSpec(title="Evidence Log", headers=["Evidence ID"]),
              SheetSpec(title="Coverage Log", headers=["Coverage ID"])]
    return write_workbook(xlsx_in, out_path, result.cells(), sheets, last_modified_by=team)

_pipeline.run_assessment = _run
_pipeline.export_assessment = _export
"""


def test_every_cell_executes_against_a_stand_in_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup and the P1 cells run for real; the run and export cells use a stand-in for the P3/P4 pipeline, so the
    results, export, download and verify cells are exercised before run_assessment exists."""
    nb = load()
    with_parameters(nb, ASK_UPLOAD=False, LOAD_SECRETS=False, TEAM="Test Team", OUT_DIR=str(tmp_path / "out"))
    cells = code_cells(nb)
    position = next(i for i, c in enumerate(cells) if tag(c) == "setup") + 1
    samples = str(REPO / "tests" / "fixtures" / "p3_samples.py")
    nb.cells = [*cells[:position], nbformat.v4.new_code_cell(STAND_IN_PIPELINE.format(samples=samples)),
                *cells[position:]]
    add_probe(nb, '{"tiers": [a.criticality.tier.value for a in assessments], "cwd": os.getcwd(), '
                  '"colab": env.colab, "team": env.team, "workbook": workbook.name, "mode": env.mode, '
                  '"ok": bool(checks.attrs["ok"]), "results": list(checks["Result"]), '
                  '"exists": out_path.is_file(), "vendors": [f.vendor_id for f in result.vendors]}')
    probe = execute(nb, monkeypatch, tmp_path)
    assert {k: probe[k] for k in ("tiers", "cwd", "colab", "team", "workbook", "mode")} == {
        "tiers": EXPECTED_TIERS, "cwd": str(REPO), "colab": False, "team": "Test Team",
        "workbook": "Meridian_Vendor_Input.xlsx", "mode": "replay"}
    assert all(any("text/html" in o.get("data", {}) for o in c.outputs) for c in nb.cells if tag(c) == "p1")
    assert probe["vendors"] == [f"V-00{i}" for i in range(1, 7)] and probe["exists"]
    assert probe["ok"] and probe["results"] == ["PASS", "PASS", "PASS", "PASS", "SKIP"]
    shown = "".join(o.get("data", {}).get("text/plain", "") for c in nb.cells if tag(c) == "results" for o in c.outputs)
    assert "V-005" in shown and "AI Usage Detected" in shown


def test_notebook_runs_top_to_bottom_in_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from footprint import pipeline

    if not hasattr(pipeline, "run_assessment"):
        pytest.skip("footprint.pipeline.run_assessment is not implemented yet (P3/P4)")
    nb = load()
    with_parameters(nb, ASK_UPLOAD=False, LOAD_SECRETS=False, TEAM="Test Team", OUT_DIR=str(tmp_path / "out"),
                    VENDORS=["V-004"], SHOW_VENDOR="V-004")
    nb.cells = code_cells(nb)
    add_probe(nb, '{"ok": bool(checks.attrs["ok"]), "failed": checks[checks["Result"] == "FAIL"].to_dict("records"), '
                  '"out": str(out_path.resolve()), "exists": out_path.is_file(), "mode": result.mode, '
                  '"vendors": [f.vendor_id for f in result.vendors]}')
    probe = execute(nb, monkeypatch, tmp_path)
    assert probe["ok"], probe["failed"]
    assert probe["exists"] and Path(probe["out"]).parent == (tmp_path / "out").resolve()
    assert probe["mode"] == "replay" and probe["vendors"] == ["V-004"]
