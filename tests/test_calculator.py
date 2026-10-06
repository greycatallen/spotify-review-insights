"""Calculator arithmetic checks the instructor will run, on a SYNTHETIC fixture in a tmp dir.

- doubling API rates doubles the API subtotal; measured time and local compute stay unchanged
- changing projected record counts never changes the recorded 100-review results
- a blank price is reported as unknown, never as zero
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cost"))
import calculator  # noqa: E402


def _fixture(tmp, jev=False):
    d = tmp / "cost"
    d.mkdir()
    shutil.copy(ROOT / "cost" / "rates.csv", d / "rates.csv")
    a = json.loads((ROOT / "cost" / "assumptions.json").read_text())
    a["local_compute"].update(average_power_watts=30, electricity_usd_per_kwh=0.3)
    (d / "assumptions.json").write_text(json.dumps(a))
    calls = []
    for kind in ("cold", "warm"):
        if kind == "cold":
            for b in range(2):
                calls.append({"run_id": "R-cold", "run_kind": "cold", "role": "enrich", "request_id": f"e{b}",
                              "outcome": "succeeded", "provider": "typesafe" if jev else "anthropic",
                              "model": "jev" if jev else "claude-haiku-4-5", "review_ids": [f"r{b}{i}" for i in range(50)],
                              "input_tokens": None if jev else 4000, "output_tokens": None if jev else 2500,
                              "billed_units": 50 if jev else None, "billed_unit": "classification" if jev else None,
                              "attempt": 1, "settings": {}})
            calls.append({"run_id": "R-cold", "run_kind": "cold", "role": "enrich", "request_id": "e-fail",
                          "outcome": "failed", "provider": "anthropic", "model": "claude-haiku-4-5",
                          "review_ids": ["r00"], "input_tokens": 0, "output_tokens": 0, "attempt": 2, "settings": {}})
        for role, rid in (("verify", "v"), ("group", "g"), ("memo", "m")):
            if kind == "cold":
                calls.append({"run_id": "R-cold", "run_kind": "cold", "role": role, "request_id": f"{rid}-{kind}",
                              "outcome": "succeeded", "provider": "anthropic", "model": "claude-haiku-4-5",
                              "review_ids": ["r00"] if role == "verify" else [], "input_tokens": 1000,
                              "output_tokens": 300, "cache_read_tokens": 200, "attempt": 1, "settings": {}})
    with open(d / "pilot_calls.jsonl", "w") as f:
        f.writelines(json.dumps(c) + "\n" for c in calls)
    runs = [{"run_id": "R-cold", "run_kind": "cold", "input_sha256": "c884", "input_rows": 100, "completed": 100,
             "failed": 0, "quarantined": 0, "unique_texts": 100, "result_cache_hits": 0, "workers": 1,
             "batch_size": 50, "wall_seconds": 40.0, "ids_match_cost_100": True,
             "stage_seconds": {"enrich": 30.0, "verify": 4.0, "group": 3.0, "memo": 3.0}},
            {"run_id": "R-warm", "run_kind": "warm", "input_sha256": "c884", "input_rows": 100, "completed": 100,
             "failed": 0, "quarantined": 0, "unique_texts": 100, "result_cache_hits": 100, "workers": 1,
             "batch_size": 50, "wall_seconds": 0.5, "ids_match_cost_100": True, "stage_seconds": {}}]
    (d / "pilot_runs.json").write_text(json.dumps(runs))
    return d


def _run(d, *extra):
    return calculator.main(["--dir", str(d), "--out", str(d / "out"), *extra])


def test_cold_cost_is_recomputable_by_hand(tmp_path):
    rep = _run(_fixture(tmp_path))
    cold = rep["measured"]["cold"]
    # enrich: 2 x (4000 in x $1/M + 2500 out x $5/M) ; failed call billed 0
    assert cold["stages"]["enrich"]["cost"]["usd"] == round(2 * (4000e-6 + 2500 * 5e-6), 6)
    # each other stage: 1000 x 1e-6 + 300 x 5e-6 + 200 cache read x 0.1e-6
    assert cold["stages"]["memo"]["cost"]["usd"] == round(1000e-6 + 1500e-6 + 20e-6, 6)
    assert rep["measured"]["warm"]["stages"]["enrich"]["requests"] == 0
    assert (tmp_path / "cost" / "out" / "usage.csv").exists()


def test_doubling_rates_doubles_api_only(tmp_path):
    d = _fixture(tmp_path)
    a, b = _run(d), _run(d, "--rate-multiplier", "2")
    ca, cb = a["measured"]["cold"], b["measured"]["cold"]
    assert abs(cb["api_cost"]["usd"] - 2 * ca["api_cost"]["usd"]) < 1e-9
    for name in a["projection"]:
        assert abs(b["projection"][name]["api_total"]["usd"] - 2 * a["projection"][name]["api_total"]["usd"]) < 1e-3
        assert b["projection"][name]["elapsed_hours_modeled"] == a["projection"][name]["elapsed_hours_modeled"]
    assert ca["wall_seconds"] == cb["wall_seconds"]
    assert a["local_compute"]["estimated_usd"] == b["local_compute"]["estimated_usd"]


def test_projected_count_does_not_change_measured(tmp_path):
    d = _fixture(tmp_path)
    a = _run(d)
    asm = json.loads((d / "assumptions.json").read_text())
    asm["full_run"]["distinct_nonempty_texts"] = 1
    (d / "assumptions.json").write_text(json.dumps(asm))
    b = _run(d)
    assert a["measured"] == b["measured"]
    assert a["projection"]["base"]["api_total"] != b["projection"]["base"]["api_total"]


def test_unknown_price_is_not_zero(tmp_path):
    rep = _run(_fixture(tmp_path, jev=True))
    enrich = rep["measured"]["cold"]["stages"]["enrich"]["cost"]
    assert enrich["usd"] is None and enrich["unknown_items"] == 2
    assert rep["measured"]["cold"]["api_cost"]["usd"] is None
    assert rep["projection"]["base"]["api_total"]["usd"] is None


def test_budget_warning(tmp_path):
    rep = _run(_fixture(tmp_path), "--budget", "0.01")
    assert all(s["over_budget"] for s in rep["projection"].values())
    assert "OVER" in (tmp_path / "cost" / "out" / "report.md").read_text()


def test_no_pilot_yet_is_reported_not_invented(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    shutil.copy(ROOT / "cost" / "rates.csv", d)
    shutil.copy(ROOT / "cost" / "assumptions.json", d)
    rep = _run(d)
    assert rep["measured"] == {} and rep["projection"] == {}
    assert "No measured pilot found" in (d / "out" / "report.md").read_text()
