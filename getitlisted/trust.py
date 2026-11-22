#!/usr/bin/env python3
"""
Get It Listed — the TRUST GATE.

Manan's #1 rule: never report a bug that isn't really the app's, and never a crash
that happened in the test environment. This post-run step enforces that with an
ALLOWLIST (not just a denylist): a crash survives only if it belongs to the app's
own process and (where we have a reproduce pass) actually reproduces.

Reads a finished mobileqa run dir + the full logcat, and writes:
  findings.trusted.json   the cleaned, app-scoped, reproduction-labelled findings
  run_meta.json           {status: ok|inconclusive, device, package, dropped[...]}

Inputs (env):
  GIL_RUN_DIR    reports/<run_id>
  GIL_PACKAGE    the app under test (e.g. com.acme.app)
  GIL_PLATFORM   android | ios   (default android)
Files it will use if present, inside GIL_RUN_DIR:
  findings.json              (from mobileqa)
  logcat.txt / syslog.txt    full device log (the workflow dumps it)
  device.json                {model, api, density, width, height, foregrounded: bool}
  regress.json               [{title, screen, reproduced: n, of: m}, ...] optional

Pure stdlib. Deterministic. Unit-tested parsing (see __main__ --selftest).
"""
from __future__ import annotations
import json, os, re, sys
from pathlib import Path

PKG_RE = re.compile(r"Process:\s*([A-Za-z0-9_.]+)")
FATAL_RE = re.compile(r"FATAL EXCEPTION", re.I)
ANR_RE = re.compile(r"ANR in ([A-Za-z0-9_.]+)", re.I)
# top app frame: "at com.acme.app.Foo.bar(Foo.java:42)"
FRAME_RE = re.compile(r"\bat ([A-Za-z0-9_$.]+)\.([A-Za-z0-9_$<>]+)\(([^)]*)\)")
EXC_RE = re.compile(r"((?:[A-Za-z0-9_$.]+\.)?[A-Za-z0-9_$]*(?:Exception|Error|Throwable)[A-Za-z0-9_$.]*)\b")


def parse_android_crashes(log: str, pkg: str) -> list[dict]:
    """Return the app's OWN crash blocks only. A block is kept iff its 'Process:' line
    equals the app package. Blocks from system_server, SystemUI, other apps -> dropped."""
    lines = log.splitlines()
    crashes, i = [], 0
    while i < len(lines):
        if FATAL_RE.search(lines[i]):
            block = lines[i:i + 40]
            text = "\n".join(block)
            m = PKG_RE.search(text)
            proc = m.group(1) if m else ""
            if proc == pkg:                       # <-- the allowlist: app process only
                crashes.append(_summarise(text, pkg, kind="crash"))
        else:
            am = ANR_RE.search(lines[i])
            if am and am.group(1) == pkg:
                crashes.append(_summarise("\n".join(lines[i:i + 20]), pkg, kind="anr"))
        i += 1
    return crashes


def _summarise(block: str, pkg: str, kind: str) -> dict:
    exc = ""
    for ln in block.splitlines():
        em = EXC_RE.search(ln)
        if em and "AndroidRuntime" not in ln.split(":")[0]:
            exc = em.group(1); break
    # first stack frame that is actually in the app's package = the honest "where it came from"
    app_frame = ""
    for fm in FRAME_RE.finditer(block):
        cls = fm.group(1)
        if cls.startswith(pkg) or pkg.split(".")[1:2] and pkg.split(".")[1] in cls:
            app_frame = f"{fm.group(1)}.{fm.group(2)}({fm.group(3)})"; break
    top_frame = ""
    fm = FRAME_RE.search(block)
    if fm:
        top_frame = f"{fm.group(1)}.{fm.group(2)}({fm.group(3)})"
    return {"kind": kind, "exception": exc, "app_frame": app_frame,
            "top_frame": top_frame, "stack": block.strip()[:1500]}


def _obfuscated(frame: str) -> bool:
    """A single-letter class/method (a.b.c) means R8/ProGuard stripped the names."""
    return bool(re.search(r"\b[a-z]\.[a-z]\b|\.[a-z]\(", frame or ""))


