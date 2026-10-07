"""Dashboard. Run: python3 -m streamlit run dashboard.py

Pick a mode in the sidebar:
  Line            -> choose 2-6 machines in flow order; the slowest one is the bottleneck
  Single machine  -> any machine on its own
Results are computed live from the data, so changing the setup updates the page immediately.
"""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

import rules
import run
import writer

st.set_page_config(page_title="Hidden Capacity Agent", layout="wide")


@st.cache_resource
def get_data():
    return rules.load()


data = get_data()
NAMES = rules.machines(data)
DAYS = rules.open_days(data)


@st.cache_data(show_spinner="Analysing PLC and SAP data...")
def analyse(day_iso, mode, machine, line):
    return rules.analyse(data, date.fromisoformat(day_iso), mode=mode, machine=machine, line=list(line))


# Sidebar: setup -------------------------------------------------------------
setup = run.load_setup()
st.sidebar.header("Setup")
mode_label = st.sidebar.radio("How is it running?", ["Line (continuous flow)", "Single machine (on its own)"],
                              index=0 if setup["mode"] == "line" else 1)
mode = "line" if mode_label.startswith("Line") else "single"
if mode == "line":
    line = st.sidebar.multiselect("Machines, in flow order (pick them in the order parts move)",
                                  list(NAMES), default=[m for m in setup["line"] if m in NAMES],
                                  format_func=NAMES.get, max_selections=6)
    machine = setup["machine"]
    if len(line) < 2:
        st.sidebar.warning("Pick at least 2 machines for a line.")
        st.stop()
else:
    line = setup["line"]
    machine = st.sidebar.selectbox("Machine", list(NAMES), index=list(NAMES).index(setup["machine"]),
                                   format_func=NAMES.get)
day = st.sidebar.selectbox("Day", [d.isoformat() for d in DAYS][::-1])
if st.sidebar.button("Save as the daily 6 am setup"):
    run.save_setup({"mode": mode, "line": line, "machine": machine})
    st.sidebar.success("Saved. The daily run will use this setup.")

r = analyse(day, mode, machine, tuple(line))

# Header ---------------------------------------------------------------------------
st.title("Hidden Capacity Agent")
st.caption("Before you buy capacity, find the capacity you already have, one bottleneck at a time.")
if mode == "line":
    st.markdown("**Line:** " + " → ".join(NAMES[m] for m in line))
else:
    st.markdown(f"**Machine:** {NAMES[machine]} (running on its own)")

h = r["health"]
color = {"PASS": "green", "WARN": "orange", "FAIL": "red"}[h["status"]]
with st.expander(f"Data health: {h['status']} ({len(h['issues'])} issues across all machines)",
                 expanded=h["status"] == "FAIL"):
    st.markdown(f"Status: :{color}[{h['status']}]")
    for i in h["issues"]:
        st.caption(f"[{i['level']}] {i['machine']}: {i['issue']}")
if r.get("halted"):
    st.error(r["halted"])
    st.stop()

v = r["verdict"]
st.subheader(f"New capacity needed? {v['verdict']}")
st.write(v["reason"])


def render_unit(u, show_capacity_chart):
    m, ct, cap = u["metrics"], u["cycle_time"], u["capacity"]
    c = st.columns(6)
    c[0].metric("OEE", f"{m['oee']:.1%}")
    c[1].metric("Availability", f"{m['availability']:.1%}")
    c[2].metric("Performance", f"{m['performance']:.1%}")
    c[3].metric("Quality", f"{m['quality']:.1%}")
    c[4].metric("Good parts / h", m["throughput_per_hour"])
    c[5].metric("Hours lost", u["hours_lost"])
    if ct["flag"]:
        st.warning(f"Cycle time: SAP says {ct['sap_ct_s']} s, PLC shows {ct['true_ct_s']} s "
                   f"({ct['diff_pct']}% gap). SAP capacity {cap['sap_capacity']}/day vs true "
                   f"{cap['true_capacity']}/day. Owner: Industrial engineer.")
    else:
        st.caption(f"Cycle time OK: SAP {ct['sap_ct_s']} s vs PLC {ct['true_ct_s']} s ({ct['diff_pct']}%).")
    left, right = st.columns(2)
    with left:
        st.markdown("**Hours lost by reason**")
        losses = pd.DataFrame([{"reason": k, "hours": x["hours"]} for k, x in u["losses"].items() if x["hours"] > 0])
        if not losses.empty:
            st.bar_chart(losses.set_index("reason").sort_values("hours", ascending=False), horizontal=True)
    with right:
        if show_capacity_chart:
            st.markdown("**Capacity vs demand (parts/day)**")
            st.bar_chart(pd.DataFrame({"parts": {
                "SAP view of max": cap["sap_capacity"],
                "True max (PLC)": cap["true_capacity"],
                "Current output": cap["current_good_output"],
                "After loss recovery": cap["current_good_output"] + cap["recoverable_parts"],
                "Demand": v["demand"],
            }}), horizontal=True)
        else:
            st.markdown("**Top 3 losses**")
            st.dataframe(pd.DataFrame(u["top3"])[["reason", "hours", "events", "parts", "owner"]],
                         hide_index=True, width="stretch")
    st.markdown("**Top 3 losses: owner and likely cause**")
    st.dataframe(pd.DataFrame(u["top3"])[["reason", "hours", "events", "parts", "value_per_day", "owner",
                                          "likely_cause"]], hide_index=True, width="stretch")


