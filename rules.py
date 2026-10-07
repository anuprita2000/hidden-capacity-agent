"""Deterministic rules: data health, true cycle time, OEE, loss finder, bottleneck and capacity verdict.

Two modes:
  analyse_machine(data, day, machine)   -> one machine running on its own
  analyse_line(data, day, [m1, m2, ...]) -> machines in one continuous flow; slowest one is the bottleneck

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
YES_THRESHOLD = 0.85            # buy only if demand > 85% of a machine's true capacity

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
    "Idle at job changes": "changeover not prepared before the run ends",
    "Other idle": "waiting on material or operators",
    "Slow running": "running below rated speed",
    "Scrap": "process or material quality issue",
}


def load(data_dir=DATA_DIR):
    d = {
        "plc": pd.read_csv(data_dir / "plc_state_log.csv", parse_dates=["timestamp"]),
        "master": pd.read_csv(data_dir / "sap_machine_master.csv").set_index("machine"),
        "bookings": pd.read_csv(data_dir / "sap_bookings.csv", parse_dates=["date"]),
        "calendar": pd.read_csv(data_dir / "shift_calendar.csv", parse_dates=["date"]),
        "demand": pd.read_csv(data_dir / "demand.csv", parse_dates=["date"]),
    }
    d["costs"] = pd.read_csv(data_dir / "costs.csv").set_index("item")["value"].to_dict()
    d["plc_by_machine"] = {m: g.sort_values("timestamp", kind="stable")  # keep PLC order on same-second events
                            for m, g in d["plc"].groupby("machine")}
    d["_cache"] = {}
    return d


def machines(data):
    """Machine list for menus: id -> 'PRS-03 · Press 3'."""
    return {m: f"{m} · {r['description']}" for m, r in data["master"].iterrows()}


def open_days(data):
    cal = data["calendar"]
    return sorted({d.date() for d in cal[cal["is_open"]]["date"]})


def _ts(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def _meta(data, machine):
    r = data["master"].loc[machine]
    return {"name": r["description"], "type": r["type"],
            "sap_ct_s": float(r["std_cycle_time_s"]), "ppc": int(r["parts_per_cycle"])}


def open_shifts(data, day):
    cal = data["calendar"]
    return cal[(cal["date"] == pd.Timestamp(day)) & cal["is_open"]]


def _planned_seconds(data, day):
    total = 0
    for _, sh in open_shifts(data, day).iterrows():
        total += (_ts(day, sh["end"]) - _ts(day, sh["start"])).total_seconds()
        total -= (_ts(day, sh["break_end"]) - _ts(day, sh["break_start"])).total_seconds()
    return total


def intervals(data, machine, day):
    """Turn PLC events into timed intervals, clipped to shifts, with breaks split out. Cached."""
    key = (machine, day)
    if key in data["_cache"]:
        return data["_cache"][key]
    plc = data["plc_by_machine"].get(machine, pd.DataFrame(columns=data["plc"].columns))
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
        for r in rows.itertuples():
            start, end = r.timestamp.to_pydatetime(), r.end.to_pydatetime()
            planned = max(timedelta(0), min(end, be) - max(start, bs))  # break time is planned, not lost
            out.append({
                "shift": sh["shift"], "start": start, "end": end, "state": r.state,
                "dur_s": (end - start - planned).total_seconds(), "planned_s": planned.total_seconds(),
                "parts": r.parts, "starts_shift": start == s,
                "touches_break": planned.total_seconds() > 0 or start == be,
                "job_change": r.state == "idle" and isinstance(r.prev_order, str) and r.prev_order != r.order_id,
            })
    df = pd.DataFrame(out)
    data["_cache"][key] = df
    return df


# 1. Data health ------------------------------------------------------------
def data_health(data, day, focus):
    """Check every machine. Problems on a focus machine can FAIL the run; elsewhere they only WARN."""
    focus = set(focus)
    issues, status = [], "PASS"
    order = ["PASS", "WARN", "FAIL"]
    day_ts = pd.Timestamp(day)
    b = data["bookings"]
    planned = _planned_seconds(data, day)

    def add(level, machine, text):
        nonlocal status
        status = max(status, level, key=order.index)
        issues.append({"level": level, "machine": machine, "issue": text})

    for m in data["master"].index:
        iv = intervals(data, m, day)
        if iv.empty:
            continue
        ct = _meta(data, m)["sap_ct_s"]
        gaps = iv[(iv["state"] == "running") & (iv["dur_s"] > 10 * ct)]
        if not gaps.empty:
            pct = 100 * gaps["dur_s"].sum() / planned
            add("FAIL" if (m in focus and pct > MISSING_FAIL_PCT) else "WARN", m,
                f"PLC log gap of {gaps['dur_s'].sum() / 60:.0f} min ({pct:.1f}% of planned time)")
        for sh, g in iv.groupby("shift"):
            plc_parts = g["parts"].sum()
            bk = b[(b["date"] == day_ts) & (b["shift"] == sh) & (b["machine"] == m)]
            if bk.empty:
                add("FAIL" if m in focus else "WARN", m, f"Shift {sh}: no SAP booking")
                continue
            sap_parts = int(bk["good_qty"].iloc[0] + bk["scrap_qty"].iloc[0])
            diff = 100 * abs(sap_parts - plc_parts) / max(plc_parts, 1)
            if diff > COUNT_MISMATCH_WARN_PCT:
                add("FAIL" if (m in focus and diff > COUNT_MISMATCH_FAIL_PCT) else "WARN", m,
                    f"Shift {sh}: PLC {plc_parts:.0f} parts vs SAP {sap_parts} ({diff:.1f}% gap)")

    # zero scrap booked in the last 7 days is not believable on a press or weld line
    recent = b[(b["date"] <= day_ts) & (b["date"] > day_ts - pd.Timedelta(days=7)) & (b["scrap_qty"] == 0)]
    for r in recent.itertuples():
        add("WARN", r.machine, f"{r.date:%Y-%m-%d} shift {r.shift}: zero scrap booked ({r.good_qty} good). "
                               f"Scrap was likely booked as good; quality is overstated for that shift.")
    return {"status": status, "issues": issues}


# 2. True cycle time -------------------------------------------------------
def true_cycle_time(data, machine, day, lookback_days=7):
    meta = _meta(data, machine)
    cycles = []
    for i in range(lookback_days):
        iv = intervals(data, machine, day - timedelta(days=i))
        if iv.empty:
            continue
        full = iv[(iv["state"] == "running") & (iv["parts"] == meta["ppc"]) & (iv["dur_s"] < 2 * meta["sap_ct_s"])]
        cycles += full["dur_s"].tolist()
    true_ct = float(pd.Series(cycles).median())
    diff_pct = 100 * (meta["sap_ct_s"] - true_ct) / true_ct
    return {"sap_ct_s": meta["sap_ct_s"], "true_ct_s": round(true_ct, 1), "diff_pct": round(diff_pct, 1),
            "flag": abs(diff_pct) > CT_FLAG_PCT, "cycles_sampled": len(cycles), "owner": "Industrial engineer"}


# 3 + 4. Metrics and loss finder -------------------------------------------
def metrics_and_losses(data, machine, day, true_ct):
    ppc = _meta(data, machine)["ppc"]
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
    full = running[running["parts"] == ppc]
    add("Slow running", (full["dur_s"] - true_ct).clip(lower=0).sum(), int((full["dur_s"] > 1.1 * true_ct).sum()))
    add("Scrap", scrap / ppc * true_ct, scrap)

    # When do short stops cluster? Find the worst 4-hour window.
    ss = iv[(iv["state"] == "stopped") & (iv["dur_s"] < SHORT_STOP_MAX_MIN * 60)] if not iv.empty else iv
    if not ss.empty:
        by_hour = ss["start"].apply(lambda t: t.hour).value_counts().reindex(range(24), fill_value=0)
        win = by_hour.rolling(4).sum()
        end_h = int(win.idxmax())
        losses["Short stops"]["window"] = f"{end_h - 3:02d}:00-{end_h + 1:02d}:00"
        losses["Short stops"]["events_in_window"] = int(win.max())

    for k, v in losses.items():
        v["hours"] = round(v["hours"], 2)
        v["parts"] = int(v["hours"] * 3600 / true_ct * ppc)
        v["owner"] = OWNERS[k]
        v["likely_cause"] = LIKELY_CAUSE[k]

    availability = run_s / planned if planned else 0
    performance = min((total_parts / ppc * true_ct) / run_s, 1.0) if run_s else 0
    quality = good / (good + scrap) if (good + scrap) else 0
    metrics = {
        "planned_hours": round(planned / 3600, 2),
        "run_hours": round(run_s / 3600, 2),
        "missing_hours": round(missing["dur_s"].sum() / 3600, 2),
        "availability": round(availability, 3),
        "performance": round(performance, 3),
        "quality": round(quality, 3),
        "oee": round(availability * performance * quality, 3),
        "aur": round(run_s / (24 * 3600), 3),  # asset utilisation: run time / calendar time
        "plc_parts": int(total_parts),
        "good_parts": good,
        "scrap_parts": scrap,
        "throughput_per_hour": round(good / (planned / 3600), 1) if planned else 0,
    }
    ranked = sorted(losses.items(), key=lambda kv: kv[1]["hours"], reverse=True)
    top3 = [dict(reason=k, **v) for k, v in ranked[:3] if v["hours"] > 0]
    return metrics, losses, top3


# 5. Capacity and verdict ----------------------------------------------------
def _demand(data, day, target):
    dem = data["demand"]
    return int(dem[(dem["date"] == pd.Timestamp(day)) & (dem["target"] == target)]["parts_required"].sum())


def machine_capacity(data, machine, metrics, top3, ct):
    """Parts/day: what the plant believes, the true ceiling, what it made, and what 50% loss recovery adds."""
    ppc = _meta(data, machine)["ppc"]
    planned_s = metrics["planned_hours"] * 3600
    target = float(data["costs"].get("recovery_target", 0.5))
    recoverable_hours = round(sum(t["hours"] for t in top3) * target, 2)
    return {
        "sap_capacity": int(planned_s / ct["sap_ct_s"] * ppc),
        "true_capacity": int(planned_s / ct["true_ct_s"] * ppc),
        "current_good_output": metrics["good_parts"],
        "recoverable_hours": recoverable_hours,
        "recoverable_parts": int(recoverable_hours * 3600 / ct["true_ct_s"] * ppc),
    }


def _node_from_unit(u):
    cap = u["capacity"]
    return {"id": u["machine"], "name": u["name"], "machine": u["machine"], "type": u["type"],
            "now": cap["current_good_output"], "after": cap["current_good_output"] + cap["recoverable_parts"],
            "true": cap["true_capacity"]}


def verdict(data, nodes, demand):
    """Shared Yes / No / Not yet logic.

    nodes = capacity points in flow order: one per station in line mode, one machine in standalone mode.
    Each node: id, name, machine (the machine to act on), type, now, after (with loss recovery), true (ceiling).
    """
    costs = data["costs"]
    target = float(costs.get("recovery_target", 0.5))
    pct = int(target * 100)
    by_id = {n["id"]: n for n in nodes}
    out_now = min(n["now"] for n in nodes)
    out_after = min(n["after"] for n in nodes)
    short_now = [n for n in nodes if n["now"] < demand]
    bottleneck = min(nodes, key=lambda n: n["now"])

    if out_now >= demand:
        v, reason, buy_at = "No", f"Current output ({out_now}/day) already covers demand ({demand}).", None
    elif out_after >= demand:
        v, buy_at = "Not yet", None
        reason = (f"Recovering {pct}% of the top 3 losses on {', '.join(n['name'] for n in short_now)} lifts output "
                  f"from {out_now} to {out_after}/day, covering demand of {demand}.")
    else:
        short_after = [n for n in nodes if n["after"] < demand]
        need = [n for n in short_after if demand > YES_THRESHOLD * n["true"]]
        if need:
            v, buy_at = "Yes", need[0]["machine"]
            reason = (f"Add capacity at {need[0]['name']}: demand ({demand}) is above {int(YES_THRESHOLD * 100)}% "
                      f"of its true capacity ({need[0]['true']}/day), so loss recovery alone cannot close the gap.")
        else:
            v, buy_at = "Not yet", None
            reason = (f"{pct}% loss recovery leaves {', '.join(n['name'] for n in short_after)} short "
                      f"({out_after}/day vs {demand}), but true capacity still covers demand. "
                      f"Go after the full top-3 losses there before buying.")

    margin, days = float(costs["contribution_margin_per_part"]), float(costs["production_days_per_year"])
    gained = max(min(out_after, demand) - out_now, 0)
    return {
        "demand": demand, "output_now": out_now, "output_after_recovery": out_after,
        "gap_parts": max(demand - out_now, 0), "bottleneck": bottleneck["machine"],
        "recoverable_value_per_year": round(gained * margin * days),
        "capex_bottleneck_type": float(costs.get(f"capex_{bottleneck['type']}", 0)),
        "verdict": v, "reason": reason, "buy_at": buy_at, "owner": "Plant manager",
    }


# Entry points -----------------------------------------------------------------
def _machine_core(data, day, machine):
    meta = _meta(data, machine)
    ct = true_cycle_time(data, machine, day)
    metrics, losses, top3 = metrics_and_losses(data, machine, day, ct["true_ct_s"])
    margin = float(data["costs"]["contribution_margin_per_part"])
    for t in top3:
        t["value_per_day"] = round(t["parts"] * margin)
    return {
        "machine": machine, "name": meta["name"], "type": meta["type"], "ppc": meta["ppc"],
        "cycle_time": ct, "metrics": metrics, "losses": losses, "top3": top3,
        "hours_lost": round(sum(v["hours"] for v in losses.values()), 2),
        "capacity": machine_capacity(data, machine, metrics, top3, ct),
    }


def analyse_machine(data, day, machine, demand=None):
    """Standalone mode: one machine running on its own against its own demand."""
    health = data_health(data, day, [machine])
    result = {"mode": "single", "scope": machine, "date": day.isoformat(), "health": health}
    if health["status"] == "FAIL":
        result["halted"] = f"Data health check failed for {machine}; OEE not reported until the data is fixed."
        return result
    unit = _machine_core(data, day, machine)
    result.update(unit)
    result["verdict"] = verdict(data, [_node_from_unit(unit)],
                                _demand(data, day, machine) if demand is None else demand)
    return result


def normalize_stations(line):
    """Accept ["PRS-01", ...] (one machine per station) or [{"machines": [...], "combine": ..., "qty": {...}}, ...].

    combine (only for stations with 2+ machines):
      "assemble" - each machine makes a different part; one finished unit needs qty[m] of each (e.g. outer + inner panel)
      "add"      - the machines make the same part side by side; their outputs add up
    """
    out = []
    for st_ in line:
        if isinstance(st_, str):
            st_ = {"machines": [st_]}
        ms = list(st_["machines"])
        combine = st_.get("combine", "assemble") if len(ms) > 1 else "single"
        qty = {m: max(1, int((st_.get("qty") or {}).get(m, 1))) for m in ms}
        out.append({"machines": ms, "combine": combine, "qty": qty})
    return out


def flat_machines(line):
    return [m for st_ in normalize_stations(line) for m in st_["machines"]]


def _station_node(i, station, units):
    """Capacity of one station in finished units/day, and the machine that limits it."""
    ms, combine, qty = station["machines"], station["combine"], station["qty"]
    cap = {m: units[m]["capacity"] for m in ms}
    measures = {
        "now": {m: cap[m]["current_good_output"] for m in ms},
        "after": {m: cap[m]["current_good_output"] + cap[m]["recoverable_parts"] for m in ms},
        "true": {m: cap[m]["true_capacity"] for m in ms},
    }
    node = {"id": f"S{i + 1}", "combine": combine}
    if combine == "add":
        for k, vals in measures.items():
            node[k] = int(sum(vals.values()))
        limiting = max(ms, key=lambda m: units[m]["hours_lost"])  # most hours to win back
        node["name"] = " + ".join(units[m]["name"] for m in ms)
    else:
        for k, vals in measures.items():
            node[k] = int(min(vals[m] / qty[m] for m in ms))
        limiting = min(ms, key=lambda m: measures["now"][m] / qty[m])
        node["name"] = units[limiting]["name"]
    node["machine"], node["type"] = limiting, units[limiting]["type"]
    node["station_name"] = " + ".join(units[m]["name"] for m in ms)
    return node


def analyse_line(data, day, line, demand=None):
    """Line mode: stations in one continuous flow. The station with the lowest output is the bottleneck.

    A station is one machine, or several machines that either feed one assembly (different parts) or run
    side by side (same part). Capacity is counted in finished units per day.
    """
    stations = normalize_stations(line)
    machines_ = flat_machines(stations)
    health = data_health(data, day, machines_)
    result = {"mode": "line", "scope": "LINE", "date": day.isoformat(), "line": machines_,
              "stations": stations, "health": health}
    if health["status"] == "FAIL":
        bad = sorted({i["machine"] for i in health["issues"] if i["level"] == "FAIL"})
        result["halted"] = f"Data health check failed for {', '.join(bad)}; line capacity not reported."
        return result
    units = {m: _machine_core(data, day, m) for m in machines_}
    nodes = [_station_node(i, st_, units) for i, st_ in enumerate(stations)]
    v = verdict(data, nodes, _demand(data, day, "LINE") if demand is None else demand)
    ranked = sorted(nodes, key=lambda n: n["now"])
    bn, nxt = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    steps = []
    for i, (st_, n) in enumerate(zip(stations, nodes)):
        lim = units[n["machine"]]
        steps.append({
            "step": i + 1, "machine": n["machine"], "name": n["station_name"], "type": n["type"],
            "combine": st_["combine"], "limiting_name": n["name"],
            "oee": lim["metrics"]["oee"], "true_ct_s": lim["cycle_time"]["true_ct_s"],
            "capacity_now": n["now"], "capacity_after_recovery": n["after"], "true_capacity": n["true"],
            "is_bottleneck": n["id"] == bn["id"],
            "is_next_bottleneck": nxt is not None and n["id"] == nxt["id"],
            "machines": [{
                "machine": m, "name": units[m]["name"], "qty": st_["qty"][m],
                "capacity_now": units[m]["capacity"]["current_good_output"],
                "oee": units[m]["metrics"]["oee"], "is_limiting": m == n["machine"] and st_["combine"] != "single",
            } for m in st_["machines"]],
        })
    result.update({
        "steps": steps,
        "units": units,
        "bottleneck": bn["machine"],
        "bottleneck_station": bn["station_name"],
        "next_bottleneck": nxt["machine"] if nxt else None,
        "next_bottleneck_name": nxt["name"] if nxt else None,
        "line_capacity": bn["now"],
        "headroom_parts": (nxt["now"] - bn["now"]) if nxt else None,
        "verdict": v,
    })
    return result


def focus_unit(result):
    """The machine the alerts are about: the machine itself, or the line's bottleneck."""
    if result.get("halted"):
        return None
    return result["units"][result["bottleneck"]] if result["mode"] == "line" else result


def analyse(data, day, mode="single", machine="PRS-03", line=None, demand=None):
    if mode == "line":
        return analyse_line(data, day, line, demand)
    return analyse_machine(data, day, machine, demand)
