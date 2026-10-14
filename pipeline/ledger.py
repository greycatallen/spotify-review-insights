"""Shared spend ledger and rate limiter used by every worker.

Spend: before dispatch, a worker reserves the call's worst-case cost. Admission is refused when
spent + reserved + next reservation would exceed the cap. After the call the reservation is
settled with the actual cost (or released if nothing was sent). Reservations still open at
startup belong to calls whose outcome is unknown (crash/timeout): they are marked `unreconciled`
and keep counting as spent until reconciled against the provider dashboard.

The ledger lives in the run's SQLite file, so separate processes share it too.
"""
import random
import threading
import time
from datetime import datetime, timezone

from .db import transaction


class BudgetExceeded(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SpendLedger:
    def __init__(self, con, cap_usd: float, run_id: str, lock=None, scope: str | None = None):
        """scope (e.g. a provider name) gives this ledger its own cap: its entries are stored as
        "<scope>:<run_id>" and only those count toward it. Without a scope, every entry counts."""
        self.con, self.cap, self.scope = con, float(cap_usd), scope
        self.run_id = f"{scope}:{run_id}" if scope else run_id
        self.lock = lock or threading.Lock()

    def committed(self) -> float:
        """Settled actuals + open/unreconciled reservations, across all runs in this state file."""
        where, args = ("WHERE run_id LIKE ?", (f"{self.scope}:%",)) if self.scope else ("", ())
        row = self.con.execute(f"""
            SELECT coalesce(sum(CASE WHEN status='settled' THEN actual_usd
                                     WHEN status IN ('reserved','unreconciled') THEN reserved_usd
                                     ELSE 0 END), 0) FROM spend {where}""", args).fetchone()
        return float(row[0])

    def reserve(self, usd: float) -> int:
        with self.lock, transaction(self.con):
            committed = self.committed()
            if committed + usd > self.cap:
                raise BudgetExceeded(f"cap ${self.cap:.4f}: committed ${committed:.4f} + next ${usd:.4f}")
            cur = self.con.execute(
                "INSERT INTO spend(run_id, reserved_usd, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (self.run_id, usd, "reserved", now(), now()))
            return cur.lastrowid

    def settle(self, reservation_id: int, actual_usd: float | None, request_id: str | None = None):
        """actual_usd=None means the price is unknown: keep the reservation counted (unreconciled)."""
        status = "settled" if actual_usd is not None else "unreconciled"
        with self.lock, transaction(self.con):
            self.con.execute("UPDATE spend SET actual_usd=?, status=?, request_id=?, updated_at=? "
                             "WHERE reservation_id=?", (actual_usd, status, request_id, now(), reservation_id))

    def release(self, reservation_id: int):
        """Nothing was sent (e.g. failed before the HTTP request): free the reservation."""
        with self.lock, transaction(self.con):
            self.con.execute("UPDATE spend SET status='released', updated_at=? WHERE reservation_id=?",
                             (now(), reservation_id))

    def mark_stale_unreconciled(self) -> int:
        with self.lock, transaction(self.con):
            where, args = (" AND run_id LIKE ?", (f"{self.scope}:%",)) if self.scope else ("", ())
            return self.con.execute("UPDATE spend SET status='unreconciled', updated_at=? WHERE status='reserved'" + where,
                                    (now(), *args)).rowcount


class RateLimiter:
    """Thread-safe token buckets for requests/minute and tokens/minute, shared by all workers."""

    def __init__(self, requests_per_min: float, tokens_per_min: float | None = None, clock=time.monotonic,
                 sleep=time.sleep):
        self.rpm, self.tpm = requests_per_min, tokens_per_min
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.req_tokens, self.tok_tokens = float(requests_per_min), float(tokens_per_min or 0)
        self.last = clock()

    def _refill(self):
        t = self.clock()
        dt, self.last = t - self.last, t
        self.req_tokens = min(self.rpm, self.req_tokens + dt * self.rpm / 60)
        if self.tpm:
            self.tok_tokens = min(self.tpm, self.tok_tokens + dt * self.tpm / 60)

    def acquire(self, est_tokens: int = 0):
        while True:
            with self.lock:
                self._refill()
                need_tok = min(est_tokens, self.tpm) if self.tpm else 0
                if self.req_tokens >= 1 and (not self.tpm or self.tok_tokens >= need_tok):
                    self.req_tokens -= 1
                    if self.tpm:
                        self.tok_tokens -= need_tok
                    return
                wait = max((1 - self.req_tokens) * 60 / self.rpm, 0.01)
            self.sleep(wait)


def backoff_delay(attempt: int, base=1.0, cap=60.0, rng=random.random) -> float:
    """Exponential backoff with full jitter."""
    return rng() * min(cap, base * 2 ** (attempt - 1))
