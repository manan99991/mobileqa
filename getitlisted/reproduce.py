#!/usr/bin/env python3
"""
Get It Listed — reproduce pass.

"Reproduce-or-it-didn't-happen." Replays the regression suite the explore run just
generated, N times on the already-installed app, and records how often each crash
recurs. trust.py uses this to label findings confirmed / intermittent / dropped.

Usage:  python reproduce.py <run_dir> <suite_dir> <times>
Writes: <run_dir>/regress.json  ->  [{"title","screen","reproduced":n,"of":m}]

A crash counts as reproduced on a replay if an app-category crash shows up on the
same screen. Coarse but conservative: we would rather label a real crash
"intermittent" than invent one. Runs only in CI (needs the emulator + Appium).
"""
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path

CFG = "getitlisted/app.blackbox.yaml"


def newest_report(before: set[str]) -> Path | None:
    dirs = {str(p) for p in Path("reports").glob("*/") if p.is_dir()}
    new = sorted(dirs - before)
    return Path(new[-1]) if new else None


def crash_screens(run: Path) -> list[str]:
    fj = run / "findings.json"
    if not fj.exists():
        return []
    return [ (f.get("screen_name") or f.get("screen") or "")
             for f in json.loads(fj.read_text())
             if f.get("category") == "crash" ]


def main():
    run_dir, suite_dir, times = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    # the crashes the exploration found, by screen
    orig = json.loads((run_dir / "findings.json").read_text()) if (run_dir / "findings.json").exists() else []
    crash_findings = [f for f in orig if f.get("category") == "crash"]
    if not crash_findings:
        (run_dir / "regress.json").write_text("[]")
        print("no crashes to reproduce")
        return

    hits_by_screen: dict[str, int] = {}
    for i in range(times):
        before = {str(p) for p in Path("reports").glob("*/") if p.is_dir()}
        print(f"-- replay {i+1}/{times}")
        try:
            subprocess.run([sys.executable, "-m", "mobileqa.cli", "regress", CFG],
                           timeout=600, check=False)
        except Exception as e:
            print("  replay error:", e); continue
        time.sleep(1)
        rpt = newest_report(before)
        screens = crash_screens(rpt) if rpt else []
        for s in set(screens):
            hits_by_screen[s] = hits_by_screen.get(s, 0) + 1

    out = []
    for f in crash_findings:
        screen = f.get("screen_name") or f.get("screen") or ""
        out.append({"title": f.get("title"), "screen": screen,
                    "reproduced": hits_by_screen.get(screen, 0), "of": times})
    (run_dir / "regress.json").write_text(json.dumps(out, indent=1))
    for o in out:
        print(f"  {o['title']} @ {o['screen']}: {o['reproduced']}/{o['of']}")


if __name__ == "__main__":
    main()
