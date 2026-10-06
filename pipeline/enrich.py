"""Stage 3: enrichment runner (provider-agnostic).

Sends only the canonical review of each distinct pending text, at most `batch_size` (<= 50)
per request. Every returned ID is validated, results and the call log are saved in one
transaction per batch, and a restart picks up exactly the texts that still have no result.
Duplicate texts are not sent again; they become cache aliases at export time.
"""
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .calls import CallRecord, Usage, log_call
from .db import transaction
from .ledger import BudgetExceeded, backoff_delay, now
from .schema import validate_batch

MAX_BATCH = 50  # GRADING_CONTRACT.md: at most 50 reviews per model request


class BackendError(Exception):
    """A failed call. `sent` = request reached the provider (may be billed); `uncertain` = outcome unknown."""

    def __init__(self, message, request_id=None, usage=None, sent=True, uncertain=False, retryable=True):
        super().__init__(message)
        self.request_id, self.usage = request_id, usage or Usage()
        self.sent, self.uncertain, self.retryable = sent, uncertain, retryable


@dataclass
class BackendResult:
    request_id: str
    records: list            # raw items, each with review_id + label fields
    usage: Usage
    extra: dict = field(default_factory=dict)


class Backend:
    """Interface implemented by the Jev / Claude backends (pipeline/backends/*)."""
    provider: str
    model: str
    label_config: str
    settings: dict
    route = "primary"

    def estimate_cost(self, items) -> float:      # worst case, for spend reservation
        raise NotImplementedError

    def estimate_tokens(self, items) -> int:      # for the shared tokens/min limiter
        return 0

    def classify(self, items) -> BackendResult:   # items: [{"review_id", "text"}]
        raise NotImplementedError


@dataclass
class RunStats:
    batches_ok: int = 0
    batches_failed: int = 0
    records_saved: int = 0
    records_rejected: int = 0
    stopped_reason: str = "completed"
    wall_seconds: float = 0.0


def pending_after(con, done_configs, cursor, limit, max_attempts, primary_config, only_failed=False):
    """Next distinct texts (by canonical source order) with no result under any accepted config."""
    marks = ",".join("?" * len(done_configs))
    sql = f"""
        SELECT r.src_order, t.text_sha256, t.canonical_id, r.review_text, coalesce(f.attempts, 0) AS attempts
        FROM texts t JOIN reviews r ON r.review_id = t.canonical_id
        LEFT JOIN failures f ON f.text_sha256 = t.text_sha256 AND f.label_config = ?
        WHERE r.src_order > ?
          AND NOT EXISTS (SELECT 1 FROM results x WHERE x.text_sha256 = t.text_sha256 AND x.label_config IN ({marks}))
          AND coalesce(f.attempts, 0) {'>=' if only_failed else '<'} ?
        ORDER BY r.src_order LIMIT ?"""
    return con.execute(sql, (primary_config, cursor, *done_configs, max_attempts, limit)).fetchall()


def _bump_failures(con, text_shas, label_config, reason):
    for sha in text_shas:
        con.execute("""INSERT INTO failures(text_sha256, label_config, attempts, last_reason, updated_at)
                       VALUES (?,?,1,?,?) ON CONFLICT(text_sha256, label_config)
                       DO UPDATE SET attempts = attempts + 1, last_reason = excluded.last_reason,
                                     updated_at = excluded.updated_at""",
                    (sha, label_config, reason[:500], now()))


