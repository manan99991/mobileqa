"""Turn an exploration run into regression cases.

  * one "reach" case per screen discovered (shortest recorded path from launch),
  * one "flow" case per form submission / multi-step business flow,
  * one "bug" case per critical/high finding (its repro steps), stored as open_bug so
    the replayer reports when the bug is fixed and promotes it to a regression guard.
"""
from __future__ import annotations

import re
import time

from ..explorer.explorer import RunResult, StepRecord
from .store import CaseStep, TestCase

_slug = re.compile(r"[^a-z0-9]+")


def slug(s: str, n: int = 40) -> str:
    return _slug.sub("-", s.lower()).strip("-")[:n] or "x"


def _case_steps(path: list[StepRecord], names: dict[str, str], texts_by_fp: dict[str, list[str]]) -> list[CaseStep]:
    out = []
    for s in path:
        out.append(CaseStep(screen=names.get(s.screen_before, s.screen_before_name), action=dict(s.action),
                            expect_screen=names.get(s.screen_after, s.screen_after_name),
                            expect_texts=[t for t in texts_by_fp.get(s.screen_after, [])[:4] if 2 < len(t) < 40 and not re.search(r"\d{2,}", t)],
                            expect_absent=["keeps stopping", "isn't responding", "Something went wrong"]))
    return out


def record_cases(result: RunResult, run_id: str, app_name: str) -> list[TestCase]:
    steps = [s for s in result.steps if s.action.get("kind") not in ("done", "wait")]
    names = {fp: n.name for fp, n in result.graph.nodes.items()}
    texts_by_fp = {fp: n.texts_sample for fp, n in result.graph.nodes.items()}
    cases: list[TestCase] = []
    now = time.strftime("%Y-%m-%d %H:%M")
    if not steps:
        return cases
    launch_fp = steps[0].screen_before

    # --- reach cases: first time each screen was reached, navigation steps since launch
    reach_paths: dict[str, list[StepRecord]] = {launch_fp: []}
    seg_start = 0
    for i, s in enumerate(steps):
        if s.screen_after in names and s.screen_after not in reach_paths and s.screen_after != s.screen_before:
            path = [p for p in steps[seg_start:i + 1] if p.screen_before != p.screen_after or p.action.get("kind") == "type"]
            crashed = any(re.search(r"keeps stopping|has stopped|isn't responding", t, re.I) for t in texts_by_fp.get(s.screen_after, []))
            if path and len(path) <= 15 and not crashed:
                reach_paths[s.screen_after] = path
                nm = names.get(s.screen_after, s.screen_after[:6])
                cases.append(TestCase(id=f"reach-{slug(nm)}", name=f"Reach '{nm}'", kind="reach",
                                      steps=_case_steps(path, names, texts_by_fp), tags=["navigation", app_name],
                                      created=now, created_run=run_id))
        if "(relaunch app)" in str(s.action.get("reason", "")):
            seg_start = i + 1

    # --- flow cases: a run of 'type' steps ending in a tap on the same screen
    i = 0
    while i < len(steps):
        if steps[i].action.get("kind") == "type":
            j = i
            while j < len(steps) and steps[j].action.get("kind") in ("type", "clear") and steps[j].screen_before == steps[i].screen_before:
                j += 1
            if j < len(steps) and steps[j].action.get("kind") == "tap" and steps[j].screen_before == steps[i].screen_before:
                flow = steps[i:j + 1]
                kinds = {s.action.get("data_kind") for s in flow}
                label = "valid" if kinds <= {"valid", "model", ""} else "edge-case"
                start_fp = flow[0].screen_before
                prefix = reach_paths.get(start_fp, []) if start_fp != launch_fp else []
                scr = names.get(start_fp, flow[0].screen_before_name)
                cases.append(TestCase(
                    id=f"flow-{slug(scr)}-{label}-{flow[-1].step:04d}",
                    name=f"{scr}: fill form ({label}) and tap '{flow[-1].action.get('element_name')}' → {names.get(flow[-1].screen_after, 'same screen')}",
                    kind="flow", steps=_case_steps(prefix + flow, names, texts_by_fp), tags=["form", label, app_name],
                    created=now, created_run=run_id))
                i = j + 1
                continue
        i += 1

    # --- bug cases from critical/high findings with repro steps
    for f in result.findings.by_severity():
        if f.severity not in ("critical", "high") or not f.repro or f.source == "link":
            continue
        n = len([r for r in f.repro if not r.startswith("(")])
        path = [s for s in steps if s.step < f.step][-n:] if n else []
        start_fp = path[0].screen_before if path else f.screen
        prefix = reach_paths.get(start_fp, []) if start_fp != launch_fp else []
        if start_fp != launch_fp and start_fp not in reach_paths:
            continue   # we don't know a clean way to get there from a fresh launch
        cs = _case_steps(prefix + path, names, texts_by_fp)
        if not cs:
            cs = [CaseStep(screen=names.get(f.screen, f.screen_name), action={"kind": "wait"}, expect_screen=names.get(f.screen, f.screen_name))]
        cs[-1].expect_absent += ["FATAL", "Exception"]
        cases.append(TestCase(id=f"bug-{f.key}-{slug(f.title, 30)}", name=f"[{f.severity}] {f.title} on '{f.screen_name}'",
                              kind="bug", steps=cs, tags=["bug", f.category, app_name], created=now, created_run=run_id,
                              finding_key=f.key, status="open_bug", notes=f.detail[:300]))

    # --- smoke: launch and look at the first screen
    cases.insert(0, TestCase(id="smoke-launch", name="App launches to its first screen", kind="smoke",
                             steps=[CaseStep(screen="(launch)", action={"kind": "wait"}, expect_screen=names.get(launch_fp, ""),
                                             expect_texts=[t for t in texts_by_fp.get(launch_fp, [])[:3]])],
                             tags=["smoke", app_name], created=now, created_run=run_id))
    return cases
