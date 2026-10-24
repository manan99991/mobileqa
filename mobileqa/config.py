"""app.yaml loader. One file describes the app under test; everything else is derived."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DeviceConfig:
    platform: str                      # "ios" | "android"
    name: str = ""                     # device name / avd / simulator name
    udid: str = ""                     # real device id (optional)
    os_version: str = ""               # "17.4", "14"
    appium_url: str = "http://127.0.0.1:4723"
    extra_caps: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    name: str
    path: str = ""                     # .apk / .app / .ipa (installs it)
    bundle_id: str = ""                # iOS bundle id (if already installed)
    package: str = ""                  # Android package (if already installed)
    activity: str = ""                 # Android launch activity (optional)
    login: dict[str, str] = field(default_factory=dict)      # username/password/pin...
    test_accounts: list[dict[str, str]] = field(default_factory=list)
    base_urls: list[str] = field(default_factory=list)       # web endpoints the app talks to
    allow_destructive: bool = False    # delete account / purchase / send message
    forbidden_texts: list[str] = field(default_factory=lambda: [
        "Delete account", "Purchase", "Buy now", "Pay", "Send money", "Log out",
    ])
    goals: list[str] = field(default_factory=list)           # extra business flows to verify
    known_issues: list[str] = field(default_factory=list)    # suppress noise


@dataclass
class RunConfig:
    max_steps: int = 400
    max_minutes: int = 45
    step_timeout_s: int = 20
    settle_s: float = 0.6              # pause after each gesture, like a human waiting for the UI
    launch_wait_s: float = 2.0
    screenshot_every_step: bool = True
    gestures: list[str] = field(default_factory=lambda: [
        "tap", "double_tap", "triple_tap", "long_press", "swipe_up", "swipe_down",
        "swipe_left", "swipe_right", "pinch_in", "pinch_out", "drag", "rotate", "back", "type",
    ])
    check_links: bool = True
    check_values: bool = True
    check_a11y: bool = True
    check_vision: bool = True          # ask the model to look at screenshots
    model: str = os.environ.get("MOBILEQA_MODEL", "claude-sonnet-5-5")
    vision_model: str = os.environ.get("MOBILEQA_VISION_MODEL", "claude-sonnet-5-5")
    suite_dir: str = "suite"
    report_dir: str = "reports"
    fail_on: list[str] = field(default_factory=lambda: ["critical", "high"])


@dataclass
class Config:
    app: AppConfig
    device: DeviceConfig
    run: RunConfig = field(default_factory=RunConfig)
    source_path: Path | None = None

    @property
    def platform(self) -> str:
        return self.device.platform.lower()


_ENV_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand_env(obj):
    """Allow ${ENV_VAR} and ${ENV_VAR:-default} anywhere in the yaml so secrets stay out of the file."""
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def load_config(path: str | Path) -> Config:
    path = Path(path)
    raw = _expand_env(yaml.safe_load(path.read_text()) or {})
    app = AppConfig(**raw.get("app", {}))
    device = DeviceConfig(**raw.get("device", {}))
    run = RunConfig(**raw.get("run", {}))
    if device.platform.lower() not in ("ios", "android"):
        raise ValueError("device.platform must be 'ios' or 'android'")
    return Config(app=app, device=device, run=run, source_path=path)
