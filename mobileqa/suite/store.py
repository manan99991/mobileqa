"""Stored test cases. One YAML per case so they diff nicely in git and grow run after run."""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


@dataclass
class CaseStep:
    screen: str                     # screen name where the action happens
    action: dict                    # Action.to_dict()
    expect_screen: str = ""         # screen name expected afterwards ("" = don't care)
    expect_texts: list[str] = field(default_factory=list)   # a few texts that should be visible afterwards
    expect_absent: list[str] = field(default_factory=list)  # texts that must NOT appear (error strings)


@dataclass
class TestCase:
    id: str
    name: str
    kind: str                       # flow | reach | bug | smoke
    steps: list[CaseStep]
    tags: list[str] = field(default_factory=list)
    created: str = ""
    created_run: str = ""
    finding_key: str = ""           # for kind=bug: which finding this reproduces
    status: str = "active"          # active | open_bug | quarantined | retired
    runs: int = 0
    passes: int = 0
    last_result: str = ""           # pass | fail | healed | error
    last_run: str = ""
    healed_count: int = 0
    notes: str = ""

    @property
    def signature(self) -> str:
        sig = "|".join(f"{s.screen}:{s.action.get('kind')}:{s.action.get('element_name')}:{s.action.get('text', '')[:20]}" for s in self.steps)
        return hashlib.sha1(sig.encode()).hexdigest()[:10]


class SuiteStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.cases_dir = self.root / "cases"
        self.cases_dir.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> list[TestCase]:
        out = []
        for p in sorted(self.cases_dir.glob("*.yaml")):
            d = yaml.safe_load(p.read_text()) or {}
            d["steps"] = [CaseStep(**s) for s in d.get("steps", [])]
            out.append(TestCase(**d))
        return out

    def save(self, case: TestCase) -> Path:
        p = self.cases_dir / f"{case.id}.yaml"
        d = asdict(case)
        p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=120))
        return p

    def signatures(self) -> dict[str, TestCase]:
        return {c.signature: c for c in self.load_all()}

    def add_new(self, cases: list[TestCase]) -> list[TestCase]:
        existing = self.signatures()
        by_finding = {c.finding_key: c for c in existing.values() if c.finding_key}
        added = []
        for c in cases:
            if c.signature in existing:
                continue
            if c.finding_key and c.finding_key in by_finding:
                continue
            c.created = c.created or time.strftime("%Y-%m-%d %H:%M")
            self.save(c)
            existing[c.signature] = c
            added.append(c)
        return added

    def stats(self) -> dict:
        cases = self.load_all()
        return {"total": len(cases),
                "active": sum(c.status == "active" for c in cases),
                "open_bugs": sum(c.status == "open_bug" for c in cases),
                "quarantined": sum(c.status == "quarantined" for c in cases)}
