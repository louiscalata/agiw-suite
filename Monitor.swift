import Cocoa
import WebKit
import Darwin

private let bundleIdentifier = "local.codemode.inference-monitor"
private let appName = "Inference Monitor"

// BEGIN MONITOR INSTANCE POLICY
struct MonitorInstanceCandidate: Equatable, Sendable {
    let bundleIdentifier: String
    let processIdentifier: Int32
    let launchDate: Date?
    let isTerminated: Bool
}

enum MonitorInstancePhase { case startup, running }

enum MonitorInstancePolicy {
    static let bundleIdentifiers: Set<String> = [
        "local.codemode.inference-monitor",
        "com.louiscalata.agiw.inference-monitor.mas"
    ]

    static func shouldExit(current: MonitorInstanceCandidate,
                           others: [MonitorInstanceCandidate],
                           phase: MonitorInstancePhase) -> Bool {
        guard bundleIdentifiers.contains(current.bundleIdentifier), !current.isTerminated else { return false }
        let peers = others.filter {
            bundleIdentifiers.contains($0.bundleIdentifier) && !$0.isTerminated &&
                $0.processIdentifier != current.processIdentifier
        }
        let candidates = [current] + peers
        // A fixed PID ordering cannot change when another candidate's date is
        // missing or that candidate departs. It makes no chronology claim.
        switch phase {
        case .startup, .running:
            let winner = candidates.min { $0.processIdentifier < $1.processIdentifier }
            return winner?.processIdentifier != current.processIdentifier
        }
    }
}
// END MONITOR INSTANCE POLICY

/// Coordinates only the two Monitor apps; it never discovers or signals worker processes.
final class MonitorInstanceCoordinator {
    private var cancelObservation: (() -> Void)?
    private let current: () -> MonitorInstanceCandidate
    private let snapshot: () -> [MonitorInstanceCandidate]
    private let subscribe: (@escaping () -> Void) -> (() -> Void)
    private let requestSelfTermination: () -> Void
    private var stopping = false

    init(current: @escaping () -> MonitorInstanceCandidate,
         snapshot: @escaping () -> [MonitorInstanceCandidate],
         subscribe: @escaping (@escaping () -> Void) -> (() -> Void),
         requestSelfTermination: @escaping () -> Void) {
        self.current = current
        self.snapshot = snapshot
        self.subscribe = subscribe
        self.requestSelfTermination = requestSelfTermination
    }

    convenience init(requestSelfTermination: @escaping () -> Void) {
        func candidate(_ app: NSRunningApplication) -> MonitorInstanceCandidate {
            MonitorInstanceCandidate(bundleIdentifier: app.bundleIdentifier ?? "",
                                     processIdentifier: app.processIdentifier,
                                     launchDate: app.launchDate, isTerminated: app.isTerminated)
        }
        self.init(current: { candidate(.current) },
                  snapshot: { NSWorkspace.shared.runningApplications.map(candidate) },
                  subscribe: { changed in
                      let token = NSWorkspace.shared.observe(\.runningApplications, options: [.new]) { _, _ in
                          DispatchQueue.main.async(execute: changed)
                      }
                      return { token.invalidate() }
                  }, requestSelfTermination: requestSelfTermination)
    }

    private func shouldExit(_ phase: MonitorInstancePhase) -> Bool {
        MonitorInstancePolicy.shouldExit(current: current(), others: snapshot(), phase: phase)
    }

    func start() -> Bool {
        guard !shouldExit(.startup) else { return false }
        // LSUIElement apps do not produce Workspace didLaunch notifications.
        cancelObservation = subscribe { [weak self] in
            guard let self = self, !self.stopping, self.shouldExit(.running) else { return }
            self.stop()
            self.requestSelfTermination()
        }
        // Close the snapshot/subscription gap before any status item or observer starts.
        guard !shouldExit(.startup) else { stop(); return false }
        return true
    }

    func stop() {
        stopping = true
        cancelObservation?()
        cancelObservation = nil
    }
}
// END MONITOR INSTANCE COORDINATOR

