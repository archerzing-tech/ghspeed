# GHSpeed 🐙

GitHub 加速工具:测速固定最快 IP + 镜像加速下载,只解决 GitHub 的问题,不碰其他任何网络。

与「抄一份 hosts」的方案不同,这里**所有候选一律先验证再使用**:IP 过 TLS 证书校验
(防污染——社区 hosts 源实测含脏数据,如 fastly 域名指到 Twitter 段),镜像拿真实
release 资产回源实测(防「根路径活着但代理是死的」),测速失败还会给出**病因分类**
(污染 / SNI 识别 / 丢包 / IP 被封),坏窗口和封锁升级都能一眼定位。

| 平台 | 界面 | 最低要求 |
|---|---|---|
| macOS | 菜单栏 🐙 | 10.13+ |
| Windows | 安装包 / 便携版(tkinter 窗口)+ 命令行 | Windows 10+(运行时内置,无需装 Python) |
| Linux(Debian 系) | 命令行(无 UI) | Python 3,systemd |

## 截图

| 菜单栏 | 展开的菜单 | 应用图标 |
|:---:|:---:|:---:|
| ![菜单栏上的小章鱼](docs/menubar.png) | ![点开的菜单](docs/menu.png) | ![应用图标](docs/icon.png) |

**Windows 10+ 简易界面(tkinter,真机截图):**

![Windows 界面](docs/windows-ui.png)

## 它解决什么

1. **加快访问 github** — 并发探测已知 GitHub 网段 + DoH 解析结果,按域名做 TLS 握手测速(证书验证防污染 IP),把最快 IP 写入 hosts 标记块,并装定时任务每 30 分钟自动重测换优
2. **加速 GitHub 下载** — ≥8MB 自动切 8 片、多通道并行(每片独立选镜像/直连,片级断点续传);
   小文件走 ghproxy 系镜像回退链 + 直连兜底,停滞/低速自动换通道。
   镜像池带**跨会话记忆评分**(吞吐 EMA、7 天没成功自动出局),清单定期实测换血
   (公共镜像平均寿命一两年,ghproxy.com / FastGit / kkgithub 等均已死)
3. **打不开变能打开** — 链路在好坏窗口间摆动(TCP 通、TLS 间歇被丢)时,测速器跨窗口多轮重试,好窗口一到就把 IP 钉住
4. **看得见的诊断** — `ghspeed status` 一屏自检全部通道、列出长期不通的域名及替代走法;
   测速失败报病因;`live` 看谁的连接正吃着加速,`activity` 回放自身动作

三平台同一份 Python 引擎(`ghspeed.py`,SNI 分片等非核心特性拆为按需加载的
[可选插件](ghspeed_sni.py)),hosts / 定时刷新 / DNS 缓存 / 提权方式按平台自动适配(launchd / systemd / 计划任务)。

## 安装

从 [Releases](../../releases) 按平台下载对应包:

### macOS 10.13+

**方式一:`.pkg` 安装包(推荐,双击即装)**

1. 双击 `GHSpeed-macos-*.pkg` → 继续 → 安装,输入一次管理员密码
   (自动装好「应用程序」里的 GHSpeed 和命令行 `ghspeed`)
2. 浏览器下载的 pkg 带隔离属性,双击若被 Gatekeeper 拦截,任选其一:
   - 终端执行 `xattr -d com.apple.quarantine ~/Downloads/GHSpeed-macos-*.pkg` 后重新双击
   - 系统设置 → 隐私与安全性 → 点「仍要打开」
   (应用未做 Apple 公证;pkg 装出来的 App 没有隔离属性,可直接打开)
3. 点菜单栏 🐙 → 「开启加速」

**方式二:zip 免安装**

1. 解压 `GHSpeed-macos-*.zip`,把 `GHSpeed.app` 拖进「应用程序」
2. 首次打开:右键点击 → 「打开」(ad-hoc 签名,绕过 Gatekeeper)
3. 点菜单栏 🐙 → 「开启加速」,输入一次管理员密码,完成

只想用命令行的话:

```bash
git clone https://github.com/archerzing-tech/ghspeed.git
cd ghspeed && sudo ./install.sh && sudo ghspeed on
```

### Windows 10+

**方式一:安装包(推荐,双击即装,内置运行时免装 Python)**

1. 下载 `GHSpeed-windows-setup-*.exe` 双击安装(可选桌面快捷方式、加入 PATH)
2. 未做代码签名:SmartScreen 若拦截,点「更多信息」→「仍要运行」
3. 打开 GHSpeed → 「开启加速」→ UAC 弹窗选「是」,完成
4. 关闭窗口=最小化到任务栏托盘 🐙(双击托盘图标回到窗口,右键托盘菜单可退出)
   (卸载前建议以管理员运行 `ghspeed off` 还原 hosts 与计划任务,卸载器结束时会再提醒)

