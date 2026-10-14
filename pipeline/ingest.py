"""Stage 1-2: ingest an input CSV into SQLite, then build the exact-text dedupe table.

Pure code/SQL, no model calls. Every source row is kept; empty texts are flagged for
quarantine, never dropped.
"""
import time

from .config import checker
from .db import get_meta, set_meta, transaction
from .io_utils import text_sha

BATCH = 5000


def ingest(con, csv_path):
    """Load every row with its contract row hash. Idempotent for the same input file."""
    file_sha = checker.sha(csv_path)
    existing = get_meta(con, "input_sha256")
    if existing:
        if existing != file_sha:
            raise ValueError(f"State DB already holds a different input ({existing[:12]}...); use a new --state file")
        return {"skipped": True, "input_sha256": file_sha}
    t0 = time.perf_counter()
    rows, n = [], 0
    with transaction(con):
        for order, row in enumerate(checker.csv_rows(csv_path)):
            text = row["review_text"]
            rows.append((row["review_id"], order, text, row["review_rating"], row["review_likes"],
                         row["app_version"], row["review_timestamp"], checker.row_sha(row),
                         text_sha(text), int(not text.strip())))
            if len(rows) >= BATCH:
                con.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                n += len(rows)
                rows.clear()
        con.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        n += len(rows)
        set_meta(con, "input_path", str(csv_path))
        set_meta(con, "input_sha256", file_sha)
    stats = dedupe(con)
    return {"skipped": False, "input_sha256": file_sha, "rows": n,
            "seconds": round(time.perf_counter() - t0, 3), **stats}


def dedupe(con):
    """Distinct nonempty texts. SQLite returns the bare columns of the min(src_order) row."""
    with transaction(con):
        con.execute("DELETE FROM texts")
        con.execute("""
            INSERT INTO texts(text_sha256, canonical_id, n_reviews, char_len)
            SELECT text_sha256, review_id, n, length(review_text) FROM (
                SELECT text_sha256, review_id, review_text, min(src_order), count(*) AS n
                FROM reviews WHERE is_empty = 0
                  AND (NOT EXISTS (SELECT 1 FROM scope) OR review_id IN (SELECT review_id FROM scope))
                GROUP BY text_sha256)
        """)
    return sql_profile(con)


def sql_profile(con):
    one = lambda q: con.execute(q).fetchone()[0]
    return {
        "records": one("SELECT count(*) FROM reviews"),
        "empty_texts": one("SELECT count(*) FROM reviews WHERE is_empty = 1"),
        "nonempty": one("SELECT count(*) FROM reviews WHERE is_empty = 0"),
        "distinct_nonempty_texts": one("SELECT count(*) FROM texts"),
        "duplicate_review_ids": one("SELECT count(*) - count(DISTINCT review_id) FROM reviews"),
        "missing_app_version": one("SELECT count(*) FROM reviews WHERE trim(app_version) = ''"),
        "reviews_reusing_a_result": one("SELECT coalesce(sum(n_reviews - 1), 0) FROM texts"),
    }


def top_duplicates(con, limit=10):
    return [dict(r) for r in con.execute(
        "SELECT t.n_reviews, r.review_text FROM texts t JOIN reviews r ON r.review_id = t.canonical_id "
        "ORDER BY t.n_reviews DESC, t.text_sha256 LIMIT ?", (limit,))]


def select_scope(con, n, seed, exclude_ids=()):
    """Seeded random sample of n NONEMPTY reviews: lowest SHA-256(seed + ':' + review_id), excluding the
    given IDs (the golden 50). Deterministic and reproducible from the seed; rebuilds the dedupe table so
    exact-text reuse happens only inside the sample. Returns (count, sha256 of the sorted ID list)."""
    import hashlib
    exclude = set(exclude_ids)
    keyed = sorted((hashlib.sha256(f"{seed}:{r[0]}".encode()).hexdigest(), r[0]) for r in
                   con.execute("SELECT review_id FROM reviews WHERE is_empty = 0") if r[0] not in exclude)
    chosen = sorted(rid for _, rid in keyed[:n])
    with transaction(con):
        con.execute("DELETE FROM scope")
        con.executemany("INSERT INTO scope VALUES (?)", [(r,) for r in chosen])
    dedupe(con)
    return len(chosen), hashlib.sha256("\n".join(chosen).encode()).hexdigest()