def run_enrichment(con, backend: Backend, *, run_id, phase, ledger, limiter, batch_size=50, workers=1,
                   max_batches=None, max_attempts=3, done_configs=None, fallback_from=None,
                   fallback_cap=None, stop_event=None, log=print, sleep=time.sleep, db_lock=None,
                   max_consecutive_failures=5):
    """Run until no pending work, the budget/batch limit is hit, or stop_event is set.

    fallback_from: label_config of the primary route; when set, only texts that exhausted
    `max_attempts` there are sent, capped at `fallback_cap` texts in total.
    """
    if not 1 <= batch_size <= MAX_BATCH:
        raise ValueError(f"batch_size must be 1..{MAX_BATCH}")
    if phase not in ("initial", "resume"):
        raise ValueError("phase must be initial or resume")
    stop_event = stop_event or threading.Event()
    db_lock = db_lock or threading.Lock()
    done_configs = list(done_configs or [backend.label_config])
    if backend.label_config not in done_configs:
        done_configs.append(backend.label_config)
    stale = ledger.mark_stale_unreconciled()
    if stale:
        log(f"[ledger] {stale} reservation(s) from an interrupted run marked unreconciled (still counted as spent)")

    stats, t0 = RunStats(), time.perf_counter()
    consecutive_failures = [0]
    in_flight, futures = set(), {}
    cursor, admitted_batches = -1, 0
    fallback_used = con.execute("SELECT count(*) FROM results WHERE label_config=?",
                                (backend.label_config,)).fetchone()[0] if fallback_from else 0

    def call(items, attempt):
        if attempt > 1:
            sleep(backoff_delay(attempt - 1))
        limiter.acquire(backend.estimate_tokens(items))
        started, t = now(), time.perf_counter()
        try:
            res = backend.classify(items)
            return ("ok", res, started, now(), time.perf_counter() - t)
        except BackendError as e:
            return ("err", e, started, now(), time.perf_counter() - t)
        except Exception as e:  # unexpected local error: treat as unknown outcome, never silently retry-free
            return ("err", BackendError(repr(e), sent=True, uncertain=True), started, now(), time.perf_counter() - t)

    def save(fut):
        kind, payload, started, ended, latency = fut.result()
        items, attempt, reservation = futures.pop(fut)
        sent = {it["review_id"]: it["text"] for it in items}
        sha_by_id = {it["review_id"]: it["text_sha256"] for it in items}
        base = dict(run_id=run_id, role="enrich", phase=phase, provider=backend.provider, model=backend.model,
                    settings=backend.settings, label_config=backend.label_config, review_ids=list(sent),
                    started_at=started, ended_at=ended, latency_s=round(latency, 4), attempt=attempt)
        with db_lock:
            if kind == "ok":
                valid, rejected = validate_batch(sent, payload.records)
                with transaction(con):
                    for rid, rec in valid.items():
                        con.execute("""INSERT INTO results(text_sha256, label_config, canonical_id, topic, intent,
                                sentiment, severity, entities, evidence_quote, needs_review, subtopic, request_id,
                                phase, route, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (sha_by_id[rid], backend.label_config, rid, rec["topic"], rec["intent"],
                                     float(rec["sentiment"]), rec["severity"],
                                     json.dumps(rec["entities"], ensure_ascii=False), rec["evidence_quote"],
                                     int(rec["needs_review"]), rec.get("subtopic"), payload.request_id, phase,
                                     backend.route, now()))
                    bad = [rid for rid in rejected if rid in sent]
                    _bump_failures(con, [sha_by_id[r] for r in bad], backend.label_config, "invalid output")
                    log_call(con, CallRecord(request_id=payload.request_id, outcome="succeeded", usage=payload.usage,
                                             extra={**payload.extra, "rejected": rejected}, **base))
                ledger.settle(reservation, payload.usage.cost_usd, payload.request_id)
                stats.batches_ok += 1
                consecutive_failures[0] = 0
                stats.records_saved += len(valid)
                stats.records_rejected += len(rejected)
            else:
                e = payload
                rid = e.request_id or f"failed-{run_id}-{started}-{items[0]['review_id']}"
                with transaction(con):
                    _bump_failures(con, list(sha_by_id.values()), backend.label_config, str(e))
                    log_call(con, CallRecord(request_id=rid, outcome="failed", usage=e.usage, error=str(e)[:500],
                                             extra={"uncertain": e.uncertain, "sent": e.sent}, **base))
                if not e.sent:
                    ledger.release(reservation)
                else:
                    # Unknown outcome or unknown price keeps the reservation counted until reconciled.
                    ledger.settle(reservation, None if e.uncertain else e.usage.cost_usd, rid)
                stats.batches_failed += 1
                consecutive_failures[0] += 1
                if consecutive_failures[0] >= max_consecutive_failures and not stop_event.is_set():
                    # Circuit breaker: a persistent error (bad key, outage) must not burn retries on every text.
                    stats.stopped_reason = f"circuit breaker: {consecutive_failures[0]} consecutive failed batches ({e})"
                    stop_event.set()
            in_flight.difference_update(sent)

    route_config = fallback_from or backend.label_config
    with ThreadPoolExecutor(max_workers=workers) as pool:
        capped = False
        while True:
            # Admit new batches while workers are free. One pass walks the queue in source order;
            # at the end of a pass the cursor wraps to -1 so earlier failures get bounded retries.
            while (not capped and not stop_event.is_set() and len(futures) < workers
                   and (max_batches is None or admitted_batches < max_batches)):
                with db_lock:
                    rows = pending_after(con, done_configs, cursor, batch_size * (workers + 4), max_attempts,
                                         route_config, only_failed=bool(fallback_from))
                rows = [r for r in rows if r["canonical_id"] not in in_flight]
                if not rows:
                    if cursor != -1:
                        cursor = -1
                        continue
                    break  # nothing admittable right now (done, or everything left is in flight)
                batch = rows[:batch_size]
                if fallback_from and fallback_cap is not None:
                    room = fallback_cap - fallback_used
                    if room <= 0:
                        stats.stopped_reason = f"fallback cap reached ({fallback_cap} texts)"
                        capped = True
                        break
                    batch = batch[:room]
                    fallback_used += len(batch)
                cursor = batch[-1]["src_order"]
                items = [{"review_id": r["canonical_id"], "text": r["review_text"], "text_sha256": r["text_sha256"]}
                         for r in batch]
                attempt = 1 + max(r["attempts"] for r in batch)
                try:
                    reservation = ledger.reserve(backend.estimate_cost(items))
                except BudgetExceeded as e:
                    stats.stopped_reason = f"budget: {e}"
                    stop_event.set()
                    break
                in_flight.update(it["review_id"] for it in items)
                futures[pool.submit(call, items, attempt)] = (items, attempt, reservation)
                admitted_batches += 1
            if not futures:
                if stop_event.is_set() and stats.stopped_reason == "completed":
                    stats.stopped_reason = "stopped by user"
                elif max_batches is not None and admitted_batches >= max_batches and stats.stopped_reason == "completed":
                    stats.stopped_reason = f"max_batches={max_batches} reached"
                break
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for fut in done:
                save(fut)
                log(f"[enrich] ok={stats.batches_ok} failed={stats.batches_failed} saved={stats.records_saved} "
                    f"rejected={stats.records_rejected} committed=${ledger.committed():.4f}")
    stats.wall_seconds = round(time.perf_counter() - t0, 3)
    return stats
