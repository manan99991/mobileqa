"""mobileqa command line.

  mobileqa explore app.yaml            autonomous exploration; grows the suite; writes report
  mobileqa regress app.yaml            replay the stored suite (self-healing) against the current build
  mobileqa ci app.yaml                 regress + explore, exit 1 if the release gate fails  <- hook into CI/Fastlane
  mobileqa watch app.yaml --every 6h   keep running ci on a schedule (standalone monitoring)
  mobileqa demo                        run everything against the built-in simulated app (no phone needed)
  mobileqa doctor app.yaml             check Appium, device, API key
  mobileqa init                        write a starter app.yaml
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .config import AppConfig, Config, DeviceConfig, RunConfig, load_config
from .explorer.graph import AppGraph

log = logging.getLogger("mobileqa")


def _run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _brain_and_device(cfg: Config, fake: bool = False):
    from .brain.planner import make_brain
    if fake:
        from .driver.fake_device import FakeDevice
        dev = FakeDevice()
    else:
        from .driver.appium_device import AppiumDevice
        dev = AppiumDevice(cfg)
    return make_brain(cfg), dev


def _llm_calls(brain) -> int:
    llm = getattr(brain, "llm", None)
    vis = getattr(brain, "vision", None)
    return (getattr(llm, "calls", 0) or 0) + (getattr(vis, "calls", 0) or 0)


def cmd_explore(cfg: Config, fake: bool, do_replay: bool = False, mode: str = "explore") -> int:
    from .explorer.explorer import Explorer
    from .report.build import gate, write_report
    from .report.notify import notify
    from .suite.recorder import record_cases
    from .suite.replayer import Replayer
    from .suite.store import SuiteStore

    run_id = _run_id()
    base = Path(cfg.run.report_dir).resolve()
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    suite_dir = Path(cfg.run.suite_dir).resolve()
    store = SuiteStore(suite_dir)
    brain, dev = _brain_and_device(cfg, fake)
    replay_results = None
    result = None
    try:
        if do_replay:
            rp = Replayer(cfg, dev, brain, store, run_dir)
            replay_results = rp.run_all()
            log.info("replay: %s", rp.summary())
        graph = AppGraph.load(suite_dir / "graph.json")   # remember the app model across runs
        ex = Explorer(cfg, dev, brain, run_dir, graph=graph)
        result = ex.run()
        if replay_results:
            result.findings.extend(list(rp.findings.items.values()))
        graph.save(suite_dir / "graph.json")
        added = store.add_new(record_cases(result, run_id, cfg.app.name))
        log.info("suite: +%d new cases (%s)", len(added), store.stats())
    finally:
        try:
            dev.quit()
        except Exception:  # noqa: BLE001
            pass
    report = write_report(cfg, run_dir, run_id, mode, result, replay_results, store.stats(), len(added), _llm_calls(brain))
    blocked = gate(cfg, result, replay_results)
    notify(cfg, run_dir, result.summary(), [f.to_dict() for f in result.findings.by_severity()], report, blocked)
    latest = base / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink() if latest.is_symlink() else shutil.rmtree(latest)
    try:
        latest.symlink_to(run_dir, target_is_directory=True)
    except OSError:
        shutil.copytree(run_dir, latest)
    print(f"\n{'RELEASE BLOCKED' if blocked else 'RELEASE OK'} — {result.summary()}\nreport: {report}")
    return 1 if blocked else 0


def cmd_regress(cfg: Config, fake: bool) -> int:
    from .report.build import gate, write_report
    from .report.notify import notify
    from .suite.replayer import Replayer
    from .suite.store import SuiteStore

    run_id = _run_id()
    run_dir = Path(cfg.run.report_dir).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    store = SuiteStore(Path(cfg.run.suite_dir).resolve())
    if not store.load_all():
        print("suite is empty — run `mobileqa explore` first")
        return 2
    brain, dev = _brain_and_device(cfg, fake)
    try:
        rp = Replayer(cfg, dev, brain, store, run_dir)
        results = rp.run_all()
    finally:
        dev.quit()
    report = write_report(cfg, run_dir, run_id, "regress", None, results, store.stats(), 0, _llm_calls(brain))
    blocked = gate(cfg, None, results)
    notify(cfg, run_dir, rp.summary(), [f.to_dict() for f in rp.findings.by_severity()], report, blocked)
    print(f"\n{'RELEASE BLOCKED' if blocked else 'RELEASE OK'} — {rp.summary()}\nreport: {report}")
    return 1 if blocked else 0


def cmd_doctor(cfg: Config) -> int:
    import requests
    ok = True
    print(f"mobileqa {__version__}")
    print(f"platform: {cfg.platform}   app: {cfg.app.path or cfg.app.bundle_id or cfg.app.package}")
    try:
        r = requests.get(cfg.device.appium_url.rstrip("/") + "/status", timeout=5)
        print(f"appium: OK {r.json().get('value', {}).get('build', {}).get('version', '')} at {cfg.device.appium_url}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"appium: NOT reachable at {cfg.device.appium_url} ({e}). Start it with:  appium --relaxed-security")
    if cfg.platform == "android":
        if shutil.which("adb"):
            import subprocess
            out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout.strip().splitlines()[1:]
            devs = [l for l in out if l.strip() and "device" in l]
            print(f"adb devices: {devs or 'NONE — start an emulator or plug in a phone with USB debugging'}")
            ok = ok and bool(devs)
        else:
            print("adb: not on PATH (install Android platform-tools)")
            ok = False
    else:
        if shutil.which("xcrun"):
            import subprocess
            out = subprocess.run(["xcrun", "simctl", "list", "devices", "booted"], capture_output=True, text=True).stdout
            booted = [l.strip() for l in out.splitlines() if "Booted" in l]
            print(f"booted simulators: {booted or 'NONE — open Simulator.app or pass device.udid for a real phone'}")
        else:
            print("xcrun: not found — iOS needs a Mac with Xcode")
            ok = False
    key = os.environ.get("ANTHROPIC_API_KEY")
    print(f"ANTHROPIC_API_KEY: {'set — Claude brain active' if key else 'missing — heuristic brain only (less insight)'}")
    if cfg.app.path and not Path(cfg.app.path).exists():
        print(f"app binary not found: {cfg.app.path}")
        ok = False
    print("\nready" if ok else "\nfix the items above, then run again")
    return 0 if ok else 1


def cmd_init(path: Path, platform: str, app: str, name: str) -> int:
    body = f"""# mobileqa app config — one file per app per platform. ${{ENV_VAR}} is expanded.
