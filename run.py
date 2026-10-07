"""Daily runner. Cron calls this at 6 am; `python3 run.py --now` runs it on demand for a demo.

The setup (line or single machine) comes from config/setup.json, which the dashboard saves.
Command-line flags override it.

Usage:
  python3 run.py --now                                        # latest day, saved setup
  python3 run.py --now --mode line --line PRS-01,PRS-03,WLD-01,PCK-01
  python3 run.py --now --mode single --machine WLD-02
  python3 run.py --date 2026-10-02                            # a specific day
  python3 run.py --backfill                                   # every day, fills the trend log (no AI, no Slack)
  python3 run.py --now --no-ai                                # template alerts instead of Claude
"""
import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

import notify
import rules
import writer

ROOT = Path(__file__).parent
OUT = ROOT / "outputs"
LOG = OUT / "daily_log.csv"
SETUP = ROOT / "config" / "setup.json"
DEFAULT_SETUP = {"mode": "line", "line": ["PRS-01", "PRS-03", "WLD-01", "PCK-01"], "machine": "PRS-03"}


def load_setup():
    if SETUP.exists():
        return {**DEFAULT_SETUP, **json.loads(SETUP.read_text())}
    return dict(DEFAULT_SETUP)


def save_setup(setup):
    SETUP.parent.mkdir(exist_ok=True)
    SETUP.write_text(json.dumps(setup, indent=1))


def scope_key(result):
    return "LINE_" + "-".join(result["line"]) if result["mode"] == "line" else result["scope"]


def log_day(result):
    """Upsert one row per day and scope. Hours recovered = first logged day's lost hours minus today's."""
    row = {"date": result["date"], "scope": scope_key(result), "status": result["health"]["status"]}
    unit = rules.focus_unit(result)
    if unit:
        m, v = unit["metrics"], result["verdict"]
        row.update(focus_machine=unit["machine"], oee=m["oee"], good_parts=m["good_parts"],
                   hours_lost=unit["hours_lost"], output=v["output_now"], demand=v["demand"], verdict=v["verdict"])
    log = pd.read_csv(LOG) if LOG.exists() else pd.DataFrame()
    if not log.empty:
        log = log[~((log["date"] == row["date"]) & (log["scope"] == row["scope"]))]
    log = pd.concat([log, pd.DataFrame([row])]).sort_values(["scope", "date"])
    if "hours_lost" in log:
        first = log.groupby("scope")["hours_lost"].transform("first")
        log["hours_recovered"] = (first - log["hours_lost"]).round(2)
    log.to_csv(LOG, index=False)


def print_result(result):
    h = result["health"]
    print(f"1. Data health: {h['status']}" +
          "".join(f"\n   - [{i['level']}] {i['machine']}: {i['issue']}" for i in h["issues"]))
    if result.get("halted"):
        print("   " + result["halted"])
        return
    if result["mode"] == "line":
        print("   Line: " + " → ".join(
            f"{s['machine']} {s['capacity_now']}/day" + (" [BOTTLENECK]" if s["is_bottleneck"] else "")
            for s in result["steps"]))
        nb = result["next_bottleneck"]
        print(f"   Line capacity {result['line_capacity']}/day · next bottleneck {nb} "
              f"(+{result['headroom_parts']} parts of headroom)")
    u = rules.focus_unit(result)
    ct, m, v = u["cycle_time"], u["metrics"], result["verdict"]
    print(f"2. {u['name']} cycle time: SAP {ct['sap_ct_s']} s vs PLC {ct['true_ct_s']} s ({ct['diff_pct']}%)"
          + ("  << FLAG" if ct["flag"] else ""))
    print(f"3. OEE {m['oee']:.1%} = A {m['availability']:.1%} x P {m['performance']:.1%} x Q {m['quality']:.1%}"
          f" · AUR {m['aur']:.1%} · {m['throughput_per_hour']} good parts/h")
    print(f"4. Lost {u['hours_lost']} h. Top 3:")
    for t in u["top3"]:
        print(f"   - {t['reason']}: {t['hours']} h, {t['events']} events -> {t['owner']}")
    print(f"5. Output {v['output_now']} vs demand {v['demand']} "
          f"(after loss recovery: {v['output_after_recovery']}). "
          f"New capacity: {v['verdict']}")


def run_day(data, day, setup, use_ai=True, use_slack=True, quiet=False):
    result = rules.analyse(data, day, mode=setup["mode"], machine=setup["machine"], line=setup["line"])
    if not quiet:
        title = "Line " + " → ".join(setup["line"]) if setup["mode"] == "line" else setup["machine"]
        print(f"\n== {day} · {title} ==")
        print_result(result)
    OUT.mkdir(exist_ok=True)
    (OUT / "results").mkdir(exist_ok=True)
    alerts, source = writer.draft_alerts(result, use_ai=use_ai)
    result["alerts"], result["alerts_source"] = alerts, source
    if use_slack:
        text = notify.format_alerts(result, alerts, source)
        status = notify.send(text, OUT / f"alerts_{day}_{scope_key(result)}.md")
        print(f"6. Alerts drafted by {source}, {status}:\n\n{text}")
    (OUT / "results" / f"{day}__{scope_key(result)}.json").write_text(json.dumps(result, indent=1, default=str))
    log_day(result)
    if not quiet:
        print(f"7. Logged to {LOG.name}")
    return result


def main():
    p = argparse.ArgumentParser(description="Hidden Capacity Agent")
    p.add_argument("--now", action="store_true", help="run immediately for the latest day")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--mode", choices=["line", "single"])
    p.add_argument("--line", help="comma-separated machine IDs in flow order")
    p.add_argument("--machine", help="machine ID for single mode")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--no-slack", action="store_true")
    a = p.parse_args()

    setup = load_setup()
    if a.mode:
        setup["mode"] = a.mode
    if a.line:
        setup["line"] = a.line.split(",")
    if a.machine:
        setup["machine"] = a.machine

    data = rules.load()
    known = set(rules.machines(data))
    unknown = [m for m in setup["line"] + [setup["machine"]] if m not in known]
    if unknown:
        p.error(f"unknown machine(s) {unknown}; known: {sorted(known)}")

    if a.backfill:
        for d in rules.open_days(data):
            run_day(data, d, setup, use_ai=False, use_slack=False, quiet=True)
        print(f"Backfilled {len(rules.open_days(data))} days into {LOG}")
        return
    day = date.fromisoformat(a.date) if a.date else rules.open_days(data)[-1]
    run_day(data, day, setup, use_ai=not a.no_ai, use_slack=not a.no_slack)


if __name__ == "__main__":
    main()
