"""Unit + end-to-end tests against the simulated app. No Appium, no network, no API key."""
from pathlib import Path

import pytest

from mobileqa.brain.actions import Action, execute
from mobileqa.brain.data import classify, inputs_for
from mobileqa.brain.planner import HeuristicBrain
from mobileqa.checks.links import LinkChecker
from mobileqa.checks.ui import check_crash_logs, run_ui_checks
from mobileqa.checks.values import ValueMemory, run_value_checks
from mobileqa.config import AppConfig, Config, DeviceConfig, RunConfig
from mobileqa.driver.base import Element, Rect
from mobileqa.driver.fake_device import FakeDevice
from mobileqa.driver.parse import parse_source
from mobileqa.explorer.explorer import Explorer
from mobileqa.report.build import write_report
from mobileqa.suite.recorder import record_cases
from mobileqa.suite.replayer import Replayer
from mobileqa.suite.store import SuiteStore


def cfg(**kw) -> Config:
    run = RunConfig(max_steps=kw.pop("max_steps", 40), check_vision=False, check_links=False, settle_s=0.0, launch_wait_s=0.0)
    return Config(AppConfig(name="Demo Shop", login={"email": "qa@example.com", "password": "secret1"}),
                  DeviceConfig(platform="android"), run)


# ---- parsing -----------------------------------------------------------------
IOS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Demo" x="0" y="0" width="390" height="844" enabled="true" visible="true">
  <XCUIElementTypeButton type="XCUIElementTypeButton" name="login_btn" label="Sign in" x="20" y="300" width="350" height="50" enabled="true" visible="true"/>
  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" name="dark" label="Dark mode" value="1" x="300" y="400" width="51" height="31" enabled="true" visible="true"/>
  <XCUIElementTypeSecureTextField type="XCUIElementTypeSecureTextField" name="pw" value="" x="20" y="200" width="350" height="44" enabled="true" visible="true"/>
