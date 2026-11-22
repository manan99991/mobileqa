// Get It Listed — iOS engine (XCUITest). Drives any installed simulator app by bundle id:
// cold-start timing, systematic crawl of every reachable screen and safe control, forms
// (empty submit + filled), rotation and background/resume on new screens, accessibility
// checks (unlabeled controls, tap targets under 44 pt), seeded random-tap stress, crash and
// freeze detection with a replayable trail. Writes the same JSON the Android engine writes.
//
// Env (pass to xcodebuild as TEST_RUNNER_<NAME>):
//   GIL_BUNDLE   bundle id to test (required)
//   GIL_OUT      host folder to write results to (required)
//   GIL_MINUTES  crawl budget (default 20)
//   GIL_ARGS     extra launch arguments, space separated
//   GIL_LOGIN_EMAIL / GIL_LOGIN_PASSWORD  optional test account
//   GIL_FORBIDDEN extra comma-separated words never to tap
//   GIL_REPLAY / GIL_REPLAY_OUT  (testReplay) trail JSON in, result JSON out
import XCTest
import Foundation

// MARK: - helpers

private let env = ProcessInfo.processInfo.environment

private func rx(_ p: String) -> NSRegularExpression { try! NSRegularExpression(pattern: p, options: [.caseInsensitive]) }
private func matches(_ r: NSRegularExpression, _ s: String) -> Bool {
    r.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
}

