"""Daily runner. Cron calls this at 6 am; `python3 run.py --now` runs it on demand for a demo.

Lines are defined in config/lines.json (edit them on the dashboard's Line setup page).
By default every line is analysed and each gets its own alerts. Flags override that.

Usage:
  python3 run.py --now                                        # every configured line, latest day
  python3 run.py --now --mode line --line PRS-01,PRS-03,WLD-01,PCK-01
  python3 run.py --now --mode single --machine WLD-02
  python3 run.py --date 2026-10-02                            # a specific day
  python3 run.py --backfill                                   # every day, fills the trend log (no AI, no Slack)
  python3 run.py --now --no-ai                                # template alerts instead of Claude
"""
import argparse
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd

import notify
import rules
import writer

ROOT = Path(__file__).parent
OUT = ROOT / "outputs"
LOG = OUT / "daily_log.csv"
LINES = ROOT / "config" / "lines.json"
# A station is one machine, or several machines that feed one assembly ("assemble": different parts,
# qty of each per finished unit) or run side by side ("add": same part, outputs add).
DEFAULT_LINES = [
    {"name": "Line A · Door panels", "demand": 1250, "stations": [
        {"machines": ["PRS-01", "PRS-03"], "combine": "assemble", "qty": {"PRS-01": 1, "PRS-03": 1}},
        {"machines": ["WLD-01"]},
        {"machines": ["PCK-01"]},
    ]},
    {"name": "Line B · Brackets", "demand": 1250, "stations": [
        {"machines": ["PRS-02"]}, {"machines": ["PRS-04"]}, {"machines": ["WLD-02"]},
    ]},
]


def load_lines():
    """Lines with normalised stations. Older files with a flat "machines" list become one station per machine."""
    raw = json.loads(LINES.read_text())["lines"] if LINES.exists() else json.loads(json.dumps(DEFAULT_LINES))
    for line in raw:
        line["stations"] = rules.normalize_stations(line.get("stations") or line.get("machines", []))
        line.pop("machines", None)
    return raw


def save_lines(lines):
    LINES.parent.mkdir(exist_ok=True)
    LINES.write_text(json.dumps({"lines": lines}, indent=1))


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def scope_key(result, name=None):
    if result["mode"] == "line":
        return slug(name) if name else "LINE_" + "-".join(result["line"])
    return result["scope"]


def log_day(result, key):
    """Upsert one row per day and scope."""
    row = {"date": result["date"], "scope": key, "status": result["health"]["status"]}
    unit = rules.focus_unit(result)
    if unit:
        m, v = unit["metrics"], result["verdict"]
        row.update(focus_machine=unit["machine"], oee=m["oee"], good_parts=m["good_parts"],
                   hours_lost=unit["hours_lost"], output=v["output_now"], demand=v["demand"], verdict=v["verdict"])
    log = pd.read_csv(LOG) if LOG.exists() else pd.DataFrame()
    if not log.empty:
        log = log[~((log["date"] == row["date"]) & (log["scope"] == row["scope"]))]
    pd.concat([log, pd.DataFrame([row])]).sort_values(["scope", "date"]).to_csv(LOG, index=False)


def print_result(result):
    h = result["health"]
    print(f"1. Data health: {h['status']}" +
          "".join(f"\n   - [{i['level']}] {i['machine']}: {i['issue']}" for i in h["issues"]))
    if result.get("halted"):
        print("   " + result["halted"])
        return
    if result["mode"] == "line":
        print("   Line: " + " → ".join(
            ("(" + " + ".join(x["machine"] for x in s["machines"]) + f" {s['combine']})" if len(s["machines"]) > 1
             else s["machine"]) + f" {s['capacity_now']}/day" + (" [BOTTLENECK]" if s["is_bottleneck"] else "")
            for s in result["steps"]))
        print(f"   Line capacity {result['line_capacity']}/day · bottleneck {result['bottleneck']} · "
              f"next bottleneck {result['next_bottleneck_name']} "
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
          f"(after loss recovery: {v['output_after_recovery']}). New capacity: {v['verdict']}")


def run_day(data, day, setup, use_ai=True, use_slack=True, quiet=False):
    """setup: {"mode": "line"|"single", "stations": [...], "machine": id, "demand": int|None, "name": str|None}"""
    result = rules.analyse(data, day, mode=setup["mode"], machine=setup.get("machine"),
                           line=setup.get("stations"), demand=setup.get("demand"))
    key = scope_key(result, setup.get("name"))
    if not quiet:
        title = setup.get("name") or ("Line " + " → ".join(result.get("line", [])) if setup["mode"] == "line"
                                      else setup["machine"])
        print(f"\n== {day} · {title} ==")
        print_result(result)
    (OUT / "results").mkdir(parents=True, exist_ok=True)
    alerts, source = writer.draft_alerts(result, use_ai=use_ai)
    result["alerts"], result["alerts_source"] = alerts, source
    if use_slack:
        text = notify.format_alerts(result, alerts, source)
        if setup.get("name"):
            text = text.replace("Hidden Capacity Agent: ", f"Hidden Capacity Agent: {setup['name']} · ", 1)
        status = notify.send(text, OUT / f"alerts_{day}_{key}.md")
        print(f"6. Alerts drafted by {source}, {status}:\n\n{text}")
    (OUT / "results" / f"{day}__{key}.json").write_text(json.dumps(result, indent=1, default=str))
    log_day(result, key)
    return result


def main():
    p = argparse.ArgumentParser(description="Hidden Capacity Agent")
    p.add_argument("--now", action="store_true", help="run immediately for the latest day")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--mode", choices=["line", "single"])
    p.add_argument("--line", help="stations in flow order, comma-separated; join machines that feed one assembly "
                                  "with +, e.g. PRS-01+PRS-03,WLD-01,PCK-01")
    p.add_argument("--machine", help="machine ID for single mode")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--no-slack", action="store_true")
    a = p.parse_args()

    if a.mode == "single" or (a.machine and not a.mode):
        setups = [{"mode": "single", "machine": a.machine or "PRS-03"}]
    elif a.mode == "line" or a.line:
        if not a.line:
            p.error("--mode line needs --line PRS-01,PRS-03,...")
        setups = [{"mode": "line", "stations": [{"machines": st.split("+"), "combine": "assemble"}
                                                for st in a.line.split(",")]}]
    else:
        setups = [{"mode": "line", **line} for line in load_lines()]

    data = rules.load()
    known = set(rules.machines(data))
    used = [m for s in setups for m in rules.flat_machines(s.get("stations", []))] + \
        [s["machine"] for s in setups if "machine" in s]
    unknown = sorted(set(used) - known)
    if unknown:
        p.error(f"unknown machine(s) {unknown}; known: {sorted(known)}")

    days = rules.open_days(data)
    if a.backfill:
        for d in days:
            for s in setups:
                run_day(data, d, s, use_ai=False, use_slack=False, quiet=True)
        print(f"Backfilled {len(days)} days x {len(setups)} setup(s) into {LOG}")
        return
    day = date.fromisoformat(a.date) if a.date else days[-1]
    for s in setups:
        run_day(data, day, s, use_ai=not a.no_ai, use_slack=not a.no_slack)


if __name__ == "__main__":
    main()
