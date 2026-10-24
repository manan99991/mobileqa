"""A simulated phone + app used by the unit tests and `mobileqa demo`.

It renders Android-style page source so the real parser is exercised, and it plants
realistic defects (dead link, wrong total, truncated text, tiny tap target, crash
button, overlapping views, missing content-desc) so the checks have something to find.
No Appium needed.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from .base import Device, ScreenState
from .parse import parse_source

W, H = 1080, 2200


@dataclass
class Node:
    cls: str
    bounds: tuple[int, int, int, int]
    text: str = ""
    desc: str = ""
    rid: str = ""
    clickable: bool = False
    checkable: bool = False
    checked: bool = False
    scrollable: bool = False
    enabled: bool = True
    password: bool = False
    children: list["Node"] = field(default_factory=list)
    on_tap: str | None = None        # screen to navigate to, or "@crash", "@toggle", "@back"
    on_long: str | None = None

    def xml(self) -> str:
        x1, y1, x2, y2 = self.bounds
        attrs = (f'class="{self.cls}" text="{_esc(self.text)}" content-desc="{_esc(self.desc)}" '
                 f'resource-id="{"com.demo:id/" + self.rid if self.rid else ""}" '
                 f'bounds="[{x1},{y1}][{x2},{y2}]" clickable="{str(self.clickable).lower()}" '
                 f'long-clickable="{str(bool(self.on_long)).lower()}" '
                 f'checkable="{str(self.checkable).lower()}" checked="{str(self.checked).lower()}" '
                 f'scrollable="{str(self.scrollable).lower()}" enabled="{str(self.enabled).lower()}" '
                 f'password="{str(self.password).lower()}" displayed="true" focused="false"')
        inner = "".join(c.xml() for c in self.children)
        return f"<{self.cls} {attrs}>{inner}</{self.cls}>"

    def hit(self, x: int, y: int) -> "Node | None":
        """Deepest node under (x, y) that reacts to a tap."""
        x1, y1, x2, y2 = self.bounds
        if not (x1 <= x < x2 and y1 <= y < y2):
            return None
        for c in reversed(self.children):
            h = c.hit(x, y)
            if h:
                return h
        return self if (self.clickable or self.checkable or self.cls.endswith("EditText")) else None


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def T(cls, b, **kw):  # noqa: N802 - tiny constructor helper
    return Node(cls=f"android.widget.{cls}", bounds=b, **kw)


class FakeDevice(Device):
    """Simulated shopping app with planted bugs."""
    platform = "android"

    def __init__(self, crash_on_checkout: bool = True):
        self.screen = "login"
        self.logged_in = False
        self.crashed = False
        self.dark_mode = False
        self.orientation = "PORTRAIT"
        self.typed: dict[str, str] = {}
        self.scroll_offset = 0
        self.crash_on_checkout = crash_on_checkout
        self._logs: list[str] = []
        self.action_log: list[str] = []
        self.launched = False

    # ---- scenes -----------------------------------------------------------
    def _tree(self) -> Node:
        root = T("FrameLayout", (0, 0, W, H))
        if self.crashed or self.screen == "home_launcher":
            root.children.append(T("TextView", (0, 900, W, 1000), text="Demo Shop keeps stopping",
                                   rid="aerr_title"))
            root.children.append(T("Button", (300, 1100, 780, 1250), text="Close app", clickable=True, on_tap="@relaunch"))
            return root
        s = self.screen
        root.children.append(T("Toolbar", (0, 0, W, 160), rid="toolbar", children=[
            T("TextView", (60, 40, 800, 120), text=_titles[s], rid="title")]))
        if s == "login":
            root.children += [
                T("EditText", (80, 500, 1000, 640), rid="email", text=self.typed.get("email", ""), desc="Email"),
                T("EditText", (80, 700, 1000, 840), rid="password", text=self.typed.get("password", ""), password=True),  # no content-desc: a11y bug
                T("Button", (80, 940, 1000, 1080), rid="login_btn", text="Sign in", clickable=True, on_tap="@login"),
                T("TextView", (80, 1150, 1000, 1210), rid="forgot", text="Forgot password? https://demo-shop.example.invalid/reset", clickable=True, on_tap="login"),
                T("TextView", (80, 1250, 1000, 1310), rid="error", text=self.typed.get("error", "")),
            ]
        elif s == "home":
            rows = []
            items = [("Blue Jacket", "$49.99"), ("Running Shoes", "$89.00"), ("Water Bottle", "$12.50"),
                     ("Backpack", "$64.00"), ("Sunglasses", "$120.00")]
            for i, (name, price) in enumerate(items):
                y = 220 + i * 260 - self.scroll_offset
                if y < 160 or y > 1900:
                    continue
                rows.append(T("LinearLayout", (0, y, W, y + 240), rid=f"row_{i}", clickable=True, on_tap="detail",
                              on_long="context", children=[
                    T("ImageView", (40, y + 20, 240, y + 220), rid="thumb"),  # no content-desc
                    T("TextView", (280, y + 40, 1000, y + 110), text=name if i != 1 else "Running Shoes Ultra Light Edi…", rid="name"),
                    T("TextView", (280, y + 130, 600, y + 190), text=price, rid="price"),
                ]))
            root.children.append(T("RecyclerView", (0, 160, W, 1980), rid="list", scrollable=True, children=rows))
            root.children.append(self._tabbar("home"))
        elif s == "detail":
            root.children += [
                T("ImageView", (0, 160, W, 900), rid="hero", desc="Blue Jacket photo"),
                T("TextView", (60, 940, 1020, 1020), text="Blue Jacket", rid="name"),
                T("TextView", (60, 1040, 500, 1100), text="Price: $49.99", rid="price"),
                T("TextView", (60, 1120, 500, 1180), text="Qty: 2", rid="qty"),
                T("TextView", (60, 1200, 700, 1260), text="Total: $89.98", rid="total"),  # wrong: 2 × 49.99 = 99.98
                T("TextView", (60, 1300, 1020, 1360), text="Size guide: https://httpbin.org/status/404", rid="size_guide", clickable=True, on_tap="detail"),
                T("TextView", (60, 1380, 1020, 1440), text="Care: https://example.com", rid="care", clickable=True, on_tap="detail"),
                T("Button", (60, 1500, 1020, 1640), text="Add to cart", rid="add", clickable=True, on_tap="cart"),
                T("ImageButton", (1040, 170, 1070, 200), desc="Share", rid="share", clickable=True, on_tap="detail"),  # 30x30 px: tiny target
                T("Button", (60, 1700, 1020, 1840), text="Checkout", rid="checkout", clickable=True, on_tap="@crash" if self.crash_on_checkout else "cart"),
            ]
        elif s == "cart":
            root.children += [
                T("TextView", (60, 300, 1020, 380), text="1 item in your cart", rid="count"),
                T("TextView", (60, 400, 1020, 480), text="Blue Jacket  $49.99", rid="line1"),
                T("TextView", (60, 420, 1020, 500), text="Subtotal $49.99", rid="subtotal"),   # overlaps line1
                T("Button", (60, 1700, 1020, 1840), text="Continue shopping", rid="cont", clickable=True, on_tap="home"),
            ]
            root.children.append(self._tabbar("cart"))
        elif s == "settings":
            root.children += [
                T("TextView", (60, 300, 700, 380), text="Dark mode", rid="dark_label"),
                T("Switch", (800, 300, 1020, 380), rid="dark", checkable=True, checked=self.dark_mode, clickable=True, on_tap="@toggle"),
                T("TextView", (60, 450, 1020, 530), text="Lorem ipsum placeholder", rid="about"),
                T("TextView", (60, 560, 1020, 640), text="Version 1.2.3", rid="version"),
                T("Button", (60, 1700, 1020, 1840), text="Log out", rid="logout", clickable=True, on_tap="@logout"),
            ]
            root.children.append(self._tabbar("settings"))
        elif s == "context":
            root.children += [
                T("TextView", (100, 800, 980, 900), text="Add to wishlist", rid="wish", clickable=True, on_tap="home"),
                T("TextView", (100, 920, 980, 1020), text="Cancel", rid="cancel", clickable=True, on_tap="home"),
            ]
        return root

    def _tabbar(self, active: str) -> Node:
        return T("BottomNavigationView", (0, 1980, W, 2200), rid="tabs", children=[
            T("FrameLayout", (0, 1980, 360, 2200), desc="Home", clickable=True, on_tap="home", checkable=True, checked=active == "home"),
            T("FrameLayout", (360, 1980, 720, 2200), desc="Cart", clickable=True, on_tap="cart", checkable=True, checked=active == "cart"),
            T("FrameLayout", (720, 1980, W, 2200), desc="Settings", clickable=True, on_tap="settings", checkable=True, checked=active == "settings"),
        ])

    # ---- Device API --------------------------------------------------------
    def launch(self):
        self.launched = True
        self.crashed = False
        self.screen = "home" if self.logged_in else "login"

    def terminate(self):
        self.launched = False

    def reset(self):
        self.__init__(self.crash_on_checkout)
        self.launch()

    def quit(self):
        self.launched = False

    def app_in_foreground(self):
        return self.launched and not self.crashed

    def snapshot(self) -> ScreenState:
        xml = '<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">' + self._tree().xml() + "</hierarchy>"
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        for e in parse_source(xml, "android"):
            r = e.rect
            if e.text:
                d.text((r.x + 4, r.y + 4), e.text[:40], fill="black")
            if e.type == "button":
                d.rectangle([r.x, r.y, r.x + r.w, r.y + r.h], outline="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return ScreenState(elements=parse_source(xml, "android"), source=xml, screenshot_png=buf.getvalue(),
                           width=W, height=H, activity=f".{self.screen.title()}Activity",
                           orientation=self.orientation)

    def logs(self):
        out, self._logs = self._logs, []
        return out

    def _do(self, node: Node | None, kind: str):
        if node is None:
            return
        target = node.on_long if kind == "long" else node.on_tap
        if kind == "long" and not target:
            return
        if target == "@crash":
            self.crashed = True
            self._logs.append("E AndroidRuntime: FATAL EXCEPTION: main\nE AndroidRuntime: java.lang.NullPointerException at CheckoutActivity.onCreate")
        elif target == "@relaunch":
            self.launch()
        elif target == "@toggle":
            self.dark_mode = not self.dark_mode
        elif target == "@login":
            if "@" in self.typed.get("email", "") and self.typed.get("password"):
                self.logged_in = True
                self.screen = "home"
                self.typed.pop("error", None)
            else:
                self.typed["error"] = "Invalid credentials"
        elif target == "@logout":
            self.logged_in = False
            self.typed = {}
            self.screen = "login"
        elif target:
            self.screen = target
            self.scroll_offset = 0

    def tap(self, x, y):
        self.action_log.append(f"tap {x},{y}")
        self._do(self._tree().hit(x, y), "tap")

    def multi_tap(self, x, y, count, interval_ms=90):
        self.action_log.append(f"tap x{count} {x},{y}")
        n = self._tree().hit(x, y)
        if n and n.rid == "dark":           # double-tap toggles twice = no change, like real life
            for _ in range(count):
                self._do(n, "tap")
        else:
            self._do(n, "tap")

    def long_press(self, x, y, ms=1200):
        self.action_log.append(f"long {x},{y}")
        self._do(self._tree().hit(x, y), "long")

    def swipe(self, x1, y1, x2, y2, ms=350):
        self.action_log.append(f"swipe {x1},{y1}->{x2},{y2}")
        if self.screen == "home":
            if y2 < y1:
                self.scroll_offset = min(self.scroll_offset + 520, 520)
            elif y2 > y1:
                self.scroll_offset = 0
        if self.screen != "login" and x2 - x1 > 500 and x1 < 20:
            self.back()

    def drag(self, x1, y1, x2, y2, hold_ms=800, ms=900):
        self.action_log.append(f"drag {x1},{y1}->{x2},{y2}")

    def pinch(self, cx, cy, scale, ms=500):
        self.action_log.append(f"pinch {scale}")

    def two_finger_tap(self, x, y):
        self.action_log.append("two_finger_tap")

    def type_text(self, text, element_id=None):
        self.action_log.append(f"type {text!r} into {element_id}")
        for e in self.snapshot().elements:
            if e.xpath == element_id and e.editable:
                self.typed[e.resource_id] = self.typed.get(e.resource_id, "") + text
                return

    def clear_text(self, element_id):
        for e in self.snapshot().elements:
            if e.xpath == element_id:
                self.typed[e.resource_id] = ""

    def hide_keyboard(self):
        pass

    def back(self):
        self.action_log.append("back")
        if self.crashed:
            return
        if self.screen in ("detail", "context"):
            self.screen = "home"
        elif self.screen in ("cart", "settings"):
            self.screen = "home"
        elif self.screen == "home":
            self.launched = False          # backed out of the app, like a user would

    def home(self):
        self.launched = False

    def rotate(self, orientation):
        self.orientation = orientation.upper()

    def shake(self):
        pass

    def background_app(self, seconds=3):
        self.action_log.append("background")

    def open_notifications(self):
        pass

    def set_network(self, airplane):
        self.action_log.append(f"airplane={airplane}")


_titles = {"login": "Welcome back", "home": "Demo Shop", "detail": "Product", "cart": "Your cart",
           "settings": "Settings", "context": "Options"}
