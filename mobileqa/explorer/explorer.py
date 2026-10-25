"""The main loop: look → judge → decide → act → look again. Over and over, like a
tester with unlimited patience, until the app is exhausted or the budget is spent."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..brain.actions import Action, execute
from ..brain.planner import Brain, StepContext
from ..checks.links import LinkChecker
from ..checks.ui import check_crash_logs, run_ui_checks
from ..checks.values import ValueMemory, run_value_checks
from ..config import Config
from ..driver.base import Device, ScreenState
from ..findings import Finding, FindingSink
from .graph import AppGraph

log = logging.getLogger("mobileqa.explorer")

SYSTEM_DIALOG_OK = re.compile(r"^(allow|while using the app|only this time|ok|continue|got it|accept|agree|not now|no thanks|later|skip|dismiss|close)$", re.I)


@dataclass
class StepRecord:
    step: int
    screen_before: str
    screen_before_name: str
    action: dict
    screen_after: str
    screen_after_name: str
    ok: bool
    note: str
    findings: list[str]
    screenshot: str
    duration_ms: int


@dataclass
class RunResult:
    findings: FindingSink
    graph: AppGraph
    steps: list[StepRecord] = field(default_factory=list)
    started: float = 0.0
    ended: float = 0.0
    run_dir: Path | None = None
    stopped_reason: str = ""

    def summary(self) -> dict:
        return {"steps": len(self.steps), "duration_s": round(self.ended - self.started, 1),
                "findings": len(self.findings),
                "critical": self.findings.count("critical"), "high": self.findings.count("high"),
                "medium": self.findings.count("medium"), "low": self.findings.count("low"),
                **self.graph.stats(), "stopped": self.stopped_reason}


class Explorer:
    def __init__(self, cfg: Config, device: Device, brain: Brain, run_dir: Path, graph: AppGraph | None = None):
        self.cfg = cfg
        self.dev = device
        self.brain = brain
        self.run_dir = run_dir
        self.shots = run_dir / "screens"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.graph = graph or AppGraph()
        self.findings = FindingSink(cfg.app.known_issues)
        self.links = LinkChecker()
        self.values = ValueMemory()
        self.steps: list[StepRecord] = []
        self.history: list[str] = []
        self.repro: list[str] = []            # human-readable path since launch
        self._same_screen_streak = 0
        self._last_fp = ""
        # route-seeding: if the config lists deep-link routes, give each a slice of the budget
        self._routes: list[str] = list(cfg.app.routes)
        self._route_idx = -1
        self._route_step = 0
        self._route_budget = 0

    # ---- helpers ----------------------------------------------------------
    def _shot(self, state: ScreenState, step: int, tag: str = "") -> str:
        if not state.screenshot_png:
            return ""
        p = self.shots / f"{step:04d}{('_' + tag) if tag else ''}.png"
        p.write_bytes(state.screenshot_png)
        return str(p.relative_to(self.run_dir))

    def _check_screen(self, state: ScreenState, name: str, step: int) -> list[Finding]:
        fs: list[Finding] = []
        fs += run_ui_checks(state, self.cfg.platform, name, step)
        if self.cfg.run.check_values:
            fs += run_value_checks(state, name, step, self.values)
        fs += check_crash_logs(self.dev.logs(), self.cfg.platform, state.fingerprint, name, step)
        return fs

    def _add(self, fs: list[Finding], shot: str) -> list[str]:
        keys = []
        for f in fs:
            f.evidence.setdefault("screenshot", shot)
            if not f.repro:
                f.repro = list(self.repro[-12:])
            if self.findings.add(f):
                log.info("  ! %s/%s %s — %s", f.severity, f.category, f.title, f.detail[:90])
            keys.append(f.key)
        return keys

    def _handle_system_dialog(self, state: ScreenState) -> bool:
        """Permission prompts etc. — accept the way a user who wants to use the app would."""
        if self.dev.app_in_foreground() and not any(e.type == "alert" for e in state.elements):
            # Android permission dialogs are a different package => app not in foreground; iOS alerts are in-tree
            return False
        for e in state.interactive():
            if SYSTEM_DIALOG_OK.match(e.name.strip()):
                log.info("  system dialog: tapping %r", e.name)
                self.dev.tap(e.rect.cx, e.rect.cy)
                time.sleep(0.8)
                return True
        return False

    def _recover(self, step: int, name: str, fp: str, shot: str) -> ScreenState:
        """App left the foreground. Crash? Backgrounded by our own action? Either way, come back."""
        logs = self.dev.logs()
        crash = check_crash_logs(logs, self.cfg.platform, fp, name, step)
        last = self.history[-1] if self.history else ""
        if crash:
            self._add(crash, shot)
        elif not any(k in last for k in ("back", "background", "home", "notifications")):
            self._add([Finding("crash", "critical", "App left the foreground unexpectedly",
                               f"After '{last}' on '{name}' the app was no longer in front (crash or unexpected exit). Logs: {' | '.join(logs[-3:])[:400]}",
                               fp, name, step=step)], shot)
        self.dev.launch()
        time.sleep(self.cfg.run.launch_wait_s)
        self.repro = ["(relaunch app)"]
        return self.dev.snapshot()

    def _seed_next_route(self, step: int) -> ScreenState | None:
        """Deep-link to the next configured route and return its first screen, or None once the
        routes are exhausted. This lets a tap-only heuristic brain reach deep screens — e.g. one
        per tool — that it would never navigate to by tapping, giving per-screen coverage for free.
        A route that fails to open is logged as a finding and skipped."""
        while self._route_idx + 1 < len(self._routes):
            self._route_idx += 1
            route = self._routes[self._route_idx]
            log.info("  seed route %d/%d: %s", self._route_idx + 1, len(self._routes), route)
            self.repro = [f"(deep link to {route})"]
            try:
                self.dev.launch_route(route)
            except Exception as e:  # noqa: BLE001
                self._add([Finding("functional", "medium", "Deep link failed to open",
                                   f"Route {route!r} did not open: {type(e).__name__}: {str(e)[:160]}",
                                   "", route, step=step)], "")
                continue
            time.sleep(self.cfg.run.launch_wait_s)
            self._route_step = 0
            return self.dev.snapshot()
        return None

    # ---- main loop ----------------------------------------------------------
    def run(self) -> RunResult:
        cfg = self.cfg.run
        result = RunResult(self.findings, self.graph, self.steps, started=time.time(), run_dir=self.run_dir)
        log.info("launching %s on %s", self.cfg.app.name, self.cfg.platform)
        self.dev.launch()
        time.sleep(cfg.launch_wait_s)
        state = self.dev.snapshot()
        if self._routes:
            # split the budget across the routes, keeping a share for free exploration at the end
            self._route_budget = max(3, cfg.max_steps // (len(self._routes) + 1))
            seeded = self._seed_next_route(0)
            if seeded is not None:
                state = seeded
        prev_state: ScreenState | None = None
        prev_action: Action | None = None
        prev_name = ""
        deadline = result.started + cfg.max_minutes * 60
        vision_every = 6

        for step in range(1, cfg.max_steps + 1):
            if time.time() > deadline:
                result.stopped_reason = "time budget"
                break
            # route-seeded mode: give each deep-linked route a slice of the budget, then move to
            # the next; once the routes run out, fall through to normal free exploration.
            if self._routes:
                self._route_step += 1
                if self._route_step > self._route_budget:
                    nxt = self._seed_next_route(step)
                    if nxt is not None:
                        state, prev_state, prev_action = nxt, None, None
                        continue
                    self._routes = []   # exhausted — explore freely with whatever budget is left
            t0 = time.time()
            shot = self._shot(state, step)

            # 0) not in app? (crash / permission dialog / backed out)
            if not self.dev.app_in_foreground():
                if self._handle_system_dialog(state):
                    state = self.dev.snapshot()
                    continue
                state = self._recover(step, prev_name, state.fingerprint, shot)
                prev_state, prev_action = None, None
                continue
            if self._handle_system_dialog(state):
                state = self.dev.snapshot()
                continue

            # 1) perceive + name + coverage
            existing = self.graph.nodes.get(state.fingerprint)
            hint = existing.name if existing else (prev_name if prev_state and prev_state.fingerprint == state.fingerprint else "")
            node, transition = self.graph.visit(state, hint, step, shot)
            if prev_state and prev_state.fingerprint == state.fingerprint:
                transition = "same screen"
                self._same_screen_streak += 1
            else:
                self._same_screen_streak = 0

            # 2) deterministic checks
            fs = self._check_screen(state, node.name, step)

            # 3) ask the brain: judge last step, name screen, find issues, pick next action
            ctx = StepContext(cfg=self.cfg, step=step, state=state, prev_state=prev_state, prev_action=prev_action,
                              prev_screen_name=prev_name, transition=transition,
                              coverage=self.graph.coverage_for(state.fingerprint),
                              visited=self.graph.visited_names(), frontier=self.graph.frontier(),
                              history=self.history, screenshot=state.screenshot_png if cfg.check_vision else None,
                              screen_name_hint=node.name if not node.name.startswith("Screen ") else "")
            res = self.brain.step(ctx)
            if res.screen_name and (node.name.startswith("Screen ") or transition == "new screen"):
                node.name = res.screen_name
            for f in fs:
                if f.screen_name.startswith("Screen ") or not f.screen_name:
                    f.screen_name = node.name
            if cfg.check_links:
                self.links.collect(state.texts + [e.resource_id for e in state.elements], node.name)
            fs += res.findings
            if prev_action is not None and not res.verdict_ok and res.verdict_note:
                fs.append(Finding("functional", "medium", f"Unexpected result after {prev_action.kind}",
                                  f"Action: {prev_action.describe()}. Expected: {prev_action.expectation}. Observed: {res.verdict_note}",
                                  state.fingerprint, node.name, prev_action.element_name, source="model", step=step))

            # 4) a designer's look at new screens (and periodically)
            if cfg.check_vision and state.screenshot_png and (transition == "new screen" or step % vision_every == 0):
                vfs, urls = self.brain.inspect_screenshot(state.screenshot_png, self.cfg.platform, node.name, state.fingerprint, step)
                fs += vfs
                self.links.collect(urls, node.name)

            keys = self._add(fs, shot)

            # 5) stuck? nudge
            action = res.action
            if self._same_screen_streak >= 8 and action.kind not in ("back", "done"):
                log.info("  stuck on %s for %d steps — pressing back", node.name, self._same_screen_streak)
                action = Action("back", reason="stuck", expectation="leave this screen")
                self._same_screen_streak = 0
            if action.kind == "done":
                if self._routes:   # this route is done, but others remain — jump to the next
                    nxt = self._seed_next_route(step)
                    if nxt is not None:
                        state, prev_state, prev_action, prev_name = nxt, None, None, ""
                        continue
                    self._routes = []
                result.stopped_reason = "brain says app exhausted"
                self.steps.append(StepRecord(step, state.fingerprint, node.name, action.to_dict(), state.fingerprint, node.name,
                                             True, res.verdict_note, keys, shot, int((time.time() - t0) * 1000)))
                break

            # 6) act
            log.info("step %d [%s] %s  — %s", step, node.name, action.describe(), action.reason[:60])
            before_fp, before_name = state.fingerprint, node.name
            try:
                execute(self.dev, action, state, settle_s=cfg.settle_s)
            except Exception as e:  # noqa: BLE001
                log.warning("  action failed: %s", e)
                self._add([Finding("functional", "low", "Gesture could not be performed",
                                   f"{action.describe()} raised {type(e).__name__}: {str(e)[:200]}",
                                   before_fp, before_name, action.element_name, step=step)], shot)
            self.history.append(action.describe())
            self.repro.append(f"On '{before_name}': {action.describe()}")

            # 7) look again, measure responsiveness
            t_act = time.time()
            new_state = self.dev.snapshot()
            dt = time.time() - t_act
            if dt > 6:
                self._add([Finding("perf", "medium", "Screen slow to respond",
                                   f"{dt:.1f}s to get a stable UI tree after {action.describe()} (hang or heavy screen).",
                                   new_state.fingerprint, before_name, step=step)], shot)
            after_name = self.graph.nodes[new_state.fingerprint].name if new_state.fingerprint in self.graph.nodes else ""
            self.graph.record(before_fp, action, new_state.fingerprint, step)
            self.steps.append(StepRecord(step, before_fp, before_name, action.to_dict(), new_state.fingerprint, after_name,
                                         res.verdict_ok, res.verdict_note, keys, shot, int((time.time() - t0) * 1000)))

            # 8) double-tap/triple-tap specific oracle: duplicate navigation / duplicate submits
            if action.kind in ("double_tap", "triple_tap") and new_state.fingerprint != before_fp:
                dup = [t for t in new_state.texts if re.search(r"(added|created|sent|submitted|ordered)\s+\d+|duplicate|twice", t, re.I)]
                if dup:
                    self._add([Finding("functional", "high", "Multi-tap caused duplicate action",
                                       f"{action.describe()} led to: {dup[:2]}", new_state.fingerprint, after_name, action.element_name, step=step)], shot)

            prev_state, prev_action, prev_name, state = state, action, before_name, new_state
            if step % 25 == 0:
                self._flush(result)
        else:
            result.stopped_reason = "step budget"

        # ---- wrap up: links, persist
        if cfg.check_links:
            log.info("checking %d links", len([u for u in self.links.seen_on if u not in self.links.results]))
            for f in self.links.check_pending():
                f.screen_name = f.screen
                self.findings.add(f)
        result.ended = time.time()
        self._flush(result)
        return result

    def _flush(self, result: RunResult) -> None:
        self.graph.save(self.run_dir / "graph.json")
        (self.run_dir / "steps.json").write_text(json.dumps([asdict(s) for s in self.steps], indent=1))
        (self.run_dir / "findings.json").write_text(json.dumps([f.to_dict() for f in self.findings.by_severity()], indent=1))
        (self.run_dir / "links.json").write_text(json.dumps(
            {u: {"status": r[0], "note": r[1], "seconds": round(r[2], 2), "screens": sorted(self.links.seen_on.get(u, []))}
             for u, r in self.links.results.items()}, indent=1))
