"""Thin wrapper over the Claude API: JSON in, JSON out, images optional, retries."""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time

log = logging.getLogger("mobileqa.llm")


def shrink_png(png: bytes, max_w: int = 900) -> tuple[str, str]:
    """Return (base64, media_type) of a down-scaled JPEG — plenty for UI judgement, cheap on tokens."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


class LLM:
    """Backend-agnostic JSON-in/JSON-out wrapper with optional images.

    Two backends, auto-selected:
      * OpenAI-compatible (free vision models — Google Gemini, Groq, GitHub Models, …): set
        MOBILEQA_LLM_BASE_URL + MOBILEQA_LLM_API_KEY, and MOBILEQA_MODEL/MOBILEQA_VISION_MODEL to the
        provider's model id (e.g. gemini-2.5-flash). This makes a thorough, vision-driven run free.
      * Anthropic (ANTHROPIC_API_KEY) — the default when no base_url is set.
    Vision input works on both. If neither is configured, `available` is False and the caller falls
    back to the heuristic brain.
    """

    def __init__(self, model: str, api_key: str | None = None, max_retries: int = 3):
        self.model = model
        self.max_retries = max_retries
        self.calls = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self._client = None
        self._kind: str | None = None
        base_url = os.environ.get("MOBILEQA_LLM_BASE_URL")
        if base_url:   # free / OpenAI-compatible endpoint takes priority when explicitly configured
            okey = api_key or os.environ.get("MOBILEQA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if okey:
                try:
                    from openai import OpenAI
                    self._client = OpenAI(api_key=okey, base_url=base_url)
                    self._kind = "openai"
                except Exception as e:  # noqa: BLE001
                    log.warning("openai-compatible client unavailable: %s", e)
        if self._client is None:
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if key:
                try:
                    import anthropic
                    self._client = anthropic.Anthropic(api_key=key)
                    self._kind = "anthropic"
                except Exception as e:  # noqa: BLE001
                    log.warning("anthropic client unavailable: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None

    def json(self, system: str, user: str, images: list[bytes] | None = None, max_tokens: int = 1500) -> dict:
        if not self._client:
            raise RuntimeError("No LLM backend — set MOBILEQA_LLM_BASE_URL + MOBILEQA_LLM_API_KEY "
                               "(free, e.g. Gemini) or ANTHROPIC_API_KEY")
        if self._kind == "openai":
            return self._json_openai(system, user, images, max_tokens)
        return self._json_anthropic(system, user, images, max_tokens)

    def _json_anthropic(self, system, user, images, max_tokens) -> dict:
        content: list[dict] = []
        for png in images or []:
            b64, mt = shrink_png(png)
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
        content.append({"type": "text", "text": user})
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.messages.create(
                    model=self.model, max_tokens=max_tokens, temperature=0.2,
                    system=system, messages=[{"role": "user", "content": content}])
                self.calls += 1
                self.in_tokens += getattr(resp.usage, "input_tokens", 0)
                self.out_tokens += getattr(resp.usage, "output_tokens", 0)
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                return parse_json(text)
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("LLM call failed (%s/%s): %s", attempt + 1, self.max_retries, e)
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM failed after retries: {last_err}")

    def _json_openai(self, system, user, images, max_tokens) -> dict:
        parts: list[dict] = []
        for png in images or []:
            b64, mt = shrink_png(png)
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}})
        parts.append({"type": "text", "text": user})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": parts}]
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model, max_tokens=max_tokens, temperature=0.2, messages=messages)
                self.calls += 1
                u = getattr(resp, "usage", None)
                if u:
                    self.in_tokens += getattr(u, "prompt_tokens", 0) or 0
                    self.out_tokens += getattr(u, "completion_tokens", 0) or 0
                text = resp.choices[0].message.content or ""
                return parse_json(text)
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("LLM call failed (%s/%s): %s", attempt + 1, self.max_retries, e)
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM failed after retries: {last_err}")


def parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # grab the outermost {...}
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"model did not return JSON: {text[:200]}")
