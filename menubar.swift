// GHSpeed 菜单栏应用 — ghspeed 命令行引擎的 GUI 壳
// 构建: swiftc -O menubar.swift -o GHSpeed.app/Contents/MacOS/GHSpeed
import AppKit

// 引擎脚本:优先用打进 App Resources 的自包含副本,开发时退回仓库路径
let ENGINE_SCRIPT: String = {
    if let bundled = Bundle.main.path(forResource: "ghspeed", ofType: "py") {
        return bundled
    }
    for rel in ["/work/ghspeed/ghspeed.py", "/tools/ghspeed/ghspeed.py"] {
        let p = NSHomeDirectory() + rel
        if FileManager.default.fileExists(atPath: p) { return p }
    }
    return NSHomeDirectory() + "/work/ghspeed/ghspeed.py"
}()
let ENGINE_BIN = "/usr/local/bin/ghspeed"
// App 自身位置(从可执行文件路径反推),装在哪开机自启就指向哪
let APP_BUNDLE: String = {
    if let exe = Bundle.main.executableURL?.path {
        var url = URL(fileURLWithPath: exe)
        for _ in 0..<3 { url.deleteLastPathComponent() } // Contents/MacOS/GHSpeed → *.app
        if url.pathExtension == "app" { return url.path }
    }
    return "/Applications/GHSpeed.app"
}()
let HOSTS_MARK = "# >>> ghspeed start >>>"

// ---------------- 进程工具 ----------------

func runShell(_ argv: [String], timeout: TimeInterval) -> (code: Int32, out: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: argv[0])
    p.arguments = Array(argv.dropFirst())
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { return (-1, "\(error)") }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    let deadline = Date().addingTimeInterval(timeout)
    while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
    if p.isRunning { p.terminate(); return (-2, "超时") }
    return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

// 弹系统密码框执行一条 shell
func adminShell(_ cmd: String, prompt: String) -> (code: Int32, out: String) {
    let esc = cmd.replacingOccurrences(of: "\\", with: "\\\\")
                  .replacingOccurrences(of: "\"", with: "\\\"")
    let src = "do shell script \"\(esc)\" with administrator privileges with prompt \"\(prompt)\""
    return runShell(["/usr/bin/osascript", "-e", src], timeout: 600)
}

func notify(_ title: String, _ msg: String) {
    let a = msg.replacingOccurrences(of: "\"", with: "\\\"")
    _ = runShell(["/usr/bin/osascript", "-e",
                  "display notification \"\(a)\" with title \"\(title)\""],
                 timeout: 10)
}

// 弹窗显示多行文本(经临时文件绕开 AppleScript 不接受裸换行的限制);后台执行,不卡菜单
func showTextDialog(_ text: String, title: String) {
    DispatchQueue.global().async {
        let tmp = "/tmp/ghspeed-dialog.txt"
        try? text.write(toFile: tmp, atomically: true, encoding: .utf8)
        _ = runShell(["/usr/bin/osascript", "-e",
            "display dialog (do shell script \"cat \(tmp)\") with title \"\(title)\" buttons {\"好\"} default button 1"],
            timeout: 60)
    }
}

// ---------------- 引擎调用 ----------------

// 管理员操作(on/off/refresh):已装好 /usr/local/bin/ghspeed 且做过免密授权则静默;
// 否则首次会自动顺带 install,之后弹系统密码框
func adminEngine(_ args: [String], done: @escaping (Bool, String) -> Void) {
    DispatchQueue.global().async {
        var ok = false
        var out = ""
        if FileManager.default.fileExists(atPath: ENGINE_BIN) {
            var argv = [ENGINE_BIN] + args
            var r = runShell(["/usr/bin/sudo", "-n"] + argv, timeout: 600)
            if r.code != 0 && r.out.contains("a password is required") {
                // 未免密授权 → 弹密码框
                r = adminShell(argv.joined(separator: " "),
                               prompt: "GitHub 加速需要管理员权限(修改 /etc/hosts)")
            }
            ok = r.code == 0; out = r.out
        } else {
            // 引擎还没装到 /usr/local/bin,先 install 再执行目标命令
            let py = "/usr/bin/python3 \(ENGINE_SCRIPT)"
            let cmd = "\(py) install && \(py) " + args.joined(separator: " ")
            let r = adminShell(cmd, prompt: "GitHub 加速首次开启:安装命令行并修改 /etc/hosts")
            ok = r.code == 0; out = r.out
        }
        DispatchQueue.main.async { done(ok, out) }
    }
}

// ---------------- 状态探测 ----------------

