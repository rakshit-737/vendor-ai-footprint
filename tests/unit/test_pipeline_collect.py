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


# --------------------------------------------------------------------------- family status (first live run)

def _e(fam, status, collector, note="", req=0, docs=0, passages=0):
    from footprint.models import CoverageEntry

    return CoverageEntry(vendor_id="V-001", family=fam, mandatory=True, status=status, collector=collector,
                         requests_used=req, documents=docs, ai_passages=passages, note=note)


def test_family_status_precedence():
    from footprint.pipeline import family_status

    S = CoverageStatus
    assert family_status([S.DONE, S.BLOCKED_TOU, S.NOT_APPLICABLE]) == S.DONE
    assert family_status([S.DONE, S.STOPPED]) == S.STOPPED
    assert family_status([S.STOPPED, S.DONE_MANUAL, S.ERROR]) == S.DONE_MANUAL
    assert family_status([S.ERROR, S.BLOCKED_BOT, S.NOT_APPLICABLE]) == S.BLOCKED_BOT
    assert family_status([S.PENDING, S.BLOCKED_TOU]) == S.PENDING
    assert family_status([S.NOT_APPLICABLE, S.NOT_APPLICABLE]) == S.NOT_APPLICABLE
    assert family_status([]) is None


def test_aggregate_coverage_one_row_per_family_keeps_details():
    """Regression: several rows per family (V-005 had two DNS rows, error + done); one status per family now."""
    from footprint.pipeline import aggregate_coverage, status_label

    P, D, J = SourceFamily.PRD, SourceFamily.DNS, SourceFamily.JOB
    rows = [_e(P, CoverageStatus.DONE, "seeds", req=3, docs=3, passages=5),
            _e(P, CoverageStatus.STOPPED, "site", "stopped(rule: cap)", req=100, docs=90, passages=7),
            _e(P, CoverageStatus.NOT_APPLICABLE, "wordpress", "WordPress REST API not detected", req=1),
            _e(D, CoverageStatus.DONE, "dns", req=6, docs=1), _e(D, CoverageStatus.ERROR, "dns", req=6),
            _e(J, CoverageStatus.PENDING, "seeds", "awaiting manual capture: 1 seed"),
            _e(J, CoverageStatus.PENDING, "jobs", "awaiting manual capture: manual sampling protocol")]
    agg = {e.family: e for e in aggregate_coverage(rows, plan(caps={P: 150, D: 40, J: 120}))}
    assert set(agg) == {P, D, J} and all(e.collector == "family" for e in agg.values())
    assert agg[P].status == CoverageStatus.STOPPED and agg[P].requests_used == 104 and agg[P].ai_passages == 12
    assert agg[P].cap == 150 and "seeds done; site stopped; wordpress not_applicable" in agg[P].note
    assert agg[D].status == CoverageStatus.DONE and agg[D].requests_used == 12
    assert agg[J].status == CoverageStatus.PENDING and status_label(agg[J]) == "awaiting_manual"


def test_collect_vendor_writes_family_status_and_fetch_log(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    store.put_raw(b"<html><body><p>We use generative AI to plan batch jobs every single night.</p></body></html>",
                  vendor_id="V-001", family=SourceFamily.PRD, collector="seeds", url_requested=AI_PAGE, status=200,
                  content_type="text/html", retrieved_at="2026-10-02T00:00:00Z", robots_decision="allowed")
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    (seeds / "V-001.toml").write_text(SEED_TOML, encoding="utf-8")
    runs = tmp_path / "runs"
    run = collect_vendor(_assessment(), "replay", seeds, store, fetcher=ReplayFetcher(store), runs_dir=runs,
                         as_of="2026-10-02")
    fams = [e.family for e in run.family_status]
    assert len(fams) == len(set(fams)) and set(SourceFamily) <= set(fams)
    d = runs / run.run_id
    assert (d / "family_status.jsonl").exists() and (d / "fetch_log.jsonl").exists()
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["family_status"]["EXEC"] == "awaiting_manual"
    assert any(ev["gate"] == "replay" and ev["decision"] == "ok" for ev in run.fetch_log)
    from footprint.pipeline import load_run_family_status, load_run_fetch_log

    assert len(load_run_family_status(run.run_id, runs)) == len(run.family_status)
    assert len(load_run_fetch_log(run.run_id, runs)) == len(run.fetch_log)
    caps = [(c.capture_id, c.url_requested) for c in run.captures]
    assert len(caps) == len(set(caps))  # a URL reused by a later collector is listed once


def test_live_dependency_check():
    from footprint.pipeline import check_live_dependencies

    assert check_live_dependencies(("json",)) == []
    assert check_live_dependencies(("footprint_no_such_module_xyz",)) == ["footprint_no_such_module_xyz"]
