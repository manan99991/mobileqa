"""HTML report + JUnit XML + summary JSON for a run."""
from __future__ import annotations

import json
import time
from pathlib import Path
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader

from ..brain.actions import Action
from ..config import Config
from ..explorer.explorer import RunResult

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def gate(cfg: Config, result: RunResult | None, replay_results) -> bool:
    """True = release blocked."""
    blocked = False
    if result is not None:
        blocked = result.findings.count(*cfg.run.fail_on) > 0
    for r in replay_results or []:
        if r.result in ("fail", "error") or (r.result == "still_failing" and r.case.kind == "bug" and "critical" in r.case.name):
            blocked = True
    return blocked


def write_report(cfg: Config, run_dir: Path, run_id: str, mode: str, result: RunResult | None,
                 replay_results=None, suite_stats: dict | None = None, added: int = 0, llm_calls: int = 0) -> Path:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    tpl = env.get_template("report.html.j2")
    findings = []
    summary = {"steps": 0, "screens": 0, "coverage_pct": 0, "critical": 0, "high": 0, "medium": 0, "low": 0, "duration_s": 0}
    screens, steps, links = [], [], {}
    if result is not None:
        summary = result.summary()
        findings = [f.to_dict() for f in result.findings.by_severity()]
        for n in result.graph.nodes.values():
            screens.append({"name": n.name, "screenshot": n.screenshot, "visits": n.visits,
                            "total": len(n.interactive_names), "done": len(n.interactive_names) - len(n.untried())})
        for s in result.steps:
            steps.append({"step": s.step, "screen_before_name": s.screen_before_name,
                          "action_text": Action.from_dict(s.action).describe(),
                          "screen_after_name": s.screen_after_name or (result.graph.nodes.get(s.screen_after).name if s.screen_after in result.graph.nodes else ""),
                          "duration_ms": s.duration_ms})
        lp = run_dir / "links.json"
        if lp.exists():
            links = json.loads(lp.read_text())
    replay = None
    rr = []
    if replay_results is not None:
        replay = {"total": len(replay_results)}
        for k in ("pass", "healed", "fail", "error", "still_failing", "fixed"):
            replay[k] = sum(1 for r in replay_results if r.result == k)
        rr = [{"result": r.result, "name": r.case.name, "kind": r.case.kind, "message": r.message} for r in replay_results]
    blocked = gate(cfg, result, replay_results)
    html = tpl.render(app=cfg.app.name, platform=cfg.platform, run_id=run_id, started=time.strftime("%Y-%m-%d %H:%M"),
                      mode=mode, summary=summary, findings=findings, screens=screens, steps=steps, links=links,
                      replay=replay, replay_results=rr, suite=suite_stats, added=added, gate_failed=blocked,
                      fail_on=cfg.run.fail_on, model=cfg.run.model, llm_calls=llm_calls)
    out = run_dir / "report.html"
    out.write_text(html, encoding="utf-8")   # report contains ≠, ·, … — force utf-8 (Windows defaults to cp1252)
    (run_dir / "summary.json").write_text(json.dumps({"run_id": run_id, "mode": mode, "blocked": blocked, "summary": summary,
                                                       "replay": replay, "suite": suite_stats, "new_cases": added}, indent=1))
    write_junit(run_dir / "junit.xml", cfg, result, replay_results)
    return out


def write_junit(path: Path, cfg: Config, result: RunResult | None, replay_results) -> None:
    cases = []
    if result is not None:
        for f in result.findings.by_severity():
            failed = f.severity in cfg.run.fail_on
            body = f'<failure message="{escape(f.title)}">{escape(f.detail)}\n{escape(chr(10).join(f.repro))}</failure>' if failed else ""
            cases.append(f'<testcase classname="explore.{escape(f.category)}" name="{escape(f.title)} [{escape(f.screen_name)}]">{body}</testcase>')
        cases.append(f'<testcase classname="explore" name="exploration completed: {result.graph.stats()["screens"]} screens, {result.graph.coverage_pct()}% elements"/>')
    for r in replay_results or []:
        body = ""
        if r.result in ("fail", "error", "still_failing"):
            body = f'<failure message="{escape(r.result)}">{escape(r.message)}</failure>'
        cases.append(f'<testcase classname="regression.{escape(r.case.kind)}" name="{escape(r.case.name)}" time="{r.duration_s}">{body}</testcase>')
    failures = sum("<failure" in c for c in cases)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="mobileqa {escape(cfg.app.name)}" tests="{len(cases)}" failures="{failures}">\n'
           + "\n".join(cases) + "\n</testsuite>\n")
    path.write_text(xml, encoding="utf-8")   # declared UTF-8; don't let a cp1252 default crash the write
