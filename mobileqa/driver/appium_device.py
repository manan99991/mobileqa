"""Appium-backed Device for iOS (XCUITest) and Android (UiAutomator2).

All gestures are W3C pointer actions, so they are real touches the OS cannot tell
from a finger: multi-finger pinch, double/triple tap, long press, drag with hold.
"""
from __future__ import annotations

import time

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

from ..config import Config
from .base import Device, ScreenState
from .parse import parse_source


class AppiumDevice(Device):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.platform = cfg.platform
        self.driver: webdriver.Remote | None = None
        self._size: tuple[int, int] | None = None
        self._log_type = "logcat" if self.platform == "android" else "syslog"

    # ---- lifecycle ---------------------------------------------------------
    def _options(self):
        d, a = self.cfg.device, self.cfg.app
        if self.platform == "ios":
            o = XCUITestOptions()
            o.automation_name = "XCUITest"
            if d.name:
                o.device_name = d.name
            if d.os_version:
                o.platform_version = d.os_version
            if d.udid:
                o.udid = d.udid
            if a.path:
                o.app = a.path
            elif a.bundle_id:
                o.bundle_id = a.bundle_id
            o.set_capability("appium:autoAcceptAlerts", False)   # we want to see and judge alerts
            o.set_capability("appium:simpleIsVisibleCheck", True)
            o.set_capability("appium:newCommandTimeout", 600)
            o.set_capability("appium:shouldUseSingletonTestManager", False)
            o.set_capability("appium:wdaLaunchTimeout", 180000)
        else:
            o = UiAutomator2Options()
            o.automation_name = "UiAutomator2"
            if d.name:
                o.device_name = d.name
            if d.udid:
                o.udid = d.udid
            if d.os_version:
                o.platform_version = d.os_version
            if a.path:
                o.app = a.path
            if a.package:
                o.app_package = a.package
            if a.activity:
                o.app_activity = a.activity
            o.set_capability("appium:autoGrantPermissions", False)  # permission prompts are part of the UX
            o.set_capability("appium:newCommandTimeout", 600)
            o.set_capability("appium:ignoreHiddenApiPolicyError", True)
            o.set_capability("appium:disableWindowAnimation", True)
            o.set_capability("appium:enforceXPath1", True)
        for k, v in d.extra_caps.items():
            o.set_capability(k if ":" in k else f"appium:{k}", v)
        return o

    @property
    def app_id(self) -> str:
        return self.cfg.app.bundle_id if self.platform == "ios" else self.cfg.app.package

    def launch(self) -> None:
        if self.driver is None:
            self.driver = webdriver.Remote(self.cfg.device.appium_url, options=self._options())
            self.driver.implicitly_wait(0)
            s = self.driver.get_window_size()
            self._size = (s["width"], s["height"])
            try:
                self.driver.get_log(self._log_type)  # drain
            except WebDriverException:
                pass
        else:
            if self.app_id:
                self.driver.activate_app(self.app_id)

    def terminate(self) -> None:
        if self.driver and self.app_id:
            try:
                self.driver.terminate_app(self.app_id)
            except WebDriverException:
                pass

    def reset(self) -> None:
        # In `ci` mode the Replayer runs before the Explorer, so reset() can be the first
        # thing to touch the device — open the session lazily instead of asserting it exists.
        # (launch() with no driver creates the Appium session and launches the app fresh,
        # which is exactly the clean state reset() wants for the first replay case.)
        if self.driver is None:
            self.launch()
            return
        if self.app_id:
            try:
                self.driver.terminate_app(self.app_id)
                if self.platform == "android":
                    self.driver.execute_script("mobile: clearApp", {"appId": self.app_id})
                else:
                    # iOS has no clear-data; reinstall if we have the bundle
                    if self.cfg.app.path:
                        self.driver.remove_app(self.app_id)
                        self.driver.install_app(self.cfg.app.path)
            except WebDriverException:
                pass
            self.driver.activate_app(self.app_id)

    def launch_route(self, route: str) -> None:
        # Open a specific route directly, so the tap-only heuristic brain can reach deep screens
        # (e.g. one per tool) it would never navigate to by tapping. A route is either a deep-link
        # URL (contains "://") or a token passed to the launch activity as a string extra —
        # the Android adb equivalent is:  am start -n <package>/<activity> -e <route_extra> <route>
        if self.driver is None:
            self.launch()
        a = self.cfg.app
        try:
            if "://" in route:
                args = {"url": route}
                args["package" if self.platform == "android" else "bundleId"] = self.app_id
                self.driver.execute_script("mobile: deepLink", args)
                return
            if self.platform != "android":
                raise RuntimeError("iOS route-seeding needs a URL deep link in app.routes")
            act = a.route_activity or a.activity
            if not act:
                raise RuntimeError("route-seeding needs app.activity (the activity that reads the route extra), or a URL route")
            component = act if "/" in act else f"{self.app_id}/{act if '.' in act else '.' + act}"
            # Deliver the route as a string extra exactly the way adb proves works:
            #   am start -n <component> -e <route_extra> <route>
            # UiAutomator2's `mobile: startActivity` extras are unreliable — the extra frequently
            # never reaches the app (observed: every seeded route landed back on home). `mobile:
            # shell` runs the real am command (Appium must be started with --relaxed-security) and
            # the extra arrives; fall back to startActivity only if shell is disabled.
            try:
                self.driver.execute_script(
                    "mobile: shell",
                    {"command": "am", "args": ["start", "-n", component, "-e", a.route_extra, route]})
            except WebDriverException:
                self.driver.execute_script("mobile: startActivity",
                                           {"intent": component, "extras": [["s", a.route_extra, route]]})
        except WebDriverException as e:
            raise RuntimeError(f"could not open route {route!r}: {e}") from e

    def quit(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            finally:
                self.driver = None

    def app_in_foreground(self) -> bool:
        assert self.driver
        if not self.app_id:
            return True
        try:
            return self.driver.query_app_state(self.app_id) == 4
        except WebDriverException:
            return False

    # ---- perception --------------------------------------------------------
    def snapshot(self) -> ScreenState:
        assert self.driver
        src = self.driver.page_source
        png = self.driver.get_screenshot_as_png() if self.cfg.run.screenshot_every_step else None
        w, h = self._size or (0, 0)
        elements = parse_source(src, self.platform)
        activity = ""
        kb = False
        try:
            if self.platform == "android":
                activity = self.driver.current_activity or ""
            kb = bool(self.driver.is_keyboard_shown())
        except WebDriverException:
            pass
        try:
            orient = self.driver.orientation
        except WebDriverException:
            orient = "PORTRAIT"
        return ScreenState(elements=elements, source=src, screenshot_png=png, width=w, height=h,
                           activity=activity, keyboard_shown=kb, orientation=orient)

    def logs(self) -> list[str]:
        assert self.driver
        try:
            return [e.get("message", "") for e in self.driver.get_log(self._log_type)]
        except WebDriverException:
            return []

    # ---- gestures (W3C actions) ---------------------------------------------
    def _finger(self, name="finger1") -> tuple[ActionBuilder, PointerInput]:
        assert self.driver
        f = PointerInput(interaction.POINTER_TOUCH, name)
        ab = ActionBuilder(self.driver, mouse=f)
        return ab, f

    def tap(self, x, y):
        ab, _ = self._finger()
        ab.pointer_action.move_to_location(x, y).pointer_down().pause(0.06).pointer_up()
        ab.perform()

    def multi_tap(self, x, y, count, interval_ms=90):
        ab, _ = self._finger()
        for i in range(count):
            ab.pointer_action.move_to_location(x, y).pointer_down().pause(0.05).pointer_up()
            if i < count - 1:
                ab.pointer_action.pause(interval_ms / 1000)
        ab.perform()

    def long_press(self, x, y, ms=1200):
        ab, _ = self._finger()
        ab.pointer_action.move_to_location(x, y).pointer_down().pause(ms / 1000).pointer_up()
        ab.perform()

    def swipe(self, x1, y1, x2, y2, ms=350):
        ab, _ = self._finger()
        ab.pointer_action.move_to_location(x1, y1).pointer_down().pause(0.05)
        # a few intermediate points so the OS sees velocity like a real finger
        steps = 6
        for i in range(1, steps + 1):
            ab.pointer_action.move_to_location(
                int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
            ab.pointer_action.pause(ms / 1000 / steps)
        ab.pointer_action.pointer_up()
        ab.perform()

    def drag(self, x1, y1, x2, y2, hold_ms=800, ms=900):
        ab, _ = self._finger()
        ab.pointer_action.move_to_location(x1, y1).pointer_down().pause(hold_ms / 1000)
        steps = 10
        for i in range(1, steps + 1):
            ab.pointer_action.move_to_location(
                int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
            ab.pointer_action.pause(ms / 1000 / steps)
        ab.pointer_action.pause(0.2).pointer_up()
        ab.perform()

    def pinch(self, cx, cy, scale, ms=500):
        assert self.driver
        # Prefer the platform-native gesture when available: more faithful than synthetic touches.
        try:
            if self.platform == "ios":
                self.driver.execute_script("mobile: pinch", {"scale": scale, "velocity": 1.0 if scale > 1 else -1.0})
                return
            self.driver.execute_script("mobile: pinchOpenGesture" if scale > 1 else "mobile: pinchCloseGesture", {
                "left": cx - 150, "top": cy - 150, "width": 300, "height": 300, "percent": 0.75})
            return
        except WebDriverException:
            pass
        # Fallback: two synthetic fingers moving apart (spread) or together (pinch).
        start = 40 if scale > 1 else 160
        end = 160 if scale > 1 else 40
        self._two_fingers(
            [(cx - int(start + (end - start) * i / 6), cy) for i in range(7)],
            [(cx + int(start + (end - start) * i / 6), cy) for i in range(7)],
            step_s=ms / 1000 / 6,
        )

    def two_finger_tap(self, x, y):
        assert self.driver
        if self.platform == "ios":
            try:
                self.driver.execute_script("mobile: twoFingerTap", {})
                return
            except WebDriverException:
                pass
        self._two_fingers([(x - 30, y)], [(x + 30, y)], step_s=0.06)

    def _two_fingers(self, path1: list[tuple[int, int]], path2: list[tuple[int, int]], step_s: float):
        """Drive two touch pointers along two paths in lock-step (W3C multi-pointer actions)."""
        assert self.driver
        ab = ActionBuilder(self.driver, mouse=PointerInput(interaction.POINTER_TOUCH, "f1"))
        f1 = ab.pointer_inputs[0]
        f2 = ab.add_pointer_input(interaction.POINTER_TOUCH, "f2")
        f1.create_pointer_move(x=path1[0][0], y=path1[0][1], duration=0)
        f2.create_pointer_move(x=path2[0][0], y=path2[0][1], duration=0)
        f1.create_pointer_down(button=0)
        f2.create_pointer_down(button=0)
        for p1, p2 in zip(path1[1:], path2[1:]):
            f1.create_pointer_move(x=p1[0], y=p1[1], duration=int(step_s * 1000))
            f2.create_pointer_move(x=p2[0], y=p2[1], duration=int(step_s * 1000))
        f1.create_pause(step_s)
        f2.create_pause(step_s)
        f1.create_pointer_up(button=0)
        f2.create_pointer_up(button=0)
        ab.perform()

    def _find(self, el_xpath: str):
        assert self.driver
        return self.driver.find_element(AppiumBy.XPATH, el_xpath)

    def type_text(self, text, element_id=None):
        assert self.driver
        target = None
        if element_id:
            try:
                target = self._find(element_id)
                target.click()
            except WebDriverException:
                target = None
        time.sleep(0.2)
        try:
            (target or self.driver.switch_to.active_element).send_keys(text)
        except WebDriverException:
            if self.platform == "android":
                self.driver.execute_script("mobile: type", {"text": text})
            else:
                self.driver.execute_script("mobile: keys", {"keys": list(text)})

    def clear_text(self, element_xpath):
        try:
            self._find(element_xpath).clear()
        except WebDriverException:
            pass

    def hide_keyboard(self):
        assert self.driver
        try:
            if self.driver.is_keyboard_shown():
                if self.platform == "android":
                    self.driver.hide_keyboard()
                else:
                    # iOS: tap "Done"/"Return" if present, else tap outside
                    try:
                        self.driver.find_element(AppiumBy.ACCESSIBILITY_ID, "Done").click()
                    except WebDriverException:
                        self.tap(self._size[0] // 2, 60)
        except WebDriverException:
            pass

    def back(self):
        assert self.driver
        if self.platform == "android":
            self.driver.back()
        else:
            # iOS has no system back: edge swipe like a human, then try the nav back button
            w, h = self._size
            self.swipe(4, h // 2, int(w * 0.8), h // 2, ms=300)

    def home(self):
        assert self.driver
        if self.platform == "android":
            self.driver.press_keycode(3)
        else:
            self.driver.execute_script("mobile: pressButton", {"name": "home"})

    def rotate(self, orientation):
        assert self.driver
        try:
            self.driver.orientation = orientation.upper()
        except WebDriverException:
            pass

    def shake(self):
        assert self.driver
        if self.platform == "ios":
            try:
                self.driver.shake()
            except WebDriverException:
                pass

    def background_app(self, seconds=3):
        assert self.driver
        self.driver.background_app(seconds)

    def open_notifications(self):
        assert self.driver
        if self.platform == "android":
            self.driver.open_notifications()
        else:
            w, _ = self._size
            self.swipe(w // 2, 2, w // 2, 600, ms=400)

    def set_network(self, airplane: bool):
        assert self.driver
        if self.platform == "android":
            try:
                self.driver.set_network_connection(1 if airplane else 6)
            except WebDriverException:
                pass
        # iOS simulators cannot toggle network from Appium; the explorer skips the offline check there.
