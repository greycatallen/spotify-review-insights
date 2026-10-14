"""Final run: a seeded random 100,000-review sample of the full 660,622-row corpus (golden 50 excluded),
with every source ID accounted for in grading/ (out-of-sample rows quarantined as not_in_scope).

    python -m pipeline.final prepare                          # free: copy ingested corpus, select the 100k scope
    python -m pipeline.final enrich --phase initial --execute # paid; press Ctrl+C once to interrupt cleanly
    python -m pipeline.final checkpoint before                # free: grading/checkpoint_before.json
    python -m pipeline.final enrich --phase resume --execute  # paid; continues with pending IDs only
    python -m pipeline.final checkpoint after                 # free
    python -m pipeline.final finish --execute                 # fallback, verify, group, rank, memo, export, check
    python -m pipeline.final status                           # free, any time

Spend caps are per provider (Jev $3.80, Claude $2.00) so neither account balance can be exhausted.
Ctrl+C: the first press stops admitting new requests, waits for in-flight calls and saves them; a
second press aborts immediately (in-flight outcomes are then reconciled on the next start).
"""
import argparse
import json
import shutil
import signal
import threading
import time

from .config import FULL_CSV, GOLDEN_CSV, GRADING_DIR, ROOT, STATE_DIR, checker
from .db import connect, get_meta, set_meta, transaction
from .enrich import run_enrichment
from .export import export_grading, iter_records, write_checkpoint
from .ingest import select_scope
from .io_utils import write_json
from .ledger import RateLimiter, SpendLedger, now

DB = STATE_DIR / "final.sqlite"
SCOPE_N, SEED = 100_000, "a5-final-100k-v1"
JEV_CAP, CLAUDE_CAP = 3.80, 2.00
VERIFY_FRACTION, VERIFY_CAP = 0.01, 1200
FALLBACK_CAP_FRACTION = 0.02
MAX_ATTEMPTS = 2           # brief: retry invalid output once
PRIMARY = "jev-1.13.0+jevq-v2+a5-common-v1"
FALLBACK = "claude-haiku-4-5+claude-v1+a5-common-v1"


def con_():
    if not DB.exists():
        raise SystemExit("state/final.sqlite not found: run `python -m pipeline.final prepare` first")
    return connect(DB)


def cmd_prepare(a):
    src = STATE_DIR / "full.sqlite"
    if DB.exists() and not a.force:
        raise SystemExit("state/final.sqlite exists (it may hold paid results). Use --force to rebuild it.")
    if not src.exists():
        from .ingest import ingest
        ingest(connect(src), FULL_CSV)
    con = connect(src)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    for p in (DB, DB.with_suffix(".sqlite-wal"), DB.with_suffix(".sqlite-shm")):
        p.unlink(missing_ok=True)
    shutil.copyfile(src, DB)
    con = connect(DB)
    golden = [r["review_id"] for r in checker.csv_rows(GOLDEN_CSV)]
    n, ids_sha = select_scope(con, SCOPE_N, SEED, exclude_ids=golden)
    texts = con.execute("SELECT count(*), sum(n_reviews) FROM texts").fetchone()
    with transaction(con):
        set_meta(con, "scope", json.dumps({"n": n, "seed": SEED, "method": "lowest SHA-256(seed:review_id) among "
                                           "nonempty reviews, golden 50 excluded", "ids_sha256": ids_sha}))
    print(json.dumps({"rows": con.execute("SELECT count(*) FROM reviews").fetchone()[0], "in_scope": n,
                      "distinct_texts_to_classify": texts[0], "reviews_reusing_a_result": texts[1] - texts[0],
                      "golden_in_scope": con.execute(f"SELECT count(*) FROM scope WHERE review_id IN "
                                                     f"({','.join('?' * len(golden))})", golden).fetchone()[0],
                      "scope_ids_sha256": ids_sha}, indent=1))


