"""Generate one week of fake plant data: 5 presses, 2 weld cells, 1 packing station.

The CSVs stand in for real systems. Every file uses the same machine IDs (PRS-03, WLD-01, ...):
  plc_state_log.csv        -> PLC / machine data collection
  sap_machine_master.csv   -> SAP machine master: name, type, standard cycle time, parts per cycle
  sap_bookings.csv         -> SAP production confirmations (good + scrap per shift)
  shift_calendar.csv       -> plant shift calendar
  costs.csv                -> finance inputs
  demand.csv               -> parts required per day, per machine and for the line

Planted problems (the agent has to find these on its own):
  1. PRS-03 standard cycle time in SAP is 42 s; the press really runs at ~36 s.
  2. PRS-03 short-stop cluster between 14:00 and 18:00 on B shift (feeder jams).
  3. PRS-03 sits idle after shift start-up and after every break.
  4. One shift (2026-10-02, B, PRS-03) has zero scrap booked in SAP.
  5. PRS-05 has a 40-minute hole in its PLC log (2026-10-01, A shift).
  6. WLD-02 loses a lot of time at job changes; put it in a line and it becomes the next bottleneck.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 7
DATA_DIR = Path(__file__).parent / "data"

START = date(2026, 9, 29)
DAYS = 7
LAST_DAY = START + timedelta(days=DAYS - 1)
HOLIDAYS = {date(2026, 10, 4)}  # Sunday, plant closed

# shift, start, end, break start, break end
SHIFTS = [("A", "06:00", "14:00", "10:00", "10:30"),
          ("B", "14:00", "22:00", "18:00", "18:30")]

# Typical machines: slow start-ups and restarts, two changeovers per shift, regular short stops.
# Tuned so every machine runs below 85% OEE (checked at the end of main()).
HEALTHY = dict(startup=(5, 10), post_break=(5, 10), job_changes=[150, 330], stops=8, breakdown=0.2, slow_windows=2)
MACHINES = {
    "PRS-01": dict(HEALTHY, name="Press 1 (double-hit die)", type="press", ct=55, sap_ct=55, ppc=2),
    "PRS-02": dict(HEALTHY, name="Press 2", type="press", ct=31, sap_ct=31, ppc=1),
    "PRS-03": dict(name="Press 3", type="press", ct=36, sap_ct=42, ppc=1, startup=(8, 15), post_break=(8, 15),
                   job_changes=[150, 330], stops=5, breakdown=0.25, slow_windows=2, cluster=True),
    "PRS-04": dict(HEALTHY, name="Press 4", type="press", ct=30, sap_ct=30.5, ppc=1),
    "PRS-05": dict(HEALTHY, name="Press 5", type="press", ct=32, sap_ct=32, ppc=1),
    "WLD-01": dict(name="Weld cell 1", type="weld", ct=34, sap_ct=34, ppc=1, startup=(4, 8), post_break=(4, 8),
                   job_changes=[150, 330], stops=6, breakdown=0.2, slow_windows=2),
    "WLD-02": dict(name="Weld cell 2", type="weld", ct=38, sap_ct=40, ppc=1, startup=(6, 10), post_break=(6, 10),
                   job_changes=[120, 300], stops=11, breakdown=0.35, slow_windows=2),
    "PCK-01": dict(HEALTHY, name="Packing 1", type="pack", ct=22, sap_ct=22, ppc=1),
}

ZERO_SCRAP = (date(2026, 10, 2), "B", "PRS-03")
MISSING = (date(2026, 10, 1), "A", "PRS-05", "11:00", 40)  # date, shift, machine, from, minutes


def at(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m)


def minutes(x):
    return timedelta(minutes=float(x))


def simulate_shift(rng, machine, d, shift, state):
    """Return PLC rows for one machine-shift. state carries the part counter and order number."""
    p = MACHINES[machine]
    name, s, e, bs, be = shift
    s, e, bs, be = at(d, s), at(d, e), at(d, bs), at(d, be)
    rows = []

    def emit(t, st):
        rows.append((machine, t, st, state["counter"], f"{machine}-{state['order']:05d}"))

    job_changes = [s + minutes(m) for m in p["job_changes"]]
    stops = [(s + minutes(t), rng.uniform(1.0, 4.0)) for t in rng.uniform(0, 480, rng.poisson(p["stops"]))]
    if p.get("cluster") and name == "B":  # planted feeder-jam cluster 14:00-18:00
        n = 38 if d == LAST_DAY else int(rng.integers(28, 42))
        stops += [(s + minutes(t), rng.uniform(0.8, 2.6)) for t in rng.uniform(0, 240, n)]
    if rng.random() < p["breakdown"]:
        stops.append((s + minutes(rng.uniform(30, 450)), rng.uniform(10, 20)))
    stops.sort()
    slow = []
    for _ in range(p["slow_windows"]):
        w = s + minutes(rng.uniform(0, 420))
        slow.append((w, w + minutes(25)))

    t = s
    emit(t, "idle")
    t += minutes(rng.uniform(*p["startup"]))
    emit(t, "running")
    break_done = False
    while t < e:
        if not break_done and t >= bs:
            emit(t, "idle")  # break (planned) + late restart (loss)
            t = be + minutes(rng.uniform(*p["post_break"]))
            break_done = True
            emit(t, "running")
            continue
        if job_changes and t >= job_changes[0]:
            job_changes.pop(0)
            state["order"] += 1
            emit(t, "idle")
            t += minutes(rng.uniform(15, 25))
            emit(t, "running")
            continue
        if stops and t >= stops[0][0]:
            _, dur = stops.pop(0)
            emit(t, "stopped")
            t += minutes(dur)
            emit(t, "running")
            continue
        factor = 1.25 if any(a <= t < b for a, b in slow) else 1.0
        t_next = t + timedelta(seconds=p["ct"] * factor * rng.normal(1.0, 0.01))
        if not break_done and t < bs <= t_next:
            t = bs
            continue
        if t_next >= e:
            break
        state["counter"] += p["ppc"]  # PLC counts parts; a double-hit die makes 2 per cycle
        t = t_next
        emit(t, "running")
    return rows


def main():
    rng = np.random.default_rng(SEED)
    DATA_DIR.mkdir(exist_ok=True)
    days = [START + timedelta(days=i) for i in range(DAYS)]

    pd.DataFrame([{"date": d, "shift": sh[0], "start": sh[1], "end": sh[2],
                   "break_start": sh[3], "break_end": sh[4], "is_open": d not in HOLIDAYS}
                  for d in days for sh in SHIFTS]).to_csv(DATA_DIR / "shift_calendar.csv", index=False)

    plc_rows, bookings = [], []
    for idx, machine in enumerate(MACHINES):
        rng = np.random.default_rng([SEED, idx])  # own stream per machine
        state = {"counter": int(rng.integers(100000, 900000)), "order": 1}
        for d in days:
            if d in HOLIDAYS:
                continue
            for sh in SHIFTS:
                before = state["counter"]
                rows = simulate_shift(rng, machine, d, sh, state)
                if (d, sh[0], machine) == MISSING[:3]:
                    gap_from = at(d, MISSING[3])
                    rows = [r for r in rows if not gap_from <= r[1] < gap_from + minutes(MISSING[4])]
                plc_rows += rows
                parts = state["counter"] - before
                scrap = int(round(parts * rng.uniform(0.015, 0.025)))
                total = parts + int(rng.integers(-2, 3))
                if (d, sh[0], machine) == ZERO_SCRAP:
                    scrap = 0
                bookings.append({"date": d, "shift": sh[0], "machine": machine,
                                 "good_qty": total - scrap, "scrap_qty": scrap})

    plc = pd.DataFrame(plc_rows, columns=["machine", "timestamp", "state", "part_count", "order_id"])
    plc["timestamp"] = plc["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    plc.to_csv(DATA_DIR / "plc_state_log.csv", index=False)
    pd.DataFrame(bookings).to_csv(DATA_DIR / "sap_bookings.csv", index=False)

    pd.DataFrame([{"machine": m, "description": p["name"], "type": p["type"],
                   "std_cycle_time_s": p["sap_ct"], "parts_per_cycle": p["ppc"]}
                  for m, p in MACHINES.items()]).to_csv(DATA_DIR / "sap_machine_master.csv", index=False)

    pd.DataFrame([
        {"item": "contribution_margin_per_part", "value": 4.20},
        {"item": "capex_press", "value": 1_000_000},
        {"item": "capex_weld", "value": 400_000},
        {"item": "capex_pack", "value": 150_000},
        {"item": "production_days_per_year", "value": 300},
        {"item": "recovery_target", "value": 0.5},
    ]).to_csv(DATA_DIR / "costs.csv", index=False)

    # Demand: one row per machine (standalone mode) plus LINE (line mode)
    pd.DataFrame([{"date": d, "target": t, "parts_required": 0 if d in HOLIDAYS else 1250}
                  for d in days for t in list(MACHINES) + ["LINE"]]).to_csv(DATA_DIR / "demand.csv", index=False)

    print(f"Wrote {len(plc):,} PLC rows for {len(MACHINES)} machines, {len(bookings)} SAP bookings to {DATA_DIR}/")
    check_oee_ceiling()


OEE_CEILING = 0.85  # requirement: every machine strictly below 85% OEE on every production day


def check_oee_ceiling():
    """Run the agent's own OEE maths over the new data and fail loudly if any machine-day reaches the ceiling."""
    import rules
    data = rules.load(DATA_DIR)
    worst = {}
    for d in rules.open_days(data):
        for m in data["master"].index:
            ct = rules.true_cycle_time(data, m, d)["true_ct_s"]
            oee = rules.metrics_and_losses(data, m, d, ct)[0]["oee"]
            worst[m] = max(worst.get(m, 0), oee)
    print("Highest daily OEE per machine: " + ", ".join(f"{m} {o:.1%}" for m, o in worst.items()))
    over = {m: o for m, o in worst.items() if o >= OEE_CEILING}
    if over:
        raise SystemExit(f"OEE check FAILED: {over} reach {OEE_CEILING:.0%}. Increase their losses in MACHINES.")
    print(f"OEE check passed: every machine is below {OEE_CEILING:.0%} on every day.")


if __name__ == "__main__":
    main()
