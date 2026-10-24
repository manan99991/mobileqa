"""The vocabulary of things the agent can do, and how to execute one on a Device."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from ..driver.base import Device, Element, ScreenState

GESTURES = ("tap", "double_tap", "triple_tap", "long_press", "two_finger_tap",
            "swipe_up", "swipe_down", "swipe_left", "swipe_right",
            "pinch_in", "pinch_out", "drag", "type", "clear",
            "back", "rotate", "background", "shake", "notifications", "offline", "wait", "done")

ELEMENT_GESTURES = {
    # What a human would try on each kind of control.
    "button": ["tap", "long_press", "double_tap"],
    "cell": ["tap", "long_press", "swipe_left", "swipe_right"],
    "link": ["tap", "long_press"],
    "tab": ["tap", "double_tap"],
    "switch": ["tap", "double_tap"],
    "checkbox": ["tap", "double_tap"],
    "input": ["type", "clear", "long_press"],
    "slider": ["drag", "tap"],
    "stepper": ["tap", "long_press"],
    "picker": ["swipe_up", "swipe_down", "tap"],
    "image": ["tap", "double_tap", "pinch_out", "pinch_in", "long_press"],
    "map": ["pinch_out", "pinch_in", "drag", "double_tap", "two_finger_tap"],
    "webview": ["swipe_up", "pinch_out", "tap"],
    "list": ["swipe_up", "swipe_down", "swipe_left", "swipe_right"],
    "text": ["tap", "long_press", "triple_tap"],
    "other": ["tap"],
}

SCREEN_GESTURES = ["swipe_up", "swipe_down", "rotate", "background", "back", "pinch_out", "shake", "notifications"]


@dataclass
class Action:
    kind: str
    element_id: str | None = None        # Element.id on the current screen
    element_name: str = ""
    text: str = ""                       # for type
    to_element_id: str | None = None     # for drag
    x: int | None = None
    y: int | None = None
    reason: str = ""
    expectation: str = ""                # what should happen, for the oracle
    data_kind: str = ""                  # valid/invalid/hostile... for typed input
    locator: list[tuple[str, str]] = field(default_factory=list)   # recorded for replay

    def describe(self) -> str:
        tgt = f" '{self.element_name}'" if self.element_name else ""
        if self.kind == "type":
            shown = self.text if len(self.text) < 40 else self.text[:37] + "..."
            return f"type {shown!r} into{tgt}"
        if self.kind == "drag":
            return f"drag{tgt} to {self.to_element_id or f'({self.x},{self.y})'}"
        if self.x is not None and not tgt:
            return f"{self.kind} at ({self.x},{self.y})"
        return f"{self.kind}{tgt}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["locator"] = [list(x) for x in self.locator]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Action":
        d = dict(d)
        d["locator"] = [tuple(x) for x in d.get("locator", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def resolve(action: Action, state: ScreenState) -> Element | None:
    if action.element_id is None:
        return None
    for e in state.elements:
        if e.id == action.element_id:
            return e
    return None


def execute(dev: Device, action: Action, state: ScreenState, settle_s: float = 0.6) -> None:
    """Perform the action with human-ish timing. Raises nothing for unknown kinds; logs instead."""
    el = resolve(action, state)
    if el is not None:
        action.element_name = action.element_name or el.name
        action.locator = el.locator_candidates()
    x = action.x if action.x is not None else (el.rect.cx if el else state.width // 2)
    y = action.y if action.y is not None else (el.rect.cy if el else state.height // 2)
    k = action.kind

    if k == "tap":
        dev.tap(x, y)
    elif k == "double_tap":
        dev.double_tap(x, y)
    elif k == "triple_tap":
        dev.triple_tap(x, y)
    elif k == "two_finger_tap":
        dev.two_finger_tap(x, y)
    elif k == "long_press":
        dev.long_press(x, y)
    elif k.startswith("swipe_"):
        direction = k.split("_", 1)[1]
        target = el if (el and el.rect.area > 0 and el.type in ("list", "cell", "picker", "webview", "image", "map")) else None
        dev.scroll({"up": "up", "down": "down", "left": "left", "right": "right"}[direction], state, target)
    elif k == "pinch_in":
        dev.pinch(x, y, 0.5)
    elif k == "pinch_out":
        dev.pinch(x, y, 2.0)
    elif k == "drag":
        if action.to_element_id:
            to = next((e for e in state.elements if e.id == action.to_element_id), None)
            tx, ty = (to.rect.cx, to.rect.cy) if to else (x, y + 300)
        else:
            tx, ty = (x + (el.rect.w // 3 if el else 200), y) if el and el.type == "slider" else (x, max(50, y - 400))
        dev.drag(x, y, tx, ty)
    elif k == "type":
        dev.type_text(action.text, el.xpath if el else None)
        time.sleep(0.2)
        dev.hide_keyboard()
    elif k == "clear":
        if el:
            dev.clear_text(el.xpath)
    elif k == "back":
        dev.back()
    elif k == "rotate":
        new = "LANDSCAPE" if state.orientation.upper().startswith("PORT") else "PORTRAIT"
        dev.rotate(new)
        time.sleep(1.0)
        dev.rotate("PORTRAIT" if new == "LANDSCAPE" else "LANDSCAPE")   # and back, like someone checking
    elif k == "background":
        dev.background_app(3)
    elif k == "shake":
        dev.shake()
    elif k == "notifications":
        dev.open_notifications()
        time.sleep(0.8)
        dev.back()
    elif k == "offline":
        dev.set_network(True)
        time.sleep(2.0)
        dev.tap(x, y)
        time.sleep(1.5)
        dev.set_network(False)
    elif k == "wait":
        time.sleep(2.0)
    elif k == "done":
        return
    time.sleep(settle_s)
