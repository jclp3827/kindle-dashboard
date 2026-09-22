import Cocoa

let HOME = NSHomeDirectory()
let REPO = HOME + "/kindle-dashboard-repo"
let SERVICE_SH = REPO + "/installers/macos/service.sh"
let CONFIG_PATH = HOME + "/.config/kindle-dashboard/config.yaml"
let PORT = 8585

enum RunState { case running, paused, stopped, checking }

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var state: RunState = .checking
    var token: String = ""

    // 菜单项引用
    var statusMenuItem: NSMenuItem!
    var toggleRunItem: NSMenuItem!
    var pauseItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        loadToken()
        buildMenu()
        // 启动时确保服务在跑（不弹浏览器）
        runService("start")
        // 定时刷新状态
        Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { _ in self.refresh() }
        refresh()
    }

    func loadToken() {
        guard let s = try? String(contentsOfFile: CONFIG_PATH, encoding: .utf8) else { return }
        for line in s.split(separator: "\n") {
            if line.contains("access_token:") {
                var v = String(line.split(separator: ":", maxSplits: 1).last ?? "")
                v = v.trimmingCharacters(in: .whitespaces)
                v = v.trimmingCharacters(in: CharacterSet(charactersIn: "\""))
                token = v
                break
            }
        }
    }

    func buildMenu() {
        let menu = NSMenu()
        // 关闭自动启用，否则 isEnabled=false 会被响应链自动覆盖
        menu.autoenablesItems = false
        statusMenuItem = NSMenuItem(title: "正在检测…", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        menu.addItem(NSMenuItem.separator())

        toggleRunItem = NSMenuItem(title: "停止看板", action: #selector(toggleRun), keyEquivalent: "")
        toggleRunItem.target = self
        menu.addItem(toggleRunItem)

        pauseItem = NSMenuItem(title: "暂停", action: #selector(togglePause), keyEquivalent: "")
        pauseItem.target = self
        menu.addItem(pauseItem)

        menu.addItem(NSMenuItem.separator())
        let openItem = NSMenuItem(title: "打开设置页", action: #selector(openSetup), keyEquivalent: "")
        openItem.target = self
        menu.addItem(openItem)
        let quitItem = NSMenuItem(title: "退出 kindle看板", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(NSMenuItem.separator())
        menu.addItem(quitItem)

        statusItem.menu = menu
        updateMenu()
    }

    @objc func toggleRun() {
        if state == .stopped {
            runService("start")
            state = .checking
            updateMenu()
            DispatchQueue.main.asyncAfter(deadline: .now() + 14) { self.refresh() }
        } else {
            runService("stop")
            state = .stopped
            updateMenu()
        }
    }

    @objc func togglePause() {
        let wantPause = (state != .paused)
        setPause(wantPause) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.refresh() }
        }
    }

    @objc func openSetup() {
        var url = "http://127.0.0.1:\(PORT)/setup"
        if !token.isEmpty { url += "?token=\(token)" }
        if let u = URL(string: url) { NSWorkspace.shared.open(u) }
    }

    @objc func quit() {
        // 退出菜单栏 = 彻底关闭：先停掉后台服务（采集/渲染/拉取/推送全部停止），再退出
        let p = Process()
        p.launchPath = "/bin/zsh"
        p.arguments = [SERVICE_SH, "stop"]
        p.standardOutput = Pipe(); p.standardError = Pipe()
        try? p.run()
        p.waitUntilExit()
        NSApp.terminate(nil)
    }

    // MARK: - 服务进程控制
    func runService(_ action: String) {
        let p = Process()
        p.launchPath = "/bin/zsh"
        p.arguments = [SERVICE_SH, action]
        p.standardOutput = Pipe(); p.standardError = Pipe()
        try? p.run()
    }

    // MARK: - 直连（绕过本地代理）
    func directSession() -> URLSession {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.connectionProxyDictionary = [
            kCFNetworkProxiesHTTPEnable: false,
            kCFNetworkProxiesHTTPSEnable: false,
            kCFNetworkProxiesSOCKSEnable: false,
            kCFNetworkProxiesProxyAutoConfigEnable: false
        ]
        cfg.timeoutIntervalForRequest = 2
        cfg.timeoutIntervalForResource = 3
        return URLSession(configuration: cfg)
    }

    func refresh() {
        let h = URL(string: "http://127.0.0.1:\(PORT)/health")!
        directSession().dataTask(with: h) { [weak self] data, resp, err in
            guard let self = self else { return }
            if err != nil {
                DispatchQueue.main.async { self.setState(.stopped) }
                return
            }
            // 健康→查 paused 状态
            let su = URL(string: "http://127.0.0.1:\(PORT)/api/system/status")!
            var req = URLRequest(url: su)
            if !self.token.isEmpty { req.setValue(self.token, forHTTPHeaderField: "X-Access-Token") }
            self.directSession().dataTask(with: req) { data, _, _ in
                var newState: RunState = .running
                if let data = data,
                   let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let paused = j["paused"] as? Bool {
                    newState = paused ? .paused : .running
                }
                DispatchQueue.main.async { self.setState(newState) }
            }.resume()
        }.resume()
    }

    func setPause(_ on: Bool, completion: @escaping () -> Void) {
        let u = URL(string: "http://127.0.0.1:\(PORT)/api/system/pause")!
        var req = URLRequest(url: u)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !token.isEmpty { req.setValue(token, forHTTPHeaderField: "X-Access-Token") }
        req.httpBody = "{\"paused\":\(on)}".data(using: .utf8)
        directSession().dataTask(with: req) { _, _, _ in completion() }.resume()
    }

    func setState(_ s: RunState) {
        state = s
        updateMenu()
    }

    func updateMenu() {
        DispatchQueue.main.async {
            switch self.state {
            case .running:
                self.statusMenuItem.title = "看板运行中"
                self.toggleRunItem.title = "停止看板"
                self.toggleRunItem.isEnabled = true
                self.pauseItem.title = "暂停"
                self.pauseItem.isEnabled = true
                self.setIcon("book", alpha: 1.0)
            case .paused:
                self.statusMenuItem.title = "已暂停（Kindle 停在当前画面）"
                self.toggleRunItem.title = "停止看板"
                self.toggleRunItem.isEnabled = true
                self.pauseItem.title = "恢复"
                self.pauseItem.isEnabled = true
                self.setIcon("book", alpha: 0.45)
            case .stopped:
                self.statusMenuItem.title = "看板已停止"
                self.toggleRunItem.title = "启动看板"
                self.toggleRunItem.isEnabled = true
                self.pauseItem.title = "暂停"
                self.pauseItem.isEnabled = false
                self.setIcon("book", alpha: 0.3)
            case .checking:
                self.statusMenuItem.title = "正在检测…"
                self.toggleRunItem.isEnabled = false
                self.pauseItem.isEnabled = false
                self.setIcon("book", alpha: 0.6)
            }
        }
    }

    func setIcon(_ symbol: String, alpha: CGFloat) {
        guard let btn = statusItem.button else { return }
        guard let img = NSImage(systemSymbolName: symbol, accessibilityDescription: "kindle看板") else { return }
        img.isTemplate = true   // 模板图：系统自动按菜单栏明暗着色
        // 只画一次，用 fraction 控制透明度来表达状态（运行实/暂停虚/停止更虚）
        let r = NSRect(origin: .zero, size: img.size)
        let faded = NSImage(size: img.size)
        faded.lockFocus()
        img.draw(in: r, from: .zero, operation: .sourceOver, fraction: alpha)
        faded.unlockFocus()
        faded.isTemplate = true
        btn.image = faded
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)   // 不进 Dock，只在菜单栏
app.run()