**方式二:便携 zip**

解压 `GHSpeed-windows-*.zip`,双击 `GHSpeed.exe`(运行时已内置,同样免装 Python)。
注意:命令行版是 `ghspeed-cli.exe`(终端里用,功能同 macOS/Linux 的 `ghspeed`);图形界面是 `GHSpeed.exe`。v0.3.1 起两者不同名——此前同目录的 `GHSpeed.exe`/`ghspeed.exe` 会因 NTFS 大小写不敏感互相覆盖,装出来的"图形界面"其实是命令行版,点开只有一个黑窗。

**方式三:源码运行**(已有 Python 3 环境时)

双击 `ghspeed_gui.pyw`,或命令行 `python ghspeed.py on` / `off` / `status` / 双击 `ghspeed.bat`。

### Linux / Debian(命令行,无 UI)

```bash
sudo dpkg -i ghspeed_*_all.deb   # 或 apt install ./ghspeed_*_all.deb
sudo ghspeed on                  # 测速 + 固定 hosts + 装 systemd 定时器
sudo ghspeed off                 # 完全还原
```

依赖仅 `python3`;开启后自动安装 `ghspeed-refresh.timer`(每 30 分钟重测),`off` 时移除。

## 使用

### macOS 菜单栏

点 🐙:

| 菜单项 | 说明 |
|---|---|
| 状态行 | 加速开关状态、固定域名数、github.com 实测延迟(2 分钟自动刷新,失败自动重测自愈) |
| GitHub 活动 | 实时显示哪些进程正连着钉选 IP(= 请求正吃到加速),5 秒一拍;有连接时菜单栏图标亮 ⚡ |
| 最近:… | ghspeed 自身动作日志最后一条(下载走了哪个镜像/通道、自动重测等) |
| 开启加速 / 关闭加速 | 核心开关;关闭完全还原 hosts 与定时任务 |
| 立即测速刷新 | 手动重测一遍最优 IP |
| 镜像检测… | 并发探测各镜像健康度与延迟 |
| ⬇️ 下载剪贴板中的 GitHub 链接 | 复制链接 → 点一下,图标显示 `⬇️ xx%`,完成后弹通知,文件落在「下载」 |
| 开机自启 | 用户级 LaunchAgent,无需管理员 |
| 系统 DNS 接管(可选) | 换成国内不投毒公共 DNS(影响全部网站;原配置自动备份)。菜单项按**系统真实状态**在「接管/还原」间切换,还原永远可用(备份丢了也会清扫回自动获取) |
| 浏览器 SNI 分片(可选) | 把 TLS ClientHello 拆散绕过 SNI 识别,浏览器直连 GitHub 网页;仅 GitHub 域走本地 relay。退出 App 时自动还原设置 |
| 关于 GHSpeed | 显示版本号与项目地址 |

**启动即测**:应用一启动就在后台先跑一轮测速(用户权限,不写 hosts、不弹密码),结果存入种子缓存——之后点「开启加速」或「立即测速刷新」大多直接走种子轮,几秒完成。若当时已开启加速且配好了免密授权,启动测完还会自动把更优 IP 应用上,无需任何点击;检测到链路波动(已开启但不通)也会自动重测自愈。

### Windows 简易界面

双击 `GHSpeed_gui.pyw` 打开(小窗,一屏放下):**开启加速、测速刷新、DNS、SNI、诊断** 五个按钮——镜像状态/活动连接并入「诊断」一次自检,镜像检测与关于在**托盘右键菜单**里。开启加速时弹 UAC 授权,提权后自动把开启做完。**操作不弹确认窗,成功结果直接写在窗口状态行**(只有失败才弹窗)。窗口关闭时最小化到系统托盘(装了 `pystray` 时),右键托盘图标退出;**退出时自动还原 SNI 分片设置**。

### 命令行(三平台通用)

