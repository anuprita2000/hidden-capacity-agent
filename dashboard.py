"""Plant dashboard. Run: python3 -m streamlit run dashboard.py

Pages
  Plant overview  - every line at a glance: on track or not, value at stake, investment calls, actions
  Lines           - one line's flow, bottleneck, capacity and owner alerts
  Machines        - any machine on its own
  Line setup      - create / edit / delete lines as stations (single machine, assembly, or side by side)
"""
import json
import uuid
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import rules
import run
import writer

st.set_page_config(page_title="Hidden Capacity · Plant dashboard", page_icon=":material/factory:", layout="wide")

# Design tokens -----------------------------------------------------------------
INK, INK2, MUTED, GRID, CARD = "#0b0b0b", "#52514e", "#8a8984", "#e3e2de", "#ffffff"
BLUE, BLUE_MID, BLUE_PALE = "#2a78d6", "#86b6ef", "#cde2fb"
STATUS = {"good": ("#0ca30c", "✓"), "warning": ("#fab219", "▲"), "critical": ("#d03b3b", "✕")}
VERDICT = {"No": ("good", "On track"), "Not yet": ("warning", "Recover losses first"),
           "Yes": ("critical", "Capacity needed")}
HEALTH = {"PASS": ("good", "Data OK"), "WARN": ("warning", "Data warnings"), "FAIL": ("critical", "Data failed")}

st.markdown("""
<style>
.block-container {padding-top: 2rem; max-width: 1400px;}
.kpi {background: CARD; border: 1px solid GRID; border-radius: 10px; padding: 14px 16px; height: 100%;}
.kpi .label {font-size: .74rem; color: INK2; text-transform: uppercase; letter-spacing: .05em; font-weight: 600;}
.kpi .value {font-size: 1.7rem; font-weight: 650; color: INK; line-height: 1.25; margin-top: 4px;}
.kpi .sub {font-size: .8rem; color: INK2; margin-top: 2px;}
.badge {display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 999px;
        font-size: .78rem; font-weight: 600; color: INK; background: CARD; border: 1px solid GRID; white-space: nowrap;}
.badge .ic {font-weight: 800;}
.flow {display: flex; flex-wrap: wrap; align-items: stretch; gap: 8px; margin: 6px 0 4px;}
.flow .arrow {align-self: center; color: MUTED; font-size: 1.2rem;}
.node {background: CARD; border: 1px solid GRID; border-radius: 10px; padding: 8px 12px; min-width: 120px;}
.node .nm {font-weight: 650; color: INK; font-size: .9rem;}
.node .id {color: INK2; font-size: .75rem;}
.node .num {font-size: 1.35rem; font-weight: 650; color: INK; margin-top: 4px;}
.node .meta {color: INK2; font-size: .75rem;}
.node.bn {border: 2px solid #d03b3b;}
.node.next {border: 2px dashed #fab219;}
.node.big {min-width: 170px; padding: 12px 16px;}
.station {border: 1px solid GRID; border-radius: 12px; padding: 8px; background: #f1f0ec;
          display: flex; flex-direction: column; gap: 6px;}
.station.bn {border: 2px solid #d03b3b;}
.station.next {border: 2px dashed #fab219;}
.st-head {font-size: .68rem; color: INK2; text-transform: uppercase; letter-spacing: .05em; font-weight: 650;}
.st-body {display: flex; flex-direction: column; gap: 6px;}
.st-foot {font-size: .85rem; color: INK;}
.node.mini {padding: 6px 10px; min-width: 160px;}
.node .num.sm {font-size: 1.05rem; margin-top: 2px;}
.section {font-size: 1.05rem; font-weight: 650; color: INK; margin: 1.4rem 0 .4rem;}
.muted {color: INK2; font-size: .85rem;}
.stats {display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 8px;}
.stat .label {font-size: .72rem; color: INK2; text-transform: uppercase; letter-spacing: .05em; font-weight: 600;}
.stat .v {font-size: 1.15rem; font-weight: 650; color: INK; margin-top: 2px;}
.stat .sub {font-size: .76rem; color: INK2;}
</style>
""".replace("CARD", CARD).replace("GRID", GRID).replace("INK2", INK2).replace("MUTED", MUTED).replace("INK", INK),
            unsafe_allow_html=True)


def badge(kind, label):
    color, icon = STATUS[kind]
    return f'<span class="badge"><span class="ic" style="color:{color}">{icon}</span>{label}</span>'


def kpi(label, value, sub=""):
    return f'<div class="kpi"><div class="label">{label}</div><div class="value">{value}</div><div class="sub">{sub}</div></div>'


def tiles(items):
    cols = st.columns(len(items))
    for c, item in zip(cols, items):
        c.markdown(kpi(*item), unsafe_allow_html=True)


