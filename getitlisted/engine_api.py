"""Call the Get It Listed engine API with a GitHub OIDC token (no stored keys)."""
from __future__ import annotations
import json, os, urllib.request, urllib.error

API = os.environ.get("GIL_API") or "https://ljdedtgsijrettyrggpy.supabase.co/functions/v1/engine-api"
AUD = "getitlisted-engine"


def oidc_token() -> str:
    url, tok = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not tok:
        raise SystemExit("no GitHub OIDC token (needs `permissions: id-token: write`)")
    r = urllib.request.Request(f"{url}&audience={AUD}", headers={"Authorization": f"bearer {tok}"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read())["value"]


def call(op: str, **kw) -> dict:
    r = urllib.request.Request(API, method="POST", data=json.dumps({"op": op, **kw}).encode(),
                               headers={"Authorization": f"Bearer {oidc_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"engine-api {op} -> {e.code} {e.read()[:200]!r}")


def put_file(url: str, data: bytes, ctype: str):
    r = urllib.request.Request(url, method="PUT", data=data, headers={"Content-Type": ctype, "x-upsert": "true"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.status
