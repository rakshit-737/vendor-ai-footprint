import hashlib
from datetime import timezone

from footprint.capture.manual import import_inbox, import_inbox_report
from footprint.capture.store import EvidenceStore
from footprint.models import SourceFamily

HDR = "nn,url,file,screenshot,captured_at_local,analyst,note\n"


def _inbox(tmp_path, rows, files):
    vdir = tmp_path / "evidence" / "manual_inbox" / "V-002"
    vdir.mkdir(parents=True)
    for name, data in files.items():
        (vdir / name).write_bytes(data)
    (vdir / "captures.csv").write_text(HDR + "".join(r + "\n" for r in rows), encoding="utf-8")
    return tmp_path / "evidence" / "manual_inbox"


def test_import_basic(tmp_path):
    inbox = _inbox(tmp_path, [
        "01,https://www.fiserv.com/en/lp/agentos-by-fiserv.html,01-agentos.html,01-agentos.png,2026-10-06 14:05,RK,agentOS landing page",
        "10,https://www.fiserv.com/footer,,,2026-10-06 14:30,RK,none found",
    ], {"01-agentos.html": b"<html>agentOS</html>", "01-agentos.png": b"\x89PNGfake"})
    st = EvidenceStore(tmp_path / "evidence")
    rep = import_inbox_report(st, inbox, tz=timezone.utc)
    assert len(rep.captures) == 1
    c = rep.captures[0]
    assert c.capture_id == hashlib.sha256(b"<html>agentOS</html>").hexdigest()
    assert c.manual and c.robots_decision == "manual" and c.captured_by == "human:RK"
    assert c.collector == "manual" and c.vendor_id == "V-002"
    assert c.retrieved_at == "2026-10-06T14:05:00Z"
    assert c.content_type == "text/html"
    shot_sha = hashlib.sha256(b"\x89PNGfake").hexdigest()
    assert c.screenshot_path == f"evidence/shots/{shot_sha}.png"
    assert (tmp_path / "evidence" / "shots" / f"{shot_sha}.png").read_bytes() == b"\x89PNGfake"
    assert "agentOS landing page" in c.note
    assert st.get_raw(c.capture_id) == b"<html>agentOS</html>"
    assert len(rep.skipped) == 1
    assert rep.skipped[0].note == "skipped: none found"
    assert rep.skipped[0].vendor_id == "V-002"


def test_idempotent(tmp_path):
    inbox = _inbox(tmp_path, ["01,https://x.example/a,a.pdf,,2026-10-06 14:05,RK,"],
                   {"a.pdf": b"%PDF-1.4 x"})
    st = EvidenceStore(tmp_path / "evidence")
    first = import_inbox(st, inbox, tz=timezone.utc)
    assert len(first) == 1 and first[0].content_type == "application/pdf"
    assert import_inbox(st, inbox, tz=timezone.utc) == []
    assert import_inbox(EvidenceStore(tmp_path / "evidence"), inbox, tz=timezone.utc) == []
    lines = (tmp_path / "evidence" / "index.jsonl").read_text().splitlines()
    assert len(lines) == 1


def test_family_inference_and_analyst_override(tmp_path):
    inbox = _inbox(tmp_path, [
        "01,https://www.linkedin.com/posts/x,a.html,,2026-10-06 14:05,,",
        "02,https://careers.fiserv.com/search?q=ml,b.html,,2026-10-06 14:06,,",
        "03,https://openai.com/index/bny/,c.html,,2026-10-06 14:07,,",
    ], {"a.html": b"a", "b.html": b"b", "c.html": b"c"})
    caps = import_inbox(EvidenceStore(tmp_path / "evidence"), inbox, analyst="ZZ", tz=timezone.utc)
    fams = {c.url_requested: c.family for c in caps}
    assert fams["https://www.linkedin.com/posts/x"] == SourceFamily.EXEC
    assert fams["https://careers.fiserv.com/search?q=ml"] == SourceFamily.JOB
    assert all(c.captured_by == "human:ZZ" for c in caps)


def test_missing_file_is_skipped(tmp_path):
    inbox = _inbox(tmp_path, ["01,https://x.example/a,gone.html,,2026-10-06 14:05,RK,"], {})
    rep = import_inbox_report(EvidenceStore(tmp_path / "evidence"), inbox, tz=timezone.utc)
    assert rep.captures == [] and rep.skipped[0].note.startswith("skipped: file not found")


def test_no_inbox(tmp_path):
    assert import_inbox(EvidenceStore(tmp_path / "evidence"), tmp_path / "nope") == []
