"""
Get It Listed — the engine's brain (vision + reasoning), provider-neutral.

Uses any OpenAI-compatible chat endpoint. Default: Google Gemini's free tier
(GEMINI_API_KEY). Every call is budgeted, retried on rate limits, rotated across
models, and must return JSON. If no key is configured, `available()` is False and
the engine runs its non-AI checks only (and says so in the report).

Env: GEMINI_API_KEY | (GIL_LLM_URL + GIL_LLM_KEY)
     GIL_MODELS     comma list, tried in order (default gemini-flash-latest,gemini-flash-lite-latest)
     GIL_LLM_MAX_CALLS  per-run budget (default 220)
"""
from __future__ import annotations
import base64, io, json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path

URL = os.environ.get("GIL_LLM_URL") or "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
KEY = os.environ.get("GIL_LLM_KEY") or os.environ.get("GEMINI_API_KEY") or ""
MODELS = [m.strip() for m in (os.environ.get("GIL_MODELS") or "gemini-flash-latest,gemini-flash-lite-latest").split(",") if m.strip()]
MAX_CALLS = int(os.environ.get("GIL_LLM_MAX_CALLS") or 220)

_calls = 0
_dead: set[str] = set()     # models that hit their daily quota this run
_log: list[dict] = []


def available() -> bool:
    return bool(KEY) and _calls < MAX_CALLS and len(_dead) < len(MODELS)


def usage() -> dict:
    return {"calls": _calls, "budget": MAX_CALLS, "models": MODELS, "exhausted": sorted(_dead)}


def image_part(path, max_w=720) -> dict | None:
    """Downscaled JPEG data-URL (keeps requests small and fast)."""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, int(im.height * max_w / im.width)))
        b = io.BytesIO(); im.save(b, "JPEG", quality=70)
        data = b.getvalue()
    except Exception:
        try:
            data = Path(path).read_bytes()
        except Exception:
            return None
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}}


def _extract_json(text: str):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1)
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                pass
    return None


def ask(system: str, prompt: str, images: list | None = None, purpose: str = "", temperature: float = 0.2,
        prefer: int = 0) -> dict | list | None:
    """One JSON-returning call. `prefer` picks a different starting model (used for independent second opinions)."""
    global _calls
    if not available():
        return None
    content = [{"type": "text", "text": prompt}] + [p for p in (images or []) if p]
    order = MODELS[prefer % len(MODELS):] + MODELS[:prefer % len(MODELS)]
    for model in order:
        if model in _dead:
            continue
        for attempt in range(4):
            if _calls >= MAX_CALLS:
                return None
            body = {"model": model, "temperature": temperature,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
            req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
            _calls += 1
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    txt = json.loads(r.read())["choices"][0]["message"]["content"]
                out = _extract_json(txt)
                _log.append({"purpose": purpose, "model": model, "ok": out is not None, "s": round(time.time() - t0, 1)})
                if out is not None:
                    return out
                content = content + [{"type": "text", "text": "Reply with valid JSON only."}]
            except urllib.error.HTTPError as e:
                msg = e.read()[:300].decode(errors="ignore")
                _log.append({"purpose": purpose, "model": model, "ok": False, "http": e.code})
                if e.code == 429:
                    if "per day" in msg.lower() or "daily" in msg.lower() or "PerDay" in msg:
                        _dead.add(model); break
                    time.sleep(min(60, 8 * (attempt + 1))); continue
                if e.code in (400, 404):
                    _dead.add(model); break
                time.sleep(5)
            except Exception as ex:
                _log.append({"purpose": purpose, "model": model, "ok": False, "err": str(ex)[:120]})
                time.sleep(5)
    return None


def save_log(path):
    Path(path).write_text(json.dumps({"usage": usage(), "calls": _log}, indent=1))
