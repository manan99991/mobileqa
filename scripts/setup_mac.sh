#!/usr/bin/env bash
# One-time setup on a Mac (needed for iOS; also runs Android). Safe to re-run.
set -euo pipefail

echo "== Homebrew"
command -v brew >/dev/null || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

echo "== Node, Python, Java, Android tools, iOS helpers"
brew install node python@3.12 openjdk@17 android-platform-tools carthage libimobiledevice ios-deploy 2>/dev/null || true
brew install --cask android-commandlinetools 2>/dev/null || true

echo "== Xcode command line tools (iOS)"
xcode-select -p >/dev/null 2>&1 || xcode-select --install || true

echo "== Appium + drivers"
npm install -g appium
appium driver install xcuitest || appium driver update xcuitest
appium driver install uiautomator2 || appium driver update uiautomator2

echo "== mobileqa"
pip3 install --upgrade pip
pip3 install -e "$(dirname "$0")/.."

echo "== Android emulator (Pixel 6, API 34) — skip if you only test iOS"
if command -v sdkmanager >/dev/null; then
  yes | sdkmanager --licenses >/dev/null || true
  sdkmanager "platform-tools" "emulator" "platforms;android-34" "system-images;android-34;google_apis;arm64-v8a" || true
  echo no | avdmanager create avd -n Pixel_6_API_34 -k "system-images;android-34;google_apis;arm64-v8a" -d pixel_6 2>/dev/null || true
fi

cat <<'EOF'

Done. Next:
  export ANTHROPIC_API_KEY=sk-ant-...        # the tester's brain
  appium --relaxed-security &                # leave running
  open -a Simulator                          # iOS, or:  emulator -avd Pixel_6_API_34 &
  mobileqa doctor apps/reckato/app.ios.yaml
  RECKATO_APP=/path/to/Reckato.app mobileqa explore apps/reckato/app.ios.yaml
EOF
