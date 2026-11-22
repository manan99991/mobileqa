#!/usr/bin/env python3
"""
Get It Listed — key-free Android test engine.

No AI, no API key, no cost. Drives the app over plain `adb` + `uiautomator`
(both ship with the free Android SDK) and tests it the way a careful QA
engineer would:

  1. Launch performance   cold-start time x3 (Android vitals: >5 s is "excessive")
  2. Systematic crawl     every reachable screen, every tappable control, forms
                          filled with sensible test data, empty-form submits,
                          rotation and background/resume on every new screen
  3. Accessibility        unlabeled buttons, tap targets under 48 dp (Google's
                          own Accessibility Scanner rules — objective, not taste)
  4. Integration points   every time the app hands off to another app (browser,
                          maps, share, mail) + the app's declared permissions,
                          deep links and exported components
  5. Stress               seeded random-input "monkey" runs (replayable exactly)
  6. Health               memory, frame jank, non-fatal errors the app logged

Every crash keeps the exact action trail, so `replay` mode can re-run it from a
clean install and prove it happens again (trust.py drops anything that doesn't).

Never taps purchase / payment / delete-account / sign-out style controls.

Usage:
  python crawler.py explore            # writes reports/gil-<ts>/
  python crawler.py replay <run_dir> 3 # writes <run_dir>/regress.json
  python crawler.py --selftest         # offline tests with a fake device

Env: GIL_PACKAGE (required), GIL_MINUTES (crawl budget, default 12),
     GIL_LOGIN_EMAIL / GIL_LOGIN_PASSWORD (optional test account),
     GIL_FORBIDDEN (extra comma-separated words never to tap).
"""
from __future__ import annotations
import hashlib, json, os, re, shlex, subprocess, sys, time
import xml.etree.ElementTree as ET
from pathlib import Path

PERMISSION_PKGS = ("com.android.permissioncontroller", "com.google.android.permissioncontroller",
                   "com.android.packageinstaller", "com.google.android.packageinstaller")
PICKER_PKGS = ("com.android.documentsui", "com.google.android.documentsui", "com.google.android.providers.media.module",
               "com.android.providers.media.module", "com.android.contacts", "com.google.android.contacts",
               "com.google.android.apps.photos", "com.android.gallery3d")
PICK_ITEM_RE = re.compile(r"(item_root|thumbnail|icon_thumb|grid_item|photo|media_item|contact_tile|cliv_name|name|title)", re.I)
PICK_ROOT_RE = re.compile(r"^(images|photos|downloads?|pictures|camera|videos|audio|documents|recent)$", re.I)
PICK_OK_RE = re.compile(r"^(select|done|open|add|ok|allow|choose|attach)$", re.I)
ALLOW_RE = re.compile(r"^(allow|while using the app|only this time|allow all the time|ok)$", re.I)
ANR_WAIT_RE = re.compile(r"^(wait)$", re.I)
FORBIDDEN_RE = re.compile(
    r"\b(buy|purchase|subscribe|subscription|pay|payment|checkout|check out|upgrade|go premium|premium|"
    r"delete|remove account|deactivate|close account|log ?out|sign ?out|unsubscribe|place order|"
    r"donate|call now|dial|report user|block user)\b", re.I)
SUBMIT_RE = re.compile(r"\b(submit|continue|next|sign ?in|log ?in|sign ?up|register|create account|"
                       r"save|send|done|confirm|get started|search|go)\b", re.I)
EXTRA_FORBIDDEN = [w.strip().lower() for w in os.environ.get("GIL_FORBIDDEN", "").split(",") if w.strip()]
NONFATAL_APP_BUGS = ("NullPointerException", "IllegalStateException", "IndexOutOfBoundsException",
                     "ClassCastException", "IllegalArgumentException", "NumberFormatException",
                     "JSONException", "SQLiteException", "ConcurrentModificationException",
                     "ArithmeticException", "UnsupportedOperationException", "OutOfMemoryError")
NETWORK_EXC = ("UnknownHostException", "SocketTimeoutException", "ConnectException", "SSLException",
               "SSLHandshakeException", "IOException")