private let FORBIDDEN = rx(#"\b(buy|purchase|subscribe|subscription|pay|payment|checkout|upgrade|go premium|premium|start (my |your )?(free )?trial|delete|erase|remove account|deactivate|close account|log ?out|sign ?out|unsubscribe|place order|donate|call|dial|facetime|report)\b"#)
private let SUBMIT = rx(#"\b(submit|continue|next|sign ?in|log ?in|sign ?up|register|create account|save|send|done|confirm|get started|search|go|run|start|build)\b"#)
private let BACKISH = rx(#"^(back|close|done|cancel|dismiss|xmark|x|close sheet|chevron\.left|chevron\.backward)$"#)
private let SYMBOL_NAME = rx(#"^[a-z0-9]+(\.[a-z0-9]+)+$"#)
private let ALLOW = rx(#"^(allow|ok|continue|allow while using app|allow once|allow full access|allow access to all photos|allow access|keep current selection|always allow|allow all)$"#)
private let EXTRA_FORBIDDEN: [String] = (env["GIL_FORBIDDEN"] ?? "").lowercased().split(separator: ",")
    .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }

private let INTERACTIVE: Set<XCUIElement.ElementType> = [.button, .cell, .link, .switch, .toggle, .textField,
    .secureTextField, .searchField, .textView, .menuItem, .tab, .segmentedControl]
private let EDITABLE: Set<XCUIElement.ElementType> = [.textField, .secureTextField, .searchField, .textView]

struct El {
    let type: XCUIElement.ElementType
    let id: String
    let label: String
    let placeholder: String
    let value: String
    let frame: CGRect
    var editable: Bool { EDITABLE.contains(type) }
    var text: String { !label.isEmpty ? label : (!placeholder.isEmpty ? placeholder : (!id.isEmpty ? id : "\(type)")) }
    var key: String { "\(type.rawValue)|\(id)|\(label.prefix(40))" }
    var center: CGPoint { CGPoint(x: frame.midX, y: frame.midY) }
}

private func typeName(_ t: XCUIElement.ElementType) -> String {
    switch t {
    case .button: return "Button"; case .cell: return "Cell"; case .link: return "Link"; case .switch, .toggle: return "Switch"
    case .textField: return "TextField"; case .secureTextField: return "SecureTextField"; case .searchField: return "SearchField"
    case .textView: return "TextView"; case .tab: return "Tab"; case .menuItem: return "MenuItem"; case .segmentedControl: return "Segments"
    default: return "Element"
    }
}

private func stableHash(_ s: String) -> String {
    var h: UInt64 = 5381
    for b in s.utf8 { h = (h &* 33) ^ UInt64(b) }
    return String(h, radix: 16)
}

private func testValue(_ e: El) -> String {
    let blob = "\(e.placeholder) \(e.label) \(e.id)".lowercased()
    if e.type == .secureTextField || blob.contains("password") || blob.contains("passcode") {
        return env["GIL_LOGIN_PASSWORD"] ?? "TestPass#2026"
    }
    if blob.contains("mail") { return env["GIL_LOGIN_EMAIL"] ?? "qa.tester@example.com" }
    if ["phone", "mobile", "tel"].contains(where: blob.contains) { return "5555550123" }
    if ["otp", "code", "pin", "verification"].contains(where: blob.contains) { return "123456" }
    if ["zip", "postal"].contains(where: blob.contains) { return "10001" }
    if ["url", "website", "link"].contains(where: blob.contains) { return "https://example.com" }
    if ["amount", "price", "qty", "quantity", "age", "number", "count", "minutes", "hours"].contains(where: blob.contains) { return "3" }
    if blob.contains("search") { return "photo" }
    if blob.contains("name") { return "Alex Tester" }
    if ["city", "town"].contains(where: blob.contains) { return "Chicago" }
    if blob.contains("address") { return "100 Main St" }
    if e.type == .textView || ["describe", "what", "note", "message", "ask", "need"].contains(where: blob.contains) {
        return "Remind me to call Alex tomorrow at 3pm and send him the invoice"
    }
    return "Test entry 1"
}

private func isForbidden(_ e: El) -> Bool {
    let blob = "\(e.label) \(e.id)".lowercased()
    if matches(FORBIDDEN, blob) { return true }
    return EXTRA_FORBIDDEN.contains(where: { blob.contains($0) })
}

private func D(_ d: [String: Any]) -> [String: Any] { d }

private func writeJSON(_ obj: Any, _ url: URL) {
    if let d = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]) { try? d.write(to: url) }
}

private struct SeededRNG {  // reproducible stress input
    var state: UInt64
    mutating func next() -> UInt64 { state = state &* 6364136223846793005 &+ 1442695040888963407; return state >> 33 }
    mutating func unit() -> Double { Double(next() % 1_000_000) / 1_000_000.0 }
}

// MARK: - test case

final class GILCrawler: XCTestCase {
    var app: XCUIApplication!
    let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    var out: URL!
    var findings: [[String: Any]] = []
    var screens: [String: [String: Any]] = [:]
    var screenOrder: [String] = []
    var tried: [String: [String: Int]] = [:]
    var history: [[String: Any]] = []
    var path: [String] = []
    var integrations: [[String: Any]] = []
    var a11yDone: Set<String> = []
    var steps = 0
    var curShot = ""
    var curScreen = "Launch"
    var lastEl: El?
    var scale: CGFloat = 3
    var screenFrame = CGRect(x: 0, y: 0, width: 393, height: 852)
    var crashTitles: Set<String> = []

    override func setUp() {
        continueAfterFailure = true
        let bundle = env["GIL_BUNDLE"] ?? ""
        app = XCUIApplication(bundleIdentifier: bundle)
        app.launchArguments = (env["GIL_ARGS"] ?? "").split(separator: " ").map(String.init)
        out = URL(fileURLWithPath: env["GIL_OUT"] ?? NSTemporaryDirectory())
        try? FileManager.default.createDirectory(at: out.appendingPathComponent("screens"), withIntermediateDirectories: true)
    }

    // MARK: snapshot

    func snapshotEls() -> [El]? {
        guard let snap = try? app.snapshot() else { return nil }
        screenFrame = snap.frame.width > 10 ? snap.frame : screenFrame
        var outEls: [El] = []
        func walk(_ s: XCUIElementSnapshot) {
            if s.elementType == .keyboard { return }
            if INTERACTIVE.contains(s.elementType), s.isEnabled, s.frame.width > 2, s.frame.height > 2,
               s.frame.intersects(screenFrame), s.frame.midY > 20, s.frame.midY < screenFrame.maxY - 2 {
                outEls.append(El(type: s.elementType, id: s.identifier, label: s.label,
                                 placeholder: s.placeholderValue ?? "", value: (s.value as? String) ?? "", frame: s.frame))
            }
            for c in s.children { walk(c) }
        }
        walk(snap)
        return outEls
    }

    func navTitle() -> String {
        guard let snap = try? app.snapshot() else { return "" }
        var title = ""
        func walk(_ s: XCUIElementSnapshot) {
            if !title.isEmpty { return }
            if s.elementType == .navigationBar, !s.identifier.isEmpty { title = s.identifier; return }
            for c in s.children { walk(c) }
        }
        walk(snap)
        if title.isEmpty {  // first prominent static text near the top
            var best: (CGFloat, String)? = nil
            func walk2(_ s: XCUIElementSnapshot) {
                if s.elementType == .staticText, !s.label.isEmpty, s.frame.minY < screenFrame.height * 0.3, s.label.count < 40 {
                    if best == nil || s.frame.minY < best!.0 { best = (s.frame.minY, s.label) }
                }
                for c in s.children { walk2(c) }
            }
            walk2(snap)
            title = best?.1 ?? ""
        }
        return title
    }

    func staticTexts() -> [String] {
        guard let snap = try? app.snapshot() else { return [] }
        var out: [String] = []
        func walk(_ s: XCUIElementSnapshot) {
            if s.elementType == .keyboard { return }
            if s.elementType == .staticText, !s.label.isEmpty, out.count < 80 { out.append(s.label) }
            for c in s.children { walk(c) }
        }
        walk(snap)
        return out
    }

    // MARK: remote-control server (lets the shared Python agent drive the app)

    func xmlEscape(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "\"", with: "&quot;")
         .replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
    }

    /// The screen as Android-style hierarchy XML (bounds in screenshot pixels), so one parser serves both platforms.
    func dumpXML() -> String {
        var out = "<?xml version='1.0'?><hierarchy>"
        let bundle = env["GIL_BUNDLE"] ?? ""
        func walk(_ s: XCUIElementSnapshot, _ pkg: String) {
            if s.elementType == .keyboard { return }
            let f = s.frame
            if f.width > 1, f.height > 1 {
                let clickable = INTERACTIVE.contains(s.elementType) && s.isEnabled
                let cls = (s.elementType == .textField || s.elementType == .searchField) ? "EditText" :
                          (s.elementType == .secureTextField ? "EditText" : (s.elementType == .textView ? "EditText" : typeName(s.elementType)))
                let label = s.label.isEmpty ? ((s.value as? String) ?? "") : s.label
                let isText = s.elementType == .staticText
                out += "<node package=\"\(pkg)\" class=\"\(cls)\" resource-id=\"\(xmlEscape(s.identifier))\" "
                out += "text=\"\(xmlEscape(isText || EDITABLE.contains(s.elementType) || !s.identifier.isEmpty ? label : (clickable ? label : "")))\" "
                out += "content-desc=\"\(xmlEscape(clickable ? s.label : ""))\" hint=\"\(xmlEscape(s.placeholderValue ?? ""))\" "
                out += "clickable=\"\(clickable)\" enabled=\"\(s.isEnabled)\" password=\"\(s.elementType == .secureTextField)\" "
                out += "checkable=\"\(s.elementType == .switch || s.elementType == .toggle)\" "
                out += "bounds=\"[\(Int(f.minX * scale)),\(Int(f.minY * scale))][\(Int(f.maxX * scale)),\(Int(f.maxY * scale))]\" />"
            }
            for c in s.children { walk(c, pkg) }
        }
        if let snap = try? app.snapshot() { walk(snap, bundle) }
        if springboard.alerts.count > 0, let sb = try? springboard.alerts.firstMatch.snapshot() { walk(sb, "com.apple.springboard") }
        return out + "</hierarchy>"
    }

    func testServe() {
        guard let dir = env["GIL_CMD_DIR"] else { return }
        let d = URL(fileURLWithPath: dir)
        _ = shot("serve_probe")          // sets scale
        if app.state != .runningForeground { app.activate() }
        var n = 0
        let start = Date()
        while Date().timeIntervalSince(start) < 3 * 3600 {
            let cmdURL = d.appendingPathComponent("cmd_\(n).json")
            guard let data = try? Data(contentsOf: cmdURL),
                  let cmd = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { usleep(150_000); continue }
            var resp: [String: Any] = ["ok": true]
            let op = cmd["op"] as? String ?? ""
            let px = { (k: String) -> CGFloat in CGFloat((cmd[k] as? Double) ?? Double(cmd[k] as? Int ?? 0)) / self.scale }
            switch op {
            case "dump": resp["xml"] = dumpXML()
            case "tap": tapPoint(CGPoint(x: px("x"), y: px("y")))
            case "type":
                if app.keyboards.count > 0 { app.typeText(cmd["text"] as? String ?? "") } else { resp["ok"] = false }
            case "clear":
                if app.keyboards.count > 0 {
                    let del = String(repeating: XCUIKeyboardKey.delete.rawValue, count: (cmd["n"] as? Int) ?? 40)
                    app.typeText(del)
                }
            case "hide_keyboard":
                if app.keyboards.count > 0 {
                    if let d = app.keyboards.firstMatch.buttons.allElementsBoundByIndex.first(where: { ["return", "done", "go", "search", "next", "send"].contains($0.label.lowercased()) }) { d.tap() }
                }
            case "back": resp["did"] = goBack(snapshotEls() ?? [])
            case "home": XCUIDevice.shared.press(.home)
            case "launch": if app.state == .runningForeground { } else { app.activate() }
            case "relaunch":
                app.terminate()
                if let args = cmd["args"] as? [String] { app.launchArguments = args }
                else { app.launchArguments = (env["GIL_ARGS"] ?? "").split(separator: " ").map(String.init) }
                app.launch()
            case "terminate": app.terminate()
            case "rotate": XCUIDevice.shared.orientation = (cmd["landscape"] as? Bool ?? false) ? .landscapeLeft : .portrait
            case "swipe":
                let dir = cmd["dir"] as? String ?? "up"
                if dir == "up" { app.swipeUp() } else if dir == "down" { app.swipeDown() } else if dir == "left" { app.swipeLeft() } else { app.swipeRight() }
            case "screenshot":
                let s = XCUIScreen.main.screenshot(); try? s.pngRepresentation.write(to: URL(fileURLWithPath: cmd["path"] as? String ?? "/tmp/x.png"))
            case "state": resp["state"] = app.state.rawValue
            case "alert": resp["handled"] = handleSystemAlerts()
            case "quit": writeJSON(resp, d.appendingPathComponent("resp_\(n).json")); return
            default: resp["ok"] = false
            }
            resp["state"] = resp["state"] ?? app.state.rawValue
            writeJSON(resp, d.appendingPathComponent("resp_\(n).json"))
            n += 1
        }
    }

    func signature(_ els: [El], _ title: String) -> String {
        let parts = Set(els.filter { $0.type != .cell }.map { "\($0.type.rawValue):\($0.id.isEmpty ? String($0.label.prefix(24)) : $0.id)" })
        return stableHash(title + "|" + parts.sorted().joined(separator: "|"))
    }

    // MARK: actions

    func tapPoint(_ p: CGPoint) {
        app.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0)).withOffset(CGVector(dx: p.x, dy: p.y)).tap()
    }

    func act(_ kind: String, _ human: String, _ extra: [String: Any] = [:]) {
        var st: [String: Any] = ["kind": kind, "human": human]
        for (k, v) in extra { st[k] = v }
        history.append(st)
        path.append(human)
        steps += 1
    }

    func shot(_ name: String) -> String {
        let s = XCUIScreen.main.screenshot()
        scale = s.image.scale > 0 ? s.image.scale : scale
        let url = out.appendingPathComponent("screens/\(name).png")
        try? s.pngRepresentation.write(to: url)
        return url.path
    }

    func loc(_ e: El?) -> [String: Any] {
        guard let e = e else { return [:] }
        let f = e.frame
        return ["label": e.text, "rid": e.id, "cls": typeName(e.type),
                "bounds": [Int(f.minX * scale), Int(f.minY * scale), Int(f.maxX * scale), Int(f.maxY * scale)]]
    }

    func handleSystemAlerts() -> Bool {
        let alerts = springboard.alerts
        guard alerts.count > 0 else { return false }
        let a = alerts.firstMatch
        for b in a.buttons.allElementsBoundByIndex where matches(ALLOW, b.label) {
            b.tap(); sleep(1); return true
        }
        if let b = a.buttons.allElementsBoundByIndex.last { b.tap(); sleep(1); return true }
        return false
    }

    func relaunch() {
        app.launch()
        history.append(["kind": "launch", "human": "Open the app"])
        path = ["Open the app"]
        sleep(2)
    }

    /// Returns true if the app crashed / froze / left after the last action.
    @discardableResult
    func checkFailure(after: String) -> Bool {
        let st = app.state
        if st == .notRunning {
            let title = "App crashes after: \(after)"
            if crashTitles.contains(title) { relaunch(); return true }
            crashTitles.insert(title)
            let after = shot("crash_\(findings.count)")
            findings.append(["severity": "critical", "category": "crash", "title": title,
                             "detail": "The app closed unexpectedly right after this step.",
                             "screen_name": curScreen,
                             "repro": (["Install the app fresh and open it"] as [String]) + Array(path.suffix(15)),
                             "evidence": D(["screenshot": curShot.isEmpty ? after : curShot, "after_screenshot": after,
                                          "t": Date().timeIntervalSince1970, "activity": curScreen,
                                          "elements": lastEl.map { [loc($0)] } ?? []]),
                             "replay": history])
            relaunch()
            return true
        }
        if st == .runningBackground || st == .runningBackgroundSuspended {
            integrations.append(["from": path.last ?? "", "opened": "another app (system or third-party)"])
            app.activate(); sleep(2)
            act("launch", "Return to the app")
            return false
        }
        return false
    }

    func goBack(_ els: [El]) -> String {
        if let snap = try? app.snapshot() {
            var navBtn: CGRect? = nil
            func walk(_ s: XCUIElementSnapshot, inNav: Bool) {
                if navBtn != nil { return }
                let nav = inNav || s.elementType == .navigationBar
                if nav, s.elementType == .button, s.frame.minX < 120, s.frame.width > 2 { navBtn = s.frame; return }
                for c in s.children { walk(c, inNav: nav) }
            }
            walk(snap, inNav: false)
            if let f = navBtn {
                tapPoint(CGPoint(x: f.midX, y: f.midY))
                act("tap", "Tap Back", ["x": f.midX, "y": f.midY, "key": "nav-back"])
                return "Tap Back"
            }
        }
        if let b = els.first(where: { $0.type == .button && matches(BACKISH, $0.label.lowercased()) }) {
            tapPoint(b.center)
            act("tap", "Tap \"\(b.text)\"", ["key": b.key, "x": b.center.x, "y": b.center.y])
            return "Tap \"\(b.text)\""
        }
        app.swipeDown()
        act("swipe", "Swipe down to close", [:])
        return "Swipe down to close"
    }

    // MARK: per-screen checks

    func a11y(_ els: [El], sig: String, name: String, shotPath: String) {
        guard !a11yDone.contains(sig) else { return }
        a11yDone.insert(sig)
        let btns = els.filter { [.button, .link, .switch, .toggle].contains($0.type) }
        let unlabeled = btns.filter { ($0.label.isEmpty && $0.id.isEmpty) || matches(SYMBOL_NAME, $0.label) }
        if !unlabeled.isEmpty {
            let ex = unlabeled.prefix(4).map { $0.label.isEmpty ? "(no label)" : "\u{201C}\($0.label)\u{201D}" }.joined(separator: ", ")
            findings.append(["severity": "medium", "category": "a11y",
                             "title": "\(unlabeled.count) button(s) have no label for screen readers",
                             "detail": "VoiceOver reads these as \(ex) instead of what they do.",
                             "screen_name": name,
                             "repro": ["Open the app", "Go to \(name)", "Turn on VoiceOver and swipe through the controls"],
                             "evidence": D(["screenshot": shotPath, "t": Date().timeIntervalSince1970, "activity": name,
                                          "elements": unlabeled.prefix(12).map { loc($0) }])])
        }
        let small = btns.filter { $0.frame.width < 44 || $0.frame.height < 44 }
        if !small.isEmpty {
            var elsOut: [[String: Any]] = []
            for e in small.prefix(12) { var l = loc(e); l["size_dp"] = "\(Int(e.frame.width))x\(Int(e.frame.height)) pt"; elsOut.append(l) }
            let e0 = small[0]
            findings.append(["severity": "low", "category": "a11y",
                             "title": "\(small.count) tap target(s) smaller than 44 pt",
                             "detail": "e.g. \u{2018}\(e0.text)\u{2019} is \(Int(e0.frame.width))x\(Int(e0.frame.height)) pt. Apple\u{2019}s guidelines ask for at least 44x44 pt.",
                             "screen_name": name,
                             "repro": ["Open the app", "Go to \(name)", "Look at \u{2018}\(e0.text)\u{2019} (boxed in the screenshot)"],
                             "evidence": D(["screenshot": shotPath, "t": Date().timeIntervalSince1970, "activity": name, "elements": elsOut])])
        }
    }

    func edgeTests(_ name: String) {
        guard screens.count <= 15 else { return }
        XCUIDevice.shared.orientation = .landscapeLeft; act("rotate", "Rotate the phone to landscape", ["landscape": true]); sleep(1)
        checkFailure(after: "rotating to landscape")
        XCUIDevice.shared.orientation = .portrait; act("rotate", "Rotate back to portrait", ["landscape": false]); sleep(1)
        checkFailure(after: "rotating back to portrait")
        if screens.count <= 8 {
            XCUIDevice.shared.press(.home); act("home", "Go to the Home Screen (app in background)"); sleep(2)
            app.activate(); act("launch", "Reopen the app"); sleep(2)
            checkFailure(after: "going to the background and reopening")
        }
    }

    func candidates(_ sig: String, _ els: [El]) -> [El] {
        let tr = tried[sig] ?? [:]
        let fields = els.filter { $0.editable }
        let fieldsDone = fields.allSatisfy { (tr[$0.key] ?? 0) > 0 }
        var out: [(Int, El)] = []
        for e in els where !isForbidden(e) {
            let n = tr[e.key] ?? 0
            let submit = !e.editable && matches(SUBMIT, e.text)
            if submit && !fields.isEmpty {
                if n == 0 || (n == 1 && fieldsDone) { out.append((n == 0 ? 0 : 2, e)) }
            } else if n == 0 {
                out.append((e.editable ? 1 : 3, e))
            }
        }
        return out.sorted { ($0.0, $0.1.frame.minY, $0.1.frame.minX) < ($1.0, $1.1.frame.minY, $1.1.frame.minX) }.map { $0.1 }
    }

    // MARK: tests

    func testExplore() {
        let minutes = Double(env["GIL_MINUTES"] ?? "20") ?? 20
        let started = Date()
        var perf: [String: Any] = [:]

        // 1. cold start (approximate: includes the test framework's launch overhead)
        var cold: [Int] = []
        for _ in 0..<3 {
            app.terminate()
            let t0 = Date(); app.launch()
            _ = app.wait(for: .runningForeground, timeout: 60)
            cold.append(Int(Date().timeIntervalSince(t0) * 1000))
        }
        perf["cold_start_ms"] = cold
        // XCUITest's launch() waits for the app to go idle, so looping animations inflate this number.
        // It is reported as an approximate figure only, never as a bug (no false findings).
        let med = cold.sorted()[1]
        if false && med > 5000 {
            findings.append(["severity": "medium", "category": "perf",
                             "title": String(format: "Slow cold start: %.1f s to first screen", Double(med) / 1000),
                             "detail": "Measured " + cold.map { String(format: "%.1fs", Double($0) / 1000) }.joined(separator: ", ")
                                + " across 3 cold starts on the simulator (includes test-runner overhead; real phones vary).",
                             "screen_name": "Launch",
                             "repro": ["Force-quit the app", "Open it from the Home Screen", "Time until the first screen"],
                             "evidence": D(["t": Date().timeIntervalSince1970])])
        }
        history = []
        relaunch()
        if app.state != .runningForeground { writeJSON(["foregrounded": false], out.appendingPathComponent("ios_state.json")) }

        // 2. crawl
        var backStreak = 0
        var slowSnaps = 0
        while Date().timeIntervalSince(started) < minutes * 60 {
            if handleSystemAlerts() { continue }
            if checkFailure(after: path.last ?? "opening the app") { continue }
            let t0 = Date()
            guard let els = snapshotEls() else { sleep(1); continue }
            if Date().timeIntervalSince(t0) > 8 {
                slowSnaps += 1
                if slowSnaps == 2 {
                    findings.append(["severity": "high", "category": "crash",
                                     "title": "App froze (not responding) after: \(path.last ?? "opening the app")",
                                     "detail": "The app took more than 8 seconds to respond twice in a row \u{2014} the main thread looks blocked.",
                                     "screen_name": curScreen, "repro": (["Install the app fresh and open it"] as [String]) + Array(path.suffix(15)),
                                     "evidence": D(["screenshot": shot("freeze_\(findings.count)"), "t": Date().timeIntervalSince1970,
                                                  "activity": curScreen, "elements": lastEl.map { [loc($0)] } ?? []]),
                                     "replay": history])
                }
            }
            let title = navTitle()
            let sig = signature(els, title)
            if screens[sig] == nil {
                let name = title.isEmpty ? "Screen \(screens.count + 1)" : title
                let p = shot("s\(String(format: "%02d", screens.count))")
                let lastLaunch = history.lastIndex(where: { ($0["kind"] as? String) == "launch" }) ?? -1
                let reach = Array(history[(lastLaunch + 1)...]).filter { ["tap", "type", "swipe"].contains($0["kind"] as? String ?? "") }
                let uiList: [[String: Any]] = els.prefix(60).map { (e: El) -> [String: Any] in
                    var l = loc(e); l["editable"] = e.editable; return l }
                screens[sig] = ["name": name, "activity": name, "shot": p, "controls": els.count, "t": Date().timeIntervalSince1970,
                                "ui": uiList, "texts": staticTexts(), "reach": reach, "scale": Double(scale)]
                screenOrder.append(sig)
                curScreen = name; curShot = p
                a11y(els, sig: sig, name: name, shotPath: p)
                edgeTests(name)
                continue
            }
            curScreen = screens[sig]?["name"] as? String ?? curScreen
            curShot = screens[sig]?["shot"] as? String ?? curShot
            let cands = candidates(sig, els)
            guard let e = cands.first else {
                backStreak += 1
                if backStreak > 6 { relaunch(); backStreak = 0; continue }
                let h = goBack(els); sleep(1)
                checkFailure(after: h)
                continue
            }
            backStreak = 0
            var tr = tried[sig] ?? [:]; tr[e.key] = (tr[e.key] ?? 0) + 1; tried[sig] = tr
            lastEl = e
            if e.editable {
                let v = testValue(e)
                tapPoint(e.center); sleep(1)
                if app.keyboards.count > 0 {
                    app.typeText(v)
                    let kb = app.keyboards.firstMatch
                    let done = kb.buttons.allElementsBoundByIndex.first { ["return", "done", "go", "search", "next", "send"].contains($0.label.lowercased()) }
                    if let d = done { d.tap() }
                }
                act("type", "Type \"\(e.type == .secureTextField ? "\u{2022}\u{2022}\u{2022}\u{2022}\u{2022}\u{2022}" : v)\" into \"\(e.text)\"",
                    ["key": e.key, "x": e.center.x, "y": e.center.y, "value": v, "el": loc(e)])
            } else {
                tapPoint(e.center)
                act("tap", "Tap \"\(e.text)\"", ["key": e.key, "x": e.center.x, "y": e.center.y, "el": loc(e)])
            }
            sleep(1)
            checkFailure(after: path.last ?? "")
        }

        // 3. seeded random-tap stress (replayable by seed)
        var stress: [[String: Any]] = []
        for seed in [4821, 1337] {
            relaunch()
            var rng = SeededRNG(state: UInt64(seed))
            var crashed = false
            var n = 0
            for i in 0..<250 {
                let p = CGPoint(x: screenFrame.width * (0.05 + 0.9 * rng.unit()), y: screenFrame.height * (0.08 + 0.84 * rng.unit()))
                // a tap can legitimately open another app (Settings, Safari, Mail): come back before the next one,
                // or the test framework waits on a backgrounded app until it times out
                if app.state == .notRunning { crashed = true; break }
                if app.state != .runningForeground { app.activate(); sleep(1); continue }
                tapPoint(p); n = i + 1
                if i % 10 == 9 { _ = handleSystemAlerts() }
            }
            stress.append(["seed": seed, "events": n, "crash": crashed, "anr": false])
            if crashed {
                findings.append(["severity": "critical", "category": "crash",
                                 "title": "Crash under rapid random input (stress seed \(seed))",
                                 "detail": "After about \(n) fast random taps the app crashed. Fast tappers and double-taps reach the same code path.",
                                 "screen_name": "Stress test",
                                 "repro": ["Install the app fresh", "Run the Get It Listed stress replay with seed \(seed) for \(n) taps",
                                           "The same seed replays the exact same taps"],
                                 "evidence": D(["screenshot": shot("stress_\(seed)"), "t": Date().timeIntervalSince1970]),
                                 "replay": [D(["kind": "stress", "seed": seed, "events": n + 10])]])
            }
        }
        perf["stress"] = stress

        let screenList: [[String: Any]] = screenOrder.compactMap { screens[$0] }
        let summary: [String: Any] = ["summary": D([
            "screens": screens.count, "steps": steps, "duration_s": Int(Date().timeIntervalSince(started)),
            "performance": perf, "integrations": Array(integrations.prefix(40)), "screen_list": screenList,
            "started_at": started.timeIntervalSince1970, "pids": [String](), "density": Double(scale)])]
        writeJSON(findings, out.appendingPathComponent("findings.json"))
        writeJSON(summary, out.appendingPathComponent("summary.json"))
        print("GIL crawl done: \(screens.count) screens, \(steps) actions, \(findings.count) raw findings")
    }

    /// Replays one trail from a fresh install (host reinstalls the app first) and reports whether it crashed.
    func testReplay() {
        guard let inPath = env["GIL_REPLAY"], let outPath = env["GIL_REPLAY_OUT"],
              let data = try? Data(contentsOf: URL(fileURLWithPath: inPath)),
              let trail = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
        app.launch(); sleep(2)
        var crashed = false
        stepLoop: for (i, st) in trail.enumerated() {
            _ = handleSystemAlerts()
            let kind = st["kind"] as? String ?? ""
            switch kind {
            case "launch": if app.state != .runningForeground { app.activate() }; sleep(2)
            case "home": XCUIDevice.shared.press(.home); sleep(2)
            case "rotate": XCUIDevice.shared.orientation = (st["landscape"] as? Bool ?? false) ? .landscapeLeft : .portrait; sleep(1)
            case "swipe": app.swipeDown(); sleep(1)
            case "stress":
                var rng = SeededRNG(state: UInt64(st["seed"] as? Int ?? 1))
                _ = snapshotEls()
                for j in 0..<(st["events"] as? Int ?? 250) {
                    let p = CGPoint(x: screenFrame.width * (0.05 + 0.9 * rng.unit()), y: screenFrame.height * (0.08 + 0.84 * rng.unit()))
                    if app.state == .notRunning { crashed = true; break stepLoop }
                    if app.state != .runningForeground { app.activate(); sleep(1); continue }   // same rule as the crawl
                    tapPoint(p)
                    if j % 10 == 9 { _ = handleSystemAlerts() }
                }
            case "tap", "type":
                var p = CGPoint(x: st["x"] as? Double ?? 0, y: st["y"] as? Double ?? 0)
                if let key = st["key"] as? String, let els = snapshotEls(), let e = els.first(where: { $0.key == key }) { p = e.center }
                tapPoint(p); sleep(1)
                if kind == "type", app.keyboards.count > 0 {
                    app.typeText(st["value"] as? String ?? "")
                    if let d = app.keyboards.firstMatch.buttons.allElementsBoundByIndex.first(where: { ["return", "done", "go", "search", "next", "send"].contains($0.label.lowercased()) }) { d.tap() }
                }
            default: break
            }
            sleep(1)
            if app.state == .notRunning { crashed = true; writeJSON(D(["crashed": true, "step": i]), URL(fileURLWithPath: outPath)); return }
        }
        writeJSON(["crashed": crashed], URL(fileURLWithPath: outPath))
    }
}
