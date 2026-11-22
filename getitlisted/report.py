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
import html, json, re, sys, datetime
from pathlib import Path

SEV = {
    "critical": ("Critical", "#D61F5B", "rgba(229,51,111,.10)", "rgba(229,51,111,.35)"),
    "high":     ("High",     "#C2560F", "rgba(240,122,43,.10)", "rgba(240,122,43,.35)"),
    "medium":   ("Medium",   "#5B3DF5", "rgba(91,61,245,.08)", "rgba(91,61,245,.30)"),
    "low":      ("Low",      "#5E5A80", "rgba(94,90,128,.08)", "rgba(94,90,128,.25)"),
    "info":     ("Info",     "#8A86A8", "rgba(138,134,168,.08)", "rgba(138,134,168,.25)"),
}
SEV_ORDER = ["critical", "high", "medium", "low", "info"]


def tip_for(f: dict) -> str:
    t = (f.get("title") or "").lower()
    if "tap target" in t:
        return "Make each control at least 48x48 dp. Add padding or set minHeight/minWidth to 48dp; the icon can stay the same size."
    if "no label" in t or "screen reader" in t:
        return "Give each icon button a contentDescription (Compose: contentDescription / Modifier.semantics) so TalkBack reads what it does."
    if "cold start" in t:
        return "Move work out of Application.onCreate and the first Activity: lazy-load SDKs, defer network calls, and add a baseline profile."
    if "random input" in t:
        return "Replay the stress command in the steps with the same seed to hit the exact input sequence, then guard the code path shown in the log."
    if "rotat" in t or "background" in t:
        return "Save and restore screen state (ViewModel / rememberSaveable / onSaveInstanceState) so it survives rotation and process death."
    return TIP.get(f.get("category", ""), "")

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




# ─────────────────────────── professional bug-report model ───────────────────────────
PRIORITY = {  # severity = how bad for a user; priority = how soon to fix
    "P1": ("P1 · Fix before launch", "#D61F5B"),
    "P2": ("P2 · Fix this release", "#C2560F"),
    "P3": ("P3 · Next release", "#5B3DF5"),
    "P4": ("P4 · Polish / backlog", "#5E5A80"),
}
CATEGORY = {"crash": "Crash / freeze", "a11y": "Accessibility", "perf": "Performance", "stability": "Stability",
            "functional": "Functional", "ui": "UI", "ux": "UX", "link": "Broken link", "content": "Content",
            "security": "Policy / security", "value": "Data"}


def kind_of(f: dict) -> str:
    t = (f.get("title") or "").lower()
    if f.get("category") == "crash":
        if "not responding" in t or "froze" in t or "freeze" in t:
            return "anr"
        return "stress" if "random input" in t else "crash"
    if "tap target" in t: return "tap"
    if "no label" in t: return "label"
    if "cold start" in t: return "coldstart"
    if f.get("category") == "stability": return "nonfatal"
    return f.get("category") or "other"


def priority_of(f: dict) -> str:
    k, sev = kind_of(f), f.get("severity", "low")
    if k in ("crash", "anr", "stress") or sev == "critical": return "P1"
    if sev == "high": return "P2"
    if sev == "medium": return "P3"
    return "P4"


EXPLAIN = {
    "crash": ("The app handles the action and stays open.",
              "The app closes immediately (crash).",
              "Every user who takes these steps loses what they were doing. Crashes count against your Google Play Android vitals and visibility.",
              "https://developer.android.com/topic/performance/vitals/crash"),
    "stress": ("The app stays responsive under fast, repeated input.",
               "The app crashes or freezes during rapid taps and swipes.",
               "Fast tappers, double-taps and slow phones reach this code path in real life.",
               "https://developer.android.com/studio/test/other-testing-tools/monkey"),
    "anr": ("The app responds to input within 5 seconds.",
            "Android shows “app isn’t responding” — the main thread was blocked.",
            "Users see a system error and often force-close the app. ANRs count against Android vitals.",
            "https://developer.android.com/topic/performance/vitals/anr"),
    "label": ("TalkBack reads out what each control does.",
              "TalkBack only says “button” — the control has no text or content description.",
              "Blind and low-vision users can’t tell what these controls do. Google Play’s pre-launch report flags this.",
              "https://support.google.com/accessibility/android/answer/7158690"),
    "tap": ("Every tappable control is at least 48 × 48 dp.",
            "The boxed control is smaller than 48 dp, so taps miss it.",
            "People with larger fingers or motor impairments mis-tap. Google Play’s pre-launch report flags this.",
            "https://support.google.com/accessibility/android/answer/7101858"),
    "coldstart": ("The first screen appears in under 5 seconds from a cold start.",
                  "The first screen takes longer than 5 seconds.",
                  "Slow launches drive uninstalls; Android vitals marks 5 s+ cold starts as excessive.",
                  "https://developer.android.com/topic/performance/vitals/launch-time"),
    "nonfatal": ("No errors are logged during normal use.",
                 "The app logged this error and recovered.",
                 "Hidden today, but a caught error usually means bad data that can turn into a crash later.",
                 "https://developer.android.com/studio/debug/logcat"),
}


