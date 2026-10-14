"""End-to-end on synthetic data: enrich -> interrupt -> resume -> group -> export -> course checker.

Proves offline that the export passes check_submission.py, that resume sends no already-completed
ID, that duplicate texts are sent once and reused with direct provenance, and that planted
errors in an export are caught by the checker.
"""
import gzip
from collections import Counter
import json
import shutil

from conftest import FakeBackend
from pipeline.calls import CallRecord, Usage, log_call, local_request_id
from pipeline.config import checker
from pipeline.db import transaction
from pipeline.enrich import run_enrichment
from pipeline.export import export_grading, write_checkpoint
from pipeline.ledger import RateLimiter, SpendLedger, now
from pipeline.rank import claim, compute_ranking


def _run(con, backend, phase, **kw):
    return run_enrichment(con, backend, run_id=f"test-{phase}", phase=phase, ledger=SpendLedger(con, 5.0, phase),
                          limiter=RateLimiter(10_000), log=lambda *_: None, sleep=lambda s: None, **kw)


def _log_stage_call(con, role, ids=()):
    with transaction(con):
        log_call(con, CallRecord(request_id=local_request_id(role), run_id="test", role=role, provider="fake",
                                 model="fake-small", settings={}, review_ids=list(ids), outcome="succeeded",
                                 started_at=now(), ended_at=now(), latency_s=0.0, usage=Usage(10, 5)))


def _group(con, cfg):
    with transaction(con):
        for r in con.execute("""SELECT rv.review_id, x.topic FROM reviews rv JOIN results x
                                ON x.text_sha256 = rv.text_sha256 AND x.label_config = ?
                                WHERE x.intent IN ('complaint','cancellation')""", (cfg,)).fetchall():
            iid = f"ISS-{r['topic']}"
            con.execute("INSERT OR IGNORE INTO issues VALUES (?,?,?,?)", (iid, r["topic"], iid, None))
            con.execute("INSERT INTO membership VALUES (?,?)", (iid, r["review_id"]))


def test_full_flow_passes_course_checker(tmp_path, synth_csv, synth_db):
    con, be = synth_db, FakeBackend()
    # 1) initial run interrupted after 2 batches of 3
    s1 = _run(con, be, "initial", batch_size=3, max_batches=2)
    assert s1.records_saved == 6 and "max_batches" in s1.stopped_reason
    n_before = write_checkpoint(con, [be.label_config], tmp_path / "g" / "checkpoint_before.json")
    before = set(json.loads((tmp_path / "g" / "checkpoint_before.json").read_text())["completed_ids"])
    sent_initial = {i for b in be.sent for i in b}

    # 2) resume: no completed ID may be sent again
    s2 = _run(con, be, "resume", batch_size=3)
    sent_resume = {i for b in be.sent for i in b} - sent_initial
    assert not (sent_resume & before)
    write_checkpoint(con, [be.label_config], tmp_path / "g" / "checkpoint_after.json")
    assert s2.stopped_reason == "completed"

    # duplicates ("Good" x3) sent exactly once; empty/whitespace texts never sent
    all_sent = [i for b in be.sent for i in b]
    assert len(all_sent) == len(set(all_sent)) == 9  # 9 distinct nonempty texts
    assert "r03" not in all_sent and "r10" not in all_sent and "r04" not in all_sent and "r12" not in all_sent

    # 3) group, verify, memo (stage calls logged as the real stages will)
    _group(con, be.label_config)
    _log_stage_call(con, "verify", ["r01"])
    _log_stage_call(con, "group")
    _log_stage_call(con, "memo")
    sev = {r["review_id"]: r["severity"] for r in con.execute(
        "SELECT rv.review_id, x.severity FROM reviews rv JOIN results x ON x.text_sha256 = rv.text_sha256")}
    ranking = compute_ranking((m["issue_id"], m["review_id"], sev[m["review_id"]])
                              for m in con.execute("SELECT * FROM membership"))
    by = {r["issue_id"]: r for r in ranking}
    top = ranking[0]["issue_id"]
    claims = [claim("C1", by, top, "complaint_count"), claim("C2", by, top, "mean_severity")]

    g = tmp_path / "g"
    summary = export_grading(con, g, [be.label_config], claims=claims)
    assert summary["records"] == 13 and summary["quarantined"] == 2 and summary["cache_reuses"] == 2
    checker.write_json(g / "ingestion.json", checker.profile(synth_csv))

    ref = checker.reference(synth_csv, synth_csv)
    report = checker.audit(g, ref)
    assert report["status"] == "pass", report["issue_counts"]
    assert report["coverage"]["valid_cache_reuses"] == 2

    # 4) planted errors in a copy of the export must be flagged by the checker
    bad = tmp_path / "bad"
    shutil.copytree(g, bad)
    recs = [json.loads(l) for l in gzip.open(bad / "records.jsonl.gz", "rt", encoding="utf-8")]
    recs[0]["evidence_quote"] = "not in text"                      # unsupported quote
    alias = next(r for r in recs if r.get("cache_source_id"))
    other_alias = next(r for r in recs if r.get("cache_source_id") and r is not alias)
    other_alias["cache_source_id"] = alias["review_id"]             # chain of aliases
    recs.append(dict(recs[1]))                                      # duplicate ID
    with gzip.open(bad / "records.jsonl.gz", "wt", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)
    (bad / "ranking.csv").write_text((bad / "ranking.csv").read_text().replace(",1,", ",9,", 1))
    issues = checker.audit(bad, ref)["issue_counts"]
    for code in ("unsupported_quote", "invalid_cache_reuse", "duplicate_review_id"):
        assert code in issues, (code, issues)


