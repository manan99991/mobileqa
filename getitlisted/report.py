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
    "stability":  "Find where this error is caught and fix the bad data feeding it before it turns into a crash.",
}

e = lambda s: html.escape(str(s if s is not None else ""))


def readiness(findings):
    w = {"critical": 25, "high": 10, "medium": 4, "low": 1, "info": 0}
    penalty = sum(w.get(f.get("severity", "low"), 1) for f in findings)
    return max(0, 100 - penalty)


def from_run(run_dir, app_name, platform, url_map=None):
    """Build the report `data` dict from a mobileqa run directory.
    Prefers findings.trusted.json (post trust-gate); falls back to findings.json.
    url_map: {local_screenshot_path -> public_url} filled by push.py."""
    run_dir = Path(run_dir)
    url_map = url_map or {}
    src = run_dir / "findings.trusted.json"
    if not src.exists():
        src = run_dir / "findings.json"
    findings_raw = json.loads(src.read_text()) if src.exists() else []
    summary = {}
    sj = run_dir / "summary.json"
    if sj.exists():
        summary = json.loads(sj.read_text()).get("summary", {})
    device = {}
    dj = run_dir / "device.json"
    if dj.exists():
        device = json.loads(dj.read_text())

    findings = []
    for f in findings_raw:
        if f.get("severity") == "info":
            continue
        ev = f.get("evidence") or {}
        shot = ev.get("screenshot", "")
        findings.append({
            "severity": f.get("severity", "low"),
            "category": f.get("category", ""),
            "title": f.get("title", ""),
            "detail": f.get("detail", ""),
            "screen": f.get("screen_name") or f.get("screen", "—"),
            "repro": f.get("repro", []) or [],
            "screenshot": url_map.get(shot, shot if str(shot).startswith("http") else ""),
            "tip": TIP.get(f.get("category", ""), ""),
            "reproduction": f.get("reproduction", ""),
            "reproduction_ratio": f.get("reproduction_ratio", ""),
            "exception": ev.get("exception", ""),
            "app_frame": ev.get("app_frame", ""),
            "obfuscated": ev.get("obfuscated", False),
        })

    stats = {
        "screens": summary.get("screens", 0),
        "steps": summary.get("steps", 0),
        "duration_s": summary.get("duration_s", 0),
    }
    return {"app_name": app_name, "platform": platform, "device": device,
            "run_at": datetime.date.today().isoformat(),
            "stats": stats, "findings": findings, "screens": [], "coverage": summary}


