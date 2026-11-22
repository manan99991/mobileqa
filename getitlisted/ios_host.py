#!/usr/bin/env python3
"""
Get It Listed — iOS host side (runs on the macOS runner).

  appinfo <App.app> <run_dir>                     -> app.json (+ app_icon.png)
  replay  <run_dir> <App.app> <bundle> <udid> <xctestrun> <times>
                                                  -> regress.json (each crash replayed from a fresh install)
  trust   <run_dir> <App.app> <since_epoch>       -> findings.trusted.json, run_meta.json, logcat.txt, app_logs.json

Trust rules (same promise as Android): a crash is reported only if Apple's own crash report
shows THIS app's process crashing, and it happens again on replay. Root cause comes from the
crash report: exception type, Swift/ObjC message, and the first frame in the app's own code.
"""
from __future__ import annotations
import glob, json, os, plistlib, re, shutil, subprocess, sys, time
from pathlib import Path


def info_plist(app: Path) -> dict:
    with open(app / "Info.plist", "rb") as f:
        return plistlib.load(f)


def appinfo(app: Path, run: Path):
    p = info_plist(app)
    icons = []
    prim = ((p.get("CFBundleIcons") or {}).get("CFBundlePrimaryIcon") or {})
    for base in prim.get("CFBundleIconFiles") or []:
        icons += glob.glob(str(app / f"{base}*.png"))
    icons += glob.glob(str(app / "AppIcon*.png"))
    out = {
        "package": p.get("CFBundleIdentifier", ""),
        "label": p.get("CFBundleDisplayName") or p.get("CFBundleName", ""),
        "version_name": p.get("CFBundleShortVersionString", ""),
        "version_code": p.get("CFBundleVersion", ""),
        "min_sdk": "iOS " + str(p.get("MinimumOSVersion", "")),
        "target_sdk": str(p.get("DTPlatformName", "")) + " " + str(p.get("DTPlatformVersion", "")),
        "permissions": sorted(k.replace("UsageDescription", "") for k in p if k.endswith("UsageDescription")),
        "executable": p.get("CFBundleExecutable", ""),
        "icon": "",
    }
    if icons:
        best = max(icons, key=lambda f: os.path.getsize(f))
        shutil.copy(best, run / "app_icon.png")
        out["icon"] = "app_icon.png"
    (run / "app.json").write_text(json.dumps(out, indent=1))
    print("app info:", out["label"], out["version_name"], "icon" if out["icon"] else "no icon")


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def replay(run: Path, app: Path, bundle: str, udid: str, xctestrun: str, times: int):
    findings = json.loads((run / "findings.json").read_text()) if (run / "findings.json").exists() else []
    out = []
    for n, f in enumerate(findings):
        if f.get("category") != "crash" or not f.get("replay"):
            continue
        trail = run / f"trail_{n}.json"
        trail.write_text(json.dumps(f["replay"]))
        hits = 0
        for k in range(times):
            res = run / f"replay_{n}_{k}.json"
            sh(f"xcrun simctl terminate {udid} {bundle}; xcrun simctl uninstall {udid} {bundle}; "
               f"xcrun simctl install {udid} '{app}'; xcrun simctl privacy {udid} grant all {bundle}")
            env = dict(os.environ, TEST_RUNNER_GIL_BUNDLE=bundle, TEST_RUNNER_GIL_REPLAY=str(trail),
                       TEST_RUNNER_GIL_REPLAY_OUT=str(res), TEST_RUNNER_GIL_ARGS=os.environ.get("GIL_ARGS", ""))
            sh(f"xcodebuild test-without-building -xctestrun '{xctestrun}' -destination 'id={udid}' "
               f"-only-testing:GILCrawler/GILCrawler/testReplay -test-timeouts-enabled NO", env=env, timeout=900)
            try:
                if json.loads(res.read_text()).get("crashed"):
                    hits += 1
            except Exception:
                pass
        out.append({"title": f["title"], "screen": f.get("screen_name"), "reproduced": hits, "of": times})
        print(f"replay: {f['title']} -> {hits}/{times}")
    (run / "regress.json").write_text(json.dumps(out, indent=1))