def crash_tip(ev: dict) -> str:
    exc = (ev.get("exception") or "").split(":")[0].split(".")[-1]
    frame = ev.get("app_frame") or ""
    where = f" in {frame}" if frame else ""
    hint = {"NullPointerException": "a value was null when this code ran \u2014 check it before use (or make it non-null) and validate the input that feeds it",
            "IndexOutOfBoundsException": "the code read past the end of a list \u2014 check the size first, especially for empty results",
            "IllegalStateException": "the code ran while the screen or object was in the wrong state \u2014 check lifecycle (e.g. after rotation or when the fragment is detached)",
            "NumberFormatException": "text was parsed as a number without checking it \u2014 validate the input or use toIntOrNull()",
            "ClassCastException": "a value had a different type than expected \u2014 check the type before casting",
            "OutOfMemoryError": "the app ran out of memory \u2014 downsample large images and release references"}.get(exc, "guard this code path and handle the bad input")
    return f"{exc or 'The crash'}{where}: {hint}. Then re-run the steps above \u2014 the app should stay open."


def _resolve(run_dir: Path, p) -> Path | None:
    if not p:
        return None
    p = Path(p)
    for c in (p, run_dir / p, run_dir / "screens" / p.name, run_dir / p.name):
        if c.exists():
            return c
    return None


def app_log_excerpt(run_dir: Path, pids, limit=80) -> list[tuple[str, str]]:
    """The app's own warnings/errors only (lines from its process IDs)."""
    lf = run_dir / "logcat.txt"
    if not lf.exists() or not pids:
        return []
    pids = {str(p) for p in pids}
    out = []
    for line in lf.read_text(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[2] in pids and parts[4] in ("E", "W", "F"):
            out.append((parts[4], line.strip()[:260]))
    return out[-limit:]


def from_run(run_dir, app_name, platform, url_map=None):
    """Build the report data from a run directory (findings.trusted.json preferred)."""
    run_dir = Path(run_dir)
    url_map = url_map or {}
    src = run_dir / "findings.trusted.json"
    if not src.exists():
        src = run_dir / "findings.json"
    raw = json.loads(src.read_text()) if src.exists() else []
    rd = lambda n: json.loads((run_dir / n).read_text()) if (run_dir / n).exists() else {}
    summary = rd("summary.json").get("summary", {})
    device, app = rd("device.json"), rd("app.json")

    findings = []
    for idx, f in enumerate(raw):
        if f.get("severity") == "info":
            continue
        ev = f.get("evidence") or {}
        shot = ev.get("screenshot", "")
        stack = ev.get("stack") or ""
        if isinstance(stack, list):
            stack = "\n".join(stack)
        mp = re.search(r"PID:\s*(\d+)", stack)
        if mp:  # keep only the crashing process's lines
            stack = "\n".join(l for l in stack.splitlines() if re.search(r"\s" + mp.group(1) + r"\s", l) or "PID:" in l)
        findings.append({
            "raw_index": idx,
            "severity": f.get("severity", "low"), "category": f.get("category", ""),
            "kind": kind_of(f), "priority": priority_of(f),
            "title": f.get("title", ""), "detail": f.get("detail", ""),
            "screen": f.get("screen_name") or f.get("screen", "—"),
            "activity": ev.get("activity", ""), "elements": ev.get("elements") or [],
            "repro": f.get("repro", []) or [],
            "screenshot": url_map.get(f"{idx}:screenshot") or url_map.get(shot, shot if str(shot).startswith("http") else ""),
            "after_screenshot": url_map.get(f"{idx}:after_screenshot", ""),
            "video_at": "", "t": ev.get("t"),
            "tip": crash_tip(ev) if kind_of(f) == "crash" and (ev.get("exception") or ev.get("app_frame")) else tip_for(f),
            "reproduction": f.get("reproduction", ""), "reproduction_ratio": f.get("reproduction_ratio", ""),
            "exception": ev.get("exception", ""), "app_frame": ev.get("app_frame", ""),
            "stack": stack[:3000], "log": ev.get("log", "") if ev.get("log") not in ("ANR",) else "",
            "obfuscated": ev.get("obfuscated", False),
        })
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (f["priority"], order.get(f["severity"], 4)))
    for n, f in enumerate(findings, 1):
        f["id"] = f"GIL-{n:03d}"

    return {"app_name": app_name, "platform": platform, "device": device, "app": app,
            "run_at": datetime.datetime.utcfromtimestamp(summary.get("started_at") or datetime.datetime.utcnow().timestamp()).strftime("%b %d, %Y · %H:%M UTC"),
            "stats": {"screens": summary.get("screens", 0), "steps": summary.get("steps", 0),
                      "duration_s": summary.get("duration_s", 0)},
            "findings": findings, "coverage": summary, "screens": [],
            "app_logs": app_log_excerpt(run_dir, summary.get("pids") or []),
            "video_urls": [], "log_url": "", "icon_url": ""}


