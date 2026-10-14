import os
import smtplib
from email.message import EmailMessage
from html import escape

VERDICT_COLORS = {"Excellent": "#1f9d6b", "Good": "#5c9b2e", "Marginal": "#b7791f", "Poor": "#9aa0a6"}


class EmailNotConfigured(Exception):
    pass


def configured():
    return bool(os.getenv("SENDER_EMAIL") and os.getenv("APP_PASSWORD"))


def render(plan):
    loc = plan["location"]
    bortle = f" · Bortle {loc['bortle']}" if loc.get("bortle") else ""
    target = plan["target"]
    rows = []
    for n in plan["nights"]:
        if not n["usable_minutes"]:
            continue
        color = VERDICT_COLORS[n["verdict"]]
        tgt = f"{n['target']['peak_alt']:g}° at {n['target']['peak_time']}" if n["target"] else "—"
        cells = [
            n["label"],
            f"<b style='color:{color}'>{n['score']} · {n['verdict']}</b>",
            f"{n['dark_start']}–{n['dark_end']}",
            n["best_window"],
            f"{n['usable_hours']:g} h",
            f"{n['avg_cloud']}%",
            f"{n['avg_precip'] if n['avg_precip'] is not None else '—'}%",
            f"{n['moon']['phase']} ({n['moon']['illumination']}%)",
            tgt,
        ]
        rows.append("<tr>" + "".join(f"<td style='padding:8px;border-bottom:1px solid #eee'>{c}</td>" for c in cells) + "</tr>")
    head = ["Night", "Score", "Dark", "Best window", "Usable", "Cloud", "Rain", "Moon", target["short"] if target["id"] != "none" else "Target"]
    return f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#1d1f24">
<h2 style="margin:0 0 4px">Clear Skies: {escape(loc['label'])}{bortle}</h2>
<p style="margin:0 0 16px;color:#666">Target: {escape(target['name'])} · limits: cloud ≤ {plan['settings']['max_cloud']}%,
rain ≤ {plan['settings']['max_precip']}%, altitude ≥ {plan['settings']['min_alt']}°, moon ≤ {plan['settings']['moon_tol']}% lit when up</p>
<table cellspacing="0" style="border-collapse:collapse;font-size:14px">
<tr style="background:#f4f5f7">{''.join(f"<th style='padding:8px;text-align:left'>{h}</th>" for h in head)}</tr>
{''.join(rows) or "<tr><td style='padding:8px' colspan='9'>No shootable nights in the forecast.</td></tr>"}
</table></body></html>"""


def send(plan, to=None):
    sender = os.getenv("SENDER_EMAIL")
    password = os.getenv("APP_PASSWORD")
    if not (sender and password):
        raise EmailNotConfigured("Email isn't set up. Add SENDER_EMAIL and APP_PASSWORD to .env (see .env.example).")
    good = sum(1 for n in plan["nights"] if n["usable_minutes"])
    msg = EmailMessage()
    msg["Subject"] = f"Clear Skies: {good} shootable night{'s' if good != 1 else ''} · {plan['location']['label']}"
    msg["From"] = sender
    msg["To"] = to or os.getenv("EMAIL_TO") or sender
    msg.set_content("Please enable HTML to view this message.")
    msg.add_alternative(render(plan), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    return msg["To"]
