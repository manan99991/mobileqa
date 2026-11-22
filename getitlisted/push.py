#!/usr/bin/env python3
"""
Get It Listed — publish a QA run to the customer dashboard (Supabase).

Reads a finished mobileqa run directory, uploads the screenshots + the rendered
customer report to Supabase Storage, and inserts a row in the `reports` table so
the report shows on the buyer's status page. Pure stdlib (urllib).

Env:
  (auth: GitHub OIDC token through engine_api — no stored keys)
  GIL_APP_KEY            the client's app_key (matches getitlisted clients.app_key)
  GIL_APP_NAME           display name
  GIL_PLATFORM           Android | iOS   (default Android)
  GIL_RUN_DIR            path to reports/<run_id>
"""
from __future__ import annotations
import json, mimetypes, os, sys, time, urllib.request, urllib.error
from pathlib import Path
import report  # local module

from engine_api import call, put_file
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
    """Upload a file to the public reports bucket via a one-time signed link; return its URL."""
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    d = call("sign_upload", path=dest)
    try:
        put_file(d["url"], path.read_bytes(), ctype)
    except Exception as ex:
        print(f"  ! upload failed: {ex}", file=sys.stderr)
    return d["public_url"]


def main():
    # Honor the trust gate: if the run was inconclusive (bad environment, app never
    # ran), publish NOTHING — we never show a customer bugs from a broken run.
    meta = json.loads((RUN_DIR / "run_meta.json").read_text()) if (RUN_DIR / "run_meta.json").exists() else {}
    if meta.get("status") == "inconclusive":
        print("run inconclusive — nothing published:", meta.get("reason", ""))
        return

    src = RUN_DIR / "findings.trusted.json"
    if not src.exists():
        src = RUN_DIR / "findings.json"
    findings = json.loads(src.read_text()) if src.exists() else []

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

    # 1b) upload the run evidence: full logs + screen-recording video
    log_url = ""
    if (RUN_DIR / "logcat.txt").exists():
        log_url = upload(RUN_DIR / "logcat.txt", f"{APP_KEY}/{RUN_ID}/logcat.txt")
    video_urls = []
    vdir = Path(os.environ.get("GIL_VIDEO_DIR") or RUN_DIR / "video")
    vids = sorted(vdir.glob("*.mp4")) if vdir.exists() else []
    for v in vids[:4]:
        video_urls.append(upload(v, f"{APP_KEY}/{RUN_ID}/{v.name}"))
    print(f"uploaded {len(video_urls)} video segment(s), log: {'yes' if log_url else 'no'}")

    # 2) render the customer report with public screenshot URLs, upload it
    data = report.from_run(RUN_DIR, APP_NAME, PLATFORM, url_map)
    data["log_url"] = log_url
    data["video_urls"] = video_urls
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
    call("report", row=row)
    print(f"published report for {APP_KEY}: score {row['score']}, {row['issues']} issues -> {report_url}")


if __name__ == "__main__":
    main()
