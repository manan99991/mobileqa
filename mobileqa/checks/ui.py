"""Deterministic UI / UX / accessibility checks run on every screen.

These are the things a careful human notices in the first second: text cut off,
things on top of each other, buttons too small for a thumb, lorem ipsum left in,
images and icons with no label for screen readers, controls hanging off-screen.
"""
from __future__ import annotations

import re

from ..driver.base import ScreenState
from ..findings import Finding

PLACEHOLDER_RE = re.compile(
    r"\b(lorem ipsum|todo|tbd|fixme|placeholder|dummy|test test|xxx+|asdf|null|undefined|NaN|\[object Object\]|%[sd]\b|\{\{?\w+\}?\}|\$\{\w+\})",
    re.I)
TRUNCATION_RE = re.compile(r"(\.\.\.|…)$")
ERROR_TEXT_RE = re.compile(
    r"(something went wrong|unexpected error|exception|stack ?trace|error code|has stopped|keeps stopping|"
    r"isn'?t responding|not responding|failed to load|unable to load|network error|internal server error|"
    r"status 5\d\d|status 4\d\d|cannot connect|try again later)", re.I)

# OS / emulator chrome that is NOT the app under test — never report these as app defects.
# On-screen ANR/crash dialogs are told apart from a real app crash by their TEXT (the launcher
# and System UI say "… isn't responding"); we deliberately do not key on the aerr_* button ids,
# because a genuine app crash dialog uses those same ids and only the text disambiguates.
SYSTEM_NOISE_RE = re.compile(
    r"(system ?ui isn'?t responding|system ?ui.*not responding|launcher isn'?t responding|"
    r"process system isn'?t responding|android system|wait|close app|google play services|"
    r"pixel ?launcher|systemui|setup wizard)", re.I)

# Emulator / OS *log* noise that is NOT the app under test. The free CI emulator's own media
# codec throws a NullPointerException every few seconds ("Delight5Decoder"), and the launcher
# and System UI ANR while a 2-vCPU software-GPU runner settles after boot. Each of these log
# lines names a system component or the codec — never the app — so they must never become
# findings. A real app crash/ANR names the app's own package and does not match here.
LOG_NOISE_RE = re.compile(
    r"(delight5decoder|nexuslauncher|com\.android\.systemui|com\.android\.launcher|"
    r"system_process|setupwizard|surfaceflinger)", re.I)

# A raw programming error on screen means the app fell through to its error boundary. Report it
# ONCE as a crash and skip the cosmetic checks, so the error text itself ("…of undefined", a
# stack frame) does not also masquerade as placeholder / unformatted-value / tiny-tap findings:
# it is one bug, not ten. Technical signatures only — a friendly "we hit a snag" is not this.
APP_ERROR_RE = re.compile(
    r"(cannot read propert(y|ies) of (undefined|null)|undefined is not an? (object|function)|"
    r"unhandled (promise )?rejection|\btypeerror\b|\breferenceerror\b|\brangeerror\b|"
    r"invariant violation|minified react error|\bat [\w$.]+ \(.+:\d+:\d+\))", re.I)


def looks_like_app_error(texts: list[str]) -> bool:
    """True when the screen is the app's own crash / error-boundary screen (a raw error is on it)."""
    joined = " ".join(texts)
    return bool(APP_ERROR_RE.search(joined)) and not SYSTEM_NOISE_RE.search(joined)

# Platform HIG minimums: iOS 44pt, Android 48dp. Rects are in the driver's units
# (points on iOS, px on Android) so we scale the Android threshold by density estimate.