# ─────────────────────────── charts (inline SVG, no scripts) ───────────────────────────
def donut(parts, total_label):
    tot = sum(v for _, v, _ in parts) or 1
    r, c = 52, 2 * 3.14159 * 52
    off, segs = 0.0, []
    for _, v, col in parts:
        if not v:
            continue
        L = c * v / tot
        segs.append(f'<circle r="{r}" cx="70" cy="70" fill="none" stroke="{col}" stroke-width="18" '
                    f'stroke-dasharray="{L:.1f} {c - L:.1f}" stroke-dashoffset="{-off:.1f}" transform="rotate(-90 70 70)"/>')
        off += L
    if not segs:
        segs.append(f'<circle r="{r}" cx="70" cy="70" fill="none" stroke="#E4DFFF" stroke-width="18"/>')
    n = sum(v for _, v, _ in parts)
    legend = "".join(f'<div class="lg"><i style="background:{col}"></i>{e(lab)}<b>{v}</b></div>' for lab, v, col in parts)
    return (f'<div class="donut"><svg viewBox="0 0 140 140" width="140" height="140" role="img" aria-label="{n} {e(total_label)}">'
            f'{"".join(segs)}<text x="70" y="68" text-anchor="middle" class="dn">{n}</text>'
            f'<text x="70" y="88" text-anchor="middle" class="dl">{e(total_label)}</text></svg><div>{legend}</div></div>')


def hbars(rows, unit="", limit=None, limit_label=""):
    mx = max([v for _, v in rows] + ([limit] if limit else []) + [1])
    out = []
    for lab, v in rows:
        w = 100 * v / mx
        bad = limit is not None and v > limit
        out.append(f'<div class="hb"><span>{e(lab)}</span><div class="hbt"><div class="hbf{" bad" if bad else ""}" style="width:{w:.1f}%"></div>'
                   + (f'<div class="hbl" style="left:{100 * limit / mx:.1f}%" title="{e(limit_label)}"></div>' if limit else "")
                   + f'</div><b>{e(v)}{e(unit)}</b></div>')
    note = f'<div class="hbn"><i></i>{e(limit_label)}</div>' if limit else ""
    return "".join(out) + note