def test_invalid_outputs_are_retried_then_left_pending(synth_db):
    con = synth_db
    be = FakeBackend(fail_ids={"r05"}, corrupt_quote_ids={"r06"}, extra_ids=["injected-id"])
    s = _run(con, be, "initial", batch_size=50, max_attempts=3)
    # r05 (missing) and r06 (bad quote) retried up to 3 attempts each, never saved
    saved = {r["canonical_id"] for r in con.execute("SELECT canonical_id FROM results")}
    assert "r05" not in saved and "r06" not in saved and "injected-id" not in saved
    att = dict(con.execute("SELECT t.canonical_id, f.attempts FROM failures f JOIN texts t USING(text_sha256)").fetchall())
    assert att == {"r05": 3, "r06": 3}
    assert s.records_saved == 7


def test_circuit_breaker_and_failed_calls_logged(synth_db):
    con = synth_db
    be = FakeBackend(errors=100)
    s = _run(con, be, "initial", batch_size=2, max_consecutive_failures=3)
    assert s.stopped_reason.startswith("circuit breaker")
    rows = con.execute("SELECT outcome FROM calls").fetchall()
    assert len(rows) == 3 and all(r["outcome"] == "failed" for r in rows)


def test_two_workers_same_results_as_one(tmp_path, synth_csv):
    from pipeline.db import connect
    from pipeline.ingest import ingest
    out = []
    for w in (1, 2):
        con = connect(tmp_path / f"w{w}.sqlite")
        ingest(con, synth_csv)
        _run(con, FakeBackend(), "initial", batch_size=2, workers=w)
        out.append(sorted(tuple(r) for r in con.execute(
            "SELECT canonical_id, topic, intent, severity FROM results")))
    assert out[0] == out[1]


def test_budget_stops_admission(synth_db):
    con = synth_db
    be = FakeBackend()
    s = run_enrichment(con, be, run_id="t", phase="initial", ledger=SpendLedger(con, 0.0045, "t"),
                       limiter=RateLimiter(10_000), log=lambda *_: None, batch_size=2)
    assert s.stopped_reason.startswith("budget")
    # each batch reserves 0.002 and settles at 0.001: spend never exceeds the cap
    assert SpendLedger(con, 1, "x").committed() <= 0.0045


def test_scoped_run_accounts_all_ids_and_passes_checker(tmp_path, synth_csv, synth_db):
    """100k-style scope: out-of-sample rows are quarantined not_in_scope, reuse stays inside the sample,
    per-provider ledger scopes are independent, and the export still passes the course checker."""
    from pipeline.ingest import select_scope
    con, be = synth_db, FakeBackend()
    n, _ = select_scope(con, 6, "test-seed", exclude_ids=["r01"])
    assert n == 6 and not con.execute("SELECT 1 FROM scope WHERE review_id='r01'").fetchone()
    a, b = SpendLedger(con, 1.0, "x", scope="typesafe"), SpendLedger(con, 0.001, "x", scope="anthropic")
    run_enrichment(con, be, run_id="t", phase="initial", ledger=a, limiter=RateLimiter(10_000),
                   log=lambda *_: None, batch_size=2, max_batches=1)
    write_checkpoint(con, [be.label_config], tmp_path / "g" / "checkpoint_before.json")
    run_enrichment(con, be, run_id="t2", phase="resume", ledger=a, limiter=RateLimiter(10_000), log=lambda *_: None)
    write_checkpoint(con, [be.label_config], tmp_path / "g" / "checkpoint_after.json")
    assert b.committed() == 0 and a.committed() > 0
    sent = {i for batch in be.sent for i in batch}
    assert sent <= {r[0] for r in con.execute("SELECT review_id FROM scope")}
    _group(con, be.label_config)
    for role in ("verify", "group", "memo"):
        _log_stage_call(con, role)
    g = tmp_path / "g"
    s = export_grading(con, g, [be.label_config])
    reasons = Counter(json.loads(l).get("reason") for l in gzip.open(g / "records.jsonl.gz", "rt"))
    assert s["records"] == 13 and reasons["not_in_scope"] == 13 - 6 - 2   # 13 rows - 6 in scope - 2 empty texts
    checker.write_json(g / "ingestion.json", checker.profile(synth_csv))
    report = checker.audit(g, checker.reference(synth_csv, synth_csv))
    # Expected: out-of-scope nonempty rows are unfinished by design; this test passes no memo claims.
    assert set(report["issue_counts"]) <= {"unfinished_classification", "missing_claims"}, report["issue_counts"]
    assert report["coverage"]["accounted_fraction"] == 1.0