def build(data):
    app = data.get("app_name", "Your app")
    platform = data.get("platform", "Android")
    ran = data.get("run_at") or datetime.date.today().isoformat()
    stats = data.get("stats", {})
    findings = data.get("findings", [])
    screens = data.get("screens", [])
    device = data.get("device", {})
    devbits = [device.get("model")]
    if device.get("api"):
        devbits.append(f"Android API {device.get('api')}")
    if device.get("width") and device.get("height"):
        devbits.append(f"{device.get('width')}×{device.get('height')}")
    devstr = " · ".join(b for b in devbits if b)

    counts = {k: sum(1 for f in findings if f.get("severity") == k) for k in SEV_ORDER}
    score = readiness(findings)
    sc = "#49d0a0" if score >= 80 else "#f5c26b" if score >= 55 else "#ff7a7a"
    verdict = ("Looks ready — only minor polish left." if score >= 80 else
               "Close, but a few things need fixing before launch." if score >= 55 else
               "Not ready — issues here would hurt real users.")
    mins = round((stats.get("duration_s") or 0) / 60) or "—"

    def tile(label, value):
        return f'<div class="st"><b>{e(value)}</b><span>{e(label)}</span></div>'

    REP = {"confirmed": ("✓ reproduces", "#49d0a0"), "intermittent": ("~ intermittent", "#f5c26b"),
           "single-occurrence": ("seen once", "#95a3b3")}
    def card(f):
        name, col, bg, bd = SEV[f.get("severity", "low")]
        shot = f.get("screenshot")
        shot_html = (f'<a class="shot" href="{e(shot)}" target="_blank" rel="noopener">'
                     f'<img src="{e(shot)}" loading="lazy" alt="screen"></a>') if shot else ""
        repro = ""
        if f.get("repro"):
            repro = '<div class="repro"><b>How to see it:</b><ol>' + "".join(
                f"<li>{e(s)}</li>" for s in f["repro"][:6]) + "</ol></div>"
        rep = ""
        if f.get("reproduction") in REP:
            lab, rc = REP[f["reproduction"]]
            ratio = f.get("reproduction_ratio", "")
            rep = f'<span class="repb" style="color:{rc};border-color:{rc}55">{lab}{(" "+e(ratio)) if ratio and f["reproduction"]!="single-occurrence" else ""}</span>'
        cause = ""
        if f.get("exception") or f.get("app_frame"):
            note = ('<div class="obf">Release build is obfuscated — send your <b>mapping.txt / dSYM</b> and we pinpoint the exact line.</div>'
                    if f.get("obfuscated") else "")
            cause = (f'<div class="cause"><div class="ck">Root cause</div>'
                     f'{f"<div><span>Exception</span><code>{e(f.get("exception"))}</code></div>" if f.get("exception") else ""}'
                     f'{f"<div><span>First app frame</span><code>{e(f.get("app_frame"))}</code></div>" if f.get("app_frame") else ""}'
                     f'{note}</div>')
        return f'''<div class="fc" style="border-color:{bd}">
          <div class="fc-top"><span class="sev" style="background:{bg};color:{col};border:1px solid {bd}">{name}</span>
            {rep}<span class="where">{e(f.get("screen","—"))}</span></div>
          <div class="ft">{e(f.get("title"))}</div>
          <div class="fd">{e(f.get("detail"))}</div>
          {cause}
          {repro}
          {f'<div class="fix"><b>Suggested fix:</b> {e(f.get("tip"))}</div>' if f.get("tip") else ""}
          {shot_html}</div>'''

    findings_sorted = sorted(findings, key=lambda f: SEV_ORDER.index(f.get("severity", "low")))
    findings_html = "".join(card(f) for f in findings_sorted) or \
        '<div class="empty">No functional issues found in this pass. 🎉</div>'

    pills = "".join(f'<span class="cpill" style="color:{SEV[k][1]}">{counts[k]} {SEV[k][0].lower()}</span>'
                    for k in SEV_ORDER if counts[k])

    ev_links = []
    for i, v in enumerate(data.get("video_urls") or []):
        ev_links.append(f'<a class="evl" href="{e(v)}" target="_blank" rel="noopener">▶ Watch the run{f" (part {i+1})" if len(data.get("video_urls"))>1 else ""}</a>')
    if data.get("log_url"):
        ev_links.append(f'<a class="evl" href="{e(data["log_url"])}" target="_blank" rel="noopener">📄 Full device log</a>')
    evidence_html = (f'<div class="card"><div class="sec-title">Evidence</div><div class="evrow">{"".join(ev_links)}</div>'
                     f'<div class="method" style="margin-top:10px">Screen recording of the full session and the complete device log — every finding above is traceable to these.</div></div>') if ev_links else ""

    cov = data.get("coverage") or {}
    perf = cov.get("performance") or {}
    rows = []
    cs = perf.get("cold_start_ms") or []
    if cs:
        rows.append(("Cold start", " · ".join(f"{t/1000:.1f}s" for t in cs)))
    if perf.get("mem_start_kb") and perf.get("mem_end_kb"):
        rows.append(("Memory (PSS)", f'{perf["mem_start_kb"]//1024} MB at launch → {perf["mem_end_kb"]//1024} MB after the full session'))
    # frame jank is not reported: the CI emulator renders in software, so it would blame the app for our machine
    st = perf.get("stress") or []
    if st:
        bad = sum(1 for r in st if r.get("crash") or r.get("anr"))
        rows.append(("Stress test", f'{len(st)} seeded random-input runs · {sum((r.get("events") or 0) for r in st):,} events · '
                     + ("survived all" if not bad else f"{bad} failed")))
    ne = cov.get("network_errors") or {}
    if ne:
        rows.append(("Network errors logged", ", ".join(f"{k} ×{v}" for k, v in ne.items()) + " (could be the test network — informational)"))
    integ = cov.get("integrations") or []
    if integ:
        apps = sorted({i.get("opened", "") for i in integ})
        rows.append(("Hands off to", ", ".join(apps)))
    dec = cov.get("declared") or {}
    if dec.get("permissions"):
        rows.append(("Permissions", ", ".join(p.replace("android.permission.", "") for p in dec["permissions"][:12])))
    if dec.get("deep_links"):
        rows.append(("Deep links", ", ".join(dec["deep_links"][:6])))
    scr = cov.get("screen_list") or []
    cov_html = ""
    if rows or scr:
        cov_html = ('<div class="card"><div class="sec-title">Coverage · performance · integrations</div>'
                    + "".join(f'<div class="kv"><span>{e(k)}</span><b>{e(v)}</b></div>' for k, v in rows)
                    + (f'<details class="scr"><summary>{len(scr)} screens reached</summary><ul>'
                       + "".join(f'<li>{e(x.get("name"))} <i>· {x.get("controls",0)} controls</i></li>' for x in scr) + '</ul></details>' if scr else "")
                    + '</div>')

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
  .repb{{font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px;border:1px solid;letter-spacing:.2px}}
  .cause{{margin-top:11px;background:var(--bg);border:1px solid var(--line2);border-radius:11px;padding:12px 13px}}
  .cause .ck{{font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:.7px;margin-bottom:8px}}
  .cause>div{{display:flex;gap:10px;align-items:baseline;margin-bottom:6px;flex-wrap:wrap}}
  .cause span{{font-size:12px;color:var(--muted);min-width:92px}}
  .cause code{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:var(--blue-soft);word-break:break-all}}
  .obf{{font-size:12.5px;color:var(--muted);margin-top:4px}}.obf b{{color:var(--text)}}
  .shot{{display:block;margin-top:12px;max-width:150px}}.shot img{{width:100%;border-radius:10px;border:1px solid var(--line2)}}
  .empty{{color:var(--faint);font-size:14px}}
  .method{{color:var(--muted);font-size:13.5px}}.method b{{color:var(--text)}}
  .evrow{{display:flex;gap:10px;flex-wrap:wrap}}
  .evl{{display:inline-block;font-size:13.5px;font-weight:600;padding:9px 14px;border-radius:10px;background:var(--card2);border:1px solid var(--line2);color:var(--text);text-decoration:none}}
  .kv{{display:flex;gap:14px;padding:9px 0;border-bottom:1px solid var(--line);font-size:13.5px;flex-wrap:wrap}}.kv span{{color:var(--muted);min-width:130px}}.kv b{{font-weight:600;flex:1}}
  .scr{{margin-top:12px;font-size:13.5px;color:var(--muted)}}.scr summary{{cursor:pointer;color:var(--text);font-weight:600}}.scr ul{{margin:8px 0 0 18px}}.scr i{{color:var(--faint)}}
  .foot{{text-align:center;color:var(--faint);font-size:12px;margin-top:24px}}.foot a{{color:var(--muted)}}
  @media(max-width:480px){{.appname{{font-size:23px}}}}