def cmd_enrich(a):
    from .backends.jev import JevBackend
    if not a.execute:
        con = con_()
        pend = con.execute("""SELECT count(*) FROM texts t WHERE NOT EXISTS (SELECT 1 FROM results x
                              WHERE x.text_sha256=t.text_sha256)""").fetchone()[0]
        print(f"Dry run: {pend} distinct texts pending; est. ${pend * 0.0000345:.2f} Jev. Add --execute to run.")
        return 0
    con = con_()
    stop = threading.Event()

    def on_sigint(sig, frm):
        if stop.is_set():
            raise KeyboardInterrupt
        print("\n[interrupt] stopping: no new requests; finishing and saving in-flight calls… (Ctrl+C again to abort)",
              flush=True)
        stop.set()
    signal.signal(signal.SIGINT, on_sigint)
    jev = JevBackend()
    run_id = f"final-{a.phase}-" + now().replace(":", "")
    ledger = SpendLedger(con, JEV_CAP, run_id, scope="typesafe")
    total = con.execute("SELECT count(*) FROM texts").fetchone()[0]
    t0 = time.perf_counter()
    count = [0]

    def log(msg):
        count[0] += 1
        if count[0] % 500 == 0 or not msg.startswith("[enrich]"):
            done = con.execute("SELECT count(*) FROM results").fetchone()[0]
            el = time.perf_counter() - t0
            print(f"{msg}  | {done:,}/{total:,} texts done, {el:,.0f}s", flush=True)
    before = con.execute("SELECT count(*) FROM results").fetchone()[0]
    print(f"[{a.phase}] {before:,}/{total:,} distinct texts already completed; workers={a.workers}; "
          f"Jev cap ${JEV_CAP:.2f}. Ctrl+C once to stop cleanly.", flush=True)
    stats = run_enrichment(con, jev, run_id=run_id, phase=a.phase, ledger=ledger,
                           limiter=RateLimiter(a.rpm, 6_000_000), batch_size=1, workers=a.workers,
                           max_attempts=MAX_ATTEMPTS, done_configs=[PRIMARY, FALLBACK], stop_event=stop, log=log)
    after = con.execute("SELECT count(*) FROM results").fetchone()[0]
    print(json.dumps({"phase": a.phase, "stopped_reason": stats.stopped_reason, "new_completed_texts": after - before,
                      "completed_texts": after, "of": total, "failed_batches": stats.batches_failed,
                      "wall_seconds": stats.wall_seconds, "jev_committed_usd": round(ledger.committed(), 4)}, indent=1))


def cmd_checkpoint(a):
    n = write_checkpoint(con_(), [PRIMARY, FALLBACK], GRADING_DIR / f"checkpoint_{a.which}.json")
    print(f"grading/checkpoint_{a.which}.json: {n:,} completed review IDs at {now()}")


def cmd_status(a):
    con = con_()
    q = lambda s, *p: con.execute(s, p).fetchone()[0]
    print(json.dumps({
        "distinct_texts": q("SELECT count(*) FROM texts"),
        "completed_texts": q("SELECT count(*) FROM results"),
        "texts_with_failures": q("SELECT count(*) FROM failures f WHERE NOT EXISTS (SELECT 1 FROM results x WHERE "
                                 "x.text_sha256=f.text_sha256)"),
        "calls": {f"{r[0]}/{r[1]}/{r[2]}": r[3] for r in con.execute(
            "SELECT role, phase, outcome, count(*) FROM calls GROUP BY 1,2,3")},
        "spend_usd": {r[0]: round(r[1], 4) for r in con.execute(
            """SELECT substr(run_id, 1, instr(run_id, ':') - 1), sum(CASE WHEN status='settled' THEN actual_usd
               WHEN status IN ('reserved','unreconciled') THEN reserved_usd ELSE 0 END) FROM spend GROUP BY 1""")},
    }, indent=1))


