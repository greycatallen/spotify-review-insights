"""Deterministic grouping and the memo citation/number audit (offline)."""
from conftest import FakeBackend
from pipeline.enrich import run_enrichment
from pipeline.group import assign_membership, evidence_pack, facet_for
from pipeline.ledger import RateLimiter, SpendLedger
from pipeline.memo import build_claims, check_memo
from pipeline.rank import compute_ranking


def test_facets():
    assert facet_for("usability", "Too many ADS between songs") == "ads"
    assert facet_for("playback", "keeps crashing on bluetooth") == "crash"
    assert facet_for("billing", "nothing matched here") == "general"


def test_every_complaint_in_exactly_one_issue(synth_db):
    con, be = synth_db, FakeBackend()
    run_enrichment(con, be, run_id="t", phase="initial", ledger=SpendLedger(con, 5, "t"),
                   limiter=RateLimiter(10_000), log=lambda *_: None)
    counts = assign_membership(con, [be.label_config])
    members = con.execute("SELECT review_id, count(*) FROM membership GROUP BY review_id").fetchall()
    assert all(n == 1 for _, n in members)
    complaints = con.execute("""SELECT count(*) FROM reviews rv JOIN results x USING(text_sha256)
                                WHERE x.intent IN ('complaint','cancellation')""").fetchone()[0]
    assert sum(counts.values()) == complaints == len(members)
    pack, sent = evidence_pack(con, [be.label_config], per_issue=2)
    assert all(len(p["examples"]) <= 2 for p in pack) and len(sent) <= 2 * len(pack)


def test_memo_audit_flags_unknown_claims_and_numbers():
    ranking = compute_ranking([("a.x", "r1", 4), ("a.x", "r2", 3), ("b.y", "r3", 2)])
    claims = build_claims(ranking)
    ok = "Issue a.x has 2 complaints [C1] with mean severity 3.500000 [C2]. 95 reviews were checked."
    audit = check_memo(ok, claims, {"reviews": 95})
    assert audit == {"unknown_claim_ids": [], "claims_cited": ["C1", "C2"], "unexplained_numbers": []}
    bad = "Issue a.x has 2 complaints [C1] costing $40,000 in revenue [C99]."
    audit = check_memo(bad, claims, {"reviews": 95})
    assert audit["unknown_claim_ids"] == ["C99"] and "40,000" in audit["unexplained_numbers"]