# Line view ------------------------------------------------------------------------
if mode == "line":
    steps = r["steps"]
    widths = []
    for i in range(len(steps)):
        widths += [5, 1] if i < len(steps) - 1 else [5]
    cols = st.columns(widths)
    for i, s in enumerate(steps):
        with cols[2 * i]:
            with st.container(border=True):
                tag = (":red[**BOTTLENECK**]" if s["is_bottleneck"]
                       else ":orange[Next bottleneck]" if s["is_next_bottleneck"] else ":gray[ok]")
                st.markdown(f"**{s['name']}**  \n{s['machine']} · {tag}")
                st.metric("Good parts/day", s["capacity_now"])
                st.caption(f"OEE {s['oee']:.0%} · true CT {s['true_ct_s']} s")
        if i < len(steps) - 1:
            cols[2 * i + 1].markdown("<div style='text-align:center;font-size:2rem;padding-top:2.5rem'>→</div>",
                                     unsafe_allow_html=True)

    c = st.columns(5)
    c[0].metric("Line capacity", f"{r['line_capacity']}/day")
    c[1].metric("Demand", f"{v['demand']}/day")
    c[2].metric("Gap", v["gap_parts"])
    c[3].metric("Headroom to next bottleneck", r["headroom_parts"])
    c[4].metric("Line output after recovery", v["output_after_recovery"])

    st.markdown(f"**Capacity per machine (parts/day). Demand = {v['demand']}**")
    chart = pd.DataFrame({
        "Now": {s["name"]: s["capacity_now"] for s in steps},
        "After loss recovery": {s["name"]: s["capacity_after_recovery"] for s in steps},
        "True max": {s["name"]: s["true_capacity"] for s in steps},
    })
    st.bar_chart(chart, stack=False)

    bn = r["units"][r["bottleneck"]]
    st.divider()
    st.subheader(f"Bottleneck deep-dive: {bn['name']}")
    st.caption("Losses and alerts focus on the bottleneck: an hour recovered anywhere else adds no line output.")
    render_unit(bn, show_capacity_chart=True)
else:
    render_unit(r, show_capacity_chart=True)

# Trend ------------------------------------------------------------------------------
st.divider()
trend = []
for d in DAYS:
    x = analyse(d.isoformat(), mode, machine, tuple(line))
    if x.get("halted"):
        continue
    u = rules.focus_unit(x)
    trend.append({"date": d.isoformat(), "output": x["verdict"]["output_now"], "demand": x["verdict"]["demand"],
                  "oee": u["metrics"]["oee"], "hours_lost": u["hours_lost"],
                  "bottleneck": x.get("bottleneck", machine)})
trend = pd.DataFrame(trend).set_index("date")
trend["hours_recovered"] = (trend["hours_lost"].iloc[0] - trend["hours_lost"]).round(2)
a, b = st.columns(2)
with a:
    st.markdown("**Output vs demand (parts/day)**")
    st.line_chart(trend[["output", "demand"]])
with b:
    st.markdown("**Hours recovered vs first day** (focus machine)")
    st.bar_chart(trend[["hours_recovered"]])
if mode == "line" and trend["bottleneck"].nunique() > 1:
    st.warning("The bottleneck moved during the week: " + ", ".join(f"{i}: {m}" for i, m in trend["bottleneck"].items()))

# All machines -------------------------------------------------------------------
with st.expander("All machines, each running on its own"):
    rows = []
    for m in NAMES:
        x = analyse(day, "single", m, tuple(line))
        if x.get("halted"):
            rows.append({"machine": NAMES[m], "status": "data FAIL"})
            continue
        rows.append({"machine": NAMES[m], "oee": x["metrics"]["oee"], "good parts/day": x["metrics"]["good_parts"],
                     "true max/day": x["capacity"]["true_capacity"], "SAP CT (s)": x["cycle_time"]["sap_ct_s"],
                     "PLC CT (s)": x["cycle_time"]["true_ct_s"], "hours lost": x["hours_lost"],
                     "verdict vs own demand": x["verdict"]["verdict"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

# Alerts -------------------------------------------------------------------------
st.divider()
scope = run.scope_key(r)
saved = Path(run.OUT) / "results" / f"{day}__{scope}.json"
alerts, source = None, None
if saved.exists():
    s = json.loads(saved.read_text())
    alerts, source = s.get("alerts"), s.get("alerts_source")
if st.button("Draft owner alerts now (Claude if an API key is set)"):
    alerts, source = writer.draft_alerts(r)
if alerts:
    st.markdown(f"**Owner alerts** (drafted by {source}, a person decides)")
    for owner, text in alerts.items():
        with st.expander(owner, expanded=True):
            st.write(text)
else:
    st.caption("No alerts drafted for this setup and day yet.")
