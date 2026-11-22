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
import json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import brain
from crawler import (Adb, parse_ui, el_label, el_key, _sel, _loc, find_el, pick_in_picker, is_forbidden,
                     PICKER_PKGS, PERMISSION_PKGS, ALLOW_RE)

SYS = ("You are a senior mobile QA engineer. You test apps the way a careful human does: understand what each "
       "feature is for, then check it really does its job. You never invent behaviour you cannot see. "
       "Reply with JSON only.")

ORACLES = ["new_file", "output_smaller_than_input", "exif_gps_removed", "share_sheet_opened", "other_app_opened",
           "calendar_event_added", "contacts_changed", "alarm_set", "notification_posted", "text_on_screen",
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
        priv = self.adb.sh(f"run-as {self.pkg} find files cache -newer /dev/null -mmin -3 -type f 2>/dev/null | head -20").split()
        facts["new_files"] = files
        facts["new_private_files"] = priv[:20]
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

    def exif_has_gps(self, path: str) -> bool | None:
        try:
            from PIL import Image
            loc = Path(tempfile.mkdtemp()) / "o.jpg"
            subprocess.run(["adb", "pull", path, str(loc)], capture_output=True, timeout=60)
            ex = Image.open(loc).getexif()
            return bool(dict(ex.get_ifd(0x8825)))
        except Exception:
            return None


def check(oracle: str, arg: str, facts: dict, probe: Probe, seed: dict, final_texts: list, screen_changed: bool):
    """Deterministic verdict for one expected outcome: (status, evidence) with status pass|fail|na."""
    files = facts.get("new_files") or []
    kinds = {"image": ("jpg", "jpeg", "png", "webp", "heic"), "video": ("mp4", "mov", "3gp", "webm"),
             "audio": ("m4a", "mp3", "wav", "aac", "ogg"), "pdf": ("pdf",), "document": ("pdf", "txt", "csv", "docx", "xlsx", "vcf", "zip")}
    if oracle == "new_file":
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
        imgs = [f for f in files if f["ext"] in ("jpg", "jpeg")]
        if not imgs:
            return "na", "No new photo to inspect."
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
        for e in u["elements"]:
            if e["pkg"] in PERMISSION_PKGS and ALLOW_RE.match(e.get("text", "")):
                self.adb.tap(e["cx"], e["cy"]); self.sleep(1); return True
        return False

    def go_to(self, screen: dict) -> list[str]:
        """Fresh launch, then replay the recorded taps to reach the screen."""
        self.adb.sh(f"am force-stop {self.pkg}")
        self.adb.launch(self.pkg); self.sleep(3)
        human = ["Open the app"]
        for st in screen.get("reach") or []:
            u = self.ui()
            if self.answer_dialogs(u):
                u = self.ui()
            if st["kind"] == "back":
                self.adb.back(); self.sleep(1); human.append("Press Back"); continue
            if st["kind"] == "pick":
                pick_in_picker(self.adb, u, self.sleep); human.append(st.get("human", "Pick a file")); continue
            el = find_el(u, st.get("el") or {}, self.pkg)
            if not el:
                continue                     # e.g. onboarding no longer shown
            self.adb.tap(el["cx"], el["cy"]); self.sleep(0.6)
            if st["kind"] == "type":
                self.adb.type_text(st.get("value", ""), clear_len=len(el.get("text") or "")); self.adb.hide_keyboard()
            human.append(st.get("human") or f'Tap "{el_label(el)}"')
            self.sleep(1.0)
        return human

    def find_target(self, u: dict, target: str) -> dict | None:
        t = (target or "").strip().lower()
        if not t:
            return None
        els = [e for e in u["elements"] if e["pkg"] == self.pkg]
        for test in (lambda l: l == t, lambda l: l.startswith(t), lambda l: t in l, lambda l: l and l in t):
            for e in els:
                lab = " ".join([e.get("text") or "", e.get("desc") or "", e.get("hint") or "", (e.get("rid") or "").replace("_", " ")]).strip().lower()
                if lab and test(lab):
                    return e
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
            r = brain.ask(SYS, f"Screen “{s['name']}” of an Android app.\nVisible text: {txt}\nControls: {ctr}\n"
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
                "Write functional test cases for these features of an Android app. The phone already holds: "
                "6 camera photos WITH GPS location, 4 screenshots, 2 short videos (8 s and 5 s), a voice memo, an MP3, "
                "Invoice_1042.pdf, Notes.txt, Expenses.csv, a .vcf and a .zip in Downloads, and 6 contacts "
                "(one email-only, one with no phone, a duplicate pair).\n"
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
                if c.get("entry_screen") in self.by_name and c.get("steps"):
                    c["id"] = f"TC{len(cases) + 1:02d}"
                    cases.append(c)
        cache.write_text(json.dumps(cases, indent=1))
        return cases

    # ── 3+4. run one case and verify it
    def run_case(self, c: dict, prefer_judge: int = 0) -> dict:
        res = {"id": c["id"], "title": c.get("title"), "feature_id": c.get("feature_id"), "type": c.get("type"),
               "expected": c.get("expected"), "entry_screen": c.get("entry_screen"), "steps_done": [], "checks": []}
        screen = self.by_name[c["entry_screen"]]
        human = self.go_to(screen)
        u0 = self.ui()
        before_shot = self.shot(f"{c['id']}_before")
        before = self.probe.snap()
        c0 = self.adb.crash_count(self.pkg)
        trail = [{"kind": "launch", "human": "Open the app"}] + list(screen.get("reach") or [])
        blocked = []
        for st in c.get("steps", [])[:14]:
            do = (st.get("do") or "").lower()
            u = self.ui()
            if self.answer_dialogs(u):
                u = self.ui()
            if do in ("tap", "type"):
                el = self.find_target(u, st.get("target", ""))
                for _ in range(2):
                    if el:
                        break
                    self.adb.sh("input swipe 540 1700 540 700 300"); self.sleep(1); u = self.ui()
                    el = self.find_target(u, st.get("target", ""))
                if not el:
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
            elif do == "pick":
                self.sleep(1.5)
                if self.adb.top_package() in PICKER_PKGS:
                    pick_in_picker(self.adb, parse_ui(self.adb.dump(), ""), self.sleep)
                    human.append(f'Pick a sample {st.get("kind", "file")} in the picker')
                    trail.append({"kind": "pick", "human": human[-1]})
                else:
                    blocked.append("No file picker opened"); human.append("(no picker opened)")
            elif do == "back":
                self.adb.back(); human.append("Press Back"); trail.append({"kind": "back", "human": "Press Back"}); self.sleep(1)
            elif do == "wait":
                self.sleep(min(20, float(st.get("seconds") or 3))); human.append(f'Wait {st.get("seconds", 3)} s')
            elif do == "scroll":
                self.adb.sh("input swipe 540 1700 540 700 300" if st.get("dir", "down") == "down" else "input swipe 540 700 540 1700 300")
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
            top = self.adb.top_package()
            if top and top != self.pkg and top not in PICKER_PKGS and top not in PERMISSION_PKGS:
                self.sleep(1.5); self.adb.back(); self.sleep(1.2)
        # let long work finish (exports, compression), then read the result
        self.sleep(3)
        u1 = self.ui()
        after_shot = self.shot(f"{c['id']}_after")
        facts = self.probe.diff(before)
        crashed = self.adb.crash_count(self.pkg) > c0
        final_texts = [t for t, *_ in (u1.get("texts") or [])]
        changed = [el_key(e) for e in u0["elements"]] != [el_key(e) for e in u1["elements"]]
        for ck in c.get("checks") or []:
            stt, ev = check(ck.get("oracle", ""), ck.get("arg", ""), facts, self.probe, self.seed, final_texts, changed)
            if stt != "na":
                res["checks"].append({"oracle": ck.get("oracle"), "arg": ck.get("arg", ""), "status": stt, "evidence": ev})
        res.update({"steps_done": human, "blocked": blocked, "before": before_shot, "after": after_shot,
                    "facts": {k: v for k, v in facts.items() if v}, "crashed": crashed, "trail": trail,
                    "final_texts": final_texts[:60]})
        if crashed:
            res.update({"verdict": "fail", "actual": "The app crashed during this test.", "severity": "critical", "confidence": 1.0})
            return res
        if blocked and len(blocked) >= max(1, len(c.get("steps", [])) // 2):
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
        hard_fail = [x for x in res["checks"] if x["status"] == "fail"]
        hard_pass = [x for x in res["checks"] if x["status"] == "pass"]
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
        if not brain.available():
            (self.run / "test_results.json").write_text(json.dumps({"skipped": "no AI key configured", "results": []}))
            print("agent: no AI key — deep functional testing skipped"); return
        model = self.learn()
        print(f"agent: learned {len(model.get('features', []))} features from {model.get('screens_studied', 0)} screens")
        cases = self.plan(model)
        print(f"agent: planned {len(cases)} test cases")
        results = []
        for c in cases:
            if time.time() - t0 > minutes * 60 or not brain.available():
                results.append({"id": c["id"], "title": c.get("title"), "feature_id": c.get("feature_id"),
                                "type": c.get("type"), "verdict": "not_run", "expected": c.get("expected")})
                continue
            try:
                r = self.run_case(c)
                if r["verdict"] == "fail" and not r.get("crashed"):
                    r2 = self.run_case(c, prefer_judge=1)          # confirm with a fresh run + second opinion
                    r["confirm"] = {"verdict": r2["verdict"], "actual": r2.get("actual")}
                    if r2["verdict"] != "fail":
                        r["verdict"] = "review"
                        r["actual"] = (r.get("actual") or "") + " (did not fail the same way on a second run)"
            except Exception as ex:
                r = {"id": c["id"], "title": c.get("title"), "verdict": "error", "actual": str(ex)[:200]}
            results.append(r)
            print(f"agent: {r['id']} {r['verdict']:<12} " + (c.get('title', '')[:70] if os.environ.get("GIL_VERBOSE") == "1" else ""))
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
                "reproduction": "confirmed" if (r.get("confirm") or {}).get("verdict") == "fail" else "",
                "reproduction_ratio": "2/2" if (r.get("confirm") or {}).get("verdict") == "fail" else "",
                "evidence": {"screenshot": r.get("before"), "after_screenshot": r.get("after"), "t": time.time(),
                             "activity": r.get("entry_screen"),
                             "log": "\n".join((r.get("facts") or {}).get("app_errors") or [])},
                "replay": r.get("trail") if crashed else None,
            })
        fpath.write_text(json.dumps(findings, indent=1))


if __name__ == "__main__":
    run = Path(sys.argv[1])
    Agent(run, os.environ["GIL_PACKAGE"]).run_all(float(os.environ.get("GIL_AGENT_MINUTES") or 45))