def cmd_finish(a):
    from .backends.claude import ClaudeBackend
    from .group import assign_membership, name_issues
    from .memo import write_memo
    from .verify import run_verify, sample_for_verification, verification_report
    from .publish import build, write_db
    con = con_()
    if not a.execute:
        print("Dry run: fallback (≤2% of texts), verify (1% sample, cap 1,200), group naming, memo, recommendations "
              "(Claude, est. $0.40–1.20, cap $2.00), then export grading/ and run the checker. Add --execute.")
        return 0
    run_id = "final-finish-" + now().replace(":", "")
    claude = SpendLedger(con, CLAUDE_CAP, run_id, scope="anthropic")
    stage = {}
    t = time.perf_counter()
    n = con.execute("SELECT count(*) FROM texts").fetchone()[0]
    fb = ClaudeBackend("claude-haiku-4-5", route="fallback")
    s = run_enrichment(con, fb, run_id=run_id, phase="resume", ledger=claude, limiter=RateLimiter(50), batch_size=25,
                       workers=1, max_attempts=MAX_ATTEMPTS, done_configs=[PRIMARY, FALLBACK], fallback_from=PRIMARY,
                       fallback_cap=int(n * FALLBACK_CAP_FRACTION), log=lambda *_: None)
    stage["fallback"] = {"seconds": round(time.perf_counter() - t, 1), **vars(s)}
    t = time.perf_counter()
    verifier = ClaudeBackend("claude-haiku-4-5", route="verify")
    sample = sample_for_verification(con, PRIMARY, VERIFY_FRACTION, cap=VERIFY_CAP)
    v = run_verify(con, verifier, sample, run_id=run_id, ledger=claude, primary_config=PRIMARY, batch_size=25,
                   log=print)
    stage["verify"] = {"seconds": round(time.perf_counter() - t, 1), **v}
    t = time.perf_counter()
    cfg = [PRIMARY, FALLBACK]
    counts = assign_membership(con, cfg)
    g = name_issues(con, cfg, run_id=run_id, ledger=claude)
    stage["group"] = {"seconds": round(time.perf_counter() - t, 1), "issues": len(counts), **g}
    records = list(iter_records(con, cfg))
    sev = {r["review_id"]: r["severity"] for r in records if r["status"] == "completed"}
    vr = verification_report(con, verifier.label_config, PRIMARY)
    in_scope = [r for r in records if r.get("reason") != "not_in_scope"]
    context = {"reviews_in_scope": len(in_scope), "completed": len(sev),
               "quarantined": sum(r["status"] == "quarantined" for r in in_scope),
               "verification_sample": vr["compared"], "verification_topic_agreement": vr["topic_agreement"],
               "verification_intent_agreement": vr["intent_agreement"]}
    t = time.perf_counter()
    m = write_memo(con, sev, context, run_id=run_id, ledger=claude)
    stage["memo"] = {"seconds": round(time.perf_counter() - t, 1), "audit": m["audit"], "attempts": m["attempts"]}
    (ROOT / "memo.md").write_text(m["memo"] + "\n", encoding="utf-8")
    t = time.perf_counter()
    summary = export_grading(con, GRADING_DIR, cfg, claims=m["claims"])
    stage["export"] = {"seconds": round(time.perf_counter() - t, 1), **summary}
    t = time.perf_counter()
    data = build(con, cfg, allow_call=True, run_label="Final run: seeded random 100,000 reviews (golden 50 excluded)")
    write_db(f"sqlite:///{STATE_DIR / 'dashboard_final.sqlite'}", data)
    stage["recommend"] = {"seconds": round(time.perf_counter() - t, 1), **data["summary"]["ai"]}
    write_json(ROOT / "run_summary.json", {"finished_at": now(), "scope": json.loads(get_meta(con, "scope")),
                                           "verification": vr | {"disagreements": len(vr["disagreements"])},
                                           "stages": stage, "claude_committed_usd": round(claude.committed(), 4)})
    print(json.dumps({k: (v if k != "verify" else {kk: v[kk] for kk in ("sent", "compared", "cached")})
                      for k, v in stage.items()}, indent=1, default=str))
    print("Now run the zero-API checker:  python -m pipeline.cli check")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("enrich"); p.add_argument("--phase", choices=["initial", "resume"], required=True)
    p.add_argument("--workers", type=int, default=6); p.add_argument("--rpm", type=int, default=4200)
    p.add_argument("--execute", action="store_true"); p.set_defaults(fn=cmd_enrich)
    p = sub.add_parser("checkpoint"); p.add_argument("which", choices=["before", "after"]); p.set_defaults(fn=cmd_checkpoint)
    p = sub.add_parser("finish"); p.add_argument("--execute", action="store_true"); p.set_defaults(fn=cmd_finish)
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status)
    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    raise SystemExit(main())
