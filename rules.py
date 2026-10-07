"""Deterministic rules: data health, true cycle time, OEE, loss finder, capacity verdict.

Every number the agent reports comes from here. Claude never does maths.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

SHORT_STOP_MAX_MIN = 5          # stops shorter than this are "short stops"
CT_FLAG_PCT = 10                # flag SAP vs PLC cycle time gaps above this
COUNT_MISMATCH_WARN_PCT = 2     # PLC vs SAP part count
COUNT_MISMATCH_FAIL_PCT = 5
MISSING_FAIL_PCT = 10           # missing PLC time as % of planned time

OWNERS = {
    "Short stops": "Maintenance",
    "Breakdowns": "Maintenance",
    "Idle after start-up / breaks": "Shift lead",
    "Idle at job changes": "Shift lead",
    "Other idle": "Shift lead",
    "Slow running": "Industrial engineer",
    "Scrap": "Quality",
}
LIKELY_CAUSE = {
    "Short stops": "likely feeder jams or sensor faults",
    "Breakdowns": "equipment failure",
    "Idle after start-up / breaks": "no relief cover at start-up and breaks",
    "Idle at job changes": "die change not prepared before the run ends",
    "Other idle": "waiting on material or operators",
    "Slow running": "press run below rated speed",
    "Scrap": "process or material quality issue",
}


def load(data_dir=DATA_DIR):
    d = {
        "plc": pd.read_csv(data_dir / "plc_state_log.csv", parse_dates=["timestamp"]),
        "master": pd.read_csv(data_dir / "sap_machine_master.csv"),
        "bookings": pd.read_csv(data_dir / "sap_bookings.csv", parse_dates=["date"]),
        "calendar": pd.read_csv(data_dir / "shift_calendar.csv", parse_dates=["date"]),
        "demand": pd.read_csv(data_dir / "demand.csv", parse_dates=["date"]),
    }
    d["costs"] = pd.read_csv(data_dir / "costs.csv").set_index("item")["value"].to_dict()
    return d


def _ts(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def open_shifts(data, day):
    cal = data["calendar"]
    return cal[(cal["date"] == pd.Timestamp(day)) & cal["is_open"]]


def intervals(data, machine, day):
    """Turn PLC events into timed intervals, clipped to shifts, with breaks split out."""
    plc = data["plc"][data["plc"]["machine"] == machine]
    out = []
    for _, sh in open_shifts(data, day).iterrows():
        s, e = _ts(day, sh["start"]), _ts(day, sh["end"])
        bs, be = _ts(day, sh["break_start"]), _ts(day, sh["break_end"])
        rows = plc[(plc["timestamp"] >= s) & (plc["timestamp"] < e)].reset_index(drop=True)
        if rows.empty:
            continue
        nxt = rows.shift(-1)
        rows["end"] = nxt["timestamp"].fillna(pd.Timestamp(e))
        rows["parts"] = (nxt["part_count"] - rows["part_count"]).fillna(0)
        rows["prev_order"] = rows["order_id"].shift(1)
        rows["shift"] = sh["shift"]
        for r in rows.itertuples():
            start, end = r.timestamp.to_pydatetime(), r.end.to_pydatetime()
            # split off planned break time
            planned = max(timedelta(0), min(end, be) - max(start, bs))
            dur = (end - start - planned).total_seconds()
            out.append({
                "shift": r.shift, "start": start, "end": end, "state": r.state,
                "dur_s": dur, "planned_s": planned.total_seconds(), "parts": r.parts,
                "starts_shift": start == s, "touches_break": planned.total_seconds() > 0 or start == be,
                "job_change": r.state == "idle" and isinstance(r.prev_order, str) and r.prev_order != r.order_id,
            })
    return pd.DataFrame(out)


# 1. Data health ------------------------------------------------------------
def data_health(data, day, machine):
    issues, status = [], "PASS"
    day_ts = pd.Timestamp(day)
    b = data["bookings"]

    def bump(level):
        nonlocal status
        order = ["PASS", "WARN", "FAIL"]
        status = max(status, level, key=order.index)

    for m in data["master"]["machine"]:
        iv = intervals(data, m, day)
        if iv.empty:
            continue
        ct = data["master"].set_index("machine").loc[m, "std_cycle_time_s"]
        # missing PLC data: a "running" interval far longer than any cycle
        gaps = iv[(iv["state"] == "running") & (iv["dur_s"] > 10 * ct)]
        planned = _planned_seconds(data, day)
        if not gaps.empty:
            pct = 100 * gaps["dur_s"].sum() / planned
            lvl = "FAIL" if (m == machine and pct > MISSING_FAIL_PCT) else "WARN"
            bump(lvl)
            issues.append({"level": lvl, "machine": m,
                           "issue": f"PLC log gap of {gaps['dur_s'].sum() / 60:.0f} min ({pct:.1f}% of planned time)"})
        for sh, g in iv.groupby("shift"):
            plc_parts = g["parts"].sum()
            bk = b[(b["date"] == day_ts) & (b["shift"] == sh) & (b["machine"] == m)]
            if bk.empty:
                lvl = "FAIL" if m == machine else "WARN"
                bump(lvl)
                issues.append({"level": lvl, "machine": m, "issue": f"Shift {sh}: no SAP booking"})
                continue
            sap_parts = int(bk["good_qty"].iloc[0] + bk["scrap_qty"].iloc[0])
            diff = 100 * abs(sap_parts - plc_parts) / max(plc_parts, 1)
            if diff > COUNT_MISMATCH_WARN_PCT:
                lvl = "FAIL" if (m == machine and diff > COUNT_MISMATCH_FAIL_PCT) else "WARN"
                bump(lvl)
                issues.append({"level": lvl, "machine": m,
                               "issue": f"Shift {sh}: PLC {plc_parts:.0f} parts vs SAP {sap_parts} ({diff:.1f}% gap)"})

    # zero scrap booked anywhere in the last 7 days is not believable on a press line
    recent = b[(b["date"] <= day_ts) & (b["date"] > day_ts - pd.Timedelta(days=7)) & (b["scrap_qty"] == 0)]
    for r in recent.itertuples():
        bump("WARN")
        issues.append({"level": "WARN", "machine": r.machine,
                       "issue": f"{r.date:%Y-%m-%d} shift {r.shift}: zero scrap booked ({r.good_qty} good). "
                                f"Scrap was likely booked as good; quality is overstated for that shift."})
    return {"status": status, "issues": issues}


def _planned_seconds(data, day):
    total = 0
    for _, sh in open_shifts(data, day).iterrows():
        total += (_ts(day, sh["end"]) - _ts(day, sh["start"])).total_seconds()
        total -= (_ts(day, sh["break_end"]) - _ts(day, sh["break_start"])).total_seconds()
    return total


# 2. True cycle time -------------------------------------------------------
def true_cycle_time(data, machine, day, lookback_days=7):
    sap_ct = float(data["master"].set_index("machine").loc[machine, "std_cycle_time_s"])
    cycles = []
    for i in range(lookback_days):
        d = day - timedelta(days=i)
        iv = intervals(data, machine, d)
        if iv.empty:
            continue
        full = iv[(iv["state"] == "running") & (iv["parts"] == 1) & (iv["dur_s"] < 2 * sap_ct)]
        cycles += full["dur_s"].tolist()
    true_ct = float(pd.Series(cycles).median())
    diff_pct = 100 * (sap_ct - true_ct) / true_ct
    return {"sap_ct_s": sap_ct, "true_ct_s": round(true_ct, 1), "diff_pct": round(diff_pct, 1),
            "flag": abs(diff_pct) > CT_FLAG_PCT, "cycles_sampled": len(cycles), "owner": "Industrial engineer"}


# 3 + 4. Metrics and loss finder -------------------------------------------
def metrics_and_losses(data, machine, day, true_ct):
    iv = intervals(data, machine, day)
    planned = _planned_seconds(data, day)
    b = data["bookings"]
    bk = b[(b["date"] == pd.Timestamp(day)) & (b["machine"] == machine)]
    good, scrap = int(bk["good_qty"].sum()), int(bk["scrap_qty"].sum())

    running = iv[iv["state"] == "running"]
    missing = running[running["dur_s"] > 10 * true_ct]
    running = running.drop(missing.index)
    run_s = running["dur_s"].sum()
    total_parts = iv["parts"].sum()

    losses = {k: {"hours": 0.0, "events": 0} for k in OWNERS}

    def add(cat, secs, n=1):
        losses[cat]["hours"] += secs / 3600
        losses[cat]["events"] += n

    for r in iv[iv["state"] == "stopped"].itertuples():
        add("Short stops" if r.dur_s < SHORT_STOP_MAX_MIN * 60 else "Breakdowns", r.dur_s)
    for r in iv[iv["state"] == "idle"].itertuples():
        if r.dur_s <= 0:
            continue
        if r.starts_shift or r.touches_break:
            add("Idle after start-up / breaks", r.dur_s)
        elif r.job_change:
            add("Idle at job changes", r.dur_s)
        else:
            add("Other idle", r.dur_s)
    full = running[running["parts"] == 1]
    add("Slow running", (full["dur_s"] - true_ct).clip(lower=0).sum(), int((full["dur_s"] > 1.1 * true_ct).sum()))
    add("Scrap", scrap * true_ct, scrap)

    # When do short stops cluster? Find the worst 4-hour window.
    ss = iv[(iv["state"] == "stopped") & (iv["dur_s"] < SHORT_STOP_MAX_MIN * 60)]
    if not ss.empty:
        by_hour = ss["start"].apply(lambda t: t.hour).value_counts().reindex(range(24), fill_value=0)
        win = by_hour.rolling(4).sum()
        end_h = int(win.idxmax())
        losses["Short stops"]["window"] = f"{end_h - 3:02d}:00-{end_h + 1:02d}:00"
        losses["Short stops"]["events_in_window"] = int(win.max())

    for k, v in losses.items():
        v["hours"] = round(v["hours"], 2)
        v["parts"] = int(v["hours"] * 3600 / true_ct)
        v["owner"] = OWNERS[k]
        v["likely_cause"] = LIKELY_CAUSE[k]

    availability = run_s / planned if planned else 0
    performance = (total_parts * true_ct) / run_s if run_s else 0
    quality = good / (good + scrap) if (good + scrap) else 0
    metrics = {
        "planned_hours": round(planned / 3600, 2),
        "run_hours": round(run_s / 3600, 2),
        "missing_hours": round(missing["dur_s"].sum() / 3600, 2),
        "availability": round(availability, 3),
        "performance": round(min(performance, 1.0), 3),
        "quality": round(quality, 3),
        "oee": round(availability * min(performance, 1.0) * quality, 3),
        "aur": round(run_s / (24 * 3600), 3),  # asset utilisation: run time / calendar time
        "plc_parts": int(total_parts),
        "good_parts": good,
        "scrap_parts": scrap,
        "throughput_per_hour": round(good / (planned / 3600), 1) if planned else 0,
    }
    ranked = sorted(losses.items(), key=lambda kv: kv[1]["hours"], reverse=True)
    top3 = [dict(reason=k, **v) for k, v in ranked[:3] if v["hours"] > 0]
    return metrics, losses, top3


# 5. Capacity verdict -------------------------------------------------------
def capacity_verdict(data, machine, day, metrics, top3, ct):
    costs = data["costs"]
    dem = data["demand"]
    demand = int(dem[(dem["date"] == pd.Timestamp(day)) & (dem["machine"] == machine)]["parts_required"].sum())
    planned_s = metrics["planned_hours"] * 3600
    target = float(costs.get("recovery_target", 0.5))

    sap_capacity = int(planned_s / ct["sap_ct_s"])     # what the plant believes is the ceiling
    true_capacity = int(planned_s / ct["true_ct_s"])   # real ceiling at 100% OEE
    current = metrics["good_parts"]
    recoverable_hours = round(sum(t["hours"] for t in top3) * target, 2)
    recoverable_parts = int(recoverable_hours * 3600 / ct["true_ct_s"])
    gap = max(demand - current, 0)

    if current >= demand:
        verdict, reason = "No", "Current output already covers demand."
    elif current + recoverable_parts >= demand:
        verdict = "Not yet"
        reason = (f"Recovering {int(target * 100)}% of the top 3 losses ({recoverable_hours} h/day) "
                  f"adds {recoverable_parts} parts/day and closes the {gap}-part gap.")
    elif demand > 0.85 * true_capacity:
        verdict, reason = "Yes", "Demand exceeds 85% of true capacity even with losses recovered."
    else:
        verdict = "Not yet"
        reason = (f"The {gap}-part gap is bigger than {int(target * 100)}% of the top losses, but true capacity "
                  f"({true_capacity}/day) still covers demand. Target the full top-3 losses first.")

    margin = float(costs["contribution_margin_per_part"])
    days = float(costs["production_days_per_year"])
    return {
        "demand": demand,
        "current_good_output": current,
        "gap_parts": gap,
        "sap_capacity": sap_capacity,
        "true_capacity": true_capacity,
        "recoverable_hours": recoverable_hours,
        "recoverable_parts": recoverable_parts,
        "recoverable_value_per_day": round(recoverable_parts * margin),
        "recoverable_value_per_year": round(recoverable_parts * margin * days),
        "new_press_capex": float(costs["new_press_capex"]),
        "verdict": verdict,
        "reason": reason,
        "owner": "Plant manager",
    }


def analyse(data, day, machine="P3"):
    """Run the full daily rule chain for one machine and one day."""
    health = data_health(data, day, machine)
    result = {"date": day.isoformat(), "machine": machine, "health": health}
    if health["status"] == "FAIL":
        result["halted"] = "Data health check failed; OEE not reported until the data is fixed."
        return result
    ct = true_cycle_time(data, machine, day)
    metrics, losses, top3 = metrics_and_losses(data, machine, day, ct["true_ct_s"])
    for t in top3:
        t["value_per_day"] = round(t["parts"] * float(data["costs"]["contribution_margin_per_part"]))
    result.update({
        "cycle_time": ct,
        "metrics": metrics,
        "losses": losses,
        "top3": top3,
        "hours_lost": round(sum(v["hours"] for v in losses.values()), 2),
        "capacity": capacity_verdict(data, machine, day, metrics, top3, ct),
    })
    return result
