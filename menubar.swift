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

// 版本号:打包后读 Info.plist;开发态退回仓库 VERSION 文件
let APP_VERSION: String = {
    if let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString")
        as? String, !v.isEmpty { return v }
    if let v = try? String(contentsOfFile: NSHomeDirectory() + "/work/ghspeed/VERSION",
                           encoding: .utf8) {
        return v.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return "dev"
}()

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
                // 未免密授权 → 弹密码框;带上 SUDO_USER 让 root 引擎能回读用户侧种子缓存
                r = adminShell("SUDO_USER=$(/usr/bin/stat -f %Su /dev/console) "
                               + argv.joined(separator: " "),
                               prompt: "GitHub 加速需要管理员权限(修改 /etc/hosts)")
            }
            ok = r.code == 0; out = r.out
        } else {
            // 引擎还没装到 /usr/local/bin,先 install 再执行目标命令
            let py = "/usr/bin/python3 \(ENGINE_SCRIPT)"
            let cmd = "export SUDO_USER=$(/usr/bin/stat -f %Su /dev/console); "
                      + "\(py) install && \(py) " + args.joined(separator: " ")
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

// 系统 DNS 是否已接管——读真实系统态(任一启用服务的 DNS 含 223.5.5.5)。
// 之前查 /var/root 的备份文件,但 osascript 提权时 HOME 保留为用户家(备份根本不在那),
// 且 /var/root 用户态读不了 → 状态永远"未接管",再点就撞"已接管过"报错
func dnsOn() -> Bool {
    let r = runShell(["/usr/sbin/networksetup", "-listallnetworkservices"], timeout: 5)
    guard r.code == 0 else { return false }
    for line in r.out.split(separator: "\n").dropFirst() {
        let svc = line.trimmingCharacters(in: .whitespaces)
        if svc.isEmpty || svc.hasPrefix("*") { continue }   // * = 已禁用的服务
        let g = runShell(["/usr/sbin/networksetup", "-getdnsservers", svc], timeout: 5)
        if g.out.contains("223.5.5.5") { return true }
    }
    return false
}

// ---------------- 活动可视化:连接级实时 + 最近动作 ----------------

func pinnedIPs() -> Set<String> {
    var ips = Set<String>()
    guard let s = try? String(contentsOfFile: "/etc/hosts", encoding: .utf8),
          let rs = s.range(of: HOSTS_MARK) else { return ips }
    for line in s[rs.upperBound...].split(separator: "\n") {
        let p = line.split(separator: " ").map(String.init)
        if p.count == 2, let c = p[0].first, c.isNumber { ips.insert(p[0]) }
    }
    return ips
}

// 正连着钉选 IP 的 ESTABLISHED 连接(= 请求正吃到加速)
func liveConnections() -> (total: Int, names: [String]) {
    let ips = pinnedIPs()
    guard !ips.isEmpty else { return (0, []) }
    let r = runShell(["/usr/sbin/lsof", "-i", "TCP", "-n", "-P", "+c", "0"], timeout: 10)
    guard r.code == 0 else { return (0, []) }
    var agg: [String: Int] = [:]
    for line in r.out.split(separator: "\n") {
        let p = line.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
        guard p.count >= 9, p[8].contains("->"),
              line.contains("(ESTABLISHED)") else { continue }   // 状态是独立列
        let remote = p[8].components(separatedBy: "->")[1]
        let ip = remote.components(separatedBy: ":").dropLast().joined(separator: ":")
        if ips.contains(ip) { agg[p[0], default: 0] += 1 }
    }
    return (agg.values.reduce(0, +),
            agg.sorted { $0.value > $1.value }.map { "\($0.key)×\($0.value)" })
}

func latestAction() -> String? {
    let path = NSHomeDirectory() + "/.ghspeed/activity.log"
    guard let s = try? String(contentsOfFile: path, encoding: .utf8),
          let line = s.split(separator: "\n").last,
          let data = line.data(using: .utf8),
          let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let act = o["action"] as? String else { return nil }
    let df = DateFormatter()
    df.dateFormat = "HH:mm"
    let ts = (o["t"] as? Double).map { df.string(from: Date(timeIntervalSince1970: $0)) } ?? ""
    let detail = o["detail"] as? String ?? ""
    return "\(ts) \(act) \(detail)".trimmingCharacters(in: .whitespaces)
}

// 探测时间线写进 activity.log(仅坏窗口 开始/结束 各一笔;「最近」行与 ghspeed activity 都能看到)
func appendProbeJournal(_ detail: String) {
    let obj: [String: Any] = ["t": Date().timeIntervalSince1970,
                              "action": "probe", "detail": detail]
    guard let d = try? JSONSerialization.data(withJSONObject: obj),
          let line = String(data: d, encoding: .utf8) else { return }
    let dir = NSHomeDirectory() + "/.ghspeed"
    try? FileManager.default.createDirectory(atPath: dir,
                                             withIntermediateDirectories: true)
    let path = dir + "/activity.log"
    if let fh = FileHandle(forWritingAtPath: path) {
        defer { try? fh.close() }
        fh.seekToEndOfFile()
        fh.write((line + "\n").data(using: .utf8)!)
    } else {
        try? (line + "\n").write(toFile: path, atomically: true, encoding: .utf8)
    }
}

// 实测 GitHub 延迟:github.com → api → codeload 逐个试(都是钉选域;
// raw/gist 本网永久 SNI 阻断,不探)。谁通算谁——github.com 单域被掐
// ≠ 整站不可用,别动不动喊"链路波动"。全都不通才返回 nil
func measureLatency() -> (ms: Int, via: String)? {
    for d in ["github.com", "api.github.com", "codeload.github.com"] {
        let sem = DispatchSemaphore(value: 0)
        var ms = -1
        var req = URLRequest(url: URL(string: "https://" + d)!)
        req.httpMethod = "HEAD"
        req.timeoutInterval = 4
        let t0 = Date()
        URLSession.shared.dataTask(with: req) { _, _, err in
            if err == nil { ms = Int(Date().timeIntervalSince(t0) * 1000) }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 5)
        if ms >= 0 { return (ms, d) }
        Thread.sleep(forTimeInterval: 0.5)
    }
    return nil
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
    var dnsItem: NSMenuItem!
    var sniItem: NSMenuItem!
    var actionItems: [NSMenuItem] = []
    var busy = false
    var busyT0: Date?
    var downloading = false
    var timer: Timer?
    var lastHeal = Date.distantPast   // 坏窗口自动重测的限流
    var lastAllDown = false           // 上一轮是否整段不通(决定 30s 加密复查)
    var githubDownStreak = 0          // github.com 连续被掐的轮数(≥2 触发自愈)
    var journalDown = false           // 波动时间线:当前是否处于坏窗口(只记翻转)
    var downSince = Date()            // 坏窗口起点(算持续时长)
    var sshSuggested = false          // ssh443 建议每次运行只提一次,别刷屏
    var liveItem: NSMenuItem!
    var recentItem: NSMenuItem!
    var liveCount = 0

    func applicationDidFinishLaunching(_ n: Notification) {
        item.button?.title = "🐙"
        item.menu = menu
        menu.delegate = self
        menu.autoenablesItems = false

        statusLine = menu.addItem(withTitle: "读取状态中…", action: nil, keyEquivalent: "")
        statusLine.isEnabled = false
        latencyLine = menu.addItem(withTitle: "", action: nil, keyEquivalent: "")
        latencyLine.isEnabled = false
        liveItem = menu.addItem(withTitle: "GitHub 活动:…", action: nil, keyEquivalent: "")
        liveItem.isEnabled = false
        recentItem = menu.addItem(withTitle: "", action: nil, keyEquivalent: "")
        recentItem.isEnabled = false
        menu.addItem(.separator())

        toggleItem = addItem("开启加速", #selector(toggle))
        refreshItem = addItem("立即测速刷新", #selector(refreshNow))
        mirrorsItem = addItem("镜像检测…", #selector(checkMirrors))
        menu.addItem(.separator())
        downloadItem = addItem("⬇️ 下载剪贴板中的 GitHub 链接", #selector(downloadClip))
        menu.addItem(.separator())
        autostartItem = addItem("开机自启", #selector(toggleAutostart))
        dnsItem = addItem("系统 DNS 接管(可选)", #selector(toggleDNS))
        sniItem = addItem("浏览器 SNI 分片(可选)", #selector(toggleSNI))
        addItem("关于 GHSpeed", #selector(about))
        addItem("退出", #selector(quit))
        actionItems = [toggleItem, refreshItem, mirrorsItem, downloadItem, autostartItem, dnsItem, sniItem]

        refreshUI()
        armTimer(120)
        // 活动轮询:谁的连接正吃着钉选 IP + 最近一条自身动作,5 秒一拍
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            DispatchQueue.global().async {
                let r = liveConnections()
                let latest = latestAction()
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.liveCount = r.total
                    if r.total > 0 {
                        self.liveItem.title = "⚡ GitHub 活动:\(r.total) 条连接 ← "
                            + r.names.joined(separator: " ")
                    } else {
                        self.liveItem.title = "GitHub 活动:当前无连接(访问 GitHub 时自动命中钉选)"
                    }
                    self.recentItem.title = latest.map { "最近:\($0)" } ?? ""
                    if !self.downloading {
                        self.item.button?.title = self.liveCount > 0 ? "🐙⚡" : "🐙"
                    }
                }
            }
        }

        // 启动即测:不等点「开启加速」,先在后台跑一轮(用户权限,只更新种子缓存,
        // 不写 hosts、绝不弹密码)。之后点开启/刷新大多走种子轮,几秒收工;
        // 若已开启且配了免密授权,顺手静默应用一轮,没配就到此为止。
        DispatchQueue.global().async { [weak self] in
            _ = runShell(engineUserCmd(["sni", "heal"]), timeout: 120)   // 启动自愈:孤儿 PAC 清扫 / relay 复活
            _ = runShell(engineUserCmd(["probe"]), timeout: 300)
            _ = runShell(engineUserCmd(["mirrors"]), timeout: 300)   // 启动预热:镜像池过期就重验,首次下载零冷启动(后台,不弹窗)
            DispatchQueue.main.async {
                self?.refreshUI()
                guard let self = self else { return }
                let (on, _) = hostsOn()
                if on, FileManager.default.fileExists(atPath: ENGINE_BIN) {
                    let r = runShell(["/usr/bin/sudo", "-n", ENGINE_BIN, "refresh", "-q"],
                                     timeout: 300)
                    if r.code == 0 {
                        notify("GitHub 加速", "启动测速完成,已自动应用最优 IP")
                    }
                    self.refreshUI()
                }
            }
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
            if !self.downloading {
                self.item.button?.title = self.liveCount > 0 ? "🐙⚡" : "🐙"
            }
            self.autostartItem.state = self.autostartInstalled() ? .on : .off
        }
        DispatchQueue.global().async {          // DNS 态要跑 networksetup,放后台查
            let dnsIsOn = dnsOn()
            DispatchQueue.main.async {
                self.dnsItem.state = dnsIsOn ? .on : .off
                self.dnsItem.title = dnsIsOn ? "系统 DNS 还原(取消接管)"
                                             : "系统 DNS 接管(可选)"
            }
        }
        DispatchQueue.global().async {          // SNI 态要查系统 PAC + relay 端口,放后台
            let sniIsOn = self.sniOn()
            DispatchQueue.main.async {
                self.sniItem.state = sniIsOn ? .on : .off
                self.sniItem.title = sniIsOn ? "浏览器 SNI 还原(取消分片)"
                                             : "浏览器 SNI 分片(可选)"
            }
        }
        DispatchQueue.global().async {
            let r = measureLatency()
            DispatchQueue.main.async {
                if let r = r {
                    if r.via == "github.com" {
                        self.latencyLine.title = "github.com:\(r.ms) ms"
                        self.githubDownStreak = 0
                    } else {
                        // github.com 被掐但别的域活着:照实说,别吓人
                        self.latencyLine.title = "github.com 波动中(\(r.via) \(r.ms) ms 可用)"
                        self.githubDownStreak += 1
                    }
                    self.lastAllDown = false
                } else if hostsOn().on {
                    // 已开启还整段不通 = 坏窗口(TLS 全被丢),不是没加速
                    self.latencyLine.title = "github.com:链路波动中(通常几分钟内自动恢复)"
                    self.lastAllDown = true
                    self.githubDownStreak += 1
                } else {
                    self.latencyLine.title = "github.com:当前直连不通(建议开启加速)"
                    self.lastAllDown = false
                }
                self.logProbeTransition(allDown: self.lastAllDown)   // 波动时间线(只记翻转)
                self.autoHealIfNeeded()
                self.armTimer(self.lastAllDown ? 30 : 120)   // 坏窗口期 30 秒一测,快点走出去
            }
        }
    }

    // 波动时间线:只在 翻转/恢复 边界记一笔 probe 行(activity.log,ghspeed activity 可回放)
    func logProbeTransition(allDown: Bool) {
        guard hostsOn().on else { return }   // 只在开启加速期间有意义
        if allDown && !journalDown {
            journalDown = true
            downSince = Date()
            appendProbeJournal("坏窗口开始(三域全不通,自愈中)")
        } else if !allDown && journalDown {
            journalDown = false
            appendProbeJournal("坏窗口结束,持续 \(Int(Date().timeIntervalSince(downSince))) 秒")
        }
    }

    // 坏窗口期加密复查,平时维持 2 分钟一拍(重排前先拆旧 Timer,防叠加)
    func armTimer(_ interval: TimeInterval) {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            self?.refreshUI()
        }
    }

    // 整段不通、或 github.com 连续两轮被掐(其余域还活着)→ 免密重测钉新 IP
    // (2 分钟最多一次;refresh -q 全量重测,当前能过 TLS 握手的候选才会被钉上)
    func autoHealIfNeeded() {
        guard Date().timeIntervalSince(lastHeal) > 120,
              lastAllDown || githubDownStreak >= 2,
              FileManager.default.fileExists(atPath: ENGINE_BIN) else { return }
        lastHeal = Date()
        DispatchQueue.global().async { [weak self] in
            let r = runShell(["/usr/bin/sudo", "-n", ENGINE_BIN, "refresh", "-q"],
                             timeout: 300)
            if r.code == 0 {
                notify("GitHub 加速", "检测到链路波动,已自动重测并更新最优 IP")
            }
            self?.suggestSsh443IfNeeded()
            DispatchQueue.main.async { self?.refreshUI() }
        }
    }

    // HTTPS 坏窗口反复时,若 SSH 443 通道可用且还没开 → 建议一句。
    // ssh.github.com:443 走 SSH 协议,没有 TLS/SNI,对这类波动免疫(阶梯第 2 级)
    func suggestSsh443IfNeeded() {
        guard !sshSuggested else { return }
        let cfg = (try? String(contentsOfFile: NSHomeDirectory() + "/.ssh/config",
                               encoding: .utf8)) ?? ""
        if cfg.contains(">>> ghspeed ssh443 >>>") { return }   // 已开启
        let t = runShell(engineUserCmd(["ssh443", "test"]), timeout: 60)
        guard t.out.contains("Hi ") || t.out.contains("successfully authenticated")
        else { return }                                        // 备用口不通,不推荐
        sshSuggested = true
        DispatchQueue.main.async {
            notify("GitHub 加速",
                   "HTTPS 波动反复,但 SSH 443 通道可用 —— 建议开启:"
                   + "ghspeed ssh443 on(git 场景免疫此类波动)")
        }
    }

    func setBusy(_ b: Bool) {
        // busy 只拦逻辑,不灰菜单项:永远可点,点了给通知(用户反馈:全灰像死了)
        busy = b
        if b { busyT0 = Date() }
    }

    func noteBusy() {
        let secs = busyT0.map { Int(Date().timeIntervalSince($0)) } ?? 0
        notify("GitHub 加速", "⏳ 上一项还在跑(\(secs) 秒),完成前先别切")
    }

    // ---- 动作 ----

    @objc func toggle(_ s: Any) {
        if busy { noteBusy(); return }
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
        if busy { noteBusy(); return }
        setBusy(true)
        statusLine.title = "⏳ 正在测速(通常几秒)…"
        adminEngine(["refresh"]) { ok, out in
            self.setBusy(false)
            notify("GitHub 加速", ok ? "测速完成,已更新最优 IP ✅" : "测速失败:\(out.suffix(120))")
            self.refreshUI()
        }
    }

    @objc func checkMirrors(_ s: Any) {
        if busy { noteBusy(); return }
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

    // 系统 DNS 接管:opt-in(影响全部网站),原配置自动备份,再点一次原样还原
    @objc func toggleDNS(_ s: Any) {
        if busy { noteBusy(); return }
        let on = dnsOn()
        if !on {
            let alert = NSAlert()
            alert.messageText = "接管系统 DNS?"
            alert.informativeText = "换成国内不投毒的公共 DNS(阿里 223.5.5.5 / 腾讯 119.29.29.29)。\n"
                + "影响全部网站(不只 GitHub);原配置自动备份,再点本项即原样还原。"
            alert.addButton(withTitle: "接管")
            alert.addButton(withTitle: "取消")
            if alert.runModal() != .alertFirstButtonReturn { return }
        }
        setBusy(true)
        statusLine.title = on ? "⏳ 正在还原系统 DNS…" : "⏳ 正在接管系统 DNS…"
        adminEngine(["dns", on ? "off" : "on"]) { ok, out in
            self.setBusy(false)
            notify("GitHub 加速", ok
                ? (on ? "已还原系统 DNS" : "已接管系统 DNS(阿里/腾讯,备份可随时还原)✅")
                : "DNS 操作失败:\(out.suffix(120))")
            self.refreshUI()
        }
    }

    // ---- 浏览器 SNI 分片(识别型阻断的本地解法;opt-in,任何关闭都原样还原) ----

    func sniOn() -> Bool {
        let r = runShell(engineUserCmd(["sni", "state"]), timeout: 15)
        return r.out.contains("PAC 接管:已开启")
    }

    @objc func toggleSNI(_ s: Any) {
        if busy { noteBusy(); return }
        let on = sniOn()
        setBusy(true)
        statusLine.title = on ? "⏳ 正在还原 SNI 分片…"
                              : "⏳ 正在开启 SNI 分片(首次先实测各策略,约半分钟)…"
        adminEngine(["sni", on ? "off" : "on"]) { ok, out in
            self.setBusy(false)
            notify("GitHub 加速", ok
                ? (on ? "已还原 SNI 分片设置"
                      : "SNI 分片已开启(仅 GitHub 域走本地 relay,其余直连)✅")
                : "SNI 操作失败:\(out.suffix(120))")
            self.refreshUI()
        }
    }

    @objc func about(_ s: Any) {
        showTextDialog(
            "🐙 GHSpeed v\(APP_VERSION)\n\n"
            + "GitHub 专用加速工具 —— 只碰 GitHub,不碰其他网络\n\n"
            + "github.com/archerzing-tech/ghspeed\n\n"
            + "macOS 菜单栏应用 · 引擎 ghspeed.py\n"
            + "Windows(tkinter 界面)与 Linux(命令行)版本见仓库 Releases",
            title: "关于 GHSpeed")
    }

    @objc func quit(_ s: Any) {
        if busy { noteBusy(); return }   // 正在执行的动作没落地前不退,防半截状态
        if sniOn() {         // 生命周期契约:退出即还原(弹一次授权;取消则下次启动自愈兜底)
            let argv = engineUserCmd(["sni", "off"])
            _ = adminShell(argv.joined(separator: " "),
                           prompt: "退出前还原浏览器 SNI 分片设置")
        }
        NSApp.terminate(nil)
    }
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