func hostsOn() -> (on: Bool, count: Int) {
    guard let s = try? String(contentsOfFile: "/etc/hosts", encoding: .utf8) else { return (false, 0) }
    guard let rs = s.range(of: HOSTS_MARK) else { return (false, 0) }
    let body = s[rs.upperBound...]
    let n = body.components(separatedBy: "\n").filter {
        $0.contains("github") && !$0.trimmingCharacters(in: .whitespaces).hasPrefix("#")
    }.count
    return (true, n)
}

// 实测 github.com 延迟(HEAD,4 秒超时);返回毫秒,-1 不通
func measureLatency() -> Int {
    let sem = DispatchSemaphore(value: 0)
    var ms = -1
    var req = URLRequest(url: URL(string: "https://github.com")!)
    req.httpMethod = "HEAD"
    req.timeoutInterval = 4
    let t0 = Date()
    URLSession.shared.dataTask(with: req) { _, _, err in
        if err == nil { ms = Int(Date().timeIntervalSince(t0) * 1000) }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + 5)
    return ms
}

// ---------------- 主应用 ----------------

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var statusLine: NSMenuItem!
    var latencyLine: NSMenuItem!
    var toggleItem: NSMenuItem!
    var refreshItem: NSMenuItem!
    var mirrorsItem: NSMenuItem!
    var downloadItem: NSMenuItem!
    var autostartItem: NSMenuItem!
    var actionItems: [NSMenuItem] = []
    var busy = false
    var downloading = false
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        item.button?.title = "🐙"
        item.menu = menu
        menu.delegate = self
        menu.autoenablesItems = false

        statusLine = menu.addItem(withTitle: "读取状态中…", action: nil, keyEquivalent: "")
        statusLine.isEnabled = false
        latencyLine = menu.addItem(withTitle: "", action: nil, keyEquivalent: "")
        latencyLine.isEnabled = false
        menu.addItem(.separator())

        toggleItem = addItem("开启加速", #selector(toggle))
        refreshItem = addItem("立即测速刷新", #selector(refreshNow))
        mirrorsItem = addItem("镜像检测…", #selector(checkMirrors))
        menu.addItem(.separator())
        downloadItem = addItem("⬇️ 下载剪贴板中的 GitHub 链接", #selector(downloadClip))
        menu.addItem(.separator())
        autostartItem = addItem("开机自启", #selector(toggleAutostart))
        addItem("退出", #selector(quit))
        actionItems = [toggleItem, refreshItem, mirrorsItem, downloadItem, autostartItem]

        refreshUI()
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.refreshUI()
        }
    }

    func addItem(_ title: String, _ action: Selector) -> NSMenuItem {
        let it = menu.addItem(withTitle: title, action: action, keyEquivalent: "")
        it.target = self
        return it
    }

    func menuNeedsUpdate(_ m: NSMenu) { refreshUI() }

    func refreshUI() {
        let (on, count) = hostsOn()
        DispatchQueue.main.async {
            self.statusLine.title = on
                ? "🚀 加速已开启(固定 \(count) 个域名)"
                : "🐙 加速未开启"
            self.toggleItem.title = on ? "关闭加速" : "开启加速"
            self.latencyLine.title = "github.com:检测中…"
            if !self.downloading { self.item.button?.title = on ? "🚀" : "🐙" }
            self.autostartItem.state = self.autostartInstalled() ? .on : .off
        }
        DispatchQueue.global().async {
            let ms = measureLatency()
            DispatchQueue.main.async {
                self.latencyLine.title = ms < 0
                    ? "github.com:当前直连不通(建议开启加速)"
                    : "github.com:\(ms) ms"
            }
        }
    }

    func setBusy(_ b: Bool) {
        busy = b
        DispatchQueue.main.async {
            for it in self.actionItems { it.isEnabled = !b }
        }
    }

    // ---- 动作 ----

    @objc func toggle(_ s: Any) {
        if busy { return }
        let (on, _) = hostsOn()
        let act = on ? "off" : "on"
        setBusy(true)
        statusLine.title = act == "on" ? "⏳ 正在开启(通常几秒,网络差时约 1 分钟)…" : "⏳ 正在关闭…"
        adminEngine([act]) { ok, out in
            self.setBusy(false)
            if ok {
                notify("GitHub 加速", act == "on" ? "已开启 ✅" : "已关闭,已还原 /etc/hosts")
            } else {
                notify("GitHub 加速", "操作失败:\(out.suffix(120))")
            }
            self.refreshUI()
        }
    }

    @objc func refreshNow(_ s: Any) {
        if busy { return }
        setBusy(true)
        statusLine.title = "⏳ 正在测速(通常几秒)…"
        adminEngine(["refresh"]) { ok, out in
            self.setBusy(false)
            notify("GitHub 加速", ok ? "测速完成,已更新最优 IP ✅" : "测速失败:\(out.suffix(120))")
            self.refreshUI()
        }
    }

    @objc func checkMirrors(_ s: Any) {
        if busy { return }
        setBusy(true)
        DispatchQueue.global().async {
            let r = runEngineMirrors()
            DispatchQueue.main.async {
                self.setBusy(false)
                showTextDialog(r.out.isEmpty ? "(无输出)" : String(r.out.suffix(1500)),
                               title: "GitHub 镜像健康检查")
            }
        }
    }

    @objc func downloadClip(_ s: Any) {
        if downloading { notify("GitHub 加速", "已有下载在进行中"); return }
        let clip = NSPasteboard.general.string(forType: .string)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard clip.hasPrefix("http"), clip.lowercased().contains("github") else {
            notify("GitHub 加速", "剪贴板里没有 GitHub 链接"); return
        }
        // 引擎 -o 要完整文件路径
        let name = clip.split(separator: "?")[0].split(separator: "/").last.map(String.init) ?? "download.bin"
        let outPath = NSHomeDirectory() + "/Downloads/" + name
        downloading = true
        setBusy(true)
        item.button?.title = "⬇️ …"
        DispatchQueue.global().async {
            let argv = engineUserCmd(["dl", clip, "-o", outPath])
            let p = Process()
            p.executableURL = URL(fileURLWithPath: argv[0])
            p.arguments = Array(argv.dropFirst())
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            do { try p.run() } catch {
                DispatchQueue.main.async { self.downloadDone(ok: false, msg: "\(error)") }
                return
            }
            let fh = pipe.fileHandleForReading
            fh.readabilityHandler = { h in
                guard let s = String(data: h.availableData, encoding: .utf8) else { return }
                // 进度行形如 "    3.5 MB / 11.0 MB  0.30 MB/s"
                if let m = s.range(of: #"([\d.]+) ([KMG])B / ([\d.]+) ([KMG])B"#,
                                   options: .regularExpression) {
                    let parts = s[m].components(separatedBy: " ")
                    if parts.count == 4,
                       let got = Double(parts[0]), let total = Double(parts[2]), total > 0 {
                        let mult: [String: Double] = ["K": 1, "M": 1e3, "G": 1e6]
                        let pct = Int(got * (mult[parts[1]] ?? 1) /
                                      (total * (mult[parts[3]] ?? 1)) * 100)
                        DispatchQueue.main.async {
                            self.item.button?.title = "⬇️ \(min(pct, 100))%"
                        }
                    }
                }
            }
            p.waitUntilExit()
            fh.readabilityHandler = nil
            DispatchQueue.main.async {
                self.downloadDone(ok: p.terminationStatus == 0, msg: name)
            }
        }
    }

    func downloadDone(ok: Bool, msg: String) {
        downloading = false
        setBusy(false)
        notify("GitHub 加速", ok ? "下载完成 → Downloads/\(msg)"
                                : "下载失败(镜像与直连均已尝试,详见 ghspeed mirrors)")
        refreshUI()
    }

    // ---- 开机自启(用户级 LaunchAgent,不需要管理员)----

    func plistPath() -> String {
        return NSHomeDirectory() + "/Library/LaunchAgents/com.ghspeed.menubar.plist"
    }

    func autostartInstalled() -> Bool {
        return FileManager.default.fileExists(atPath: plistPath())
    }

    @objc func toggleAutostart(_ s: Any) {
        let path = plistPath()
        if autostartInstalled() {
            _ = runShell(["/bin/launchctl", "unload", path], timeout: 15)
            try? FileManager.default.removeItem(atPath: path)
            notify("GitHub 加速", "已取消开机自启")
        } else {
            let xml = """
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0"><dict>
                <key>Label</key><string>com.ghspeed.menubar</string>
                <key>ProgramArguments</key>
                <array><string>\(APP_BUNDLE)/Contents/MacOS/GHSpeed</string></array>
                <key>RunAtLoad</key><true/>
            </dict></plist>
            """
            let dir = NSHomeDirectory() + "/Library/LaunchAgents"
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
            try? xml.write(toFile: path, atomically: true, encoding: .utf8)
            _ = runShell(["/bin/launchctl", "load", path], timeout: 15)
            notify("GitHub 加速", "已设置开机自启")
        }
        refreshUI()
    }

    @objc func quit(_ s: Any) { NSApp.terminate(nil) }
}

// 用户权限就能跑的引擎调用
func engineUserCmd(_ args: [String]) -> [String] {
    if FileManager.default.fileExists(atPath: ENGINE_BIN) {
        return [ENGINE_BIN] + args
    }
    return ["/usr/bin/python3", ENGINE_SCRIPT] + args
}

func runEngineMirrors() -> (code: Int32, out: String) {
    let argv = engineUserCmd(["mirrors"])
    return runShell(argv, timeout: 120)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
