"""The tester's brain. ClaudeBrain thinks with the model; HeuristicBrain is the
no-API fallback (systematic but not clever). Both expose the same interface."""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field

from ..config import Config
from ..driver.base import Element, ScreenState
from ..findings import Finding
from . import prompts
from .actions import ELEMENT_GESTURES, GESTURES, SCREEN_GESTURES, Action
from ..checks.ui import looks_like_app_error
from .data import inputs_for, valid_value
from .llm import LLM

log = logging.getLogger("mobileqa.brain")


@dataclass
class StepContext:
    cfg: Config
    step: int
    state: ScreenState
    prev_state: ScreenState | None
    prev_action: Action | None
    prev_screen_name: str
    transition: str                      # "same screen" | "new screen" | "known screen"
    coverage: dict[str, list[str]]       # element name -> gestures done, on this screen
    visited: list[str]                   # screen names
    frontier: list[str]                  # screens with untried elements
    history: list[str]                   # recent action descriptions
    screenshot: bytes | None = None
    screen_name_hint: str = ""


@dataclass
class StepResult:
    action: Action
    screen_name: str
    findings: list[Finding] = field(default_factory=list)
    verdict_ok: bool = True
    verdict_note: str = ""


class Brain:
    def step(self, ctx: StepContext) -> StepResult:  # pragma: no cover - interface
        raise NotImplementedError

    def inspect_screenshot(self, png: bytes, platform: str, screen_name: str, fingerprint: str, step: int) -> tuple[list[Finding], list[str]]:
        return [], []

    def heal(self, recorded: dict, state: ScreenState) -> Element | None:
        return None


# --------------------------------------------------------------------------------------
class HeuristicBrain(Brain):
    """Coverage-driven explorer: least-exercised element first, every gesture in turn,
    forms filled valid-then-invalid, screen gestures sprinkled in."""

    def __init__(self, cfg: Config, seed: int = 7):
        self.cfg = cfg
        self.rnd = random.Random(seed)
        self.form_phase: dict[str, int] = {}     # screen fp -> which input data round we're on

    def name_screen(self, state: ScreenState, hint: str = "") -> str:
        if hint:
            return hint
        for e in state.elements:
            if e.type == "text" and e.text and e.rect.y < state.height * 0.15 and 2 <= len(e.text) <= 40:
                return e.text
        if state.activity:
            return re.sub(r"Activity$", "", state.activity.split(".")[-1]) or state.activity
        for e in state.elements:
            if e.text and len(e.text) <= 30:
                return e.text
        return f"Screen {state.fingerprint[:4]}"

    def step(self, ctx: StepContext) -> StepResult:
        st = ctx.state
        name = self.name_screen(st, ctx.screen_name_hint)
        forbidden = [f.lower() for f in self.cfg.app.forbidden_texts] if not self.cfg.app.allow_destructive else []
        interactive = [e for e in st.interactive()
                       if not any(f in (e.text + " " + e.label).lower() for f in forbidden)
                       and e.type not in ("keyboard",)]
        verdict_ok, note = True, ""
        if ctx.prev_action and ctx.prev_action.kind in ("tap",) and ctx.transition == "same screen" and ctx.prev_state:
            sig = lambda s: (s.texts, [(e.name, e.checked, e.enabled) for e in s.elements])  # noqa: E731
            if sig(ctx.prev_state) == sig(st):
                prev_el = next((e for e in ctx.prev_state.elements if e.id == ctx.prev_action.element_id), None)
                already_selected = bool(prev_el and prev_el.checked)   # tapping the active tab does nothing, fine
                note = "" if already_selected else "tap produced no visible change"
                verdict_ok = already_selected or ctx.prev_action.element_name.lower() in ("", "cancel", "close", "ok", "done")

        # 0) the app crashed to its error screen: take its recovery action (Try again / Retry /
        # Reload) if there is one, else go back — never spend the budget flailing on it.
        if looks_like_app_error(st.texts):
            retry = next((e for e in interactive
                          if re.search(r"try again|retry|reload|restart|go home", e.name, re.I)), None)
            if retry:
                return StepResult(Action("tap", retry.id, retry.name,
                                         reason="recover from the app's error screen",
                                         expectation="the app leaves the error screen"), name, [], verdict_ok, note)
            return StepResult(Action("back", reason="leave the app's error screen",
                                     expectation="a working screen returns"), name, [], verdict_ok, note)

        # 1) forms: fill every empty input with valid data, then submit
        inputs = [e for e in interactive if e.editable]
        empty_inputs = [e for e in inputs if not e.text or e.text.lower() in (e.label.lower(), e.resource_id.lower())]
        if empty_inputs and ctx.coverage.get(empty_inputs[0].name, []).count("type") < 1:
            e = empty_inputs[0]
            val = valid_value(e, self.cfg.app.login)
            return StepResult(Action("type", e.id, e.name, text=val, data_kind="valid",
                                     reason="fill form with valid data",
                                     expectation="field accepts the value and shows it"), name, [], verdict_ok, note)
        if inputs and all(ctx.coverage.get(e.name, []).count("type") >= 1 for e in inputs):
            submit = next((e for e in interactive if e.type == "button" and re.search(
                r"sign|log ?in|submit|continue|next|save|search|go|done|confirm|apply|send|create|register", e.name, re.I)), None)
            if submit and "tap" not in ctx.coverage.get(submit.name, []):
                return StepResult(Action("tap", submit.id, submit.name, reason="submit filled form",
                                         expectation="form submits; next screen or validation message"), name, [], verdict_ok, note)
            # second+ rounds: edge-case data
            rnd = self.form_phase.get(st.fingerprint, 0)
            if rnd < 3 and submit:
                self.form_phase[st.fingerprint] = rnd + 1
                e = inputs[rnd % len(inputs)]
                cands = [i for i in inputs_for(e, self.cfg.app.login) if i.kind != "valid"]
                inp = cands[rnd % len(cands)]
                return StepResult(Action("clear" if False else "type", e.id, e.name, text=inp.value, data_kind=inp.kind,
                                         reason=f"edge-case input ({inp.kind})",
                                         expectation="app validates gracefully; no crash; clear message"), name, [], verdict_ok, note)

        # 2) least-exercised interactive element, next untried gesture for its type
        best, best_gesture, best_score = None, None, 10 ** 9
        for e in interactive:
            done = ctx.coverage.get(e.name, [])
            allowed = [g for g in ELEMENT_GESTURES.get(e.type, ["tap"]) if g in self.cfg.run.gestures or g in ("tap", "type")]
            untried = [g for g in allowed if g not in done]
            if not untried:
                continue
            # prefer taps first (they open new screens), then richer gestures; prefer top-of-screen navigation last
            score = len(done) * 10 + (0 if untried[0] == "tap" else 3) + (5 if e.type in ("tab", "tabbar") else 0)
            if e.type == "list":
                score += 2
            if score < best_score:
                best, best_gesture, best_score = e, untried[0], score
        if best is not None:
            if best_gesture == "type":
                return StepResult(Action("type", best.id, best.name, text=valid_value(best, self.cfg.app.login), data_kind="valid",
                                         reason="exercise input", expectation="value appears in field"), name, [], verdict_ok, note)
            return StepResult(Action(best_gesture, best.id, best.name, reason="untried gesture on element",
                                     expectation=_expect(best_gesture, best)), name, [], verdict_ok, note)

        # 3) screen-level gestures not yet done here
        done_screen = ctx.coverage.get("@screen", [])
        for g in SCREEN_GESTURES:
            if g in self.cfg.run.gestures and g not in done_screen and g != "back":
                return StepResult(Action(g, None, "", reason="screen-level gesture",
                                         expectation="layout survives; state preserved"), name, [], verdict_ok, note)

        # 4) exhausted: go back toward the frontier, or finish
        if ctx.frontier:
            return StepResult(Action("back", reason="screen exhausted; return to under-explored screens",
                                     expectation="previous screen appears"), name, [], verdict_ok, note)
        return StepResult(Action("done", reason="everything reachable has been exercised"), name, [], verdict_ok, note)


