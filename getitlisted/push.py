#!/usr/bin/env python3
"""
Get It Listed — publish a QA run to the customer dashboard (Supabase).

Reads a finished mobileqa run directory, uploads the screenshots + the rendered
customer report to Supabase Storage, and inserts a row in the `reports` table so
the report shows on the buyer's status page. Pure stdlib (urllib).

Env:
  SB_URL                 https://<ref>.supabase.co
  SB_SERVICE_KEY         service-role key (GitHub secret; never public)
  GIL_APP_KEY            the client's app_key (matches getitlisted clients.app_key)
  GIL_APP_NAME           display name
  GIL_PLATFORM           Android | iOS   (default Android)
  GIL_RUN_DIR            path to reports/<run_id>
"""
from __future__ import annotations
import json, mimetypes, os, sys, time, urllib.request, urllib.error
from pathlib import Path
import report  # local module

SB_URL = os.environ["SB_URL"].rstrip("/")
KEY = os.environ["SB_SERVICE_KEY"]
APP_KEY = os.environ["GIL_APP_KEY"]
APP_NAME = os.environ.get("GIL_APP_NAME", APP_KEY)
PLATFORM = os.environ.get("GIL_PLATFORM", "Android")
RUN_DIR = Path(os.environ["GIL_RUN_DIR"])
BUCKET = "reports"
RUN_ID = RUN_DIR.name or str(int(time.time()))


def _req(method, url, data=None, headers=None):
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read()


def upload(path: Path, dest: str) -> str:
    """Upload a file to Storage, return its public URL."""
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    url = f"{SB_URL}/storage/v1/object/{BUCKET}/{dest}"
    status, body = _req("POST", url, data=path.read_bytes(), headers={
        "apikey": KEY, "Authorization": f"Bearer {KEY}",
        "Content-Type": ctype, "x-upsert": "true"})
    if status not in (200, 201):
        print(f"  ! upload {dest} -> {status} {body[:120]!r}", file=sys.stderr)
    return f"{SB_URL}/storage/v1/object/public/{BUCKET}/{dest}"


def main():
    findings = json.loads((RUN_DIR / "findings.json").read_text()) if (RUN_DIR / "findings.json").exists() else []

    # 1) upload every referenced screenshot, build local-path -> public-url map
    url_map = {}
    shots = {(f.get("evidence") or {}).get("screenshot") for f in findings}
    shots.discard(None); shots.discard("")
    for sp in shots:
        p = Path(sp)
        if not p.is_absolute():
            p = RUN_DIR / sp
        if p.exists():
            url_map[sp] = upload(p, f"{APP_KEY}/{RUN_ID}/{p.name}")
    print(f"uploaded {len(url_map)} screenshots")

    # 2) render the customer report with public screenshot URLs, upload it
    data = report.from_run(RUN_DIR, APP_NAME, PLATFORM, url_map)
    html = report.build(data)
    rpt_path = RUN_DIR / "qa_report.html"
    rpt_path.write_text(html, encoding="utf-8")
    report_url = upload(rpt_path, f"{APP_KEY}/{RUN_ID}/qa_report.html")

    # 3) counts + score, then insert the reports row
    sev = [f.get("severity") for f in findings]
    row = {
        "app_key": APP_KEY, "platform": PLATFORM, "run_id": RUN_ID,
        "score": report.readiness([f for f in data["findings"]]),
        "critical": sev.count("critical"), "high": sev.count("high"),
        "medium": sev.count("medium"), "low": sev.count("low"),
        "issues": len(data["findings"]), "report_url": report_url, "status": "ready",
    }
    status, body = _req("POST", f"{SB_URL}/rest/v1/reports", data=json.dumps(row).encode(),
                        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
                                 "Content-Type": "application/json", "Prefer": "return=minimal"})
    if status not in (200, 201, 204):
        print(f"! insert reports row -> {status} {body[:200]!r}", file=sys.stderr)
        sys.exit(1)
    print(f"published report for {APP_KEY}: score {row['score']}, {row['issues']} issues -> {report_url}")


if __name__ == "__main__":
    main()
