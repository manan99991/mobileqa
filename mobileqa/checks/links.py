"""Dead-link checker. Collects every URL visible in the UI tree (text, labels,
accessibility values), the app's configured base URLs, and any URLs the vision
model reads off a screenshot, and verifies them over HTTP."""
from __future__ import annotations

import concurrent.futures as cf
import re
import time

import requests

from ..findings import Finding

URL_RE = re.compile(r"(https?://[^\s'\"<>)\]]+|www\.[a-z0-9.-]+\.[a-z]{2,}[^\s'\"<>)\]]*)", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")

UA = "Mozilla/5.0 (Linux; Android 14) mobileqa/0.1 link-check"


class LinkChecker:
    def __init__(self, timeout: float = 10.0, workers: int = 6):
        self.timeout = timeout
        self.workers = workers
        self.results: dict[str, tuple[int | None, str, float]] = {}   # url -> (status, note, seconds)
        self.seen_on: dict[str, set[str]] = {}

    @staticmethod
    def extract(texts: list[str]) -> list[str]:
        urls = []
        for t in texts:
            for m in URL_RE.findall(t or ""):
                u = m.rstrip(".,;:!?")
                if u.lower().startswith("www."):
                    u = "https://" + u
                if u not in urls:
                    urls.append(u)
        return urls

    def collect(self, texts: list[str], screen: str) -> list[str]:
        new = []
        for u in self.extract(texts):
            self.seen_on.setdefault(u, set()).add(screen)
            if u not in self.results:
                new.append(u)
        return new

    def _probe(self, url: str) -> tuple[int | None, str, float]:
        t0 = time.time()
        try:
            r = requests.head(url, allow_redirects=True, timeout=self.timeout, headers={"User-Agent": UA})
            if r.status_code in (403, 405, 400, 501) or r.status_code >= 500:
                r = requests.get(url, allow_redirects=True, timeout=self.timeout, headers={"User-Agent": UA}, stream=True)
            note = ""
            if r.history:
                note = f"redirected {len(r.history)}x to {r.url}"
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                body = r.text[:4000].lower() if hasattr(r, "text") else ""
                if any(s in body for s in ("page not found", "404", "no longer available", "domain for sale", "parked")):
                    note = (note + " " if note else "") + "200 but body looks like a not-found/parked page"
            return r.status_code, note, time.time() - t0
        except requests.exceptions.SSLError as e:
            return None, f"SSL error: {str(e)[:120]}", time.time() - t0
        except requests.exceptions.ConnectionError as e:
            return None, f"connection failed: {str(e)[:120]}", time.time() - t0
        except requests.exceptions.Timeout:
            return None, f"timeout after {self.timeout}s", time.time() - t0
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {str(e)[:120]}", time.time() - t0

    def check_pending(self) -> list[Finding]:
        pending = [u for u in self.seen_on if u not in self.results]
        if not pending:
            return []
        with cf.ThreadPoolExecutor(self.workers) as ex:
            for url, res in zip(pending, ex.map(self._probe, pending)):
                self.results[url] = res
        return self.findings_for(pending)

    def findings_for(self, urls: list[str]) -> list[Finding]:
        out = []
        for u in urls:
            status, note, secs = self.results[u]
            screens = sorted(self.seen_on.get(u, []))
            scr = screens[0] if screens else ""
            ev = {"url": u, "status": status, "note": note, "seconds": round(secs, 2), "screens": screens}
            if status is None:
                out.append(Finding("link", "high", "Link unreachable", note, scr, element=u, evidence=ev, source="link"))
            elif status >= 500:
                out.append(Finding("link", "high", f"Link returns server error {status}", "", scr, element=u, evidence=ev, source="link"))
            elif status in (404, 410):
                out.append(Finding("link", "high", f"Dead link ({status})", "", scr, element=u, evidence=ev, source="link"))
            elif status in (401, 403):
                out.append(Finding("link", "low", f"Link requires auth ({status})", "", scr, element=u, evidence=ev, source="link"))
            elif status >= 400:
                out.append(Finding("link", "medium", f"Link returns {status}", "", scr, element=u, evidence=ev, source="link"))
            elif "not-found/parked" in note:
                out.append(Finding("link", "medium", "Link resolves to a not-found page", note, scr, element=u, evidence=ev, source="link"))
            elif u.lower().startswith("http://"):
                out.append(Finding("security", "medium", "Insecure http:// link", "", scr, element=u, evidence=ev, source="link"))
            if secs > 5:
                out.append(Finding("perf", "low", "Slow link", f"{u} took {secs:.1f}s", scr, element=u, evidence=ev, source="link"))
        return out
