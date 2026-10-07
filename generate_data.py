"""Generate one week of fake plant data for a 5-press line.

The CSVs stand in for real systems:
  plc_state_log.csv        -> PLC / machine data collection
  sap_machine_master.csv   -> SAP standard cycle times
  sap_bookings.csv         -> SAP production confirmations (good + scrap per shift)
  shift_calendar.csv       -> plant shift calendar
  costs.csv                -> finance inputs
  demand.csv               -> parts required per day

Planted problems (the agent has to find these on its own):
  1. Press 3 standard cycle time in SAP is 42 s; the press really runs at ~36 s.
  2. Press 3 short-stop cluster between 14:00 and 18:00 on B shift (feeder jams).
  3. Press 3 sits idle after shift start-up and after every break.
  4. One shift (2026-10-02, B, Press 3) has zero scrap booked in SAP.
  5. Press 5 has a 40-minute hole in its PLC log (2026-10-01, A shift).
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

TRUE_CT = {"P1": 30.0, "P2": 31.0, "P3": 36.0, "P4": 30.0, "P5": 32.0}
SAP_CT = {"P1": 30.0, "P2": 31.0, "P3": 42.0, "P4": 30.5, "P5": 32.0}
BOTTLENECK = "P3"

ZERO_SCRAP = (date(2026, 10, 2), "B", "P3")
MISSING = (date(2026, 10, 1), "A", "P5", "11:00", 40)  # date, shift, machine, from, minutes


def at(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m)


def minutes(x):
    return timedelta(minutes=float(x))


def simulate_shift(rng, machine, d, shift, state):
    """Return PLC rows for one machine-shift. state carries the part counter and order number."""
    name, s, e, bs, be = shift
    s, e, bs, be = at(d, s), at(d, e), at(d, bs), at(d, be)
    bn = machine == BOTTLENECK
    ct = TRUE_CT[machine]
    rows = []

    def emit(t, st):
        rows.append((machine, t, st, state["counter"], f"{machine}-{state['order']:05d}"))

    # Scheduled events
    job_changes = [s + minutes(150), s + minutes(330)] if bn else [s + minutes(240)]
    stops = []  # (time, minutes)
    for t in rng.uniform(0, 480, rng.poisson(5 if bn else 2)):
        stops.append((s + minutes(t), rng.uniform(1.0, 4.0)))
    if bn and name == "B":  # planted feeder-jam cluster 14:00-18:00
        n = 38 if d == LAST_DAY else int(rng.integers(28, 42))
        for t in rng.uniform(0, 240, n):
            stops.append((s + minutes(t), rng.uniform(0.8, 2.6)))
    if rng.random() < (0.25 if bn else 0.1):  # occasional breakdown
        stops.append((s + minutes(rng.uniform(30, 450)), rng.uniform(10, 20)))
    stops.sort()
    slow_windows = []
    for _ in range(2 if bn else 1):
        w = s + minutes(rng.uniform(0, 420))
        slow_windows.append((w, w + minutes(25)))

    startup = rng.uniform(8, 15) if bn else rng.uniform(1, 3)
    post_break = rng.uniform(8, 15) if bn else rng.uniform(0.5, 2)

    t = s
    emit(t, "idle")
    t += minutes(startup)
    emit(t, "running")
    break_done = False
    while t < e:
        if not break_done and t >= bs:
            emit(t, "idle")  # break (planned) + late restart (loss)
            t = be + minutes(post_break)
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
        factor = 1.25 if any(a <= t < b for a, b in slow_windows) else 1.0
        cycle = ct * factor * rng.normal(1.0, 0.01)
        t_next = t + timedelta(seconds=cycle)
        if not break_done and t < bs <= t_next:
            t = bs
            continue
        if t_next >= e:
            break
        state["counter"] += 1
        t = t_next
        emit(t, "running")
    return rows


def main():
    rng = np.random.default_rng(SEED)
    DATA_DIR.mkdir(exist_ok=True)
    days = [START + timedelta(days=i) for i in range(DAYS)]

    # Shift calendar
    cal = [{"date": d, "shift": sh[0], "start": sh[1], "end": sh[2],
            "break_start": sh[3], "break_end": sh[4], "is_open": d not in HOLIDAYS}
           for d in days for sh in SHIFTS]
    pd.DataFrame(cal).to_csv(DATA_DIR / "shift_calendar.csv", index=False)

    # PLC log + SAP bookings
    plc_rows, bookings = [], []
    for machine in TRUE_CT:
        state = {"counter": int(rng.integers(100000, 900000)), "order": 1}
        for d in days:
            if d in HOLIDAYS:
                continue
            for sh in SHIFTS:
                before = state["counter"]
                rows = simulate_shift(rng, machine, d, sh, state)
                if (d, sh[0], machine) == MISSING[:3]:
                    gap_from = at(d, MISSING[3])
                    gap_to = gap_from + minutes(MISSING[4])
                    rows = [r for r in rows if not gap_from <= r[1] < gap_to]
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

    # SAP machine master
    pd.DataFrame([{"machine": m, "description": f"Press {m[1]}", "std_cycle_time_s": SAP_CT[m]}
                  for m in SAP_CT]).to_csv(DATA_DIR / "sap_machine_master.csv", index=False)

    # Costs
    pd.DataFrame([
        {"item": "contribution_margin_per_part", "value": 4.20},
        {"item": "new_press_capex", "value": 1_000_000},
        {"item": "production_days_per_year", "value": 300},
        {"item": "recovery_target", "value": 0.5},
    ]).to_csv(DATA_DIR / "costs.csv", index=False)

    # Demand (the line ships what the bottleneck makes)
    pd.DataFrame([{"date": d, "machine": BOTTLENECK, "parts_required": 0 if d in HOLIDAYS else 1250}
                  for d in days]).to_csv(DATA_DIR / "demand.csv", index=False)

    print(f"Wrote {len(plc):,} PLC rows, {len(bookings)} SAP bookings to {DATA_DIR}/")


if __name__ == "__main__":
    main()
