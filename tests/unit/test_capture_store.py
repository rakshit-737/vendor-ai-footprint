import gzip
import hashlib
import json

import pytest

from footprint.capture.store import EvidenceStore
from footprint.models import SourceFamily


def _meta(**kw):
    m = dict(vendor_id="V-001", family=SourceFamily.PRD, collector="test",
             url_requested="https://example.com/a", retrieved_at="2026-10-02T12:00:00Z")
    m.update(kw)
    return m


def test_put_raw_content_addressed(tmp_path):
    st = EvidenceStore(tmp_path / "evidence")
    data = b"<html>hello AI</html>"
    cap = st.put_raw(data, **_meta(headers={"Server": "x", "Set-Cookie": "no"}))
    sha = hashlib.sha256(data).hexdigest()
    assert cap.capture_id == sha
    assert cap.size == len(data)
    assert cap.blob_path == f"evidence/blobs/{sha[:2]}/{sha}.gz"
    blob = tmp_path / "evidence" / "blobs" / sha[:2] / f"{sha}.gz"
    assert gzip.decompress(blob.read_bytes()) == data
    assert cap.headers == {"server": "x"}
    assert st.get_raw(sha) == data


def test_index_jsonl_sorted_and_append_only(tmp_path):
    st = EvidenceStore(tmp_path / "evidence")
    st.put_raw(b"one", **_meta())
    st.put_raw(b"two", **_meta(retrieved_at="2026-10-03T00:00:00Z"))
    lines = (tmp_path / "evidence" / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for ln in lines:
        obj = json.loads(ln)
        assert list(obj) == sorted(obj)
        assert ln == json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_find_by_url_latest(tmp_path):
    st = EvidenceStore(tmp_path / "evidence")
    st.put_raw(b"new", **_meta(retrieved_at="2026-10-03T00:00:00Z"))
    st.put_raw(b"old", **_meta(retrieved_at="2026-10-01T00:00:00Z"))
    assert st.get_raw(st.find_by_url("https://example.com/a").capture_id) == b"new"
    assert st.find_by_url("https://nope") is None
    # a fresh store instance reads the index from disk
    st2 = EvidenceStore(tmp_path / "evidence")
    assert st2.find_by_url("https://example.com/a").capture_id == hashlib.sha256(b"new").hexdigest()


def test_same_bytes_twice_one_blob(tmp_path):
    st = EvidenceStore(tmp_path / "evidence")
    a = st.put_raw(b"same", **_meta())
    b = st.put_raw(b"same", **_meta(url_requested="https://example.com/b"))
    assert a.capture_id == b.capture_id
    assert len(list((tmp_path / "evidence" / "blobs").rglob("*.gz"))) == 1


def test_text_store_exact(tmp_path):
    st = EvidenceStore(tmp_path / "evidence")
    text = "line1\r\nline2\nunicode: café — AI\n"
    sha, path = st.put_text(text)
    assert sha == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert path == f"evidence/text/{sha}.txt"
    assert (tmp_path / "evidence" / "text" / f"{sha}.txt").read_bytes() == text.encode("utf-8")
    assert st.get_text(sha) == text
    assert st.put_text(text) == (sha, path)


def test_get_raw_missing(tmp_path):
    with pytest.raises(KeyError):
        EvidenceStore(tmp_path / "evidence").get_raw("0" * 64)