</XCUIElementTypeApplication>"""


def test_parse_ios():
    els = parse_source(IOS_XML, "ios")
    btn = next(e for e in els if e.resource_id == "login_btn")
    assert btn.type == "button" and btn.text == "Sign in" and btn.rect == Rect(20, 300, 350, 50) and btn.interactive
    sw = next(e for e in els if e.resource_id == "dark")
    assert sw.type == "switch" and sw.checked is True
    pw = next(e for e in els if e.resource_id == "pw")
    assert pw.type == "input" and pw.password and pw.editable
    assert all(e.xpath.startswith("/XCUIElementTypeApplication") for e in els)


def test_parse_android_fake():
    d = FakeDevice()
    d.launch()
    s = d.snapshot()
    assert s.find(resource_id="login_btn").type == "button"
    assert s.find(resource_id="password").password
    assert s.fingerprint == d.snapshot().fingerprint      # stable


# ---- checks --------------------------------------------------------------------
def test_ui_checks_find_planted_defects():
    d = FakeDevice()
    d.logged_in = True
    d.launch()
    titles = set()
    for screen in ("home", "detail", "cart", "settings"):
        d.screen = screen
        titles |= {f.title for f in run_ui_checks(d.snapshot(), "android", screen, 1)}
    assert "Text truncated with ellipsis" in titles
    assert "Text elements overlap" in titles
    assert "Tap target smaller than platform minimum" in titles
    assert "Placeholder or debug text visible" in titles
    assert "Interactive control has no accessible label" in titles


def test_value_check_total():
    d = FakeDevice()
    d.logged_in = True
    d.launch()
    d.screen = "detail"
    fs = run_value_checks(d.snapshot(), "detail", 1, ValueMemory())
    assert any("Total" in f.title for f in fs)


def test_cross_screen_price_consistency():
    from mobileqa.driver.base import ScreenState
    mem = ValueMemory()
    a = ScreenState([Element("1", "text", "T", text="Blue Jacket", rect=Rect(0, 0, 200, 40)),
                     Element("2", "text", "T", text="$49.99", rect=Rect(220, 0, 100, 40))], "", None, 400, 800)
    b = ScreenState([Element("1", "text", "T", text="Blue Jacket", rect=Rect(0, 0, 200, 40)),
                     Element("2", "text", "T", text="$59.99", rect=Rect(220, 0, 100, 40))], "", None, 400, 800)
    assert not mem.observe(a, "list", 1)
    fs = mem.observe(b, "detail", 2)
    assert fs and "different price" in fs[0].title


def test_crash_log_detection():
    fs = check_crash_logs(["E AndroidRuntime: FATAL EXCEPTION: main java.lang.NullPointerException"], "android", "fp", "Home", 1)
    assert len(fs) == 1 and fs[0].severity == "critical"
    fs = check_crash_logs(["*** Terminating app due to uncaught exception 'NSRangeException'"], "ios", "fp", "Home", 1)
    assert fs and fs[0].category == "crash"


def test_crash_log_ignores_emulator_noise():
    """The CI emulator's own codec NPE and the launcher/SystemUI ANR must never become findings,
    but a real app crash in the same batch still must."""
    noise = [
        "E Delight5Decoder: java.lang.NullPointerException",            # emulator video codec spam
        "E ActivityManager: ANR in com.android.systemui",              # System UI ANR while settling
        "W ActivityManager: ANR in com.google.android.apps.nexuslauncher (reason: ...)",  # Pixel Launcher ANR
        "E AndroidRuntime: FATAL EXCEPTION in com.android.systemui",   # system crash, not the app
    ]
    assert check_crash_logs(noise, "android", "fp", "Home", 1) == []
    mixed = noise + ["E AndroidRuntime: FATAL EXCEPTION: main java.lang.IllegalStateException at com.demo.CheckoutActivity"]
    fs = check_crash_logs(mixed, "android", "fp", "Home", 1)
    assert len(fs) == 1 and fs[0].severity == "critical"


def test_link_extraction_and_classification():
    lc = LinkChecker()
    urls = lc.extract(["see https://example.com/a. and www.foo.org/x?y=1)", "nothing"])
    assert urls == ["https://example.com/a", "https://www.foo.org/x?y=1"]
    lc.seen_on = {"https://x.test/404": {"Home"}, "https://y.test/ok": {"Home"}, "http://z.test": {"Home"}}
    lc.results = {"https://x.test/404": (404, "", 0.1), "https://y.test/ok": (200, "", 0.1), "http://z.test": (200, "", 0.1)}
    fs = lc.findings_for(list(lc.results))
    assert {f.title for f in fs} == {"Dead link (404)", "Insecure http:// link"}


# ---- test data --------------------------------------------------------------------
def test_data_generator():
    e = Element("1", "input", "EditText", label="Email address", rect=Rect(0, 0, 100, 40), editable=True)
    assert classify(e) == "email"
    kinds = {i.kind for i in inputs_for(e)}
    assert {"valid", "empty", "invalid", "boundary", "unicode", "hostile"} <= kinds
    assert inputs_for(e, {"email": "me@x.com"})[0].value == "me@x.com"


# ---- gestures reach the device ----------------------------------------------------
def test_every_gesture_executes():
    d = FakeDevice()
    d.logged_in = True
    d.launch()
    d.screen = "detail"
    s = d.snapshot()
    hero = s.find(resource_id="hero")
    for kind in ("tap", "double_tap", "triple_tap", "long_press", "two_finger_tap", "swipe_up", "swipe_down", "swipe_left",
                 "swipe_right", "pinch_in", "pinch_out", "drag", "rotate", "background", "shake", "notifications", "offline"):
        execute(d, Action(kind, hero.id), d.snapshot(), settle_s=0)
    assert any(a.startswith("tap x3") for a in d.action_log)
    assert any(a.startswith("pinch 2.0") for a in d.action_log) and any(a.startswith("pinch 0.5") for a in d.action_log)
    assert "two_finger_tap" in d.action_log and "background" in d.action_log


# ---- the whole loop -----------------------------------------------------------------
def test_explore_logs_in_finds_crash_and_grows_suite(tmp_path: Path):
    c = cfg(max_steps=45)
    dev = FakeDevice()
    r = Explorer(c, dev, HeuristicBrain(c), tmp_path / "run").run()
    assert dev.logged_in, "agent should log in with the provided credentials"
    titles = {f.title for f in r.findings.by_severity()}
    assert "Uncaught exception in logcat" in titles
    assert r.findings.count("critical") >= 1
    assert r.graph.stats()["screens"] >= 4
    assert (tmp_path / "run" / "findings.json").exists()

    cases = record_cases(r, "t", "Demo Shop")
    store = SuiteStore(tmp_path / "suite")
    added = store.add_new(cases)
    kinds = {x.kind for x in added}
    assert {"smoke", "reach", "bug"} <= kinds
    assert store.add_new(record_cases(r, "t2", "Demo Shop")) == []      # idempotent

    # replay on the same (broken) build: bugs still fail, navigation passes
    rp = Replayer(c, FakeDevice(), HeuristicBrain(c), store, tmp_path / "replay1")
    res = rp.run_all()
    by_kind = {}
    for x in res:
        by_kind.setdefault(x.case.kind, set()).add(x.result)
    assert by_kind["reach"] <= {"pass", "healed"}
    assert "still_failing" in by_kind["bug"]

    # replay on a fixed build: the crash case flips to fixed and becomes a regression guard
    rp2 = Replayer(c, FakeDevice(crash_on_checkout=False), HeuristicBrain(c), store, tmp_path / "replay2")
    res2 = rp2.run_all()
    crash_case = next(x for x in res2 if "exception" in x.case.id)
    assert crash_case.result == "fixed"
    assert next(x for x in store.load_all() if x.id == crash_case.case.id).status == "active"

    out = write_report(c, tmp_path / "run", "t", "test", r, res2, store.stats(), len(added))
    html = out.read_text()
    assert "RELEASE BLOCKED" in html and "Uncaught exception" in html
    assert (tmp_path / "run" / "junit.xml").read_text().count("<failure") >= 1


def test_self_healing_locator(tmp_path: Path):
    """Rename a button between runs; the replayer should still find it by fuzzy name."""
    c = cfg(max_steps=12)
    r = Explorer(c, FakeDevice(), HeuristicBrain(c), tmp_path / "run").run()
    store = SuiteStore(tmp_path / "suite")
    store.add_new(record_cases(r, "t", "Demo Shop"))
    case = next(x for x in store.load_all() if x.id == "reach-demo-shop")
    tap = next(s for s in case.steps if s.action["kind"] == "tap")
    tap.action["locator"] = [["accessibility id", "login_button_v2"]]       # stale id
    tap.action["element_name"] = "Sign In"                                   # case differs
    store.save(case)
    res = Replayer(c, FakeDevice(), HeuristicBrain(c), store, tmp_path / "rp").run_case(case)
    assert res.result == "healed" and res.healed_steps


def test_app_error_screen_reported_once_not_as_noise():
    """The app's own crash screen is ONE crash finding — its raw error text must not also be
    flagged as placeholder/unformatted-value/tiny-tap noise."""
    from mobileqa.driver.base import ScreenState
    from mobileqa.checks.ui import looks_like_app_error, run_ui_checks
    els = [
        Element("1", "text", "TextView", text="Something went wrong", rect=Rect(0, 0, 400, 40)),
        Element("2", "text", "TextView",
                text="TypeError: Cannot read property 'ErrorBoundary' of undefined", rect=Rect(0, 50, 1000, 90)),
        Element("3", "button", "Button", text="Try again", rect=Rect(0, 100, 60, 130), clickable=True),  # tiny
    ]
    st = ScreenState(els, "", None, 1080, 2000)
    assert looks_like_app_error(st.texts)
    fs = run_ui_checks(st, "android", "Crash", 1)
    assert len(fs) == 1 and fs[0].severity == "critical" and fs[0].category == "crash"
    assert not any(k in f.title for f in fs for k in ("Placeholder", "Tap target", "truncated"))
    # a normal screen with the word "undefined" buried in prose is NOT an app error
    assert not looks_like_app_error(["Your balance is undefined until payday"])


def test_heuristic_brain_recovers_from_error_screen():
    """On the error screen the brain taps the recovery action instead of flailing on it."""
    from mobileqa.brain.planner import StepContext
    from mobileqa.driver.base import ScreenState
    c = cfg()
    els = [
        Element("1", "text", "TextView", text="Something went wrong", rect=Rect(0, 0, 400, 40)),
        Element("2", "text", "TextView", text="TypeError: Cannot read property 'x' of undefined", rect=Rect(0, 50, 1000, 90)),
        Element("3", "button", "Button", text="Try again", rect=Rect(0, 110, 300, 170), clickable=True),
    ]
    st = ScreenState(els, "", None, 1080, 2000)
    ctx = StepContext(cfg=c, step=1, state=st, prev_state=None, prev_action=None, prev_screen_name="",
                      transition="new screen", coverage={}, visited=[], frontier=[], history=[])
    res = HeuristicBrain(c).step(ctx)
    assert res.action.kind == "tap" and "again" in res.action.element_name.lower()


def test_route_seeding_reaches_deep_screens(tmp_path: Path):
    """With routes configured, the explorer deep-links to each one so a tap-only brain still
    reaches deep screens it would never navigate to — and actually checks them once there."""
    c = cfg(max_steps=30)
    c.app.routes = ["settings", "detail", "cart"]
    dev = FakeDevice()
    r = Explorer(c, dev, HeuristicBrain(c), tmp_path / "run").run()
    seeded = [a for a in dev.action_log if a.startswith("route ")]
    assert any("route settings" in a for a in seeded)
    assert any("route detail" in a for a in seeded)
    assert any("route cart" in a for a in seeded)
    # proof it didn't merely open the route but explored & ran checks there:
    titles = {f.title for f in r.findings.by_severity()}
    assert "Placeholder or debug text visible" in titles   # the settings screen's "Lorem ipsum placeholder"


def test_route_sharding_offset_and_limit(tmp_path: Path, monkeypatch):
    """A big route list is sharded across runs: OFFSET rotates the start, LIMIT caps this run."""
    c = cfg(max_steps=5)
    c.app.routes = [f"r{i}" for i in range(10)]            # r0..r9
    monkeypatch.setenv("MOBILEQA_ROUTE_OFFSET", "3")
    monkeypatch.setenv("MOBILEQA_ROUTE_LIMIT", "4")
    ex = Explorer(c, FakeDevice(), HeuristicBrain(c), tmp_path / "run")
    assert ex._routes == ["r3", "r4", "r5", "r6"]          # rotated by 3, first 4
    # offset wraps past the end
    monkeypatch.setenv("MOBILEQA_ROUTE_OFFSET", "9")
    monkeypatch.delenv("MOBILEQA_ROUTE_LIMIT")
    ex2 = Explorer(c, FakeDevice(), HeuristicBrain(c), tmp_path / "run2")
    assert ex2._routes[0] == "r9" and ex2._routes[1] == "r0" and len(ex2._routes) == 10


def test_no_routes_means_normal_launch(tmp_path: Path):
    """Backward-compat: with no routes, launch_route is never called and behaviour is unchanged."""
    c = cfg(max_steps=20)
    dev = FakeDevice()
    Explorer(c, dev, HeuristicBrain(c), tmp_path / "run").run()
    assert not any(a.startswith("route ") for a in dev.action_log)


def test_forbidden_actions_are_never_taken():
    c = cfg(max_steps=60)
    c.app.forbidden_texts = ["Log out", "Checkout"]
    dev = FakeDevice()
    Explorer(c, dev, HeuristicBrain(c), Path("/tmp/mobileqa_forbidden")).run()
    assert dev.logged_in and not dev.crashed, "Checkout (forbidden) must never have been tapped"


class _ScriptedLLM:
    """Stands in for Claude: always answers with a tap on the first interactive element + one finding."""
    available = True
    calls = 0

    def json(self, system, user, images=None, max_tokens=0):
        self.calls += 1
        assert "SCREEN NOW" in user and "COVERAGE" in user
        import re as _re
        ids = _re.findall(r"^\[([\d.]+)\] (?:button|input)", user.split("SCREEN NOW")[1], _re.M)
        return {"verdict": {"ok": True, "note": "fine"}, "screen_name": "Scripted screen",
                "findings": [{"category": "ux", "severity": "low", "title": "Scripted finding", "detail": "d", "element": "x"}],
                "action": {"kind": "tap", "element_id": ids[0] if ids else None, "reason": "r", "expectation": "e"}}


def test_claude_brain_parses_model_output(tmp_path: Path):
    from mobileqa.brain.planner import ClaudeBrain
    c = cfg(max_steps=5)
    llm = _ScriptedLLM()
    brain = ClaudeBrain(c, llm=llm, vision=llm)
    r = Explorer(c, FakeDevice(), brain, tmp_path / "run").run()
    assert llm.calls >= 5
    assert any(f.title == "Scripted finding" and f.source == "model" for f in r.findings.by_severity())
    assert any(n.name == "Scripted screen" for n in r.graph.nodes.values())


def test_llm_json_parser():
    from mobileqa.brain.llm import parse_json
    assert parse_json('Sure! ```json\n{"a": 1}\n```')["a"] == 1
    assert parse_json('text {"b": [1,2]} trailing')["b"] == [1, 2]
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_openai_text_handles_str_object_and_dict():
    """Response shape varies by SDK/endpoint; all three must yield the content text."""
    from mobileqa.brain.llm import _openai_text

    # openai 3.x against GitHub Models returns the content as a plain string
    assert _openai_text('{"action":"tap"}') == '{"action":"tap"}'
    # dict-shaped response
    assert _openai_text({"choices": [{"message": {"content": "hello"}}]}) == "hello"

    # ChatCompletion-object-shaped response
    class _N:
        pass
    msg = _N(); msg.content = "world"
    choice = _N(); choice.message = msg
    obj = _N(); obj.choices = [choice]
    assert _openai_text(obj) == "world"


def test_llm_openai_path_accepts_bare_string_return():
    """Regression: a bare-string completion (openai 3.x + GitHub Models) must parse, not crash the
    brain to heuristic with "'str' object has no attribute 'choices'"."""
    from mobileqa.brain.llm import LLM

    llm = LLM.__new__(LLM)   # bypass __init__ — no network, no keys
    llm.model, llm.max_retries = "openai/gpt-4o-mini", 1
    llm.calls = llm.in_tokens = llm.out_tokens = 0
    llm._kind = "openai"

    class _Completions:
        def create(self, **kw):
            return '{"thought":"ok","action":{"kind":"back"}}'   # a string, like GitHub Models returns

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    llm._client = _Client()
    out = llm.json("system", "user")
    assert out["action"]["kind"] == "back"


def test_openai_no_vision_drops_images_and_sends_plain_string(monkeypatch):
    """With MOBILEQA_NO_VISION set (text-only providers like Groq gpt-oss), screenshots must be
    dropped and the user content sent as a plain string, so a text model never errors on image parts."""
    from mobileqa.brain.llm import LLM

    monkeypatch.setenv("MOBILEQA_NO_VISION", "1")
    seen = {}

    class _Completions:
        def create(self, **kw):
            seen["messages"] = kw["messages"]
            return '{"action":{"kind":"back"}}'

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    llm = LLM.__new__(LLM)
    llm.model, llm.max_retries = "openai/gpt-oss-120b", 1
    llm.calls = llm.in_tokens = llm.out_tokens = 0
    llm._kind, llm._client = "openai", _Client()

    out = llm.json("system", "pick an action", images=[b"\x89PNGfake"])
    user_content = seen["messages"][1]["content"]
    assert isinstance(user_content, str)           # not a parts-list
    assert "image_url" not in str(user_content)     # screenshot dropped
    assert out["action"]["kind"] == "back"


def test_openai_reasoning_effort_passed_as_extra_body(monkeypatch):
    """MOBILEQA_REASONING_EFFORT must reach the provider as extra_body={"reasoning_effort": ...} so a
    reasoning model (e.g. Groq gpt-oss) caps its thinking and leaves room for the final JSON. When the
    env var is unset, no extra_body is sent, so non-reasoning providers are untouched."""
    from mobileqa.brain.llm import LLM

    seen = {}

    class _Completions:
        def create(self, **kw):
            seen.clear(); seen.update(kw)
            return '{"action":{"kind":"back"}}'

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    def make():
        llm = LLM.__new__(LLM)
        llm.model, llm.max_retries = "openai/gpt-oss-120b", 1
        llm.calls = llm.in_tokens = llm.out_tokens = 0
        llm._kind, llm._client = "openai", _Client()
        return llm

    monkeypatch.setenv("MOBILEQA_REASONING_EFFORT", "low")
    out = make().json("system", "pick an action")
    assert seen.get("extra_body") == {"reasoning_effort": "low"}   # configured -> passed through
    assert out["action"]["kind"] == "back"

    monkeypatch.delenv("MOBILEQA_REASONING_EFFORT", raising=False)
    make().json("system", "pick an action")
    assert "extra_body" not in seen                                 # unset -> nothing sent
