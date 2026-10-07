"""One-page dashboard. Run: streamlit run dashboard.py"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st

OUT = Path(__file__).parent / "outputs"

st.set_page_config(page_title="Hidden Capacity Agent", layout="wide")
st.title("Hidden Capacity Agent: Press 3")
st.caption("Before you buy capacity, find the capacity you already have, one bottleneck at a time.")

files = sorted((OUT / "results").glob("*.json"))
if not files:
    st.warning("No results yet. Run `python3 run.py --backfill` then `python3 run.py --now`.")
    st.stop()

day = st.selectbox("Day", [f.stem for f in files][::-1])
r = json.loads((OUT / "results" / f"{day}.json").read_text())

# Data health
h = r["health"]
color = {"PASS": "green", "WARN": "orange", "FAIL": "red"}[h["status"]]
st.markdown(f"**Data health:** :{color}[{h['status']}]")
for i in h["issues"]:
    st.caption(f"[{i['level']}] {i['machine']}: {i['issue']}")
if r.get("halted"):
    st.error(r["halted"])
    st.stop()

m, cap, ct = r["metrics"], r["capacity"], r["cycle_time"]

# Verdict + KPIs
st.subheader(f"New press needed? {cap['verdict']}")
st.write(cap["reason"])
c = st.columns(6)
c[0].metric("OEE", f"{m['oee']:.1%}")
c[1].metric("Availability", f"{m['availability']:.1%}")
c[2].metric("Performance", f"{m['performance']:.1%}")
c[3].metric("Quality", f"{m['quality']:.1%}")
c[4].metric("Good parts / h", m["throughput_per_hour"])
c[5].metric("Hours lost", r["hours_lost"])

if ct["flag"]:
    st.warning(f"Cycle time: SAP says {ct['sap_ct_s']} s, PLC shows {ct['true_ct_s']} s "
               f"({ct['diff_pct']}% gap). SAP understates capacity. Owner: Industrial engineer.")

left, right = st.columns(2)
with left:
    st.markdown("**Hours lost by reason**")
    losses = pd.DataFrame([{"reason": k, "hours": v["hours"]} for k, v in r["losses"].items() if v["hours"] > 0])
    st.bar_chart(losses.set_index("reason").sort_values("hours", ascending=False), horizontal=True)
with right:
    st.markdown("**Capacity vs demand (parts/day)**")
    capdf = pd.DataFrame({"parts": {
        "SAP view of max": cap["sap_capacity"],
        "True max (PLC)": cap["true_capacity"],
        "Current output": cap["current_good_output"],
        "With 50% of top-3 recovered": cap["current_good_output"] + cap["recoverable_parts"],
        "Demand": cap["demand"],
    }})
    st.bar_chart(capdf, horizontal=True)

log_path = OUT / "daily_log.csv"
if log_path.exists():
    log = pd.read_csv(log_path).set_index("date")
    a, b = st.columns(2)
    with a:
        st.markdown("**OEE trend**")
        st.line_chart(log[["oee"]])
    with b:
        st.markdown("**Hours recovered vs first day**")
        st.bar_chart(log[["hours_recovered"]])

st.markdown("**Top 3 losses**")
st.dataframe(pd.DataFrame(r["top3"])[["reason", "hours", "events", "parts", "value_per_day", "owner", "likely_cause"]],
             hide_index=True, width="stretch")

st.markdown(f"**Owner alerts** (drafted by {r.get('alerts_source', 'n/a')}, a person decides)")
for owner, text in r.get("alerts", {}).items():
    with st.expander(owner, expanded=True):
        st.write(text)
