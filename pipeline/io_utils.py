"""Atomic file writes and small hashing helpers."""
import csv
import gzip
import hashlib
import io
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def text_sha(text: str) -> str:
    """Exact-text cache key: no trimming, case folding or Unicode normalization."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@contextmanager
def atomic_open(path, mode="w", gz=False):
    """Write to a temp file in the same folder, fsync, then os.replace().

    Readers never see a half-written file, and a crash leaves the previous version intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        if gz:
            # mtime=0 keeps the gzip bytes deterministic across exports.
            raw = open(tmp, "wb")
            f = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
            text = io.TextIOWrapper(f, encoding="utf-8", newline="\n")
            try:
                yield text
            finally:
                text.close()
                raw.flush()
                os.fsync(raw.fileno())
                raw.close()
        else:
            with open(tmp, mode, encoding="utf-8", newline="") as f:
                yield f
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path, value):
    with atomic_open(path) as f:
        f.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")


def write_jsonl(path, rows, gz=False):
    with atomic_open(path, gz=gz) as f:
        for row in rows:
            f.write(compact(row) + "\n")


def write_csv(path, header, rows):
    with atomic_open(path) as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def read_jsonl(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)
