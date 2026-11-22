"""
Get It Listed — iPhone simulator device for the shared functional agent.

Talks to the XCUITest remote-control server (GILCrawler.testServe) through a folder of
numbered command/response JSON files, and exposes the same calls the agent makes on Android
(dump, tap, type_text, back, screenshot, ...). Screen dumps arrive as Android-style XML, so
the same parser, selectors and checks are reused.

IOSProbe gives iPhone versions of the objective device checks: new files in the app's
container and the photo library, calendar / reminders / contacts databases, share sheet seen,
and errors in the app's unified log.
"""
from __future__ import annotations
import glob, json, os, re, sqlite3, subprocess, time
from pathlib import Path

SHARE_MARKERS = re.compile(r"\b(AirDrop|Copy|Save to Files|Save Image|Save Video|Add to Shared Album|Print|Messages|Mail)\b")


class IOSDevice:
    def __init__(self, cmd_dir: str, bundle: str, udid: str, exe: str, applog: str | None = None):
        self.dir, self.bundle, self.udid, self.exe = Path(cmd_dir), bundle, udid, exe
        self.n = 0
        self.applog = applog
        self.t0 = time.time()
        self.seen_share = False

    # ── transport
    def _cmd(self, op: str, timeout: float = 90, **kw) -> dict:
        n = self.n; self.n += 1
        tmp = self.dir / f".cmd_{n}.json"
        tmp.write_text(json.dumps({"op": op, **kw}))
        tmp.rename(self.dir / f"cmd_{n}.json")
        rp = self.dir / f"resp_{n}.json"
        end = time.time() + timeout
        while time.time() < end:
            if rp.exists():
                try:
                    return json.loads(rp.read_text())
                except Exception:
                    time.sleep(0.1); continue
            time.sleep(0.1)
        return {"ok": False, "timeout": True}

    def quit(self): self._cmd("quit", timeout=10)

    # ── the calls the agent uses
    def dump(self) -> str:
        x = self._cmd("dump").get("xml") or "<hierarchy/>"
        if SHARE_MARKERS.search(x) and ("ActivityList" in x or x.count("AirDrop") or "Save to Files" in x):
            self.seen_share = True
        return x

    def screenshot(self, path): self._cmd("screenshot", path=str(Path(path).resolve()))
    def tap(self, x, y): self._cmd("tap", x=int(x), y=int(y))
    def type_text(self, text, clear_len=0):
        if clear_len:
            self._cmd("clear", n=min(clear_len + 2, 80))
        self._cmd("type", text=text)
    def hide_keyboard(self): self._cmd("hide_keyboard")
    def back(self):
        if self.state() != 4:
            self._cmd("launch")
        else:
            self._cmd("back")
    def home(self): self._cmd("home")
    def launch(self, pkg=None): self._cmd("launch")
    def restart(self, pkg=None): self._cmd("relaunch", timeout=120)
    def rotate(self, landscape: bool): self._cmd("rotate", landscape=landscape)
    def scroll(self, direction="down"): self._cmd("swipe", dir="up" if direction == "down" else "down")
    def state(self) -> int: return int(self._cmd("state").get("state") or 0)
    def top_package(self) -> str: return self.bundle if self.state() == 4 else "other"
    def answer_alert(self) -> bool: return bool(self._cmd("alert").get("handled"))

    def pick(self, kind="image") -> bool:
        """In the iOS photo/file picker (shown inside the app): pick the first item, then confirm."""
        from crawler import parse_ui
        want = {"image": r"^(photo|image|screenshot)", "video": r"^video", "audio": r"\.(m4a|mp3)",
                "document": r"\.(pdf|txt|csv|vcf|zip)", "contact": r"."}.get(kind, r"^(photo|image)")
        for _ in range(4):
            u = parse_ui(self.dump(), self.bundle)
            els = u["elements"]
            ok = [e for e in els if re.match(r"^(add|done|choose|open|select)$", (e.get("text") or e.get("desc") or "").strip(), re.I)]
            items = [e for e in els if re.search(want, (e.get("text") or e.get("desc") or ""), re.I)]
            if items:
                it = sorted(items, key=lambda e: (e["cy"], e["cx"]))[0]
                self.tap(it["cx"], it["cy"]); time.sleep(1.2)
                u2 = parse_ui(self.dump(), self.bundle)
                ok = [e for e in u2["elements"] if re.match(r"^(add|done|choose|open|select)$", (e.get("text") or e.get("desc") or "").strip(), re.I)]
                if ok:
                    self.tap(ok[0]["cx"], ok[0]["cy"]); time.sleep(1.2)
                return True
            if ok:
                self.tap(ok[0]["cx"], ok[0]["cy"]); time.sleep(1.2); continue
            return False
        return False

    def crash_count(self, pkg=None) -> int:
        n = 0
        for p in glob.glob(os.path.expanduser("~/Library/Logs/DiagnosticReports/**/*.ips"), recursive=True):
            if os.path.getmtime(p) >= self.t0 - 5:
                try:
                    head = Path(p).read_text(errors="ignore")[:4000]
                    if f'"{self.exe}"' in head or f'"procName" : "{self.exe}"' in head:
                        n += 1
                except Exception:
                    pass
        return n

    def anr_count(self, pkg=None) -> int: return 0
    def pids(self, pkg=None) -> list: return []
    def sh(self, cmd, timeout=30) -> str: return ""


