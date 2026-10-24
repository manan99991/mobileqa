"""App model: screens are nodes, actions are edges. Tracks what has been exercised so
the agent keeps finding new ground, and persists across runs so the suite grows."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..driver.base import ScreenState


@dataclass
class ScreenNode:
    fingerprint: str
    name: str
    visits: int = 0
    first_step: int = 0
    element_gestures: dict[str, list[str]] = field(default_factory=dict)   # element name -> gestures tried
    interactive_names: list[str] = field(default_factory=list)
    screenshot: str = ""                     # path of a representative screenshot
    texts_sample: list[str] = field(default_factory=list)

    def untried(self) -> list[str]:
        out = []
        for n in self.interactive_names:
            done = self.element_gestures.get(n, [])
            if "tap" not in done and "type" not in done:
                out.append(n)
        return out


@dataclass
class Edge:
    src: str
    action: str
    dst: str
    step: int


class AppGraph:
    def __init__(self):
        self.nodes: dict[str, ScreenNode] = {}
        self.edges: list[Edge] = []
        self.path: list[tuple[str, dict]] = []   # (fingerprint, action dict) — the current run's trajectory

    # ---- screens --------------------------------------------------------
    def visit(self, state: ScreenState, name: str, step: int, screenshot_path: str = "") -> tuple[ScreenNode, str]:
        fp = state.fingerprint
        node = self.nodes.get(fp)
        transition = "known screen"
        if node is None:
            node = ScreenNode(fp, name or f"Screen {fp[:4]}", first_step=step, screenshot=screenshot_path,
                              texts_sample=state.texts[:12])
            self.nodes[fp] = node
            transition = "new screen"
        elif name and node.name.startswith("Screen "):
            node.name = name
        node.visits += 1
        names = []
        for e in state.interactive():
            if e.name not in names:
                names.append(e.name)
        for n in names:
            if n not in node.interactive_names:
                node.interactive_names.append(n)
        if screenshot_path and not node.screenshot:
            node.screenshot = screenshot_path
        return node, transition

    def record(self, src_fp: str, action, dst_fp: str, step: int) -> None:
        node = self.nodes.get(src_fp)
        if node is not None:
            key = action.element_name or "@screen"
            if action.element_id is None:
                key = "@screen"
            node.element_gestures.setdefault(key, [])
            if action.kind not in node.element_gestures[key]:
                node.element_gestures[key].append(action.kind)
        self.edges.append(Edge(src_fp, action.describe(), dst_fp, step))
        self.path.append((src_fp, action.to_dict()))

    # ---- queries --------------------------------------------------------
    def coverage_for(self, fp: str) -> dict[str, list[str]]:
        n = self.nodes.get(fp)
        return dict(n.element_gestures) if n else {}

    def visited_names(self) -> list[str]:
        return [n.name for n in self.nodes.values()]

    def frontier(self) -> list[str]:
        return [f"{n.name} ({len(n.untried())} untried)" for n in self.nodes.values() if n.untried()]

    def coverage_pct(self) -> float:
        total = sum(len(n.interactive_names) for n in self.nodes.values())
        if not total:
            return 0.0
        done = sum(len(n.interactive_names) - len(n.untried()) for n in self.nodes.values())
        return round(100 * done / total, 1)

    def stats(self) -> dict:
        return {"screens": len(self.nodes), "edges": len(self.edges),
                "elements": sum(len(n.interactive_names) for n in self.nodes.values()),
                "coverage_pct": self.coverage_pct(),
                "frontier": len(self.frontier())}

    # ---- persistence ----------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"nodes": {k: asdict(v) for k, v in self.nodes.items()},
                "edges": [asdict(e) for e in self.edges[-2000:]]}
        path.write_text(json.dumps(data, indent=1))

    @classmethod
    def load(cls, path: Path) -> "AppGraph":
        g = cls()
        if path.exists():
            data = json.loads(path.read_text())
            for k, v in data.get("nodes", {}).items():
                g.nodes[k] = ScreenNode(**v)
            g.edges = [Edge(**e) for e in data.get("edges", [])]
            for n in g.nodes.values():
                n.visits = 0   # per-run
        return g
