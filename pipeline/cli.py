"""Command-line entry point. Offline commands run freely; paid commands require --execute.

    python -m pipeline.cli ingest   --input <csv> --state state/full.sqlite
    python -m pipeline.cli status   --state state/full.sqlite
    python -m pipeline.cli profile  --input <csv> --out grading/ingestion.json   # provided helper
    python -m pipeline.cli checkpoint --state ... --config <label_config> --out grading/checkpoint_before.json
    python -m pipeline.cli check    --input <csv> --submission grading            # zero-API course checker
"""
import argparse
import json
import sys
import time
from pathlib import Path

from .config import FULL_CSV, ROOT, STATE_DIR, checker
from .db import connect, get_meta
from .export import write_checkpoint
from .ingest import ingest, sql_profile, top_duplicates


def cmd_ingest(a):
    con = connect(a.state)
    print(json.dumps(ingest(con, a.input), indent=2))
    for r in top_duplicates(con, 5):
        print(f"  reused {r['n_reviews']:>6}x  {r['review_text'][:60]!r}")


def cmd_status(a):
    con = connect(a.state)
    prof = sql_profile(con)
    prof["input_sha256"] = get_meta(con, "input_sha256")
    prof["results_by_config"] = {r[0]: r[1] for r in con.execute(
        "SELECT label_config, count(*) FROM results GROUP BY 1")}
    prof["calls_by_role_outcome"] = {f"{r[0]}/{r[1]}": r[2] for r in con.execute(
        "SELECT role, outcome, count(*) FROM calls GROUP BY 1, 2")}
    prof["failures_pending"] = con.execute("SELECT count(*) FROM failures").fetchone()[0]
    prof["spend_committed_usd"] = con.execute("""SELECT coalesce(sum(CASE WHEN status='settled' THEN actual_usd
        WHEN status IN ('reserved','unreconciled') THEN reserved_usd ELSE 0 END),0) FROM spend""").fetchone()[0]
    print(json.dumps(prof, indent=2))


def cmd_profile(a):
    t = time.perf_counter()
    checker.write_json(a.out, checker.profile(a.input))
    print(f"wrote {a.out} with the provided helper in {time.perf_counter() - t:.1f}s")


def cmd_checkpoint(a):
    n = write_checkpoint(connect(a.state), a.config, a.out)
    print(f"wrote {a.out}: {n} completed IDs")


def cmd_check(a):
    ref_path = a.reference
    if not ref_path.exists():
        print(f"building reference {ref_path} (reads the CSV, no classifications) ...")
        checker.write_json(ref_path, checker.reference(a.input, a.input))
    ref = checker.read_json(ref_path)
    gold = checker.read_json(a.gold) if a.gold else None
    report = checker.audit(a.submission, ref, gold, checker.reference_hash(ref))
    checker.write_json(a.out, report)
    print(json.dumps({"status": report["status"], "coverage": report["coverage"],
                      "issue_counts": report["issue_counts"]}, indent=2))
    return 0 if report["status"] == "pass" else 1


def cmd_rank(a):
    """Recompute the baseline ranking from saved grading/ files only (no state DB, no model calls) and
    compare it byte-for-byte with the saved ranking.csv."""
    import csv as _csv
    import gzip
    from .rank import RANKING_HEADER, compute_ranking, ranking_table
    folder = a.grading
    path = folder / "records.jsonl.gz"
    with (gzip.open(path, "rt", encoding="utf-8") if path.exists() else open(folder / "records.jsonl", encoding="utf-8")) as f:
        sev = {r["review_id"]: r["severity"] for r in map(json.loads, f) if r["status"] == "completed"}
    with open(folder / "membership.csv", encoding="utf-8", newline="") as f:
        pairs = [(r["issue_id"], r["review_id"]) for r in _csv.DictReader(f)]
    rows = [list(RANKING_HEADER)] + ranking_table(compute_ranking((i, r, sev[r]) for i, r in pairs))
    with open(folder / "ranking.csv", encoding="utf-8", newline="") as f:
        saved = list(_csv.reader(f))
    out = a.out or (ROOT / "ranking_recomputed.csv")
    with open(out, "w", encoding="utf-8", newline="") as f:
        _csv.writer(f, lineterminator="\n").writerows(rows)
    print(f"{len(rows) - 1} issues recomputed from {len(pairs):,} memberships -> {out}")
    print("identical to saved ranking.csv:", rows == saved)
    return 0 if rows == saved else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest"); s.add_argument("--input", type=Path, default=FULL_CSV)
    s.add_argument("--state", type=Path, default=STATE_DIR / "full.sqlite"); s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("status"); s.add_argument("--state", type=Path, default=STATE_DIR / "full.sqlite")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("profile"); s.add_argument("--input", type=Path, default=FULL_CSV)
    s.add_argument("--out", type=Path, default=ROOT / "grading" / "ingestion.json"); s.set_defaults(fn=cmd_profile)
    s = sub.add_parser("checkpoint"); s.add_argument("--state", type=Path, required=True)
    s.add_argument("--config", action="append", required=True, help="label_config(s), primary first")
    s.add_argument("--out", type=Path, required=True); s.set_defaults(fn=cmd_checkpoint)
    s = sub.add_parser("check"); s.add_argument("--input", type=Path, default=FULL_CSV)
    s.add_argument("--submission", type=Path, default=ROOT / "grading")
    s.add_argument("--reference", type=Path, default=ROOT / "local-reference.json")
    s.add_argument("--gold", type=Path); s.add_argument("--out", type=Path, default=ROOT / "self-check.json")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("rank", help="recompute ranking from grading/ files only (no model calls)")
    s.add_argument("--grading", type=Path, default=ROOT / "grading"); s.add_argument("--out", type=Path)
    s.set_defaults(fn=cmd_rank)
    a = p.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
