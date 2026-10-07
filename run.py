"""Daily runner. Cron calls this at 6 am; `python3 run.py --now` runs it on demand for a demo.

Usage:
  python3 run.py --now                 # analyse the latest day in the data
  python3 run.py --date 2026-10-02     # analyse a specific day
  python3 run.py --backfill            # analyse every day (fills the trend chart), no AI or Slack
  python3 run.py --now --no-ai         # skip the Claude step, use template alerts
"""
import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

import notify
import rules
import writer

OUT = Path(__file__).parent / "outputs"
LOG = OUT / "daily_log.csv"


def log_day(result):
    """Upsert one row per day. Hours recovered = baseline (first logged day) minus today's lost hours."""
    row = {"date": result["date"], "status": result["health"]["status"]}
    if "metrics" in result:
        m, cap = result["metrics"], result["capacity"]
        row.update(oee=m["oee"], availability=m["availability"], performance=m["performance"],
                   quality=m["quality"], good_parts=m["good_parts"], hours_lost=result["hours_lost"],
                   demand=cap["demand"], verdict=cap["verdict"])
    log = pd.read_csv(LOG) if LOG.exists() else pd.DataFrame()
    if not log.empty:
        log = log[log["date"] != row["date"]]
    log = pd.concat([log, pd.DataFrame([row])]).sort_values("date")
    if "hours_lost" in log:
        valid = log["hours_lost"].dropna()
        baseline = valid.iloc[0] if not valid.empty else 0
        log["hours_recovered"] = (baseline - log["hours_lost"]).round(2)
    log.to_csv(LOG, index=False)


def run_day(data, day, use_ai=True, use_slack=True):
    print(f"\n== {day} · Press 3 ==")
    result = rules.analyse(data, day)
    h = result["health"]
    print(f"1. Data health: {h['status']}" + "".join(f"\n   - [{i['level']}] {i['machine']}: {i['issue']}" for i in h["issues"]))
    if result.get("halted"):
        print("   " + result["halted"])
    else:
        ct, m, cap = result["cycle_time"], result["metrics"], result["capacity"]
        print(f"2. Cycle time: SAP {ct['sap_ct_s']} s vs PLC {ct['true_ct_s']} s ({ct['diff_pct']}%)"
              + ("  << FLAG" if ct["flag"] else ""))
        print(f"3. OEE {m['oee']:.1%} = A {m['availability']:.1%} x P {m['performance']:.1%} x Q {m['quality']:.1%}"
              f" · AUR {m['aur']:.1%} · {m['throughput_per_hour']} good parts/h")
        print(f"4. Lost {result['hours_lost']} h. Top 3:")
        for t in result["top3"]:
            print(f"   - {t['reason']}: {t['hours']} h, {t['events']} events -> {t['owner']}")
        print(f"5. Capacity: output {cap['current_good_output']} vs demand {cap['demand']} "
              f"(SAP thinks max {cap['sap_capacity']}, true max {cap['true_capacity']}). "
              f"New press: {cap['verdict']}")
    OUT.mkdir(exist_ok=True)
    (OUT / "results").mkdir(exist_ok=True)
    if use_ai or use_slack:
        alerts, source = writer.draft_alerts(result, use_ai=use_ai)
        result["alerts"], result["alerts_source"] = alerts, source
        text = notify.format_alerts(result, alerts, source)
        status = notify.send(text, OUT / f"alerts_{day}.md") if use_slack else "not sent"
        print(f"6. Alerts drafted by {source}, {status}:\n")
        print(text)
    else:
        alerts, source = writer.draft_alerts(result, use_ai=False)
        result["alerts"], result["alerts_source"] = alerts, source
    (OUT / "results" / f"{day}.json").write_text(json.dumps(result, indent=1, default=str))
    log_day(result)
    print(f"7. Logged to {LOG.name}")
    return result


def main():
    p = argparse.ArgumentParser(description="Hidden Capacity Agent")
    p.add_argument("--now", action="store_true", help="run immediately for the latest day")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--no-slack", action="store_true")
    a = p.parse_args()

    data = rules.load()
    cal = data["calendar"]
    open_days = sorted({d.date() for d in cal[cal["is_open"]]["date"]})
    if a.backfill:
        for d in open_days:
            run_day(data, d, use_ai=False, use_slack=False)
        return
    day = date.fromisoformat(a.date) if a.date else open_days[-1]
    run_day(data, day, use_ai=not a.no_ai, use_slack=not a.no_slack)


if __name__ == "__main__":
    main()
