"""AI step: Claude turns the computed facts into one alert per owner.

Claude only explains and drafts. It receives finished numbers and is told not to compute new ones.
If no API key is configured (or the call fails), a plain template writes the alerts instead,
so the daily run never breaks.
"""
import json
import os

import anthropic

MODEL = "claude-opus-5-5"

SYSTEM = """You are the daily capacity analyst for a stamping plant. Each morning you receive the
computed results for the bottleneck press and write short alerts for the people who own each problem.

Rules:
- Use ONLY the numbers in the facts JSON. Do not calculate, estimate, or round differently.
- One alert per owner listed in "owners". Each alert: the loss, the likely cause, one concrete floor
  action for tomorrow, and the hours and dollars at stake. 60 words max, plain language, no hype.
- The Plant manager alert must state the new-press verdict and the reason.
- If the data health status is WARN, mention the data issue in the alert of the owner it affects.
- You draft; people decide. Do not claim anything has been done.

Output format, exactly (no other text):
### <Owner>
<alert text>
"""


def owners_for(result):
    owners = []
    for t in result.get("top3", []):
        if t["owner"] not in owners:
            owners.append(t["owner"])
    if result.get("cycle_time", {}).get("flag") and "Industrial engineer" not in owners:
        owners.append("Industrial engineer")
    owners.append("Plant manager")
    return owners


def _facts(result):
    keep = ["date", "machine", "health", "cycle_time", "metrics", "top3", "hours_lost", "capacity", "halted"]
    return {k: result[k] for k in keep if k in result}


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
    facts = _facts(result)
    facts["owners"] = owners_for(result)
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": "Facts:\n" + json.dumps(facts, indent=1, default=str)}],
        extra_body={"output_config": {"effort": "low"}, "fallbacks": "default"},
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    text = "".join(b.text for b in response.content if b.type == "text")
    alerts = _parse(text)
    if not alerts:
        raise RuntimeError("Claude returned no parsable alerts")
    return alerts


def draft_with_template(result):
    m, cap, ct = result["metrics"], result["capacity"], result["cycle_time"]
    press = f"Press {result['machine'][1:]}"
    alerts = {}
    for t in result["top3"]:
        where = (f", {t['events_in_window']} of them between {t['window'].replace('-', ' and ')}"
                 if t.get("window") else "")
        line = (f"{press} lost {t['hours']} h yesterday to {t['reason'].lower()} "
                f"({t['events']} events{where}), {t['likely_cause']}. "
                f"Recovering half = {t['parts'] // 2} parts/day = ${t['value_per_day'] // 2}/day.")
        alerts[t["owner"]] = (alerts[t["owner"]] + "\n" + line) if t["owner"] in alerts else line
    if ct["flag"]:
        alerts["Industrial engineer"] = (alerts.get("Industrial engineer", "") + "\n" +
            f"SAP standard cycle time is {ct['sap_ct_s']} s; the PLC median over {ct['cycles_sampled']} cycles is "
            f"{ct['true_ct_s']} s ({ct['diff_pct']}% gap). SAP understates capacity: "
            f"{cap['sap_capacity']} vs {cap['true_capacity']} parts/day.").strip()
    alerts["Plant manager"] = (
        f"{press} OEE {m['oee']:.0%}, {result['hours_lost']} h lost yesterday. Output {cap['current_good_output']} "
        f"vs demand {cap['demand']}. New press: {cap['verdict']}. {cap['reason']} "
        f"Worth ${cap['recoverable_value_per_year']:,.0f}/yr against a ${cap['new_press_capex']:,.0f} press.")
    return alerts


def draft_alerts(result, use_ai=True):
    """Returns (alerts dict, source string)."""
    if result.get("halted"):
        issues = "; ".join(i["issue"] for i in result["health"]["issues"])
        return {"Plant manager": f"{result['halted']} Issues: {issues}"}, "rules"
    if use_ai and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        try:
            return draft_with_claude(result), "claude"
        except (anthropic.APIError, RuntimeError) as e:
            print(f"  Claude step failed ({e}); using template alerts.")
    return draft_with_template(result), "template"
