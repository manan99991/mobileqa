"""Device abstraction. Everything above this layer is platform-agnostic.

A Device knows how to do what a human thumb does — tap, hold, double/triple tap,
swipe, fling, pinch, spread, drag, rotate the phone, type, go back/home — and how
to see: screenshot + the accessibility tree ("page source") turned into Elements.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    def intersects(self, o: "Rect") -> bool:
        return not (self.x + self.w <= o.x or o.x + o.w <= self.x or
                    self.y + self.h <= o.y or o.y + o.h <= self.y)

    def intersection_area(self, o: "Rect") -> int:
        ix = max(0, min(self.x + self.w, o.x + o.w) - max(self.x, o.x))
        iy = max(0, min(self.y + self.h, o.y + o.h) - max(self.y, o.y))
        return ix * iy

    def contains(self, o: "Rect") -> bool:
        return (o.x >= self.x and o.y >= self.y and
                o.x + o.w <= self.x + self.w and o.y + o.h <= self.y + self.h)


@dataclass
class Element:
    """One node of the UI tree, normalised across iOS and Android."""
    id: str                         # stable within a screen: index path
    type: str                       # normalised: button, text, input, image, switch, slider, list, cell, link, tab, other
    raw_type: str                   # XCUIElementTypeButton / android.widget.Button
    text: str = ""                  # visible label / value
    label: str = ""                 # accessibility label / content-desc
    resource_id: str = ""           # accessibilityIdentifier / resource-id
    rect: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    clickable: bool = False
    enabled: bool = True
    visible: bool = True
    focused: bool = False
    checked: bool | None = None
    scrollable: bool = False
    editable: bool = False
    password: bool = False
    depth: int = 0
    children: int = 0
    xpath: str = ""                 # absolute index-path xpath, always resolvable on this screen

    @property
    def name(self) -> str:
        return self.text or self.label or self.resource_id or self.raw_type.split(".")[-1]

    @property
    def interactive(self) -> bool:
        return self.enabled and self.visible and (
            self.clickable or self.editable or self.scrollable
            or self.type in ("button", "input", "switch", "slider", "tab", "link", "cell", "checkbox", "stepper", "picker"))

    def locator_candidates(self) -> list[tuple[str, str]]:
        """Ordered list of (strategy, value) we can use to find this element again."""
        cands: list[tuple[str, str]] = []
        if self.resource_id:
            cands.append(("accessibility id", self.resource_id))
            cands.append(("id", self.resource_id))
        if self.label:
            cands.append(("accessibility id", self.label))
        if self.text:
            cands.append(("text", self.text))
        if self.xpath:
            cands.append(("xpath", self.xpath))
        return cands

    def short(self) -> str:
        bits = [self.type, repr(self.name[:40])]
        if self.checked is not None:
            bits.append("checked" if self.checked else "unchecked")
        if not self.enabled:
            bits.append("disabled")
        return " ".join(bits)


@dataclass
class ScreenState:
    """A snapshot of what's on screen."""
    elements: list[Element]
    source: str
    screenshot_png: bytes | None
    width: int
    height: int
    activity: str = ""              # Android activity / iOS top view controller if known
    keyboard_shown: bool = False
    orientation: str = "PORTRAIT"

    @property
    def fingerprint(self) -> str:
        """Structural hash: same layout = same screen, even if dynamic text differs."""
        sig = []
        for e in self.elements:
            # Bucket positions so minor layout jitter does not create new screens.
            sig.append(f"{e.type}:{e.resource_id or _norm(e.label) or _norm(e.text)}:{e.rect.x // 40}:{e.rect.y // 40}")
        sig.sort()
        return hashlib.sha1("|".join(sig).encode()).hexdigest()[:12]

    @property
    def texts(self) -> list[str]:
        out = []
        for e in self.elements:
            for t in (e.text, e.label):
                if t and t not in out:
                    out.append(t)
        return out

    def interactive(self) -> list[Element]:
        return [e for e in self.elements if e.interactive and e.rect.area > 0]

    def find(self, **kw) -> Element | None:
        for e in self.elements:
            if all(getattr(e, k) == v for k, v in kw.items()):
                return e
        return None

    def summary(self, limit: int = 60) -> str:
        """Compact text for the model: one line per interactive/visible element."""
        lines = []
        for e in self.elements:
            if not e.visible or e.rect.area == 0:
                continue
            if not (e.interactive or e.text or e.label):
                continue
            flags = []
            if e.interactive:
                flags.append("I")
            if e.editable:
                flags.append("E")
            if e.scrollable:
                flags.append("S")
            if e.checked is not None:
                flags.append("on" if e.checked else "off")
            if not e.enabled:
                flags.append("disabled")
            lines.append(f"[{e.id}] {e.type} {e.name[:60]!r} @({e.rect.cx},{e.rect.cy}) {e.rect.w}x{e.rect.h} {' '.join(flags)}")
        if len(lines) > limit:
            lines = lines[:limit] + [f"... {len(lines) - limit} more"]
        return "\n".join(lines)


