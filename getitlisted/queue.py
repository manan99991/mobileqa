#!/usr/bin/env python3
"""
Get It Listed — test queue (Supabase-backed, stdlib only).

  pick            -> prints JSON list of build ids to test now and marks them running.
                     New uploads run right away; then each active Launch-Ready / Both
                     Stores app is re-tested once a day for its 14 days.
  fetch <id>      -> writes the build's details to $GITHUB_ENV, every customer value
                     masked so it never shows in the (public) Actions log.
  finish <id> <ok|failed>

Auth: GitHub OIDC token via engine_api (no stored keys). due() mirrors engine-api.ts.
"""
from __future__ import annotations
import datetime as dt, json, os, re, sys, urllib.parse, urllib.request, urllib.error

SB = os.environ.get("SB_URL") or "https://ljdedtgsijrettyrggpy.supabase.co"
KEY = os.environ.get("SB_SERVICE_KEY", "")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
DAILY_GAP_H = 20
MAX_PER_PICK = 4


def req(method, path, body=None, extra=None):
    r = urllib.request.Request(SB + path, method=method, headers={**H, **(extra or {})},
                               data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            t = resp.read()
            return json.loads(t) if t else None
    except urllib.error.HTTPError as e:
        sys.exit(f"supabase {method} {path.split('?')[0]} -> {e.code} {e.read()[:200]!r}")


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def due(builds, clients, t):
    """Pure scheduling rule (unit-tested): latest build per active paid app."""
    cl = {c["app_key"]: c for c in clients}
    latest = {}
    for b in sorted(builds, key=lambda b: b["created_at"], reverse=True):
        latest.setdefault(b["app_key"], b)
    out = []
    for key, b in latest.items():
        c = cl.get(key)
        if not c or c.get("status") != "active" or c.get("tier") == "pass":
            continue
        if b["status"] == "running" and b.get("last_run_at") and t - parse_ts(b["last_run_at"]) < dt.timedelta(hours=2):
            continue  # a run is in flight
        if b["status"] == "pending":
            out.append((0, b["id"])); continue
        start = dt.date.fromisoformat(c["start_date"])
        if t.date() > start + dt.timedelta(days=13):
            continue  # 14-day window over
        last = parse_ts(b.get("last_run_at"))
        if last is None or t - last >= dt.timedelta(hours=DAILY_GAP_H):
            out.append((1, b["id"]))
    return [i for _, i in sorted(out)][:MAX_PER_PICK]


def forbidden_from_notes(notes: str) -> str:
    words = re.findall(r"\b(?:skip|avoid|don'?t (?:tap|touch|open))\s+(?:the\s+)?([A-Za-z][A-Za-z ]{1,30}?)(?:\s+(?:screen|button|page|tab))?(?=[.,;!\n]|$)",
                       notes or "", re.I)
    return ",".join(w.strip().lower() for w in words if w.strip())


def pick():
    from engine_api import call
    print(json.dumps(call("pick").get("ids", [])))


def fetch(i):
    from engine_api import call
    d = call("fetch", id=int(i))
    vals = {"GIL_APK_URL": d["apk_url"], "GIL_MAPPING_URL": d.get("mapping_url", ""), "GIL_APP_KEY": d["app_key"],
            "GIL_APP_NAME": d["app_name"], "GIL_LOGIN_EMAIL": d.get("login_email", ""),
            "GIL_LOGIN_PASSWORD": d.get("login_password", ""), "GIL_FORBIDDEN": d.get("forbidden", "")}
    for v in vals.values():
        if v:
            print(f"::add-mask::{v}")
    with open(os.environ["GITHUB_ENV"], "a") as f:
        for k, v in vals.items():
            f.write(f"{k}<<__EOV__\n{v}\n__EOV__\n")
    print("build loaded")


def finish(i, result):
    from engine_api import call
    call("finish", id=int(i), ok=(result == "ok"))


def _selftest():
    t = dt.datetime(2026, 11, 22, 12, tzinfo=dt.timezone.utc)
    iso = lambda h: (t - dt.timedelta(hours=h)).isoformat()
    clients = [
        {"app_key": "a", "status": "active", "tier": "launch", "start_date": "2026-11-20"},
        {"app_key": "b", "status": "active", "tier": "pass", "start_date": "2026-11-20"},
        {"app_key": "c", "status": "active", "tier": "both", "start_date": "2026-11-01"},   # window over
        {"app_key": "d", "status": "active", "tier": "launch", "start_date": "2026-11-21"},
        {"app_key": "e", "status": "completed", "tier": "launch", "start_date": "2026-11-21"},
        {"app_key": "f", "status": "active", "tier": "launch", "start_date": "2026-11-21"},
    ]
    builds = [
        {"id": 1, "app_key": "a", "status": "done", "last_run_at": iso(25), "created_at": iso(50)},  # daily due
        {"id": 2, "app_key": "b", "status": "pending", "last_run_at": None, "created_at": iso(1)},  # pass tier
        {"id": 3, "app_key": "c", "status": "done", "last_run_at": iso(30), "created_at": iso(200)},
        {"id": 4, "app_key": "d", "status": "done", "last_run_at": iso(3), "created_at": iso(30)},  # ran recently
        {"id": 5, "app_key": "d", "status": "pending", "last_run_at": None, "created_at": iso(1)},  # new upload wins
        {"id": 6, "app_key": "e", "status": "pending", "last_run_at": None, "created_at": iso(1)},  # not active
        {"id": 7, "app_key": "f", "status": "running", "last_run_at": iso(1), "created_at": iso(5)},  # in flight
    ]
    got = due(builds, clients, t)
    assert got == [5, 1], got
    assert forbidden_from_notes("Skip the Billing screen. Don't tap Reset data, thanks") == "billing,reset data", \
        forbidden_from_notes("Skip the Billing screen. Don't tap Reset data, thanks")
    print("queue selftest OK", got)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "--selftest": _selftest()
    elif cmd == "pick": pick()
    elif cmd == "fetch": fetch(sys.argv[2])
    elif cmd == "finish": finish(sys.argv[2], sys.argv[3])
    else: sys.exit(__doc__)