class IOSProbe:
    """Objective before/after evidence on the iPhone simulator."""

    def __init__(self, dev: IOSDevice, pkg: str):
        self.dev, self.pkg = dev, pkg
        self.data = Path(os.path.expanduser(f"~/Library/Developer/CoreSimulator/Devices/{dev.udid}/data"))
        try:
            self.container = Path(subprocess.run(["xcrun", "simctl", "get_app_container", dev.udid, pkg, "data"],
                                                 capture_output=True, text=True, timeout=30).stdout.strip())
        except Exception:
            self.container = None

    def _count(self, db: Path, table: str) -> int:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            n = con.execute(f"select count(*) from {table}").fetchone()[0]
            con.close(); return n
        except Exception:
            return -1

    def snap(self) -> dict:
        self.dev.seen_share = False
        return {"t": time.time(),
                "calendar": self._count(self.data / "Library/Calendar/Calendar.sqlitedb", "CalendarItem"),
                "contacts": self._count(self.data / "Library/AddressBook/AddressBook.sqlitedb", "ABPerson"),
                "photos": len(glob.glob(str(self.data / "Media/DCIM/**/*.*"), recursive=True))}

    def _new_files(self, root: Path | None, since: float) -> list[dict]:
        out = []
        if not root or not root.exists():
            return out
        for dp, _, fs in os.walk(root):
            if "/Library/Caches/com.apple" in dp or "/SplashBoard" in dp:
                continue
            for f in fs:
                p = Path(dp) / f
                try:
                    st = p.stat()
                except Exception:
                    continue
                if st.st_mtime >= since and not f.startswith(".") and not f.endswith((".plist", ".db", ".sqlite", "-wal", "-shm", ".log")):
                    out.append({"path": str(p), "bytes": st.st_size, "ext": f.rsplit(".", 1)[-1].lower() if "." in f else ""})
        return out[:40]

    def diff(self, before: dict) -> dict:
        t = before["t"]
        files = self._new_files(self.container, t) + self._new_files(self.data / "Media/DCIM", t)
        after = self.snap() if False else {
            "calendar": self._count(self.data / "Library/Calendar/Calendar.sqlitedb", "CalendarItem"),
            "contacts": self._count(self.data / "Library/AddressBook/AddressBook.sqlitedb", "ABPerson")}
        facts = {"new_files": files, "new_private_files": []}
        for k in ("calendar", "contacts"):
            if before.get(k, -1) >= 0 and after.get(k, -1) >= 0:
                facts[f"{k}_delta"] = after[k] - before[k]
        facts["intents"] = ["iOS share sheet (UIActivityViewController) opened"] if self.dev.seen_share else []
        errs = []
        if self.dev.applog and Path(self.dev.applog).exists():
            for line in Path(self.dev.applog).read_text(errors="ignore").splitlines()[-4000:]:
                m = re.match(r"^(\S+ \S+)\s+(E|F)\s", line)
                if m:
                    try:
                        ts = time.mktime(time.strptime(m.group(1)[:19], "%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        ts = t
                    if ts >= t - 1:
                        errs.append(line[:240])
        facts["app_errors"] = errs[-6:]
        return facts

    def exif_has_gps(self, path: str):
        try:
            from PIL import Image
            return bool(dict(Image.open(path).getexif().get_ifd(0x8825)))
        except Exception:
            return None
