"""footprint.bundle: the deterministic Colab bundle, its secret scan and manifest, and the notebook helpers.

Offline: the bundle tests build from a synthetic tree in tmp_path with a fake wheel; the tests that run the real
`uv build` set UV_OFFLINE=1 and skip when uv is not installed. The real bundle is built once per module, extracted,
and run in fresh interpreters from its own code, as the Colab setup cell does. Secret-looking tokens are assembled at
run time, so this file never holds one.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import openpyxl
import pytest

from footprint import bundle, pipeline
from footprint.bundle import (
    BUNDLE_FORMAT,
    MANIFEST_NAME,
    MAX_BUNDLE_BYTES,
    PLACEHOLDER_TEAM,
    VERIFIED_MARKER,
    ZIP_EPOCH,
    NotebookEnv,
    TextTable,
    WorkbookInput,
    build_bundle,
    build_wheel,
    bundle_files,
    cells_table,
    coverage_frame,
    criticality_frame,
    depth_frame,
    evidence_frame,
    find_root,
    holds_secret,
    is_excluded,
    notebook_setup,
    offer_download,
    output_path,
    progress_printer,
    rationale_table,
    read_inventory,
    read_manifest,
    reverify,
    risk_frame,
    secret_values,
    summary_frame,
    tier_overrides,
    verify_bundle,
    verify_export,
)
from footprint.capture.store import EvidenceStore
from footprint.models import SheetSpec, StudentCells
from footprint.pipeline import assess_example, assess_profiles
from footprint.workbook import read_workbook, write_workbook

REPO = Path(__file__).resolve().parents[2]
INPUT = REPO / "data" / "input" / "Meridian_Vendor_Input.xlsx"
EXPECTED_TIERS = ["High", "Critical", "High", "High", "Critical", "Critical"]

_p = REPO / "tests" / "fixtures" / "p3_samples.py"
_spec = importlib.util.spec_from_file_location("p3_samples", _p)
samples = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("p3_samples", samples)
_spec.loader.exec_module(samples)  # type: ignore[union-attr]


# --------------------------------------------------------------------------- tokens assembled at run time

def google_key() -> str:
    return "AI" + "za" + "Sy" + "B" * 33


def google_aq_key() -> str:
    return "A" + "Q." + "Ab8" + "x" * 47


def github_token() -> str:
    return "gh" + "p_" + "a" * 36


def github_pat() -> str:
    return "github" + "_pat_" + "11" + "B" * 30


DOTENV_SECRET = "zz-dotenv-secret-value-0042"  # matches no pattern: only the exact-value scan finds it
ENV_SECRET = "qq-environment-secret-value-77"


# --------------------------------------------------------------------------- synthetic repository

def tiny_xlsx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


TREE: dict[str, str | bytes] = {
    "pyproject.toml": '[project]\nname = "footprint"\nversion = "9.9.9"\n',
    "uv.lock": "version = 1\n",
    "README.md": "# readme\n",
    "CLAUDE.md": "# rules\n",
    "config/rubric.toml": "version = '1'\n",
    "config/.env": f"GEMINI_API_KEY={DOTENV_SECRET}\n",
    "prompts/.gitkeep": "",
    "seeds/V-001.toml": "aliases = ['Acme']\n",
    "src/footprint/__init__.py": "__version__ = '9.9.9'\n",
    "src/footprint/__pycache__/x.cpython-314.pyc": b"\x00junk",
    "src/footprint.egg-info/PKG-INFO": "Name: footprint\n",
    "app/.gitkeep": "",
    "notebooks/demo.ipynb": "{}\n",
    "notebooks/.ipynb_checkpoints/demo-checkpoint.ipynb": "{}\n",
    "app/.streamlit/config.toml": '[server]\naddress = "localhost"\n',
    "app/.streamlit/secrets.toml": 'k = "v"\n',
    "app/.streamlit/credentials.toml": '[general]\nemail = ""\n',
    "data/input/in.xlsx": tiny_xlsx(),
    "data/input/~$in.xlsx": b"lock",
    "evidence/index.jsonl": '{"capture_id": "ab"}\n',
    "evidence/text/abc.txt": "public vendor text\n",
    "evidence/blobs/ab/abc.gz": gzip.compress(b"<html>public page</html>", mtime=0),
    "evidence/llm_cache/aa/key.json": '{"response": {}}\n',
    "evidence/footprint_bundle_old.zip": b"PK\x03\x04old",
    "runs/V-001-20261002-x/manifest.json": "{}\n",
    "review/reviews.jsonl": "",
    "review/sandbox/reviews.jsonl": "sandbox copy\n",
    "tests/gold/gold_v1.json": "[]\n",
    ".env": f"GEMINI_API_KEY={DOTENV_SECRET}\nFOOTPRINT_TEAM_NAME=Team Test\n",
    ".env.local": "X=1\n",
    ".streamlit/secrets.toml": 'k = "v"\n',
    "scratch/notes.txt": "scratch\n",
    "submission/out.xlsx": tiny_xlsx(),
    ".git/config": "[core]\n",
    ".venv/pyvenv.cfg": "home = x\n",
}

EXPECTED = {
    "pyproject.toml", "uv.lock", "README.md", "CLAUDE.md", "config/rubric.toml", "prompts/.gitkeep",
    "seeds/V-001.toml", "src/footprint/__init__.py", "app/.gitkeep", "app/.streamlit/config.toml",
    "notebooks/demo.ipynb", "data/input/in.xlsx",
    "evidence/index.jsonl", "evidence/text/abc.txt", "evidence/blobs/ab/abc.gz", "evidence/llm_cache/aa/key.json",
    "runs/V-001-20261002-x/manifest.json", "review/reviews.jsonl", "tests/gold/gold_v1.json",
}


def write_tree(root: Path, tree: dict[str, str | bytes] = TREE) -> Path:
    for rel, content in tree.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return root


def fake_wheel(folder: Path, member: bytes = b"__version__ = '9.9.9'\n") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "footprint-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("footprint/__init__.py", member)
    return path


def clean_env(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Remove variables for the test and restore them afterwards (setenv records the original state)."""
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    clean_env(monkeypatch, *bundle.SECRET_ENV_VARS)
    return write_tree(tmp_path / "repo")


