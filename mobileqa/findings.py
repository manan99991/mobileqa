"""Finding = one defect the agent would file as a bug."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = ("critical", "high", "medium", "low", "info")


@dataclass
class Finding:
    category: str            # crash | functional | value | link | ui | ux | a11y | perf | security | content
    severity: str            # critical | high | medium | low | info
    title: str
    detail: str = ""
    screen: str = ""         # screen fingerprint
    screen_name: str = ""    # human name of the screen
    element: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)   # screenshot path, log lines, url, expected/actual
    repro: list[str] = field(default_factory=list)           # human-readable steps that got here
    source: str = "rule"     # rule | vision | model | link | replay
    step: int = 0

    @property
    def key(self) -> str:
        """Dedupe key: same bug on the same screen is one finding."""
        base = f"{self.category}|{self.title}|{self.screen}|{self.element}"
        return hashlib.sha1(base.encode()).hexdigest()[:10]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d


class FindingSink:
    def __init__(self, known_issues: list[str] | None = None):
        self.items: dict[str, Finding] = {}
        self.known = [k.lower() for k in (known_issues or [])]

    def add(self, f: Finding) -> bool:
        if any(k in f.title.lower() or k in f.detail.lower() for k in self.known):
            f.severity = "info"
            f.title = "[known] " + f.title
        if f.key in self.items:
            return False
        self.items[f.key] = f
        return True

    def extend(self, fs: list[Finding]) -> int:
        return sum(self.add(f) for f in fs)

    def by_severity(self) -> list[Finding]:
        order = {s: i for i, s in enumerate(SEVERITIES)}
        return sorted(self.items.values(), key=lambda f: (order.get(f.severity, 9), f.category, f.title))

    def count(self, *sev: str) -> int:
        return sum(1 for f in self.items.values() if f.severity in sev)

    def __len__(self):
        return len(self.items)
