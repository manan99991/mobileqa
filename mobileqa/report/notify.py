"""Push results to where the developer already looks: Slack, email, GitHub issues.
All optional, all driven by environment variables so nothing secret lives in app.yaml.

  MOBILEQA_SLACK_WEBHOOK   Slack incoming-webhook URL
  MOBILEQA_EMAIL_TO        recipient; plus SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS
  MOBILEQA_GITHUB_REPO     owner/repo — one issue per new critical/high finding (needs `gh` logged in)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path

import requests

log = logging.getLogger("mobileqa.notify")


def notify(cfg, run_dir: Path, summary: dict, findings: list[dict], report_path: Path, blocked: bool) -> None:
    title = f"{'🔴 RELEASE BLOCKED' if blocked else '🟢 QA passed'} — {cfg.app.name} ({cfg.platform})"
    top = [f for f in findings if f["severity"] in ("critical", "high")][:10]
    lines = [title, f"{summary.get('steps', 0)} actions · {summary.get('screens', 0)} screens · {summary.get('coverage_pct', 0)}% elements · "
             f"{summary.get('critical', 0)} critical / {summary.get('high', 0)} high / {summary.get('medium', 0)} medium"]
    for f in top:
        lines.append(f"• [{f['severity']}] {f['title']} — {f['screen_name']}: {f['detail'][:140]}")
    lines.append(f"Report: {report_path}")
    text = "\n".join(lines)

    hook = os.environ.get("MOBILEQA_SLACK_WEBHOOK")
    if hook:
        try:
            requests.post(hook, json={"text": text}, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("slack notify failed: %s", e)

    to = os.environ.get("MOBILEQA_EMAIL_TO")
    if to and os.environ.get("SMTP_HOST"):
        try:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = title, os.environ.get("SMTP_USER", "mobileqa@localhost"), to
            msg.set_content(text)
            with open(report_path, "rb") as fh:
                msg.add_attachment(fh.read(), maintype="text", subtype="html", filename="report.html")
            with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))) as s:
                s.starttls()
                if os.environ.get("SMTP_USER"):
                    s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
                s.send_message(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("email notify failed: %s", e)

    repo = os.environ.get("MOBILEQA_GITHUB_REPO")
    if repo and shutil.which("gh"):
        seen_path = run_dir.parent / ".issues_filed.json"
        seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
        for f in top:
            if f["key"] in seen:
                continue
            body = (f"**Severity:** {f['severity']}  **Category:** {f['category']}  **Screen:** {f['screen_name']}\n\n"
                    f"{f['detail']}\n\n**Steps to reproduce**\n" + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(f.get("repro", [])))
                    + f"\n\n_Filed automatically by mobileqa run {run_dir.name}. Finding key `{f['key']}`._")
            try:
                out = subprocess.run(["gh", "issue", "create", "-R", repo, "-t", f"[QA] {f['title']} ({f['screen_name']})",
                                      "-b", body, "-l", "bug,mobileqa"], capture_output=True, text=True, timeout=60)
                if out.returncode == 0:
                    seen[f["key"]] = out.stdout.strip()
            except Exception as e:  # noqa: BLE001
                log.warning("gh issue failed: %s", e)
        seen_path.write_text(json.dumps(seen, indent=1))