def _expect(g: str, e: Element) -> str:
    return {
        "tap": f"'{e.name}' responds: navigates, toggles or shows feedback",
        "long_press": "context menu / tooltip / nothing — but no crash",
        "double_tap": "no duplicate action (e.g. two submits, double navigation)",
        "triple_tap": "no crash, no odd state",
        "swipe_left": "row actions or carousel move; no crash",
        "swipe_right": "row actions or back navigation; no crash",
        "swipe_up": "content scrolls; reaches an end without blank areas",
        "swipe_down": "content scrolls / pull-to-refresh works",
        "pinch_out": "image/map zooms in smoothly",
        "pinch_in": "zooms back out; nothing stuck",
        "drag": "slider value changes and is displayed",
        "type": "text appears in the field",
        "clear": "field empties; dependent state updates",
    }.get(g, "no crash; sensible behaviour")


# --------------------------------------------------------------------------------------
class ClaudeBrain(Brain):
    def __init__(self, cfg: Config, llm: LLM | None = None, vision: LLM | None = None):
        self.cfg = cfg
        self.llm = llm or LLM(cfg.run.model)
        self.vision = vision or LLM(cfg.run.vision_model)
        self.fallback = HeuristicBrain(cfg)

    def step(self, ctx: StepContext) -> StepResult:
        st = ctx.state
        creds = {k: ("***" if "pass" in k.lower() else v) for k, v in self.cfg.app.login.items()}
        user = prompts.STEP.format(
            app_name=self.cfg.app.name,
            goals="; ".join(self.cfg.app.goals) or "none given — explore everything",
            forbidden=", ".join(self.cfg.app.forbidden_texts) if not self.cfg.app.allow_destructive else "nothing",
            creds=(dict(self.cfg.app.login) if self.cfg.app.login else "none") if ctx.step < 3 or any(e.password for e in st.elements) else creds,
            prev_action=ctx.prev_action.describe() if ctx.prev_action else "none (app just launched)",
            prev_expectation=ctx.prev_action.expectation if ctx.prev_action else "-",
            prev_screen_name=ctx.prev_screen_name or "-",
            before=ctx.prev_state.summary(45) if ctx.prev_state else "-",
            transition=ctx.transition, orientation=st.orientation, keyboard="shown" if st.keyboard_shown else "hidden",
            after=st.summary(70),
            coverage={k: v for k, v in ctx.coverage.items()} or "nothing yet",
            visited=ctx.visited[-30:], frontier=ctx.frontier[:10], history=ctx.history[-8:],
            step=ctx.step, max_steps=self.cfg.run.max_steps,
            screenshot_note="A screenshot of the current screen is attached." if ctx.screenshot else "",
        )
        try:
            data = self.llm.json(prompts.SYSTEM.format(platform=self.cfg.platform), user,
                                 images=[ctx.screenshot] if ctx.screenshot else None)
        except Exception as e:  # noqa: BLE001
            log.warning("model step failed, using heuristic: %s", e)
            return self.fallback.step(ctx)

        a = data.get("action") or {}
        kind = a.get("kind", "tap")
        if kind not in GESTURES:
            kind = "tap"
        eid = a.get("element_id")
        eid = str(eid) if eid not in (None, "", "null") else None
        el = next((e for e in st.elements if e.id == eid), None) if eid else None
        if kind in ("tap", "double_tap", "triple_tap", "long_press", "type", "clear", "drag") and el is None and kind != "drag":
            log.info("model picked unknown element %r; falling back", eid)
            return self.fallback.step(ctx)
        # safety: refuse forbidden texts even if the model asked
        if el is not None and not self.cfg.app.allow_destructive and any(
                f.lower() in (el.text + " " + el.label).lower() for f in self.cfg.app.forbidden_texts):
            return self.fallback.step(ctx)
        action = Action(kind, el.id if el else None, el.name if el else "", text=str(a.get("text") or ""),
                        to_element_id=(str(a["to_element_id"]) if a.get("to_element_id") not in (None, "", "null") else None),
                        reason=str(a.get("reason", "")), expectation=str(a.get("expectation", "")),
                        data_kind="model")
        name = str(data.get("screen_name") or self.fallback.name_screen(st, ctx.screen_name_hint))[:40]
        findings = [_finding_from(f, st.fingerprint, name, ctx.step, "model") for f in data.get("findings", []) if isinstance(f, dict)]
        v = data.get("verdict") or {}
        return StepResult(action, name, findings, bool(v.get("ok", True)), str(v.get("note", "")))

    def inspect_screenshot(self, png, platform, screen_name, fingerprint, step):
        if not self.vision.available:
            return [], []
        try:
            data = self.vision.json(prompts.VISION_SYSTEM.format(platform=platform, screen=screen_name),
                                    "Review this screen.", images=[png], max_tokens=1200)
        except Exception as e:  # noqa: BLE001
            log.warning("vision failed: %s", e)
            return [], []
        fs = [_finding_from(f, fingerprint, screen_name, step, "vision") for f in data.get("findings", []) if isinstance(f, dict)]
        return fs, [u for u in data.get("urls", []) if isinstance(u, str)]

    def heal(self, recorded: dict, state: ScreenState) -> Element | None:
        if not self.llm.available:
            return None
        try:
            data = self.llm.json(prompts.HEAL_SYSTEM, f"RECORDED ELEMENT: {recorded}\n\nCURRENT SCREEN:\n{state.summary(80)}", max_tokens=300)
        except Exception:  # noqa: BLE001
            return None
        eid = data.get("element_id")
        if eid in (None, "null", "") or float(data.get("confidence", 0)) < 0.6:
            return None
        return next((e for e in state.elements if e.id == str(eid)), None)


def _finding_from(f: dict, fp: str, screen: str, step: int, source: str) -> Finding:
    sev = str(f.get("severity", "medium")).lower()
    if sev not in ("critical", "high", "medium", "low", "info"):
        sev = "medium"
    cat = str(f.get("category", "ux")).lower()
    return Finding(cat, sev, str(f.get("title", "Issue"))[:120], str(f.get("detail", ""))[:1200],
                   fp, screen, str(f.get("element", ""))[:60], source=source, step=step)


def make_brain(cfg: Config) -> Brain:
    llm = LLM(cfg.run.model)
    if llm.available:
        return ClaudeBrain(cfg, llm, LLM(cfg.run.vision_model))
    log.warning("No LLM backend (set MOBILEQA_LLM_BASE_URL+MOBILEQA_LLM_API_KEY for a free vision model like Gemini, or ANTHROPIC_API_KEY) — running the heuristic brain (systematic, less insightful)")
    return HeuristicBrain(cfg)
