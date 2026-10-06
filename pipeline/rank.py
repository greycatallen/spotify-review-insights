"""Baseline ranking exactly as GRADING_CONTRACT.md defines it, in integer/Decimal arithmetic.

priority_score = severity_sum = complaint_count x mean_severity; order by score desc, issue_id asc;
mean to six decimals with decimal half-up rounding (the checker's own mean_string helper).
"""
from collections import defaultdict

from .config import checker

RANKING_HEADER = ("rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score")
CLAIM_METRICS = ("complaint_count", "severity_sum", "mean_severity", "priority_score")


def compute_ranking(memberships):
    """memberships: iterable of (issue_id, review_id, severity). Returns ranked list of dicts."""
    sev_by_issue, pairs = defaultdict(list), set()
    for issue_id, review_id, severity in memberships:
        if (issue_id, review_id) in pairs:
            raise ValueError(f"duplicate membership {(issue_id, review_id)}")
        if type(severity) is not int or not 1 <= severity <= 5:
            raise ValueError(f"bad severity for {review_id}: {severity!r}")
        pairs.add((issue_id, review_id))
        sev_by_issue[issue_id].append(severity)
    rows = []
    for iid, sevs in sev_by_issue.items():
        total = sum(sevs)
        rows.append({"issue_id": iid, "complaint_count": len(sevs), "severity_sum": total,
                     "mean_severity": checker.mean_string(total, len(sevs)), "priority_score": total})
    rows.sort(key=lambda r: (-r["priority_score"], r["issue_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def ranking_table(rows):
    return [[str(r[k]) for k in RANKING_HEADER] for r in rows]


def claim(claim_id, ranking_by_id, issue_id, metric):
    """A claims.csv row whose value is read from the computed ranking, never typed by hand."""
    if metric not in CLAIM_METRICS:
        raise ValueError(f"unsupported claim metric {metric}")
    return [claim_id, issue_id, metric, str(ranking_by_id[issue_id][metric])]
