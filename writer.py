"""AI step: Claude turns the computed facts into one alert per owner.

Claude only explains and drafts. It receives finished numbers and is told not to compute new ones.
If no API key is configured (or the call fails), a plain template writes the alerts instead,
so the daily run never breaks.
"""
import json
import os

import anthropic

import rules

MODEL = "claude-opus-5-5"

SYSTEM = """You are the daily capacity analyst for a manufacturing plant. Each morning you receive the
computed results either for one machine running on its own (mode "single") or for a line of machines in
one continuous flow (mode "line"), and you write short alerts for the people who own each problem.

Rules:
- Use ONLY the numbers in the facts JSON. Do not calculate, estimate, or round differently.
- One alert per owner listed in "owners". Each alert: the loss, the likely cause, one concrete floor
  action for tomorrow, and the hours and dollars at stake. 60 words max, plain language, no hype.
- In line mode, loss alerts are about the bottleneck machine only: hours recovered elsewhere add no output.
- The Plant manager alert must state the capacity verdict and the reason. In line mode it must also name
  the bottleneck, the next bottleneck, and the headroom between them.
- If the data health status is WARN, mention the data issue in the alert of the owner it affects.
- You draft; people decide. Do not claim anything has been done.

Output format, exactly (no other text):
### <Owner>
<alert text>
"""


def owners_for(result):
    unit = rules.focus_unit(result)
    owners = []
    for t in unit["top3"]:
        if t["owner"] not in owners:
            owners.append(t["owner"])
    if unit["cycle_time"]["flag"] and "Industrial engineer" not in owners:
        owners.append("Industrial engineer")
    owners.append("Plant manager")
    return owners


def _facts(result):
    unit = rules.focus_unit(result)
    facts = {
        "mode": result["mode"], "date": result["date"], "health": result["health"],
        "focus_machine": {k: unit[k] for k in ["machine", "name", "cycle_time", "metrics", "top3",
                                                "hours_lost", "capacity"]},
        "verdict": result["verdict"],
        "owners": owners_for(result),
    }
    if result["mode"] == "line":
        facts["line"] = {k: result[k] for k in ["steps", "bottleneck", "next_bottleneck",
                                                 "line_capacity", "headroom_parts"]}
    return facts


def _parse(text):
    alerts, owner, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith("### "):
            if owner:
                alerts[owner] = "\n".join(buf).strip()
            owner, buf = line[4:].strip(), []
        elif owner:
            buf.append(line)
    if owner:
        alerts[owner] = "\n".join(buf).strip()
    return alerts


def draft_with_claude(result):
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": "Facts:\n" + json.dumps(_facts(result), indent=1, default=str)}],
        extra_body={"output_config": {"effort": "low"}, "fallbacks": "default"},
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    alerts = _parse("".join(b.text for b in response.content if b.type == "text"))
    if not alerts:
        raise RuntimeError("Claude returned no parsable alerts")
    return alerts


def draft_with_template(result):
    unit = rules.focus_unit(result)
    m, cap, ct, v = unit["metrics"], unit["capacity"], unit["cycle_time"], result["verdict"]
    who = unit["name"]
    alerts = {}
    for t in unit["top3"]:
        where = (f", {t['events_in_window']} of them between {t['window'].replace('-', ' and ')}"
                 if t.get("window") else "")
        line = (f"{who} lost {t['hours']} h yesterday to {t['reason'].lower()} "
                f"({t['events']} events{where}), {t['likely_cause']}. "
                f"Recovering half = {t['parts'] // 2} parts/day = ${t['value_per_day'] // 2}/day.")
        alerts[t["owner"]] = (alerts[t["owner"]] + "\n" + line) if t["owner"] in alerts else line
    if ct["flag"]:
        alerts["Industrial engineer"] = (alerts.get("Industrial engineer", "") + "\n" +
            f"{who}: SAP standard cycle time is {ct['sap_ct_s']} s; the PLC median over {ct['cycles_sampled']} "
            f"cycles is {ct['true_ct_s']} s ({ct['diff_pct']}% gap). SAP understates capacity: "
            f"{cap['sap_capacity']} vs {cap['true_capacity']} parts/day.").strip()

    pm = ""
    if result["mode"] == "line":
        names = {s["machine"]: s["name"] for s in result["steps"]}
        flow = " → ".join(names.values())
        pm = f"Line {flow}: bottleneck is {who} at {result['line_capacity']} parts/day (OEE {m['oee']:.0%}). "
        if result["next_bottleneck"]:
            pm += (f"Next bottleneck is {names[result['next_bottleneck']]}, {result['headroom_parts']} "
                   f"parts/day of headroom above it. ")
    else:
        pm = f"{who} OEE {m['oee']:.0%}, {unit['hours_lost']} h lost yesterday. "
    pm += (f"Output {v['output_now']} vs demand {v['demand']}. New capacity: {v['verdict']}. {v['reason']} "
           f"Loss recovery is worth ${v['recoverable_value_per_year']:,.0f}/yr.")
    alerts["Plant manager"] = pm
    return alerts


def draft_alerts(result, use_ai=True):
    """Returns (alerts dict, source string)."""
    if result.get("halted"):
        issues = "; ".join(f"{i['machine']}: {i['issue']}" for i in result["health"]["issues"] if i["level"] == "FAIL")
        return {"Plant manager": f"{result['halted']} {issues}"}, "rules"
    if use_ai and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        try:
            return draft_with_claude(result), "claude"
        except (anthropic.APIError, RuntimeError) as e:
            print(f"  Claude step failed ({e}); using template alerts.")
    return draft_with_template(result), "template"
