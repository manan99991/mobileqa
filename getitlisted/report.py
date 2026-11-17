#!/usr/bin/env python3
"""
Get It Listed — customer-facing QA report.

Consumes a mobileqa run directory (findings.json + summary.json, written by
mobileqa/report/build.py) and renders a polished, non-technical HTML report that
a paying customer reads. Screenshot paths are expected to be already rewritten to
public URLs by push.py before build() is called.

Pure Python, no third-party deps.
"""
from __future__ import annotations
import html, json, sys, datetime
from pathlib import Path

SEV = {
    "critical": ("Critical", "#ff7a7a", "rgba(255,122,122,.12)", "rgba(255,122,122,.32)"),
    "high":     ("High",     "#f5a26b", "rgba(245,162,107,.12)", "rgba(245,162,107,.32)"),
    "medium":   ("Medium",   "#f5c26b", "rgba(245,194,107,.12)", "rgba(245,194,107,.32)"),
    "low":      ("Low",      "#9ab6ff", "rgba(91,140,255,.12)",  "rgba(91,140,255,.32)"),
    "info":     ("Info",     "#95a3b3", "rgba(149,163,179,.12)", "rgba(149,163,179,.3)"),
}
SEV_ORDER = ["critical", "high", "medium", "low", "info"]

# Generic, honest remediation guidance by category (the engine finds the issue;
# this is standard advice for that class, not a claim it diagnosed your code).
TIP = {
    "crash":      "Reproduce with the steps below, then add input validation / error handling so it fails gracefully.",
    "functional": "The control looks active but didn't do its job — wire the handler, or disable it with a reason.",
    "value":      "The number shown doesn't match the expected calculation — check the formula and data binding.",
    "link":       "Fix or remove the broken link so it doesn't dead-end a reviewer.",
    "ui":         "Adjust the layout, contrast, or tap-target size so it's clean on a real phone.",
    "ux":         "Smooth this interaction — users expect feedback here and aren't getting it.",
    "a11y":       "Add an accessible label / content description so screen-reader users (and Play's a11y check) pass.",
    "perf":       "Show a loading state and speed up this screen; a blank frame reads as frozen.",
    "content":    "Replace the placeholder or incorrect copy before a reviewer sees it.",
    "security":   "Review the flagged behavior — it could trip Google Play policy.",
}

e = lambda s: html.escape(str(s if s is not None else ""))


def readiness(findings):
    w = {"critical": 25, "high": 10, "medium": 4, "low": 1, "info": 0}
    penalty = sum(w.get(f.get("severity", "low"), 1) for f in findings)
    return max(0, 100 - penalty)


def from_run(run_dir, app_name, platform, url_map=None):
    """Build the report `data` dict from a mobileqa run directory.
    url_map: {local_screenshot_path -> public_url} filled by push.py."""
    run_dir = Path(run_dir)
    url_map = url_map or {}
    findings_raw = json.loads((run_dir / "findings.json").read_text()) if (run_dir / "findings.json").exists() else []
    summary = {}
    sj = run_dir / "summary.json"
    if sj.exists():
        summary = json.loads(sj.read_text()).get("summary", {})

    findings = []
    for f in findings_raw:
        if f.get("severity") == "info":
            continue
        shot = (f.get("evidence") or {}).get("screenshot", "")
        findings.append({
            "severity": f.get("severity", "low"),
            "category": f.get("category", ""),
            "title": f.get("title", ""),
            "detail": f.get("detail", ""),
            "screen": f.get("screen_name") or f.get("screen", "—"),
            "repro": f.get("repro", []) or [],
            "screenshot": url_map.get(shot, shot if str(shot).startswith("http") else ""),
            "tip": TIP.get(f.get("category", ""), ""),
        })

    stats = {
        "screens": summary.get("screens", 0),
        "steps": summary.get("steps", 0),
        "duration_s": summary.get("duration_s", 0),
    }
    return {"app_name": app_name, "platform": platform,
            "run_at": datetime.date.today().isoformat(),
            "stats": stats, "findings": findings, "screens": []}


