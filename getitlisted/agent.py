#!/usr/bin/env python3
"""
Get It Listed — deep functional tester (Android).

Runs after the crawl (which mapped every reachable screen, its controls and the taps to
reach it). Works like a human QA engineer:

  1. LEARN    look at each screen (vision) -> what each feature does, its inputs, its output
  2. PLAN     write test cases per feature: happy path, edge, negative, boundary, state
  3. RUN      drive the app through every case with real content seeded on the phone
  4. VERIFY   objective device checks (new files, size vs input, EXIF GPS removed, share sheet,
              calendar/contacts/alarms/notifications changed, app errors logged) + an
              independent AI judge looking at before/after screens
  5. CONFIRM  every failure is re-run; it is a bug only if it fails again with evidence.
              Anything uncertain is "needs a human look", never a bug.

Writes into the run dir: app_model.json, test_plan.json, test_results.json, and appends
functional findings to findings.json (crashes found here carry a replay trail so the normal
3x replay + trust gate still applies).

usage: agent.py <run_dir>       env: GIL_PACKAGE, GEMINI_API_KEY (or GIL_LLM_*), GIL_AGENT_MINUTES
"""
from __future__ import annotations
import json, os, re, shlex, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import brain
from crawler import (Adb, parse_ui, el_label, el_key, _sel, _loc, find_el, pick_in_picker, is_forbidden,
                     PICKER_PKGS, PERMISSION_PKGS, ALLOW_RE)

SYS = ("You are a senior mobile QA engineer. You test apps the way a careful human does: understand what each "
       "feature is for, then check it really does its job. You never invent behaviour you cannot see. "
       "Reply with JSON only.")

ORACLES = ["new_file", "output_smaller_than_input", "exif_gps_removed", "share_sheet_opened", "other_app_opened",
           "calendar_event_added", "contacts_changed", "alarm_set", "notification_posted", "text_on_screen", "text_not_on_screen", "video_silent", "output_under_mb", "element_present",
           "no_app_error_logged", "screen_changed"]


