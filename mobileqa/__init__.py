"""mobileqa — an autonomous, human-like tester for iOS and Android apps.

Point it at an app (see examples/app.yaml), and it will:
  * launch the app on a real device/simulator/emulator through Appium,
  * look at every screen the way a person would (screenshot + accessibility tree),
  * tap, double-tap, triple-tap, long-press, swipe, scroll, pinch, zoom, drag, rotate, type,
  * fill forms with valid, invalid and nasty data,
  * check UI/UX (overlap, truncation, tiny tap targets, missing labels, contrast, placeholder text),
  * check values (totals, dates, currency, cross-screen consistency),
  * check every link/URL it sees for dead pages,
  * detect crashes, ANRs, hangs, and unexpected exits,
  * record every meaningful path as a regression test and replay the suite on each deploy,
  * heal broken locators automatically when the UI changes,
  * write an HTML report + JUnit XML and exit non-zero so CI blocks the release.
"""

__version__ = "0.1.0"
