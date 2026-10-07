"""Send alerts to Slack (if SLACK_WEBHOOK_URL is set) and always save a markdown copy."""
import json
import os
import urllib.request


def format_alerts(result, alerts, source):
    cap = result.get("capacity", {})
    head = f"*Hidden Capacity Agent: Press {result['machine'][1:]}, {result['date']}*"
    if cap:
        head += f"\nNew press: *{cap['verdict']}*. {cap['reason']}"
    head += f"\nData health: {result['health']['status']}  ·  drafted by: {source} (a person decides)"
    body = "\n\n".join(f"*{owner}*\n{text}" for owner, text in alerts.items())
    return head + "\n\n" + body


def send(text, out_path):
    out_path.write_text(text.replace("*", "**") + "\n")
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return "saved (no SLACK_WEBHOOK_URL set)"
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return f"sent to Slack ({r.status})"
