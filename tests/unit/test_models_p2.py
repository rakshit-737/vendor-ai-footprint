import hashlib

from footprint.models import Capture, CollectorResult, Document, FetchOutcome, Passage, SourceFamily


def _cap():
    return Capture(capture_id="a" * 64, vendor_id="V-001", family=SourceFamily.LEG, collector="site",
                   url_requested="https://x.com/privacy", retrieved_at="2026-10-02T00:00:00Z")


def test_capture_roundtrip():
    c = _cap()
    assert Capture.model_validate_json(c.model_dump_json()) == c
    assert c.robots_decision == "not_applicable" and not c.manual


def test_passage_id():
    pid = Passage.make_id("d", 1, 5)
    assert pid == hashlib.sha256(b"d|1|5").hexdigest()[:16]


def test_document_and_results():
    d = Document(doc_id="b" * 64, capture_id="a" * 64, vendor_id="V-001", family=SourceFamily.PRD,
                 url="u", kind="pdf", pages=[0, 100])
    r = CollectorResult(captures=[_cap()], documents=[d])
    assert r.leads == [] and r.documents[0].pages == [0, 100]
    o = FetchOutcome(ok=False, reason="replay_miss")
    assert o.capture is None
