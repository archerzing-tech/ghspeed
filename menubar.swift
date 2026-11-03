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
        let base = engineUserBase()
        if base[0] == ENGINE_BIN {
            let argv = base + args
            var r = runShell(["/usr/bin/sudo", "-n"] + argv, timeout: 600)
            if r.code != 0 && r.out.contains("a password is required") {
                // 未免密授权 → 弹密码框;带上 SUDO_USER 让 root 引擎能回读用户侧种子缓存
                r = adminShell("SUDO_USER=$(/usr/bin/stat -f %Su /dev/console) "
                               + argv.joined(separator: " "),
                               prompt: "GitHub 加速需要管理员权限(修改 /etc/hosts)")
            }
            ok = r.code == 0; out = r.out
        } else {
            // CLI 缺失或落后于 App:用 App 内引擎,并顺手把 CLI 装成同版本
            let py = "/usr/bin/python3 '\(ENGINE_SCRIPT)'"
            let cmd = "export SUDO_USER=$(/usr/bin/stat -f %Su /dev/console); "
                      + "\(py) install >/dev/null 2>&1 || true; \(py) "
                      + args.joined(separator: " ")
            let r = adminShell(cmd, prompt: "GitHub 加速需要管理员权限(修改 /etc/hosts)")
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

// ---------------- 菜单 UI 缓存:冷启动即有内容,不等后台探测 ----------------

let UI_CACHE_FILE = NSHomeDirectory() + "/.ghspeed/menubar-ui.json"

func loadUiCache() -> [String: Any] {
    return (NSDictionary(contentsOfFile: UI_CACHE_FILE) as? [String: Any]) ?? [:]
}

func saveUiCache(_ key: String, _ value: Any) {
    var d = loadUiCache()
    d[key] = value
    let dir = NSHomeDirectory() + "/.ghspeed"
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    _ = NSDictionary(dictionary: d).write(toFile: UI_CACHE_FILE, atomically: true)
}

func hhmm(_ ts: Double) -> String {
    let df = DateFormatter()
    df.dateFormat = "HH:mm"
    return df.string(from: Date(timeIntervalSince1970: ts))
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

struct UpdateInfo {
    let latest: String
    let update: Bool
    let asset: String
}

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
    var tierItem: NSMenuItem!
    var updateItem: NSMenuItem!
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
    var lastLiveAt = Date.distantPast   // ⚡ 图标粘滞:30 秒内见过连接就算活跃,不闪
    var busyItem: NSMenuItem?           // 正在跑的动作对应的菜单项(置灰防连点)
    var busyItemTitle = ""
    var probing = false                 // 延迟探测单飞:上一轮没回来不再发新一轮
    var lastProbeAt = Date.distantPast
    var lastDnsCheck = Date.distantPast
    var lastSniCheck = Date.distantPast
    var lastTierCheck = Date.distantPast
    var allDownStreak = 0               // 整段不通的连续轮数(≥2 才认,防单次超时误报)
    var healEpisodeNotified = false     // 一段坏窗口「已自动重测」只通知一次

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
        updateItem = addItem("检查更新…", #selector(checkUpdate))
        menu.addItem(.separator())
        downloadItem = addItem("⬇️ 下载剪贴板中的 GitHub 链接", #selector(downloadClip))
        menu.addItem(.separator())
        autostartItem = addItem("开机自启", #selector(toggleAutostart))
        dnsItem = addItem("系统 DNS 接管(可选)", #selector(toggleDNS))
        sniItem = addItem("浏览器 SNI 分片(可选)", #selector(toggleSNI))
        tierItem = addItem("本地下载档位(可选)", #selector(toggleTier))
        addItem("关于 GHSpeed", #selector(about))
        addItem("退出", #selector(quit))
        actionItems = [toggleItem, refreshItem, mirrorsItem, updateItem, downloadItem,
                       autostartItem, dnsItem, sniItem, tierItem]

        primeFromCache()   // 冷启动先摆上次的结论,菜单第一下打开就有内容
        refreshUI()
        armTimer(120)
        // 启动后静默查一次更新(吃 6h 缓存):有新版才通知一次,不打断正事
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            self?.autoCheckUpdate()
        }
        // 长期挂着的菜单栏应用也别错过新版:每 6 小时再静默看一次
        Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { [weak self] _ in
            self?.autoCheckUpdate()
        }
        // 活动轮询:谁的连接正吃着钉选 IP + 最近一条自身动作,5 秒一拍
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            DispatchQueue.global().async {
                let r = liveConnections()
                let latest = latestAction()
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.liveCount = r.total
                    if r.total > 0 { self.lastLiveAt = Date() }
                    if r.total > 0 {
                        self.liveItem.title = "⚡ GitHub 活动:\(r.total) 条连接 ← "
                            + r.names.joined(separator: " ")
                    } else {
                        self.liveItem.title = "GitHub 活动:当前无连接(访问 GitHub 时自动命中钉选)"
                    }
                    self.recentItem.title = latest.map { "最近:\($0)" } ?? ""
                    if !self.downloading { self.updateIcon() }
                }
            }
        }

        // 启动即测:不等点「开启加速」,先在后台跑一轮(用户权限,只更新种子缓存,
        // 不写 hosts、绝不弹密码)。之后点开启/刷新大多走种子轮,几秒收工;
        // 若已开启且配了免密授权,顺手静默应用一轮,没配就到此为止。
        DispatchQueue.global().async { [weak self] in
            // 顺序讲究:probe 最优先 —— 种子缓存热了,点「开启加速」才能几秒收工;
            // sni heal 只是孤儿清扫,排最后,不挡正事
            _ = runShell(engineUserCmd(["probe"]), timeout: 300)
            _ = runShell(engineUserCmd(["mirrors"]), timeout: 300)   // 启动预热:镜像池过期就重验,首次下载零冷启动(后台,不弹窗)
            _ = runShell(engineUserCmd(["sni", "heal"]), timeout: 120)   // 启动自愈:孤儿 PAC 清扫 / relay 复活
            DispatchQueue.main.async {
                self?.refreshUI(true)
                guard let self = self else { return }
                let (on, _) = hostsOn()
                if on, FileManager.default.fileExists(atPath: ENGINE_BIN), !self.busy {
                    let r = runShell(["/usr/bin/sudo", "-n", ENGINE_BIN, "refresh", "-q"],
                                     timeout: 300)
                    if r.code == 0 {
                        notify("GitHub 加速", "启动测速完成,已自动应用最优 IP")
                    }
                    self.refreshUI(true)
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

    // 只在主线程调用(所有调用点都在主线程)。原则:已有的结论先摆着,探测只做增量更新,
    // 绝不把界面打回「检测中…」干等 —— 菜单开合要即时有内容。
    func refreshUI(_ force: Bool = false) {
        let (on, count) = hostsOn()
        if !busy {   // 动作进行中,状态行让位给「⏳ 正在…」提示
            statusLine.title = on
                ? "🚀 加速已开启(固定 \(count) 个域名)"
                : "🐙 加速未开启"
        }
        if busyItem !== toggleItem {
            toggleItem.title = on ? "关闭加速" : "开启加速"
        }
        if !downloading { updateIcon() }
        autostartItem.state = autostartInstalled() ? .on : .off

        let now = Date()
        // DNS / SNI 态有 5 分钟缓存:菜单反复开合不再反复跑 networksetup / 引擎子进程
        if now.timeIntervalSince(lastDnsCheck) > 300 {
            lastDnsCheck = now
            DispatchQueue.global().async { [weak self] in
                let dnsIsOn = dnsOn()   // 要跑 networksetup,放后台
                DispatchQueue.main.async {
                    guard let self = self, self.busyItem !== self.dnsItem else { return }
                    self.dnsItem.state = dnsIsOn ? .on : .off
                    self.dnsItem.title = dnsIsOn ? "系统 DNS 还原(取消接管)"
                                                 : "系统 DNS 接管(可选)"
                    saveUiCache("dns_on", dnsIsOn)
                }
            }
        }
        if now.timeIntervalSince(lastSniCheck) > 300 {
            lastSniCheck = now
            DispatchQueue.global().async { [weak self] in
                let sniIsOn = self?.sniOn() ?? false   // 要查系统 PAC + relay 端口,放后台
                DispatchQueue.main.async {
                    guard let self = self, self.busyItem !== self.sniItem else { return }
                    self.sniItem.state = sniIsOn ? .on : .off
                    self.sniItem.title = sniIsOn ? "浏览器 SNI 还原(取消分片)"
                                                 : "浏览器 SNI 分片(可选)"
                    saveUiCache("sni_on", sniIsOn)
                }
            }
        }
        if now.timeIntervalSince(lastTierCheck) > 300 {
            lastTierCheck = now
            DispatchQueue.global().async { [weak self] in
                let tierIsOn = self?.tierOn() ?? false   // 读档位状态文件 + 探端口
                DispatchQueue.main.async {
                    guard let self = self, self.busyItem !== self.tierItem else { return }
                    self.tierItem.state = tierIsOn ? .on : .off
                    self.tierItem.title = tierIsOn ? "本地下载档位还原(取消接管)"
                                                   : "本地下载档位(可选)"
                    saveUiCache("tier_on", tierIsOn)
                }
            }
        }
        // 延迟探测:25 秒内测过直接复用旧值(连开菜单/连点不再刷屏式探测),且探测单飞
        guard !probing else { return }
        guard force || now.timeIntervalSince(lastProbeAt) > 25 else { return }
        probing = true
        lastProbeAt = now
        DispatchQueue.global().async { [weak self] in
            let r = measureLatency()
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.probing = false
                self.absorbLatency(r)
            }
        }
    }

    // 冷启动先用 ~/.ghspeed/menubar-ui.json 里的上次结论把菜单填上,后台探测慢慢刷新 ——
    // 打开应用第一下就有可看可点的状态
    func primeFromCache() {
        let c = loadUiCache()
        let (on, count) = hostsOn()
        statusLine.title = on ? "🚀 加速已开启(固定 \(count) 个域名)" : "🐙 加速未开启"
        toggleItem.title = on ? "关闭加速" : "开启加速"
        if let ms = c["latency_ms"] as? Int, let via = c["latency_via"] as? String,
           let ts = c["latency_ts"] as? Double, ms >= 0 {
            let ageH = (Date().timeIntervalSince1970 - ts) / 3600
            let stamp = ageH < 24 ? (ageH > 10.0 / 60 ? "(\(hhmm(ts)) 测)" : "") : ""
            latencyLine.title = via == "github.com"
                ? "github.com:\(ms) ms\(stamp)"
                : "github.com 波动中(\(via) \(ms) ms 可用)\(stamp)"
            lastProbeAt = Date(timeIntervalSince1970: ts)
        } else {
            latencyLine.title = "github.com:检测中…"
        }
        if let b = c["dns_on"] as? Bool {
            dnsItem.state = b ? .on : .off
            dnsItem.title = b ? "系统 DNS 还原(取消接管)" : "系统 DNS 接管(可选)"
            lastDnsCheck = Date()
        }
        if let b = c["sni_on"] as? Bool {
            sniItem.state = b ? .on : .off
            sniItem.title = b ? "浏览器 SNI 还原(取消分片)" : "浏览器 SNI 分片(可选)"
            lastSniCheck = Date()
        }
        if let b = c["tier_on"] as? Bool {
            tierItem.state = b ? .on : .off
            tierItem.title = b ? "本地下载档位还原(取消接管)" : "本地下载档位(可选)"
            lastTierCheck = Date()
        }
    }

    // 图标 ⚡ 粘滞 30 秒:连接来了又走,图标不再每 5 秒闪来闪去
    func updateIcon() {
        let live = Date().timeIntervalSince(lastLiveAt) < 30
        item.button?.title = live ? "🐙⚡" : "🐙"
    }

    // 探测结果落地。防抖原则:单轮异常不改口(可能是单次超时),连续两轮异常才承认「波动」,
    // 状态行不再在 ms 和「波动中」之间反复横跳
    func absorbLatency(_ r: (ms: Int, via: String)?) {
        if let r = r {
            saveUiCache("latency_ms", r.ms)
            saveUiCache("latency_via", r.via)
            saveUiCache("latency_ts", Date().timeIntervalSince1970)
            if r.via == "github.com" {
                githubDownStreak = 0
                allDownStreak = 0
                lastAllDown = false
                healEpisodeNotified = false   // 好窗口回来了,下一段坏窗口重新允许报一次
                latencyLine.title = "github.com:\(r.ms) ms"
            } else {
                // github.com 被掐但别的域活着;首轮先不动状态行,连续两轮才说「波动」
                githubDownStreak += 1
                lastAllDown = false
                if githubDownStreak >= 2 {
                    latencyLine.title = "github.com 波动中(\(r.via) \(r.ms) ms 可用)"
                }
            }
        } else if hostsOn().on {
            // 已开启还整段不通 = 坏窗口(TLS 全被丢),不是没加速;连续两轮才认
            allDownStreak += 1
            githubDownStreak += 1
            if allDownStreak >= 2 {
                lastAllDown = true
                latencyLine.title = "github.com:链路波动中(通常几分钟内自动恢复)"
            }
        } else {
            allDownStreak = 0
            lastAllDown = false
            latencyLine.title = "github.com:当前直连不通(建议开启加速)"
        }
        logProbeTransition(allDown: lastAllDown)   // 波动时间线(只记翻转)
        autoHealIfNeeded()
        armTimer(lastAllDown ? 30 : 120)   // 坏窗口期 30 秒一测,快点走出去
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
        guard !busy,   // 用户动作/上一轮自愈还在跑就先不叠加(防两个引擎并发写 hosts)
              Date().timeIntervalSince(lastHeal) > 120,
              lastAllDown || githubDownStreak >= 2,
              FileManager.default.fileExists(atPath: ENGINE_BIN) else { return }
        lastHeal = Date()
        beginAction(refreshItem, "⏳ 自动重测中…")
        DispatchQueue.global().async { [weak self] in
            let r = runShell(["/usr/bin/sudo", "-n", ENGINE_BIN, "refresh", "-q"],
                             timeout: 300)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.endAction()
                // 一段坏窗口只报一次,别每 2 分钟弹一条「检测到波动」
                if r.code == 0 && !self.healEpisodeNotified {
                    notify("GitHub 加速", "检测到链路波动,已自动重测并更新最优 IP")
                    self.healEpisodeNotified = true
                }
                self.suggestSsh443IfNeeded()
                self.refreshUI(true)
            }
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
        busy = b
        if b { busyT0 = Date() }
    }

    // 动作开始:正在跑的那一项置灰 + 标 ⏳(看得见在忙,也点不出第二次);
    // 其余菜单项照常可点(全灰像死了),点了会收到「还在跑」提醒
    func beginAction(_ item: NSMenuItem?, _ title: String) {
        setBusy(true)
        busyItem = item
        busyItemTitle = item?.title ?? ""
        item?.isEnabled = false
        item?.title = title
    }

    func endAction() {
        setBusy(false)
        busyItem?.isEnabled = true
        busyItem?.title = busyItemTitle
        busyItem = nil
    }

    func noteBusy() {
        let secs = busyT0.map { Int(Date().timeIntervalSince($0)) } ?? 0
        let what = busyItemTitle.replacingOccurrences(of: "⏳ ", with: "")
        notify("GitHub 加速", "⏳ \(what)还在跑(\(secs) 秒),完成前先别切")
    }

    // ---- 动作 ----

    @objc func toggle(_ s: Any) {
        if busy { noteBusy(); return }
        let (on, _) = hostsOn()
        let act = on ? "off" : "on"
        beginAction(toggleItem, act == "on" ? "⏳ 正在开启…" : "⏳ 正在关闭…")
        statusLine.title = act == "on" ? "⏳ 正在开启(通常几秒,网络差时约 1 分钟)…" : "⏳ 正在关闭…"
        adminEngine([act]) { ok, out in
            self.endAction()
            if ok {
                notify("GitHub 加速", act == "on" ? "已开启 ✅" : "已关闭,已还原 /etc/hosts")
            } else {
                notify("GitHub 加速", "操作失败:\(out.suffix(120))")
            }
            self.refreshUI(true)
        }
    }

    @objc func refreshNow(_ s: Any) {
        if busy { noteBusy(); return }
        beginAction(refreshItem, "⏳ 正在测速…")
        statusLine.title = "⏳ 正在测速(通常几秒)…"
        adminEngine(["refresh"]) { ok, out in
            self.endAction()
            notify("GitHub 加速", ok ? "测速完成,已更新最优 IP ✅" : "测速失败:\(out.suffix(120))")
            self.refreshUI(true)
        }
    }

    @objc func checkMirrors(_ s: Any) {
        if busy { noteBusy(); return }
        beginAction(mirrorsItem, "⏳ 镜像检测中…")
        DispatchQueue.global().async {
            let r = runEngineMirrors()
            DispatchQueue.main.async {
                self.endAction()
                showTextDialog(r.out.isEmpty ? "(无输出)" : String(r.out.suffix(1500)),
                               title: "GitHub 镜像健康检查")
            }
        }
    }

    @objc func downloadClip(_ s: Any) {
        if downloading { notify("GitHub 加速", "已有下载在进行中"); return }
        if busy { noteBusy(); return }   // 别和 on/off/refresh 并发:一个引擎一个 hosts,叠着跑会互相踩
        let clip = NSPasteboard.general.string(forType: .string)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard clip.hasPrefix("http"), clip.lowercased().contains("github") else {
            notify("GitHub 加速", "剪贴板里没有 GitHub 链接"); return
        }
        // 引擎 -o 要完整文件路径
        let name = clip.split(separator: "?")[0].split(separator: "/").last.map(String.init) ?? "download.bin"
        let outPath = NSHomeDirectory() + "/Downloads/" + name
        downloading = true
        beginAction(downloadItem, "⬇️ 下载中…")
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
        endAction()
        notify("GitHub 加速", ok ? "下载完成 → Downloads/\(msg)"
                                : "下载失败(镜像与直连均已尝试,详见 ghspeed mirrors)")
        refreshUI(true)
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
        beginAction(dnsItem, on ? "⏳ 正在还原系统 DNS…" : "⏳ 正在接管系统 DNS…")
        statusLine.title = on ? "⏳ 正在还原系统 DNS…" : "⏳ 正在接管系统 DNS…"
        adminEngine(["dns", on ? "off" : "on"]) { ok, out in
            self.endAction()
            self.lastDnsCheck = .distantPast   // 下次打开菜单重查真实态
            notify("GitHub 加速", ok
                ? (on ? "已还原系统 DNS" : "已接管系统 DNS(阿里/腾讯,备份可随时还原)✅")
                : "DNS 操作失败:\(out.suffix(120))")
            self.refreshUI(true)
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
        beginAction(sniItem, on ? "⏳ 正在还原 SNI 分片…"
                                : "⏳ 正在开启 SNI 分片(首次先实测各策略,约半分钟)…")
        statusLine.title = on ? "⏳ 正在还原 SNI 分片…"
                              : "⏳ 正在开启 SNI 分片(首次先实测各策略,约半分钟)…"
        adminEngine(["sni", on ? "off" : "on"]) { ok, out in
            self.endAction()
            self.lastSniCheck = .distantPast   // 下次打开菜单重查真实态
            notify("GitHub 加速", ok
                ? (on ? "已还原 SNI 分片设置"
                      : "SNI 分片已开启(仅 GitHub 域走本地 relay,其余直连)✅")
                : "SNI 操作失败:\(out.suffix(120))")
            self.refreshUI(true)
        }
    }

    // ---- 本地下载档位(浏览器下载无感走镜像;PAC 由 SNI 分片承载) ----

    func tierOn() -> Bool {
        let r = runShell(engineUserCmd(["tier", "state"]), timeout: 15)
        return r.out.contains("下载档位:已开启")
    }

    @objc func toggleTier(_ s: Any) {
        if busy { noteBusy(); return }
        let on = tierOn()
        if !on && !sniOn() {
            let alert = NSAlert()
            alert.messageText = "开启本地下载档位?"
            alert.informativeText = "档位靠 PAC 把下载域交给本地代理,需要同时开启"
                + "「浏览器 SNI 分片」(仅 GitHub 域分流,其余网站直连)。\n\n"
                + "开启后浏览器下载 release/raw 会被改写到实测最快的镜像通道;"
                + "github.com 网页与登录 Cookie 永不解密。首次会生成并安装本地 CA"
                + "(用户级,关闭档位时自动摘除)。"
            alert.addButton(withTitle: "一起开启")
            alert.addButton(withTitle: "取消")
            if alert.runModal() != .alertFirstButtonReturn { return }
        }
        var args = ["tier", on ? "off" : "on"]
        if !on { args.append("--with-sni") }
        beginAction(tierItem, on ? "⏳ 正在还原下载档位…"
                                 : "⏳ 正在开启下载档位(首次生成 CA)…")
        statusLine.title = on ? "⏳ 正在还原下载档位…" : "⏳ 正在开启下载档位…"
        adminEngine(args) { ok, out in
            self.endAction()
            self.lastTierCheck = .distantPast   // 下次打开菜单重查真实态
            notify("GitHub 加速", ok
                ? (on ? "已还原下载档位(服务已停,CA 已摘除)"
                      : "下载档位已开启:浏览器下载无感走镜像 ✅")
                : "下载档位操作失败:\(out.suffix(140))")
            self.refreshUI(true)
        }
    }

    // ---- 检查更新 / 一键升级(下载走镜像链,校验后打开安装器) ----

    func updateCheck(cache: Bool) -> UpdateInfo? {
        var args = ["update", "--check", "--json"]
        if cache { args.append("--cache") }
        let r = runShell(engineUserCmd(args), timeout: 120)
        guard let line = r.out.split(separator: "\n").last(where: { $0.hasPrefix("{") }),
              let o = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
              let latest = o["latest"] as? String else { return nil }
        let asset = (o["asset"] as? [String: Any])?["name"] as? String ?? ""
        return UpdateInfo(latest: latest, update: (o["update"] as? Bool) ?? false, asset: asset)
    }

    func autoCheckUpdate() {
        guard !busy else { return }
        DispatchQueue.global().async { [weak self] in
            guard let info = self?.updateCheck(cache: true), info.update else { return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.updateItem.title = "有新版 v\(info.latest)(点此更新)"
                if (loadUiCache()["update_seen"] as? String) != info.latest {
                    notify("GitHub 加速",
                           "发现新版 v\(info.latest) —— 菜单栏 🐙 →「有新版」一键升级")
                    saveUiCache("update_seen", info.latest)
                }
            }
        }
    }

    @objc func checkUpdate(_ s: Any) {
        if busy { noteBusy(); return }
        beginAction(updateItem, "⏳ 检查更新中…")
        statusLine.title = "⏳ 正在检查新版…"
        DispatchQueue.global().async { [weak self] in
            let info = self?.updateCheck(cache: false)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.endAction()
                guard let info = info else {
                    notify("GHSpeed", "检查更新失败:网络或 GitHub 暂不可达,稍后再试")
                    self.refreshUI(true)
                    return
                }
                if !info.update {
                    self.updateItem.title = "检查更新…"
                    notify("GHSpeed", "已是最新版 v\(APP_VERSION) ✅")
                    self.refreshUI(true)
                    return
                }
                self.updateItem.title = "有新版 v\(info.latest)(点此更新)"
                let alert = NSAlert()
                alert.messageText = "发现新版 v\(info.latest)"
                alert.informativeText = "当前 v\(APP_VERSION)"
                    + (info.asset.isEmpty ? "" : "\n安装包:\(info.asset)")
                    + "\n\n下载走镜像通道(坏窗口也能下),sha256 校验通过后自动打开安装器。"
                alert.addButton(withTitle: "下载并安装")
                alert.addButton(withTitle: "稍后")
                if alert.runModal() != .alertFirstButtonReturn { return }
                self.beginAction(self.updateItem, "⏳ 下载新版中…")
                self.statusLine.title = "⏳ 正在下载新版(经镜像通道)…"
                DispatchQueue.global().async {
                    let r = runShell(engineUserCmd(["update", "--yes"]), timeout: 900)
                    DispatchQueue.main.async {
                        self.endAction()
                        notify("GHSpeed", r.code == 0
                            ? "新版 v\(info.latest) 已下载,安装器已打开 ✅"
                            : "升级失败:\(r.out.suffix(140))")
                        self.refreshUI(true)
                    }
                }
            }
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
        if sniOn() || tierOn() {   // 生命周期契约:退出即还原(弹一次授权;取消则下次启动自愈兜底)
            let argv = engineUserCmd(["sni", "off"])
            _ = adminShell(argv.joined(separator: " "),
                           prompt: "退出前还原浏览器 SNI 分片 / 下载档位")
        }
        NSApp.terminate(nil)
    }
}

// 用户权限就能跑的引擎调用:已装 CLI 与 App 版本不一致时优先用 App 内引擎,
// 否则新增菜单项(tier/update)会在旧命令上直接报「invalid choice」
func verTuple(_ s: String) -> (Int, Int, Int) {
    guard let re = try? NSRegularExpression(pattern: "(\\d+)\\.(\\d+)\\.(\\d+)") else {
        return (0, 0, 0)
    }
    let ns = s as NSString
    guard let m = re.firstMatch(in: s, range: NSRange(location: 0, length: ns.length)) else {
        return (0, 0, 0)
    }
    return (Int(ns.substring(with: m.range(at: 1))) ?? 0,
            Int(ns.substring(with: m.range(at: 2))) ?? 0,
            Int(ns.substring(with: m.range(at: 3))) ?? 0)
}

func engineUserBase() -> [String] {
    if FileManager.default.fileExists(atPath: ENGINE_BIN) {
        let v = runShell([ENGINE_BIN, "--version"], timeout: 10).out
        // 旧 CLI 不认识新命令 → 用 App 内引擎;CLI 更新则继续用 CLI,别倒退
        if verTuple(v) >= verTuple(APP_VERSION) {
            return [ENGINE_BIN]
        }
    }
    return ["/usr/bin/python3", ENGINE_SCRIPT]
}

func engineUserCmd(_ args: [String]) -> [String] {
    return engineUserBase() + args
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
