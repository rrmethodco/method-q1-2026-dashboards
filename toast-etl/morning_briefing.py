"""Morning briefing email — agentic delivery layer.

Pulls the latest LLM-generated per-outlet snapshots written by
reporting_agent.py + the reconciliation report from
cross_source_reconciler.py and sends Ross a single HTML email digest.

Uses Gmail SMTP via app password (auth gate documented below). Slack
delivery is deferred until Method's Slack admin approves the bot install
(per Ross 2026-05-05); this email path is the agentic delivery
mechanism in the meantime.

Environment:
  GMAIL_USER       — sender (typically rr@methodco.com)
  GMAIL_APP_PASS   — Gmail app password (NOT the account password)
                     Generate at https://myaccount.google.com/apppasswords
                     after enabling 2FA. 16 chars, no spaces.
  BRIEFING_TO      — recipient (default: rr@methodco.com)
  BRIEFING_CC      — optional CC list, comma-separated

If GMAIL_USER or GMAIL_APP_PASS is missing, the script writes the
digest HTML to data/_snapshots/_morning_briefing.html and exits 0
(graceful degradation — the digest is still committed to the repo).
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAP_DIR = DATA_DIR / "_snapshots"
RECON_PATH = DATA_DIR / "_validation" / "_reconciliation.json"
DIGEST_PATH = SNAP_DIR / "_morning_briefing.html"

# Method Co brand palette (from CLAUDE.md global instructions)
BRAND = {
    "white": "#FFFFFF",
    "black": "#231F20",
    "seafoam": "#BCC8BE",
    "grey": "#E2DEDE",
    "tan": "#D8CFC4",
    "slate": "#212931",
    "royal": "#405766",
    "turquoise": "#2BA6BB",  # primary accent
}
FONT_STACK = "'Gotham','Montserrat','Helvetica Neue',Arial,sans-serif"


def _fmt_money(v: float | None) -> str:
    if v is None: return "—"
    if abs(v) >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:     return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None: return "—"
    return f"{v:+.1f}%"


def _delta_pct(cur: float | None, prior: float | None) -> float | None:
    if cur is None or prior is None or prior == 0: return None
    return (cur - prior) / prior * 100


def load_latest_snapshots() -> list[dict]:
    """Load all snapshots from this morning's run (the latest week_end)."""
    if not SNAP_DIR.exists(): return []
    snaps = []
    for f in sorted(SNAP_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            snaps.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    if not snaps: return []
    # Filter to the most recent week_end (in case older snapshots linger)
    latest_we = max(s.get("week_end", "") for s in snaps)
    return [s for s in snaps if s.get("week_end") == latest_we]


def load_reconciliation() -> dict | None:
    if not RECON_PATH.exists(): return None
    try:
        return json.loads(RECON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def render_outlet_card(snap: dict) -> str:
    """One outlet's card in the digest. Concise — operator format."""
    b = snap.get("briefing") or {}
    m = (snap.get("metrics") or {}).get("current_week") or {}
    pw = (snap.get("metrics") or {}).get("prior_week") or {}
    yoy = (snap.get("metrics") or {}).get("year_over_year") or {}

    wow = _delta_pct(m.get("net_sales"), pw.get("net_sales"))
    yoy_d = _delta_pct(m.get("net_sales"), yoy.get("net_sales"))

    headline = b.get("headline") or "No headline available."
    wins = b.get("wins") or []
    concerns = b.get("concerns") or []
    actions = b.get("action_items") or []
    outlook = b.get("outlook") or ""

    def lis(items, key):
        if not items: return "<em style='color:#999;'>None.</em>"
        return "<ul style='margin:4px 0 0 0;padding-left:18px;'>" + "".join(
            f"<li style='margin:2px 0;'>{i.get(key, '') if isinstance(i, dict) else i}</li>"
            for i in items
        ) + "</ul>"

    return f"""
    <div style="border-left:4px solid {BRAND['turquoise']};padding:14px 18px;margin:0 0 18px 0;background:{BRAND['white']};border-radius:4px;border:1px solid {BRAND['grey']};">
      <div style="font-family:{FONT_STACK};font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:{BRAND['royal']};margin-bottom:4px;">
        {snap.get('property') or ''}
      </div>
      <div style="font-family:{FONT_STACK};font-size:18px;font-weight:600;color:{BRAND['black']};margin-bottom:4px;">
        {snap.get('outlet_name') or snap.get('outlet_id')}
      </div>
      <div style="font-family:{FONT_STACK};font-size:14px;color:{BRAND['slate']};margin-bottom:10px;">
        {headline}
      </div>
      <table style="width:100%;font-family:{FONT_STACK};font-size:12px;color:{BRAND['slate']};margin-bottom:12px;border-collapse:collapse;">
        <tr>
          <td style="padding:3px 8px 3px 0;color:{BRAND['royal']};">Net Sales</td>
          <td style="padding:3px 0;font-weight:600;">{_fmt_money(m.get('net_sales'))}</td>
          <td style="padding:3px 8px;color:{BRAND['royal']};">WoW</td>
          <td style="padding:3px 0;">{_fmt_pct(wow)}</td>
          <td style="padding:3px 8px;color:{BRAND['royal']};">YoY</td>
          <td style="padding:3px 0;">{_fmt_pct(yoy_d)}</td>
        </tr>
        <tr>
          <td style="padding:3px 8px 3px 0;color:{BRAND['royal']};">Covers</td>
          <td style="padding:3px 0;">{m.get('guests', '—'):,}</td>
          <td style="padding:3px 8px;color:{BRAND['royal']};">Avg/Gst</td>
          <td style="padding:3px 0;">{_fmt_money(m.get('avg_guest_spend'))}</td>
          <td style="padding:3px 8px;color:{BRAND['royal']};">Labor%</td>
          <td style="padding:3px 0;">{m.get('labor_pct_of_sales', 0):.1f}%</td>
        </tr>
      </table>
      <div style="font-family:{FONT_STACK};font-size:13px;color:{BRAND['black']};margin-bottom:10px;">
        <strong style="color:#2A8245;">Wins.</strong>{lis(wins, 'text')}
      </div>
      <div style="font-family:{FONT_STACK};font-size:13px;color:{BRAND['black']};margin-bottom:10px;">
        <strong style="color:#A03A2C;">Concerns.</strong>{lis(concerns, 'text')}
      </div>
      <div style="font-family:{FONT_STACK};font-size:13px;color:{BRAND['black']};margin-bottom:6px;">
        <strong style="color:{BRAND['turquoise']};">Action items.</strong>
        {''.join(f"<div style='margin:4px 0 0 18px;'><b>{a.get('action', '')}</b> — {a.get('owner', '—')} · {a.get('by_when', '')} · <em>{a.get('rationale', '')}</em></div>" for a in actions) if actions else "<em style='color:#999;'>None.</em>"}
      </div>
      <div style="font-family:{FONT_STACK};font-size:12px;color:{BRAND['royal']};font-style:italic;margin-top:8px;">
        Outlook: {outlook}
      </div>
    </div>
    """


def render_recon_block(recon: dict | None) -> str:
    if not recon:
        return ""
    s = recon.get("summary") or {}
    n_alert = s.get("checks_alert") or 0
    n_ok = s.get("checks_ok") or 0
    color = "#A03A2C" if n_alert else "#2A8245"
    label = "RECONCILIATION ALERTS" if n_alert else "ALL RECONCILED"

    rows = []
    for o in recon.get("outlets", []):
        for c in o.get("checks", []):
            if c.get("status") == "alert":
                rows.append(f"<tr><td style='padding:4px 8px;border-top:1px solid {BRAND['grey']};'>"
                            f"<strong>{o['outlet']}</strong></td>"
                            f"<td style='padding:4px 8px;border-top:1px solid {BRAND['grey']};'>"
                            f"{c['kind']}</td>"
                            f"<td style='padding:4px 8px;border-top:1px solid {BRAND['grey']};'>"
                            f"{c.get('detail', '')}</td></tr>")
    alert_table = ""
    if rows:
        alert_table = (f"<table style='width:100%;font-family:{FONT_STACK};font-size:12px;"
                       f"color:{BRAND['slate']};margin-top:8px;border-collapse:collapse;'>"
                       f"<thead><tr><th style='text-align:left;padding:4px 8px;color:{BRAND['royal']};'>Outlet</th>"
                       f"<th style='text-align:left;padding:4px 8px;color:{BRAND['royal']};'>Check</th>"
                       f"<th style='text-align:left;padding:4px 8px;color:{BRAND['royal']};'>Detail</th></tr></thead>"
                       f"<tbody>{''.join(rows)}</tbody></table>")

    return f"""
    <div style="border-left:4px solid {color};padding:14px 18px;margin:0 0 18px 0;background:{BRAND['white']};border:1px solid {BRAND['grey']};border-radius:4px;">
      <div style="font-family:{FONT_STACK};font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:{color};font-weight:600;margin-bottom:4px;">
        {label}
      </div>
      <div style="font-family:{FONT_STACK};font-size:13px;color:{BRAND['slate']};">
        {n_ok} checks OK · {n_alert} alerts across {(s.get('outlets_reconciled') or 0)} outlets · 30-day lookback
      </div>
      {alert_table}
    </div>
    """


def render_html(snaps: list[dict], recon: dict | None) -> str:
    today = date.today()
    we = snaps[0].get("week_end") if snaps else "—"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Method Co Morning Briefing — {today.isoformat()}</title></head>
<body style="margin:0;padding:24px;background:{BRAND['grey']};font-family:{FONT_STACK};">
  <div style="max-width:760px;margin:0 auto;">
    <div style="text-align:center;margin-bottom:24px;">
      <div style="font-family:{FONT_STACK};font-size:11px;letter-spacing:0.22em;text-transform:uppercase;color:{BRAND['royal']};">
        Method Co · F&amp;B Morning Briefing
      </div>
      <div style="font-family:{FONT_STACK};font-size:24px;font-weight:600;color:{BRAND['black']};margin-top:6px;">
        Week ending {we}
      </div>
      <div style="font-family:{FONT_STACK};font-size:12px;color:{BRAND['slate']};margin-top:4px;">
        Generated by reporting agent (Sonnet) · {today.isoformat()}
      </div>
    </div>
    {render_recon_block(recon)}
    {''.join(render_outlet_card(s) for s in sorted(snaps, key=lambda x: -(x.get('metrics', {}).get('current_week', {}).get('net_sales', 0))))}
    <div style="text-align:center;margin-top:24px;font-family:{FONT_STACK};font-size:11px;color:{BRAND['royal']};">
      Method Co · Trustworthy Reporting Engine · Phase A.2<br>
      <a href="https://rrmethodco.github.io/method-q1-2026-dashboards/" style="color:{BRAND['turquoise']};text-decoration:none;">Open dashboard</a>
    </div>
  </div>
</body></html>"""


def send_email(html: str, recipient: str, cc: list[str] | None = None) -> bool:
    """Send via Gmail SMTP. Returns True on success, False on auth/connection failure."""
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASS")
    if not user or not pwd:
        sys.stderr.write("GMAIL_USER or GMAIL_APP_PASS not set — skipping send "
                         "(digest still committed to data/_snapshots/_morning_briefing.html)\n")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Method Co Morning Briefing — {date.today().isoformat()}"
    msg["From"] = user
    msg["To"] = recipient
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.attach(MIMEText("Open in an HTML-capable client. (Plain-text fallback "
                        "not yet implemented.)", "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(user, pwd)
            recipients = [recipient] + (cc or [])
            smtp.sendmail(user, recipients, msg.as_string())
        sys.stdout.write(f"sent morning briefing to {recipient}"
                         + (f" (cc: {', '.join(cc)})" if cc else "") + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"SMTP send failed: {e}\n")
        return False


def main() -> int:
    snaps = load_latest_snapshots()
    if not snaps:
        sys.stdout.write("no snapshots in data/_snapshots/ — nothing to brief\n")
        return 0
    recon = load_reconciliation()
    html = render_html(snaps, recon)

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_PATH.write_text(html, encoding="utf-8")
    sys.stdout.write(f"wrote digest -> {DIGEST_PATH} ({len(snaps)} outlets briefed)\n")

    recipient = os.environ.get("BRIEFING_TO", "rr@methodco.com")
    cc_raw = os.environ.get("BRIEFING_CC", "")
    cc = [e.strip() for e in cc_raw.split(",") if e.strip()] or None

    sent = send_email(html, recipient, cc)
    return 0 if sent or not os.environ.get("GMAIL_USER") else 1


if __name__ == "__main__":
    raise SystemExit(main())
