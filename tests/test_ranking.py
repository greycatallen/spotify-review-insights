"""Baseline ranking must equal the checker's own calculation, including half-up rounding."""
import random

import pytest

from pipeline.config import checker
from pipeline.rank import claim, compute_ranking


def test_half_up_not_bankers():
    # 129/128 = 1.0078125 exactly: half-up -> 1.007813 (banker's rounding would give 1.007812)
    rows = compute_ranking([("i", f"r{k}", 2 if k == 0 else 1) for k in range(128)])
    assert rows[0]["mean_severity"] == "1.007813"


def test_order_score_desc_then_issue_id_asc():
    m = [("b", "r1", 4), ("a", "r2", 4), ("c", "r3", 5), ("c", "r4", 1), ("d", "r5", 2)]
    rows = compute_ranking(m)
    # c has score 6 (> 4), then a/b tie at 4 broken by issue_id, then d
    assert [r["issue_id"] for r in rows] == ["c", "a", "b", "d"]
    assert [r["rank"] for r in rows] == [1, 2, 3, 4]


def test_matches_checker_on_random_data():
    rng = random.Random(7)
    m = [(f"issue-{rng.randint(0, 30):02d}", f"r{k}", rng.randint(1, 5)) for k in range(5000)]
    ours = compute_ranking(m)
    by = {}
    for iid, _, s in m:
        by.setdefault(iid, []).append(s)
    ref = sorted(({"issue_id": i, "priority_score": sum(v), "mean_severity": checker.mean_string(sum(v), len(v))}
                  for i, v in by.items()), key=lambda x: (-x["priority_score"], x["issue_id"]))
    assert [(r["issue_id"], r["mean_severity"]) for r in ours] == [(r["issue_id"], r["mean_severity"]) for r in ref]


def test_duplicate_membership_and_bad_severity_rejected():
    with pytest.raises(ValueError):
        compute_ranking([("a", "r1", 3), ("a", "r1", 3)])
    with pytest.raises(ValueError):
        compute_ranking([("a", "r1", 3.0)])


def test_claims_read_from_ranking():
    rows = compute_ranking([("a", "r1", 3), ("a", "r2", 4)])
    by = {r["issue_id"]: r for r in rows}
    assert claim("C1", by, "a", "mean_severity") == ["C1", "a", "mean_severity", "3.500000"]
    with pytest.raises(ValueError):
        claim("C2", by, "a", "revenue")
