"""Replays stored cases on a fresh install. Finds elements by their recorded locators;
when the UI has changed, asks the brain to point at the equivalent element and
rewrites the case (self-healing). Runs the same UI/value/crash checks on every step."""
from __future__ import annotations

import difflib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..brain.actions import Action, execute
from ..brain.planner import Brain
from ..checks.ui import check_crash_logs, run_ui_checks
from ..checks.values import ValueMemory, run_value_checks
from ..config import Config
from ..driver.base import Device, Element, ScreenState
from ..findings import Finding, FindingSink
from .store import SuiteStore, TestCase

log = logging.getLogger("mobileqa.replay")


@dataclass
class CaseResult:
    case: TestCase
    result: str                   # pass | fail | healed | error | still_failing | fixed
    message: str = ""
    duration_s: float = 0.0
    screenshots: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    healed_steps: list[int] = field(default_factory=list)


class Replayer:
    def __init__(self, cfg: Config, dev: Device, brain: Brain, store: SuiteStore, run_dir: Path):
        self.cfg, self.dev, self.brain, self.store = cfg, dev, brain, store
        self.run_dir = run_dir
        (run_dir / "replay").mkdir(parents=True, exist_ok=True)
        self.findings = FindingSink(cfg.app.known_issues)
        self.results: list[CaseResult] = []

    # ---- locating ------------------------------------------------------------
    def locate(self, action: dict, state: ScreenState) -> tuple[Element | None, bool]:
        """Returns (element, healed)."""
        locs = [tuple(x) for x in action.get("locator", [])]
        name = action.get("element_name", "")
        for strat, val in locs:
            for e in state.elements:
                if strat in ("accessibility id", "id") and val and (e.resource_id == val or e.label == val):
                    return e, False
                if strat == "text" and val and e.text == val:
                    return e, False
        for strat, val in locs:
            if strat == "xpath":
                for e in state.elements:
                    if e.xpath == val and e.interactive:
                        return e, False
        # fuzzy: closest name among interactive elements
        if name:
            cands = state.interactive()
            best = difflib.get_close_matches(name, [c.name for c in cands], n=1, cutoff=0.75)
            if best:
                return next(c for c in cands if c.name == best[0]), True
        healed = self.brain.heal({"name": name, "locators": locs, "kind": action.get("kind")}, state)
        return healed, healed is not None

    # ---- one case ------------------------------------------------------------
    def run_case(self, case: TestCase) -> CaseResult:
        t0 = time.time()
        res = CaseResult(case, "pass")
        values = ValueMemory()
        self.dev.reset()
        time.sleep(self.cfg.run.launch_wait_s)
        shots_dir = self.run_dir / "replay"
        try:
            for i, step in enumerate(case.steps):
                state = self.dev.snapshot()
                shot = shots_dir / f"{case.id}-{i:02d}.png"
                if state.screenshot_png:
                    shot.write_bytes(state.screenshot_png)
                    res.screenshots.append(str(shot.relative_to(self.run_dir)))
                action = Action.from_dict(step.action)
                if action.kind not in ("wait", "back", "rotate", "background", "shake", "notifications", "offline", "done") and action.element_id is not None:
                    el, healed = self.locate(step.action, state)
                    if el is None:
                        res.result, res.message = "fail", f"step {i + 1}: could not find '{action.element_name}' on '{step.screen}'. Visible: {[e.name for e in state.interactive()][:12]}"
                        break
                    action.element_id = el.id
                    if healed:
                        res.healed_steps.append(i)
                        step.action["locator"] = [list(x) for x in el.locator_candidates()]
                        step.action["element_name"] = el.name
                        log.info("  healed step %d -> %s", i + 1, el.name)
                if action.kind == "wait":
                    time.sleep(min(1.5, self.cfg.run.launch_wait_s))
                else:
                    execute(self.dev, action, state, settle_s=self.cfg.run.settle_s)
                after = self.dev.snapshot()
                if not self.dev.app_in_foreground():
                    crash = check_crash_logs(self.dev.logs(), self.cfg.platform, after.fingerprint, step.expect_screen, i)
                    res.result, res.message = "fail", f"step {i + 1}: app left the foreground after {action.describe()}" + (f" — {crash[0].detail[:200]}" if crash else "")
                    res.findings += self._file(crash or [Finding("crash", "critical", "App exited during replay", res.message, after.fingerprint, step.expect_screen, step=i)], case)
                    break
                # checks
                fs = run_ui_checks(after, self.cfg.platform, step.expect_screen or step.screen, i)
                fs += run_value_checks(after, step.expect_screen or step.screen, i, values)
                fs += check_crash_logs(self.dev.logs(), self.cfg.platform, after.fingerprint, step.expect_screen, i)
                res.findings += self._file(fs, case)
                # expectations
                texts = " ".join(after.texts).lower()
                bad = [t for t in step.expect_absent if t.lower() in texts]
                if bad:
                    res.result, res.message = "fail", f"step {i + 1}: forbidden text visible: {bad}"
                    break
                if step.expect_texts:
                    hits = sum(1 for t in step.expect_texts if t.lower() in texts)
                    if hits == 0:
                        res.result, res.message = "fail", f"step {i + 1}: expected to see one of {step.expect_texts} on '{step.expect_screen}', saw {after.texts[:8]}"
                        break
            if res.result == "pass" and res.healed_steps:
                res.result = "healed"
        except Exception as e:  # noqa: BLE001
            res.result, res.message = "error", f"{type(e).__name__}: {e}"
            log.exception("replay error in %s", case.id)
        res.duration_s = round(time.time() - t0, 1)

        # bookkeeping on the stored case
        case.runs += 1
        case.last_run = time.strftime("%Y-%m-%d %H:%M")
        if case.status == "open_bug":
            if case.finding_key and case.finding_key in res.findings and res.result in ("pass", "healed"):
                res.result, res.message = "fail", f"the original defect still reproduces ({case.finding_key})"
            if res.result in ("pass", "healed"):
                res.result = "fixed"
                case.status = "active"            # bug fixed -> becomes a permanent regression guard
                case.notes = (case.notes + f"\nFixed as of {case.last_run}").strip()
            elif res.result == "fail":
                res.result = "still_failing"
        if res.result in ("pass", "healed", "fixed"):
            case.passes += 1
        if res.healed_steps:
            case.healed_count += len(res.healed_steps)
        case.last_result = res.result
        self.store.save(case)
        return res

    def _file(self, fs: list[Finding], case: TestCase) -> list[str]:
        keys = []
        for f in fs:
            f.source = "replay"
            f.repro = [f"case {case.id}"]
            self.findings.add(f)
            keys.append(f.key)
        return keys

    def run_all(self, only: list[str] | None = None) -> list[CaseResult]:
        cases = [c for c in self.store.load_all() if c.status in ("active", "open_bug")]
        if only:
            cases = [c for c in cases if any(o in c.id or o in c.tags for o in only)]
        log.info("replaying %d cases", len(cases))
        for c in cases:
            r = self.run_case(c)
            log.info("  %-8s %s (%ss) %s", r.result.upper(), c.name, r.duration_s, r.message[:100])
            self.results.append(r)
        return self.results

    def summary(self) -> dict:
        s = {"total": len(self.results)}
        for k in ("pass", "healed", "fail", "error", "still_failing", "fixed"):
            s[k] = sum(1 for r in self.results if r.result == k)
        return s