# ───────────────────────────── device layer ─────────────────────────────
class Adb:
    def __init__(self, serial: str | None = None):
        self.base = ["adb"] + (["-s", serial] if serial else [])

    def sh(self, cmd: str, timeout: int = 30) -> str:
        try:
            r = subprocess.run(self.base + ["shell", cmd], capture_output=True, text=True,
                               timeout=timeout, errors="ignore")
            return r.stdout
        except subprocess.TimeoutExpired:
            return ""

    def pull(self, remote: str, local: str):
        subprocess.run(self.base + ["pull", remote, local], capture_output=True, timeout=60)

    def exec_out(self, cmd: str, timeout: int = 30) -> bytes:
        try:
            return subprocess.run(self.base + ["exec-out", cmd], capture_output=True,
                                  timeout=timeout).stdout
        except subprocess.TimeoutExpired:
            return b""

    # higher-level
    def dump(self) -> str:
        out = self.exec_out("uiautomator dump --compressed /dev/tty", timeout=25).decode("utf-8", "ignore")
        i, j = out.find("<?xml"), out.rfind("</hierarchy>")
        if i >= 0 and j > i:
            return out[i:j + len("</hierarchy>")]
        self.sh("uiautomator dump --compressed /sdcard/gil_ui.xml", timeout=25)
        return self.sh("cat /sdcard/gil_ui.xml")

    def screenshot(self, path: Path):
        data = self.exec_out("screencap -p", timeout=20)
        if data:
            path.write_bytes(data)

    def top_package(self) -> str:
        out = self.sh("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        m = re.search(r"\s([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.$]+", out)
        return m.group(1) if m else ""

    def top_activity(self) -> str:
        out = self.sh("dumpsys window | grep -E 'mCurrentFocus'")
        m = re.search(r"\s([a-zA-Z0-9_.]+/[a-zA-Z0-9_.$]+)", out)
        return m.group(1) if m else ""

    def tap(self, x: int, y: int): self.sh(f"input tap {x} {y}")
    def back(self): self.sh("input keyevent 4")
    def home(self): self.sh("input keyevent 3")
    def hide_keyboard(self): self.sh("input keyevent 111")  # ESCAPE closes IME without navigating

    def type_text(self, text: str, clear_len: int = 0):
        if clear_len:
            self.sh("input keyevent 123 " + " ".join(["67"] * min(clear_len + 2, 120)))
        safe = text.replace(" ", "%s")
        self.sh("input text " + shlex.quote(safe))

    def rotate(self, landscape: bool):
        self.sh("settings put system accelerometer_rotation 0")
        self.sh(f"settings put system user_rotation {1 if landscape else 0}")

    def crash_count(self, pkg: str) -> int:
        out = self.sh("logcat -d -b crash", timeout=30)
        return len(re.findall(r"Process:\s*" + re.escape(pkg) + r"\b", out))

    def anr_count(self, pkg: str) -> int:
        out = self.sh("logcat -d -b events | grep -c 'am_anr.*" + pkg + "'", timeout=30)
        try: return int(out.strip().splitlines()[-1])
        except Exception: return 0

    def crash_log(self) -> str:
        return self.sh("logcat -d -b crash", timeout=30)

    def launcher_component(self, pkg: str) -> str:
        out = self.sh(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {pkg}")
        lines = [l.strip() for l in out.splitlines() if "/" in l]
        return lines[-1] if lines else ""

    def launch(self, pkg: str):
        self.sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")

    def cold_start_ms(self, pkg: str, comp: str) -> int | None:
        self.sh(f"am force-stop {pkg}")
        time.sleep(1)
        out = self.sh(f"am start -W -n {comp}", timeout=60)
        m = re.search(r"TotalTime:\s*(\d+)", out)
        return int(m.group(1)) if m else None

    def clear_data(self, pkg: str): self.sh(f"pm clear {pkg}", timeout=60)
    def restart(self, pkg: str):
        self.sh(f"am force-stop {pkg}"); self.launch(pkg)
    def scroll(self, direction: str = "down"):
        self.sh("input swipe 540 1700 540 700 300" if direction == "down" else "input swipe 540 700 540 1700 300")
    def pick(self, kind: str = "image") -> bool:
        return pick_in_picker(self, parse_ui(self.dump(), ""), time.sleep, kind)
    def pids(self, pkg: str) -> list[str]: return self.sh(f"pidof {pkg}").split()
    def density(self) -> float:
        m = re.search(r"(\d+)\s*$", self.sh("wm density").strip().splitlines()[-1] if self.sh("wm density").strip() else "")
        return (int(m.group(1)) / 160.0) if m else 2.625


# ───────────────────────────── UI model ─────────────────────────────
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _has_label(node) -> bool:
    for n in node.iter():
        if (n.get("text") or "").strip() or (n.get("content-desc") or "").strip():
            return True
    return False


def parse_ui(xml: str, pkg: str) -> dict:
    """-> {'pkg_nodes': bool, 'elements': [...], 'fields': [...], 'raw_pkgs': set}"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {"elements": [], "pkgs": set(), "ok": False}
    pkgs, elements, texts = set(), [], []
    for idx, n in enumerate(root.iter("node")):
        p = n.get("package") or ""
        pkgs.add(p)
        m = BOUNDS_RE.match(n.get("bounds") or "")
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        t = (n.get("text") or "").strip()
        if t and p == pkg:
            texts.append((t, y1, y2 - y1, x1, x2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        cls = n.get("class") or ""
        clickable = n.get("clickable") == "true" or n.get("long-clickable") == "true"
        editable = "EditText" in cls or "AutoCompleteTextView" in cls
        if not (clickable or editable):
            continue
        if n.get("enabled") == "false":
            continue
        text = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        if not text and clickable and not editable:  # use first descendant text as the label
            for c in n.iter():
                t = (c.get("text") or "").strip() or (c.get("content-desc") or "").strip()
                if t:
                    text = t; break
        elements.append({
            "pkg": p, "cls": cls.split(".")[-1], "rid": (n.get("resource-id") or "").split("/")[-1],
            "text": text[:80], "desc": desc[:80], "hint": (n.get("hint") or "")[:60],
            "password": n.get("password") == "true", "editable": editable,
            "bounds": [x1, y1, x2, y2], "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
            "labeled": _has_label(n), "checkable": n.get("checkable") == "true", "idx": idx,
        })
    return {"elements": elements, "pkgs": pkgs, "ok": True, "texts": texts}


BROKEN_TEXT = re.compile(r"(^|\s)(null|undefined|NaN|nil)(\s|$)|%[sd@]|\{\d\}|\{\{.*\}\}|\bTODO\b|\bFIXME\b|lorem ipsum|"
                         r"@string/|R\.string\.|\$\{|\[object Object\]|java\.lang\.|Exception:")
NAV_WORDS = re.compile(r"^[‹<←\s]*(back|skip|clear|close|cancel|done|next|ok|menu|more|search|test entry \d+|photo)$", re.I)


def screen_title(ui: dict) -> str:
    """Name a screen by its heading: the biggest text in the top third that isn't a nav word."""
    texts = ui.get("texts") or []
    if not texts:
        return ""
    cands = [(t, y, h) for t, y, h, *_ in texts if y < 900 and 3 <= len(t) <= 40 and not NAV_WORDS.match(t)]
    if not cands:
        return ""
    cands.sort(key=lambda c: (-c[2], c[1]))
    return cands[0][0]


def el_label(e: dict) -> str:
    return e.get("text") or e.get("desc") or e.get("hint") or (e.get("rid") or "").replace("_", " ") or e.get("cls", "control")


def el_key(e: dict) -> str:
    return f'{e.get("rid")}|{e.get("text")}|{e.get("desc")}|{e.get("cls")}' if (e.get("rid") or e.get("text") or e.get("desc")) \
        else f'{e.get("cls")}@{e["cx"]//40},{e["cy"]//40}'


def screen_sig(activity: str, elements: list[dict]) -> str:
    """Structure-based identity: activity + the set of control ids/classes (not their text,
    so a list with different data is still 'the same screen')."""
    parts = sorted({f'{e["cls"]}:{e["rid"]}' for e in elements if e.get("rid")} or
                   {f'{e["cls"]}:{(e.get("text") or e.get("desc"))[:20]}' for e in elements})
    return hashlib.sha1((activity + "|" + "|".join(parts)).encode()).hexdigest()[:10]


def is_forbidden(e: dict) -> bool:
    blob = " ".join([e.get("text", ""), e.get("desc", ""), (e.get("rid") or "").replace("_", " ")]).lower()
    if FORBIDDEN_RE.search(blob):
        return True
    return any(w in blob for w in EXTRA_FORBIDDEN)


def test_value(e: dict) -> str:
    blob = " ".join([e.get("hint", ""), e.get("text", ""), e.get("desc", ""), e.get("rid", "")]).lower()
    if e.get("password") or "password" in blob or "passcode" in blob:
        return os.environ.get("GIL_LOGIN_PASSWORD") or "TestPass#2026"
    if "mail" in blob:
        return os.environ.get("GIL_LOGIN_EMAIL") or "qa.tester@example.com"
    if "user" in blob and os.environ.get("GIL_LOGIN_EMAIL"):
        return os.environ["GIL_LOGIN_EMAIL"]
    if any(k in blob for k in ("phone", "mobile", "tel")): return "5555550123"
    if any(k in blob for k in ("otp", "code", "pin", "verification")): return "123456"
    if any(k in blob for k in ("zip", "postal")): return "10001"
    if any(k in blob for k in ("url", "website", "link")): return "https://example.com"
    if any(k in blob for k in ("amount", "price", "qty", "quantity", "age", "number", "count", "weight", "height")): return "3"
    if "search" in blob: return "test"
    if "first" in blob: return "Alex"
    if "last" in blob or "surname" in blob: return "Tester"
    if "name" in blob: return "Alex Tester"
    if any(k in blob for k in ("city", "town")): return "Chicago"
    if "address" in blob: return "100 Main St"
    return "Test entry 1"


def a11y_issues(elements: list[dict], density: float) -> dict:
    unlabeled = [e for e in elements if not e["editable"] and not e["labeled"]]
    small = []
    for e in elements:
        x1, y1, x2, y2 = e["bounds"]
        w, h = (x2 - x1) / density, (y2 - y1) / density
        if (w < 48 or h < 48) and not e["editable"]:
            small.append((e, round(w), round(h)))
    return {"unlabeled": unlabeled, "small": small}


def parse_monkey(out: str, pkg: str) -> dict:
    crashed = bool(re.search(r"// CRASH: " + re.escape(pkg), out))
    anr = bool(re.search(r"// NOT RESPONDING: " + re.escape(pkg), out))
    m = re.search(r"Events injected:\s*(\d+)", out)
    return {"crash": crashed, "anr": anr, "events": int(m.group(1)) if m else None}


def parse_nonfatal(logcat: str, pids: set[str]) -> dict:
    """Group the app's own logged (caught) exceptions by class. Lines are filtered to the
    app's own process IDs so other apps / the OS never count."""
    bugs, net = {}, {}
    for line in logcat.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[2] not in pids or parts[4] not in ("E", "W"):
            continue
        m = re.search(r"([a-zA-Z0-9_.]+(?:Exception|Error))\b", line)
        if not m:
            continue
        name = m.group(1).split(".")[-1]
        bucket = bugs if name in NONFATAL_APP_BUGS else net if name in NETWORK_EXC else None
        if bucket is None:
            continue
        b = bucket.setdefault(name, {"count": 0, "sample": line.strip()[:300]})
        b["count"] += 1
    return {"bugs": bugs, "network": net}


# ───────────────────────────── the crawler ─────────────────────────────
class Crawler:
    def __init__(self, adb: Adb, pkg: str, out: Path, minutes: float = 12, sleep=time.sleep, clock=time.time):
        self.adb, self.pkg, self.out = adb, pkg, out
        self.deadline_s = minutes * 60
        self.sleep, self.clock = sleep, clock
        (out / "screens").mkdir(parents=True, exist_ok=True)
        self.findings: list[dict] = []
        self.screens: dict[str, dict] = {}     # sig -> {name, activity, shot, controls}
        self.tried: dict[str, dict[str, int]] = {}
        self.history: list[dict] = []          # every action since the clean install (replayable)
        self.path: list[str] = []              # human steps since the last (re)launch
        self.integrations: list[dict] = []
        self.steps = 0
        self.crashes_seen = 0
        self.anrs_seen = 0
        self.pids: set[str] = set()
        self.density = 2.625
        self.a11y_flagged: set[str] = set()
        self.perf: dict = {}
        self.slowest: list[tuple[float, str]] = []
        self.last_el = None
        self.picks = 0
        self.edges: dict = {}
        self.remaining: dict = {}
        self.last_move = None
        self.hops = 0
        self.cur_activity = ""
        self.cur_shot = ""

    # ── helpers
    def _act(self, kind: str, human: str, **kw):
        self.last_el = kw.get("el") if kind in ("tap", "type") else None
        self.history.append({"kind": kind, "human": human, **kw})
        self.path.append(human)
        self.steps += 1

    def _settle(self) -> tuple[dict, float]:
        t0 = self.clock()
        prev, ui = None, None
        for _ in range(2):
            xml = self.adb.dump()
            ui = parse_ui(xml, self.pkg)
            sig = tuple(el_key(e) for e in ui["elements"])
            if sig == prev and ui["ok"]:
                break
            prev = sig
            self.sleep(0.3)
        return ui, self.clock() - t0

    def _note_pids(self):
        self.pids.update(self.adb.pids(self.pkg))

    def _handle_system_dialogs(self, ui: dict) -> bool:
        """Permission prompts and 'app isn't responding' dialogs are answered automatically
        (and identically during replay), so they never derail the trail."""
        for e in ui["elements"]:
            if e["pkg"] in PERMISSION_PKGS and ALLOW_RE.match(e.get("text", "")):
                self.adb.tap(e["cx"], e["cy"]); self.sleep(1); return True
        for e in ui["elements"]:
            if e["pkg"] == "android" and ANR_WAIT_RE.match(e.get("text", "")):
                self.adb.tap(e["cx"], e["cy"]); self.sleep(2); return True
        return False

    def _check_failures(self, after: str, screen_name: str) -> bool:
        c = self.adb.crash_count(self.pkg)
        a = self.adb.anr_count(self.pkg)
        hit = False
        if c > self.crashes_seen:
            self.crashes_seen = c
            if any(f["title"] == f"App crashes after: {after}" for f in self.findings):
                return True
            shot = self.out / "screens" / f"crash_{len(self.findings)}.png"
            self.adb.screenshot(shot)
            log = self.adb.crash_log()
            exc = re.findall(r"^\s*([a-zA-Z0-9_.$]+(?:Exception|Error))(?::.*)?$", log, re.M)
            self.findings.append({
                "severity": "critical", "category": "crash",
                "title": f"App crashes after: {after}",
                "detail": f"The app closed unexpectedly right after this step"
                          + (f" ({exc[-1].split('.')[-1]})." if exc else "."),
                "screen_name": screen_name, "repro": ["Install the app fresh and open it"] + self.path[-15:],
                "evidence": {"screenshot": self.cur_shot or str(shot), "after_screenshot": str(shot),
                             "log": exc[-1] if exc else "", "t": time.time(),
                             "activity": self.cur_activity,
                             "elements": [_loc(self.last_el)] if self.last_el else []},
                "replay": list(self.history),
            })
            hit = True
        if a > self.anrs_seen:
            self.anrs_seen = a
            self.findings.append({
                "severity": "high", "category": "crash",
                "title": f"App froze (not responding) after: {after}",
                "detail": "Android showed an 'app isn't responding' error — the main thread was blocked for 5+ seconds.",
                "screen_name": screen_name, "repro": ["Install the app fresh and open it"] + self.path[-15:],
                "evidence": {"log": "ANR", "t": time.time(), "activity": self.cur_activity,
                             "screenshot": str(self._shot_now(f"anr_{len(self.findings)}")),
                             "elements": [_loc(self.last_el)] if self.last_el else []},
                "replay": list(self.history),
            })
            hit = True
        return hit

    def _relaunch(self, why: str):
        self.adb.launch(self.pkg)
        self.history.append({"kind": "launch", "human": "Open the app"})
        self.path = ["Open the app"]
        self.sleep(2.5)
        self._note_pids()

    def _record_screen(self, sig: str, activity: str, ui: dict) -> str:
        if sig in self.screens:
            return self.screens[sig]["name"]
        name = screen_title(ui) or (activity.split("/")[-1].split(".")[-1] or "Screen")
        if any(sc["name"] == name for sc in self.screens.values()):
            name = f"{name} ({sum(1 for sc in self.screens.values() if sc['name'].startswith(name)) + 1})"
        shot = self.out / "screens" / f"s{len(self.screens):02d}.png"
        self.adb.screenshot(shot)
        last_launch = max([i for i, h in enumerate(self.history) if h["kind"] == "launch"] or [0])
        self.screens[sig] = {"name": name, "activity": activity, "shot": str(shot),
                             "controls": len(ui["elements"]), "t": time.time(),
                             "ui": [{"label": el_label(e), "cls": e["cls"], "rid": e["rid"], "editable": e["editable"],
                                     "bounds": e["bounds"]} for e in ui["elements"] if e["pkg"] == self.pkg][:60],
                             "texts": [t for t, *_ in (ui.get("texts") or [])][:80],
                             "reach": [h for h in self.history[last_launch + 1:] if h["kind"] in ("tap", "type", "pick", "back")]}
        self._content_checks(ui, name, str(shot))
        # accessibility — one grouped finding per screen, objective rules only
        iss = a11y_issues([e for e in ui["elements"] if e["pkg"] == self.pkg], self.density)
        if iss["unlabeled"] and sig not in self.a11y_flagged:
            ids = ", ".join(sorted({e["rid"] or e["cls"] for e in iss["unlabeled"]})[:4])
            self.findings.append({
                "severity": "medium", "category": "a11y",
                "title": f"{len(iss['unlabeled'])} button(s) have no label for screen readers",
                "detail": f"Tappable controls with no text or content description ({ids}). TalkBack users hear only 'button', "
                          "and Google Play's pre-launch accessibility check flags this.",
                "screen_name": name, "repro": ["Open the app", f"Go to {name}", "Turn on TalkBack and swipe through the controls"],
                "evidence": {"screenshot": str(shot), "t": time.time(), "activity": activity,
                             "elements": [_loc(e) for e in iss["unlabeled"][:12]]},
            })
        if iss["small"] and sig not in self.a11y_flagged:
            ex = iss["small"][0]
            self.findings.append({
                "severity": "low", "category": "a11y",
                "title": f"{len(iss['small'])} tap target(s) smaller than 48 dp",
                "detail": f"e.g. '{el_label(ex[0])}' is {ex[1]}x{ex[2]} dp. Android guidelines ask for at least 48x48 dp "
                          "so controls are easy to hit.",
                "screen_name": name, "repro": ["Open the app", f"Go to {name}", f"Look at '{el_label(ex[0])}' (boxed in the screenshot)"],
                "evidence": {"screenshot": str(shot), "t": time.time(), "activity": activity,
                             "elements": [dict(_loc(e), size_dp=f"{w}x{h} dp") for e, w, h in iss["small"][:12]]},
            })
        self.a11y_flagged.add(sig)
        return name

    def _content_checks(self, ui: dict, name: str, shot: str):
        """Objective text defects a human tester would spot: leaked placeholders and overlapping text."""
        texts = ui.get("texts") or []
        bad = []
        for t, y, h, x1, x2 in texts:
            if BROKEN_TEXT.search(t):
                bad.append((t, [x1, y, x2, y + h]))
        if bad:
            ex = "; ".join(f"\u201c{t[:50]}\u201d" for t, _ in bad[:3])
            self.findings.append({
                "severity": "medium", "category": "content",
                "title": f"Placeholder or broken text shown to users on {name}",
                "detail": f"The screen shows raw developer text: {ex}. Users see this as a bug.",
                "screen_name": name, "repro": ["Open the app", f"Go to {name}", "Read the boxed text"],
                "evidence": {"screenshot": shot, "t": time.time(), "activity": name,
                             "elements": [{"label": t[:40], "bounds": b} for t, b in bad[:8]]},
            })
        boxes = [(t, x1, y, x2, y + h) for t, y, h, x1, x2 in texts if h > 8 and x2 - x1 > 8]
        over = []
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                ix = min(a[3], b[3]) - max(a[1], b[1]); iy = min(a[4], b[4]) - max(a[2], b[2])
                if ix > 6 and iy > 6 and a[0] != b[0]:
                    small = min((a[3] - a[1]) * (a[4] - a[2]), (b[3] - b[1]) * (b[4] - b[2]))
                    if ix * iy > 0.35 * small:
                        over.append((a, b))
        if over:
            (a, b) = over[0]
            self.findings.append({
                "severity": "low", "category": "ui",
                "title": f"Overlapping text on {name}",
                "detail": f"\u201c{a[0][:40]}\u201d and \u201c{b[0][:40]}\u201d are drawn on top of each other.",
                "screen_name": name, "repro": ["Open the app", f"Go to {name}", "Look at the boxed text"],
                "evidence": {"screenshot": shot, "t": time.time(), "activity": name,
                             "elements": [{"label": x[0][:40], "bounds": list(x[1:])} for pair in over[:3] for x in pair]},
            })

    def _edge_tests(self, sig: str, name: str):
        """Rotation + background/resume on each new screen — classic state-loss crash sources."""
        if len(self.screens) <= 10:
            self.adb.rotate(True); self._act("rotate", "Rotate the phone to landscape", landscape=True)
            self.sleep(1.5)
            self._check_failures("rotating to landscape", name)
            self.adb.rotate(False); self._act("rotate", "Rotate back to portrait", landscape=False)
            self.sleep(1.5)
            self._check_failures("rotating back to portrait", name)
        if len(self.screens) <= 5:
            self.adb.home(); self._act("home", "Press Home (send the app to the background)")
            self.sleep(1.5)
            self.adb.launch(self.pkg); self._act("launch", "Reopen the app from the launcher")
            self.sleep(2)
            self._check_failures("going to the background and reopening", name)

    def _candidates(self, sig: str, ui: dict) -> list[dict]:
        tried = self.tried.setdefault(sig, {})
        fields = [e for e in ui["elements"] if e["pkg"] == self.pkg and e["editable"]]
        fields_done = all(tried.get(el_key(f), 0) for f in fields)
        out = []
        for e in ui["elements"]:
            if e["pkg"] != self.pkg or is_forbidden(e):
                continue
            k = el_key(e)
            n = tried.get(k, 0)
            submit = (not e["editable"]) and bool(SUBMIT_RE.search(el_label(e)))
            if submit and fields:
                # once with the form empty (validation test), once after it's filled
                if n == 0 or (n == 1 and fields_done):
                    out.append((0 if n == 0 else 2, e))
            elif n == 0:
                out.append((1 if e["editable"] else 3, e))
        out.sort(key=lambda t: (t[0], t[1]["cy"], t[1]["cx"]))
        return [e for _, e in out]

    # ── main loop
    def _shot_now(self, name: str) -> Path:
        p = self.out / "screens" / f"{name}.png"
        self.adb.screenshot(p)
        return p

    def run(self) -> dict:
        t_start = self.clock()
        self.started_at = time.time()
        self.density = self.adb.density()
        comp = self.adb.launcher_component(self.pkg)
        self.adb.clear_data(self.pkg)
        # 1. launch performance
        times = [t for t in (self.adb.cold_start_ms(self.pkg, comp) for _ in range(3)) if t] if comp else []
        if times:
            med = sorted(times)[len(times) // 2]
            self.perf["cold_start_ms"] = times
            if med > 5000:
                self.findings.append({
                    "severity": "medium", "category": "perf",
                    "title": f"Slow cold start: {med/1000:.1f} s to first screen",
                    "detail": f"Measured {', '.join(f'{t/1000:.1f}s' for t in times)} across 3 cold starts. Google's Android "
                              "vitals treats 5 s+ as excessive. (Measured on an emulator; real phones vary.)",
                    "screen_name": "Launch", "repro": ["Force-stop the app", "Open it from the launcher", "Time until the first screen"],
                    "evidence": {},
                })
        self.adb.clear_data(self.pkg)
        self.history = []
        self._relaunch("start")
        self.perf["mem_start_kb"] = self._pss()
        self._check_failures("opening the app", "Launch")

        back_streak = 0
        while self.clock() - t_start < self.deadline_s:
            ui, settle = self._settle()
            if self._handle_system_dialogs(ui):
                continue
            top = self.adb.top_package()
            if top and top != self.pkg:
                if top in PICKER_PKGS and self.path and self.picks < 12:
                    self.picks += 1
                    if pick_in_picker(self.adb, ui, self.sleep):
                        self.integrations.append({"from": self.path[-1], "opened": top, "picked": True})
                        self._act("pick", f"Pick a sample file in the system picker ({top.split('.')[-2]})")
                        self.sleep(1.5)
                        self._check_failures("picking a file", "System picker")
                        continue
                if top in PERMISSION_PKGS or top == "android":
                    self.adb.back(); self.sleep(1); continue
                launcher = "launcher" in top or "nexuslauncher" in top
                if not launcher and self.path:
                    self.integrations.append({"from": self.path[-1], "opened": top,
                                              "activity": self.adb.top_activity()})
                    self.adb.back(); self._act("back", "Press Back"); self.sleep(1.5)
                    if self.adb.top_package() != self.pkg:
                        self._relaunch("returned from another app")
                else:
                    self._check_failures("leaving the app", "—")
                    self._relaunch("app left foreground")
                continue
            activity = self.adb.top_activity()
            self.cur_activity = activity
            sig = screen_sig(activity, [e for e in ui["elements"] if e["pkg"] == self.pkg])
            new = sig not in self.screens
            name = self._record_screen(sig, activity, ui)
            self.cur_shot = self.screens[sig]["shot"]
            self.slowest.append((round(settle, 1), name))
            if new:
                self._edge_tests(sig, name)
                continue
            if self.last_move:
                self.edges[self.last_move] = sig
                self.last_move = None
            cands = self._candidates(sig, ui)
            self.remaining[sig] = len(cands)
            if not cands:
                # route through an already-tried control that leads to a screen with untried controls
                route = [e for e in ui["elements"] if e["pkg"] == self.pkg and not e["editable"]
                         and self.remaining.get(self.edges.get((sig, el_key(e))), 0) > 0
                         and self.edges.get((sig, el_key(e))) != sig]
                if route and self.hops < 400:
                    e = route[0]; self.hops += 1
                    self.adb.tap(e["cx"], e["cy"])
                    self._act("tap", f'Tap "{el_label(e)}"', el=_sel(e))
                    self.last_move = (sig, el_key(e))
                    self.sleep(1.0)
                    self._check_failures(f'Tap "{el_label(e)}"', name)
                    continue
                back_streak += 1
                if back_streak > 2 or len(self.path) > 30:
                    self._relaunch("explored this branch"); back_streak = 0; continue
                self.adb.back(); self._act("back", "Press Back"); self.sleep(1.2)
                self._check_failures("pressing Back", name)
                continue
            back_streak = 0
            e = cands[0]
            k = el_key(e)
            self.last_move = (sig, k)
            self.tried[sig][k] = self.tried[sig].get(k, 0) + 1
            if e["editable"]:
                val = test_value(e)
                self.adb.tap(e["cx"], e["cy"]); self.sleep(0.4)
                self.adb.type_text(val, clear_len=len(e.get("text", "")))
                self.adb.hide_keyboard()
                human = f'Type "{val if not e["password"] else "••••••••"}" into "{el_label(e)}"'
                self._act("type", human, el=_sel(e), value=val)
            else:
                self.adb.tap(e["cx"], e["cy"])
                human = f'Tap "{el_label(e)}"'
                self._act("tap", human, el=_sel(e))
            self.sleep(1.0)
            self._note_pids()
            if self._check_failures(human, name):
                self._relaunch("after crash")

        self.perf["mem_end_kb"] = self._pss()
        self.perf["jank"] = self._jank()
        duration = round(self.clock() - t_start)
        return self._finish(duration)

    def _pss(self) -> int | None:
        out = self.adb.sh(f"dumpsys meminfo {self.pkg}")
        m = re.search(r"TOTAL(?: PSS)?:?\s+(\d+)", out)
        return int(m.group(1)) if m else None

    def _jank(self) -> dict:
        out = self.adb.sh(f"dumpsys gfxinfo {self.pkg}")
        tot = re.search(r"Total frames rendered:\s*(\d+)", out)
        jk = re.search(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", out)
        return {"frames": int(tot.group(1)) if tot else None,
                "janky_pct": float(jk.group(2)) if jk else None}

    def stress(self, seeds=(4821, 1337, 2026), events=1500):
        """Seeded random-input runs. A seed + event count replays the exact same input."""
        runs = []
        for seed in seeds:
            self.adb.clear_data(self.pkg)
            self.adb.launch(self.pkg); self.sleep(2.5)
            c0, a0 = self.adb.crash_count(self.pkg), self.adb.anr_count(self.pkg)
            out = self.adb.sh(f"monkey -p {self.pkg} -s {seed} --throttle 120 --pct-syskeys 0 "
                              f"--pct-appswitch 0 -v {events}", timeout=600)
            self._note_pids()
            res = parse_monkey(out, self.pkg)
            crashed = res["crash"] or self.adb.crash_count(self.pkg) > c0
            anr = res["anr"] or self.adb.anr_count(self.pkg) > a0
            runs.append({"seed": seed, "events": res["events"], "crash": crashed, "anr": anr})
            if crashed or anr:
                self.crashes_seen = self.adb.crash_count(self.pkg)
                self.anrs_seen = self.adb.anr_count(self.pkg)
                n = (res["events"] or events) + 50
                shot = self.out / "screens" / f"stress_{seed}.png"
                self.adb.screenshot(shot)
                self.findings.append({
                    "severity": "critical" if crashed else "high", "category": "crash",
                    "title": f"{'Crash' if crashed else 'Freeze'} under rapid random input (stress seed {seed})",
                    "detail": f"After about {res['events'] or '?'} fast random taps/swipes the app "
                              f"{'crashed' if crashed else 'stopped responding'}. Real users with fast fingers, "
                              "slow phones or double-taps hit the same code path.",
                    "screen_name": "Stress test",
                    "repro": ["Install the app fresh",
                              f"Run: adb shell monkey -p {self.pkg} -s {seed} --throttle 120 --pct-syskeys 0 --pct-appswitch 0 {n}",
                              "The same seed replays the exact same input sequence"],
                    "evidence": {"screenshot": str(shot), "log": "", "t": time.time()},
                    "replay": [{"kind": "monkey", "seed": seed, "events": n}],
                })
        self.perf["stress"] = runs
        return runs

    def _finish(self, duration: int) -> dict:
        # memory growth — only reported, never a 'bug' on its own
        summary = {
            "screens": len(self.screens), "steps": self.steps, "duration_s": duration,
            "performance": self.perf, "integrations": self.integrations[:40],
            "slowest_screens": sorted(self.slowest, reverse=True)[:5],
            "screen_list": [{"name": s["name"], "activity": s["activity"], "controls": s["controls"],
                             "shot": s["shot"], "t": s.get("t"), "ui": s.get("ui"), "texts": s.get("texts"),
                             "reach": s.get("reach")} for s in self.screens.values()],
            "started_at": self.started_at, "density": self.density,
            "pids": sorted(self.pids),
        }
        return summary


def _loc(e: dict | None) -> dict:
    """Where a control is: its label, id and on-screen box (device pixels)."""
    if not e:
        return {}
    return {"label": el_label(e), "rid": e.get("rid") or "", "cls": e.get("cls") or "", "bounds": e.get("bounds")}


def _sel(e: dict) -> dict:
    return {k: e.get(k) for k in ("rid", "text", "desc", "cls", "cx", "cy", "editable", "password", "bounds")}


def find_el(ui: dict, sel: dict, pkg: str) -> dict | None:
    cands = [e for e in ui["elements"] if e["pkg"] == pkg]
    for e in cands:
        if sel.get("rid") and e["rid"] == sel["rid"] and (e["text"] == sel.get("text") or e["desc"] == sel.get("desc")):
            return e
    for e in cands:
        if sel.get("rid") and e["rid"] == sel["rid"]:
            return e
    for e in cands:
        if (sel.get("text") and e["text"] == sel["text"]) or (sel.get("desc") and e["desc"] == sel["desc"]):
            return e
    return None


PICK_PREFER = {"image": re.compile(r"^IMG_|\.jpe?g$", re.I), "video": re.compile(r"^VID_|\.mp4$", re.I),
               "audio": re.compile(r"voice memo|song|\.m4a$|\.mp3$", re.I), "document": re.compile(r"\.pdf$|invoice", re.I),
               "pdf": re.compile(r"\.pdf$|invoice", re.I)}


def pick_in_picker(adb, ui: dict, sleep, kind: str | None = None) -> bool:
    """In a system file/photo/contact picker: open a sensible folder if needed, pick the first
    item, then confirm (multi-select pickers). Same logic in crawl and replay, so picks are reproducible."""
    picked = False
    for _ in range(5):
        if adb.top_package() not in PICKER_PKGS:
            return True                      # picker closed: the app got the file
        els = ui["elements"]
        ok = [e for e in els if PICK_OK_RE.match((e.get("text") or "").strip())]
        items = [e for e in els if PICK_ITEM_RE.search(e.get("rid") or "") and not e["editable"]
                 and (e["bounds"][3] - e["bounds"][1]) > 60]
        roots = [e for e in els if PICK_ROOT_RE.match((e.get("text") or "").strip())]
        if picked and ok:
            tgt = ok[0]
        elif items and not picked:
            pref = PICK_PREFER.get(kind or "")
            named = [e for e in els if pref and pref.search((e.get("text") or e.get("desc") or "").strip())] if pref else []
            tgt = sorted(named or items, key=lambda e: (e["cy"], e["cx"]))[0]; picked = True
        elif roots and not picked:
            tgt = roots[0]
        elif ok:
            tgt = ok[0]
        else:
            return False
        adb.tap(tgt["cx"], tgt["cy"]); sleep(1.5)
        ui = parse_ui(adb.dump(), "")
    return adb.top_package() not in PICKER_PKGS


# ───────────────────────────── replay (reproduce) ─────────────────────────────
def minimal_trail(trail: list[dict]) -> list[dict]:
    """Drop rotate-and-back and home-and-reopen pairs, keeping the last two steps intact
    (if the crash happened right after a rotation, that rotation is the cause)."""
    out, i, n = [], 0, len(trail)
    while i < n:
        a = trail[i]; b = trail[i + 1] if i + 1 < n else None
        tail = i + 1 >= n - 1
        if b and not tail and a["kind"] == "rotate" and b["kind"] == "rotate" and a.get("landscape") and not b.get("landscape"):
            i += 2; continue
        if b and not tail and a["kind"] == "home" and b["kind"] == "launch":
            i += 2; continue
        out.append(a); i += 1
    return out

def replay_once(adb: Adb, pkg: str, trail: list[dict], sleep=time.sleep) -> bool:
    adb.clear_data(pkg)
    c0, a0 = adb.crash_count(pkg), adb.anr_count(pkg)
    for st in trail:
        k = st["kind"]
        if k == "monkey":
            adb.launch(pkg); sleep(2.5)
            adb.sh(f"monkey -p {pkg} -s {st['seed']} --throttle 120 --pct-syskeys 0 --pct-appswitch 0 -v {st['events']}",
                   timeout=600)
        elif k == "launch": adb.launch(pkg); sleep(2.5)
        elif k == "back": adb.back(); sleep(1.2)
        elif k == "home": adb.home(); sleep(1.5)
        elif k == "rotate": adb.rotate(st.get("landscape", False)); sleep(1.5)
        elif k == "pick": pick_in_picker(adb, parse_ui(adb.dump(), pkg), sleep)
        elif k in ("tap", "type"):
            ui = parse_ui(adb.dump(), pkg)
            for e in ui["elements"]:  # same auto-answers as the crawl
                if e["pkg"] in PERMISSION_PKGS and ALLOW_RE.match(e.get("text", "")):
                    adb.tap(e["cx"], e["cy"]); sleep(1); ui = parse_ui(adb.dump(), pkg); break
            e = find_el(ui, st["el"], pkg)
            x, y = (e["cx"], e["cy"]) if e else (st["el"]["cx"], st["el"]["cy"])
            adb.tap(x, y); sleep(0.4)
            if k == "type":
                adb.type_text(st["value"], clear_len=len((e or {}).get("text", "")))
                adb.hide_keyboard()
            sleep(1.0)
        if adb.crash_count(pkg) > c0 or adb.anr_count(pkg) > a0:
            adb.rotate(False)
            return True
    adb.rotate(False)
    return False


def replay(run_dir: Path, times: int, adb: Adb | None = None, pkg: str | None = None, sleep=time.sleep):
    adb = adb or Adb()
    pkg = pkg or os.environ["GIL_PACKAGE"]
    findings = json.loads((run_dir / "findings.json").read_text())
    out, changed = [], False
    for f in findings:
        if f.get("category") != "crash" or not f.get("replay"):
            continue
        n = sum(1 for _ in range(times) if replay_once(adb, pkg, f["replay"], sleep))
        # try the shortest trail: drop rotate/background detours. Only keep the short
        # version if it ALSO crashes every single time.
        short = minimal_trail(f["replay"])
        if n and len(short) < len(f["replay"]) and all(replay_once(adb, pkg, short, sleep) for _ in range(times)):
            f["replay"] = short
            f["repro"] = ["Install the app fresh and open it"] + [st["human"] for st in short if st["kind"] != "launch" or st is not short[0]][-25:]
            changed = True
        out.append({"title": f["title"], "screen": f.get("screen_name"), "reproduced": n, "of": times})
        print(f"replay: {f['title']} -> {n}/{times}")
    (run_dir / "regress.json").write_text(json.dumps(out, indent=1))
    if changed:
        (run_dir / "findings.json").write_text(json.dumps(findings, indent=1))
    return out


# ───────────────────────────── entrypoints ─────────────────────────────
def explore(adb: Adb | None = None, pkg: str | None = None, out_root: Path = Path("reports"),
            minutes: float | None = None, sleep=time.sleep, clock=time.time, stress=True) -> Path:
    adb = adb or Adb()
    pkg = pkg or os.environ["GIL_PACKAGE"]
    minutes = minutes if minutes is not None else float(os.environ.get("GIL_MINUTES", "12"))
    run = out_root / time.strftime("gil-%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    c = Crawler(adb, pkg, run, minutes, sleep, clock)
    summary = c.run()
    if stress:
        c.stress()
    summary["performance"] = c.perf
    # non-fatal errors the app itself logged (own PIDs only)
    nf = parse_nonfatal(adb.sh("logcat -d -v threadtime", timeout=60), c.pids)
    for name, b in sorted(nf["bugs"].items(), key=lambda kv: -kv[1]["count"])[:6]:
        c.findings.append({
            "severity": "low", "category": "stability",
            "title": f"App logged {b['count']}x {name} (caught, not crashing — yet)",
            "detail": "The app caught this error and kept running, but it usually means a code path is getting bad data. "
                      "Sample log line is attached.",
            "screen_name": "During the crawl", "repro": ["Run the app through the screens listed in this report",
                                                         "Watch logcat for the same error"],
            "evidence": {"log": b["sample"]},
        })
    summary["network_errors"] = {k: v["count"] for k, v in nf["network"].items()}
    summary["declared"] = declared_surface(adb, pkg)
    (run / "findings.json").write_text(json.dumps(c.findings, indent=1))
    (run / "summary.json").write_text(json.dumps({"summary": summary}, indent=1))
    print(f"crawl done: {summary['screens']} screens, {summary['steps']} actions, "
          f"{len(c.findings)} raw findings, {len(c.integrations)} integration hand-offs")
    return run


def declared_surface(adb: Adb, pkg: str) -> dict:
    out = adb.sh(f"dumpsys package {pkg}", timeout=60)
    perms = sorted(set(re.findall(r"(android\.permission\.[A-Z_]+): granted=", out)))
    schemes = sorted(set(re.findall(r'Scheme: "([^"]+)"', out)) - {"http", "https"} |
                     set(f"https://{h}" for h in re.findall(r'Authority: "([^"]+)"', out)))
    return {"permissions": perms[:40], "deep_links": schemes[:20]}


# ───────────────────────────── self test (fake device) ─────────────────────────────
class FakeAdb(Adb):
    """A 3-screen app: Home -> (Sign up form | About). Submitting the sign-up form with an
    empty email crashes; rotation on About crashes nothing. Used to test crawl + replay logic."""
    PKG = "com.acme.app"

    def __init__(self):
        self.screen = "home"; self.crashes = 0; self.email = ""; self.fg = self.PKG; self.shots = 0
        self.log = []

    def _node(self, rid, cls, text="", desc="", b=(0, 0, 200, 200), click=True, pkg=None, pw=False):
        return (f'<node package="{pkg or self.PKG}" class="android.widget.{cls}" resource-id="{self.PKG}:id/{rid}" '
                f'text="{text}" content-desc="{desc}" clickable="{"true" if click else "false"}" enabled="true" '
                f'password="{"true" if pw else "false"}" bounds="[{b[0]},{b[1]}][{b[2]},{b[3]}]" />')

    def dump(self):
        n = {
            "home": [self._node("signup", "Button", "Sign up", b=(0, 0, 500, 200)),
                     self._node("about", "Button", "About", b=(0, 300, 500, 500)),
                     self._node("buy", "Button", "Buy Pro", b=(0, 600, 500, 800)),
                     self._node("icon", "ImageButton", b=(600, 0, 640, 40))],
            "signup": [self._node("email", "EditText", self.email, b=(0, 0, 900, 200)),
                       self._node("go", "Button", "Create account", b=(0, 300, 900, 500))],
            "about": [self._node("site", "Button", "Website", b=(0, 0, 900, 200))],
        }[self.screen] if self.fg == self.PKG else []
        return '<?xml version="1.0"?><hierarchy>' + "".join(n) + "</hierarchy>"

    def sh(self, cmd, timeout=30):
        if cmd.startswith("input tap"): return ""
        if cmd.startswith("input text"): self.email = cmd.split(" ", 2)[2].strip("'"); return ""
        if cmd.startswith("pidof"): return "4242"
        if cmd.startswith("wm density"): return "Physical density: 420"
        if cmd.startswith("dumpsys package"): return 'android.permission.CAMERA: granted=true\nScheme: "acme"'
        if cmd.startswith("logcat -d -v threadtime"):
            return "11-21 10:00:00.000  4242  4242 E Acme    : java.lang.IllegalStateException: bad\n" \
                   "11-21 10:00:00.000  999  999 E Other   : java.lang.NullPointerException: not ours"
        if cmd.startswith("monkey -p") and " -s " in cmd: return "Events injected: 1500\n"
        if cmd.startswith("monkey"): self.fg = self.PKG; self.screen = "home"; return ""
        if cmd.startswith("pm clear"): self.screen = "home"; self.email = ""; return ""
        if cmd.startswith("cmd package resolve-activity"): return "com.acme.app/.Main"
        if cmd.startswith("am start -W"): return "TotalTime: 6200"
        if cmd.startswith("dumpsys meminfo"): return "TOTAL PSS:   81234"
        if cmd.startswith("dumpsys gfxinfo"): return "Total frames rendered: 900\nJanky frames: 90 (10.00%)"
        return ""

    def tap(self, x, y):
        self.log.append(("tap", self.screen, x, y))
        if self.fg != self.PKG: return
        if self.screen == "home":
            if y < 250 and x < 500: self.screen = "signup"
            elif 300 <= y < 500: self.screen = "about"
        elif self.screen == "signup" and 300 <= y < 500:
            if not self.email: self.crashes += 1; self.fg = "launcher"; self.screen = "home"
            else: self.screen = "home"
        elif self.screen == "about" and y < 200:
            self.fg = "com.android.chrome"

    def back(self):
        if self.fg not in (self.PKG,): self.fg = self.PKG; return
        if self.screen == "home": self.fg = "com.google.android.apps.nexuslauncher"
        else: self.screen = "home"

    def home(self): self.fg = "com.google.android.apps.nexuslauncher"
    def launch(self, pkg): self.fg = self.PKG
    def clear_data(self, pkg): self.screen = "home"; self.email = ""; self.fg = self.PKG
    def type_text(self, text, clear_len=0): self.email = text
    def hide_keyboard(self): pass
    def rotate(self, landscape): pass
    def screenshot(self, path): path.write_bytes(b"png")
    def top_package(self): return self.fg
    def top_activity(self): return f"{self.fg}/.{self.screen.title()}Activity"
    def crash_count(self, pkg): return self.crashes
    def anr_count(self, pkg): return 0
    def crash_log(self): return "FATAL EXCEPTION: main\nProcess: com.acme.app, PID: 4242\njava.lang.NullPointerException: email"
    def density(self): return 2.625


def _selftest():
    import tempfile
    # pure functions
    assert is_forbidden({"text": "Buy Pro"}) and is_forbidden({"text": "Sign out"}) and not is_forbidden({"text": "Sign up"})
    assert test_value({"hint": "Email address"}) == "qa.tester@example.com"
    assert test_value({"rid": "phone_input"}) == "5555550123"
    assert test_value({"password": True}).startswith("TestPass")
    assert parse_monkey("// CRASH: com.acme.app (pid 1)\nEvents injected: 812", "com.acme.app") == {"crash": True, "anr": False, "events": 812}
    assert parse_monkey("// CRASH: com.other (pid 1)", "com.acme.app")["crash"] is False
    nf = parse_nonfatal("11-21 10:00:00.000  42  42 E X : java.lang.NullPointerException\n"
                        "11-21 10:00:00.000  7  7 E X : java.lang.NullPointerException", {"42"})
    assert nf["bugs"]["NullPointerException"]["count"] == 1, nf

    clock = {"t": 0.0}
    def fake_sleep(s): clock["t"] += s
    def fake_clock(): clock["t"] += 0.5; return clock["t"]

    with tempfile.TemporaryDirectory() as d:
        dev = FakeAdb()
        run = explore(dev, FakeAdb.PKG, Path(d), minutes=3, sleep=fake_sleep, clock=fake_clock)
        F = json.loads((run / "findings.json").read_text())
        S = json.loads((run / "summary.json").read_text())["summary"]
        titles = [f["title"] for f in F]
        crash = [f for f in F if f["category"] == "crash"]
        assert crash, titles
        assert any("Create account" in f["title"] for f in crash), titles
        assert not any("Buy" in str(x) for x in dev.log if x[1] == "home" and x[3] >= 600), "tapped a forbidden control"
        assert any(f["category"] == "a11y" and "no label" in f["title"] for f in F), titles
        assert any(f["category"] == "perf" for f in F), titles          # 6.2 s cold start
        assert any(f["category"] == "stability" and "IllegalState" in f["title"] for f in F), titles
        assert not any("NullPointer" in f["title"] for f in F if f["category"] == "stability"), "counted another app's log"
        assert any(i["opened"] == "com.android.chrome" for i in S["integrations"]), S["integrations"]
        assert S["screens"] >= 3, S
        assert S["declared"]["permissions"] == ["android.permission.CAMERA"]
        # replay reproduces the real crash from a clean install
        reg = replay(run, 3, adb=dev, pkg=FakeAdb.PKG, sleep=fake_sleep)
        assert reg and all(r["reproduced"] == 3 for r in reg if "Create account" in r["title"]), reg
        print(f"selftest OK — {S['screens']} screens, {S['steps']} actions, findings: {titles}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    elif len(sys.argv) > 1 and sys.argv[1] == "replay":
        replay(Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 3)
    else:
        print(explore())
