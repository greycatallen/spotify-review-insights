"""Stage 4: independent verification of a declared sample with a different model/prompt.

The verifier never sees the primary labels (independent judgement). Agreement is recorded per
review; disagreements are reported and can set needs_review, they never silently overwrite labels.
"""
import json
import random
import time

from .calls import CallRecord, log_call
from .db import transaction
from .enrich import BackendError
from .ledger import BudgetExceeded, now
from .schema import validate_batch


def sample_for_verification(con, label_config, fraction, seed="a5-verify-v1", include_needs_review=True, cap=None):
    """Deterministic sample of canonical review ids: hash-ordered fraction + all needs_review rows."""
    rows = con.execute("""SELECT x.canonical_id, x.needs_review, r.review_text FROM results x
                          JOIN reviews r ON r.review_id = x.canonical_id WHERE x.label_config=?""",
                       (label_config,)).fetchall()
    rng = random.Random(seed)
    picked = [r for r in rows if rng.random() < fraction or (include_needs_review and r["needs_review"])]
    picked.sort(key=lambda r: r["canonical_id"])
    return [{"review_id": r["canonical_id"], "text": r["review_text"]} for r in picked[:cap]]


def run_verify(con, backend, items, *, run_id, ledger, primary_config, batch_size=25, log=print):
    done = {r[0] for r in con.execute("SELECT review_id FROM verifications WHERE label_config=?",
                                      (backend.label_config,))}
    todo = [it for it in items if it["review_id"] not in done]   # cached: no repeat calls on a warm run
    primary = {r["canonical_id"]: dict(r) for r in con.execute(
        "SELECT canonical_id, topic, intent, severity FROM results WHERE label_config=?", (primary_config,))}
    stats = {"sent": 0, "agree_topic": 0, "agree_intent": 0, "agree_severity": 0, "compared": 0, "cached": len(items) - len(todo)}
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        try:
            res_id = ledger.reserve(backend.estimate_cost(batch))
        except BudgetExceeded as e:
            log(f"[verify] stopped: {e}")
            break
        started, t = now(), time.perf_counter()
        base = dict(run_id=run_id, role="verify", phase=None, provider=backend.provider, model=backend.model,
                    settings=backend.settings, label_config=backend.label_config,
                    review_ids=[b["review_id"] for b in batch], started_at=started)
        try:
            out = backend.classify(batch)
        except BackendError as e:
            with transaction(con):
                log_call(con, CallRecord(request_id=e.request_id or f"verify-failed-{started}", outcome="failed",
                                         usage=e.usage, error=str(e)[:500], ended_at=now(),
                                         latency_s=round(time.perf_counter() - t, 4), **base))
            ledger.settle(res_id, None if e.uncertain else e.usage.cost_usd) if e.sent else ledger.release(res_id)
            continue
        valid, rejected = validate_batch({b["review_id"]: b["text"] for b in batch}, out.records)
        with transaction(con):
            log_call(con, CallRecord(request_id=out.request_id, outcome="succeeded", usage=out.usage,
                                     ended_at=now(), latency_s=round(time.perf_counter() - t, 4),
                                     extra={**out.extra, "rejected": rejected}, **base))
            for rid, v in valid.items():
                p = primary.get(rid, {})
                agree = [int(p.get(k) == v[k]) for k in ("topic", "intent", "severity")]
                con.execute("INSERT OR REPLACE INTO verifications VALUES (?,?,?,?,?,?,?,?,?)",
                            (rid, backend.label_config, out.request_id, v["topic"], v["intent"], v["severity"], *agree))
                stats["compared"] += 1
                stats["agree_topic"] += agree[0]
                stats["agree_intent"] += agree[1]
                stats["agree_severity"] += agree[2]
        ledger.settle(res_id, out.usage.cost_usd, out.request_id)
        stats["sent"] += len(batch)
    return stats


def verification_report(con, verify_config, primary_config):
    rows = con.execute("""SELECT v.*, x.topic AS p_topic, x.intent AS p_intent, x.severity AS p_severity
                          FROM verifications v JOIN results x ON x.canonical_id = v.review_id AND x.label_config=?
                          WHERE v.label_config=?""", (primary_config, verify_config)).fetchall()
    n = len(rows)
    pct = lambda k: round(sum(r[k] for r in rows) / n, 4) if n else None
    return {"compared": n, "topic_agreement": pct("agrees_topic"), "intent_agreement": pct("agrees_intent"),
            "severity_agreement": pct("agrees_severity"),
            "disagreements": [json.loads(json.dumps({k: r[k] for k in r.keys()})) for r in rows
                              if not (r["agrees_topic"] and r["agrees_intent"])][:50]}