```
ghspeed on | off          开启/关闭(还原 hosts 与定时任务)
ghspeed status            查看固定 IP、延迟、守护状态 + 多级通道自检
ghspeed probe             手动测速(不写 hosts)
ghspeed mirrors [--export 文件]  镜像健康检查(带跨会话记忆评分;--export 把实测池导出为 JSON)
ghspeed url <链接|owner/repo>    把链接展开成直连 + 各镜像前缀的等价下载链接
ghspeed refresh           重测并更新 hosts(root / 管理员)
ghspeed dl <url|owner/repo[@ref]> [-o 路径] [--direct] [--min-speed KB/s] [--proxy http://127.0.0.1:7890]
                          （owner/repo 自动转整仓 zip 经最快镜像下;≥8MB 资产自动切
                          8 片多通道并行,片级断点续传,失败退回单流链)
ghspeed git on | off      git clone 走镜像(push 前记得 off)
ghspeed ssh443 test|on|off   git 改走 ssh.github.com:443 官方备用 SSH 口
ghspeed proxy             探测本机代理端口并给出接管命令(只检测,不接管)
ghspeed dns on|off|show   系统 DNS 换干净公共 DNS(opt-in;off 永远可取消,没备份也能清扫回自动获取)
ghspeed sni test|on|off|state|git   浏览器 SNI 分片(可选):ClientHello 拆散绕过识别,
                          仅 GitHub 域走本地 relay;`sni git on` 让 git clone/fetch
                          也走分片 relay(只写 github.com 域级配置,off 原样还原)
ghspeed live             看当前谁正连着钉选 IP(请求吃到加速的实时视图)
ghspeed activity         回放 ghspeed 自身动作日志(下载通道/刷新/开关)
ghspeed hosts-export [文件]  测速结果导出成 GitHub520 兼容 hosts 文件(SwitchHosts 可直接指向;
                          默认 ~/.ghspeed/hosts,probe/refresh 自动刷新)
ghspeed --version         版本号
```

## 请求可视化(怎么知道"我的请求吃了加速")

诚实边界:hosts 钉选是系统级被动命中(不经过本工具),"命中次数"原理上拿不到;
可视化用两个真实信号——

1. **连接级实时**:谁(哪个进程)正连着钉选 IP,就是正在吃加速的请求。
   macOS 菜单 5 秒一拍(有连接时菜单栏图标亮 ⚡),Windows 界面「活动」按钮 3 秒刷新,
   命令行 `ghspeed live`
2. **动作日志回放**:ghspeed 自身动作(dl 走了哪个镜像/直连/代理及速度、自动重测、
   ssh443/DNS 开关)记录在 `~/.ghspeed/activity.log`,菜单栏「最近」/`ghspeed activity`

## 多级可访问性阶梯(HTTPS 被掐时逐级退守)

「有时候访问不了」通常不是一种病:链路好坏窗口摆动、SNI 阻断、DNS 污染各是各的。
ghspeed 按阶梯逐级兜底,`ghspeed status` 一次看清各级状态:

1. **hosts 钉 IP** —— 主通道。测速选最快 IP 写 hosts,30 分钟自动刷新;
   候选 IP 除 DoH/内置网段外,还会拉**社区 hosts 源**(GitHub520/ineo6,6h 缓存),
   但一律过 TLS+SNI 真实验证才可能被钉上。想用 SwitchHosts 管理的话:
   `ghspeed hosts-export` 输出 GitHub520 同款格式文件(probe/refresh 自动刷新),
   SwitchHosts 指向它即可——IP 比社区源多一道证书验证闸(与 ghspeed 自身
   钉选二选一,别同时开)
