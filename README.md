# GHSpeed 🐙

macOS 专用 GitHub 加速工具:菜单栏一键开关,只解决 GitHub 的问题,不碰其他任何网络。

> **当前仅支持 macOS。** 其他平台暂不考虑。

## 截图

| 菜单栏 | 展开的菜单 | 应用图标 |
|:---:|:---:|:---:|
| ![菜单栏上的小章鱼](docs/menubar.png) | ![点开的菜单](docs/menu.png) | ![应用图标](docs/icon.png) |

## 它解决什么

1. **加快访问 github** — 并发探测已知 GitHub 网段 + DoH 解析结果,按域名做 TLS 握手测速(证书验证防污染 IP),把最快 IP 写入 `/etc/hosts` 标记块,launchd 每 30 分钟自动重测换优
2. **加速 GitHub 下载** — ghproxy 系镜像回退链 + 直连兜底,停滞/低速自动换通道,支持 Range 断点续传
3. **打不开变能打开** — 链路在好坏窗口间摆动(TCP 通、TLS 间歇被丢)时,测速器跨窗口多轮重试,好窗口一到就把 IP 钉住

## 安装

从 [Releases](../../releases) 下载,二选一:

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
- 本机直连链路若整段 TLS 被丢(坏窗口),`on` 会跨窗口重试(3/8 秒间隔,最多 3 轮,长坏窗口交给 30 分钟守护);极端情况下部分域名可能仍钉不上,等 30 分钟守护自动补
- 镜像列表内置 4 个公共 ghproxy 系镜像,失效时用 `GHSPEED_MIRRORS` 自行替换

## 构建

```bash
./make_pkg.sh        # 产出 dist/GHSpeed-macos-*.pkg + GHSpeed.app(需 Xcode CLT 的 swiftc)
```

App 自包含 `ghspeed.py` 引擎;打 `v*` 标签推送后 CI 自动构建发布。

## License

[MIT](LICENSE)
