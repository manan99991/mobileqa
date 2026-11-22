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
    summary = json.loads((RUN_DIR / "summary.json").read_text()).get("summary", {}) if (RUN_DIR / "summary.json").exists() else {}
    base = f"{APP_KEY}/{RUN_ID}"

    # 1) evidence screenshots: box the exact control(s) in red, shrink for the web, upload
    url_map = {}
    for n, f in enumerate(findings):
        ev = f.get("evidence") or {}
        for key in ("screenshot", "after_screenshot"):
            sp = ev.get(key)
            p = report._resolve(RUN_DIR, sp)
            if not p:
                continue
            out = web_image(p, RUN_DIR / f"ev_{n}_{key}.jpg", ev.get("elements") if key == "screenshot" else None)
            url_map[f"{n}:{key}"] = upload(out, f"{base}/{out.name}")
    print(f"uploaded {len(url_map)} evidence screenshots")

    data = report.from_run(RUN_DIR, APP_NAME, PLATFORM, url_map)

    # 2) every screen the engine reached (gallery)
    for k, sc in enumerate((summary.get("screen_list") or [])[:40]):
        p = report._resolve(RUN_DIR, sc.get("shot"))
        if p:
            out = web_image(p, RUN_DIR / f"screen_{k:02d}.jpg", None, width=360)
            data["screens"].append({"name": sc.get("name", ""), "url": upload(out, f"{base}/{out.name}")})

    # 3) app icon
    for ic in ("app_icon.png", "app_icon.webp"):
        if (RUN_DIR / ic).exists():
            data["icon_url"] = upload(RUN_DIR / ic, f"{base}/{ic}")
            break

    # 4) session video + a "jump to this moment" link per finding
    vdir = Path(os.environ.get("GIL_VIDEO_DIR") or RUN_DIR / "video")
    segs = []
    if (vdir / "index.txt").exists():
        for line in (vdir / "index.txt").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and (vdir / parts[0]).exists() and (vdir / parts[0]).stat().st_size > 10_000:
                segs.append((parts[0], float(parts[1])))
    # upload only the parts that show a bug (plus the opening part) to keep storage small
    def seg_for(t):
        c = [nm for nm, st in segs if st <= t]
        return c[-1] if c else None
    needed = {segs[0][0]} if segs else set()
    for f in data["findings"]:
        if f.get("t"):
            nm = seg_for(f["t"])
            if nm:
                needed.add(nm)
    seg_url = {}
    for name, _ in segs:
        if name in needed and len(seg_url) < 12:
            seg_url[name] = upload(vdir / name, f"{base}/{name}")
    data["video_urls"] = [seg_url[nm] for nm, _ in segs if nm in seg_url]
    for f in data["findings"]:
        t = f.get("t")
        if not t:
            continue
        cands = [(nm, st) for nm, st in segs if st <= t and nm in seg_url]
        if cands:
            nm, st = cands[-1]
            f["video_at"] = f"{seg_url[nm]}#t={max(0, int(t - st) - 3)}"
    print(f"uploaded {len(seg_url)} video segment(s)")

    # 5) full device log
    if (RUN_DIR / "logcat.txt").exists():
        data["log_url"] = upload(RUN_DIR / "logcat.txt", f"{base}/logcat.txt")

    # 6) render + upload the report, then register it on the dashboard
    html = report.build(data)
    rpt_path = RUN_DIR / "qa_report.html"
    rpt_path.write_text(html, encoding="utf-8")
    upload(rpt_path, f"{base}/qa_report.html")
    # Supabase serves .html as plain text, so customers open it through our viewer page
    report_url = f"https://getitlisted.app/report/?r={base}"
    sev = [f["severity"] for f in data["findings"]]
    row = {
        "app_key": APP_KEY, "platform": PLATFORM, "run_id": RUN_ID,
        "score": report.readiness(data["findings"]),
        "critical": sev.count("critical"), "high": sev.count("high"),
        "medium": sev.count("medium"), "low": sev.count("low"),
        "issues": len(data["findings"]), "report_url": report_url, "status": "ready",
    }
    call("report", row=row)
    print(f"published report: score {row['score']}, {row['issues']} issues")


def web_image(src: Path, dst: Path, elements=None, width=720) -> Path:
    """Draw numbered red boxes around the given controls, resize for the web, save as JPEG."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return src
    try:
        im = Image.open(src).convert("RGB")
        if elements:
            d = ImageDraw.Draw(im)
            lw = max(4, im.width // 180)
            for k, el in enumerate(elements, 1):
                b = el.get("bounds")
                if not b:
                    continue
                x1, y1, x2, y2 = b
                pad = lw * 2
                d.rounded_rectangle([x1 - pad, y1 - pad, x2 + pad, y2 + pad], radius=lw * 3, outline=(229, 51, 111), width=lw)
                r = lw * 5
                cx, cy = max(r, x1 - pad), max(r, y1 - pad)
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(229, 51, 111))
                try:
                    d.text((cx, cy), str(k), fill="white", anchor="mm", font_size=int(r * 1.3))
                except TypeError:
                    d.text((cx - r // 3, cy - r // 2), str(k), fill="white")
        if im.width > width:
            im = im.resize((width, int(im.height * width / im.width)))
        im.save(dst, "JPEG", quality=82)
        return dst
    except Exception as ex:
        print(f"  ! image prep failed: {ex}", file=sys.stderr)
        return src


if __name__ == "__main__":
    main()