# ─────────────────────────── page ───────────────────────────
def build(data):
    app_name = data.get("app_name", "Your app")
    platform = data.get("platform", "Android")
    findings = data.get("findings", [])
    device, app, cov = data.get("device") or {}, data.get("app") or {}, data.get("coverage") or {}
    perf, stats = cov.get("performance") or {}, data.get("stats", {})
    counts = {k: sum(1 for f in findings if f["severity"] == k) for k in SEV_ORDER}
    score = readiness(findings)
    sc = "#0E9F7B" if score >= 80 else "#B87900" if score >= 55 else "#D61F5B"
    verdict = ("Looks ready — only polish left." if score >= 80 else
               "Close — fix the items marked P1 and P2 before launch." if score >= 55 else
               "Not ready — these issues would hurt real users.")
    mins = max(1, round((stats.get("duration_s") or 0) / 60))
    crashes = [f for f in findings if f["kind"] in ("crash", "anr", "stress")]
    ver = " ".join(x for x in [app.get("version_name") and f'v{app["version_name"]}',
                               app.get("version_code") and f'(build {app["version_code"]})'] if x)
    icon = (f'<img class="appicon" src="{e(data["icon_url"])}" alt="">' if data.get("icon_url")
            else f'<div class="appicon ph">{e((app_name or "A")[:1].upper())}</div>')
    dev_short = " · ".join(x for x in [device.get("model"), device.get("release") and f'Android {device["release"]}',
                                           device.get("api") and f'API {device["api"]}'] if x) or "Android device"

    # ── hero
    hero = f'''<section class="hero">
      <div class="who">{icon}<div><div class="eyebrow">QA report for</div><h1>{e(app_name)}</h1>
        <div class="meta">{e(app.get("package") or "")}{" · " + e(ver) if ver else ""} · {e(platform)} · tested {e(data.get("run_at"))}</div></div></div>
      <div class="score"><div class="ring" style="--p:{score};--c:{sc}"><b>{score}</b><span>of 100</span></div>
        <div><div class="verdict">{e(verdict)}</div>
        <div class="pills">{"".join(f'<span class="sp" style="color:{SEV[k][1]};background:{SEV[k][2]}">{counts[k]} {SEV[k][0].lower()}</span>' for k in SEV_ORDER if counts.get(k))}</div>
        <div class="heroact">{('<a class="btn" href="#issues">See the ' + str(len(findings)) + ' issues</a>') if findings else ''}
          {f'<a class="btn ghost" href="{e(data["video_urls"][0])}">Watch the test session</a>' if data.get("video_urls") else ''}</div></div></div>
    </section>'''

    # ── at a glance
    tiles = [("Issues found", len(findings)), ("Crashes & freezes", len(crashes)), ("Screens tested", stats.get("screens", 0)),
             ("Actions performed", stats.get("steps", 0)), ("Test time", f"{mins} min"),
             ("Stress events", f'{sum((r.get("events") or 0) for r in perf.get("stress") or []):,}')]
    sev_chart = donut([(SEV[k][0], counts.get(k, 0), SEV[k][1]) for k in ("critical", "high", "medium", "low")], "issues")
    catc = {}
    for f in findings:
        catc[CATEGORY.get(f["category"], f["category"].title())] = catc.get(CATEGORY.get(f["category"], f["category"].title()), 0) + 1
    cat_chart = hbars(sorted(catc.items(), key=lambda kv: -kv[1])) if catc else '<p class="muted">No issues to chart.</p>'
    cs = perf.get("cold_start_ms") or []
    cs_chart = hbars([(f"Run {i + 1}", round(t / 1000, 1)) for i, t in enumerate(cs)], " s", 5.0,
                     "Android vitals limit: 5 s") if cs else '<p class="muted">Not measured.</p>'
    mem = ""
    if perf.get("mem_start_kb") and perf.get("mem_end_kb"):
        a, b = perf["mem_start_kb"] // 1024, perf["mem_end_kb"] // 1024
        mem = hbars([("At launch", a), ("After full session", b)], " MB")
    glance = f'''<section class="card"><h2>At a glance</h2>
      <div class="tiles">{"".join(f'<div class="tile"><b>{e(v)}</b><span>{e(k)}</span></div>' for k, v in tiles)}</div>
      <div class="charts">
        <div class="chart"><h3>Issues by severity</h3>{sev_chart}</div>
        <div class="chart"><h3>Issues by type</h3>{cat_chart}</div>
        <div class="chart"><h3>Cold start time</h3>{cs_chart}</div>
        <div class="chart"><h3>Memory use</h3>{mem or '<p class="muted">Not measured.</p>'}</div>
      </div></section>'''

    # ── next steps
    steps = []
    for f in findings:
        if f["priority"] in ("P1", "P2"):
            steps.append(f'<li><a href="#{f["id"]}">{f["id"]}</a> <span class="pp" style="color:{PRIORITY[f["priority"]][1]}">{f["priority"]}</span> {e(f["title"])}</li>')
    lowcnt = sum(1 for f in findings if f["priority"] in ("P3", "P4"))
    if lowcnt:
        steps.append(f'<li>Then work through the {lowcnt} P3/P4 item{"s" if lowcnt > 1 else ""} below — mostly quick accessibility and polish fixes.</li>')
    if any(f.get("obfuscated") for f in findings):
        steps.append('<li>Upload your <b>mapping.txt</b> with your next build so crash reports point to the exact line of your source code.</li>')
    steps.append('<li>Upload the fixed build from your dashboard — we re-test it automatically and every issue here is re-checked.</li>')
    nexts = f'<section class="card"><h2>Next steps</h2><ol class="next">{"".join(steps)}</ol></section>'

    # ── summary table
    rows = "".join(
        f'<tr><td><a href="#{f["id"]}">{f["id"]}</a></td><td>{e(f["title"])}</td>'
        f'<td><span class="sev" style="color:{SEV[f["severity"]][1]};background:{SEV[f["severity"]][2]}">{SEV[f["severity"]][0]}</span></td>'
        f'<td style="color:{PRIORITY[f["priority"]][1]};font-weight:700">{f["priority"]}</td><td>{e(f["screen"])}</td></tr>'
        for f in findings)
    table = (f'<section class="card" id="issues"><h2>All issues</h2><div class="tw"><table><thead><tr><th>ID</th><th>Issue</th><th>Severity</th>'
             f'<th>Priority</th><th>Where</th></tr></thead><tbody>{rows}</tbody></table></div>'
             f'<p class="legend"><b>Severity</b> is how badly it hurts a user. <b>Priority</b> is how soon to fix it: '
             + " · ".join(v[0] for v in PRIORITY.values()) + '.</p></section>') if findings else \
        '<section class="card" id="issues"><h2>All issues</h2><p class="empty">No issues found in this pass. Nice work.</p></section>'

    # ── bug cards
    REP = {"confirmed": ("Reproduced", "#0E9F7B"), "intermittent": ("Intermittent", "#B87900"),
           "single-occurrence": ("Seen once", "#5E5A80")}

    def card(f):
        sname, scol, sbg, sbd = SEV[f["severity"]]
        plab, pcol = PRIORITY[f["priority"]]
        expected, actual, impact, ref = EXPLAIN.get(f["kind"], ("", "", "", ""))
        if f["kind"] in ("crash", "anr", "stress", "nonfatal", "coldstart") and f.get("detail"):
            actual = f["detail"]
        rep = ""
        if f.get("reproduction") in REP:
            lab, rc = REP[f["reproduction"]]
            rep = f'<span class="chip" style="color:{rc};border-color:{rc}66">{lab} {e(f.get("reproduction_ratio") or "")}</span>'
        loc = [("Screen", f["screen"])]
        if f.get("activity"):
            loc.append(("Activity", f["activity"].split("/")[-1]))
        for el in f["elements"][:3]:
            b = el.get("bounds")
            pos = f' at ({b[0]}, {b[1]})–({b[2]}, {b[3]}) px' if b else ""
            loc.append(("Control", f'“{el.get("label")}”' + (f' · id {el["rid"]}' if el.get("rid") else "")
                        + (f' · {el["size_dp"]}' if el.get("size_dp") else "") + pos))
        if len(f["elements"]) > 3:
            loc.append(("", f'+ {len(f["elements"]) - 3} more, all boxed in the screenshot'))
        loc.append(("Device", dev_short))
        if f.get("video_at"):
            loc.append(("Video", f'<a href="{e(f["video_at"])}">▶ Jump to this moment</a>'))
        loc_html = "".join(f'<div class="kv"><span>{e(k)}</span><b>{v if k == "Video" else e(v)}</b></div>' for k, v in loc)
        shots = ""
        if f.get("screenshot"):
            shots += (f'<figure><a href="{e(f["screenshot"])}"><img src="{e(f["screenshot"])}" loading="lazy" alt="Screen where {e(f["id"])} happens"></a>'
                      f'<figcaption>{"Before the tap — the control is boxed in red" if f["kind"] == "crash" else "Problem area boxed in red"}</figcaption></figure>')
        if f.get("after_screenshot"):
            shots += (f'<figure><a href="{e(f["after_screenshot"])}"><img src="{e(f["after_screenshot"])}" loading="lazy" alt="After"></a>'
                      f'<figcaption>Right after — the app has closed</figcaption></figure>')
        cause = ""
        if f.get("exception") or f.get("app_frame") or f.get("stack") or f.get("log"):
            cause = ('<div class="blk"><h4>Root cause (from the crash log)</h4><div class="cause">'
                     + (f'<div><span>Exception</span><code>{e(f["exception"])}</code></div>' if f.get("exception") else "")
                     + (f'<div><span>Your code</span><code>{e(f["app_frame"])}</code></div>' if f.get("app_frame") else "")
                     + (f'<pre>{e(f["stack"])}</pre>' if f.get("stack") else (f'<pre>{e(f["log"])}</pre>' if f.get("log") else ""))
                     + ('<p class="obf">This build is obfuscated. Upload your mapping.txt and we’ll show the exact source line.</p>' if f.get("obfuscated") else "")
                     + '</div></div>')
        repro = "".join(f"<li>{e(s)}</li>" for s in f["repro"][:25])
        return f'''<article class="bug" id="{f["id"]}" style="border-top-color:{scol}">
      <header><span class="bid">{f["id"]}</span><h3>{e(f["title"])}</h3>
        <div class="chips"><span class="chip solid" style="background:{scol}">{sname}</span>
        <span class="chip" style="color:{pcol};border-color:{pcol}66">{plab}</span>
        <span class="chip">{e(CATEGORY.get(f["category"], f["category"].title()))}</span>{rep}</div></header>
      <div class="bgrid"><div>
        <div class="blk"><h4>Where</h4>{loc_html}</div>
        {f'<div class="ea"><div><h4>Expected</h4><p>{e(expected)}</p></div><div><h4>Actual</h4><p>{e(actual)}</p></div></div>' if expected else f'<div class="blk"><h4>What happens</h4><p>{e(f["detail"])}</p></div>'}
        {f'<div class="blk"><h4>Who it affects</h4><p>{e(impact)}</p></div>' if impact else ""}
        {f'<div class="blk"><h4>Steps to reproduce</h4><ol>{repro}</ol></div>' if repro else ""}
        {cause}
        {f'<div class="fix"><h4>Suggested fix</h4><p>{e(f["tip"])}</p>' + (f'<a href="{e(ref)}">Android guidance</a>' if ref else "") + '</div>' if f.get("tip") else ""}
      </div><div class="shots">{shots or '<p class="muted">No screenshot for this issue type — see the steps and log.</p>'}</div></div>
    </article>'''

    bugs = "".join(card(f) for f in findings)

    # ── screens gallery
    gal = data.get("screens") or []
    gallery = (f'<section class="card"><h2>Screens tested <span class="n">{len(gal)}</span></h2><div class="gal">'
               + "".join(f'<figure><a href="{e(s["url"])}"><img src="{e(s["url"])}" loading="lazy" alt=""></a><figcaption>{e(s["name"])}</figcaption></figure>' for s in gal)
               + '</div></section>') if gal else ""

    # ── performance & stability
    prow = []
    if cs: prow.append(("Cold start (3 runs)", " · ".join(f"{t / 1000:.1f} s" for t in cs)))
    if perf.get("mem_start_kb"): prow.append(("Memory", f'{perf["mem_start_kb"] // 1024} MB at launch → {(perf.get("mem_end_kb") or 0) // 1024} MB after the session'))
    for r in perf.get("stress") or []:
        prow.append((f'Stress seed {r.get("seed")}', f'{(r.get("events") or 0):,} random events — ' + ("crashed" if r.get("crash") else "froze" if r.get("anr") else "survived")))
    ne = cov.get("network_errors") or {}
    if ne: prow.append(("Network errors logged", ", ".join(f"{k} ×{v}" for k, v in ne.items()) + " (test network; informational)"))
    perf_html = (f'<section class="card"><h2>Performance & stability</h2>'
                 + "".join(f'<div class="kv"><span>{e(k)}</span><b>{e(v)}</b></div>' for k, v in prow) + '</section>') if prow else ""

    # ── app surface & integrations
    integ = cov.get("integrations") or []
    dec = cov.get("declared") or {}
    perms = app.get("permissions") or dec.get("permissions") or []
    srow = []
    if app.get("min_sdk") or app.get("target_sdk"): srow.append(("SDK levels", f'min {app.get("min_sdk") or "?"} · target {app.get("target_sdk") or "?"}'))
    if app.get("native_code"): srow.append(("Native code", app["native_code"]))
    if integ: srow.append(("Hands off to other apps", ", ".join(sorted({i.get("opened", "") for i in integ}))))
    if perms: srow.append((f"Permissions ({len(perms)})", ", ".join(p.split(".")[-1] for p in perms[:30])))
    if dec.get("deep_links"): srow.append(("Deep links", ", ".join(dec["deep_links"][:10])))
    surface = (f'<section class="card"><h2>App surface & integrations</h2>'
               + "".join(f'<div class="kv"><span>{e(k)}</span><b>{e(v)}</b></div>' for k, v in srow) + '</section>') if srow else ""

    # ── app logs
    logs = data.get("app_logs") or []
    log_html = ""
    if logs or data.get("log_url"):
        lines = "".join(f'<div class="ll {lv}">{e(l)}</div>' for lv, l in logs) or '<div class="ll">No warnings or errors from your app during the session.</div>'
        log_html = (f'<section class="card"><h2>Your app’s logs</h2><p class="muted">Warnings and errors from your app’s own process only '
                    f'(other apps and the OS are filtered out).</p><div class="logs">{lines}</div>'
                    + (f'<div class="evrow">' + (f'<a class="btn ghost" href="{e(data["log_url"])}">Download the full device log</a>' if data.get("log_url") else "")
                       + "".join(f'<a class="btn ghost" href="{e(v)}">▶ Session video{f" part {i + 1}" if len(data.get("video_urls")) > 1 else ""}</a>' for i, v in enumerate(data.get("video_urls") or []))
                       + '</div>') + '</section>')

    # ── environment
    env = [("Device", device.get("model") or "—"), ("Device type", device.get("kind") or "Android emulator"),
           ("Android", " · ".join(x for x in [device.get("release") and f'Android {device["release"]}', device.get("api") and f'API {device["api"]}'] if x) or "—"),
           ("Screen", " · ".join(x for x in [device.get("width") and device.get("height") and f'{device["width"]} × {device["height"]} px', device.get("density") and f'{device["density"]} dpi'] if x) or "—"),
           ("CPU / ABI", device.get("abi") or "—"), ("Locale · time zone", f'{device.get("locale") or "en-US"} · {device.get("timezone") or "UTC"}'),
           ("Network", device.get("network") or "Wi-Fi"), ("Browser", "Not used — native app (links that open a browser are listed under integrations)"),
           ("App build", " ".join(x for x in [app.get("package"), ver] if x) or "—"),
           ("Test run", f'{data.get("run_at")} · {mins} min'), ("Engine", "Get It Listed engine · crawl, edge cases, stress, accessibility, replay x3")]
    env_html = '<section class="card"><h2>Test environment</h2><div class="envg">' + "".join(
        f'<div class="kv"><span>{e(k)}</span><b>{e(v)}</b></div>' for k, v in env) + '</div></section>'

    method = ('<section class="card"><h2>How we tested</h2><p class="muted">We installed your build on a clean Android device and worked through it '
              'systematically: timed three cold starts, opened every screen we could reach, tapped every safe control, submitted forms empty and filled, '
              'rotated and backgrounded the app on each new screen, ran seeded random-input stress tests, and checked every screen against Google’s '
              'accessibility rules. Every crash was replayed from a fresh install, and only crashes inside your app’s own process that happen again are '
              'reported. Purchase, payment, delete-account and sign-out controls are never tapped.</p></section>')

    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(app_name)} — QA report — Get It Listed</title>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;700;900&family=Instrument+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{{--bg:#F2EFFF;--card:#fff;--line:#E4DFFF;--text:#1A1446;--muted:#5E5A80;--faint:#8A86A8;--v:#5B3DF5;--disp:"Unbounded",system-ui,sans-serif;--sans:"Instrument Sans",system-ui,sans-serif}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--sans);background:var(--bg);color:var(--text);line-height:1.55;font-size:15.5px;padding:22px 14px 70px;-webkit-font-smoothing:antialiased}}
a{{color:var(--v)}}
.wrap{{max-width:1040px;margin:0 auto}}
.brandbar{{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;font-weight:700}}
.brandbar .b{{display:flex;gap:9px;align-items:center;font-family:var(--disp);font-size:15px}}
.brandbar small{{font-weight:500;color:var(--muted);font-size:13px}}
h1{{font-family:var(--disp);font-weight:900;font-size:clamp(26px,4vw,38px);letter-spacing:-.03em;line-height:1.05}}
h2{{font-family:var(--disp);font-size:20px;font-weight:700;letter-spacing:-.02em;margin-bottom:16px}}
h2 .n{{font-size:13px;background:var(--bg);border-radius:99px;padding:3px 10px;vertical-align:middle;color:var(--muted)}}
h3{{font-family:var(--disp);font-size:14px;font-weight:700}}
h4{{font-size:13px;font-weight:700;color:var(--muted);margin-bottom:5px}}
.card,.hero{{background:var(--card);border-radius:26px;padding:26px;margin-bottom:14px}}
.hero{{display:grid;grid-template-columns:1.2fr 1fr;gap:24px;align-items:center}}
.who{{display:flex;gap:16px;align-items:center}}
.appicon{{width:72px;height:72px;border-radius:20px;flex:none;object-fit:cover;box-shadow:0 0 0 1px var(--line)}}
.appicon.ph{{display:grid;place-items:center;background:var(--v);color:#fff;font-family:var(--disp);font-size:30px;font-weight:900}}
.eyebrow{{font-size:13px;color:var(--muted);font-weight:600}}
.meta{{color:var(--muted);font-size:13.5px;margin-top:6px;word-break:break-word}}
.score{{display:flex;gap:18px;align-items:center}}
.ring{{--a:calc(var(--p)*1%);width:112px;height:112px;border-radius:50%;flex:none;display:grid;place-items:center;background:conic-gradient(var(--c) var(--a),#EEEAFF 0);position:relative}}
.ring::before{{content:"";position:absolute;inset:11px;border-radius:50%;background:#fff}}
.ring b,.ring span{{position:relative;grid-area:1/1;text-align:center}}
.ring b{{font-family:var(--disp);font-size:32px;font-weight:900;margin-top:-14px;color:var(--c)}}.ring span{{font-size:11px;color:var(--faint);margin-top:30px}}
.verdict{{font-weight:700;font-size:17px;margin-bottom:8px}}
.pills{{display:flex;gap:6px;flex-wrap:wrap}}.sp{{font-weight:700;font-size:12.5px;border-radius:99px;padding:4px 10px}}
.heroact,.evrow{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}
.btn{{display:inline-block;background:var(--v);color:#fff;text-decoration:none;font-weight:700;font-size:14px;border-radius:99px;padding:10px 18px}}
.btn.ghost{{background:#fff;color:var(--text);box-shadow:inset 0 0 0 2px var(--line)}}
.tiles{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:20px}}
.tile{{background:var(--bg);border-radius:18px;padding:14px}}
.tile b{{font-family:var(--disp);font-size:24px;font-weight:900;display:block;line-height:1.1}}.tile span{{font-size:12.5px;color:var(--muted)}}
.charts{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}
.chart{{border:2px solid var(--line);border-radius:20px;padding:16px}}.chart h3{{margin-bottom:12px}}
.donut{{display:flex;gap:16px;align-items:center}}.donut .dn{{font-family:var(--disp);font-size:28px;font-weight:900;fill:var(--text)}}.donut .dl{{font-size:11px;fill:var(--faint)}}
.lg{{display:flex;align-items:center;gap:8px;font-size:13.5px;margin:4px 0}}.lg i{{width:11px;height:11px;border-radius:3px}}.lg b{{margin-left:auto;padding-left:14px}}
.hb{{display:grid;grid-template-columns:110px 1fr 58px;gap:10px;align-items:center;font-size:13px;margin:7px 0}}
.hbt{{height:12px;background:#F2EFFF;border-radius:6px;position:relative}}.hbf{{height:100%;border-radius:6px;background:var(--v)}}.hbf.bad{{background:#E5336F}}
.hbl{{position:absolute;top:-4px;bottom:-4px;width:2px;background:#1A1446}}.hb b{{text-align:right}}
.hbn{{font-size:12px;color:var(--muted);display:flex;gap:6px;align-items:center;margin-top:6px}}.hbn i{{width:2px;height:12px;background:#1A1446}}
.next{{margin-left:20px;display:grid;gap:8px}}.next a{{font-weight:700}}.pp{{font-weight:700;font-size:12.5px}}
.tw{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;font-size:12.5px;color:var(--muted);padding:8px 10px;border-bottom:2px solid var(--line)}}
td{{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}}td a{{font-weight:700;white-space:nowrap}}
.sev{{font-weight:700;font-size:12px;border-radius:99px;padding:3px 9px;white-space:nowrap}}
.legend{{font-size:12.5px;color:var(--muted);margin-top:12px}}
.bug{{background:#fff;border-radius:26px;padding:24px;margin-bottom:14px;border-top:6px solid}}
.bug header{{margin-bottom:16px}}.bid{{font-family:var(--disp);font-weight:700;font-size:13px;color:var(--muted)}}
.bug h3{{font-size:19px;margin:4px 0 10px;line-height:1.25}}
.chips{{display:flex;gap:6px;flex-wrap:wrap}}.chip{{font-size:12.5px;font-weight:700;border:2px solid var(--line);border-radius:99px;padding:3px 10px;color:var(--muted)}}.chip.solid{{color:#fff;border-color:transparent}}
.bgrid{{display:grid;grid-template-columns:1fr 300px;gap:22px}}
.blk,.ea,.fix{{margin-bottom:16px}}
.kv{{display:flex;gap:12px;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px}}.kv span{{color:var(--muted);min-width:120px;flex:none}}.kv b{{font-weight:600;word-break:break-word}}
.ea{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.ea>div{{background:var(--bg);border-radius:16px;padding:12px 14px;font-size:14.5px}}
.ea>div:last-child{{background:#FFF0F5}}
.blk ol{{margin-left:20px;font-size:14.5px}}.blk p{{font-size:14.5px}}
.cause{{background:var(--bg);border-radius:16px;padding:12px 14px;font-size:13.5px}}.cause div{{display:flex;gap:10px;margin-bottom:4px}}.cause span{{color:var(--muted);min-width:80px}}
code,pre{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:#3B23C7;word-break:break-all}}
pre{{white-space:pre-wrap;margin-top:8px;max-height:220px;overflow:auto;background:#fff;border-radius:10px;padding:10px;color:var(--text)}}
.obf{{color:#B87900;font-size:13px;margin-top:6px}}
.fix{{background:#E8FAF4;border-radius:16px;padding:12px 14px;font-size:14.5px}}.fix h4{{color:#0B7D61}}.fix a{{font-size:13.5px;font-weight:700}}
.shots{{display:grid;gap:12px;align-content:start}}
figure img{{width:100%;border-radius:16px;border:2px solid var(--line);display:block}}figcaption{{font-size:12.5px;color:var(--muted);margin-top:5px}}
.gal{{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:12px}}
.logs{{background:#14102F;border-radius:16px;padding:12px;max-height:340px;overflow:auto;margin-top:10px}}
.ll{{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#C9C3F0;white-space:pre-wrap;word-break:break-all;padding:1px 0}}
.ll.E,.ll.F{{color:#FF8FB4}}.ll.W{{color:#FFD37A}}
.envg{{display:grid;grid-template-columns:1fr 1fr;column-gap:24px}}
.muted{{color:var(--muted);font-size:14px}}.empty{{color:var(--muted)}}
.foot{{text-align:center;color:var(--muted);font-size:13px;margin-top:22px}}
@media(max-width:820px){{.hero,.bgrid,.charts,.envg,.ea{{grid-template-columns:1fr}}.tiles{{grid-template-columns:repeat(3,1fr)}}.score{{flex-wrap:wrap}}}}
@media(max-width:480px){{.tiles{{grid-template-columns:repeat(2,1fr)}}.card,.hero,.bug{{padding:18px}}.kv{{flex-direction:column;gap:2px}}.hb{{grid-template-columns:80px 1fr 50px}}}}
@media print{{body{{background:#fff;padding:0}}.card,.hero,.bug{{break-inside:avoid;box-shadow:0 0 0 1px var(--line)}}.btn{{display:none}}}}
</style></head><body><div class="wrap">
<div class="brandbar"><div class="b"><svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true"><rect width="32" height="32" rx="10" fill="#5B3DF5"/><path d="M9 16.5l4.5 4.5L23 11.5" stroke="#fff" stroke-width="3.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>Get It Listed</div><small>Automated QA report</small></div>
{hero}{glance}{nexts}{table}{bugs}{gallery}{perf_html}{surface}{log_html}{env_html}{method}
<div class="foot">Prepared for {e(app_name)} by Get It Listed · getitlisted.app · Questions: hello@getitlisted.app</div>
</div></body></html>'''