2. **动态镜像池** —— 下载/clone 的备胎。发布站(ghproxy.link)+ 已知家族域名做候选,
   拿真实 release 资产逐个验证,**下单顺序按实测吞吐排(256KB 真下载实测,
   低延迟≠宽管道,下载快才算快;同吞吐再看延迟)**;
   静态默认序只是候选来源、不代表最快,免检入池不存在(静态也要过验证)。
   24h 缓存;发布站够不着时沿用旧池不洗空。两款 GUI 启动时后台预热,
   首次下载零冷启动。池子带**跨会话记忆**:每次验证/下载成败都入账
   (吞吐 EMA、累计成败、最近成功时间),排序实测优先、历史次之,
   7 天没成功过的自动出局;`ghspeed mirrors --export` 可把实测通过的池
   导出成带时间戳的 JSON。公共镜像平均寿命一两年(ghproxy.com / FastGit /
   kkgithub / moeyy 前缀均已死),清单 2026-10 实测换血,死了的不留尸位;
   终极自保:花 10 分钟自部署 [hunshcn/gh-proxy](https://github.com/hunshcn/gh-proxy)
   的 Cloudflare Worker 私有前缀,填进 `GHSPEED_MIRRORS` 即不依赖任何公共站
3. **ssh443** —— git 的活路。`ssh.github.com:443` 是 GitHub 官方备用 SSH 口,
   **不走 TLS、没有 SNI 可拦**,HTTPS 被整段丢弃的坏窗口里它通常还活着
   (先 `ghspeed ssh443 test` 验一把,通就 `on`,写入 `~/.ssh/config` 标记块,`off` 还原)
4. **本地代理协作** —— 最后一级。`ghspeed proxy` 探测 Clash/V2Ray 常见端口,
   发现了给出**只针对 GitHub** 的 git/下载接管命令;`ghspeed dl --proxy` 或
   `GHSPEED_PROXY=...` 可把它作为下载链的最后一级兜底
5. **浏览器 SNI 分片**(可选,opt-in)—— 网页侧的 SNI 解法。本地起一个
   127.0.0.1 relay,把 TLS ClientHello **拆成多个 TCP 段**发出去,部分部署下的
   SNI 识别逻辑拼不出来就不拦;`sni test` 先实测四种手法(直连对照/切 1 字节/
   切 SNI 中间/乱序)在你的网络上谁活得久,默认每条连接轮换、不赌单一手法。
   PAC 只把 GitHub 域分流给 relay(其余网站一律 DIRECT,只碰 GitHub 不破),
   PAC 恒带 `; DIRECT` 兜底——relay 死了浏览器自动回落直连,不会断网。
   还原五路收口:显式 off / 退 App / 总 off / DIRECT 兜底 / 启动自愈。
   诚实的边界:只治"识别型"阻断,治不了拥塞丢包;遇到做完整流重组的节点无效,
   效果随时段漂移,所以是 opt-in + 一键还原 + 自动重测换策略

另有一个独立的可选开关:`ghspeed dns on` 把系统 DNS 换成国内不投毒的公共 DNS
(阿里 223.5.5.5 / 腾讯 119.29.29.29), hosts 没钉到的域(如 `ssh.github.com`)
和其他应用都能拿到真实解析;原配置备份在 `~/.ghspeed/dns_backup.json`。
接管状态一律读**系统真实 DNS**(不赌备份文件在哪个家),UI 按钮按真实状态切换;
`ghspeed dns off` 永远可取消——有备份按备份还原,备份丢了就把我们的 DNS 清扫回自动获取,
重复 `dns on` 也不再报错(幂等)。影响全部网站,所以默认不动。

自定义镜像列表:

```bash
GHSPEED_MIRRORS="https://your-mirror.example.com/" ghspeed dl <url>
```

## 工作原理

```
┌─ 平台壳 ────────────────────────────────────┐
│  macOS:GHSpeed.app(菜单栏 🐙)              │
│  Windows:ghspeed_gui.pyw(tkinter 窗口)    │
│  Linux:无(直接用命令行)                    │
└──────────────┬──────────────────────────────┘
               │ 调用(同一份引擎)
┌─ CLI 引擎 ───▼──────────────────────────────┐
│  ghspeed.py(纯标准库 Python 3,三平台通用) │
│  + 可选插件 ghspeed_sni.py(SNI 分片,懒加载)│
│  · DoH + 静态网段候选 IP                     │
│  · 并发 TLS 握手测速(SNI 验证书)            │
│  · 最快 IP → hosts 标记块                    │
│  · 定时重测:launchd / systemd / 计划任务    │
│  · 下载:镜像回退链+断点续传+停滞跳道        │
└─────────────────────────────────────────────┘
```

只动这些 GitHub 域名:`github.com`、`api`、`codeload`、`*.githubusercontent.com` 等 13 个;不改系统代理、不改 DNS 服务器、其他网站一概不处理,`off` 后零残留。

## 已知限制

- codeload 的整仓 zip 是动态流式响应(无 Content-Length),`dl owner/repo` 不走分片并行,单流吃最快镜像;release 资产不受影响(≥8MB 照样 8 片并行)
- `raw.githubusercontent.com` 与 `gist.github.com` 在部分网络受 SNI 级阻断(与 IP 无关),hosts 钉不住浏览器访问——这两个域的资源走镜像下载(`ghspeed dl` / 菜单下载);浏览器直连想试的话,`ghspeed sni on` 的分片 relay 有机会绕过识别型阻断(`sni test` 先看你的网络吃不吃这套)
- 直连链路整段 TLS 被丢时(「坏窗口」,几分钟一变),github.com 可能暂时连不上——这是网络本身在摆动,不是加速失效。refresh 会保留旧钉不回退,macOS 菜单栏检测到波动还会自动重测(免密时);窗口一开就恢复
- 测速失败时会报**病因分类**(证书不匹配=污染 IP / TLS 握手失败=SNI 识别 / 无响应=被丢包 / TCP 不通=IP 被封),`ghspeed probe` 与 status 里可见——审查手段在演进,先能说清是哪种病
- 镜像清单 2026-10 实测换血(死透的移除,只留验证活着的),且带跨会话记忆评分;仍失效时用 `GHSPEED_MIRRORS` 自行替换(自部署 Worker 见上文「动态镜像池」)

## 构建

```bash
./make_pkg.sh        # macOS:dist/GHSpeed-macos-*.pkg + GHSpeed.app(需 Xcode CLT 的 swiftc)
```

Linux `.deb` 与 Windows `.zip` 由 CI(打 `v*` 标签)构建,脚本见 `.github/workflows/release.yml`;Windows 界面为标准库 tkinter,无需额外依赖。

App 自包含 `ghspeed.py` 引擎与可选插件 `ghspeed_sni.py`;三平台共用同一套引擎文件。

## License

[MIT](LICENSE)