def build(data):
    app = data.get("app_name", "Your app")
    platform = data.get("platform", "Android")
    ran = data.get("run_at") or datetime.date.today().isoformat()
    stats = data.get("stats", {})
    findings = data.get("findings", [])
    screens = data.get("screens", [])

    counts = {k: sum(1 for f in findings if f.get("severity") == k) for k in SEV_ORDER}
    score = readiness(findings)
    sc = "#49d0a0" if score >= 80 else "#f5c26b" if score >= 55 else "#ff7a7a"
    verdict = ("Looks ready — only minor polish left." if score >= 80 else
               "Close, but a few things need fixing before launch." if score >= 55 else
               "Not ready — issues here would hurt real users.")
    mins = round((stats.get("duration_s") or 0) / 60) or "—"

    def tile(label, value):
        return f'<div class="st"><b>{e(value)}</b><span>{e(label)}</span></div>'

    def card(f):
        name, col, bg, bd = SEV[f.get("severity", "low")]
        shot = f.get("screenshot")
        shot_html = (f'<a class="shot" href="{e(shot)}" target="_blank" rel="noopener">'
                     f'<img src="{e(shot)}" loading="lazy" alt="screen"></a>') if shot else ""
        repro = ""
        if f.get("repro"):
            repro = '<div class="repro"><b>How to see it:</b><ol>' + "".join(
                f"<li>{e(s)}</li>" for s in f["repro"][:6]) + "</ol></div>"
        return f'''<div class="fc" style="border-color:{bd}">
          <div class="fc-top"><span class="sev" style="background:{bg};color:{col};border:1px solid {bd}">{name}</span>
            <span class="where">{e(f.get("screen","—"))}</span></div>
          <div class="ft">{e(f.get("title"))}</div>
          <div class="fd">{e(f.get("detail"))}</div>
          {repro}
          {f'<div class="fix"><b>Suggested fix:</b> {e(f.get("tip"))}</div>' if f.get("tip") else ""}
          {shot_html}</div>'''

    findings_sorted = sorted(findings, key=lambda f: SEV_ORDER.index(f.get("severity", "low")))
    findings_html = "".join(card(f) for f in findings_sorted) or \
        '<div class="empty">No functional issues found in this pass. 🎉</div>'

    pills = "".join(f'<span class="cpill" style="color:{SEV[k][1]}">{counts[k]} {SEV[k][0].lower()}</span>'
                    for k in SEV_ORDER if counts[k])

    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{e(app)} — QA Report · Get It Listed</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700;12..96,800&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{{--bg:#0e1116;--card:#161b22;--card2:#1c232d;--line:#263040;--line2:#313d4e;--text:#eef3f8;--muted:#95a3b3;--faint:#6b7889;--green:#49d0a0;--disp:"Bricolage Grotesque",sans-serif;--sans:"IBM Plex Sans",-apple-system,sans-serif;}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:var(--sans);background:radial-gradient(1100px 500px at 50% -8%,rgba(91,140,255,.10),transparent 60%),var(--bg);color:var(--text);line-height:1.55;-webkit-font-smoothing:antialiased;padding:24px 16px 80px}}
  .wrap{{max-width:720px;margin:0 auto}} .mk{{width:34px;height:34px}}
  .brand{{display:flex;align-items:center;gap:11px;margin-bottom:20px}}
  .brand h1{{font-family:var(--disp);font-size:18px;font-weight:700}}.brand span{{color:var(--muted);font-size:13px;font-weight:500}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;margin-bottom:16px}}
  .eyebrow{{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:7px}}
  .appname{{font-family:var(--disp);font-size:27px;font-weight:800;letter-spacing:-.6px}}
  .meta{{color:var(--muted);font-size:13.5px;margin-top:6px}}
  .score-wrap{{display:flex;align-items:center;gap:20px;margin:18px 0 6px;flex-wrap:wrap}}
  .ring{{width:96px;height:96px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;background:conic-gradient({sc} {score}%,var(--card2) 0)}}
  .ring .inner{{width:78px;height:78px;border-radius:50%;background:var(--card);display:flex;flex-direction:column;align-items:center;justify-content:center}}
  .ring b{{font-family:var(--disp);font-size:26px;font-weight:800;color:{sc};line-height:1}}.ring span{{font-size:10px;color:var(--faint);margin-top:2px}}
  .verdict b{{font-family:var(--disp);font-size:18px;font-weight:700;display:block;margin-bottom:5px}}.verdict p{{color:var(--muted);font-size:14.5px}}
  .cpills{{display:flex;gap:9px;flex-wrap:wrap;margin-top:10px}}.cpill{{font-size:12.5px;font-weight:700}}
  .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:10px}}
  .st{{background:var(--card2);border:1px solid var(--line);border-radius:13px;padding:13px 14px}}
  .st b{{font-family:var(--disp);font-size:24px;font-weight:800;display:block;line-height:1;letter-spacing:-.5px}}.st span{{font-size:12px;color:var(--muted);display:block;margin-top:5px}}
  .sec-title{{font-size:12px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:.8px;margin-bottom:15px}}
  .fc{{border:1px solid var(--line2);border-radius:14px;padding:16px;margin-bottom:12px;background:var(--card2)}}
  .fc-top{{display:flex;align-items:center;gap:10px;margin-bottom:9px;flex-wrap:wrap}}
  .sev{{font-size:11px;font-weight:800;padding:3px 10px;border-radius:999px;text-transform:uppercase;letter-spacing:.4px}}
  .where{{font-size:12.5px;color:var(--faint);font-weight:500}}
  .ft{{font-family:var(--disp);font-weight:700;font-size:16px;margin-bottom:5px}}.fd{{color:var(--muted);font-size:14.5px}}
  .repro{{margin-top:10px;font-size:13.5px;color:var(--muted)}}.repro b{{color:var(--text)}}.repro ol{{margin:6px 0 0 18px}}.repro li{{margin-bottom:3px}}
  .fix{{margin-top:10px;font-size:13.5px;color:var(--text);background:rgba(73,208,160,.08);border:1px solid rgba(73,208,160,.22);border-radius:10px;padding:10px 12px}}.fix b{{color:var(--green)}}
  .shot{{display:block;margin-top:12px;max-width:150px}}.shot img{{width:100%;border-radius:10px;border:1px solid var(--line2)}}
  .empty{{color:var(--faint);font-size:14px}}
  .method{{color:var(--muted);font-size:13.5px}}.method b{{color:var(--text)}}
  .foot{{text-align:center;color:var(--faint);font-size:12px;margin-top:24px}}.foot a{{color:var(--muted)}}
  @media(max-width:480px){{.appname{{font-size:23px}}}}
