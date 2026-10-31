# mobileqa — the tester that never gets bored

Point it at an app on iOS or Android and it does what a good manual tester does, all day, every build:

- launches the app on a real phone, simulator or emulator (Appium, real OS-level touches)
- looks at every screen two ways: the accessibility tree and the screenshot (Claude judges the screenshot like a designer)
- taps, double-taps, triple-taps, long-presses, swipes all four ways, flings, pinches in and out, drags sliders, rotates, backgrounds the app, opens notifications, shakes, goes offline mid-action
- fills every form with real data, then with empty / 2,000-character / emoji / invalid / hostile data
- checks UI (overlap, clipped text, tiny tap targets, placeholder copy, missing labels, off-screen controls)
- checks values (totals vs. price × qty, subtotal + tax, the same item showing two prices on two screens, NaN/undefined, bad dates)
- checks every link it sees over HTTP (404, 5xx, unreachable, http://, parked domains)
- catches crashes, ANRs, hangs, slow screens from logcat / syslog and the foreground state
- remembers the app as a graph of screens and keeps pushing into unexplored corners
- turns what it did into a regression suite (YAML files you can commit), replays it on every build, and heals its own locators when the UI changes
- writes an HTML report with screenshots + repro steps, a JUnit file for CI, and exits non-zero to block a release
- can file GitHub issues / post to Slack / email the report by itself

Both Reckato and Tenzy are pre-configured under `apps/`.

## 60-second demo (no phone needed)

```bash
pip install -e .
mobileqa demo
open demo_out/reports/latest/report.html
```

That runs the whole loop against a built-in fake shopping app with planted bugs (a crash on Checkout, a wrong total, a dead link, overlapping text, a 30px share button, lorem ipsum in Settings). You'll see it log in, find all of them, write 8 regression cases, and block the release.

## Real setup (one time)

### Mac (iOS + Android)

1. Open Terminal, `cd` into this folder.
2. Run `./scripts/setup_mac.sh` — installs Node, Appium, drivers, Android tools, and mobileqa. Takes ~10 min.
3. Get an API key at console.anthropic.com → API Keys → Create. Then `export ANTHROPIC_API_KEY=sk-ant-...` (put it in `~/.zshrc` to keep it).
4. In a second Terminal tab: `appium --relaxed-security` — leave it running.
5. Start a device:
   - iOS: open Xcode → Xcode menu → Open Developer Tool → Simulator. Or plug in your iPhone and put its UDID into `device.udid` in the yaml (Finder → click the phone → click the text under its name until the UDID shows).
   - Android: `emulator -avd Pixel_6_API_34 &` or plug in a phone with USB debugging on.
6. `mobileqa doctor apps/reckato/app.ios.yaml` — fix anything it lists.

### Windows (Android only; iOS needs a Mac or a device cloud — see below)

1. Right-click PowerShell → Run as administrator.
2. `Set-ExecutionPolicy Bypass -Scope Process` then `.\scripts\setup_windows.ps1`.
3. Follow the 5 lines it prints at the end.

### Build a test binary of the app

The agent needs an installable debug build (not the store .aab/.ipa).

- Reckato / Tenzy (Expo): `npx eas-cli build --platform android --profile preview --local --output qa-build.apk`
  For iOS simulator, add this profile to `eas.json` once, then `--profile qa-simulator`:
  ```json
  "qa-simulator": { "extends": "preview", "ios": { "simulator": true } }
  ```
- Native Android: `./gradlew assembleDebug` → `app/build/outputs/apk/debug/app-debug.apk`
- Native iOS: `xcodebuild -scheme App -sdk iphonesimulator -configuration Debug -derivedDataPath build` → the `.app` under `build/Build/Products`

Point `app.path` (or the `RECKATO_APK` / `RECKATO_APP` / `TENZY_APK` / `TENZY_APP` env var) at it. If the app is already installed on the device, leave `path` blank and keep `package` / `bundle_id`.

## Running

```bash
mobileqa explore apps/reckato/app.android.yaml      # first run: explore everything, build the suite
mobileqa regress apps/reckato/app.android.yaml      # replay the suite on a new build
mobileqa ci      apps/reckato/app.android.yaml      # regress + explore, exit 1 if blocked  ← use in CI / Fastlane
mobileqa watch   apps/reckato/app.android.yaml --every 6h   # standalone monitor
```

Every run writes `reports/<timestamp>/report.html` (+ `junit.xml`, `findings.json`, `steps.json`, screenshots) and `reports/latest`.
The suite lives in `qa/suite/<platform>/cases/*.yaml` — commit it; it grows every run.

Useful flags: `--steps 150` (quicker), `--no-vision` (skip screenshot review, cheaper), `--fake` (dry run on the simulated app).

## When it runs

| Trigger | How |
|---|---|
| Every push / PR / nightly | copy `ci/mobileqa.yml` into the app repo's `.github/workflows/` and add the secrets listed at the top of it |
| Before every store upload | paste `ci/Fastfile.snippet.rb` into your Fastfile; call `qa_gate(platform: "ios")` before `upload_to_testflight` / `upload_to_play_store` |
| On its own, forever | `mobileqa watch app.yaml --every 6h` on any always-on Mac/PC, or the nightly cron in the workflow |

Release gate: `run.fail_on: [critical, high]` — anything at that severity (crash, dead link, wrong total, overlap, placeholder copy, replay failure) fails the job. Add true positives you've accepted to `known_issues` to downgrade them to info.

## How it thinks

Each step the brain gets: the previous action and what it expected, the screen before and after (as an element list + screenshot), coverage on this screen, the app-wide frontier, business goals from the yaml, and the forbidden list. It judges the last action, names the screen, reports what it sees, and picks the next action that opens the most new ground — finish the flow in progress first, then untouched elements, then untried gestures, then edge-case inputs, then back out to an under-explored screen. Deterministic checks (`mobileqa/checks/`) run on every screen regardless. Without an `ANTHROPIC_API_KEY` it falls back to a systematic coverage-driven explorer — still useful, less insight.

Destructive actions are never taken unless `allow_destructive: true`; the `forbidden_texts` list is enforced in code, not just in the prompt.

## How it learns (the playbook)

The tester compounds: every real defect found and fixed is generalised to its *class* and added once to `mobileqa/brain/playbook.py`. From then on the agent probes **every** tool for that pattern on every run — each entry is injected into both the action brain (what to try + what counts as a bug) and the vision brain (what to look for in the screenshot). So a bug found in one tool becomes a permanent check across all of them, instead of being re-found by luck.

Seeded patterns (from the 2026-10-31 Tenzy review): **dead-control** (a control that looks actionable but does nothing / is disabled with no reason), **blank-output** (a produced result/preview that comes back blank), **result-no-preview** (a result described with stats but never shown), **thin-options** (a converter offering fewer formats than the platform supports). The heuristic (no-API) brain files the dead-control one deterministically too.

To add one when you fix a bug: append a `Pattern` to `playbook.py` (probe, tell, severity, where/when learned); if it's catchable deterministically, add a check in `mobileqa/checks/` as well. `apps/<app>/tool_tests.yaml` holds the per-tool contract (what each tool produces and which invariants bite). `tests/test_playbook.py` keeps the mechanism honest.

## iOS without a Mac

Appium is a wire protocol, so a device cloud works unchanged: set `device.appium_url` to the provider's hub and put their caps in `device.extra_caps` (BrowserStack: `bstack:options` with `userName`/`accessKey`, app uploaded via their REST API; Sauce Labs / LambdaTest similar). `ci/mobileqa.yml` already builds iOS on GitHub's macOS runners, which is the cheapest path.

## Layout

```
mobileqa/
  driver/      Device abstraction, Appium implementation (W3C multi-touch), page-source parser, FakeDevice
  checks/      ui/a11y/crash rules, link checker, value checker
  brain/       Claude planner + vision reviewer + self-heal, heuristic fallback, gesture catalogue, test data
  explorer/    the loop and the screen graph
  suite/       case store (YAML), recorder, self-healing replayer
  report/      HTML + JUnit + notifications
apps/          Reckato + Tenzy configs, both platforms
ci/            GitHub Actions workflow, Fastlane lane
scripts/       setup_mac.sh, setup_windows.ps1
tests/         pytest — runs fully offline against the fake app
```
