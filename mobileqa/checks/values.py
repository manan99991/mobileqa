"""Value checks: does the arithmetic on the screen add up, are formats consistent,
do the same facts agree across screens (price on list == price on detail)."""
from __future__ import annotations

import re
from datetime import datetime

from ..driver.base import ScreenState
from ..findings import Finding

MONEY_RE = re.compile(r"(?<![\w.])([$€£₹¥]|USD|EUR|GBP|INR)\s?(-?\d{1,3}(?:[,\d]{0,12})(?:\.\d{1,2})?)|(-?\d{1,3}(?:[,\d]{0,12})(?:\.\d{1,2})?)\s?([$€£₹¥]|USD|EUR|GBP|INR)(?![\w])")
QTY_RE = re.compile(r"\b(?:qty|quantity|x)\s*[:=]?\s*(\d{1,4})\b|\b(\d{1,4})\s*[x×]\b", re.I)
PCT_RE = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s?%")
DATE_PATTERNS = [
    ("%Y-%m-%d", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("%m/%d/%Y", re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")),
    ("%d/%m/%Y", re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")),
    ("%b %d, %Y", re.compile(r"\b[A-Z][a-z]{2} \d{1,2}, \d{4}\b")),
    ("%d %b %Y", re.compile(r"\b\d{1,2} [A-Z][a-z]{2} \d{4}\b")),
]


def money(text: str) -> list[float]:
    vals = []
    for m in MONEY_RE.finditer(text or ""):
        num = m.group(2) or m.group(3)
        try:
            vals.append(round(float(num.replace(",", "")), 2))
        except (TypeError, ValueError):
            pass
    return vals


def run_value_checks(state: ScreenState, screen_name: str, step: int, memory: "ValueMemory") -> list[Finding]:
    out: list[Finding] = []
    texts = state.texts
    labelled: dict[str, float] = {}
    for t in texts:
        low = t.lower()
        vals = money(t)
        if not vals:
            continue
        for key in ("total", "subtotal", "tax", "shipping", "discount", "price", "fee", "balance", "due"):
            if key in low and key not in labelled:
                labelled[key] = vals[-1]

    # Total = subtotal + tax + shipping - discount, when those are present
    if "total" in labelled and "subtotal" in labelled:
        expected = labelled["subtotal"] + labelled.get("tax", 0) + labelled.get("shipping", 0) - labelled.get("discount", 0)
        if abs(expected - labelled["total"]) > 0.011:
            out.append(Finding("value", "high", "Total does not add up",
                               f"subtotal {labelled['subtotal']} + tax {labelled.get('tax', 0)} + shipping {labelled.get('shipping', 0)} - discount {labelled.get('discount', 0)} = {expected:.2f}, screen shows total {labelled['total']:.2f}.",
                               state.fingerprint, screen_name, "total", step=step))

    # Total = price × qty when both visible and no subtotal line
    if "total" in labelled and "price" in labelled and "subtotal" not in labelled:
        qty = None
        for t in texts:
            m = QTY_RE.search(t)
            if m:
                qty = int(m.group(1) or m.group(2))
                break
        if qty:
            expected = round(labelled["price"] * qty, 2)
            if abs(expected - labelled["total"]) > 0.011:
                out.append(Finding("value", "high", "Total ≠ price × quantity",
                                   f"{labelled['price']:.2f} × {qty} = {expected:.2f}, screen shows {labelled['total']:.2f}.",
                                   state.fingerprint, screen_name, "total", step=step))

    # Negative money / NaN / absurd
    for t in texts:
        for v in money(t):
            if v < 0 and "refund" not in t.lower() and "discount" not in t.lower() and "credit" not in t.lower():
                out.append(Finding("value", "medium", "Negative amount displayed", f"'{t}'", state.fingerprint, screen_name, t[:30], step=step))
            if v > 1_000_000_000:
                out.append(Finding("value", "medium", "Implausibly large amount", f"'{t}'", state.fingerprint, screen_name, t[:30], step=step))
        for m in PCT_RE.finditer(t):
            p = float(m.group(1))
            if p > 100 or p < -100:
                out.append(Finding("value", "medium", "Percentage out of range", f"'{t}'", state.fingerprint, screen_name, t[:30], step=step))
        if re.search(r"\b(NaN|Infinity|null|undefined)\b", t):
            out.append(Finding("value", "high", "Unformatted programming value shown", f"'{t}'", state.fingerprint, screen_name, t[:30], step=step))
        if re.search(r"\d+\.\d{3,}", t) and "%" not in t and not re.search(r"\d+\.\d+\.\d+", t):
            out.append(Finding("value", "low", "Number with excess decimals", f"'{t}' — probably unformatted float.", state.fingerprint, screen_name, t[:30], step=step))

    # Mixed date formats on one screen
    fmts = set()
    for t in texts:
        for name, rx in DATE_PATTERNS:
            if rx.search(t):
                fmts.add(name.replace("%d/%m/%Y", "%m/%d/%Y"))   # ambiguous slash formats count once
                for m in rx.finditer(t):
                    try:
                        d = datetime.strptime(m.group(0), name)
                        if d.year < 1990 or d.year > datetime.now().year + 20:
                            out.append(Finding("value", "medium", "Date out of plausible range", f"'{t}'", state.fingerprint, screen_name, t[:30], step=step))
                    except ValueError:
                        pass
    if len(fmts) > 1:
        out.append(Finding("ux", "low", "Mixed date formats on one screen", f"Formats: {sorted(fmts)}", state.fingerprint, screen_name, step=step))

    # Cross-screen consistency: a named thing should show the same price everywhere.
    out += memory.observe(state, screen_name, step)
    return out


class ValueMemory:
    """Remembers 'entity -> price' pairs seen on earlier screens and flags disagreements."""

    def __init__(self):
        self.prices: dict[str, tuple[float, str]] = {}

    def observe(self, state: ScreenState, screen_name: str, step: int) -> list[Finding]:
        out = []
        els = [e for e in state.elements if e.visible and (e.text or e.label)]
        # Pair each money value with the nearest non-money text to its left/above (same row-ish).
        for e in els:
            vals = money(e.text)
            if not vals or any(k in e.text.lower() for k in ("total", "subtotal", "tax", "shipping", "discount")):
                continue
            label = None
            best = 10 ** 9
            for o in els:
                if o is e or money(o.text) or len(o.text) < 3 or o.type != "text":
                    continue
                dy = abs(o.rect.cy - e.rect.cy)
                dx = e.rect.x - o.rect.x
                if dy < max(o.rect.h, e.rect.h) * 1.6 and dx >= -10:
                    score = dy * 3 + abs(dx)
                    if score < best:
                        best, label = score, o.text.strip()
            if not label:
                continue
            key = re.sub(r"\W+", " ", label.lower()).strip()[:40]
            if key in self.prices:
                prev, where = self.prices[key]
                if abs(prev - vals[-1]) > 0.011:
                    out.append(Finding("value", "high", "Same item, different price on two screens",
                                       f"'{label}' is {prev:.2f} on '{where}' but {vals[-1]:.2f} on '{screen_name}'.",
                                       state.fingerprint, screen_name, label[:30], step=step))
            else:
                self.prices[key] = (vals[-1], screen_name)
        return out