def parse_ips(path: str, exe: str) -> dict | None:
    txt = Path(path).read_text(errors="ignore")
    nl = txt.find("\n")
    try:
        head, body = json.loads(txt[:nl]), json.loads(txt[nl + 1:])
    except Exception:
        return None
    proc = body.get("procName") or head.get("app_name") or head.get("name") or ""
    if proc != exe:
        return None
    imgs = body.get("usedImages") or []
    def fmt(fr):
        name = imgs[fr["imageIndex"]].get("name", "?") if isinstance(fr.get("imageIndex"), int) and fr["imageIndex"] < len(imgs) else "?"
        loc = f' ({Path(fr["sourceFile"]).name}:{fr.get("sourceLine")})' if fr.get("sourceFile") else ""
        return name, f'{name}  {fr.get("symbol", "?")}{loc}'
    frames = []
    if body.get("lastExceptionBacktrace"):
        frames = body["lastExceptionBacktrace"]
    else:
        ft = body.get("faultingThread", 0)
        th = body.get("threads") or []
        if isinstance(ft, int) and ft < len(th):
            frames = th[ft].get("frames") or []
    lines = [fmt(fr) for fr in frames[:40]]
    app_frame = next((l.split("  ", 1)[1] for n, l in lines if n == exe), "")
    exc = body.get("exception") or {}
    msgs = []
    for v in (body.get("asi") or {}).values():
        msgs += v if isinstance(v, list) else [str(v)]
    cr = (body.get("crashInfo") or {})
    exception = " ".join(x for x in [exc.get("type", ""), exc.get("signal", ""), (exc.get("subtype") or "")] if x)
    if msgs:
        exception += " — " + " ".join(m.strip() for m in msgs)[:300]
    return {"exception": exception.strip(), "app_frame": app_frame, "stack": "\n".join(l for _, l in lines[:18]),
            "t": os.path.getmtime(path), "file": path}


def trust(run: Path, app: Path, since: float):
    exe = info_plist(app).get("CFBundleExecutable", "")
    raw = json.loads((run / "findings.json").read_text()) if (run / "findings.json").exists() else []
    reg = {r["title"]: r for r in (json.loads((run / "regress.json").read_text()) if (run / "regress.json").exists() else [])}
    reports = []
    for d in (os.path.expanduser("~/Library/Logs/DiagnosticReports"),):
        for p in glob.glob(d + "/**/*.ips", recursive=True):
            if os.path.getmtime(p) >= since - 5:
                r = parse_ips(p, exe)
                if r:
                    reports.append(r)
    reports.sort(key=lambda r: r["t"])
    kept, dropped = [], 0
    used = set()
    for f in raw:
        if f.get("category") == "crash":
            r = reg.get(f["title"], {})
            n, of = r.get("reproduced", 0), r.get("of", 0)
            is_freeze = "froze" in f.get("title", "")
            if n == 0 or (not is_freeze and not reports):
                dropped += 1
                continue
            f["reproduction"] = "confirmed" if n == of else "intermittent"
            f["reproduction_ratio"] = f"{n}/{of}"
            if not is_freeze:
                t = (f.get("evidence") or {}).get("t", 0)
                cand = [r for r in reports if r["file"] not in used and r["t"] >= t - 30] or [r for r in reports if r["file"] not in used] or reports
                r0 = cand[0]
                used.add(r0["file"])
                ev = f.setdefault("evidence", {})
                ev.update({"exception": r0["exception"], "app_frame": r0["app_frame"], "stack": r0["stack"], "obfuscated": False})
                if r0["exception"]:
                    short = r0["exception"].split(" — ")[0]
                    f["detail"] = f"The app closed unexpectedly right after this step ({short})."
        kept.append(f)
    (run / "findings.trusted.json").write_text(json.dumps(kept, indent=1))
    state = json.loads((run / "ios_state.json").read_text()) if (run / "ios_state.json").exists() else {}
    summ = json.loads((run / "summary.json").read_text()).get("summary", {}) if (run / "summary.json").exists() else {}
    status = "inconclusive" if (state.get("foregrounded") is False or summ.get("screens", 0) == 0) else "ok"
    (run / "run_meta.json").write_text(json.dumps({"status": status, "reason": "app never reached a screen" if status != "ok" else "",
                                                     "kept": len(kept), "dropped": dropped, "crash_reports": len(reports)}))
    # app's own log: errors and faults from the unified log, plus all crash reports, as the downloadable log
    applog = run / "applog_ios.txt"
    lines = applog.read_text(errors="ignore").splitlines() if applog.exists() else []
    picked = []
    for l in lines:
        m = re.match(r"^\S+ \S+\s+(E|F|Df|I|Db)\s", l)
        lvl = m.group(1) if m else ""
        if lvl in ("E", "F") or re.search(r"\b(error|fault|failed|exception)\b", l, re.I):
            picked.append(["F" if lvl == "F" else ("E" if lvl == "E" or "error" in l.lower() else "W"), l[:260]])
    (run / "app_logs.json").write_text(json.dumps(picked[-80:]))
    with open(run / "logcat.txt", "w") as fo:
        fo.write("\n".join(lines))
        for r in reports:
            fo.write(f"\n\n===== crash report {Path(r['file']).name} =====\n{r['exception']}\n{r['stack']}\n")
    print(f"trust gate (iOS): kept {len(kept)} findings, dropped {dropped} (app crash reports: {len(reports)}); status {status}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "appinfo":
        appinfo(Path(sys.argv[2]), Path(sys.argv[3]))
    elif cmd == "replay":
        replay(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6], int(sys.argv[7]))
    elif cmd == "trust":
        trust(Path(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4]))
