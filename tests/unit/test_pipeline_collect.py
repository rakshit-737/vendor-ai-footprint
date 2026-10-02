"""Offline integration: collect_vendor in replay mode over a temporary evidence store."""

from __future__ import annotations

import json

from footprint.capture.store import EvidenceStore
from footprint.models import CoverageStatus, SourceFamily
from footprint.net.fetcher import ReplayFetcher
from footprint.pipeline import CollectionRun, collect_vendor, load_run_coverage
from footprint.sheets import coverage_log_sheet
import importlib.util
import sys
from pathlib import Path

_p = Path(__file__).resolve().parents[1] / "fixtures" / "collectors" / "_fakes.py"
_spec = importlib.util.spec_from_file_location("collector_fakes", _p)
_fakes = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("collector_fakes", _fakes)
_spec.loader.exec_module(_fakes)
plan, profile = _fakes.plan, _fakes.profile

HOME = "https://www.automworx.com/"
AI_PAGE = "https://www.automworx.com/ai"
SEED_TOML = f'''vendor_id = "V-001"
domains = ["automworx.com"]
[ats]
platform = "none"
[[seed]]
url = "{AI_PAGE}"
family = "PRD"
automation = "auto"
[[seed]]
url = "https://www.linkedin.com/posts/x"
family = "EXEC"
automation = "manual"
'''


def _assessment():
    from types import SimpleNamespace

    return SimpleNamespace(profile=profile(), depth=plan(caps={f: 5 for f in SourceFamily}))


def test_collect_vendor_replay(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    html = (b"<html><head><title>AI</title></head><body><p>Our team builds tools.</p>"
            b"<p>We use generative AI and machine learning to automate batch scheduling.</p>"
            b"<p>Contact us.</p></body></html>")
    for u in (AI_PAGE, HOME):
        store.put_raw(html, vendor_id="V-001", family=SourceFamily.PRD, collector="seeds", url_requested=u,
                      status=200, content_type="text/html; charset=utf-8", retrieved_at="2026-10-02T00:00:00Z",
                      robots_decision="allowed")
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    (seeds / "V-001.toml").write_text(SEED_TOML, encoding="utf-8")
    runs = tmp_path / "runs"
    run = collect_vendor(_assessment(), "replay", seeds, store, fetcher=ReplayFetcher(store), runs_dir=runs,
                         as_of="2026-10-02")
    assert isinstance(run, CollectionRun)
    assert run.run_id.startswith("V-001-20261002-")
    assert run.documents and run.passages
    assert any("generative AI" in p.text for p in run.passages)
    fams = {e.family for e in run.coverage}
    assert set(SourceFamily) <= fams  # every planned family has a status
    seed_prd = [e for e in run.coverage if e.collector == "seeds" and e.family == SourceFamily.PRD]
    assert seed_prd and seed_prd[0].status == CoverageStatus.DONE and seed_prd[0].ai_passages >= 1
    assert "https://www.linkedin.com/posts/x" in run.leads
    d = runs / run.run_id
    for name in ("manifest.json", "coverage.jsonl", "captures.jsonl", "passages.jsonl"):
        assert (d / name).exists()
    assert json.loads((d / "manifest.json").read_text(encoding="utf-8"))["counts"]["passages"] == len(run.passages)
    cov = load_run_coverage(run.run_id, runs)
    assert len(cov) == len(run.coverage)
    sheet = coverage_log_sheet(cov)
    assert sheet.title == "Coverage Log" and len(sheet.rows) == len(cov)
    # deterministic run id
    again = collect_vendor(_assessment(), "replay", seeds, store, fetcher=ReplayFetcher(store), write=False,
                           as_of="2026-10-02")
    assert again.run_id == run.run_id


def test_crashing_collector_is_logged(tmp_path):
    class Boom:
        name, family = "boom", SourceFamily.PRD

        def applies(self, *a):
            return True

        def collect(self, ctx):
            raise RuntimeError("x")

    store = EvidenceStore(tmp_path / "e")
    run = collect_vendor(_assessment(), "replay", tmp_path, store, fetcher=ReplayFetcher(store), collectors=[Boom()],
                         write=False)
    assert any(e.collector == "boom" and e.status == CoverageStatus.ERROR for e in run.coverage)