_ws = re.compile(r"\s+")


def _norm(s: str) -> str:
    """Strip digits/times so counters and clocks don't make a 'new screen'."""
    s = re.sub(r"\d[\d.,:/-]*", "#", s or "")
    return _ws.sub(" ", s).strip().lower()[:30]


class Device(ABC):
    """What the explorer needs from a phone. Coordinates are in screen points/pixels
    consistent with the rects in ScreenState."""

    platform: str = "generic"

    # ---- lifecycle ---------------------------------------------------------
    @abstractmethod
    def launch(self) -> None: ...

    @abstractmethod
    def terminate(self) -> None: ...

    @abstractmethod
    def reset(self) -> None:
        """Clear app data and relaunch — a fresh user."""

    @abstractmethod
    def quit(self) -> None: ...

    @abstractmethod
    def app_in_foreground(self) -> bool: ...

    # ---- perception --------------------------------------------------------
    @abstractmethod
    def snapshot(self) -> ScreenState: ...

    @abstractmethod
    def logs(self) -> list[str]:
        """Recent device log lines (logcat / syslog) since last call; used for crash/ANR detection."""

    # ---- gestures ----------------------------------------------------------
    @abstractmethod
    def tap(self, x: int, y: int) -> None: ...

    @abstractmethod
    def multi_tap(self, x: int, y: int, count: int, interval_ms: int = 90) -> None: ...

    @abstractmethod
    def long_press(self, x: int, y: int, ms: int = 1200) -> None: ...

    @abstractmethod
    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 350) -> None: ...

    @abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int, hold_ms: int = 800, ms: int = 900) -> None: ...

    @abstractmethod
    def pinch(self, cx: int, cy: int, scale: float, ms: int = 500) -> None:
        """scale < 1 pinches in (zoom out), scale > 1 spreads (zoom in)."""

    @abstractmethod
    def two_finger_tap(self, x: int, y: int) -> None: ...

    @abstractmethod
    def type_text(self, text: str, element_id: str | None = None) -> None: ...

    @abstractmethod
    def clear_text(self, element_id: str) -> None: ...

    @abstractmethod
    def hide_keyboard(self) -> None: ...

    @abstractmethod
    def back(self) -> None: ...

    @abstractmethod
    def home(self) -> None: ...

    @abstractmethod
    def rotate(self, orientation: str) -> None: ...

    @abstractmethod
    def shake(self) -> None: ...

    @abstractmethod
    def background_app(self, seconds: int = 3) -> None: ...

    @abstractmethod
    def open_notifications(self) -> None: ...

    @abstractmethod
    def set_network(self, airplane: bool) -> None: ...

    # ---- convenience built on primitives ------------------------------------
    def double_tap(self, x: int, y: int) -> None:
        self.multi_tap(x, y, 2)

    def triple_tap(self, x: int, y: int) -> None:
        self.multi_tap(x, y, 3)

    def scroll(self, direction: str, state: ScreenState, element: Element | None = None, fraction: float = 0.6) -> None:
        """Human-style scroll inside an element (or whole screen)."""
        r = element.rect if element else Rect(0, int(state.height * 0.12), state.width, int(state.height * 0.76))
        dx = int(r.w * fraction / 2)
        dy = int(r.h * fraction / 2)
        cx, cy = r.cx, r.cy
        if direction == "up":        # finger moves up => content scrolls down
            self.swipe(cx, cy + dy, cx, cy - dy)
        elif direction == "down":
            self.swipe(cx, cy - dy, cx, cy + dy)
        elif direction == "left":
            self.swipe(cx + dx, cy, cx - dx, cy)
        elif direction == "right":
            self.swipe(cx - dx, cy, cx + dx, cy)

    def fling(self, direction: str, state: ScreenState) -> None:
        self.scroll(direction, state, fraction=0.9)
        # faster swipe == fling