@pytest.fixture
def built(tree: Path, tmp_path: Path) -> tuple[Path, dict]:
    out = tmp_path / "out" / "footprint_bundle.zip"
    summary = build_bundle(out, root=tree, wheel=fake_wheel(tmp_path / "wheel"))
    return out, summary


# --------------------------------------------------------------------------- contents and manifest

def test_bundle_holds_exactly_the_replay_pack(built: tuple[Path, dict]) -> None:
    out, _ = built
    names = set(zipfile.ZipFile(out).namelist())
    assert names == EXPECTED | {"dist/footprint-9.9.9-py3-none-any.whl", MANIFEST_NAME}


def test_never_bundles_env_files_or_secret_values(built: tuple[Path, dict]) -> None:
    out, _ = built
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        blobs = [z.read(n) for n in names]
    assert not [n for n in names if n.split("/")[-1].startswith(".env")]
    assert not [n for n in names if n.split("/")[0] in {"scratch", "submission", ".git", ".venv", ".streamlit"}]
    assert not [n for n in names if "__pycache__" in n or "sandbox" in n or n.endswith((".pyc", ".zip"))]
    assert not [n for n in names if n.endswith(("secrets.toml", "credentials.toml"))]
    assert not any(DOTENV_SECRET.encode() in blob for blob in blobs)