def stats_html(items):
    cells = "".join(f'<div class="stat"><div class="label">{a}</div><div class="v">{b}</div><div class="sub">{c}</div></div>'
                    for a, b, c in items)
    return f'<div class="stats">{cells}</div>'


def section(title, note=""):
    st.markdown(f'<div class="section">{title}</div>' + (f'<div class="muted">{note}</div>' if note else ""),
                unsafe_allow_html=True)


def oee_kind(o):
    return "good" if o >= 0.85 else "warning" if o >= 0.65 else "critical"


def style(chart, height=260):
    return (chart.properties(height=height)
            .configure_axis(gridColor=GRID, domainColor=GRID, tickColor=GRID, labelColor=INK2, titleColor=INK2,
                            labelFontSize=11, titleFontSize=11, titleFontWeight="normal")
            .configure_view(stroke=None)
            .configure_legend(orient="top", labelColor=INK2, title=None, labelFontSize=11, symbolType="square"))


def flow_html(steps, big=False):
    """Stations left to right. A station with several machines is drawn as a stacked group."""
    parts = []
    for i, s_ in enumerate(steps):
        role, cls_role = "", ""
        if s_["is_bottleneck"]:
            cls_role, role = " bn", badge("critical", "Bottleneck")
        elif s_["is_next_bottleneck"]:
            cls_role, role = " next", badge("warning", "Next bottleneck")
        if len(s_["machines"]) == 1:
            cls = ("node big" if big else "node") + cls_role
            parts.append(f'<div class="{cls}"><div class="nm">{s_["name"]}</div><div class="id">{s_["machine"]}</div>'
                         f'<div class="num">{s_["capacity_now"]:,}</div><div class="meta">good parts/day · OEE '
                         f'{s_["oee"]:.0%}</div>' + (f'<div style="margin-top:6px">{role}</div>' if role else "") + "</div>")
        else:
            assemble = s_["combine"] == "assemble"
            head = "Different parts → one assembly" if assemble else "Same part · outputs add"
            inner = ""
            for x in s_["machines"]:
                qty = f" · needs {x['qty']} per set" if assemble and x["qty"] != 1 else ""
                tag = ('<div class="meta" style="color:#d03b3b;font-weight:600">✕ limits the sets</div>'
                       if assemble and x["is_limiting"] else "")
                inner += (f'<div class="node mini"><div class="nm">{x["name"]}</div><div class="id">{x["machine"]}{qty}</div>'
                          f'<div class="num sm">{x["capacity_now"]:,}</div><div class="meta">parts/day · OEE {x["oee"]:.0%}'
                          f'</div>{tag}</div>')
            unit = "complete sets/day" if assemble else "parts/day combined"
            parts.append(f'<div class="station{cls_role}"><div class="st-head">{head}</div><div class="st-body">{inner}'
                         f'</div><div class="st-foot"><b>{s_["capacity_now"]:,}</b> {unit}</div>'
                         + (f'<div>{role}</div>' if role else "") + "</div>")
        if i < len(steps) - 1:
            parts.append('<div class="arrow">→</div>')
    return '<div class="flow">' + "".join(parts) + "</div>"


def bullet_chart(rows, demand, order, x_title="Good parts / day"):
    """Bullet chart: true max (track) > after recovery > now, with demand as a dashed rule."""
    df = pd.DataFrame(rows)
    domain = ["True max", "After loss recovery", "Now"]
    scale = alt.Scale(domain=domain, range=[BLUE_PALE, BLUE_MID, BLUE])
    base = alt.Chart(df)
    layers = []
    for measure, size in zip(domain, [26, 16, 8]):
        layers.append(base.transform_filter(alt.datum.measure == measure).mark_bar(size=size, cornerRadiusEnd=4).encode(
            y=alt.Y("machine:N", title=None, axis=alt.Axis(labelLimit=220),
                    scale=alt.Scale(domain=order, paddingInner=0.35, paddingOuter=0.2)),
            x=alt.X("parts:Q", title=x_title),
            color=alt.Color("measure:N", scale=scale),
            tooltip=[alt.Tooltip("machine:N", title="Machine"), alt.Tooltip("measure:N", title="Measure"),
                     alt.Tooltip("parts:Q", title="Parts/day", format=",")]))
    rule = alt.Chart(pd.DataFrame({"demand": [demand]})).mark_rule(color=INK, strokeDash=[4, 3], size=2).encode(
        x="demand:Q", tooltip=[alt.Tooltip("demand:Q", title="Demand", format=",")])
    label = alt.Chart(pd.DataFrame({"demand": [demand], "t": [f"Demand {demand:,}"]})).mark_text(
        align="left", dx=4, dy=-6, color=INK, fontSize=11, fontWeight=600).encode(x="demand:Q", y=alt.value(0),
                                                                                    text="t:N")
    return style(alt.layer(*layers, rule, label), height=110 + 56 * len(order))


