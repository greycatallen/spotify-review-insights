"""Development checkpoints before the full run: 500 then 10,000 reviews (COST_CALCULATOR.md: measure
100, then 500, then 10,000). Each run is a cold run on a fresh state file with the same configured
pipeline as the pilot, so worker-count comparisons use the same reviews and cache conditions.

    python -m pipeline.devrun --set 500 --workers 1                 # dry run: plan + estimate, no calls
    python -m pipeline.devrun --set 500 --workers 1 --execute
    python -m pipeline.devrun --set 500 --workers 2 --enrich-only --execute   # worker comparison
    python -m pipeline.devrun --set 10000 --workers 4 --execute

Outputs go to cost/dev/<set>_w<workers>[_enrich]/ in the calculator's format, and the offline
calculator is re-run there to refresh the full-run estimate.
"""
import argparse
import json
import subprocess
import sys
import time

from .config import ANALYSIS_10000_CSV, CHECKPOINT_500_CSV, COST_DIR, MANIFEST, ROOT, STATE_DIR, checker
from .db import connect
from .ingest import ingest
from .io_utils import write_json, write_jsonl
from .pilot import run_once

SETS = {"500": (CHECKPOINT_500_CSV, "checkpoint_500.csv", 0.10), "10000": (ANALYSIS_10000_CSV, "analysis_10000.csv", 0.02)}
# Measured on the pilot (cost/report.md): per-review Jev enrich, per-review Haiku verify, fixed group+memo.
EST = {"enrich": 0.0000345, "verify": 0.00043, "fixed": 0.011}


def progress_logger(every):
    """Print every Nth enrichment progress line (the runner logs once per saved batch)."""
    count = [0]

    def log(msg):
        count[0] += 1
        if not msg.startswith("[enrich]") or count[0] % every == 0:
            print(msg, flush=True)
    return log


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", choices=SETS, required=True)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--verify-fraction", type=float, help="declared verification sample (default: 10%% / 2%%)")
    ap.add_argument("--enrich-only", action="store_true", help="skip verify/group/memo (worker-count comparison)")
    ap.add_argument("--budget", type=float, help="hard cap for this run in USD (default: 3x the estimate)")
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args(argv)
    csv_path, manifest_name, default_vf = SETS[a.set]
    vf = default_vf if a.verify_fraction is None else a.verify_fraction
    want = json.loads(MANIFEST.read_text())["files"][manifest_name]["sha256"]
    ok = checker.sha(csv_path) == want
    n = sum(1 for _ in checker.csv_rows(csv_path))
    est = n * EST["enrich"] + (0 if a.enrich_only else n * vf * EST["verify"] + EST["fixed"])
    budget = a.budget or round(max(3 * est, 0.10), 2)
    tag = f"{a.set}_w{a.workers}{'_enrich' if a.enrich_only else ''}"
    print(f"{manifest_name}: {n} reviews, sha256 matches manifest: {ok}")
    print(f"Plan: cold run, Jev jevq-v2, {a.workers} worker(s); "
          + ("enrich only" if a.enrich_only else f"verify {vf:.0%} + needs_review, group, memo") +
          f". Estimated ${est:.3f} (from pilot measurements); hard cap ${budget:.2f}.")
    if not ok:
        raise SystemExit("checksum mismatch; refusing to run")
    if not a.execute:
        print("Dry run only. Re-run with --execute to make the paid calls.")
        return 0

    db = STATE_DIR / f"dev_{tag}.sqlite"
    for p in (db, db.with_suffix(".sqlite-wal"), db.with_suffix(".sqlite-shm")):
        p.unlink(missing_ok=True)
    con = connect(db)
    t = time.perf_counter()
    ingest(con, csv_path)
    ingest_s = round(time.perf_counter() - t, 3)
    run, calls, records, memo = run_once(con, "cold", budget, input_csv=csv_path, name=f"dev{a.set}",
                                         workers=a.workers, verify_fraction=vf, enrich_only=a.enrich_only,
                                         log=progress_logger(every=1 if n <= 500 else 250))
    run["ingest_seconds"] = ingest_s
    run["ids_match_input"] = [r["review_id"] for r in records] == [r["review_id"] for r in checker.csv_rows(csv_path)]
    out = COST_DIR / "dev" / tag
    rows = [{**c, "run_kind": "cold", "review_ids": json.loads(c["review_ids"]), "settings": json.loads(c["settings"]),
             "extra": json.loads(c["extra"] or "{}"), "tier": "standard",
             "route": "fallback" if c["role"] == "enrich" and c["provider"] != "typesafe" else "primary"} for c in calls]
    write_jsonl(out / "pilot_calls.jsonl", rows)
    write_json(out / "pilot_runs.json", [run])
    write_jsonl(out / "records.jsonl", records)
    if not a.enrich_only:
        (out / "memo.md").write_text(memo["memo"] + "\n", encoding="utf-8")
        write_json(out / "claims.json", {"claims": memo["claims"], "audit": memo["audit"]})
    lat = [c["latency_s"] for c in calls if c["role"] == "enrich" and c["provider"] == "typesafe"]
    summary = {k: run[k] for k in ("wall_seconds", "stage_seconds", "completed", "failed", "unique_texts",
                                   "result_cache_hits", "workers", "memo_audit", "ledger_committed_usd_after")}
    summary["enrich_reviews_per_second"] = round(run["unique_texts"] / run["stage_seconds"]["enrich"], 2)
    summary["jev_latency_s"] = {"mean": round(sum(lat) / len(lat), 4), "max": round(max(lat), 4)} if lat else None
    summary["failed_calls"] = sum(c["outcome"] == "failed" for c in calls)
    print(json.dumps(summary, indent=1))
    subprocess.run([sys.executable, str(ROOT / "cost" / "calculator.py"), "--dir", str(out), "--rates",
                    str(COST_DIR / "rates.csv"), "--assumptions", str(COST_DIR / "assumptions.json")],
                   check=True, stdout=subprocess.DEVNULL)
    print(f"Saved run evidence and refreshed estimate in {out.relative_to(ROOT)}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