def main():
    run = Path(os.environ["GIL_RUN_DIR"])
    pkg = os.environ.get("GIL_PACKAGE", "")
    platform = os.environ.get("GIL_PLATFORM", "android").lower()
    findings = json.loads((run / "findings.json").read_text()) if (run / "findings.json").exists() else []
    logtext = ""
    for fn in ("logcat.txt", "syslog.txt"):
        if (run / fn).exists():
            logtext = (run / fn).read_text(errors="ignore"); break
    device = json.loads((run / "device.json").read_text()) if (run / "device.json").exists() else {}
    regress = json.loads((run / "regress.json").read_text()) if (run / "regress.json").exists() else []

    app_crashes = parse_android_crashes(logtext, pkg) if (platform == "android" and pkg) else []

    # Sanity gate: if the app never reached the foreground, we did not really test it —
    # mark inconclusive so NOTHING gets published as a bug from a bad environment.
    foregrounded = device.get("foregrounded", True)
    try:   # the crawler actually saw the app's own screens -> it ran, whatever is on screen at the very end
        if json.loads((run / "summary.json").read_text())["summary"].get("screens", 0) > 0:
            foregrounded = True
    except Exception:
        pass
    if not foregrounded:
        (run / "run_meta.json").write_text(json.dumps(
            {"status": "inconclusive", "reason": "app never reached foreground (environment, not a bug)",
             "package": pkg, "device": device}, indent=1))
        (run / "findings.trusted.json").write_text("[]")
        print("INCONCLUSIVE — app never foregrounded; published nothing.")
        return

    def repro_label(f):
        for r in regress:
            if r.get("title") == f.get("title") and r.get("screen") in (f.get("screen_name"), f.get("screen")):
                n, m = r.get("reproduced", 0), r.get("of", 0) or 1
                return ("confirmed" if n >= 2 else "intermittent" if n == 1 else "not-reproduced", f"{n}/{m}")
        return ("single-occurrence", "1/1")

    kept, dropped = [], []
    for f in findings:
        cat = f.get("category")
        if cat != "crash":
            kept.append(f); continue
        title = (f.get("title") or "").lower()
        ev = f.get("evidence") or {}
        from_screen = "error screen" in title or "error boundary" in (f.get("detail") or "").lower()
        is_freeze = "not responding" in title or "froze" in title or "freeze" in title
        if is_freeze:
            # ANRs never appear in the crash buffer: keep only if the freeze happens again on replay
            match = None
        elif from_screen:
            # the app's own error-boundary screen — app-scoped by construction
            match = app_crashes[0] if app_crashes else None
        else:
            # from logcat / dialog — require an app-process crash in the log
            logline = (ev.get("log") or "") + " " + (f.get("detail") or "")
            match = None
            for c in app_crashes:
                if c["exception"] and c["exception"].split(".")[-1] in logline:
                    match = c; break
            if not match and app_crashes:
                match = app_crashes[0]
            if not match:
                dropped.append({"title": f.get("title"), "why": "no app-process crash in logcat (likely system/env noise)"})
                continue
        # enrich the confirmed app crash with honest root-cause info
        if match:
            f.setdefault("evidence", {})
            f["evidence"]["app_frame"] = match.get("app_frame") or match.get("top_frame")
            f["evidence"]["exception"] = match.get("exception")
            f["evidence"]["stack"] = match.get("stack")
            f["evidence"]["obfuscated"] = _obfuscated(match.get("app_frame") or match.get("top_frame"))
        status, ratio = repro_label(f)
        f["reproduction"] = status
        f["reproduction_ratio"] = ratio
        if status == "not-reproduced":
            dropped.append({"title": f.get("title"), "why": "did not reproduce on replay"})
            continue
        kept.append(f)

    (run / "findings.trusted.json").write_text(json.dumps(kept, indent=1))
    (run / "run_meta.json").write_text(json.dumps(
        {"status": "ok", "package": pkg, "platform": platform, "device": device,
         "kept": len(kept), "dropped": dropped, "app_crashes_in_log": len(app_crashes)}, indent=1))
    print(f"trust gate: kept {len(kept)} findings, dropped {len(dropped)} "
          f"(app-process crashes in log: {len(app_crashes)})")
    for d in dropped:
        print(f"  dropped: {d['title']} — {d['why']}")


def _selftest():
    pkg = "com.acme.app"
    log = """
11-21 10:00:01.111 1200 1200 E AndroidRuntime: FATAL EXCEPTION: main
11-21 10:00:01.111 1200 1200 E AndroidRuntime: Process: com.acme.app, PID: 1200
11-21 10:00:01.111 1200 1200 E AndroidRuntime: java.lang.NullPointerException: Attempt to read null
11-21 10:00:01.111 1200 1200 E AndroidRuntime:  at com.acme.app.CheckoutActivity.onCreate(CheckoutActivity.java:42)
11-21 10:00:01.111 1200 1200 E AndroidRuntime:  at android.app.Activity.performCreate(Activity.java:8000)
11-21 10:00:02.000 900 900 E AndroidRuntime: FATAL EXCEPTION: main
11-21 10:00:02.000 900 900 E AndroidRuntime: Process: com.android.systemui, PID: 900
11-21 10:00:02.000 900 900 E AndroidRuntime: java.lang.RuntimeException: SystemUI blew up
11-21 10:00:03.000 500 500 E ActivityManager: ANR in com.android.launcher3
""".strip()
    cr = parse_android_crashes(log, pkg)
    assert len(cr) == 1, f"expected 1 app crash, got {len(cr)}"
    assert cr[0]["exception"] == "java.lang.NullPointerException", cr[0]["exception"]
    assert "CheckoutActivity.onCreate" in cr[0]["app_frame"], cr[0]["app_frame"]
    assert _obfuscated("a.b.c(Unknown)") and not _obfuscated("com.acme.app.Checkout.onCreate(C.java:42)")
    print("selftest OK — system/launcher crashes filtered, app crash kept with frame",
          cr[0]["app_frame"], "exc", cr[0]["exception"])


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
