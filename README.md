# GHSpeed 🐙

macOS 专用 GitHub 加速工具:菜单栏一键开关,只解决 GitHub 的问题,不碰其他任何网络。

> **当前仅支持 macOS。** 其他版本(0.0.1)暂不考虑。

## 它解决什么

1. **加快访问 github** — 并发探测已知 GitHub 网段 + DoH 解析结果,按域名做 TLS 握手测速(证书验证防污染 IP),把最快 IP 写入 `/etc/hosts` 标记块,launchd 每 30 分钟自动重测换优
2. **加速 GitHub 下载** — ghproxy 系镜像回退链 + 直连兜底,停滞/低速自动换通道,支持 Range 断点续传
3. **打不开变能打开** — 链路在好坏窗口间摆动(TCP 通、TLS 间歇被丢)时,测速器跨窗口多轮重试,好窗口一到就把 IP 钉住

## 安装

从 [Releases](../../releases) 下载 `GHSpeed-macos-*.zip`:

1. 解压,把 `GHSpeed.app` 拖进「应用程序」
2. 首次打开:右键点击 → 「打开」(ad-hoc 签名,绕过 Gatekeeper)
3. 点菜单栏 🐙 → 「开启加速」,输入一次管理员密码,完成

只想用命令行的话:

```bash
git clone https://github.com/archerzing-tech/ghspeed.git
cd ghspeed && sudo ./install.sh && sudo ghspeed on
```

## 使用

### 菜单栏(推荐)

点 🐙:

| 菜单项 | 说明 |
|---|---|
| 状态行 | 加速开关状态、固定域名数、github.com 实测延迟(5 分钟自动刷新) |
| 开启加速 / 关闭加速 | 核心开关;关闭完全还原 `/etc/hosts` 与定时任务 |
| 立即测速刷新 | 手动重测一遍最优 IP |
| 镜像检测… | 并发探测各镜像健康度与延迟 |
| ⬇️ 下载剪贴板中的 GitHub 链接 | 复制链接 → 点一下,图标显示 `⬇️ xx%`,完成后弹通知,文件落在「下载」 |
| 开机自启 | 用户级 LaunchAgent,无需管理员 |

### 命令行

```
ghspeed on | off          开启/关闭(还原 hosts 与 launchd 守护)
ghspeed status            查看固定 IP、延迟、守护状态
ghspeed probe             手动测速(不写 hosts)
ghspeed mirrors           镜像健康检查
ghspeed refresh           重测并更新 hosts(root)
ghspeed dl <url> [-o 路径] [--direct] [--min-speed KB/s]
ghspeed git on | off      git clone 走镜像(push 前记得 off)
```

自定义镜像列表:

```bash
GHSPEED_MIRRORS="https://your-mirror.example.com/" ghspeed dl <url>
```

## 工作原理

```
┌─ GUI 壳 ─────────────────────────────┐
│  GHSpeed.app (Swift NSStatusItem)     │
└──────────────┬───────────────────────┘
               │ 调用
┌─ CLI 引擎 ───▼───────────────────────┐
│  ghspeed.py (纯标准库 Python 3)       │
│  · DoH + 静态网段候选 IP              │
│  · 并发 TLS 握手测速(SNI 验证书)     │
│  · 最快 IP → /etc/hosts 标记块        │
│  · launchd 每 30 分钟自动重测         │
│  · 下载:镜像回退链+断点续传+停滞跳道  │
└──────────────────────────────────────┘
```

只动这些 GitHub 域名:`github.com`、`api`、`codeload`、`*.githubusercontent.com` 等 13 个;不改系统代理、不改 DNS 服务器、其他网站一概不处理,`off` 后零残留。

## 已知限制

- `raw.githubusercontent.com` 与 `gist.github.com` 在部分网络受 SNI 级阻断(与 IP 无关),hosts 钉不住浏览器访问——这两个域的资源走镜像下载(`ghspeed dl` / 菜单下载)即可
- 本机直连链路若整段 TLS 被丢(坏窗口),`on` 会跨窗口重试(3/10/25 秒间隔,最多 4 轮);极端情况下部分域名可能仍钉不上,等 30 分钟守护自动补
- 镜像列表内置 4 个公共 ghproxy 系镜像,失效时用 `GHSPEED_MIRRORS` 自行替换

## 构建

```bash
./build_app.sh        # 需要 Xcode Command Line Tools(swiftc)
```

产物在 `dist/GHSpeed.app`,自包含 `ghspeed.py` 引擎。

## License

[MIT](LICENSE)
