"""Content-addressed evidence store.

Layout under ``root`` (default ``evidence``):
  blobs/<ab>/<sha>.gz   gzip of raw bytes; sha256 over the *uncompressed* bytes
  text/<sha>.txt        extracted text, UTF-8, stored byte-exact (no newline translation)
  shots/<sha>.<ext>     manual-capture screenshots
  index.jsonl           append-only, one Capture per line, JSON with sorted keys
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

from footprint.models import CAPTURE_HEADER_KEYS, Capture


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps_sorted(obj: dict) -> str:
    """Deterministic JSON line: sorted keys, compact separators, UTF-8 kept."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class EvidenceStore:
    def __init__(self, root: str | os.PathLike = "evidence") -> None:
        self.root = Path(root)
        self.index_path = self.root / "index.jsonl"
        self._index: list[Capture] | None = None

    # ------------------------------------------------------------ paths
    def rel(self, p: Path) -> str:
        """Repo-relative POSIX path (relative to cwd if possible, else root-name prefixed)."""
        p = Path(p).resolve()
        try:
            return p.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            return (Path(self.root.name) / p.relative_to(self.root.resolve())).as_posix()

    def _blob_file(self, sha: str) -> Path:
        return self.root / "blobs" / sha[:2] / f"{sha}.gz"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    # ------------------------------------------------------------ index
    def captures(self) -> list[Capture]:
        """All index entries in append order (cached after first read)."""
        if self._index is None:
            self._index = []
            if self.index_path.exists():
                with self.index_path.open("r", encoding="utf-8", newline="") as fh:
                    for line in fh:
                        if line.strip():
                            self._index.append(Capture.model_validate_json(line))
        return list(self._index)

    def _append(self, cap: Capture) -> None:
        self.captures()
        self.root.mkdir(parents=True, exist_ok=True)
        line = dumps_sorted(cap.model_dump(mode="json")) + "\n"
        with self.index_path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(line)
        assert self._index is not None
        self._index.append(cap)

    # ------------------------------------------------------------ raw
    def put_raw(self, data: bytes, **meta) -> Capture:
        """Store raw bytes and append a Capture to the index.

        ``meta`` = Capture fields other than capture_id/size/blob_path. Headers are
        lower-cased and filtered to CAPTURE_HEADER_KEYS.
        """
        for k in ("capture_id", "size", "blob_path"):
            meta.pop(k, None)
        sha = sha256_hex(data)
        blob = self._blob_file(sha)
        if not blob.exists():
            self._atomic_write(blob, gzip.compress(data, mtime=0))
        raw_headers = meta.pop("headers", None) or {}
        headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        headers = {k: v for k, v in sorted(headers.items()) if k in CAPTURE_HEADER_KEYS}
        cap = Capture(capture_id=sha, size=len(data), blob_path=self.rel(blob), headers=headers, **meta)
        self._append(cap)
        return cap

    def has_blob(self, capture_id: str) -> bool:
        return self._blob_file(capture_id).exists()

    def get_raw(self, capture_id: str) -> bytes:
        blob = self._blob_file(capture_id)
        if not blob.exists():
            raise KeyError(capture_id)
        return gzip.decompress(blob.read_bytes())

    def find_by_url(self, url: str) -> Capture | None:
        """Latest capture (by retrieved_at; later index line wins ties) with url_requested == url."""
        best: Capture | None = None
        for c in self.captures():
            if c.url_requested == url and (best is None or c.retrieved_at >= best.retrieved_at):
                best = c
        return best

    # ------------------------------------------------------------ text
    def put_text(self, text: str) -> tuple[str, str]:
        data = text.encode("utf-8")
        sha = sha256_hex(data)
        path = self.root / "text" / f"{sha}.txt"
        if not path.exists():
            self._atomic_write(path, data)
        return sha, self.rel(path)

    def get_text(self, sha: str) -> str:
        path = self.root / "text" / f"{sha}.txt"
        if not path.exists():
            raise KeyError(sha)
        return path.read_bytes().decode("utf-8")

    # ------------------------------------------------------------ shots
    def put_shot(self, data: bytes, ext: str = ".png") -> str:
        """Copy a screenshot to shots/<sha><ext>; returns repo-relative path."""
        sha = sha256_hex(data)
        path = self.root / "shots" / f"{sha}{(ext or '.png').lower()}"
        if not path.exists():
            self._atomic_write(path, data)
        return self.rel(path)
