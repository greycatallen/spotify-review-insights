#!/usr/bin/env python3
"""Offline cost/runtime calculator for the 100-review pilot (COST_CALCULATOR.md).

Default action is OFFLINE REPLAY: it reads saved measurements and editable rates/assumptions,
recomputes every number and writes usage.csv, report.json and report.md. It never imports an
API client, reads keys or makes network calls. Paid pilot execution is a separate, explicit
command in the pipeline CLI (`python -m pipeline.cli pilot --execute ...`).

    python3 cost/calculator.py                      # replay with cost/ defaults
    python3 cost/calculator.py --rate-multiplier 2  # what-if: double every API rate
    python3 cost/calculator.py --budget 10          # what-if: different budget

Billing formula (per mutually exclusive item): item_cost = billed_units x price_usd / per_units.
Standard library only.
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
USAGE_ITEMS = ("input_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_read_tokens",
               "output_tokens", "classification", "request")
ROLES = ("enrich", "verify", "group", "memo")


# ---------------------------------------------------------------- inputs

def load_rates(path, multiplier=1.0):
    rates = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            key = (r["provider"], r["model"], r["tier"], r["item"])
            price = r["price_usd"].strip()
            rates[key] = {**r, "unit_price": (float(price) * multiplier / float(r["per_units"])) if price else None}
    return rates


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def usage_rows(calls):
    """One row per (call, billed item). Units come straight from the saved provider usage."""
    rows = []
    for c in calls:
        mapping = {
            "input_tokens": c.get("input_tokens"),
            "cache_write_5m_tokens": c.get("cache_write_tokens"),
            "cache_read_tokens": c.get("cache_read_tokens"),
            "output_tokens": c.get("output_tokens"),
        }
        if c.get("billed_unit"):
            mapping[c["billed_unit"]] = c.get("billed_units")
        for item, units in mapping.items():
            # Keep one input_tokens row even when usage is missing, so missing usage shows as unknown,
            # unless the call is billed in another unit (e.g. per classification) and reports no tokens.
            keep_missing = item == "input_tokens" and not c.get("billed_unit")
            if units in (None, 0) and not keep_missing:
                continue
            rows.append({"run_id": c["run_id"], "run_kind": c["run_kind"], "role": c["role"],
                         "request_id": c["request_id"], "outcome": c["outcome"], "provider": c["provider"],
                         "model": c["model"], "tier": c.get("tier", "standard"), "item": item,
                         "billed_units": units})
    return rows


# ---------------------------------------------------------------- arithmetic

def price_rows(rows, rates):
    """Adds item_cost (None when units or price are unknown)."""
    out = []
    for r in rows:
        rate = rates.get((r["provider"], r["model"], r["tier"], r["item"]))
        price = rate["unit_price"] if rate else None
        units = r["billed_units"]
        cost = units * price if (units is not None and price is not None) else None
        out.append({**r, "unit_price_usd": price, "item_cost_usd": cost,
                    "rate_source": rate["source_url"] if rate else "", "rate_checked": rate["checked_date"] if rate else ""})
    return out


class Money:
    """Sum that stays honest: unknown parts are counted, never treated as zero."""

    def __init__(self):
        self.known, self.unknown_items = 0.0, 0

    def add(self, v):
        if v is None:
            self.unknown_items += 1
        else:
            self.known += v
        return self

    def value(self):
        return self.known if self.unknown_items == 0 else None

    def as_dict(self):
        return {"usd": round(self.known, 6) if self.unknown_items == 0 else None,
                "known_part_usd": round(self.known, 6), "unknown_items": self.unknown_items}


def measured(priced, calls, runs):
    by_kind = {}
    for run in runs:
        kind = run["run_kind"]
        rc = [c for c in calls if c["run_id"] == run["run_id"]]
        rp = [p for p in priced if p["run_id"] == run["run_id"]]
        stage = {}
        for role in ROLES:
            cs = [c for c in rc if c["role"] == role]
            m = Money()
            for p in rp:
                if p["role"] == role:
                    m.add(p["item_cost_usd"])
            stage[role] = {
                "requests": len(cs),
                "failed": sum(c["outcome"] == "failed" for c in cs),
                "retries": sum(c.get("attempt", 1) > 1 for c in cs),
                "fallback_requests": sum(c.get("route") == "fallback" for c in cs),
                "reviews_sent": sum(len(c.get("review_ids", [])) for c in cs),
                "input_tokens": sum(c.get("input_tokens") or 0 for c in cs),
                "output_tokens": sum(c.get("output_tokens") or 0 for c in cs),
                "cache_read_tokens": sum(c.get("cache_read_tokens") or 0 for c in cs),
                "usage_missing_calls": sum(c.get("input_tokens") is None and not c.get("billed_unit") for c in cs),
                "cost": m.as_dict(),
                "seconds": run.get("stage_seconds", {}).get(role),
                "settings": sorted({json.dumps({"model": c["model"], **c.get("settings", {})}, sort_keys=True)
                                    for c in cs}),
            }
        total = Money()
        for p in rp:
            total.add(p["item_cost_usd"])
        n_in, n_done = run["input_rows"], run["completed"]
        t = total.value()
        by_kind[kind] = {
            **{k: run.get(k) for k in ("run_id", "input_sha256", "input_rows", "completed", "failed", "quarantined",
                                       "unique_texts", "result_cache_hits", "workers", "batch_size",
                                       "wall_seconds", "started_at", "label_config")},
            "ids_match_cost_100": run.get("ids_match_cost_100"),
            "stages": stage,
            "api_cost": total.as_dict(),
            "cost_per_1000_input_rows_usd": round(t / n_in * 1000, 6) if t is not None and n_in else None,
            "cost_per_completed_record_usd": round(t / n_done, 8) if t is not None and n_done else None,
            "throughput_rows_per_s": round(n_in / run["wall_seconds"], 3) if run.get("wall_seconds") else None,
            "provider_prompt_cache_read_tokens": sum(s["cache_read_tokens"] for s in stage.values()),
        }
    return by_kind


def project(cold, assumptions, rates, budget, rate_multiplier):
    """Full-run scenarios extrapolated stage by stage from the cold pilot. Pure arithmetic."""
    full = assumptions["full_run"]
    st = cold["stages"]
    enrich_texts = max(cold["unique_texts"] or 0, 1)

    def per_text(role, denom):
        v = st[role]["cost"]["usd"]
        return None if v is None or not denom else v / denom

    enrich_unit = per_text("enrich", enrich_texts)        # includes measured retries/failed attempts
    verify_unit = per_text("verify", st["verify"]["reviews_sent"])
    fixed_gm = Money().add(st["group"]["cost"]["usd"]).add(st["memo"]["cost"]["usd"]).value()

    # Fallback per text: measured if the pilot used it, else enrich tokens/text at the fallback model's rates.
    fb = assumptions["fallback_model"]
    fb_in = rates.get((fb["provider"], fb["model"], fb["tier"], "input_tokens"), {}).get("unit_price")
    fb_out = rates.get((fb["provider"], fb["model"], fb["tier"], "output_tokens"), {}).get("unit_price")
    tok_in = st["enrich"]["input_tokens"] / enrich_texts
    tok_out = st["enrich"]["output_tokens"] / enrich_texts
    fallback_unit = (tok_in * fb_in + tok_out * fb_out) if (fb_in is not None and fb_out is not None and tok_in) else None

    sec = {r: st[r]["seconds"] for r in ROLES}
    overhead = sum(v for v in assumptions["one_time_overhead_seconds"].values() if v) or 0.0

    out = {}
    for reuse in (True, False):
        work = full["distinct_nonempty_texts"] if reuse else full["nonempty_texts"]
        for name, sc in assumptions["scenarios"].items():
            items = {
                "enrich": None if enrich_unit is None else enrich_unit * work * (1 + sc["extra_retry_fraction"]),
                "fallback": None if fallback_unit is None else fallback_unit * work * min(
                    sc["fallback_fraction"], assumptions["max_fallback_fraction"]),
                "verify": None if verify_unit is None else verify_unit * work * assumptions["verify_fraction"],
                "group_memo": None if fixed_gm is None else fixed_gm * sc["group_memo_scale"],
            }
            total = Money()
            for v in items.values():
                total.add(v)
            # Time: per-text seconds at the measured worker count, scaled by the modeled speedup.
            speed = max(sc["worker_speedup"], 1.0)
            secs = {
                "enrich": None if not sec["enrich"] else sec["enrich"] / enrich_texts * work * (1 + sc["extra_retry_fraction"]) / speed,
                "verify": None if not sec["verify"] or not st["verify"]["reviews_sent"] else
                          sec["verify"] / st["verify"]["reviews_sent"] * work * assumptions["verify_fraction"] / speed,
                "group_memo": None if sec["group"] is None or sec["memo"] is None else (sec["group"] + sec["memo"]) * sc["group_memo_scale"],
            }
            known_secs = [v for v in secs.values() if v is not None]
            t = total.value()
            out[f"{name}{'' if reuse else '_no_reuse'}"] = {
                "exact_text_reuse": reuse, "work_texts": work,
                "rows_accounted": full["input_rows"], "nonempty_outputs": full["nonempty_texts"],
                "empty_quarantines": full["empty_text_quarantines"],
                "assumptions": sc, "cost_by_stage_usd": {k: (round(v, 4) if v is not None else None) for k, v in items.items()},
                "api_total": {**total.as_dict(), "usd": round(t, 4) if t is not None else None},
                "elapsed_hours_modeled": round((sum(known_secs) + overhead) / 3600, 2) if known_secs else None,
                "elapsed_unknown_stages": [k for k, v in secs.items() if v is None],
                "over_budget": (t is not None and t > budget) or (t is None and total.known > budget),
            }
    return out


# ---------------------------------------------------------------- report

def fmt(v, d=4):
    return "unknown" if v is None else f"${v:,.{d}f}"


def render(rep):
    m, L = rep["measured"], []
    L += ["# 100-review pilot: cost & runtime calculator", "",
          f"Generated by `python3 cost/calculator.py` (offline replay, no API calls). "
          f"Rate multiplier: {rep['rate_multiplier']}. Budget: ${rep['budget_usd']:.2f}.", ""]
    if not m:
        L += ["**No measured pilot found.** `pilot_calls.jsonl` / `pilot_runs.json` are empty. "
              "Nothing below is estimated until the explicit paid pilot has run.", ""]
        return "\n".join(L)
    L += ["## Measured (cold vs warm)", "", "| | " + " | ".join(m) + " |", "|---|" + "---|" * len(m)]
    rows = [("Input SHA-256", lambda k: f"`{(m[k]['input_sha256'] or '')[:12]}…`"),
            ("IDs match cost_100.csv", lambda k: str(m[k]["ids_match_cost_100"])),
            ("Rows / completed / failed", lambda k: f"{m[k]['input_rows']} / {m[k]['completed']} / {m[k]['failed']}"),
            ("Unique texts / result-cache hits", lambda k: f"{m[k]['unique_texts']} / {m[k]['result_cache_hits']}"),
            ("Workers / batch size", lambda k: f"{m[k]['workers']} / {m[k]['batch_size']}"),
            ("Enrich requests (failed, retries)", lambda k: "{requests} ({failed}, {retries})".format(**m[k]["stages"]["enrich"])),
            ("Wall-clock seconds", lambda k: str(m[k]["wall_seconds"])),
            ("Throughput rows/s", lambda k: str(m[k]["throughput_rows_per_s"])),
            ("API cost", lambda k: fmt(m[k]["api_cost"]["usd"], 6)),
            ("Cost / 1,000 input rows", lambda k: fmt(m[k]["cost_per_1000_input_rows_usd"], 6)),
            ("Cost / completed record", lambda k: fmt(m[k]["cost_per_completed_record_usd"], 8)),
            ("Provider prompt-cache read tokens", lambda k: str(m[k]["provider_prompt_cache_read_tokens"]))]
    for label, f in rows:
        L.append(f"| {label} | " + " | ".join(f(k) for k in m) + " |")
    L += ["", "### Cold run by stage", "", "| Stage | Requests | Failed | Reviews sent | In tok | Out tok | Cost | Seconds | Settings |",
          "|---|---|---|---|---|---|---|---|---|"]
    for role, s in m["cold"]["stages"].items():
        L.append(f"| {role} | {s['requests']} | {s['failed']} | {s['reviews_sent']} | {s['input_tokens']} | "
                 f"{s['output_tokens']} | {fmt(s['cost']['usd'], 6)} | {s['seconds']} | {'<br>'.join(s['settings'])} |")
    L += ["", "## Full-run scenarios (660,622 rows; 660,609 classified; 13 quarantined)", "",
          "| Scenario | Texts sent | Enrich | Fallback | Verify | Group+memo | **API total** | Modeled hours | Budget |",
          "|---|---|---|---|---|---|---|---|---|"]
    for name, s in rep["projection"].items():
        c = s["cost_by_stage_usd"]
        warn = "⚠️ OVER" if s["over_budget"] else "ok"
        L.append(f"| {name} | {s['work_texts']:,} | {fmt(c['enrich'],2)} | {fmt(c['fallback'],2)} | {fmt(c['verify'],2)} | "
                 f"{fmt(c['group_memo'],2)} | **{fmt(s['api_total']['usd'],2)}** | {s['elapsed_hours_modeled']} | {warn} |")
    if any(s["over_budget"] for s in rep["projection"].values()):
        L += ["", f"> **Warning:** at least one scenario exceeds the ${rep['budget_usd']:.2f} budget."]
    if any(s["api_total"]["unknown_items"] for s in rep["projection"].values()):
        L += ["", "> Some stages have unknown prices or usage; totals marked unknown are not zero."]
    lc = rep["local_compute"]
    L += ["", "## Local compute (separate from API spend)", "",
          f"Device: {lc['device']}. Power: {lc['average_power_watts'] or 'unknown'} W; "
          f"electricity: {lc['electricity_usd_per_kwh'] or 'unknown'} $/kWh → local cost: "
          f"{fmt(lc['estimated_usd'], 4)}.", "",
          "Formulas: item_cost = billed_units × price_usd / per_units; total = Σ item_cost. "
          "Sources: `pilot_calls.jsonl`, `pilot_runs.json`, `rates.csv`, `assumptions.json`; derived `usage.csv`."]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=HERE, help="folder with pilot_calls.jsonl, pilot_runs.json, rates.csv")
    ap.add_argument("--rates", type=Path)
    ap.add_argument("--assumptions", type=Path)
    ap.add_argument("--rate-multiplier", type=float, default=1.0)
    ap.add_argument("--budget", type=float)
    ap.add_argument("--out", type=Path, help="output folder (default: --dir)")
    args = ap.parse_args(argv)
    d = args.dir
    out = args.out or d
    rates = load_rates(args.rates or d / "rates.csv", args.rate_multiplier)
    assumptions = json.loads((args.assumptions or d / "assumptions.json").read_text(encoding="utf-8"))
    budget = args.budget if args.budget is not None else assumptions["budget_usd"]
    calls_path, runs_path = d / "pilot_calls.jsonl", d / "pilot_runs.json"
    calls = load_jsonl(calls_path) if calls_path.exists() else []
    runs = json.loads(runs_path.read_text(encoding="utf-8")) if runs_path.exists() else []

    priced = price_rows(usage_rows(calls), rates)
    meas = measured(priced, calls, runs) if runs else {}
    proj = project(meas["cold"], assumptions, rates, budget, args.rate_multiplier) if "cold" in meas else {}
    lc = dict(assumptions["local_compute"])
    hrs = (meas.get("cold", {}).get("wall_seconds") or 0) / 3600
    lc["estimated_usd"] = (lc["average_power_watts"] / 1000 * hrs * lc["electricity_usd_per_kwh"]
                           if lc.get("average_power_watts") and lc.get("electricity_usd_per_kwh") and hrs else None)
    rep = {"rate_multiplier": args.rate_multiplier, "budget_usd": budget, "measured": meas, "projection": proj,
           "local_compute": lc}

    out.mkdir(parents=True, exist_ok=True)
    with open(out / "usage.csv", "w", encoding="utf-8", newline="") as f:
        cols = ["run_id", "run_kind", "role", "request_id", "outcome", "provider", "model", "tier", "item",
                "billed_units", "unit_price_usd", "item_cost_usd", "rate_source", "rate_checked"]
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(priced)
    (out / "report.json").write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render(rep), encoding="utf-8")
    print(render(rep))
    return rep


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