app:
  name: {name or 'My App'}
  path: {app or '/path/to/app.apk  # or .app / .ipa; leave blank if already installed'}
  package: {'com.example.app' if platform == 'android' else ''}        # Android
  bundle_id: {'com.example.app' if platform == 'ios' else ''}         # iOS
  login:                      # optional test account; the agent logs in with these
    email: ${{QA_EMAIL}}
    password: ${{QA_PASSWORD}}
  base_urls: []               # web endpoints to link-check too
  allow_destructive: false    # delete account / purchases / sending messages
  goals:                      # business flows the agent must complete and verify
    - "Complete a search and open a result"
  known_issues: []            # substrings of finding titles to downgrade to info

device:
  platform: {platform}
  name: {'Pixel_8_API_34' if platform == 'android' else 'iPhone 15'}
  os_version: ""
  udid: ""                    # real device
  appium_url: http://127.0.0.1:4723
  # extra_caps: {{"appium:autoGrantPermissions": true}}

run:
  max_steps: 400
  max_minutes: 45
  check_links: true
  check_values: true
  check_a11y: true
  check_vision: true          # Claude looks at screenshots (needs ANTHROPIC_API_KEY)
  model: claude-sonnet-5-5
  suite_dir: suite
  report_dir: reports
  fail_on: [critical, high]
"""
    path.write_text(body)
    print(f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mobileqa", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("explore", "regress", "ci", "watch", "doctor"):
        sp = sub.add_parser(name)
        sp.add_argument("config", help="app.yaml")
        sp.add_argument("--fake", action="store_true", help="use the built-in simulated app instead of a device")
        sp.add_argument("--steps", type=int, help="override run.max_steps")
        sp.add_argument("--minutes", type=int, help="override run.max_minutes")
        sp.add_argument("--no-vision", action="store_true")
        if name == "watch":
            sp.add_argument("--every", default="6h", help="e.g. 30m, 6h, 1d")
    d = sub.add_parser("demo")
    d.add_argument("--steps", type=int, default=60)
    i = sub.add_parser("init")
    i.add_argument("--platform", choices=["android", "ios"], default="android")
    i.add_argument("--app", default="")
    i.add_argument("--name", default="")
    i.add_argument("--out", default="app.yaml")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s", stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if a.cmd == "init":
        return cmd_init(Path(a.out), a.platform, a.app, a.name)
    if a.cmd == "demo":
        cfg = Config(AppConfig(name="Demo Shop", login={"email": "qa@example.com", "password": "secret1"}),
                     DeviceConfig(platform="android"),
                     RunConfig(max_steps=a.steps, check_vision=False, settle_s=0.05, launch_wait_s=0.1,
                               suite_dir="demo_out/suite", report_dir="demo_out/reports"))
        return cmd_explore(cfg, fake=True, do_replay=True, mode="demo")

    cfg = load_config(a.config)
    if a.steps:
        cfg.run.max_steps = a.steps
    if a.minutes:
        cfg.run.max_minutes = a.minutes
    if a.no_vision:
        cfg.run.check_vision = False
    if a.fake:
        cfg.run.settle_s, cfg.run.launch_wait_s = 0.05, 0.1
    if a.cmd == "doctor":
        return cmd_doctor(cfg)
    if a.cmd == "explore":
        return cmd_explore(cfg, a.fake)
    if a.cmd == "regress":
        return cmd_regress(cfg, a.fake)
    if a.cmd == "ci":
        return cmd_explore(cfg, a.fake, do_replay=True, mode="ci")
    if a.cmd == "watch":
        unit = {"m": 60, "h": 3600, "d": 86400}[a.every[-1]]
        every = int(a.every[:-1]) * unit
        while True:
            rc = cmd_explore(cfg, a.fake, do_replay=True, mode="watch")
            log.info("watch: run finished rc=%s; next in %ss", rc, every)
            time.sleep(every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