// This lock belongs only to the observer app. Pipeline and model locks are never touched.
final class InstanceLock {
    private var descriptor: Int32 = -1

    init() throws {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/state/inference-monitor", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let path = directory.appendingPathComponent("app.lock").path
        descriptor = open(path, O_CREAT | O_RDWR | O_CLOEXEC, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            close(descriptor)
            descriptor = -1
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(EWOULDBLOCK))
        }
        // A server child must never retain the single-instance lock after its parent exits.
        _ = fcntl(descriptor, F_SETFD, FD_CLOEXEC)
    }

    deinit {
        if descriptor >= 0 {
            flock(descriptor, LOCK_UN)
            close(descriptor)
        }
    }
}

final class DashboardController: NSViewController {
    let webView: WKWebView
    private(set) var expectedURL: URL?
    private(set) var activeNavigation: WKNavigation?
    private var needsRetry = false
    private var retryCount = 0
    private var retryTimer: Timer?
    private let waiting = NSView()
    private let titleLabel = NSTextField(labelWithString: "Starting Inference Monitor")
    private let detailLabel = NSTextField(wrappingLabelWithString: "Connecting to the local observation service…")
    private let spinner = NSProgressIndicator()

    init(owner: AppDelegate) {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController.add(owner, name: "monitor")
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init(nibName: nil, bundle: nil)
        webView.navigationDelegate = owner
        webView.underPageBackgroundColor = .black
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func loadView() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 820, height: 660))
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.black.cgColor
        view = root
        webView.translatesAutoresizingMaskIntoConstraints = false
        waiting.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(webView)
        root.addSubview(waiting)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.topAnchor.constraint(equalTo: root.topAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            waiting.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            waiting.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            waiting.topAnchor.constraint(equalTo: root.topAnchor),
            waiting.bottomAnchor.constraint(equalTo: root.bottomAnchor)
        ])
        titleLabel.font = .systemFont(ofSize: 22, weight: .semibold)
        titleLabel.textColor = .white
        detailLabel.font = .systemFont(ofSize: 14)
        detailLabel.textColor = NSColor(calibratedWhite: 0.72, alpha: 1)
        detailLabel.alignment = .center
        detailLabel.maximumNumberOfLines = 5
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.startAnimation(nil)
        let stack = NSStackView(views: [spinner, titleLabel, detailLabel])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        waiting.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: waiting.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: waiting.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: waiting.widthAnchor, constant: -64),
            detailLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 560)
        ])
        webView.isHidden = true
    }

    func loadDashboard(_ url: URL) {
        _ = view
        expectedURL = url
        needsRetry = false
        showMessage("Connecting to the dashboard", detail: "Reading live, observed model activity…", isError: false)
        activeNavigation = webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10))
    }

    func navigationFailed() {
        needsRetry = true
        showMessage("Dashboard unavailable", detail: "Reconnecting to the local dashboard…", isError: false)
    }

    func retryIfNeeded(_ url: URL) {
        guard needsRetry, expectedURL == url, retryTimer == nil else { return }
        guard retryCount < 5 else {
            showMessage("Dashboard unavailable", detail: "The local dashboard could not load after several attempts. Quit and reopen Inference Monitor to retry.", isError: true)
            return
        }
        let delay = min(4.0, 0.25 * pow(2.0, Double(retryCount)))
        retryCount += 1
        retryTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            self.retryTimer = nil
            guard self.needsRetry, self.expectedURL == url else { return }
            self.loadDashboard(url)
        }
        if let timer = retryTimer { RunLoop.main.add(timer, forMode: .common) }
    }

    func cancelRetry() {
        retryTimer?.invalidate()
        retryTimer = nil
        retryCount = 0
        needsRetry = false
        expectedURL = nil
        activeNavigation = nil
        webView.stopLoading()
    }

    func showMessage(_ title: String, detail: String, isError: Bool) {
        _ = view
        titleLabel.stringValue = title
        detailLabel.stringValue = detail
        waiting.isHidden = false
        webView.isHidden = true
        if isError { spinner.stopAnimation(nil) } else { spinner.startAnimation(nil) }
        spinner.isHidden = isError
    }

    func showDashboard() {
        retryTimer?.invalidate()
        retryTimer = nil
        retryCount = 0
        needsRetry = false
        waiting.isHidden = true
        webView.isHidden = false
        spinner.stopAnimation(nil)
    }

    func invalidate() {
        cancelRetry()
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "monitor")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler, NSWindowDelegate {
    private var instanceLock: InstanceLock?
    private var instanceCoordinator: MonitorInstanceCoordinator?
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var popoverDashboard: DashboardController!
    private var windowDashboard: DashboardController?
    private var dashboardWindow: NSWindow?
    private var observer: Process?
    private var jevSetupProcess: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var startupBytes = Data()
    private var errorBytes = Data()
    private var startupTimer: Timer?
    private var pollingTimer: Timer?
    private var restartTimer: Timer?
    private var stableTimer: Timer?
    private var restartDelay: TimeInterval = 1
    private var observerGeneration: UInt64 = 0
    private var origin: URL?
    private var requestInFlight = false
    private var pollFailures = 0
    private var observerHealthy = false
    private var terminating = false
    private var serviceError: String?
    private let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 1
        configuration.timeoutIntervalForResource = 1
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        return URLSession(configuration: configuration)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            instanceLock = try InstanceLock()
        } catch {
            // LaunchServices normally coalesces opens; flock handles simultaneous direct launches.
            NSApp.terminate(nil)
            return
        }
        let coordinator = MonitorInstanceCoordinator { NSApp.terminate(nil) }
        instanceCoordinator = coordinator
        guard coordinator.start() else {
            NSApp.terminate(nil)
            return
        }
        NSApp.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "cpu", accessibilityDescription: "Inference Monitor")
            button.image?.isTemplate = true
            button.imagePosition = .imageLeading
            button.title = " ?"
            button.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
            button.toolTip = "Inference Monitor · starting observer"
            button.target = self
            button.action = #selector(statusClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.setAccessibilityLabel("Inference Monitor")
        }
        popoverDashboard = DashboardController(owner: self)
        popover = NSPopover()
        popover.behavior = .transient
        popover.animates = true
        popover.contentViewController = popoverDashboard
        popover.contentSize = NSSize(width: 820, height: 660)
        configureApplicationMenu()
        launchObserver()
        // Explicit diagnostic launch; ordinary background opens remain menu-only.
        if CommandLine.arguments.contains("--show-window") {
            openDashboardWindow(nil)
        }
    }

    // Repeated background opens must neither create another observer nor steal focus.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { false }

    private func configureApplicationMenu() {
        let menuBar = NSMenu()
        let applicationItem = NSMenuItem(title: appName, action: nil, keyEquivalent: "")
        let applicationMenu = NSMenu(title: appName)
        let compact = NSMenuItem(title: "Show Compact Monitor", action: #selector(toggleCompactMonitor(_:)), keyEquivalent: "m")
        compact.keyEquivalentModifierMask = [.command, .shift]
        compact.target = self
        applicationMenu.addItem(compact)
        let dashboard = NSMenuItem(title: "Open Dashboard", action: #selector(openDashboardWindow(_:)), keyEquivalent: "1")
        dashboard.keyEquivalentModifierMask = [.command]
        dashboard.target = self
        applicationMenu.addItem(dashboard)
        applicationMenu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Inference Monitor", action: #selector(quitMonitor(_:)), keyEquivalent: "q")
        quit.keyEquivalentModifierMask = [.command]
        quit.target = self
        applicationMenu.addItem(quit)
        applicationItem.submenu = applicationMenu
        menuBar.addItem(applicationItem)
        NSApp.mainMenu = menuBar
    }

    @objc private func statusClicked(_ sender: Any?) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            popover.performClose(nil)
            let menu = NSMenu()
            let open = NSMenuItem(title: "Open Dashboard Window", action: #selector(openDashboardWindow(_:)), keyEquivalent: "")
            open.target = self
            menu.addItem(open)
            menu.addItem(.separator())
            let quit = NSMenuItem(title: "Quit Inference Monitor", action: #selector(quitMonitor(_:)), keyEquivalent: "q")
            quit.target = self
            menu.addItem(quit)
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
            return
        }
        toggleCompactMonitor(sender)
    }

    @objc private func toggleCompactMonitor(_ sender: Any?) {
        if popover.isShown { popover.performClose(nil); return }
        guard let button = statusItem.button else { return }
        let screen = button.window?.screen ?? NSScreen.main
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        popover.contentSize = NSSize(width: min(820, max(360, visible.width - 32)),
                                    height: min(660, max(300, visible.height - 48)))
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }

    @objc func openDashboardWindow(_ sender: Any?) {
        popover.performClose(nil)
        if dashboardWindow == nil {
            let controller = DashboardController(owner: self)
            let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
            let frame = NSRect(x: 0, y: 0, width: min(1150, screen.width - 48), height: min(800, screen.height - 64))
            let window = NSWindow(contentRect: frame,
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable],
                                  backing: .buffered, defer: false)
            window.title = appName
            window.contentViewController = controller
            window.minSize = NSSize(width: 640, height: 460)
            window.isReleasedWhenClosed = false
            window.delegate = self
            // AppKit adopts the controller's initial 820×660 view when attached.
            // Restore the requested full-window content size after attachment.
            window.setContentSize(frame.size)
            window.center()
            dashboardWindow = window
            windowDashboard = controller
            if let error = serviceError {
                controller.showMessage("Observer unavailable", detail: error, isError: true)
            } else if let url = origin {
                controller.loadDashboard(url)
            }
        }
        NSApp.activate(ignoringOtherApps: true)
        dashboardWindow?.makeKeyAndOrderFront(nil)
    }

    @objc func quitMonitor(_ sender: Any?) { NSApp.terminate(nil) }

    private func launchObserver() {
        observerGeneration += 1
        restartTimer?.invalidate()
        restartTimer = nil
        popoverDashboard?.cancelRetry()
        windowDashboard?.cancelRetry()
        serviceError = nil
        origin = nil
        requestInFlight = false
        pollFailures = 0
        observerHealthy = false
        startupBytes.removeAll()
        errorBytes.removeAll()
        popoverDashboard?.showMessage("Connecting to the dashboard", detail: "Starting the local observer…", isError: false)
        windowDashboard?.showMessage("Connecting to the dashboard", detail: "Starting the local observer…", isError: false)
        guard let resources = Bundle.main.resourceURL else {
            failService("The application resources are missing. Rebuild Inference Monitor.")
            return
        }
        let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        guard let python = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            failService("Python 3 is unavailable. Install Python 3, then reopen Inference Monitor.")
            return
        }
        let script = resources.appendingPathComponent("server.py")
        guard FileManager.default.fileExists(atPath: script.path) else {
            failService("The bundled observer is missing. Rebuild Inference Monitor.")
            return
        }
        let process = Process()
        let output = Pipe()
        let errors = Pipe()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-B", "-u", script.path, "--port", "0", "--parent-pid", String(getpid())]
        process.currentDirectoryURL = resources
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        // launchd and Finder start apps with /usr/bin:/bin:/usr/sbin:/sbin. Give
        // owner commands the same tool lookup a login shell has, so the monitor
        // does not diverge from the command line (Homebrew python3, lms, node).
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        environment["PATH"] = ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.lmstudio/bin",
                               "/usr/bin", "/bin", "/usr/sbin", "/sbin"].joined(separator: ":")
        process.environment = environment
        process.standardOutput = output
        process.standardError = errors
        process.standardInput = FileHandle.nullDevice
        stdoutPipe = output
        stderrPipe = errors
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
            } else {
                DispatchQueue.main.async { self?.consumeStartup(data) }
            }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
            } else {
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.errorBytes.append(data)
                    if self.errorBytes.count > 2048 { self.errorBytes = self.errorBytes.suffix(2048) }
                }
            }
        }
        process.terminationHandler = { [weak self] stopped in
            DispatchQueue.main.async {
                guard let self = self, !self.terminating, self.observer === stopped else { return }
                self.scheduleObserverRestart("The local observer stopped (exit \(stopped.terminationStatus)).")
            }
        }
        observer = process
        do {
            try process.run()
            startupTimer = Timer.scheduledTimer(withTimeInterval: 10, repeats: false) { [weak self] _ in
                guard let self = self, self.origin == nil else { return }
                self.failService("The local observer did not become ready. Reconnecting…")
                if self.observer?.isRunning == true { self.observer?.terminate() }
            }
        } catch {
            scheduleObserverRestart("Could not start the local observer: \(error.localizedDescription)")
        }
    }

    private func scheduleObserverRestart(_ reason: String) {
        guard !terminating, restartTimer == nil else { return }
        observerGeneration += 1
        stableTimer?.invalidate()
        stableTimer = nil
        popoverDashboard?.cancelRetry()
        windowDashboard?.cancelRetry()
        origin = nil
        requestInFlight = false
        pollFailures = 0
        observerHealthy = false
        let delay = restartDelay
        restartDelay = min(30, restartDelay * 2)
        failService("\(reason) Retrying in \(Int(delay)) second\(delay == 1 ? "" : "s")…")
        restartTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            guard let self = self, !self.terminating else { return }
            self.restartTimer = nil
            self.launchObserver()
        }
        if let timer = restartTimer { RunLoop.main.add(timer, forMode: .common) }
    }

    private func consumeStartup(_ data: Data) {
        guard origin == nil, serviceError == nil, !terminating else { return }
        startupBytes.append(data)
        guard startupBytes.count <= 4096 else {
            failService("The observer returned an invalid startup response. Reconnecting…")
            if observer?.isRunning == true { observer?.terminate() }
            return
        }
        guard let newline = startupBytes.firstIndex(of: 10) else { return }
        let line = startupBytes.prefix(upTo: newline)
        guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              let port = object["port"] as? Int, (1...65535).contains(port),
              let pid = object["pid"] as? Int, pid == Int(observer?.processIdentifier ?? -1),
              let url = URL(string: "http://127.0.0.1:\(port)/") else {
            failService("The observer returned an invalid startup response. Reconnecting…")
            if observer?.isRunning == true { observer?.terminate() }
            return
        }
        startupTimer?.invalidate()
        startupTimer = nil
        origin = url
        startupBytes.removeAll()
        // Reset exponential backoff only after the observer stays up, so a
        // process that repeatedly starts and crashes cannot retry every second.
        stableTimer?.invalidate()
        stableTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { [weak self] _ in
            self?.restartDelay = 1
            self?.stableTimer = nil
        }
        popoverDashboard.loadDashboard(url)
        windowDashboard?.loadDashboard(url)
        pollSnapshot()
        pollingTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.pollSnapshot() }
        if let timer = pollingTimer { RunLoop.main.add(timer, forMode: .common) }
    }

    private func pollSnapshot() {
        guard !requestInFlight, !terminating, let base = origin,
              let process = observer, serviceError == nil else { return }
        let generation = observerGeneration
        requestInFlight = true
        let url = base.appendingPathComponent("api/snapshot")
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 1)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        session.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self = self, !self.terminating, self.observerGeneration == generation,
                      self.observer === process, self.origin == base else { return }
                self.requestInFlight = false
                guard error == nil, (response as? HTTPURLResponse)?.statusCode == 200,
                      let data = data, data.count < 2_000_000,
                      let snapshot = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    self.observerHealthy = false
                    self.showUnknownBadge("Observer data unavailable; working count is unknown")
                    self.pollFailures += 1
                    if self.pollFailures == 5, self.observer?.isRunning == true {
                        self.observer?.terminate()
                    }
                    return
                }
                self.pollFailures = 0
                self.observerHealthy = true
                self.updateBadge(snapshot)
                self.popoverDashboard?.retryIfNeeded(base)
                self.windowDashboard?.retryIfNeeded(base)
            }
        }.resume()
    }

    private func updateBadge(_ snapshot: [String: Any]) {
        guard (snapshot["schemaVersion"] as? Int) == 1,
              let sampledAt = snapshot["sampledAt"] as? Double, sampledAt.isFinite,
              let models = snapshot["models"] as? [[String: Any]] else {
            showUnknownBadge("Unsupported observer data; working count is unknown")
            return
        }
        let elapsed = Date().timeIntervalSince1970 - sampledAt
        guard elapsed.isFinite, elapsed >= -2 else {
            showUnknownBadge("Observer timestamp is invalid; working count is unknown")
            return
        }
        var active = 0
        var uncertain = models.isEmpty
        for model in models {
            let state = model["state"] as? String ?? "unknown"
            let host = model["host"] as? String ?? ""
            let threshold: Double = host == "windows" ? 30 : 3
            guard ["mac", "windows"].contains(host),
                  let age = model["ageSeconds"] as? Double, age.isFinite, age >= 0,
                  age + max(0, elapsed) <= threshold else {
                uncertain = true
                continue
            }
            switch state {
            case "generating", "busy": active += 1
            case "idle", "unloaded": break
            case "loaded": uncertain = true
            default: uncertain = true
            }
        }
        let label = uncertain ? (active > 0 ? "\(active)·?" : "?") : String(active)
        let memory = Self.memoryBadge(snapshot, elapsed: elapsed)
        applyMemoryTint(memory?.level)
        // Critical also gets a "!" so the warning never depends on colour alone.
        statusItem.button?.title = " \(label)" + (memory?.level == "critical" ? " !" : "")
        let detail = uncertain ? " · some telemetry is stale or unavailable" : " · fresh observed activity"
        let memoryLine = memory.map { "\nMemory \($0.level)" + ($0.summary.isEmpty ? "" : ": \($0.summary)") } ?? ""
        statusItem.button?.toolTip = "Inference Monitor · \(active) observed working\(detail)\(memoryLine)"
        statusItem.button?.setAccessibilityValue("\(active) models observed working\(detail)"
            + (memory.map { ". Memory \($0.level)" } ?? ""))
    }

    private func showUnknownBadge(_ detail: String) {
        applyMemoryTint(nil)
        statusItem?.button?.title = " ?"
        statusItem?.button?.toolTip = "Inference Monitor · \(detail)"
        statusItem?.button?.setAccessibilityValue("Working count unknown")
    }

    /// Amber when memory is tight, red when critical; the normal menu bar colour otherwise.
    private func applyMemoryTint(_ level: String?) {
        statusItem?.button?.contentTintColor = level == "critical" ? .systemRed
            : level == "tight" ? .systemOrange : nil
    }

    /// Closed reader for snapshot["memory"] (written by mem_guard.py): only the known levels, only while the
    /// sample is fresh, and bounded printable text for the tooltip.
    static func memoryBadge(_ snapshot: [String: Any], elapsed: Double) -> (level: String, summary: String)? {
        guard elapsed <= 10, let memory = snapshot["memory"] as? [String: Any],
              let level = memory["level"] as? String,
              ["ok", "watch", "tight", "critical"].contains(level) else { return nil }
        func clean(_ value: Any?) -> String? {
            guard let text = value as? String else { return nil }
            let printable = String(text.unicodeScalars.filter { $0.value >= 0x20 && $0.value != 0x7F }
                .map(Character.init)).trimmingCharacters(in: .whitespaces)
            return printable.isEmpty ? nil : String(printable.prefix(80))
        }
        var parts: [String] = []
        if let reasons = memory["reasons"] as? [Any], let reason = clean(reasons.first) { parts.append(reason) }
        if let consumers = memory["consumers"] as? [Any], let top = consumers.first as? [String: Any],
           let name = clean(top["label"]), let bytes = top["residentBytes"] as? Double,
           bytes.isFinite, bytes > 0, bytes < 1e15 {
            parts.append(String(format: "%@ about %.1f GB", name, bytes / 1_000_000_000))
        }
        return (level, parts.joined(separator: " · "))
    }

    private func failService(_ detail: String) {
        serviceError = detail
        startupTimer?.invalidate()
        pollingTimer?.invalidate()
        showUnknownBadge(detail)
        popoverDashboard?.showMessage("Observer unavailable", detail: detail, isError: true)
        windowDashboard?.showMessage("Observer unavailable", detail: detail, isError: true)
    }

    private func allowedOrigin(_ url: URL?) -> Bool {
        guard let url = url, let origin = origin else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == origin.port
            && url.user == nil && url.password == nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let documentation = Set(["https://nodejs.org/en/download", "https://docs.typesafe.ai/",
                                 "https://github.com/louiscalata/nisi/tree/v0.2.0#command-line"])
        if navigationAction.navigationType == .linkActivated,
           let url = navigationAction.request.url, documentation.contains(url.absoluteString) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        // Only the explicit documentation links above may leave this origin.
        // Subframes, file URLs and arbitrary loopback services remain blocked.
        decisionHandler(navigationAction.targetFrame?.isMainFrame == true && allowedOrigin(navigationAction.request.url) ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(navigationResponse.isForMainFrame && allowedOrigin(navigationResponse.response.url) ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let controller = controller(for: webView), controller.activeNavigation === navigation,
              controller.expectedURL == origin, allowedOrigin(webView.url), serviceError == nil else { return }
        controller.showDashboard()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        navigationFailed(webView, navigation: navigation, error: error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        navigationFailed(webView, navigation: navigation, error: error)
    }

    private func navigationFailed(_ webView: WKWebView, navigation: WKNavigation, error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled,
              let controller = controller(for: webView), controller.activeNavigation === navigation,
              controller.expectedURL == origin, serviceError == nil, !terminating else { return }
        controller.navigationFailed()
        if observerHealthy, let origin = origin { controller.retryIfNeeded(origin) }
    }

    private func controller(for webView: WKWebView) -> DashboardController? {
        if webView === popoverDashboard?.webView { return popoverDashboard }
        if webView === windowDashboard?.webView { return windowDashboard }
        return nil
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "monitor", message.frameInfo.isMainFrame,
              let webView = message.webView, controller(for: webView) != nil, allowedOrigin(webView.url),
              let origin = origin else { return }
        let sender = message.frameInfo.securityOrigin
        guard sender.protocol == "http", sender.host == "127.0.0.1", sender.port == origin.port,
              let body = message.body as? [String: Any], body.count == 1,
              let action = body["action"] as? String else { return }
        switch action {
        case "expand": openDashboardWindow(nil)
        case "quit": quitMonitor(nil)
        case "jev-configure":
            guard jevSetupProcess?.isRunning != true,
                  let executable = Bundle.main.executableURL?.deletingLastPathComponent().appendingPathComponent("JevKeychain") else { return }
            let setup = Process()
            setup.executableURL = executable
            setup.arguments = ["configure"]
            setup.environment = ["PATH": "/usr/bin:/bin"]
            setup.standardInput = FileHandle.nullDevice
            setup.standardOutput = FileHandle.nullDevice
            setup.standardError = FileHandle.nullDevice
            setup.terminationHandler = { [weak webView] _ in
                DispatchQueue.main.async {
                    webView?.evaluateJavaScript("window.dispatchEvent(new Event('focus'))", completionHandler: nil)
                }
            }
            do { try setup.run(); jevSetupProcess = setup } catch {
                let alert = NSAlert()
                alert.messageText = "Jev setup is unavailable"
                alert.informativeText = "The signed Keychain helper could not start. Reinstall this AGIW build."
                alert.runModal()
            }
        default: break
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        terminating = true
        instanceCoordinator?.stop()
        instanceCoordinator = nil
        startupTimer?.invalidate()
        pollingTimer?.invalidate()
        restartTimer?.invalidate()
        stableTimer?.invalidate()
        session.invalidateAndCancel()
        popoverDashboard?.invalidate()
        windowDashboard?.invalidate()
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        if let process = observer, process.isRunning {
            process.terminate()
            // Only our still-running Process is targeted. Never signal a discovered model/server PID.
            let deadline = Date().addingTimeInterval(1.5)
            while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.025) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
        instanceLock = nil
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