# ───────────────────────────── device state probes (objective evidence) ─────────────────────────────
class Probe:
    def __init__(self, adb: Adb, pkg: str):
        self.adb, self.pkg = adb, pkg

    def _count(self, uri: str) -> int:
        out = self.adb.sh(f"content query --uri {uri} --projection _id 2>/dev/null | grep -c Row", timeout=30)
        try:
            return int(out.strip().splitlines()[-1])
        except Exception:
            return -1

    def snap(self) -> dict:
        self.adb.sh("touch /sdcard/.gil_marker")
        return {
            "t": time.time(),
            "epoch": int((self.adb.sh("date +%s") or "0").strip() or 0),
            "devtime": self.adb.sh("date +'%m-%d %H:%M:%S.000'").strip(),
            "calendar": self._count("content://com.android.calendar/events"),
            "contacts": self._count("content://com.android.contacts/raw_contacts"),
            "alarms": self.adb.sh(f"dumpsys alarm | grep -c '{self.pkg}'").strip(),
            "notifs": self.adb.sh(f"dumpsys notification --noredact | grep -c 'pkg={self.pkg}'").strip(),
        }

    def diff(self, before: dict) -> dict:
        facts = {}
        new = self.adb.sh("find /sdcard -newer /sdcard/.gil_marker -type f ! -path '*/Android/data/*' ! -name '.gil_marker' "
                          "2>/dev/null | head -40", timeout=60).split()
        files = []
        for f in new:
            sz = self.adb.sh(f"stat -c %s '{f}'").strip()
            files.append({"path": f, "bytes": int(sz) if sz.isdigit() else None,
                          "ext": f.rsplit(".", 1)[-1].lower() if "." in f else ""})
        # app-private outputs for debuggable builds
        mins = int((time.time() - before["t"]) // 60) + 2
        priv, changed = [], []
        for line in self.adb.sh(f"run-as {self.pkg} find files cache -type f -mmin -{mins} -exec stat -c '%s %Y %n' {{}} + 2>/dev/null | head -60",
                                timeout=60).splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            if before.get("epoch") and int(parts[1]) < before["epoch"]:
                continue                       # written before this test started: not this test's output
            changed.append(parts[2])
            parts = [parts[0], parts[2]]
            if not re.search(r"(\.(xml|json|db|prof|tmp|lock|lck)|-journal|-wal|-shm)$|/(code_cache|WebView|image_manager)", parts[1]):
                files.append({"path": "app:" + parts[1], "bytes": int(parts[0]),
                              "ext": parts[1].rsplit(".", 1)[-1].lower() if "." in parts[1] else ""})
                priv.append(parts[1])
        facts["new_files"] = files
        facts["new_private_files"] = priv[:20]
        facts["changed_private_files"] = changed[:40]
        after = self.snap_counts()
        for k in ("calendar", "contacts"):
            if before.get(k, -1) >= 0 and after.get(k, -1) >= 0:
                facts[f"{k}_delta"] = after[k] - before[k]
        try:
            facts["alarms_delta"] = int(after["alarms"]) - int(before["alarms"])
            facts["notifications_delta"] = int(after["notifs"]) - int(before["notifs"])
        except Exception:
            pass
        log = self.adb.sh(f"logcat -d -v threadtime -T '{before['devtime']}'", timeout=60)
        starts = re.findall(r"START u0 \{([^}]*)\}", log)
        facts["intents"] = [re.sub(r"\s+", " ", s)[:200] for s in starts][-10:]
        pids = set(self.adb.sh(f"pidof {self.pkg}").split())
        errs = []
        for line in log.splitlines():
            parts = line.split()
            if len(parts) > 5 and parts[2] in pids and parts[4] in ("E", "F") and re.search(r"Exception|Error|FATAL", line):
                errs.append(line.strip()[:240])
        facts["app_errors"] = errs[-6:]
        return facts

    def snap_counts(self) -> dict:
        return {"calendar": self._count("content://com.android.calendar/events"),
                "contacts": self._count("content://com.android.contacts/raw_contacts"),
                "alarms": self.adb.sh(f"dumpsys alarm | grep -c '{self.pkg}'").strip(),
                "notifs": self.adb.sh(f"dumpsys notification --noredact | grep -c 'pkg={self.pkg}'").strip()}

    def read_bytes(self, path: str) -> bytes:
        try:
            if path.startswith("app:"):
                return self.adb.exec_out(f"run-as {self.pkg} cat {path[4:]}", timeout=90)
            loc = Path(tempfile.mkdtemp()) / "f"
            subprocess.run(["adb", "pull", path, str(loc)], capture_output=True, timeout=90)
            return loc.read_bytes()
        except Exception:
            return b""

    def exif_has_gps(self, path: str) -> bool | None:
        try:
            from PIL import Image
            loc = Path(tempfile.mkdtemp()) / "o.jpg"
            if path.startswith("app:"):
                loc.write_bytes(self.adb.exec_out(f"run-as {self.pkg} cat {path[4:]}", timeout=60))
            else:
                subprocess.run(["adb", "pull", path, str(loc)], capture_output=True, timeout=60)
            ex = Image.open(loc).getexif()
            return bool(dict(ex.get_ifd(0x8825)))
        except Exception:
            return None


INPUT_NAME = re.compile(r"(^|/)(sample[-_ ]|input[-_ ]|original[-_ ])|/Inbox/|/tmp/.*upload", re.I)


def split_io(files: list, seed: dict) -> tuple[list, list]:
    """New files are either copies of what went IN (the app's sample, our seeded file) or what came OUT."""
    seeded = {x.get("name") for x in (seed or {}).get("files", [])}
    ins, outs = [], []
    for f in files:
        p = f["path"] if isinstance(f, dict) else str(f)
        (ins if (INPUT_NAME.search(p) or Path(p).name in seeded) else outs).append(f)
    outs.sort(key=lambda f: f.get("mtime", 0) if isinstance(f, dict) else 0)
    return ins, outs


def has_sound(data: bytes) -> bool:
    return re.search(rb"hdlr.{8}soun", data, re.S) is not None


def check(oracle: str, arg: str, facts: dict, probe: Probe, seed: dict, final_texts: list, screen_changed: bool):
    """Deterministic verdict for one expected outcome: (status, evidence) with status pass|fail|na."""
    ins, files = split_io(facts.get("new_files") or [], seed)
    facts = dict(facts, new_private_files=[x for x in (facts.get("new_private_files") or []) if not INPUT_NAME.search(str(x))])
    kinds = {"image": ("jpg", "jpeg", "png", "webp", "heic", "gif"), "gif": ("gif",), "video": ("mp4", "mov", "3gp", "webm"),
             "audio": ("m4a", "mp3", "wav", "aac", "ogg"), "pdf": ("pdf",), "document": ("pdf", "txt", "csv", "docx", "xlsx", "vcf", "zip")}
    if oracle == "new_file":
        junk = re.compile(r"(\.lck|-journal|-wal|-shm|\.db|\.tmp|\.xml)$|/shared_prefs/|/no_backup/|/code_cache/", re.I)
        files = [f for f in files if not junk.search(f["path"] if isinstance(f, dict) else str(f))]
        facts = dict(facts, new_private_files=[x for x in (facts.get("new_private_files") or []) if not junk.search(str(x))])
        if (arg or "").startswith("path:"):
            needle = arg[5:]
            allp = [f["path"] if isinstance(f, dict) else str(f) for f in files] + [str(x) for x in (facts.get("new_private_files") or []) + (facts.get("changed_private_files") or [])]
            hitp = [p for p in allp if needle in p]
            return ("pass", f"The app recorded a finished result ({Path(hitp[0]).name} updated)") if hitp else \
                   ("fail", f"The app did not record a finished result ({needle} was not written)")
        want = kinds.get((arg or "").lower())
        hit = [f for f in files if not want or f["ext"] in want] + ([] if want else facts.get("new_private_files") or [])
        priv = [p for p in (facts.get("new_private_files") or []) if not want or p.rsplit(".", 1)[-1].lower() in want]
        if hit or priv:
            names = [Path(f["path"]).name if isinstance(f, dict) else Path(f).name for f in (hit or priv)][:3]
            return "pass", f"New {arg or 'file'} created: {', '.join(names)}"
        return "fail", f"No new {arg or 'output'} file appeared on the phone (checked shared storage" + \
               (" and the app's own storage)." if True else ").")
    if oracle == "output_smaller_than_input":
        ins = [x for x in seed.get("files", []) if not arg or x["kind"] == arg]
        outs = [f for f in files if f.get("bytes")]
        if not outs or not ins:
            return "na", "No output file to compare."
        biggest_in = max(x["bytes"] for x in ins)
        o = outs[-1]
        return ("pass" if o["bytes"] < biggest_in else "fail",
                f"Output {Path(o['path']).name} is {o['bytes'] / 1e6:.2f} MB vs input up to {biggest_in / 1e6:.2f} MB")
    if oracle == "exif_gps_removed":
        imgs = [f for f in files if f["ext"] in ("jpg", "jpeg", "heic")]
        if not imgs:
            return "na", "No new photo to inspect."
        src = [f for f in ins if f["ext"] in ("jpg", "jpeg", "heic")]
        src_gps = probe.exif_has_gps(src[-1]["path"]) if src else None
        seeded_gps = any(x.get("gps") for x in (seed or {}).get("files", []))
        if not (src_gps or (not src and seeded_gps)):
            return "na", ("The input photo had no location in it" if src_gps is False else
                          "We couldn't confirm the input photo had a location") + ", so this can't prove the location was removed."
        g = probe.exif_has_gps(imgs[-1]["path"])
        if g is None:
            return "na", "Could not read the photo's metadata."
        return ("fail", f"{Path(imgs[-1]['path']).name} still contains GPS location data") if g else \
               ("pass", f"{Path(imgs[-1]['path']).name} has no GPS location data")
    if oracle in ("share_sheet_opened", "other_app_opened"):
        it = facts.get("intents") or []
        share = [i for i in it if "SEND" in i or "Chooser" in i or "ResolverActivity" in i]
        hit = share if oracle == "share_sheet_opened" else [i for i in it if probe.pkg not in i.split("cmp=")[-1]]
        if arg:
            hit = [i for i in hit if arg.lower() in i.lower()] or hit
        return ("pass", "Android started: " + hit[-1][:140]) if hit else ("fail", "No share sheet / other app was opened.")
    if oracle == "calendar_event_added":
        d = facts.get("calendar_delta")
        if d is None:
            return "na", "Calendar could not be read."
        return ("pass" if d > 0 else "fail", f"Calendar events changed by {d:+d}")
    if oracle == "contacts_changed":
        d = facts.get("contacts_delta")
        if d is None:
            return "na", "Contacts could not be read."
        return ("pass" if d != 0 else "fail", f"Contacts changed by {d:+d}")
    if oracle == "alarm_set":
        d = facts.get("alarms_delta", 0)
        return ("pass" if d > 0 else "fail", f"Scheduled alarms for the app changed by {d:+d}")
    if oracle == "notification_posted":
        d = facts.get("notifications_delta", 0)
        return ("pass" if d > 0 else "fail", f"App notifications changed by {d:+d}")
    if oracle == "text_on_screen":
        if not arg:
            return "na", ""
        blob = " ".join(final_texts).lower()
        return ("pass", f"Screen shows “{arg}”") if arg.lower() in blob else ("fail", f"Screen does not show “{arg}”")
    if oracle in ("video_silent", "output_under_mb"):
        MEDIA = ("jpg", "jpeg", "png", "heic", "webp", "gif", "mp4", "mov", "m4v", "3gp", "webm", "pdf", "m4a", "mp3", "wav", "aac")
        exts = ("mp4", "mov", "m4v") if oracle == "video_silent" else MEDIA
        outs = [f for f in files if f["ext"] in exts and f.get("bytes")]
        if not outs:
            return "na", "No output file to inspect."
        o = outs[-1]
        if oracle == "output_under_mb":
            lim = float(arg or 0)
            mb = o["bytes"] / 1e6
            return ("pass" if mb <= lim else "fail", f"{Path(o['path']).name} is {mb:.2f} MB (limit {lim:g} MB)")
        data = probe.read_bytes(o["path"])
        if not data:
            return "na", "Could not read the video file."
        src = [f for f in ins if f["ext"] in exts]
        sd = probe.read_bytes(src[-1]["path"]) if src else b""
        seeded_sound = any(x.get("kind") == "video" for x in (seed or {}).get("files", []))   # our seeded videos all carry a tone
        if src and sd and not has_sound(sd):
            return "na", "The input video had no sound to begin with, so this can't prove the sound was removed."
        if not (src and sd) and not seeded_sound:
            return "na", "We couldn't confirm the input video had sound, so this can't prove the sound was removed."
        return ("fail", f"{Path(o['path']).name} still has a sound track") if has_sound(data) else \
               ("pass", f"{Path(o['path']).name} has no sound track (the input had one)")
    if oracle == "text_not_on_screen":
        if not arg:
            return "na", ""
        blob = " ".join(final_texts).lower()
        return ("fail", f"Screen still shows \u201c{arg}\u201d") if arg.lower() in blob else ("pass", f"\u201c{arg}\u201d is gone from the result")
    if oracle == "no_app_error_logged":
        errs = facts.get("app_errors") or []
        return ("fail", "App logged: " + errs[-1][-160:]) if errs else ("pass", "No errors logged by the app")
    if oracle == "screen_changed":
        return ("pass", "The screen responded") if screen_changed else ("fail", "Nothing on screen changed after the action")
    return "na", ""


# ───────────────────────────── the agent ─────────────────────────────
class Agent:
    def __init__(self, run: Path, pkg: str, adb: Adb | None = None, sleep=time.sleep):
        self.run, self.pkg, self.adb, self.sleep = run, pkg, adb or Adb(), sleep
        self.summary = json.loads((run / "summary.json").read_text())["summary"]
        self.screens = [s for s in self.summary.get("screen_list") or [] if s.get("ui") is not None]
        self.by_name = {s["name"]: s for s in self.screens}
        self.ios = self.adb.__class__.__name__ == "IOSDevice"
        self.platform = "iPhone" if self.ios else "Android"
        if self.ios:
            from ios_device import IOSProbe
            self.probe = IOSProbe(self.adb, pkg)
        else:
            self.probe = Probe(self.adb, pkg)
        seedf = Path(os.environ.get("GIL_SEED_MANIFEST", "seed_manifest.json"))
        self.seed = json.loads(seedf.read_text()) if seedf.exists() else {"files": []}
        (run / "agent").mkdir(exist_ok=True)
        self.shot_n = 0

    # ── helpers
    def shot(self, tag: str) -> str:
        self.shot_n += 1
        p = self.run / "agent" / f"{self.shot_n:03d}_{tag}.png"
        self.adb.screenshot(p)
        return str(p)

    def ui(self) -> dict:
        prev = None
        for _ in range(6):
            u = parse_ui(self.adb.dump(), self.pkg)
            sig = tuple(el_key(e) for e in u["elements"])
            if sig == prev:
                return u
            prev = sig
            self.sleep(0.8)
        return u

    def answer_dialogs(self, u: dict) -> bool:
        if self.ios:
            return any(e["pkg"] == "com.apple.springboard" for e in u["elements"]) and self.adb.answer_alert()
        for e in u["elements"]:
            if e["pkg"] in PERMISSION_PKGS and ALLOW_RE.match(e.get("text", "")):
                self.adb.tap(e["cx"], e["cy"]); self.sleep(1); return True
        return False

    def go_to(self, screen: dict) -> list[str]:
        """Fresh launch, then replay the recorded taps to reach the screen."""
        self.adb.restart(self.pkg); self.sleep(3)
        human = ["Open the app"]
        for st in screen.get("reach") or []:
            u = self.ui()
            if self.answer_dialogs(u):
                u = self.ui()
            if st["kind"] == "back":
                self.adb.back(); self.sleep(1); human.append("Press Back"); continue
            if st["kind"] == "pick":
                pick_in_picker(self.adb, u, self.sleep); human.append(st.get("human", "Pick a file")); continue
            sel = st.get("el") or {}
            if "label" in sel and "text" not in sel:      # iOS trail entries carry label + pixel bounds
                b = sel.get("bounds") or [0, 0, 0, 0]
                sel = {"rid": sel.get("rid"), "text": sel.get("label"), "desc": sel.get("label"),
                       "cx": (b[0] + b[2]) // 2, "cy": (b[1] + b[3]) // 2}
            el = find_el(u, sel, self.pkg)
            if not el and self.ios and sel.get("cx"):
                el = {"cx": sel["cx"], "cy": sel["cy"], "text": sel.get("text") or "", "cls": "", "rid": "", "desc": ""}
            if not el:
                continue                     # e.g. onboarding no longer shown
            self.adb.tap(el["cx"], el["cy"]); self.sleep(0.6)
            if st["kind"] == "type":
                self.adb.type_text(st.get("value", ""), clear_len=len(el.get("text") or "")); self.adb.hide_keyboard()
            human.append(st.get("human") or f'Tap "{el_label(el)}"')
            self.sleep(1.0)
        return human

    SHARE_SEED = {"image": ("images", "IMG_20261110_100000.jpg", "image/jpeg"),
                  "video": ("video", "VID_20261120_101500.mp4", "video/mp4"),
                  "audio": ("audio", "Voice memo 1.m4a", "audio/mp4"),
                  "pdf": ("file", "Invoice_1042.pdf", "application/pdf")}

    def share_in(self, kind: str, text: str | None = None) -> tuple[bool, str]:
        """Share a sample file (or text) into the app the way a person does from Gallery/Files: Android's Share sheet."""
        self.close_strays(); self.adb.sh(f"am force-stop {self.pkg}"); self.sleep(1)
        if kind == "text" or text:
            out = self.adb.sh(f"am start -W -a android.intent.action.SEND -t text/plain --es android.intent.extra.TEXT "
                              f"{shlex.quote(text or '')} -p {self.pkg}", timeout=40)
            return self._landed(out, f"Share this text to the app: “{text}”")
        coll, name, mime = self.SHARE_SEED.get(kind, self.SHARE_SEED["image"])
        base = "content://media/external/file" if coll == "file" else f"content://media/external/{coll}/media"
        q = self.adb.sh(f"content query --uri {base} --projection _id --where \"_display_name='{name}'\"")
        m = re.search(r"_id=(\d+)", q or "")
        if not m:
            return False, f"No sample {kind} on the phone to share"
        uri = f"{base}/{m.group(1)}"
        out = self.adb.sh(f"am start -W -a android.intent.action.SEND -t {mime} --eu android.intent.extra.STREAM {uri} "
                          f"--grant-read-uri-permission -p {self.pkg}", timeout=40)
        return self._landed(out, f"Share the sample {kind} “{name}” to the app from the phone's Share sheet")

    def front_package(self) -> str:
        """The app really in front. Window focus lags on a cold start; Android's top resumed activity doesn't."""
        if self.ios:
            return self.adb.top_package()
        # ground truth: the app that owns what is on screen, straight from the screen dump
        pk = [p for p in re.findall(r'package="([^"]+)"', self.adb.dump() or "") if p not in ("com.android.systemui", "android")]
        if pk:
            return max(set(pk), key=pk.count)
        out = self.adb.sh("dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity'") or ""
        for key in ("topResumedActivity", "mResumedActivity"):
            m = re.search(key + r"\s*[=:]\s*ActivityRecord\{\S+ u\d+ ([\w.]+)/", out)
            if m:
                return m.group(1)
        return self.adb.top_package()

    def app_in_front(self) -> bool:
        return self.front_package() == self.pkg

    def close_strays(self):
        """A picker or other app left open by the last test must not cover the next one."""
        # press Back until it's gone; never force-stop system packages (the media provider is one of them)
        for _ in range(4):
            if self.front_package() in PICKER_PKGS:
                self.adb.back(); self.sleep(1)
            else:
                return
        for p in PICKER_PKGS:                 # UI apps only: never the media/storage providers
            if ".providers." not in p:
                self.adb.sh(f"am force-stop {p}")
        self.adb.home(); self.sleep(1)

    def _landed(self, out: str, how: str) -> tuple[bool, str]:
        """Android said ok — but is the app really in front now? Report exactly what happened if not."""
        for _ in range(8):
            self.sleep(1.5)
            if self.app_in_front():
                return True, how
        said = " ".join((out or "").split())[:160]
        if os.environ.get("GIL_VERBOSE") == "1":
            lg = self.adb.sh("logcat -d -v brief -t 400", timeout=40)
            keep = [l for l in lg.splitlines() if re.search(r"ActivityTaskManager|ActivityManager|AndroidRuntime|" + re.escape(self.pkg), l)]
            print("agent: share diagnostics ->", said, "\n  " + "\n  ".join(keep[-25:]))
        return False, f"After sharing, the app was not on screen ({self.front_package() or 'nothing'} was); Android said: {said}"

    def find_target(self, u: dict, target: str, near: str = "") -> dict | None:
        t = (target or "").strip().lower()
        if not t:
            return None
        els = [e for e in u["elements"] if e["pkg"] == self.pkg]
        if near:
            nl = near.strip().lower()
            anchors = [(y + h / 2) for tx, y, h, *_ in (u.get("texts") or []) if nl in tx.lower()]
            if not anchors:
                return None
            ay = anchors[0]
            cands = [e for e in els if t == (e.get("text") or e.get("desc") or "").strip().lower()
                     or t in (e.get("text") or e.get("desc") or "").strip().lower()]
            cands = [e for e in cands if abs(e["cy"] - ay) < 220]
            return min(cands, key=lambda e: abs(e["cy"] - ay)) if cands else None
        for test in (lambda l: l == t, lambda l: l.startswith(t), lambda l: t in l, lambda l: l and l in t):
            for e in els:
                lab = " ".join([e.get("text") or "", e.get("desc") or "", e.get("hint") or "", (e.get("rid") or "").replace("_", " ")]).strip().lower()
                if lab and test(lab):
                    return e
        # Compose/Flutter often put the words in a plain text node inside a tappable row: tap the words themselves
        for test in (lambda l: l == t, lambda l: l.startswith(t)):
            for tx, y, h, x1, x2 in (u.get("texts") or []):
                if test((tx or "").strip().lower()):
                    return {"cx": int((x1 + x2) / 2), "cy": int(y + h / 2), "text": tx, "desc": "", "rid": "", "cls": "text",
                            "pkg": self.pkg, "bounds": [x1, y, x2, y + h]}
        return None

    def ai_pick(self, u: dict, intent: str, shot: str) -> dict | None:
        els = [e for e in u["elements"] if e["pkg"] == self.pkg][:50]
        if not els:
            return None
        listing = "\n".join(f"{i}: {e['cls']} “{el_label(e)}” at ({e['cx']},{e['cy']})" for i, e in enumerate(els))
        r = brain.ask(SYS, f"On this screen, which control should be used to: {intent}?\nControls:\n{listing}\n"
                           'Reply {"index": <number or -1 if none fits>, "why": "..."}', [brain.image_part(shot)], "pick-control")
        try:
            i = int((r or {}).get("index", -1))
            return els[i] if 0 <= i < len(els) and not is_forbidden(els[i]) else None
        except Exception:
            return None

    # ── 1. learn
    def learn(self) -> dict:
        cache = self.run / "app_model.json"
        if cache.exists():
            return json.loads(cache.read_text())
        picks = sorted({s["name"]: s for s in self.screens}.values(), key=lambda s: -s.get("controls", 0))[:28]
        notes = []
        for s in picks:
            ctr = ", ".join(sorted({c["label"] for c in s["ui"] if c["label"]})[:40])
            txt = " | ".join((s.get("texts") or [])[:40])
            r = brain.ask(SYS, f"Screen “{s['name']}” of an {self.platform} app.\nVisible text: {txt}\nControls: {ctr}\n"
                               "Describe what a user can DO here. JSON: {\"purpose\":\"...\",\"features\":[{\"name\":\"...\","
                               "\"what_it_does\":\"...\",\"inputs\":[\"...\"],\"controls\":[\"exact control labels used\"],"
                               "\"expected_output\":\"what should happen / be produced\",\"output_kind\":\"file|share|text|calendar|"
                               "contacts|alarm|notification|settings|navigation|none\"}]}",
                          [brain.image_part(s["shot"])], "learn-screen")
            if r:
                notes.append({"screen": s["name"], **r})
        model = brain.ask(SYS, "Here is what each screen of the app offers:\n" + json.dumps(notes)[:60000] +
                          "\nMerge into one feature inventory (no duplicates). For each feature give the screen where it starts. "
                          "JSON: {\"app_summary\":\"...\",\"features\":[{\"id\":\"F1\",\"name\":\"...\",\"purpose\":\"...\","
                          "\"entry_screen\":\"exact screen name from the list\",\"inputs\":[\"...\"],\"expected_result\":\"...\","
                          "\"output_kind\":\"...\",\"risk\":\"high|medium|low\"}]}", None, "learn-merge") or {}
        model["screens_studied"] = len(notes)
        model["screen_notes"] = notes
        cache.write_text(json.dumps(model, indent=1))
        return model

    # ── 2. plan
    def plan(self, model: dict) -> list[dict]:
        cache = self.run / "test_plan.json"
        if cache.exists():
            return json.loads(cache.read_text())
        feats = [f for f in model.get("features", []) if f.get("entry_screen") in self.by_name]
        feats.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f.get("risk"), 1))
        cases = []
        for i in range(0, len(feats), 6):
            batch = feats[i:i + 6]
            ctx = []
            for f in batch:
                s = self.by_name[f["entry_screen"]]
                ctx.append({"feature": f, "controls_on_entry_screen": sorted({c["label"] for c in s["ui"] if c["label"]})[:50]})
            r = brain.ask(SYS,
                f"Write functional test cases for these features of an {self.platform} app. The phone already holds: "
                + (os.environ.get("GIL_SEED_DESC") or "6 camera photos WITH GPS location, 4 screenshots, 2 short videos (8 s and 5 s), "
                   "a voice memo, an MP3, Invoice_1042.pdf, Notes.txt, Expenses.csv, a .vcf and a .zip in Downloads, and 6 contacts "
                   "(one email-only, one with no phone, a duplicate pair).") + "\n"
                "For each feature write 2-4 cases mixing: happy path, edge case, negative/invalid input, boundary, state "
                "(rotate/background mid-task). Steps must use EXACT control labels from controls_on_entry_screen when "
                "starting; later steps may name controls that will appear. Never include buying, paying, deleting accounts "
                "or signing out.\n"
                "Step actions: tap(target), type(target,text), pick(kind: image|video|audio|document|contact), back, "
                "wait(seconds), scroll(down|up), rotate, background.\n"
                f"Objective checks you can request (choose all that apply): {', '.join(ORACLES)}. "
                "Args: new_file: image|video|audio|pdf|document; output_smaller_than_input: image|video|audio; text_on_screen: the text.\n"
                + json.dumps(ctx)[:40000] +
                '\nJSON: {"cases":[{"id":"TC1","feature_id":"F1","title":"...","type":"happy|edge|negative|boundary|state",'
                '"entry_screen":"...","steps":[{"do":"tap","target":"..."},{"do":"type","target":"...","text":"..."},'
                '{"do":"pick","kind":"image"}],"expected":"what a correct app does","checks":[{"oracle":"new_file","arg":"image"}]}]}',
                None, "plan")
            for c in (r or {}).get("cases", []):
                if (c.get("entry_screen") in self.by_name or c.get("entry_screen") == "launch") and c.get("steps"):
                    c["id"] = f"TC{len(cases) + 1:02d}"
                    cases.append(c)
        cache.write_text(json.dumps(cases, indent=1))
        return cases

    # ── 3+4. run one case and verify it
    def run_case(self, c: dict, prefer_judge: int = 0) -> dict:
        res = {"id": c["id"], "title": c.get("title"), "feature_id": c.get("feature_id"), "type": c.get("type"),
               "expected": c.get("expected"), "entry_screen": c.get("entry_screen"), "steps_done": [], "checks": []}
        screen = self.by_name.get(c["entry_screen"]) or {"name": "Home", "reach": []}
        pre = None
        if c.get("launch_args") and self.ios:
            pre = self.probe.snap()          # the tool may start working as soon as it opens
            self.adb.restart(self.pkg, c["launch_args"]); self.sleep(3)
            human = [f"Open the tool directly (launch arguments: {' '.join(c['launch_args'])})"]
        else:
            human = self.go_to(screen)
        u0 = self.ui()
        before_shot = self.shot(f"{c['id']}_before")
        before = pre or self.probe.snap()
        c0 = self.adb.crash_count(self.pkg)
        trail = [{"kind": "launch", "human": "Open the app"}] + list(screen.get("reach") or [])
        blocked = []
        self.timeouts = []
        for st in c.get("steps", [])[:14]:
            do = (st.get("do") or "").lower()
            u = self.ui()
            if self.answer_dialogs(u):
                u = self.ui()
            if do in ("tap", "type"):
                near = st.get("near", "")
                el = self.find_target(u, st.get("target", ""), near)
                for _ in range(14 if near else (3 if st.get("optional") else 2)):
                    if el:
                        break
                    self.adb.scroll("down"); self.sleep(1); u = self.ui()
                    el = self.find_target(u, st.get("target", ""), near)
                if not el and st.get("optional"):
                    continue
                if not el and not near:
                    el = self.ai_pick(u, f'{do} "{st.get("target")}"' + (f' and type "{st.get("text")}"' if do == "type" else ""),
                                      self.shot(f"{c['id']}_find"))
                if not el or is_forbidden(el):
                    blocked.append(f'Could not find “{st.get("target")}”'); human.append(f'(control “{st.get("target")}” not found)')
                    continue
                self.adb.tap(el["cx"], el["cy"]); self.sleep(0.6)
                if do == "type":
                    self.adb.type_text(st.get("text", ""), clear_len=len(el.get("text") or "")); self.adb.hide_keyboard()
                    human.append(f'Type “{st.get("text", "")}” into “{el_label(el)}”')
                    trail.append({"kind": "type", "human": human[-1], "el": _sel(el), "value": st.get("text", "")})
                else:
                    human.append(f'Tap “{el_label(el)}”')
                    trail.append({"kind": "tap", "human": human[-1], "el": _sel(el)})
                self.sleep(1.2)
            elif do == "share_in" and not self.ios:
                ok, how = self.share_in(st.get("kind", "image"), st.get("text"))
                (human.append(how), trail.append({"kind": "share_in", "human": how})) if ok else (blocked.append(how), human.append(f"({how})"))
                self.sleep(3)
            elif do == "pick" and self.ios:
                self.sleep(1.5)
                if self.adb.pick(st.get("kind", "image")):
                    human.append(f'Pick a sample {st.get("kind", "file")}'); trail.append({"kind": "pick", "human": human[-1]})
                else:
                    blocked.append("No picker item could be chosen"); human.append("(no picker item found)")
            elif do == "pick":
                for _ in range(8):                      # the picker can take a few seconds to come up
                    self.sleep(1)
                    if self.front_package() in PICKER_PKGS:
                        break
                if self.front_package() in PICKER_PKGS:
                    pui = parse_ui(self.adb.dump(), "")
                    ok = pick_in_picker(self.adb, pui, self.sleep, st.get("kind"), front=self.front_package)
                    if not ok and os.environ.get("GIL_VERBOSE") == "1":
                        now = parse_ui(self.adb.dump(), "")
                        print(f"agent: picker diagnostics ({self.front_package()}) ->",
                              [((e.get("text") or e.get("desc") or "")[:30], (e.get("rid") or "").split("/")[-1]) for e in now["elements"]][:40])
                    self.sleep(2)
                    if ok and self.app_in_front():
                        human.append(f'Pick a sample {st.get("kind", "file")} in the picker')
                        trail.append({"kind": "pick", "human": human[-1]})
                    else:
                        blocked.append(f'Could not pick a sample {st.get("kind", "file")} in the system picker')
                        human.append("(picker: no sample could be picked)")
                        if not self.app_in_front():
                            self.adb.back(); self.sleep(1)
                else:
                    blocked.append("No file picker opened"); human.append("(no picker opened)")
            elif do == "wait_for":
                want, end, seen = st.get("target", ""), time.time() + min(180, float(st.get("seconds") or 60)), False
                ids, busy = [w for w in want.split("|") if w], st.get("busy", "")
                while time.time() < end:
                    xml = self.adb.dump()
                    if any(f'resource-id="{w}"' in xml for w in ids) and not (busy and f'resource-id="{busy}"' in xml):
                        seen = True; break
                    self.sleep(2)
                human.append("Wait for the result screen" + ("" if seen else " \u2014 it never appeared"))
                if not seen:
                    self.timeouts.append(want)
            elif do == "back":
                self.adb.back(); human.append("Press Back"); trail.append({"kind": "back", "human": "Press Back"}); self.sleep(1)
            elif do == "wait":
                self.sleep(min(20, float(st.get("seconds") or 3))); human.append(f'Wait {st.get("seconds", 3)} s')
            elif do == "scroll":
                self.adb.scroll(st.get("dir", "down"))
                human.append(f'Scroll {st.get("dir", "down")}'); self.sleep(1)
            elif do == "rotate":
                self.adb.rotate(True); self.sleep(1.5); self.adb.rotate(False); self.sleep(1.5)
                human.append("Rotate the phone and back"); trail += [{"kind": "rotate", "landscape": True, "human": "Rotate"}, {"kind": "rotate", "landscape": False, "human": "Rotate back"}]
            elif do == "background":
                self.adb.home(); self.sleep(2); self.adb.launch(self.pkg); self.sleep(2)
                human.append("Send the app to the background and reopen it"); trail += [{"kind": "home", "human": "Home"}, {"kind": "launch", "human": "Reopen"}]
            if self.adb.crash_count(self.pkg) > c0:
                break
            # bring the app back if a share sheet / other app opened, so later steps run in the app
            top = self.front_package()
            if self.ios and top != self.pkg:
                self.sleep(1.5); self.adb.back(); self.sleep(1.2)
            elif top and top != self.pkg and top not in PICKER_PKGS and top not in PERMISSION_PKGS:
                self.sleep(1.5); self.adb.back(); self.sleep(1.2)
        # let long work finish (exports, compression), then read the result
        self.sleep(3)
        u1 = self.ui()
        # results often render in stages (a size or speed row appears once the output has been measured):
        # keep looking for up to 30 s for what the test expects on screen before judging
        expect = [ck for ck in (c.get("checks") or []) if (ck.get("oracle") == "element_present" and not ck.get("absent"))
                  or ck.get("oracle") == "text_on_screen"]
        def _seen(xml: str, texts: list) -> bool:
            for ck in expect:
                if ck["oracle"] == "text_on_screen":
                    if not any((ck.get("arg") or "").lower() in (t or "").lower() for t in texts):
                        return False
                    continue
                alts = [x for x in (ck.get("arg") or "").split("|") if x]
                m = next((re.search(r'resource-id="' + re.escape(x) + r'"[^>]*', xml) for x in alts if f'resource-id="{x}"' in xml), None)
                if not m or (ck.get("contains") and ck["contains"].lower() not in m.group(0).lower()):
                    return False
            return True
        t_end = time.time() + 30
        while expect and time.time() < t_end:
            xml = self.adb.dump()
            if _seen(xml, [t for t, *_ in (parse_ui(xml, self.pkg).get("texts") or [])]):
                break
            self.sleep(2)
        u1 = self.ui()
        after_shot = self.shot(f"{c['id']}_after")
        facts = self.probe.diff(before)
        crashed = self.adb.crash_count(self.pkg) > c0
        final_texts = [t for t, *_ in (u1.get("texts") or [])]
        self.last_xml = self.adb.dump()
        changed = [el_key(e) for e in u0["elements"]] != [el_key(e) for e in u1["elements"]]
        for ck in c.get("checks") or []:
            if ck.get("oracle") == "element_present":
                alts = [x for x in (ck.get("arg") or "").split("|") if x]
                hit = next((x for x in alts if f'resource-id="{x}"' in self.last_xml), None)
                ok = hit is not None
                lab = ck.get("contains")
                if ok and lab:
                    m = re.search(r'resource-id="' + re.escape(hit) + r'"[^>]*', self.last_xml)
                    ok = bool(m and lab.lower() in m.group(0).lower())
                said = ""
                if hit:
                    m2 = re.search(r'resource-id="' + re.escape(hit) + r'"[^>]*', self.last_xml)
                    lab2 = re.search(r'(?:text|content-desc|label)="([^"]{1,200})"', m2.group(0)) if m2 else None
                    said = lab2.group(1) if lab2 else ""
                if ck.get("absent"):
                    ok = not ok
                    if not ok:
                        ck = dict(ck, why_fail=f"The tool stopped and showed: \u201c{said or hit}\u201d")
                res["checks"].append({"oracle": "element_present", "arg": ck.get("arg"), "status": "pass" if ok else "fail",
                                      "evidence": ck.get("why_pass") or ck.get("why") or (f"Finish screen shows \u201c{hit}\u201d" + (f" containing \u201c{lab}\u201d" if lab else ""))
                                      if ok else (ck.get("why_fail") or (f"The result screen never appeared" if len(alts) > 1 else f"\u201c{ck.get('arg')}\u201d" + (f" with \u201c{lab}\u201d" if lab else "") + " never appeared"))})
                continue
            stt, ev = check(ck.get("oracle", ""), ck.get("arg", ""), facts, self.probe, self.seed, final_texts, changed)
            if stt != "na" or ev:   # "can't tell" is kept and shown, never counted as a pass or a fail
                res["checks"].append({"oracle": ck.get("oracle"), "arg": ck.get("arg", ""), "status": stt, "evidence": ev, "pre": bool(ck.get("pre"))})
        res.update({"steps_done": human, "blocked": blocked, "before": before_shot, "after": after_shot,
                    "facts": {k: v for k, v in facts.items() if v}, "crashed": crashed, "trail": trail,
                    "final_texts": final_texts[:60]})
        if crashed:
            res.update({"verdict": "fail", "actual": "The app crashed during this test.", "severity": "critical", "confidence": 1.0})
            return res
        pre_fail = [x for x in res["checks"] if x.get("pre") and x["status"] == "fail"]
        if pre_fail and not crashed:
            blocked = blocked + ["The screen this test needs never appeared (" + pre_fail[0]["evidence"] + ")"]
        res["checks"] = [x for x in res["checks"] if not x.get("pre")]
        if blocked:   # the test did not run as written: it can neither pass nor fail (no fake bugs, no fake passes)
            res.update({"verdict": "blocked", "actual": "; ".join(blocked), "confidence": 1.0})
            return res
        j = brain.ask(SYS,
            f"Judge this functional test.\nTest: {c.get('title')}\nExpected: {c.get('expected')}\n"
            f"Steps actually performed: {json.dumps(human)}\nSteps that could not be performed: {json.dumps(blocked)}\n"
            f"Objective device checks: {json.dumps(res['checks'])}\nDevice facts: {json.dumps(res['facts'])[:3000]}\n"
            f"Text on the final screen: {json.dumps(final_texts[:60])}\n"
            "Image 1 = before, image 2 = after.\nRules: say \"fail\" ONLY if the evidence clearly shows the app did not do "
            "what it should (a failed objective check, an error message, wrong/empty result, nothing happened). If steps "
            "could not be performed or the evidence is unclear, say \"inconclusive\". Do not guess.\n"
            'JSON: {"verdict":"pass|fail|inconclusive","actual":"what actually happened, one sentence",'
            '"evidence":"the specific thing you see that proves it","severity":"critical|high|medium|low","confidence":0.0,'
            '"likely_cause":"if failed: the most likely cause, one sentence","fix":"if failed: what the developer should change, one sentence"}',
            [brain.image_part(before_shot), brain.image_part(after_shot)], "judge", prefer=prefer_judge) or {}
        hard_fail = [x for x in res["checks"] if x["status"] == "fail" and x["oracle"] != "no_app_error_logged"]
        hard_pass = [x for x in res["checks"] if x["status"] == "pass"]
        only_logged = not hard_fail and any(x["status"] == "fail" for x in res["checks"])
        if not j and only_logged:
            res.update({"verdict": "review", "severity": "low", "confidence": 0.5,
                        "actual": "The feature ran, but the app logged an error while doing it: " +
                                  next(x["evidence"] for x in res["checks"] if x["status"] == "fail"), "judge_evidence": ""})
            return res
        if not j:
            # no AI judge available: only objective device checks decide; anything else is for a human
            final = "fail" if hard_fail else ("pass" if hard_pass else ("review" if blocked else "inconclusive"))
            res.update({"verdict": final, "severity": c.get("severity") or "medium", "confidence": 1.0 if hard_fail or hard_pass else 0.0,
                        "actual": "; ".join(x["evidence"] for x in hard_fail) if hard_fail else
                                  ("; ".join(x["evidence"] for x in hard_pass) if hard_pass else "No objective check applied."),
                        "judge_evidence": ""})
            return res
        verdict = j.get("verdict", "inconclusive")
        conf = float(j.get("confidence") or 0)
        if hard_fail and verdict == "fail":
            final = "fail"
        elif hard_fail and verdict != "fail":
            final = "review"                 # device check failed but the judge disagrees -> a human should look
        elif verdict == "fail" and conf >= 0.8 and not hard_pass:
            final = "fail"
        elif verdict == "fail":
            final = "review"
        elif verdict == "pass" or (hard_pass and not hard_fail):
            final = "pass"
        else:
            final = "review" if blocked else "inconclusive"
        res.update({"verdict": final, "actual": j.get("actual", ""), "judge_evidence": j.get("evidence", ""),
                    "likely_cause": j.get("likely_cause", ""), "fix": j.get("fix", ""),
                    "severity": j.get("severity", "medium"), "confidence": conf})
        return res

    # ── main
    def run_all(self, minutes: float):
        t0 = time.time()
        plan_dir = os.environ.get("GIL_PLAN_DIR")
        if plan_dir and Path(plan_dir, "test_plan.json").exists():
            # a human/Claude-written plan: copy it in so learn/plan are skipped
            for f in ("app_model.json", "test_plan.json"):
                if Path(plan_dir, f).exists():
                    (self.run / f).write_text(Path(plan_dir, f).read_text())
        if not brain.available() and not (self.run / "test_plan.json").exists():
            (self.run / "test_results.json").write_text(json.dumps({"skipped": "no AI key configured and no written test plan", "results": []}))
            print("agent: no AI key and no plan — deep functional testing skipped"); return
        model = self.learn()
        print(f"agent: learned {len(model.get('features', []))} features from {model.get('screens_studied', 0)} screens")
        cases = self.plan(model)
        print(f"agent: planned {len(cases)} test cases")
        results = []
        for c in cases:
            if time.time() - t0 > minutes * 60:   # no key -> device checks only, still run everything
                results.append({"id": c["id"], "title": c.get("title"), "feature_id": c.get("feature_id"),
                                "type": c.get("type"), "verdict": "not_run", "expected": c.get("expected")})
                continue
            try:
                r = self.run_case(c)
                if r.get("crashed"):
                    r2 = self.run_case(c)                          # crashes must happen again too
                    r["confirm"] = {"verdict": "fail" if r2.get("crashed") else r2["verdict"], "actual": r2.get("actual")}
                    r["crash_ratio"] = "2/2" if r2.get("crashed") else "1/2"
                elif r["verdict"] == "fail":
                    r2 = self.run_case(c, prefer_judge=1)          # confirm with a fresh run + second opinion
                    r["confirm"] = {"verdict": r2["verdict"], "actual": r2.get("actual")}
                    if r2["verdict"] != "fail":
                        r["verdict"] = "review"
                        r["actual"] = (r.get("actual") or "") + " (did not fail the same way on a second run)"
            except Exception as ex:
                r = {"id": c["id"], "title": c.get("title"), "verdict": "error", "actual": str(ex)[:200]}
            results.append(r)
            print(f"agent: {r['id']} {r['verdict']:<12} " + (c.get('title', '')[:70] + "  ->  " + str(r.get("actual") or "")[:110] if os.environ.get("GIL_VERBOSE") == "1" else ""))
        (self.run / "test_results.json").write_text(json.dumps({"results": results, "features": model.get("features", []),
                                                                "app_summary": model.get("app_summary", ""),
                                                                "usage": brain.usage()}, indent=1))
        self.to_findings(results, model)
        brain.save_log(self.run / "agent" / "brain_log.json")

    def to_findings(self, results: list[dict], model: dict):
        fpath = self.run / "findings.json"
        findings = json.loads(fpath.read_text()) if fpath.exists() else []
        feats = {f.get("id"): f for f in model.get("features", [])}
        for r in results:
            if r.get("verdict") != "fail":
                continue
            f = feats.get(r.get("feature_id"), {})
            crashed = r.get("crashed")
            findings.append({
                "severity": "critical" if crashed else (r.get("severity") or "medium"),
                "category": "crash" if crashed else "functional",
                "title": (f"App crashes during: {r['title']}" if crashed else f"{f.get('name') or 'Feature'}: {r['title']}")[:140],
                "detail": r.get("actual") or "",
                "screen_name": r.get("entry_screen"),
                "repro": ["Install the app fresh and open it"] + [s for s in r.get("steps_done", [])[1:]],
                "expected_text": r.get("expected"), "actual_text": r.get("actual"),
                "test_case": r["id"], "feature": f.get("name"),
                "checks": r.get("checks"), "judge_evidence": r.get("judge_evidence"),
                "fix_text": " ".join(x for x in [("Likely cause: " + r["likely_cause"]) if r.get("likely_cause") else "",
                                                 r.get("fix") or ""] if x),
                "reproduction": ("confirmed" if (r.get("confirm") or {}).get("verdict") == "fail" else
                                 "intermittent" if crashed else ""),
                "reproduction_ratio": r.get("crash_ratio") or ("2/2" if (r.get("confirm") or {}).get("verdict") == "fail" else ""),
                "evidence": {"screenshot": r.get("before"), "after_screenshot": r.get("after"), "t": time.time(),
                             "activity": r.get("entry_screen"),
                             "log": "\n".join((r.get("facts") or {}).get("app_errors") or [])},
                "replay": r.get("trail") if crashed else None,
            })
        fpath.write_text(json.dumps(findings, indent=1))


if __name__ == "__main__":
    run = Path(sys.argv[1])
    if (os.environ.get("GIL_PLATFORM") or "").lower() == "ios":
        from ios_device import IOSDevice
        dev = IOSDevice(os.environ["GIL_CMD_DIR"], os.environ["BUNDLE"], os.environ["UDID"], os.environ["EXE"],
                        str(run / "applog_ios.txt"))
        if not dev._cmd("state", timeout=300).get("state"):        # wait for the remote control to start
            print("agent: the iOS remote control never started"); sys.exit(1)
        print("agent: iOS remote control ready")
        try:
            Agent(run, os.environ["BUNDLE"], adb=dev).run_all(float(os.environ.get("GIL_AGENT_MINUTES") or 45))
        finally:
            dev.quit()
    else:
        Agent(run, os.environ["GIL_PACKAGE"]).run_all(float(os.environ.get("GIL_AGENT_MINUTES") or 45))