def losses_chart(losses):
    df = pd.DataFrame([{"reason": k, "hours": v["hours"], "owner": v["owner"], "events": v["events"]}
                       for k, v in losses.items() if v["hours"] > 0])
    bars = alt.Chart(df).mark_bar(size=16, cornerRadiusEnd=4, color=BLUE).encode(
        y=alt.Y("reason:N", sort="-x", title=None, axis=alt.Axis(labelLimit=200)),
        x=alt.X("hours:Q", title="Hours lost"),
        tooltip=[alt.Tooltip("reason:N", title="Loss"), alt.Tooltip("hours:Q", title="Hours", format=".2f"),
                 alt.Tooltip("events:Q", title="Events"), alt.Tooltip("owner:N", title="Owner")])
    text = bars.mark_text(align="left", dx=4, color=INK2, fontSize=11).encode(text=alt.Text("hours:Q", format=".2f"))
    return style(bars + text, height=max(140, 34 * len(df)))


def trend_chart(df, value_title):
    """df: date, series, value. Output/OEE lines; demand drawn dashed."""
    line = alt.Chart(df).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=60, filled=True)).encode(
        x=alt.X("date:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("value:Q", title=value_title, scale=alt.Scale(zero=False)),
        color=alt.Color("series:N", scale=alt.Scale(
            domain=[x for x in df["series"].unique() if x != "Demand"] + (["Demand"] if "Demand" in set(df["series"]) else []),
            range=[BLUE, MUTED])),
        strokeDash=alt.StrokeDash("dash:N", legend=None, scale=alt.Scale(domain=["solid", "dash"],
                                                                          range=[[1, 0], [5, 4]])),
        tooltip=["date:N", "series:N", alt.Tooltip("value:Q", format=",.3~f")])
    return style(line, height=240)


# Data ----------------------------------------------------------------------------
@st.cache_resource
def get_data():
    return rules.load()


data = get_data()
NAMES = rules.machines(data)
SHORT = {m: r["description"] for m, r in data["master"].iterrows()}
DAYS = [d.isoformat() for d in rules.open_days(data)]


@st.cache_data(show_spinner="Analysing PLC and SAP data...")
def a_line(day_iso, stations_json, demand):
    return rules.analyse_line(data, date.fromisoformat(day_iso), json.loads(stations_json), demand)


def skey(ln):
    """Hashable key for a line's stations (cache key for a_line)."""
    return json.dumps(rules.normalize_stations(ln["stations"]), sort_keys=True)


@st.cache_data(show_spinner="Analysing PLC and SAP data...")
def a_machine(day_iso, machine, demand=None):
    return rules.analyse_machine(data, date.fromisoformat(day_iso), machine, demand)


def own_demand(day_iso, machine):
    return rules._demand(data, date.fromisoformat(day_iso), machine)


def _with_ids(line):
    line = dict(line, id=uuid.uuid4().hex[:8])
    line["stations"] = [dict(st_, sid=uuid.uuid4().hex[:8]) for st_ in line["stations"]]
    return line


if "lines" not in st.session_state:
    st.session_state.lines = [_with_ids(line) for line in run.load_lines()]
if "day" not in st.session_state:
    st.session_state.day = DAYS[-1]


def line_machines(ln):
    return [m for st_ in ln["stations"] for m in st_["machines"]]


def lines():
    """Lines that can be analysed: at least 2 machines and no empty station."""
    return [ln for ln in st.session_state.lines
            if len(line_machines(ln)) >= 2 and all(st_["machines"] for st_ in ln["stations"])]


def line_of(machine):
    for ln in lines():
        if machine in line_machines(ln):
            return ln["name"]
    return "Standalone"


# Pages ---------------------------------------------------------------------------
def page_overview():
    day = st.session_state.day
    results = [(ln, a_line(day, skey(ln), int(ln["demand"]))) for ln in lines()]
    ok = [(ln, r) for ln, r in results if not r.get("halted")]

    st.title("Plant overview")
    st.markdown(f'<div class="muted">Plant 1 · Stamping &amp; Assembly · {day} · computed after shifts closed. '
                f'Rules do the maths, Claude drafts the alerts, people decide.</div>', unsafe_allow_html=True)

    on_track = sum(r["verdict"]["verdict"] == "No" for _, r in ok)
    output = sum(r["verdict"]["output_now"] for _, r in ok)
    demand = sum(r["verdict"]["demand"] for _, r in ok)
    gap = sum(r["verdict"]["gap_parts"] for _, r in ok)
    value = sum(r["verdict"]["recoverable_value_per_year"] for _, r in ok)
    calls = []
    for ln, r in ok:
        if r["verdict"]["verdict"] == "Yes":
            m = r["verdict"]["buy_at"]
            capex = float(data["costs"].get(f"capex_{r['units'][m]['type']}", 0))
            calls.append((ln["name"], SHORT[m], capex))
    worst = max((r["health"]["status"] for _, r in results), key=["PASS", "WARN", "FAIL"].index, default="PASS")
    kind, label = HEALTH[worst]
    st.write("")
    tiles([
        ("Lines meeting demand", f"{on_track} of {len(results)}", "yesterday's good output vs demand"),
        ("Plant output", f"{output:,}", f"of {demand:,} parts/day demand ({output / demand:.0%})" if demand else ""),
        ("Shortfall", f"{gap:,}", "parts/day across all lines"),
        ("Recoverable value", f"${value / 1000:,.0f}K/yr", "50% of top-3 bottleneck losses"),
        ("Investment calls", f"{len(calls)} justified",
         " · ".join(f"{c[1]} ${c[2] / 1000:,.0f}K" for c in calls) or "none: recover losses first"),
        ("Data health", badge(kind, label), f"{sum(len(r['health']['issues']) for _, r in results[:1])} issues"),
    ])

    section("Lines", "Each line ships what its bottleneck makes. Click a line to see the details.")
    for ln, r in results:
        with st.container(border=True):
            if r.get("halted"):
                st.markdown(f"**{ln['name']}** " + badge("critical", "Data failed"), unsafe_allow_html=True)
                st.caption(r["halted"])
                continue
            v = r["verdict"]
            kind, label = VERDICT[v["verdict"]]
            head, btn = st.columns([6, 1])
            head.markdown(f"<span style='font-size:1.1rem;font-weight:650'>{ln['name']}</span> &nbsp; "
                          f"{badge(kind, label)}<div class='muted' style='margin-top:4px'>{v['reason']}</div>",
                          unsafe_allow_html=True)
            if btn.button("Open line →", key=f"open_{ln['id']}"):
                st.session_state.sel_line = ln["name"]
                st.switch_page(LINE_PAGE)
            st.markdown(flow_html(r["steps"]), unsafe_allow_html=True)
            bn = r["units"][r["bottleneck"]]
            nb = r["next_bottleneck"]
            st.markdown(stats_html([
                ("Output / demand", f"{v['output_now']:,} / {v['demand']:,}", f"{v['gap_parts']:,} parts/day short"
                 if v["gap_parts"] else "demand covered"),
                ("Bottleneck", SHORT[r["bottleneck"]], f"OEE {bn['metrics']['oee']:.0%}"),
                ("Next bottleneck", r["next_bottleneck_name"] if nb else "—",
                 f"{r['headroom_parts']:,} units of headroom" if nb else ""),
                ("After loss recovery", f"{v['output_after_recovery']:,}", "parts/day"),
                ("Value at stake", f"${v['recoverable_value_per_year'] / 1000:,.0f}K", "per year"),
            ]), unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        section("Output vs demand by line", "Bars: output now, after loss recovery, true max. Dashed: demand.")
        rows, order = [], []
        for ln, r in ok:
            order.append(ln["name"])
            v = r["verdict"]
            bn_unit = r["units"][r["bottleneck"]]
            rows += [{"machine": ln["name"], "measure": "Now", "parts": v["output_now"]},
                     {"machine": ln["name"], "measure": "After loss recovery", "parts": v["output_after_recovery"]},
                     {"machine": ln["name"], "measure": "True max", "parts": min(s["true_capacity"] for s in r["steps"])}]
        if rows:
            st.altair_chart(bullet_chart(rows, max(r["verdict"]["demand"] for _, r in ok), order, "Finished units / day"),
                            use_container_width=True)
    with right:
        section("Plant output vs demand, last 6 days", "Sum of all lines.")
        trend = []
        for d in DAYS:
            out = dem = 0
            for ln in lines():
                r = a_line(d, skey(ln), int(ln["demand"]))
                if not r.get("halted"):
                    out += r["verdict"]["output_now"]
                    dem += r["verdict"]["demand"]
            trend += [{"date": d[5:], "series": "Output", "value": out, "dash": "solid"},
                      {"date": d[5:], "series": "Demand", "value": dem, "dash": "dash"}]
        st.altair_chart(trend_chart(pd.DataFrame(trend), "Parts / day"), use_container_width=True)

    section("Priority actions", "Bottleneck losses only, ranked by value. An hour saved anywhere else adds no output.")
    acts = []
    for ln, r in ok:
        bn = r["units"][r["bottleneck"]]
        for t in bn["top3"]:
            acts.append({"Line": ln["name"], "Machine": bn["name"], "Owner": t["owner"], "Loss": t["reason"],
                         "Likely cause": t["likely_cause"], "Hours lost": t["hours"],
                         "Parts/day at 50%": t["parts"] // 2, "Value/day": t["value_per_day"] // 2})
        ct = bn["cycle_time"]
        if ct["flag"]:
            acts.append({"Line": ln["name"], "Machine": bn["name"], "Owner": "Industrial engineer",
                         "Loss": "Wrong SAP cycle time", "Likely cause": f"SAP {ct['sap_ct_s']} s vs PLC {ct['true_ct_s']} s",
                         "Hours lost": None, "Parts/day at 50%": None, "Value/day": None})
    if acts:
        st.dataframe(pd.DataFrame(acts).sort_values("Value/day", ascending=False, na_position="last")
                     [["Value/day", "Line", "Machine", "Owner", "Loss", "Hours lost", "Parts/day at 50%", "Likely cause"]],
                     hide_index=True, width="stretch",
                     column_config={"Value/day": st.column_config.NumberColumn(format="$%d"),
                                    "Hours lost": st.column_config.NumberColumn(format="%.2f h")})

    section("Machine status", "Every machine on its own. Role is its place in its line.")
    roles = {}
    for ln, r in ok:
        roles[r["bottleneck"]] = "✕ Bottleneck"
        if r["next_bottleneck"]:
            roles[r["next_bottleneck"]] = "▲ Next bottleneck"
    rows = []
    for m in NAMES:
        x = a_machine(day, m)
        if x.get("halted"):
            rows.append({"Machine": NAMES[m], "Line": line_of(m), "Status": "✕ Data failed"})
            continue
        o = x["metrics"]["oee"]
        rows.append({"Machine": NAMES[m], "Line": line_of(m), "Role": roles.get(m, "—"), "OEE": o,
                     "Status": {"good": "✓ Good", "warning": "▲ Watch", "critical": "✕ Poor"}[oee_kind(o)],
                     "Good/day": x["metrics"]["good_parts"], "True max": x["capacity"]["true_capacity"],
                     "CT SAP→PLC": f"{x['cycle_time']['sap_ct_s']:g} → {x['cycle_time']['true_ct_s']:g} s",
                     "Hrs lost": x["hours_lost"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"OEE": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)})
    st.caption("OEE status: ✓ Good ≥ 85% · ▲ Watch 65–85% · ✕ Poor < 65%")

    with st.expander(f"Data health details ({worst})"):
        for i in (results[0][1]["health"]["issues"] if results else []):
            st.markdown(badge("critical" if i["level"] == "FAIL" else "warning", i["level"]) +
                        f" &nbsp; **{i['machine']}**: {i['issue']}", unsafe_allow_html=True)


def page_line():
    day = st.session_state.day
    names = [ln["name"] for ln in lines()]
    if not names:
        st.info("No lines yet. Create one on the Line setup page.")
        return
    sel = st.session_state.get("sel_line", names[0])
    name = st.selectbox("Line", names, index=names.index(sel) if sel in names else 0)
    st.session_state.sel_line = name
    ln = next(x for x in lines() if x["name"] == name)
    r = a_line(day, skey(ln), int(ln["demand"]))
    if r.get("halted"):
        st.error(r["halted"])
        return
    v = r["verdict"]
    kind, label = VERDICT[v["verdict"]]
    st.markdown(f"<h2 style='margin-bottom:0'>{name} &nbsp; {badge(kind, label)}</h2>"
                f"<div class='muted'>{v['reason']}</div>", unsafe_allow_html=True)
    st.markdown(flow_html(r["steps"], big=True), unsafe_allow_html=True)
    tiles([
        ("Line capacity", f"{r['line_capacity']:,}", "finished units/day (what the bottleneck allows)"),
        ("Demand", f"{v['demand']:,}", "units/day"),
        ("Gap", f"{v['gap_parts']:,}", "units/day short" if v["gap_parts"] else "demand covered"),
        ("Headroom", f"{r['headroom_parts']:,}" if r["headroom_parts"] is not None else "—",
         f"units before {r['next_bottleneck_name']} takes over" if r["next_bottleneck"] else ""),
        ("After loss recovery", f"{v['output_after_recovery']:,}", "50% of top-3 losses on every machine"),
    ])

    section("Capacity per station", "Units/day. Bars: true max, after loss recovery, now. Dashed: demand. "
                                    "Assembly stations count complete sets.")
    rows, order = [], []
    for s in r["steps"]:
        label_ = f"{s['step']}. {s['name']}" + (" (sets)" if s["combine"] == "assemble" else "")
        order.append(label_)
        rows += [{"machine": label_, "measure": "True max", "parts": s["true_capacity"]},
                 {"machine": label_, "measure": "After loss recovery", "parts": s["capacity_after_recovery"]},
                 {"machine": label_, "measure": "Now", "parts": s["capacity_now"]}]
    st.altair_chart(bullet_chart(rows, v["demand"], order, "Finished units / day"), use_container_width=True)

    bn = r["units"][r["bottleneck"]]
    st.divider()
    st.subheader(f"Bottleneck deep-dive: {bn['name']}")
    bn_step = next(s for s in r["steps"] if s["is_bottleneck"])
    if bn_step["combine"] == "assemble":
        others = ", ".join(f"{x['name']} makes {x['capacity_now']:,}/day" for x in bn_step["machines"]
                           if x["machine"] != bn["machine"])
        st.info(f"**{bn['name']} limits the assembly.** It makes {bn['capacity']['current_good_output']:,} parts/day "
                f"while {others}, so the next station only receives {r['line_capacity']:,} complete sets. "
                f"Extra parts from the faster press just pile up as inventory.")
    st.caption("Alerts focus here: an hour recovered on any other machine adds no line output.")
    machine_body(bn)

    section("Last 6 days")
    trend, lost = [], []
    for d in DAYS:
        x = a_line(d, skey(ln), int(ln["demand"]))
        if x.get("halted"):
            continue
        trend += [{"date": d[5:], "series": "Line output", "value": x["verdict"]["output_now"], "dash": "solid"},
                  {"date": d[5:], "series": "Demand", "value": x["verdict"]["demand"], "dash": "dash"}]
        lost.append({"date": d[5:], "hours": x["units"][x["bottleneck"]]["hours_lost"], "bottleneck": SHORT[x["bottleneck"]]})
    a, b = st.columns(2)
    a.altair_chart(trend_chart(pd.DataFrame(trend), "Parts / day"), use_container_width=True)
    lost = pd.DataFrame(lost)
    b.altair_chart(style(alt.Chart(lost).mark_bar(size=22, cornerRadiusEnd=4, color=BLUE).encode(
        x=alt.X("date:N", title=None, axis=alt.Axis(labelAngle=0)), y=alt.Y("hours:Q", title="Hours lost at bottleneck"),
        tooltip=["date:N", "bottleneck:N", alt.Tooltip("hours:Q", format=".2f")]), height=240),
        use_container_width=True)
    if lost["bottleneck"].nunique() > 1:
        st.warning("The bottleneck moved this week: " + ", ".join(f"{a_}: {b_}" for a_, b_ in zip(lost["date"], lost["bottleneck"])))

    alerts_block(r, run.slug(name))


def machine_body(u):
    m, ct, cap = u["metrics"], u["cycle_time"], u["capacity"]
    made = m["good_parts"] + m["scrap_parts"]
    planned = m["planned_hours"]
    status = {"good": "Good", "warning": "Watch", "critical": "Poor"}[oee_kind(m["oee"])]
    tiles([
        ("OEE", f"{m['oee']:.1%}", f"{badge(oee_kind(m['oee']), status)}<br>world class is 85%"),
        ("Availability", f"{m['availability']:.1%}",
         f"ran {m['run_hours']:.1f} h of {planned:.1f} h planned"),
        ("Performance", f"{m['performance']:.1%}",
         f"speed while running, vs best {ct['true_ct_s']:g} s/cycle"),
        ("Quality", f"{m['quality']:.1%}",
         f"{m['good_parts']:,} good + {m['scrap_parts']:,} scrap = {made:,} made"),
        ("Good parts / hour", f"{m['throughput_per_hour']:g}",
         f"{m['good_parts']:,} good parts in {planned:.0f} planned hours"),
        ("Hours lost", f"{u['hours_lost']:.1f} h",
         f"of {planned:.1f} planned hours ({u['hours_lost'] / planned:.0%})" if planned else ""),
    ])
    st.caption(f"How to read this: OEE = Availability × Performance × Quality "
               f"({m['availability']:.0%} × {m['performance']:.0%} × {m['quality']:.0%} = {m['oee']:.0%}). "
               f"Planned time = {planned:.1f} h (two 8-hour shifts minus breaks). "
               f"Hours lost = planned time not spent making good parts at full speed.")
    if ct["flag"]:
        st.warning(f"**Cycle time is wrong in SAP.** SAP says {ct['sap_ct_s']} s, the PLC shows {ct['true_ct_s']} s "
                   f"({ct['diff_pct']}% gap over {ct['cycles_sampled']:,} cycles). SAP shows a max of "
                   f"{cap['sap_capacity']:,}/day; the true max is {cap['true_capacity']:,}/day. Owner: Industrial engineer.")
    left, right = st.columns([1, 1])
    with left:
        section("Hours lost by reason")
        st.altair_chart(losses_chart(u["losses"]), use_container_width=True)
    with right:
        section("Top 3 losses")
        st.dataframe(pd.DataFrame(u["top3"])[["reason", "hours", "events", "owner", "likely_cause", "value_per_day"]]
                     .rename(columns={"reason": "Loss", "hours": "Hours", "events": "Events", "owner": "Owner",
                                      "likely_cause": "Likely cause", "value_per_day": "Value/day"}),
                     hide_index=True, width="stretch",
                     column_config={"Value/day": st.column_config.NumberColumn(format="$%d")})


def alerts_block(r, key):
    section("Owner alerts", "Drafted, not sent. A person reviews and acts.")
    saved = Path(run.OUT) / "results" / f"{st.session_state.day}__{key}.json"
    state_key = f"alerts_{st.session_state.day}_{key}"
    if state_key not in st.session_state and saved.exists():
        s = json.loads(saved.read_text())
        st.session_state[state_key] = (s.get("alerts"), s.get("alerts_source"))
    if st.button("Draft alerts now (Claude if an API key is set, otherwise template)", key=f"draft_{key}"):
        st.session_state[state_key] = writer.draft_alerts(r)
    alerts, source = st.session_state.get(state_key, (None, None))
    if not alerts:
        st.caption("No alerts drafted for this day yet.")
        return
    st.caption(f"Drafted by {source}")
    cols = st.columns(2)
    for i, (owner, text) in enumerate(alerts.items()):
        with cols[i % 2].container(border=True):
            st.markdown(f"**{owner}**")
            st.write(text.replace("$", "\\$"))


def page_machine():
    day = st.session_state.day
    ids = list(NAMES)
    machine = st.selectbox("Machine", ids, index=ids.index(st.session_state.get("sel_machine", "PRS-03")),
                           format_func=NAMES.get)
    st.session_state.sel_machine = machine
    demand = st.number_input("Demand for this machine on its own (parts/day)", min_value=0, step=50,
                             value=own_demand(day, machine))
    x = a_machine(day, machine, int(demand))
    if x.get("halted"):
        st.error(x["halted"])
        return
    v = x["verdict"]
    kind, label = VERDICT[v["verdict"]]
    st.markdown(f"<h2 style='margin-bottom:0'>{x['name']} &nbsp; {badge(kind, label)}</h2>"
                f"<div class='muted'>{machine} · {x['type']} · {line_of(machine)} · {v['reason']}</div>",
                unsafe_allow_html=True)
    st.write("")
    machine_body(x)
    left, right = st.columns(2)
    with left:
        section("Capacity vs demand", "Bars: true max, after loss recovery, now. Dashed: demand.")
        cap = x["capacity"]
        rows = [{"machine": x["name"], "measure": "True max", "parts": cap["true_capacity"]},
                {"machine": x["name"], "measure": "After loss recovery",
                 "parts": cap["current_good_output"] + cap["recoverable_parts"]},
                {"machine": x["name"], "measure": "Now", "parts": cap["current_good_output"]}]
        st.altair_chart(bullet_chart(rows, int(demand), [x["name"]]), use_container_width=True)
        st.caption(f"SAP shows a max of {cap['sap_capacity']:,}/day.")
    with right:
        section("OEE, last 6 days")
        t = []
        for d in DAYS:
            y = a_machine(d, machine, int(demand))
            if not y.get("halted"):
                t.append({"date": d[5:], "series": "OEE", "value": y["metrics"]["oee"], "dash": "solid"})
        st.altair_chart(trend_chart(pd.DataFrame(t), "OEE"), use_container_width=True)
    alerts_block(x, machine)


def page_setup():
    st.title("Line setup")
    st.markdown('<div class="muted">Describe how the plant actually runs. A line is a series of <b>stations</b> in '
                'the order parts move. A station is one machine, or several machines that either make '
                '<b>different parts for one assembly</b> (e.g. outer + inner door panel into one weld cell) or the '
                '<b>same part side by side</b>. Changes show on every page right away; <b>Save</b> makes the 6 am '
                'run use them.</div>', unsafe_allow_html=True)
    st.write("")
    ss = st.session_state
    remove_line = None
    combine_labels = {"assemble": "Different parts → one assembly", "add": "Same part → outputs add"}
    for i, ln in enumerate(ss.lines):
        with st.container(border=True):
            a, b, c = st.columns([3, 1.2, 0.7])
            ln["name"] = a.text_input("Line name", ln["name"], key=f"name_{ln['id']}")
            ln["demand"] = int(b.number_input("Demand (finished units/day)", min_value=0, step=50,
                                              value=int(ln["demand"]), key=f"dem_{ln['id']}"))
            c.write("")
            c.write("")
            if c.button("Delete line", key=f"del_{ln['id']}"):
                remove_line = i
            remove_station = None
            for j, st_ in enumerate(ln["stations"]):
                k = f"{ln['id']}_{st_['sid']}"
                cols = st.columns([0.5, 3.2, 2.2, 0.6])
                cols[0].markdown(f"<div style='padding-top:2.1rem;font-weight:650'>Station {j + 1}</div>",
                                 unsafe_allow_html=True)
                st_["machines"] = cols[1].multiselect("Machines at this station", list(NAMES), default=st_["machines"],
                                                      format_func=NAMES.get, key=f"m_{k}")
                if len(st_["machines"]) > 1:
                    cur = st_.get("combine") if st_.get("combine") in combine_labels else "assemble"
                    st_["combine"] = cols[2].radio("How do they combine?", list(combine_labels),
                                                   index=list(combine_labels).index(cur),
                                                   format_func=combine_labels.get, key=f"c_{k}")
                    if st_["combine"] == "assemble":
                        qcols = st.columns([0.5] + [1] * len(st_["machines"]) + [max(0.1, 4 - len(st_["machines"]))])
                        qty = st_.get("qty") or {}
                        new_qty = {}
                        for q, m in zip(qcols[1:], st_["machines"]):
                            new_qty[m] = int(q.number_input(f"{SHORT[m]}: parts per set", min_value=1, step=1,
                                                            value=int(qty.get(m, 1)), key=f"q_{k}_{m}"))
                        st_["qty"] = new_qty
                else:
                    st_["combine"] = "single"
                    cols[2].markdown("<div class='muted' style='padding-top:2.2rem'>Single machine</div>",
                                     unsafe_allow_html=True)
                cols[3].write("")
                cols[3].write("")
                if len(ln["stations"]) > 1 and cols[3].button("✕", key=f"rs_{k}", help="Remove this station"):
                    remove_station = j
            if remove_station is not None:
                ln["stations"].pop(remove_station)
                st.rerun()
            x, y = st.columns([1, 5])
            if x.button("+ Add station", key=f"as_{ln['id']}"):
                ln["stations"].append({"sid": uuid.uuid4().hex[:8], "machines": [], "combine": "single", "qty": {}})
                st.rerun()
            ms = line_machines(ln)
            if len(ms) < 2 or not all(st_["machines"] for st_ in ln["stations"]):
                y.warning("A line needs at least 2 machines and no empty stations. It is hidden until then.")
            else:
                flow = []
                for st_ in ln["stations"]:
                    if len(st_["machines"]) > 1:
                        join = " + " if st_["combine"] == "assemble" else " ‖ "
                        flow.append("(" + join.join(SHORT[m] for m in st_["machines"]) + ")")
                    else:
                        flow.append(SHORT[st_["machines"][0]])
                y.caption("Flow: " + " → ".join(flow) + "   ·   + = assembled together, ‖ = same part side by side")
    if remove_line is not None:
        ss.lines.pop(remove_line)
        st.rerun()

    used = [m for ln in ss.lines for m in line_machines(ln)]
    dupes = sorted({m for m in used if used.count(m) > 1})
    if dupes:
        st.warning(f"{', '.join(SHORT[m] for m in dupes)} appears more than once. A machine can only sit in one "
                   f"station of one line, otherwise its output is counted twice.")
    names = [ln["name"] for ln in ss.lines]
    if len(set(names)) < len(names):
        st.warning("Two lines have the same name. Give each line a unique name.")
    free = [SHORT[m] for m in NAMES if m not in used]
    st.caption("Not in any line (shown as standalone): " + (", ".join(free) if free else "none"))

    a, b, _ = st.columns([1, 1.6, 3.4])
    if a.button("+ Add line"):
        ss.lines.append({"id": uuid.uuid4().hex[:8], "name": f"Line {chr(65 + len(ss.lines))}", "demand": 1000,
                         "stations": [{"sid": uuid.uuid4().hex[:8], "machines": [], "combine": "single", "qty": {}}]})
        st.rerun()
    if b.button("Save for the 6 am run", type="primary", disabled=bool(dupes) or len(set(names)) < len(names)):
        run.save_lines([{"name": ln["name"], "demand": ln["demand"],
                         "stations": rules.normalize_stations(ln["stations"])} for ln in lines()])
        st.success("Saved. The daily run will analyse these lines and send each one's alerts.")
    with st.expander("How the daily run works"):
        st.code("0 6 * * * cd ~/hidden-capacity-agent && /usr/bin/python3 run.py --now >> outputs/cron.log 2>&1",
                language="bash")
        st.caption("Runs every saved line at 6 am, drafts one alert per owner per line, posts to Slack if "
                   "SLACK_WEBHOOK_URL is set, and saves a copy in outputs/.")


# Navigation ------------------------------------------------------------------------
OVERVIEW_PAGE = st.Page(page_overview, title="Plant overview", icon=":material/dashboard:", default=True)
LINE_PAGE = st.Page(page_line, title="Lines", icon=":material/account_tree:")
MACHINE_PAGE = st.Page(page_machine, title="Machines", icon=":material/precision_manufacturing:")
SETUP_PAGE = st.Page(page_setup, title="Line setup", icon=":material/tune:")
nav = st.navigation({"Plant": [OVERVIEW_PAGE, LINE_PAGE, MACHINE_PAGE], "Admin": [SETUP_PAGE]})

with st.sidebar:
    st.markdown("**Hidden Capacity Agent**")
    st.caption("Find the capacity you already have, one bottleneck at a time.")
    st.session_state.day = st.selectbox("Production day", DAYS[::-1], index=DAYS[::-1].index(st.session_state.day))

nav.run()