def run_ui_checks(state: ScreenState, platform: str, screen_name: str, step: int) -> list[Finding]:
    out: list[Finding] = []
    # The app crashed into its error boundary — one crash finding, and none of the cosmetic
    # checks below (they would re-flag the raw error text as placeholder/value/tap-target noise).
    if looks_like_app_error(state.texts):
        return [Finding("crash", "critical", "App crashed to its error screen",
                        f"A raw error is on screen — the app fell through to its error boundary: "
                        f"{' '.join(state.texts)[:220]}",
                        state.fingerprint, screen_name, step=step)]
    els = [e for e in state.elements if e.visible and e.rect.area > 0]
    min_target = 44 if platform == "ios" else _android_min_px(state)

    for e in els:
        name = e.name[:50]
        # --- truncation
        if e.type == "text" and e.text and TRUNCATION_RE.search(e.text) and len(e.text) > 12:
            out.append(Finding("ui", "medium", "Text truncated with ellipsis",
                               f"'{e.text}' is cut off. Check it reads fully at this width and with larger system font sizes.",
                               state.fingerprint, screen_name, name, source="rule", step=step))
        # --- placeholder / dev text
        if e.text and PLACEHOLDER_RE.search(e.text):
            out.append(Finding("content", "high", "Placeholder or debug text visible",
                               f"'{e.text}' looks like placeholder/dev text shipped to the UI.",
                               state.fingerprint, screen_name, name, step=step))
        # --- error text surfaced to the user (but skip OS/emulator chrome, not the app's fault)
        if e.text and ERROR_TEXT_RE.search(e.text) and len(e.text) < 300 and not SYSTEM_NOISE_RE.search(e.text):
            out.append(Finding("functional", "high", "Error message shown on screen",
                               f"Screen shows: '{e.text}'.", state.fingerprint, screen_name, name, step=step))
        # --- tiny tap targets
        small = min(e.rect.w, e.rect.h)
        if e.interactive and e.type != "list" and small < min_target:
            out.append(Finding("a11y", "medium" if small < 0.5 * min_target else "low",
                               "Tap target smaller than platform minimum",
                               f"'{name}' is {e.rect.w}x{e.rect.h}; minimum is {min_target}x{min_target} ({'44pt iOS' if platform == 'ios' else '48dp Android'}).",
                               state.fingerprint, screen_name, name, step=step))
        # --- interactive element with nothing a screen reader can say
        if (e.interactive and e.type in ("button", "image", "switch", "checkbox", "tab", "link")
                and not (e.text or e.label) and e.children == 0):   # containers inherit their children's text
            out.append(Finding("a11y", "medium", "Interactive control has no accessible label",
                               f"{e.raw_type.split('.')[-1]} ({e.resource_id or 'no id'}) at ({e.rect.cx},{e.rect.cy}) has no text or content description; VoiceOver/TalkBack will read nothing useful.",
                               state.fingerprint, screen_name, e.resource_id or f"@{e.rect.cx},{e.rect.cy}", step=step))
        # --- input with no label/hint
        if e.type == "input" and not (e.label or e.resource_id) and not e.text:
            out.append(Finding("a11y", "low", "Text field has no hint or accessibility label",
                               f"Input at ({e.rect.cx},{e.rect.cy}) has no placeholder, label or id.",
                               state.fingerprint, screen_name, f"@{e.rect.cx},{e.rect.cy}", step=step))
        # --- off-screen
        if e.interactive and (e.rect.x < 0 or e.rect.y < 0 or
                              e.rect.x + e.rect.w > state.width + 2 or e.rect.y + e.rect.h > state.height + 2):
            if state.width and state.height:
                out.append(Finding("ui", "medium", "Interactive element extends off-screen",
                                   f"'{name}' bounds {e.rect} exceed screen {state.width}x{state.height}.",
                                   state.fingerprint, screen_name, name, step=step))
        # --- disabled primary action with no explanation (UX)
        if e.type == "button" and not e.enabled and e.rect.w > state.width * 0.6:
            out.append(Finding("ux", "low", "Primary button disabled",
                               f"'{name}' is disabled. Make sure the screen tells the user why / what to fill in.",
                               state.fingerprint, screen_name, name, step=step))

    # --- overlapping text elements (layout collision)
    texts = [e for e in els if e.type == "text" and e.text]
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            if a.rect.contains(b.rect) or b.rect.contains(a.rect):
                continue
            inter = a.rect.intersection_area(b.rect)
            if inter and inter > 0.25 * min(a.rect.area, b.rect.area):
                out.append(Finding("ui", "high", "Text elements overlap",
                                   f"'{a.text[:40]}' and '{b.text[:40]}' overlap by {inter}px².",
                                   state.fingerprint, screen_name, f"{a.name[:20]}+{b.name[:20]}", step=step))

    # --- empty screen
    if len([e for e in els if e.text or e.label]) == 0 and not state.keyboard_shown:
        out.append(Finding("functional", "high", "Blank screen",
                           "No text or labelled elements are visible. Possible failed load or white screen.",
                           state.fingerprint, screen_name, step=step))

    # --- crash dialogs (ignore OS/emulator "System UI isn't responding" — that's the environment)
    joined = " ".join(state.texts).lower()
    if SYSTEM_NOISE_RE.search(joined):
        return out
    for pat, title in (("keeps stopping", "App crash dialog (Android)"),
                       ("has stopped", "App crash dialog (Android)"),
                       ("isn't responding", "App Not Responding dialog"),
                       ("unfortunately", "App crash dialog (Android)")):
        if pat in joined:
            out.append(Finding("crash", "critical", title, f"System dialog text: {joined[:200]}",
                               state.fingerprint, screen_name, step=step))
            break
    return out


def _android_min_px(state: ScreenState) -> int:
    # Infer density from screen width: 48dp at xxhdpi (1080px wide, ~2.75x) ≈ 132px, mdpi 48px.
    if not state.width:
        return 48
    density = max(1.0, state.width / 393.0)   # 393dp is a typical phone width
    return int(48 * density * 0.9)            # 10% tolerance


def check_crash_logs(lines: list[str], platform: str, screen: str, screen_name: str, step: int) -> list[Finding]:
    out: list[Finding] = []
    for ln in lines:
        if LOG_NOISE_RE.search(ln):
            continue   # emulator/OS log noise (codec NPE, launcher/SystemUI ANR), not the app
        low = ln.lower()
        n_before = len(out)
        if platform == "android":
            if "fatal exception" in low or "androidruntime" in low and "exception" in low:
                out.append(Finding("crash", "critical", "Uncaught exception in logcat", ln[:600], screen, screen_name,
                                   evidence={"log": ln}, step=step))
            elif "anr in" in low or "application not responding" in low:
                out.append(Finding("crash", "critical", "ANR (Application Not Responding)", ln[:600], screen, screen_name,
                                   evidence={"log": ln}, step=step))
            elif "strictmode" in low and "violation" in low:
                out.append(Finding("perf", "low", "StrictMode violation", ln[:400], screen, screen_name, step=step))
        else:
            if "terminating app due to uncaught exception" in low or "exc_bad_access" in low or "sigabrt" in low:
                out.append(Finding("crash", "critical", "iOS crash in syslog", ln[:600], screen, screen_name,
                                   evidence={"log": ln}, step=step))
            elif "watchdog" in low and "exceeded" in low:
                out.append(Finding("crash", "critical", "iOS watchdog timeout", ln[:600], screen, screen_name, step=step))
        if len(out) > n_before:
            continue   # already filed as a crash
        if "nullpointer" in low or "unhandled promise rejection" in low or "uncaught typeerror" in low:
            out.append(Finding("functional", "high", "Runtime error in logs", ln[:500], screen, screen_name, step=step))
    return out