</style></head><body><div class="wrap">
  <div class="brand"><svg class="mk" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="6" fill="#1D4ED8"/><path d="M6.8 12.4l3.2 3.2L17.4 8.2" stroke="#fff" stroke-width="2.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
    <div><h1>Get It Listed <span>· automated QA report</span></h1></div></div>
  <div class="card">
    <div class="eyebrow">Tested app · {e(platform)}</div>
    <div class="appname">{e(app)}</div>
    <div class="meta">Explored by the Get It Listed engine · {e(ran)}{f" · {e(devstr)}" if devstr else ""}</div>
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
  {cov_html}
  {evidence_html}
  <div class="card"><div class="sec-title">How this was tested</div>
    <div class="method">Our engine installed your build on a clean Android device and worked through it systematically: it measured cold-start time, opened every screen it could reach, tapped every safe control, submitted forms both empty and filled, rotated the phone and sent the app to the background on each new screen, hammered it with seeded random input, and checked every screen against Google's accessibility rules. Every crash was replayed from a fresh install — only crashes that happen again, inside your app's own process, are reported. Purchase, payment, delete-account and sign-out controls are never tapped. <b>This is deep exploratory testing, not a check of your app's intended business logic</b> — a strong first line of defense, not a replacement for your own acceptance tests.</div></div>
  <div class="foot">Get It Listed · <a href="https://getitlisted.app">getitlisted.app</a> · automated QA engine</div>
</div></body></html>'''


if __name__ == "__main__":
    # standalone: python report.py <run_dir> <app_name> [platform] [out.html]
    rd, app = sys.argv[1], sys.argv[2]
    plat = sys.argv[3] if len(sys.argv) > 3 else "Android"
    out = sys.argv[4] if len(sys.argv) > 4 else "qa_report.html"
    Path(out).write_text(build(from_run(rd, app, plat)))
    print("wrote", out)
