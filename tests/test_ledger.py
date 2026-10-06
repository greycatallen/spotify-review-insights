"""Spend reservations are shared across workers and never let committed spend pass the cap."""
import threading

import pytest

from pipeline.db import connect
from pipeline.ledger import BudgetExceeded, RateLimiter, SpendLedger, backoff_delay


def test_reserve_settle_release(tmp_path):
    led = SpendLedger(connect(tmp_path / "s.sqlite"), cap_usd=1.0, run_id="t")
    a = led.reserve(0.6)
    with pytest.raises(BudgetExceeded):
        led.reserve(0.5)                      # 0.6 reserved + 0.5 > 1.0
    led.settle(a, 0.2)
    b = led.reserve(0.5)                      # 0.2 spent + 0.5 ok
    led.release(b)
    assert led.committed() == pytest.approx(0.2)


def test_unknown_price_or_outcome_keeps_reservation_counted(tmp_path):
    led = SpendLedger(connect(tmp_path / "s.sqlite"), cap_usd=1.0, run_id="t")
    a = led.reserve(0.4)
    led.settle(a, None)
    assert led.committed() == pytest.approx(0.4)


def test_crash_leftover_reservations_become_unreconciled(tmp_path):
    con = connect(tmp_path / "s.sqlite")
    SpendLedger(con, 1.0, "crashed").reserve(0.3)
    led = SpendLedger(con, 1.0, "next")
    assert led.mark_stale_unreconciled() == 1
    assert led.committed() == pytest.approx(0.3)


def test_concurrent_workers_never_exceed_cap(tmp_path):
    led = SpendLedger(connect(tmp_path / "s.sqlite"), cap_usd=1.0, run_id="t")
    ok = []

    def worker():
        for _ in range(50):
            try:
                ok.append(led.reserve(0.03))
            except BudgetExceeded:
                return

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 33 and led.committed() <= 1.0


def test_rate_limiter_waits_when_bucket_empty():
    t = [0.0]
    slept = []
    lim = RateLimiter(requests_per_min=2, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    lim.acquire(); lim.acquire(); lim.acquire()
    assert slept and sum(slept) >= 29


def test_backoff_is_bounded_and_jittered():
    assert backoff_delay(10, base=1, cap=60, rng=lambda: 1.0) == 60
    assert backoff_delay(1, rng=lambda: 0.5) == 0.5