</style></head><body><div class="wrap">
  <div class="brand"><svg class="mk" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="6" fill="#1D4ED8"/><path d="M6.8 12.4l3.2 3.2L17.4 8.2" stroke="#fff" stroke-width="2.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
    <div><h1>Get It Listed <span>· automated QA report</span></h1></div></div>
  <div class="card">
    <div class="eyebrow">Tested app · {e(platform)}</div>
    <div class="appname">{e(app)}</div>
    <div class="meta">Explored by the Get It Listed engine · {e(ran)}</div>
    <div class="score-wrap">
      <div class="ring"><div class="inner"><b>{score}</b><span>READY</span></div></div>
      <div class="verdict"><b>{e(verdict)}</b><p>Automated end-to-end exploration on a real Android environment. The score weighs issues by how much they'd hurt a real user.</p>
        <div class="cpills">{pills or '<span class="cpill" style="color:var(--green)">no issues</span>'}</div></div></div></div>
  <div class="card"><div class="sec-title">What the engine did</div><div class="stats">
      {tile("screens explored", stats.get("screens", 0))}
      {tile("actions taken", stats.get("steps", 0))}
      {tile("minutes", mins)}
      {tile("issues found", len(findings))}</div></div>
  <div class="card"><div class="sec-title">Findings{f' · {len(findings)}' if findings else ''}</div>{findings_html}</div>
  <div class="card"><div class="sec-title">How this was tested</div>
    <div class="method">An automated agent installed your build on a real Android environment and explored it like a person — tapping every control, opening every screen, filling forms with normal and hostile input, and watching for crashes, dead buttons, blank results, broken links, slow screens, accessibility gaps and Google Play policy red-flags. <b>This is deep exploratory testing, not a check of your app's intended business logic</b> — a strong first line of defense, not a replacement for your own acceptance tests.</div></div>
  <div class="foot">Get It Listed · <a href="https://getitlisted.app">getitlisted.app</a> · automated QA engine</div>
</div></body></html>'''


if __name__ == "__main__":
    # standalone: python report.py <run_dir> <app_name> [platform] [out.html]
    rd, app = sys.argv[1], sys.argv[2]
    plat = sys.argv[3] if len(sys.argv) > 3 else "Android"
    out = sys.argv[4] if len(sys.argv) > 4 else "qa_report.html"
    Path(out).write_text(build(from_run(rd, app, plat)))
    print("wrote", out)