def test_manifest_hashes_every_bundled_file(built: tuple[Path, dict]) -> None:
    out, summary = built
    manifest = read_manifest(out)
    with zipfile.ZipFile(out) as z:
        payload = {n: z.read(n) for n in z.namelist() if n != MANIFEST_NAME}
    assert manifest["files"] == {n: hashlib.sha256(d).hexdigest() for n, d in payload.items()}
    assert manifest["format"] == BUNDLE_FORMAT
    assert (manifest["name"], manifest["version"]) == ("footprint", "9.9.9")
    assert manifest["wheel"] == "dist/footprint-9.9.9-py3-none-any.whl"
    assert manifest["replay_packages"] == bundle.package_versions()
    assert manifest["file_count"] == len(payload) == summary["files"]
    assert manifest["total_bytes"] == sum(len(d) for d in payload.values())
    assert summary["path"] == str(out.resolve())
    assert summary["bytes"] == out.stat().st_size
    assert summary["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert summary["wheel"] == manifest["wheel"]


def test_bundle_is_deterministic(tree: Path, tmp_path: Path) -> None:
    wheel = fake_wheel(tmp_path / "wheel")
    first = build_bundle(tmp_path / "a.zip", root=tree, wheel=wheel)
    for path in tree.rglob("*"):
        if path.is_file():
            os.utime(path, (1_900_000_000, 1_900_000_000))
    second = build_bundle(tmp_path / "b.zip", root=tree, wheel=wheel)
    assert first["sha256"] == second["sha256"]
    with zipfile.ZipFile(tmp_path / "a.zip") as z:
        infos = z.infolist()
    names = [i.filename for i in infos]
    assert names == sorted(names)
    assert {i.date_time for i in infos} == {ZIP_EPOCH}
    assert {(i.create_system, i.external_attr >> 16) for i in infos} == {(3, 0o100644)}
    stored = {i.filename for i in infos if i.compress_type == zipfile.ZIP_STORED}
    assert stored == {"data/input/in.xlsx", "evidence/blobs/ab/abc.gz", "dist/footprint-9.9.9-py3-none-any.whl"}


def test_without_wheel_and_with_a_custom_include(tree: Path, tmp_path: Path) -> None:
    summary = build_bundle(tmp_path / "small.zip", root=tree, include=("config", "seeds"), wheel=False)
    assert set(zipfile.ZipFile(tmp_path / "small.zip").namelist()) == {"config/rubric.toml", "seeds/V-001.toml",
                                                                       MANIFEST_NAME}
    assert summary["files"] == 2 and summary["wheel"] == ""


def test_output_zip_inside_the_tree_is_never_bundled(tree: Path) -> None:
    out = tree / "evidence" / "bundle.zip"
    build_bundle(out, root=tree, include=("evidence",), wheel=False)
    build_bundle(out, root=tree, include=("evidence",), wheel=False)  # second build sees the first zip on disk
    assert "evidence/bundle.zip" not in zipfile.ZipFile(out).namelist()


# --------------------------------------------------------------------------- secret scan

@pytest.mark.parametrize("token", [google_key, google_aq_key, github_token, github_pat])
def test_secret_token_refuses_the_build_and_names_only_the_file(tree: Path, tmp_path: Path, token) -> None:
    value = token()
    (tree / "config" / "leak.toml").write_text(f'api = "{value}"\n', encoding="utf-8")
    out = tmp_path / "x.zip"
    with pytest.raises(ValueError) as err:
        build_bundle(out, root=tree, wheel=False)
    assert "config/leak.toml" in str(err.value)
    assert value not in str(err.value)
    assert not out.exists() and not list(tmp_path.glob("*.partial"))


def test_secret_inside_a_gzip_blob_or_the_wheel_is_found(tree: Path, tmp_path: Path) -> None:
    (tree / "evidence" / "blobs" / "ab" / "leak.gz").write_bytes(gzip.compress(google_key().encode(), mtime=0))
    with pytest.raises(ValueError, match="evidence/blobs/ab/leak.gz"):
        build_bundle(tmp_path / "x.zip", root=tree, wheel=False)
    (tree / "evidence" / "blobs" / "ab" / "leak.gz").unlink()
    bad_wheel = fake_wheel(tmp_path / "badwheel", member=f"KEY = '{github_token()}'\n".encode())
    with pytest.raises(ValueError, match="dist/footprint-9.9.9-py3-none-any.whl"):
        build_bundle(tmp_path / "x.zip", root=tree, wheel=bad_wheel)


def test_dotenv_and_environment_values_are_never_bundled(tree: Path, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    (tree / "evidence" / "text" / "leak.txt").write_text(f"oops {DOTENV_SECRET}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence/text/leak.txt") as err:
        build_bundle(tmp_path / "x.zip", root=tree, wheel=False)
    assert DOTENV_SECRET not in str(err.value)
    (tree / "evidence" / "text" / "leak.txt").unlink()

    monkeypatch.setenv("GEMINI_API_KEY", ENV_SECRET)
    (tree / "runs" / "V-001-20261002-x" / "notes.txt").write_text(ENV_SECRET, encoding="utf-8")
    with pytest.raises(ValueError, match="runs/V-001-20261002-x/notes.txt") as err:
        build_bundle(tmp_path / "x.zip", root=tree, wheel=False)
    assert ENV_SECRET not in str(err.value)


def test_reviewed_public_captures_may_embed_a_third_party_key_but_never_our_values(
        tree: Path, tmp_path: Path) -> None:
    """A vendor page that embeds its own public Maps key must not block the bundle once reviewed and listed; the
    .env values are still refused in that file, and token patterns still apply everywhere else."""
    blob = tree / "evidence" / "blobs" / "cd" / "maps.gz"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(gzip.compress(f'<script src="maps.js?key={google_key()}"></script>'.encode(), mtime=0))
    with pytest.raises(ValueError, match="evidence/blobs/cd/maps.gz"):
        build_bundle(tmp_path / "x.zip", root=tree, wheel=False)
    summary = build_bundle(tmp_path / "x.zip", root=tree, wheel=False, allow_tokens_in=["evidence/blobs/cd/maps.gz"])
    assert "evidence/blobs/cd/maps.gz" in zipfile.ZipFile(summary["path"]).namelist()

    blob.write_bytes(gzip.compress(f"maps {DOTENV_SECRET}".encode(), mtime=0))
    with pytest.raises(ValueError, match="evidence/blobs/cd/maps.gz") as err:
        build_bundle(tmp_path / "y.zip", root=tree, wheel=False, allow_tokens_in=["evidence/blobs/*"])
    assert DOTENV_SECRET not in str(err.value)
    blob.unlink()
    (tree / "config" / "leak.toml").write_text(f'k = "{github_token()}"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="config/leak.toml"):
        build_bundle(tmp_path / "z.zip", root=tree, wheel=False, allow_tokens_in=["evidence/blobs/*"])
    assert not (tmp_path / "y.zip").exists() and not (tmp_path / "z.zip").exists()


def test_secret_values_reads_dotenv_keys_but_not_ordinary_settings(tree: Path) -> None:
    values = secret_values(tree)
    assert values == [DOTENV_SECRET.encode()]  # FOOTPRINT_TEAM_NAME is not a secret


def test_token_like_text_that_is_not_a_key_passes() -> None:
    jwt_segment = b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0AQ." + b"x" * 45
    assert not holds_secret(jwt_segment)
    assert not holds_secret(b"AIza too short")
    assert holds_secret(b"key: " + google_aq_key().encode())
    assert holds_secret(b"x", [b"x"])


@pytest.mark.parametrize("arcname, excluded", [
    (".env", True), ("config/.env.production", True), (".env.example", False), ("a/__pycache__/m.pyc", True),
    ("review/sandbox/r.jsonl", True), ("review/reviews.jsonl", False), ("src/footprint.egg-info/x", True),
    ("evidence/footprint_bundle.zip", True), ("deploy/id_rsa", True), ("x/secrets.toml", True),
    ("evidence/text/abc.txt", False), ("data/input/~$Book.xlsx", True),
    # a .streamlit folder gives its config.toml only (localhost, no usage statistics), never secrets or credentials
    ("app/.streamlit/config.toml", False), (".streamlit/config.toml", False), ("app/.streamlit/secrets.toml", True),
    ("app/.streamlit/credentials.toml", True), ("app/.streamlit/sub/config.toml", True),
])
def test_exclusion_rules(arcname: str, excluded: bool) -> None:
    assert is_excluded(arcname) is excluded


# --------------------------------------------------------------------------- limits and errors

def test_size_limit_refuses_and_leaves_nothing(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "big.zip"
    with pytest.raises(ValueError, match="limit"):
        build_bundle(out, root=tree, wheel=False, max_bytes=200)
    assert not out.exists() and not list(tmp_path.glob("*.partial"))


def test_include_entries_must_exist_and_stay_inside_the_root(tree: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found.*missing_dir"):
        build_bundle(tmp_path / "x.zip", root=tree, include=("config", "missing_dir"), wheel=False)
    with pytest.raises(ValueError, match="relative"):
        bundle_files(tree, ("../outside",))
    with pytest.raises(ValueError, match="not a wheel"):
        build_bundle(tmp_path / "x.zip", root=tree, wheel=tmp_path / "nothing.whl")


# --------------------------------------------------------------------------- verification

def test_verify_bundle_zip_and_extracted_folder(built: tuple[Path, dict], tmp_path: Path) -> None:
    out, _ = built
    assert verify_bundle(out) == []
    folder = tmp_path / "extracted"
    zipfile.ZipFile(out).extractall(folder)
    assert verify_bundle(folder) == []
    (folder / "config" / "rubric.toml").write_text("tampered\n", encoding="utf-8")
    (folder / "seeds" / "V-001.toml").unlink()
    (folder / "extra.txt").write_text("not listed\n", encoding="utf-8")
    assert verify_bundle(folder) == ["changed: config/rubric.toml", "missing: seeds/V-001.toml"]


# --------------------------------------------------------------------------- real uv build and real repository

def _uv(monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    monkeypatch.setenv("UV_OFFLINE", "1")  # never reach the network from a unit test


def test_build_wheel_with_uv_is_reproducible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _uv(monkeypatch)
    project = tmp_path / "project"  # a snapshot, so concurrent edits to src/ cannot race the two builds
    shutil.copytree(REPO / "src", project / "src", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(REPO / name, project / name)
    first = build_wheel(project, tmp_path / "a")
    second = build_wheel(project, tmp_path / "b")
    assert first.name.startswith("footprint-") and first.suffix == ".whl"
    assert first.read_bytes() == second.read_bytes()
    names = zipfile.ZipFile(first).namelist()
    assert "footprint/bundle.py" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    """The real repository's bundle, built once with the real (offline) `uv build`."""
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("UV_OFFLINE", "1")
        out = tmp_path_factory.mktemp("real") / "footprint_bundle.zip"
        return out, build_bundle(out, root=REPO)


def test_real_repository_bundle_is_complete_small_and_clean(real_bundle: tuple[Path, dict]) -> None:
    out, summary = real_bundle
    assert summary["bytes"] < MAX_BUNDLE_BYTES
    names = set(zipfile.ZipFile(out).namelist())
    for required in ("evidence/index.jsonl", "data/input/Meridian_Vendor_Input.xlsx", "config/rubric.toml",
                     "seeds/V-001.toml", "src/footprint/bundle.py", "pyproject.toml", MANIFEST_NAME):
        assert required in names
    assert summary["wheel"] in names and summary["wheel"].startswith("dist/footprint-")
    assert any(n.startswith("evidence/text/") for n in names) and any(n.startswith("runs/") for n in names)
    assert not [n for n in names if n.split("/")[-1].startswith(".env") and n.split("/")[-1] != ".env.example"]
    assert not [n for n in names if n.split("/")[0] in {"scratch", "submission", ".git", ".venv"}]
    assert verify_bundle(out) == []
    assert not holds_secret(out.read_bytes(), secret_values(REPO))  # the zip itself, every member included


PROBE = "PROBE "
P1_PROBE = """
import json, os, sys
{path_setup}
import footprint
from footprint.bundle import WorkbookInput, notebook_setup, read_inventory
from footprint.pipeline import assess_profiles

env = notebook_setup(load_secrets=False, team="Team Test")
data = read_inventory(WorkbookInput("data/input/Meridian_Vendor_Input.xlsx"))
print({probe!r} + json.dumps({{"package": footprint.__file__, "cwd": os.getcwd(), "bundle": env.bundle,
                               "tiers": [a.criticality.tier.value for a in assess_profiles(data)]}}))
"""
REPLAY_PROBE = """
import json, sys
{path_setup}
from footprint.pipeline import run_assessment

result = run_assessment("data/input/Meridian_Vendor_Input.xlsx", "replay", vendors=["V-004"], team="Team Test")
print({probe!r} + json.dumps({{vid: cells.model_dump() for vid, cells in result.cells().items()}}, sort_keys=True))
"""


def run_probe(template: str, cwd: Path, src: Path | None = None) -> dict:
    """Run a probe script in a fresh interpreter (no secrets, no override store), as Colab would: with ``src``
    first on sys.path, the code is the bundle's own, not the repository's editable install."""
    path_setup = f"sys.path.insert(0, {str(src)!r})" if src is not None else ""
    env = {k: v for k, v in os.environ.items()
           if k not in {*bundle.SECRET_ENV_VARS, *NOTEBOOK_ENV, "FOOTPRINT_OVERRIDES"}}
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, "-c", template.format(path_setup=path_setup, probe=PROBE)], cwd=cwd,
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=1800, check=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(next(line for line in proc.stdout.splitlines() if line.startswith(PROBE))[len(PROBE):])


CODE_INCLUDE = tuple(entry for entry in bundle.DEFAULT_INCLUDE if entry not in {"evidence", "runs"})
"""The real bundle without the evidence pack and run records: what P1 needs. (Windows Defender scans every freshly
extracted file on first open, which makes the full pack take minutes to verify there; Colab has no such cost.)"""


def test_extracted_bundle_runs_on_its_own_code_config_and_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the Colab setup cell does, without Colab: the bundle's code, config and workbook give the P1 tiers."""
    clean_env(monkeypatch, *bundle.SECRET_ENV_VARS)
    out = tmp_path / "footprint_bundle.zip"
    build_bundle(out, root=REPO, include=CODE_INCLUDE, wheel=False)
    folder = tmp_path / "content" / "footprint"
    with zipfile.ZipFile(out) as archive:
        archive.extractall(folder)
    probe = run_probe(P1_PROBE, folder / "notebooks", src=folder / "src")
    assert Path(probe["package"]).resolve().is_relative_to(folder.resolve())
    assert Path(probe["cwd"]) == folder.resolve()
    assert probe["bundle"].startswith("verified (")
    assert probe["tiers"] == EXPECTED_TIERS


@pytest.mark.skipif(not hasattr(pipeline, "run_assessment"),
                    reason="footprint.pipeline.run_assessment is not implemented yet (P3/P4)")
def test_extracted_bundle_replays_the_same_cells_as_the_repository(
        real_bundle: tuple[Path, dict], tmp_path: Path) -> None:
    """Offline replay from the full bundle alone reproduces the repository's L-V cells (one vendor, to stay quick).
    Slow on Windows (see CODE_INCLUDE)."""
    folder = tmp_path / "content" / "footprint"
    with zipfile.ZipFile(real_bundle[0]) as archive:
        archive.extractall(folder)
    from_bundle = run_probe(REPLAY_PROBE, folder, src=folder / "src")
    from_repo = run_probe(REPLAY_PROBE, REPO)
    assert from_bundle == from_repo and list(from_bundle) == ["V-004"]


# --------------------------------------------------------------------------- notebook setup

def make_root(base: Path) -> Path:
    (base / "src" / "footprint").mkdir(parents=True)
    (base / "config").mkdir()
    return base


@pytest.fixture
def quiet_pandas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle, "_pandas_display", lambda: None)


NOTEBOOK_ENV = ("GEMINI_API_KEY", "FOOTPRINT_TEAM_NAME", "FOOTPRINT_SEC_CONTACT")


def test_find_root_walks_up_and_falls_back_to_the_package(tmp_path: Path) -> None:
    root = make_root(tmp_path / "checkout")
    (root / "notebooks").mkdir()
    assert find_root(root / "notebooks") == root.resolve()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert find_root(elsewhere) == REPO


def test_notebook_setup_local_loads_dotenv_without_showing_values(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, quiet_pandas: None) -> None:
    clean_env(monkeypatch, *NOTEBOOK_ENV)
    root = make_root(tmp_path / "checkout")
    (root / ".env").write_text(f"GEMINI_API_KEY={DOTENV_SECRET}\nFOOTPRINT_TEAM_NAME=Team Test\n", encoding="utf-8")
    (root / "notebooks").mkdir()
    monkeypatch.chdir(root / "notebooks")
    env = notebook_setup()
    assert Path.cwd() == root.resolve()
    assert env.gemini_key and env.team == "Team Test" and not env.colab and env.mode == "replay"
    assert env.bundle.startswith("repository checkout")
    assert os.environ["GEMINI_API_KEY"] == DOTENV_SECRET
    shown = repr(env) + env._repr_html_() + capsys.readouterr().out
    assert DOTENV_SECRET not in shown and "Gemini key: available" in shown


def test_notebook_setup_without_secrets_uses_the_team_placeholder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_pandas: None) -> None:
    clean_env(monkeypatch, *NOTEBOOK_ENV)
    root = make_root(tmp_path / "checkout")
    (root / ".env").write_text(f"GEMINI_API_KEY={DOTENV_SECRET}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    env = notebook_setup(root=root, load_secrets=False)
    assert not env.gemini_key and "GEMINI_API_KEY" not in os.environ
    assert env.team == PLACEHOLDER_TEAM and "placeholder" in repr(env)
    assert notebook_setup(root=root, load_secrets=False, team="Team Given").team == "Team Given"


def test_notebook_setup_live_modes_run_one_vendor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="one vendor"):
        notebook_setup(mode="live_rules", root=tmp_path)
    with pytest.raises(ValueError, match="one vendor"):
        notebook_setup(mode="live_ai", vendors=["V-001", "V-002"], root=tmp_path)
    with pytest.raises(ValueError, match="MODE"):
        notebook_setup(mode="fast", root=tmp_path)


def test_notebook_setup_live_mode_locally_checks_the_extras(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_pandas: None) -> None:
    clean_env(monkeypatch, *NOTEBOOK_ENV)
    root = make_root(tmp_path / "checkout")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bundle, "_missing_modules", lambda names: ["protego"])
    with pytest.raises(RuntimeError, match="uv sync --all-extras"):
        notebook_setup(mode="live_rules", vendors=["V-004"], root=root, load_secrets=False)
    monkeypatch.setattr(bundle, "_missing_modules", lambda names: [])
    env = notebook_setup(mode="live_ai", vendors=["V-004"], root=root, load_secrets=False)
    assert any("falls back to the rules" in note for note in env.notes)


class _SecretNotFound(Exception):
    pass


def fake_colab(monkeypatch: pytest.MonkeyPatch, *, secrets: dict[str, str] | None = None,
               uploads: dict[str, bytes] | None = None) -> list[str]:
    """Install a stand-in google.colab (userdata, files) for one test; returns the list of downloaded paths."""
    downloads: list[str] = []

    def get(name: str) -> str:
        if name not in (secrets or {}):
            raise _SecretNotFound(name)
        return (secrets or {})[name]

    colab = types.ModuleType("google.colab")
    colab.userdata = types.SimpleNamespace(get=get)  # type: ignore[attr-defined]
    colab.files = types.SimpleNamespace(upload=lambda: dict(uploads or {}),  # type: ignore[attr-defined]
                                        download=downloads.append)
    monkeypatch.setitem(sys.modules, "google.colab", colab)
    return downloads


def test_notebook_setup_on_colab_reads_only_the_secrets_the_mode_uses(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, quiet_pandas: None) -> None:
    clean_env(monkeypatch, *NOTEBOOK_ENV)
    fake_colab(monkeypatch, secrets={"GEMINI_API_KEY": ENV_SECRET, "FOOTPRINT_TEAM_NAME": "Team Colab"})
    root = make_root(tmp_path / "content" / "footprint")
    (root / ".env").write_text("FOOTPRINT_TEAM_NAME=Team From Dotenv\n", encoding="utf-8")  # ignored on Colab
    monkeypatch.chdir(tmp_path)
    env = notebook_setup(root=root)
    assert env.colab and env.team == "Team Colab"
    assert not env.gemini_key and "GEMINI_API_KEY" not in os.environ  # replay never pulls the key in
    monkeypatch.setattr(bundle, "_missing_modules", lambda names: [])
    env = notebook_setup(mode="live_ai", vendors=["V-004"], root=root)
    assert env.gemini_key and os.environ["GEMINI_API_KEY"] == ENV_SECRET
    shown = repr(env) + env._repr_html_() + capsys.readouterr().out
    assert ENV_SECRET not in shown and "Google Colab" in shown


def test_colab_secret_is_none_when_missing_or_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_colab(monkeypatch, secrets={"FOOTPRINT_TEAM_NAME": ""})
    assert bundle.colab_secret("GEMINI_API_KEY") is None and bundle.colab_secret("FOOTPRINT_TEAM_NAME") is None


def test_notebook_setup_verifies_an_extracted_bundle_once(
        built: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_pandas: None) -> None:
    clean_env(monkeypatch, *NOTEBOOK_ENV)
    out, _ = built
    folder = tmp_path / "content" / "footprint"
    zipfile.ZipFile(out).extractall(folder)
    monkeypatch.chdir(tmp_path)
    env = notebook_setup(root=folder, load_secrets=False)
    assert env.bundle.startswith("verified (") and (folder / VERIFIED_MARKER).is_file()
    assert notebook_setup(root=folder, load_secrets=False).bundle.startswith("verified at first setup")
    (folder / VERIFIED_MARKER).unlink()
    (folder / "seeds" / "V-001.toml").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match its manifest"):
        notebook_setup(root=folder, load_secrets=False)


def colab_bundle(tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[list[str]]]:
    """An extracted bundle built where trafilatura 2.2.0 and pypdf 6.0.0 were installed, on a fake Colab runtime
    that has pypdf 6.0.0 only and has imported none of the replay packages; returns (folder, pip installs)."""
    clean_env(monkeypatch, *NOTEBOOK_ENV)
    monkeypatch.setattr(bundle, "package_versions", lambda names=(): {"pypdf": "6.0.0", "trafilatura": "2.2.0"})
    out = tmp_path / "footprint_bundle.zip"
    build_bundle(out, root=tree, wheel=fake_wheel(tmp_path / "wheel"))
    folder = tmp_path / "content" / "footprint"
    zipfile.ZipFile(out).extractall(folder)
    fake_colab(monkeypatch)
    installs: list[list[str]] = []
    monkeypatch.setattr(bundle, "_pip_install", installs.append)
    monkeypatch.setattr(bundle, "package_versions", lambda names=(): {"pypdf": "6.0.0"})  # the Colab runtime
    for module in set(bundle.REPLAY_PACKAGES.values()) - ALWAYS_IMPORTED:
        monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.chdir(tmp_path)
    return folder, installs


ALWAYS_IMPORTED = {"pydantic", "pydantic_core"}
"""Replay packages that footprint itself imports, so a notebook kernel has always loaded them before the setup runs
(and the tests never unload them)."""


def test_colab_setup_installs_what_the_runtime_lacks(tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     quiet_pandas: None) -> None:
    folder, installs = colab_bundle(tree, tmp_path, monkeypatch)
    env = notebook_setup(root=folder)
    assert installs == [["trafilatura==2.2.0"]]
    assert any("build machine" in note for note in env.notes)

    installs.clear()
    missing = iter([["protego"], []])  # missing before the install, present after it
    monkeypatch.setattr(bundle, "_missing_modules", lambda names: next(missing))
    env = notebook_setup(mode="live_rules", vendors=["V-004"], root=folder, load_secrets=False)
    wheel = folder / "dist" / "footprint-9.9.9-py3-none-any.whl"
    assert installs == [[f"{wheel}[live,ai]", "trafilatura==2.2.0"]]
    assert any("playwright install" in note for note in env.notes)


def test_colab_setup_asks_for_a_restart_when_a_replaced_package_was_imported(
        tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_pandas: None) -> None:
    folder, installs = colab_bundle(tree, tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "trafilatura", types.ModuleType("trafilatura"))  # the old version, loaded
    with pytest.raises(RuntimeError, match="restart the session") as err:
        notebook_setup(root=folder)
    assert installs == [["trafilatura==2.2.0"]] and "trafilatura" in str(err.value)
    monkeypatch.setitem(sys.modules, "pypdf", types.ModuleType("pypdf"))  # loaded, but already the right version
    monkeypatch.delitem(sys.modules, "trafilatura")
    assert notebook_setup(root=folder).colab


def test_replay_packages_cover_every_text_extractor() -> None:
    """Every library whose version can change extracted text (and so doc ids and quotes) is recorded and pinned."""
    assert {"trafilatura", "lxml", "pypdf", "htmldate", "charset-normalizer", "protego"} <= set(bundle.REPLAY_PACKAGES)
    for name, module in bundle.REPLAY_PACKAGES.items():
        assert module.isidentifier(), name
    assert set(bundle.package_versions()) <= set(bundle.REPLAY_PACKAGES)


def test_replay_pins_pydantic_because_llm_cache_keys_hash_its_schemas(tree: Path, tmp_path: Path) -> None:
    """The cache key holds the SHA-256 of the response schema pydantic generates at run time: a Colab kernel with
    another pydantic could miss every cached reply. The bundle pins pydantic and its core, and records the hashes."""
    from importlib import metadata

    from footprint import ai

    assert bundle.REPLAY_PACKAGES["pydantic"] == "pydantic"
    assert bundle.REPLAY_PACKAGES["pydantic-core"] == "pydantic_core"
    versions = bundle.package_versions()
    assert versions["pydantic"] == metadata.version("pydantic")
    assert versions["pydantic-core"] == metadata.version("pydantic-core")
    hashes = bundle.llm_schema_hashes()
    assert hashes == {"expand": ai.schema_sha256(ai.EXPAND_SCHEMA), "extract": ai.schema_sha256(ai.EXTRACT_SCHEMA),
                      "triage": ai.schema_sha256(ai.TRIAGE_SCHEMA)}
    out = tmp_path / "b.zip"
    build_bundle(out, root=tree, wheel=False)
    manifest = read_manifest(out)
    assert manifest["llm_schemas"] == hashes
    assert manifest["replay_packages"]["pydantic"] == versions["pydantic"]


def test_colab_setup_pins_pydantic_and_asks_for_a_restart(tree: Path, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch, quiet_pandas: None) -> None:
    """Colab's preinstalled pydantic differs from the build machine's: the setup installs the recorded version and,
    because footprint has imported pydantic already, asks for a restart; after it, nothing is installed."""
    clean_env(monkeypatch, *NOTEBOOK_ENV)
    built_with = {"pydantic": "2.13.5", "pydantic-core": "2.41.5", "pypdf": "6.0.0"}
    monkeypatch.setattr(bundle, "package_versions", lambda names=(): dict(built_with))
    out = tmp_path / "footprint_bundle.zip"
    build_bundle(out, root=tree, wheel=fake_wheel(tmp_path / "wheel"))
    folder = tmp_path / "content" / "footprint"
    zipfile.ZipFile(out).extractall(folder)
    fake_colab(monkeypatch)
    installs: list[list[str]] = []
    monkeypatch.setattr(bundle, "_pip_install", installs.append)
    colab = {"pydantic": "2.11.9", "pydantic-core": "2.33.2", "pypdf": "6.0.0"}
    monkeypatch.setattr(bundle, "package_versions", lambda names=(): {n: colab[n] for n in names if n in colab})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="restart the session") as err:
        notebook_setup(root=folder)
    assert installs == [["pydantic==2.13.5", "pydantic-core==2.41.5"]]
    assert "pydantic" in str(err.value) and "pydantic_core" in str(err.value)
    installs.clear()
    colab.update(built_with)  # after the restart
    env = notebook_setup(root=folder)
    assert installs == [] and not [n for n in env.notes if n.startswith("WARNING")]


def test_setup_warns_when_the_llm_schemas_differ_from_the_bundle(built: tuple[Path, dict], tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch,
                                                                 quiet_pandas: None) -> None:
    clean_env(monkeypatch, *NOTEBOOK_ENV)
    out, _ = built
    folder = tmp_path / "content" / "footprint"
    zipfile.ZipFile(out).extractall(folder)
    monkeypatch.chdir(tmp_path)
    assert bundle.schema_drift(folder) == [] and bundle.schema_drift(tmp_path) == []
    env = notebook_setup(root=folder, load_secrets=False)
    assert not [n for n in env.notes if n.startswith("WARNING")]
    manifest = read_manifest(folder)
    manifest["llm_schemas"]["extract"] = "0" * 64  # as if another pydantic had generated the schema
    (folder / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert bundle.schema_drift(folder) == ["extract"]
    env = notebook_setup(root=folder, load_secrets=False)
    warning = next(n for n in env.notes if n.startswith("WARNING"))
    assert "extract" in warning and "cached Gemini reply" in warning and "pydantic==" in warning


# --------------------------------------------------------------------------- workbook input

def test_workbook_input_defaults_to_the_path() -> None:
    wb = WorkbookInput(INPUT)
    assert wb.source() == INPUT and wb.name == INPUT.name
    assert wb.sha256() == hashlib.sha256(INPUT.read_bytes()).hexdigest()
    assert "from" in repr(wb) and wb.sha256()[:16] in repr(wb)
    with pytest.raises(FileNotFoundError):
        WorkbookInput("no/such/file.xlsx").source()


@pytest.mark.parametrize("value", [
    ({"name": "upload.xlsx", "content": memoryview(b"uploaded bytes")},),        # ipywidgets 8
    {"upload.xlsx": {"metadata": {}, "content": b"uploaded bytes"}},          # ipywidgets 7
])
def test_workbook_input_reads_the_widget_upload(value: object) -> None:
    pytest.importorskip("ipywidgets")
    wb = WorkbookInput(INPUT, ask=True)
    assert wb.widget is not None and wb.source() == INPUT  # nothing picked yet
    wb.widget = types.SimpleNamespace(value=value)
    assert wb.source() == b"uploaded bytes" and wb.name == "upload.xlsx"


def test_workbook_input_on_colab_uses_the_file_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_colab(monkeypatch, uploads={"notes.txt": b"x", "Vendors.xlsx": b"xlsx bytes"})
    wb = WorkbookInput(INPUT, ask=True)
    assert wb.source() == b"xlsx bytes" and wb.name == "Vendors.xlsx"
    fake_colab(monkeypatch, uploads={})
    wb = WorkbookInput(INPUT, ask=True)
    assert wb.source() == INPUT and "No .xlsx uploaded" in repr(wb)


def test_read_inventory_validates_and_reports(capsys: pytest.CaptureFixture) -> None:
    data = read_inventory(WorkbookInput(INPUT))
    assert [v.vendor_id for v in data.vendors] == [f"V-00{i}" for i in range(1, 7)]
    assert "6 vendors" in capsys.readouterr().out
    with pytest.raises(ValueError, match="failed validation"):
        read_inventory(b"not a workbook")
    assert "E01" in capsys.readouterr().out


def test_tier_overrides_follow_the_review_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOOTPRINT_OVERRIDES", str(tmp_path / "missing.jsonl"))
    assert tier_overrides() is None
    (tmp_path / "overrides.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("FOOTPRINT_OVERRIDES", str(tmp_path / "overrides.jsonl"))
    assert tier_overrides().path == tmp_path / "overrides.jsonl"


# --------------------------------------------------------------------------- tables

@pytest.fixture(scope="module")
def p1() -> tuple[list, object]:
    data = read_workbook(INPUT)
    return assess_profiles(data), assess_example(data)


def test_criticality_and_depth_frames(p1: tuple[list, object]) -> None:
    assessments, example = p1
    frame = criticality_frame(assessments, example=example)
    assert list(frame["Tier (L)"])[:6] == EXPECTED_TIERS
    assert list(frame["Vendor ID"]) == [f"V-00{i}" for i in range(1, 7)] + ["V-000 (calibration)"]
    assert frame.loc[0, "D"] == "3-P"  # AutomWorx: privileged production access (D3-P)
    assert frame.loc[4, "Floors"].startswith("F2")  # BNY: payment-path floor
    depth = depth_frame(assessments)
    assert list(depth["Tier"]) == EXPECTED_TIERS
    assert depth.loc[0, "Modifiers"] == "M-D3P" and "LEG (full" in depth.loc[0, "Mandatory families"]
    text = rationale_table(assessments)
    assert "V-005" in repr(text) and "<td" in text._repr_html_()


def evidenced_findings(**kw):
    item = samples.item(role="Primary", evidence_id="V-901-E-0001")
    verdict = samples.verdict(keys=[item.item_key])
    return samples.findings(evidence=[item], verdict=verdict, cells=StudentCells(ai_usage_detected="Yes"), **kw)


def test_result_frames(tmp_path: Path) -> None:
    result = samples.assessment(vendors=[evidenced_findings()])
    summary = summary_frame(result)
    assert summary.loc[0, "AI usage (O)"] == "Yes" and summary.loc[0, "Decisive evidence"] == "V-901-E-0001"
    assert summary.loc[0, "AI risk (S)"] == "High" and summary.loc[0, "Score (ARP of 18)"] == 12
    evidence = evidence_frame(result)
    assert list(evidence["Evidence ID"]) == ["V-901-E-0001"]
    assert evidence.loc[0, "Excerpt"] == samples.EXCERPT  # never shortened
    assert evidence.loc[0, "Tags"].startswith("U1 · SR:B")
    assert evidence_frame(result, "V-999").empty
    risk = risk_frame(result)
    assert (risk.loc[0, "E (exposure)"], risk.loc[0, "ARP"], risk.loc[0, "Missing checks"]) == ("2", 12, "t2, t3, t6")
    coverage = coverage_frame(result)
    assert list(coverage["Coverage ID"]) == ["V-901-C-01"] and coverage.loc[0, "Status"] == "done"
    cells = cells_table(result, "V-901")
    assert [row[0] for row in cells.rows] == list("LMNOPQRSTUV")
    assert cells.rows[3][2] == "Yes" and cells.rows[3][1] == "AI Usage Detected (Y/N)"
    assert cells_table(result, "V-999").title.startswith("V-901")


def test_unassigned_evidence_ids_fall_back_to_the_item_key() -> None:
    item = samples.item(role="Primary")
    findings = samples.findings(evidence=[item], verdict=samples.verdict(keys=[item.item_key]))
    frame = summary_frame(samples.assessment(vendors=[findings]))
    assert frame.loc[0, "Decisive evidence"] == item.item_key[:12]


def test_text_table_escapes_html() -> None:
    table = TextTable(["A"], [["<script>alert(1)</script>"], [None]], title="t & u")
    assert "<script>" not in table._repr_html_() and "&lt;script&gt;" in table._repr_html_()
    assert "t &amp; u" in table._repr_html_() and "A: " in repr(table)
    assert list(table.to_frame()["A"]) == ["<script>alert(1)</script>", ""]


def test_progress_printer(capsys: pytest.CaptureFixture) -> None:
    report = progress_printer()
    report("V-001 collected", 0.25)
    report("done", 1.5)
    assert capsys.readouterr().out.splitlines() == ["[ 25.0%] V-001 collected", "[100.0%] done"]


def test_output_path_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture) -> None:
    result = samples.assessment()
    path = output_path(tmp_path / "out", result)
    assert path.parent.is_dir() and path.name == f"Meridian_Vendor_Assessment_{result.run_id}.xlsx"
    with pytest.raises(FileNotFoundError):
        offer_download(path)
    path.write_bytes(b"xlsx")
    link = offer_download(path)
    assert str(path.resolve()) in capsys.readouterr().out and link is not None
    downloads = fake_colab(monkeypatch)
    assert "Downloading" in offer_download(path) and downloads == [str(path)]


# --------------------------------------------------------------------------- verify_export

def stored_item(store: EvidenceStore, vendor_id: str):
    capture = store.put_raw(b"<html>raw page</html>", vendor_id=vendor_id, family="PRD", collector="site",
                            url_requested=samples.URL, retrieved_at="2026-10-02T12:00:00Z")
    doc_id, _ = store.put_text(samples.DOC_TEXT)
    return samples.item(vendor_id=vendor_id, capture_id=capture.capture_id, doc_id=doc_id, role="Primary",
                        evidence_id=f"{vendor_id}-E-0001")


def real_result(store: EvidenceStore) -> tuple:
    """A synthetic AssessmentResult over the real workbook's vendors, plus the cells written for them."""
    data = read_workbook(INPUT)
    vendors, cells = [], {}
    for a in assess_profiles(data):
        vid = a.profile.vendor_id
        item = stored_item(store, vid)
        inputs = samples.risk_inputs(e_items=[item.item_key], k_items=[item.item_key])
        texts = {name: f"{vid} {name} text" for name in StudentCells.model_fields}
        texts.update(criticality_tier=a.criticality.tier.value, ai_usage_detected="Yes", ai_risk_class="High")
        c = StudentCells(**texts)
        cells[vid] = c
        vendors.append(samples.findings(
            profile=a.profile, criticality=a.criticality, coverage=[samples.coverage(vendor_id=vid)],
            evidence=[item], verdict=samples.verdict(keys=[item.item_key]),
            risk=samples.risk_result(inputs=inputs), cells=c))
    result = samples.assessment(vendors=vendors, input_sha256=hashlib.sha256(INPUT.read_bytes()).hexdigest())
    return result, cells


SHEETS = [SheetSpec(title="Evidence Log", headers=["Evidence ID"]), SheetSpec(title="Coverage Log", headers=["ID"])]


def test_verify_export_passes_on_a_faithful_export(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    result, cells = real_result(store)
    out = tmp_path / "out.xlsx"
    write_workbook(INPUT, out, cells, SHEETS)
    checks = verify_export(result, WorkbookInput(INPUT), out, reference=out, store=store)
    assert checks.attrs["ok"], checks.to_dict("records")
    assert list(checks["Result"]) == ["PASS"] * 6
    assert "6 of 6 items" in checks.loc[4, "Detail"]


def test_verify_export_fails_on_tampering(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    result, cells = real_result(store)
    out = tmp_path / "out.xlsx"
    write_workbook(INPUT, out, cells, SHEETS[:1])
    book = openpyxl.load_workbook(out, rich_text=True)
    book["Vendor Inventory"]["U6"] = "edited by hand"
    book["Vendor Inventory"]["C7"] = "renamed vendor"
    book.save(out)
    text_file = next((tmp_path / "evidence" / "text").glob("*.txt"))
    text_file.write_bytes(text_file.read_bytes().replace(b"machine learning", b"rules engine...."))
    checks = verify_export(result, b"not the input", out, store=store)
    outcome = dict(zip(checks["Check"], checks["Result"]))
    assert not checks.attrs["ok"]
    assert outcome["Input is the workbook that was assessed"] == "FAIL"
    assert outcome["Exported L-V cells equal the assessment"] == "FAIL"
    assert outcome["Evidence Log and Coverage Log sheets present"] == "FAIL"
    assert outcome["Cited evidence re-verifies (capture and text SHA-256, exact quote)"] == "FAIL"
    fidelity = verify_export(result, INPUT, out, store=store)
    assert dict(zip(fidelity["Check"], fidelity["Result"]))[
        "Provided cells, V-000, styles and sheets unchanged"] == "FAIL"


def test_reverify_checks_hashes_and_the_exact_slice(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    item = stored_item(store, "V-901")
    assert reverify(item, store) == []
    moved = item.model_copy(update={"doc_id": "e" * 64, "text_sha256": "e" * 64})
    assert reverify(moved, store)


def test_notebook_env_lines_never_hold_values() -> None:
    env = NotebookEnv(root="r", colab=False, mode="replay", team="Team X", gemini_key=True, bundle="b",
                      notes=("n",))
    assert "Gemini key: available" in repr(env) and "Note: n" in repr(env)
