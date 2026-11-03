#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ghspeed —— GitHub 专用加速工具(macOS)

参考 FastGithub(测速选 IP + hosts 固定)、SwitchHosts(hosts 管理)、
ghproxy 系镜像(下载加速)的思路,做三件事:

  1. 并发测速:对 GitHub 已知 IP 段 + DoH 实时解析出的 IP 做 TLS 握手测速,
     证书按域名校验 —— 被 DNS 污染的假 IP 握手必失败,自动淘汰
  2. 把每个 GitHub 域名固定到最快可用 IP(写入 /etc/hosts)
     —— 解决"打不开 / 打开慢"
  3. launchd 定时任务每 30 分钟自动重测刷新;`dl` 子命令走镜像加速
     release / raw / archive 大文件下载,支持断点续传

严格只处理 GitHub 相关域名:不设系统代理、不改 DNS 服务器、
hosts 里只增加带标记的一小块,`ghspeed off` 可完整还原。
其他任何网络访问不受影响。
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
import urllib.request
from urllib.parse import urlparse, unquote

# ---------------- 常量 ----------------

import platform as _platform

IS_MAC = _platform.system() == "Darwin"
IS_WIN = _platform.system() == "Windows"
IS_LINUX = not IS_MAC and not IS_WIN

if IS_WIN:
    import ctypes

    def is_admin():
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    HOSTS_FILE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "drivers", "etc", "hosts")
    BACKUP_FILE = HOSTS_FILE + ".ghspeed.bak"
    STATE_DIR = (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                              "ghspeed") if is_admin()
                 else os.path.join(os.path.expanduser("~"), ".ghspeed"))
else:
    def is_admin():
        return os.geteuid() == 0
    HOSTS_FILE = "/etc/hosts"
    BACKUP_FILE = "/etc/hosts.ghspeed.bak"
    if os.geteuid() == 0:
        STATE_DIR = ("/Library/Application Support/ghspeed" if IS_MAC
                     else "/var/lib/ghspeed")
    else:
        STATE_DIR = os.path.expanduser("~/.ghspeed")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Windows 的 GUI 进程没有控制台:不显式给 CREATE_NO_WINDOW,拉起来的每个
# 控制台子程序(schtasks/powershell/netstat/ipconfig…)都会闪一个黑窗。
# 这里统一收口,顺带给所有外部命令兜上超时——界面绝不能被一个卡住的子进程吊死。
_CREATE_NO_WINDOW = 0x08000000


def _run(cmd, timeout=20, **kw):
    """外部命令统一入口:Windows 不弹黑窗;text 模式容错解码(中文系统 GBK)"""
    if IS_WIN:
        kw.setdefault("creationflags", _CREATE_NO_WINDOW)
    if kw.get("text"):
        kw.setdefault("errors", "replace")
    if "input" not in kw:
        kw.setdefault("stdin", subprocess.DEVNULL)   # 绝不让子程序等键盘输入
    return subprocess.run(cmd, timeout=timeout, **kw)


def _popen(cmd, **kw):
    """后台常驻子进程统一入口:Windows 不弹黑窗"""
    if IS_WIN:
        kw.setdefault("creationflags", _CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, **kw)


MARK_S = "# >>> ghspeed start >>>"
MARK_E = "# <<< ghspeed end <<<"

PLIST_LABEL = "com.ghspeed.refresh"
PLIST_PATH = "/Library/LaunchDaemons/%s.plist" % PLIST_LABEL
SYSTEMD_SERVICE = "/etc/systemd/system/ghspeed-refresh.service"
SYSTEMD_TIMER = "/etc/systemd/system/ghspeed-refresh.timer"
WIN_TASK = "GHSpeed-Refresh"
LOG_FILE = "/var/log/ghspeed.log"
REFRESH_INTERVAL = 1800  # 秒

# 版本:优先读仓库旁的 VERSION 文件,安装后的散装副本用内置值兜底
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "VERSION")) as _f:
        __version__ = _f.read().strip() or "0.6.1"
except Exception:
    __version__ = "0.6.1"

# 只处理这些 GitHub 域名,一个都不多
DOMAINS = [
    "github.com",                          # 主站
    "api.github.com",                      # API
    "codeload.github.com",                 # 源码包(zip/tar)下载
    "gist.github.com",
    "raw.githubusercontent.com",           # raw 文件
    "gist.githubusercontent.com",
    "camo.githubusercontent.com",          # README 图片代理
    "avatars.githubusercontent.com",       # 头像
    "desktop.githubusercontent.com",
    "favicons.githubusercontent.com",
    "objects.githubusercontent.com",       # release 资产
    "release-assets.githubusercontent.com",
    "github-cloud.githubusercontent.com",
    # ---- 页面完整性(缺了页面半瘫/图片裂图,2026-11 补齐) ----
    "github.githubassets.com",             # 页面全部 JS/CSS 静态资源
    "user-images.githubusercontent.com",   # issue/README 用户图片
    "private-user-images.githubusercontent.com",  # 私有仓库图片(签名 URL)
    "collector.github.com",                # 遥测埋点(被掐会拖慢页面加载)
    # ---- 上传与 LFS ----
    "uploads.github.com",                  # release 资产上传 / API 附件(POST)
    "media.githubusercontent.com",         # git-lfs / 媒体内容重定向
]

# DoH(国内可达、解析 GitHub 不投毒的公共 DNS),失败自动跳过
DOH_PROVIDERS = [
    "https://dns.alidns.com/resolve?name={d}&type=A",
    "https://223.5.5.5/resolve?name={d}&type=A",
    "https://120.53.53.53/dns-query?name={d}&type=A",
]

# 下载镜像(ghproxy 系)静态种子:冷启动/发布站也够不着时的保底。
# 2026-10-29 全量实测换血——ghproxy 公共镜像生态平均寿命只有一两年
# (ghproxy.com / FastGit / kkgithub / moeyy 前缀 / gitclone.com 均已死),
# 清单只留当时真验证过的;死透的不留尸位,省掉冷启动每轮的无谓探测:
#   gh.llkk.cc(DNS 污染)、ghp.ci、ghproxy.cc、gh-proxy.net、
#   ghproxy.homeboyc.cn、cdn.gh-proxy.com、gh.ddlc.top(验证全败)
# 这里只是候选来源,入池仍逐个真验证——死了进不来,复活自然回来。
MIRRORS = [
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://ghfast.top/",
    "https://ghproxy.cn/",
    "https://cors.isteed.cc/",
]

# jsDelivr 官方三边界(cdn / fastly / gcore):gh 文件(raw)的 CDN 直读通道。
# 只适用公共仓库、单文件 ≤20MB;两边不适用就 404,dl 通道级失败自动跳过,
# 所以不验也有它——它是「多条回家的路」,不是「必须活的镜」。
JSDELIVR_HOSTS = ["cdn.jsdelivr.net", "fastly.jsdelivr.net", "gcore.jsdelivr.net"]

_RAW_URL_RE = re.compile(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+?)/([^/]+?)/(.+)")


def _is_raw_url(u):
    return bool(_RAW_URL_RE.match(u))


def _raw_jsdelivr(host, url):
    """raw 链接 → jsDelivr 链接(https://<host>/gh/<u>/<r>@<ref>/<path>);
    不是 raw 链接返回 None(通道自动退化为直连,不会错下)"""
    m = _RAW_URL_RE.match(url)
    if not m:
        return None
    o, r, ref, path = m.groups()
    return "https://%s/gh/%s/%s@%s/%s" % (host, o, r, ref, path)

# ---------------- 动态节点池(节点必须能定期更新,不能是死的) ----------------
# 来源一律只当"候选":IP 要过 TLS+SNI 真实探测,镜像要能真实回源 release
# 资产,验证不过的(无论静态还是动态)都不进推荐链。缓存按用户放
# ~/.ghspeed/dyn.json,带 TTL;拉不到源就沿用旧池,永不因源挂而变残。

USER_DYN_FILE = os.path.join(os.path.expanduser("~"), ".ghspeed", "dyn.json")

# 镜像发布站:正文带当前可用镜像域名(页面改版也不怕,候选一律过验证)
MIRROR_FEED_SOURCES = ["https://ghproxy.link/"]
# 发布站页面是 JS 渲染的,静态抓只能看到零星几个,再把已知家族域名直接
# 列为种子候选;入池前同样逐个验证,死了的自然进不来
MIRROR_EXTRA_CANDIDATES = [
    # gh.b52m.cn:2026-10-29 境外探测存活的小站(稳定性未知,验证说了算)
    "https://gh.b52m.cn/",
]
MIRROR_FEED_TTL = 86400      # 镜像池 24h 重拉

# 社区 hosts 源:人工维护的新鲜 IP,作测速候选(GitHub520 官方分发 + ineo6 双路)
HOSTS_FEED_SOURCES = [
    "https://raw.hellogithub.com/hosts",
    "https://cdn.jsdelivr.net/gh/ineo6/hosts/hosts",
    "https://gitlab.com/ineo6/hosts/-/raw/master/hosts",
]
IP_FEED_TTL = 21600          # hosts 源 6h 重拉

# 本地代理常见端口(Clash/Clash Verge/V2RayN/SSR…)——只检测并给出接管命令
PROXY_PORTS = [7890, 7897, 7891, 1080, 2080, 8080, 8118, 10808, 10809, 8889]

GITHUB_URL_RE = re.compile(
    r"^https?://([\w.-]*\.)?"
    r"(github\.com|githubusercontent\.com|githubassets\.com|github\.io)/",
    re.I)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) ghspeed/1.0"

# ---------------- 自更新 ----------------
# 检查:API 直连 → releases/latest 重定向(直连 + 镜像)→ 资产页解析;
# 下载:复用 dl 的镜像链 + 分片 + 断点续传;校验:API digest 或 SHA256SUMS.txt;
# 安装:只负责打开系统安装包,不静默替换——用户始终看得见发生了什么。
UPDATE_REPO = "archerzing-tech/ghspeed"
UPDATE_API = "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO
UPDATE_WEB = "https://github.com/%s/releases" % UPDATE_REPO
UPDATE_CHECK_FILE = os.path.join(os.path.expanduser("~"), ".ghspeed", "update.json")
UPDATE_CACHE_TTL = 6 * 3600

# ---------------- 小工具 ----------------


def need_root():
    if not is_admin():
        if IS_WIN:
            sys.exit("需要管理员权限:请在「以管理员身份运行」的终端里执行:  "
                     "python ghspeed.py %s"
                     % (sys.argv[1] if len(sys.argv) > 1 else "on"))
        sys.exit("需要 root 权限,请用 sudo 运行,例如:  sudo ghspeed %s"
                 % (sys.argv[1] if len(sys.argv) > 1 else "on"))


def human(n):
    return "%.1f MB" % (n / 1e6) if n >= 1e6 else "%.1f KB" % (n / 1e3)


def _cert_cands():
    """证书包候选位:certifi(谁装的都行) + 各家系统证书包路径。
    Windows 的 pip --user 装到 %APPDATA%\\Python\\PythonXY\\site-packages 也要找。"""
    cands = []
    try:
        import certifi
        cands.append(certifi.where())
    except Exception:
        pass
    if IS_WIN:
        ap, vi = os.environ.get("APPDATA", ""), sys.version_info
        cands += [
            os.path.join(os.path.dirname(sys.executable), "Lib", "site-packages",
                         "certifi", "cacert.pem"),
            os.path.join(ap, "Python", "Python%d%d" % (vi[0], vi[1]),
                         "site-packages", "certifi", "cacert.pem"),
        ]
    cands += ["/etc/ssl/cert.pem",                       # macOS 系统证书包
              "/usr/local/etc/openssl@3/cert.pem",
              "/usr/local/etc/openssl/cert.pem",
              "/opt/homebrew/etc/ca-certificates/cert.pem"]
    return cands


_CERT_INSTALL_TRIED = False   # 每进程最多现场装一次


def _pip_install_certifi():
    """现场补救:pip --user 装 certifi(静默,成功与否都不打断调用方)。
    python.org 官方包/venv 是重灾区;系统 python 有 OS 证书链,一般轮不到这里。"""
    global _CERT_INSTALL_TRIED
    if _CERT_INSTALL_TRIED:
        return False
    _CERT_INSTALL_TRIED = True
    try:
        r = _run([sys.executable, "-m", "pip", "install", "--user", "--quiet",
                  "certifi"], timeout=180, check=False, capture_output=True)
        ok = r.returncode == 0
        journal("certifi", "pip --user 安装%s" % ("成功" if ok else "失败"))
        return ok
    except Exception as e:
        journal("certifi", "pip --user 安装失败:%s" % str(e)[:60])
        return False


def ssl_context():
    """TLS 客户端上下文:显式指定证书包(不少 python.org/venv 装机找不到根证书,
    CERTIFICATE_VERIFY_FAILED 全家黑);一个候选都没有时现场 pip --user 装 certifi。
    不改变信任语义,只是把「该有的根证书」给配齐。"""
    for ca in _cert_cands():
        if os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    if _pip_install_certifi():   # 装完当场再试一次
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
    return ssl.create_default_context()


def save_state(state, dead=None):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"time": time.time(), "map": state, "dead": dead or {}},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def load_seeds():
    """种子缓存:当前身份的 state;root 身份下再回读发起 sudo 的用户缓存,取较新者。

    GUI 启动即测那一轮以用户权限跑,结果落在 ~/.ghspeed/state.json;
    之后点「开启加速」引擎以 root 运行,只读 /Library 下的 state ——
    不回读的话启动那轮的成果就浪费了。sudo 把发起者放在 SUDO_USER。
    """
    cands = []
    st = load_state()
    if st:
        cands.append(st)
    if is_admin() and os.environ.get("SUDO_USER", "root") != "root":
        try:
            import pwd
            home = pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir
            with open(os.path.join(home, ".ghspeed", "state.json")) as f:
                cands.append(json.load(f))
        except Exception:
            pass
    if not cands:
        return {}
    return max(cands, key=lambda d: d.get("time", 0))


# ---------------- 动态池:拉取 / 验证 / 缓存 ----------------


def dyn_load():
    try:
        with open(USER_DYN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def dyn_save(d):
    try:
        os.makedirs(os.path.dirname(USER_DYN_FILE), exist_ok=True)
        tmp = USER_DYN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, USER_DYN_FILE)
    except Exception:
        pass


def fetch_url(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return r.read(1 << 20).decode("utf-8", "replace")


# ---------------- 活动可视化:连接级实时 + 自身动作日志 ----------------
# hosts 钉选是被动的(OS 直接命中,不经过本工具),"命中次数"原理上拿不到;
# 可视化用两个真实信号:①谁的连接正连着钉选 IP(lsof/netstat 求交集),
# ②ghspeed 自身动作(dl 走了哪个通道、refresh/on/off/ssh443/dns)落日志回放。

ACTIVITY_FILE = os.path.join(os.path.expanduser("~"), ".ghspeed", "activity.log")


def journal(action, detail=""):
    """自身动作日志;root 同时落到系统态目录,读取时两边合并"""
    entry = json.dumps({"t": round(time.time(), 1), "action": action,
                        "detail": detail}, ensure_ascii=False)
    for path in (ACTIVITY_FILE,):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass
    if is_admin() and STATE_DIR != os.path.dirname(ACTIVITY_FILE):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "activity.log"), "a",
                      encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass


def journal_tail(n=20):
    """合并读取(用户文件 + 系统'目录,若有权限),返回最近 n 条 dict"""
    seen, out = set(), []
    for path in (os.path.join(STATE_DIR, "activity.log"), ACTIVITY_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and line not in seen:
                        seen.add(line)
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
    out.sort(key=lambda d: d.get("t", 0))
    return out[-n:]


def pinned_ips():
    """当前钉选 IP 集合(hosts 块 + 上次测速缓存)"""
    ips = set()
    try:
        ips.update(current_map_from_hosts().values())
    except Exception:
        pass
    st = load_state()
    if st:
        for v in st.get("map", {}).values():
            ips.add(v.get("ip", ""))
    ips.discard("")
    return ips


def live_connections():
    """[(进程名, 连接数)] —— 正连着钉选 IP 的 ESTABLISHED 连接"""
    ips = pinned_ips()
    if not ips:
        return []
    agg = {}
    try:
        if IS_WIN:
            r = _run(["netstat", "-ano", "-p", "TCP"], timeout=10,
                     capture_output=True, text=True)
            pids = {}
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "ESTABLISHED":
                    ip = parts[2].rsplit(":", 1)[0].strip("[]")
                    if ip in ips:
                        pids[parts[4]] = pids.get(parts[4], 0) + 1
            if pids:
                r2 = _run(["tasklist", "/FO", "CSV", "/NH"], timeout=10,
                          capture_output=True, text=True)
                for line in r2.stdout.splitlines():
                    cells = [c.strip().strip('"') for c in line.split('","')]
                    if len(cells) >= 2 and cells[1] in pids:
                        agg[cells[0]] = agg.get(cells[0], 0) + pids.pop(cells[1])
            for pid, n in pids.items():   # 查不到名字的留 PID
                agg["PID %s" % pid] = agg.get("PID %s" % pid, 0) + n
        else:
            r = _run(["lsof", "-i", "TCP", "-n", "-P", "+c", "0"], timeout=10,
                     capture_output=True, text=True)
            for line in r.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 9 and "->" in parts[8] \
                        and "(ESTABLISHED)" in line:   # 状态在 NAME 之后的独立列
                    remote = parts[8].split("->")[-1]
                    ip = remote.rsplit(":", 1)[0].strip("[]")
                    if ip in ips:
                        agg[parts[0]] = agg.get(parts[0], 0) + 1
    except Exception:
        return []
    return sorted(agg.items(), key=lambda kv: -kv[1])


def validate_mirror(prefix, timeout=8):
    """拿真实 release 资产过一遍镜像,HTTP 200 返回耗时,否则 None"""
    try:
        req = urllib.request.Request(prefix + MIRROR_TEST_URL,
                                     headers={"User-Agent": UA}, method="HEAD")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
            r.read(0)
            if r.getcode() != 200:
                return None
        return time.perf_counter() - t0
    except Exception:
        return None


def _mirror_speed(prefix, timeout=3.5, cap=262144):
    """Range GET 真实资产前 256KB → B/s;限时定量(慢镜像采到哪算哪),
    绝不吊住整轮验证。0 = 没测出(窄管道/不支持 Range),排序时自然沉底"""
    try:
        req = urllib.request.Request(prefix + MIRROR_TEST_URL, headers={
            "User-Agent": UA, "Range": "bytes=0-%d" % (cap - 1)})
        t0 = time.perf_counter()
        got = 0
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
            while got < cap and time.perf_counter() - t0 <= timeout:
                b = r.read(65536)
                if not b:
                    break
                got += len(b)
        dt = time.perf_counter() - t0
        return got / dt if got >= 65536 and dt > 0 else 0.0
    except Exception:
        return 0.0


def _pool_urls(entries):
    """dyn["mirrors"] 兼容两代形态:旧 ["url"] / 新 [[url, rtt, mbps]](吞吐优先序)→ 出 url 列表"""
    out = []
    for e in entries:
        u = e[0] if isinstance(e, (list, tuple)) else e
        if u not in out:
            out.append(u)
    return out


# 镜像不是验证一次就一劳永逸:公共镜像平均寿命一两年,上周的冠军这周可能
# 就死了。评分表(存 dyn.json)把每次真实交互——池子验证、单流下载成败——
# 记成跨会话的记忆:吞吐 EMA、累计成败、最近成功时间。7 天没成功过的
# 自动退出下单链(记录保留,复活验证通过自然回来)。
MIRROR_SCORE_EVICTION = 7 * 86400


def mirror_record_batch(results):
    """把一批 (镜像, 成败, 吞吐B/s) 记进评分表;一次落盘,别每条一写。"""
    if not results:
        return
    dyn = dyn_load()
    st = dyn.get("mirror_stats") or {}
    now = time.time()
    for m, ok, mbps in results:
        s = st.get(m) or {"ema": 0, "ok": 0, "fail": 0, "last_ok": 0}
        if ok:
            if mbps:   # 有吞吐才更新 EMA(HEAD 级成功也算活,但不冲淡历史吞吐)
                s["ema"] = int(mbps) if not s["ema"] \
                    else int(0.6 * mbps + 0.4 * s["ema"])
            s["ok"] = s.get("ok", 0) + 1
            s["fail"] = 0
            s["last_ok"] = now
        else:
            s["fail"] = s.get("fail", 0) + 1
        st[m] = s
    dyn["mirror_stats"] = st
    dyn_save(dyn)


def _pool_rank(alive, stats):
    """下单链排序:本次实测吞吐优先,历史 EMA 次之(冷启动时的记忆权重),
    延迟殿后;7 天没成功过的剔除(验证这轮本来就过不了才会走到这)。"""
    now = time.time()

    def key(e):
        p, rtt = e[0], e[1]
        mbps = e[2] if len(e) > 2 else 0.0
        s = stats.get(p) or {}
        return (-mbps, -(s.get("ema") or 0), rtt if rtt is not None else 9e9)

    out = [e for e in alive
           if not ((stats.get(e[0]) or {}).get("last_ok")
                   and now - stats[e[0]]["last_ok"] > MIRROR_SCORE_EVICTION)]
    return sorted(out, key=key)


# 自建镜像:用户自己的 gh-proxy Worker/VPS,~/.ghspeed/mirrors.json 持久。
# 公共镜像平均寿命一两年,自建才是不死的;同样只在验证通过时进推荐链。
CUSTOM_MIRRORS_FILE = os.path.join(os.path.expanduser("~"), ".ghspeed", "mirrors.json")


def custom_mirrors():
    try:
        with open(CUSTOM_MIRRORS_FILE) as f:
            xs = json.load(f).get("custom") or []
        return [str(x).strip().rstrip("/") + "/" for x in xs if str(x).strip()]
    except Exception:
        return []


def _custom_mirrors_save(xs):
    os.makedirs(os.path.dirname(CUSTOM_MIRRORS_FILE), exist_ok=True)
    tmp = CUSTOM_MIRRORS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"custom": xs, "time": time.time()}, f)
    os.replace(tmp, CUSTOM_MIRRORS_FILE)


def mirror_pool(force=False):
    """镜像下单链:静态种子 + 发布站动态候选,**一律真验证、按实测吞吐排序**。
    静态默认序只是候选来源,不代表最快——低延迟≠宽管道,下载快(吞吐)才算快,
    延迟只做同吞吐时的次序。24h 缓存(连吞吐/延迟一起存);
    源够不着/坏窗口全验不过时沿用旧池,永不洗空。"""
    env = os.environ.get("GHSPEED_MIRRORS")
    if env:   # 用户显式指定时完全尊重用户,不掺动态
        return [m.strip().rstrip("/") + "/" for m in env.split(",") if m.strip()]
    dyn = dyn_load()
    cached = dyn.get("mirrors") or []
    now = time.time()
    if not force and cached and now - dyn.get("mirrors_ts", 0) < MIRROR_FEED_TTL:
        return _pool_urls(cached)   # 缓存里存的就是延迟序

    cands, seen = [], set()
    for c in list(MIRRORS) + list(MIRROR_EXTRA_CANDIDATES) + custom_mirrors():
        # 自建镜像们排进候选,同样真验证 —— 活的才进推荐链
        if c not in seen:
            seen.add(c)
            cands.append(c)
    for src in _env_csv("GHSPEED_MIRROR_FEEDS", MIRROR_FEED_SOURCES):
        try:
            html = fetch_url(src, timeout=8)
        except Exception:
            continue   # 发布站死了不打紧:静态+种子候选照常验证
        for host in re.findall(r"https?://([a-zA-Z0-9.-]+)", html):
            host = host.lower().rstrip(".")
            if (re.search(r"gh|proxy|mirror", host)
                    and not host.endswith(("github.com", "github.io",
                                           "githubusercontent.com",
                                           "githubassets.com", "ghproxy.link"))):
                u = "https://%s/" % host
                if u not in seen:
                    seen.add(u)
                    cands.append(u)
    def _probe(p):   # 活性(HEAD) + 吞吐(256KB 实测)一次采齐
        rtt = validate_mirror(p)
        return (p, rtt, _mirror_speed(p) if rtt is not None else 0.0)
    alive, seen_r = [], []
    with cf.ThreadPoolExecutor(10) as ex:
        for p, rtt, sp in ex.map(_probe, cands[:28]):
            seen_r.append((p, rtt is not None, sp))   # 成败都入账:失败也是记忆
            if rtt is not None:
                alive.append((p, rtt, sp))
    mirror_record_batch(seen_r)
    alive = _pool_rank(alive, dyn.get("mirror_stats") or {})
    # ★ 动态排序:吞吐优先(下载快才是真快),历史 EMA 次之,延迟殿后
    if alive:
        dyn["mirrors"], dyn["mirrors_ts"] = alive, now
        dyn_save(dyn)
    return _pool_urls(alive or cached)   # 全验不过=疑似坏窗口,沿用旧池不洗空


def ip_feed_candidates(force=False):
    """社区 hosts 源 → {domain: [ip,...]};6h 缓存,拉不到就吃缓存/返回空"""
    dyn = dyn_load()
    feeds = dyn.get("feed_ips") or {}
    now = time.time()
    if not force and feeds and now - dyn.get("ips_ts", 0) < IP_FEED_TTL:
        return feeds
    got = {}

    def parse(text):
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split()
            if len(parts) >= 2 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
                ip, host = parts[0], parts[1].lower()
                if host in DOMAINS:
                    got.setdefault(host, [])
                    if ip not in got[host]:
                        got[host].append(ip)

    def grab(src):
        try:
            parse(fetch_url(src, timeout=8))
        except Exception:
            pass

    with cf.ThreadPoolExecutor(3) as ex:
        list(ex.map(grab, HOSTS_FEED_SOURCES))
    if got:
        dyn["feed_ips"], dyn["ips_ts"] = got, now
        dyn_save(dyn)
        return got
    return feeds


def _env_csv(name, default):
    v = os.environ.get(name)
    return [x.strip() for x in v.split(",") if x.strip()] if v else default


# ---------------- 测速 ----------------


def static_candidates(domain):
    """内置候选 IP(来自 GitHub 官方公布的网段,历史稳定值)"""
    ips = set()
    if domain in ("github.com", "gist.github.com", "uploads.github.com"):
        ips |= {"140.82.%d.%d" % (n, h) for n in range(112, 122) for h in (3, 4)}
        ips |= {"20.205.243.166", "20.27.177.113", "20.200.245.247",
                "20.26.156.215", "20.248.137.49"}
    elif domain == "api.github.com":
        ips |= {"140.82.%d.%d" % (n, h) for n in range(112, 122) for h in (5, 6)}
    elif domain == "codeload.github.com":
        ips |= {"140.82.%d.%d" % (n, h) for n in range(112, 122) for h in (9, 10)}
    elif domain == "collector.github.com":
        # 埋点域,与主站同段(Fastly/own CIDR 混部);错了验证会淘汰,无害
        ips |= {"140.82.%d.%d" % (n, h) for n in range(112, 122) for h in (3, 4)}
    elif domain.endswith("githubusercontent.com"):
        ips |= {"185.199.%d.133" % n for n in (108, 109, 110, 111)}
    elif domain.endswith("githubassets.com"):
        # Fastly 的 pages 同段(.153);错了验证会淘汰,无害
        ips |= {"185.199.%d.153" % n for n in (108, 109, 110, 111)}
    return ips


def doh_lookup(domain, timeout=5):
    """通过 DoH 拿最新的 A 记录;全部失败则返回空列表"""
    ips = []
    for tpl in DOH_PROVIDERS:
        try:
            req = urllib.request.Request(
                tpl.format(d=domain), headers={"accept": "application/dns-json",
                                               "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl_context()) as r:
                data = json.loads(r.read().decode())
            for a in data.get("Answer", []):
                ip = str(a.get("data", ""))
                if a.get("type") == 1 and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    ips.append(ip)
        except Exception:
            continue
    return ips


def probe(domain, ip, timeout=4.0):
    """对 ip:443 完整走一遍 HTTPS(SNI/证书按 domain 校验)。

    证书校验通过 + 返回 HTTP 响应 => 该 IP 真的在服务这个 GitHub 域名。
    污染 IP / 不可达 IP 会在这一步直接抛异常。返回耗时(秒)。
    """
    ctx = ssl_context()
    t0 = time.perf_counter()
    with socket.create_connection((ip, 443), timeout=timeout) as sock:
        sock.settimeout(timeout)
        with ctx.wrap_socket(sock, server_hostname=domain) as tls:
            tls.sendall(("HEAD / HTTP/1.1\r\nHost: %s\r\n"
                         "User-Agent: %s\r\nConnection: close\r\n\r\n"
                         % (domain, UA)).encode())
            head = tls.recv(128)
    if head[:5] == b"HTTP/":
        return time.perf_counter() - t0
    raise ConnectionError("非 HTTP 响应")


def candidates_for(domain, cap=18):
    out = []

    def add(xs):
        for x in xs:
            if x not in out:
                out.append(x)

    add(doh_lookup(domain))                  # 实时解析优先(新鲜)
    feeds = ip_feed_candidates().get(domain)  # 社区 hosts 源(人工维护的活 IP)
    if feeds:
        add(feeds)
    add(sorted(static_candidates(domain)))   # 内置兜底
    return out[:cap]


PROBE_TIMEOUT = 2.5   # 好窗口里 TLS 握手 <1s;坏窗口等再久也是白等,收紧


def _fail_kind(exc):
    """一次探测失败的病因归类(只做诊断输出,不参与选路)。
    审查在演进(QUIC-SNI 阻断 2024 已落地,TCP-SNI 升级是时间问题),
    把「连不上」拆成病因,封锁升级时用户能第一时间自查。"""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "证书不匹配(污染 IP)"
    if isinstance(exc, ssl.SSLError):
        return "TLS 握手失败(SNI 识别?)"
    if isinstance(exc, ConnectionResetError):
        return "连接被重置"
    if isinstance(exc, socket.timeout):
        return "无响应(被丢包)"
    if isinstance(exc, OSError):
        return "TCP 不通(IP 被封)"
    return "其他"


def wave(pairs):
    """并行探测一批 (domain, ip),返回 ({domain: (rtt, ip) 中的最优}, {domain: 病因计数})。

    所有域名共享一个线程池(旧版逐域名各开一个池再串行排队,
    13 个域名要等 13 轮);坏 IP 在 PROBE_TIMEOUT 内出局,不拖全局。
    """
    out, diag = {}, {}

    def one(job):
        d, ip = job
        try:
            return (d, ip, probe(d, ip, timeout=PROBE_TIMEOUT))
        except Exception as e:
            k = _fail_kind(e)
            diag.setdefault(d, {})
            diag[d][k] = diag[d].get(k, 0) + 1
            return (d, ip, None)

    with cf.ThreadPoolExecutor(max_workers=48) as ex:
        for d, ip, rtt in ex.map(one, pairs):
            if rtt is not None and (d not in out or rtt < out[d][0]):
                out[d] = (rtt, ip)
    return out, diag


def find_best(progress=False, on_round=None):
    """逐域名测速,返回 {domain: {"ip":..., "rtt_ms":...}}。

    两级加速,解决"开启要等一两分钟":
    1. 种子轮 —— 上一轮测出的冠军 IP 大概率还活着,先只验这些 IP,
       全过的话几秒收工,不必全量扫网段;
    2. 全局波次 —— 种子没过的域名把全部候选 IP 丢进同一个线程池并行测。
    链路坏窗口(TCP 通、TLS 被丢,几分钟一变)靠 3/10/25 秒间隔的
    跨窗口重试兜底(阶梯压到 3/8 秒,长坏窗口交给 30 分钟守护去翻盘);on_round 回调在每轮结束拿到当前部分结果,
    调用方可边测边把已测通的写进 hosts,不必等全部完成。
    连续全轮失败的域名(本网络硬阻断,如 raw/gist 的 SNI 阻断)
    记入 state 的 dead 表,之后跳过重试轮,避免每次都白等阶梯。
    """
    prev_data = load_seeds() or {}
    prev = prev_data.get("map", {})
    dead = prev_data.get("dead", {})   # 域名 -> 连续全轮失败次数

    def report(done, failed):
        if not progress:
            return
        for d in done:
            print("  + %-38s %-16s %4d ms"
                  % (d, state[d]["ip"], state[d]["rtt_ms"]), flush=True)
        for d in failed:
            print("  x %-38s 本轮候选 IP 均不可达" % d, flush=True)

    state = {}
    diag_all = {}   # 域名 -> {病因: 次数},全轮失败的域名据此报病因

    def merge_diag(dg):
        for d, ks in dg.items():
            t = diag_all.setdefault(d, {})
            for k, n in ks.items():
                t[k] = t.get(k, 0) + n

    # 种子轮:上一轮的冠军 IP
    seeds = [(d, prev[d]["ip"]) for d in DOMAINS if prev.get(d, {}).get("ip")]
    if seeds:
        r, dg = wave(seeds)
        merge_diag(dg)
        for d, (rtt, ip) in r.items():
            state[d] = {"ip": ip, "rtt_ms": round(rtt * 1000)}
        report(sorted(r), [d for d, _ in seeds if d not in r])
    pending = [d for d in DOMAINS if d not in state]
    if progress and seeds and pending:
        print("  ... 种子轮通过 %d/%d,全量补测 %d 个 ..."
              % (len(seeds) - len(pending), len(seeds), len(pending)), flush=True)

    # 全量波次 + 跨窗口重试
    for pause in (0, 3, 8):
        if pause:
            # 上一轮已全败的域名不再浪费跨窗口等待(窗口尺度几分钟,
            # 短阶梯很少等来翻转);第一波仍会测它们,解禁自然恢复
            pending = [d for d in pending if dead.get(d, 0) < 1]
        if not pending:
            break
        if pause:
            if progress:
                print("  ... 链路抖动,%.0f 秒后跨窗口重试 %d 个域名 ..."
                      % (pause, len(pending)), flush=True)
            time.sleep(pause)
        if pending:
            r, dg = wave([(d, ip) for d in pending for ip in candidates_for(d)])
            merge_diag(dg)
            for d, (rtt, ip) in r.items():
                state[d] = {"ip": ip, "rtt_ms": round(rtt * 1000)}
            report(sorted(r), [d for d in pending if d not in r])
        pending = [d for d in DOMAINS if d not in state]
        if on_round:
            on_round(dict(state))
        if not pending:
            break

    # 维护"连续全败"计数:测通的清零,全轮失败的 +1(≥2 后跳过重试轮)
    for d in DOMAINS:
        if d in state:
            dead.pop(d, None)
        else:
            if progress and diag_all.get(d):   # 病因见人话:手工 probe 时才打印/记录
                top = max(diag_all[d].items(), key=lambda kv: kv[1])
                print("  ! %-38s 主要病因:%s(×%d)"
                      % (d, top[0], top[1]), flush=True)
                journal("link", "%s 本轮全败,病因:%s×%d" % (d, top[0], top[1]))
            dead[d] = dead.get(d, 0) + 1
    return state, dead


# ---------------- /etc/hosts ----------------


def merge_with_existing(state):
    """本轮没测通的域名,保留 hosts 里的旧钉(旧 IP 再差也比裸奔强)。

    坏窗口期间某域整轮全败,write_hosts 会把它原本钉好的条目刷掉;
    等窗口开了,主域反而没了 pin,只能吃污染 DNS —— github.com 从
    hosts 里消失就是这个原因。以 hosts 现有条目兜底补齐。
    """
    old = current_map_from_hosts()
    for d in DOMAINS:
        if d not in state and d in old:
            state[d] = {"ip": old[d], "rtt_ms": -1}
    return state


def hosts_block(state):
    hint = "ghspeed off 可整体移除" if IS_WIN else "sudo ghspeed off 可整体移除"
    lines = [MARK_S, "# ghspeed 自动生成,勿手改;" + hint,
             "# Update time: " + time.strftime("%Y-%m-%d %H:%M:%S %Z")]
    for d in sorted(state):
        lines.append("%s %s" % (state[d]["ip"], d))
    lines.append(MARK_E)
    return "\n".join(lines) + "\n"


def read_hosts():
    with open(HOSTS_FILE, encoding="utf-8", errors="replace") as f:
        return f.read()


def current_map_from_hosts():
    try:
        text = read_hosts()
    except Exception:
        return {}
    m, inb = {}, False
    for line in text.splitlines():
        s = line.strip()
        if s == MARK_S:
            inb = True
            continue
        if s == MARK_E:
            break
        if inb and s and not s.startswith("#"):
            parts = s.split()
            if len(parts) == 2:
                m[parts[1]] = parts[0]
    return m


def flush_dns():
    # 刷新失败/卡住都不该拖住「关闭加速」:全部有界,失败当没事
    try:
        if IS_WIN:
            _run(["ipconfig", "/flushdns"], timeout=6,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_MAC:
            _run(["dscacheutil", "-flushcache"], timeout=6,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _run(["killall", "-HUP", "mDNSResponder"], timeout=6,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # systemd-resolved 的两种入口,有哪个用哪个,都没有就无所谓
            _run(["resolvectl", "flush-caches"], timeout=6,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _run(["systemd-resolve", "--flush-caches"], timeout=6,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _replace_hosts(text):
    """把 text 写进 hosts:先临时文件 + 原子替换,替换被拒再退回原地重写。

    Windows 上 hosts 常被安全软件或另一进程的读句柄短暂占住,MoveFileEx
    会直接抛 WinError 5(拒绝访问)/ 32(占用);这类冲突通常几百毫秒就散,
    所以先重试几次。仍不行就原地重写(不换文件、不动 ACL),绕开"改名"
    这一步——很多保护只拦改名/删除,不拦写内容。
    """
    tmp = HOSTS_FILE + ".ghspeed.tmp"
    last = None
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        for delay in (0, 0.2, 0.5, 1.0):
            if delay:
                time.sleep(delay)
            try:
                os.replace(tmp, HOSTS_FILE)
                flush_dns()
                return
            except PermissionError as e:      # WinError 5 / 32 都归到这里
                last = e
                try:                           # 目标带只读属性时先摘掉
                    os.chmod(HOSTS_FILE, 0o666)
                except OSError:
                    pass
        for delay in (0, 0.2, 0.5, 1.0):
            if delay:
                time.sleep(delay)
            try:
                os.chmod(HOSTS_FILE, 0o666)
            except OSError:
                pass
            try:
                with open(HOSTS_FILE, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                flush_dns()
                return
            except PermissionError as e:
                last = e
        raise last
    finally:
        try:                                   # 失败也别留垃圾文件
            os.unlink(tmp)
        except OSError:
            pass


def write_hosts(state):
    """把 ghspeed 的 hosts 块替换/追加进去,其余内容原样保留"""
    text = read_hosts()
    block = hosts_block(state)
    if MARK_S in text:
        text = re.sub(re.escape(MARK_S) + r".*?" + re.escape(MARK_E),
                      block.strip(), text, flags=re.S)
    else:
        if not text.endswith("\n"):
            text += "\n"
        if not os.path.exists(BACKUP_FILE):
            shutil.copy2(HOSTS_FILE, BACKUP_FILE)
            os.chmod(BACKUP_FILE, 0o644)
        text += "\n" + block
    _replace_hosts(text)


def remove_hosts():
    text = read_hosts()
    if MARK_S in text:
        text = re.sub(re.escape(MARK_S) + r".*?" + re.escape(MARK_E) + r"\n?",
                      "", text, flags=re.S)
        _replace_hosts(text)
        flush_dns()
        return True
    return False


# ---------------- 定时刷新守护(macOS launchd / Linux systemd / Windows schtasks)----------------

PLIST_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>%(label)s</string>
    <key>ProgramArguments</key>
    <array>
        <string>%(exe)s</string>
        <string>%(script)s</string>
        <string>watch</string>
        <string>--quiet</string>
        <string>--budget</string>
        <string>900</string>
    </array>
    <key>StartInterval</key><integer>%(interval)d</integer>
    <key>Nice</key><integer>10</integer>
    <key>StandardOutPath</key><string>%(log)s</string>
    <key>StandardErrorPath</key><string>%(log)s</string>
</dict>
</plist>
"""

SYSTEMD_SERVICE_TMPL = """[Unit]
Description=GHSpeed GitHub hosts 定时刷新
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=%(exec_start)s
Nice=10
"""

SYSTEMD_TIMER_TMPL = """[Unit]
Description=GHSpeed 每 %(minutes)d 分钟重测 GitHub 最优 IP

[Timer]
OnBootSec=2min
OnCalendar=*:0/%(minutes)d
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
"""


def daemon_install():
    if getattr(sys, "frozen", False):
        # PyInstaller 打包:__file__ 指向临时解压目录,重启即失效,只能用 exe;
        # Windows 下定时刷新必须走命令行版 ghspeed-cli.exe —— GUI 的 GHSpeed.exe
        # 带参数只会开界面不会刷新(NTFS 大小写不敏感,GUI/CLI 必须不同名)
        exe = os.path.join(os.path.dirname(sys.executable), "ghspeed-cli.exe")
        if not os.path.exists(exe):
            exe = sys.executable   # 单发场景没带 CLI,退回自身
        script, minutes = "", REFRESH_INTERVAL // 60
    else:
        exe, script, minutes = sys.executable, os.path.realpath(__file__), REFRESH_INTERVAL // 60
    if IS_WIN:
        # SYSTEM 账号自带管理员权限;TR 内层引号由 schtasks 约定为 \"
        if script:
            tr = '\\"%s\\" \\"%s\\" watch --quiet --budget 900' % (exe, script)
        else:
            tr = '\\"%s\\" watch --quiet --budget 900' % exe
        try:
            _run(["schtasks", "/Create", "/F", "/RU", "SYSTEM",
                  "/SC", "MINUTE", "/MO", str(minutes),
                  "/TN", WIN_TASK, "/TR", tr],
                 timeout=20, check=False, capture_output=True)
        except Exception:
            pass   # 定时刷新装不上不影响本次 hosts 生效
    elif IS_MAC:
        body = PLIST_TMPL % {"label": PLIST_LABEL, "exe": exe,
                             "script": script, "interval": REFRESH_INTERVAL,
                             "log": LOG_FILE}
        with open(PLIST_PATH, "w") as f:
            f.write(body)
        os.chmod(PLIST_PATH, 0o644)
        shutil.chown(PLIST_PATH, "root", "wheel")
        subprocess.run(["launchctl", "bootout", "system/" + PLIST_LABEL],
                       capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", "system", PLIST_PATH],
                           capture_output=True, text=True)
        if r.returncode != 0:  # 旧系统兜底
            subprocess.run(["launchctl", "load", "-w", PLIST_PATH], check=False)
    else:
        args = "watch --quiet --budget 900"
        exec_start = ("%s %s %s" % (exe, script, args)) if script else \
                     ("%s %s" % (exe, args))
        for path, body in (
                (SYSTEMD_SERVICE, SYSTEMD_SERVICE_TMPL
                 % {"exec_start": exec_start}),
                (SYSTEMD_TIMER, SYSTEMD_TIMER_TMPL % {"minutes": minutes})):
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o644)
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
        subprocess.run(["systemctl", "enable", "--now", "ghspeed-refresh.timer"],
                       check=False, capture_output=True)


def daemon_remove():
    if IS_WIN:
        try:
            _run(["schtasks", "/Delete", "/F", "/TN", WIN_TASK],
                 timeout=15, check=False, capture_output=True)
        except Exception:
            pass   # 计划任务服务卡住也不该拖住关闭
    elif IS_MAC:
        subprocess.run(["launchctl", "bootout", "system/" + PLIST_LABEL],
                       capture_output=True)
        subprocess.run(["launchctl", "unload", "-w", PLIST_PATH], capture_output=True)
        try:
            os.remove(PLIST_PATH)
        except FileNotFoundError:
            pass
    else:
        subprocess.run(["systemctl", "disable", "--now", "ghspeed-refresh.timer"],
                       check=False, capture_output=True)
        for path in (SYSTEMD_SERVICE, SYSTEMD_TIMER):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)


def daemon_installed():
    if IS_WIN:
        try:
            return _run(["schtasks", "/Query", "/TN", WIN_TASK],
                        timeout=10, check=False, capture_output=True).returncode == 0
        except Exception:
            return False
    if IS_MAC:
        return os.path.exists(PLIST_PATH)
    return os.path.exists(SYSTEMD_TIMER)


def daemon_cmd_current():
    """已装的定时任务是否已是当前命令模板(watch 自愈档)。

    历史安装跑的是 refresh(固定 30 分钟一测,坏窗口最多盲 30 分钟);
    升级后在下次 refresh 时静默迁移到 watch(健康几秒收工,坏窗口内加密重测)。
    """
    try:
        if IS_WIN:
            r = _run(["schtasks", "/Query", "/TN", WIN_TASK, "/V", "/FO", "LIST"],
                     timeout=20, check=False, capture_output=True, text=True,
                     errors="replace")
            out = r.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            return "watch" in out
        path = PLIST_PATH if IS_MAC else SYSTEMD_SERVICE
        with open(path, encoding="utf-8", errors="replace") as f:
            return "watch" in f.read()
    except Exception:
        return False


# ---------------- 子命令 ----------------


def cmd_probe(args):
    print("正在对 %d 个 GitHub 域名测速(不写 hosts)..." % len(DOMAINS))
    state, dead = find_best(progress=True)
    if not state:
        sys.exit("没有任何可用 IP,请检查网络")
    save_state(state, dead)
    hosts_export_write()   # GitHub520 兼容导出跟着测速结果一起新鲜
    journal("probe", "%d/%d 通" % (len(state), len(DOMAINS)))
    print("\n共 %d/%d 个域名可用,结果已缓存(启用请:sudo ghspeed on)"
          % (len(state), len(DOMAINS)))
    threading.Thread(  # 顺手把动态镜像池/hosts 源缓存也焐热,dl 和 mirrors 直接受益
        target=lambda: (mirror_pool(), ip_feed_candidates()), daemon=True).start()


def cmd_on(args):
    need_root()
    print("正在测速 GitHub 各域名候选 IP(有缓存时几秒,全量约 20 秒)...")

    def on_round(partial):
        # 边测边生效:每轮结束把已测通的域名先钉进 hosts,
        # 用户侧 github.com 早点能用,不必等全部测完
        if partial:
            write_hosts(merge_with_existing(dict(partial)))
            flush_dns()

    state, dead = find_best(progress=True, on_round=on_round)
    if not state:
        sys.exit("没有测到任何可用 IP,本次不做任何修改")
    state = merge_with_existing(state)
    write_hosts(state)
    save_state(state, dead)
    daemon_install()
    journal("on", "%d/%d 域名" % (len(state), len(DOMAINS)))
    print("\n已启用:%d/%d 个域名固定到最快 IP" % (len(state), len(DOMAINS)))
    print("  - hosts 备份:%s(首次启用时创建)" % BACKUP_FILE)
    print("  - 每 %d 分钟自动重测刷新,日志:%s" % (REFRESH_INTERVAL // 60, LOG_FILE))
    print("  - 停用:sudo ghspeed off")


def cmd_off(args):
    need_root()
    removed = remove_hosts()
    daemon_remove()
    sni_moved = False
    try:
        sni_moved = sni_off(quiet=True)
    except SystemExit:
        pass
    journal("off", "hosts 已还原" if removed else "本来就未开启")
    print("hosts 中的 ghspeed 条目:%s" % ("已移除" if removed else "本来就没有"))
    if sni_moved:
        print("浏览器 SNI 分片(PAC + relay):已一并还原")
    if os.path.exists(BACKUP_FILE):
        print("原始 hosts 备份仍在:%s" % BACKUP_FILE)
    print("定时刷新任务:已卸载")


def quick_health(timeout=4.0):
    """钉选链路健康快查:github.com → api → codeload 逐个试当前钉选 IP,
    谁通算谁(与 macOS 菜单栏同一套口径)。三域全不通 = 坏窗口,返回 False。
    只做 3 次 TLS 握手,一两秒收工,给定时任务当廉价的「要不要自愈」开关。"""
    try:
        ips = current_map_from_hosts()
    except Exception:
        ips = {}
    if not ips:
        ips = {d: v["ip"] for d, v in (load_state() or {}).get("map", {}).items()}
    for d in ("github.com", "api.github.com", "codeload.github.com"):
        ip = ips.get(d)
        if not ip:
            continue
        try:
            probe(d, ip, timeout=timeout)
            return True, d
        except Exception:
            continue
    return False, ""


def cmd_watch(args):
    """坏窗口自愈守护:健康即退(几秒),坏窗口内按拍重测,预算用完收工。

    给三端定时任务当「加密档」:平时一次快查就收工,链路坏窗口期自动升级为
    每拍重测,窗口一开立刻把新 IP 钉上 —— macOS 菜单栏已有同款循环,
    这里让 Windows/Linux(以及菜单栏没开着的场景)吃到同一份逻辑。
    """
    quiet = getattr(args, "quiet", False)
    if not current_map_from_hosts():
        if not quiet:
            print("ghspeed: 未开启加速(hosts 无钉选),watch 无事可做")
        return
    t0, beat = time.time(), 0
    while True:
        beat += 1
        ok, via = quick_health()
        if ok:
            if beat > 1:   # 本轮自愈成功,记一笔(健康快查不打日志)
                journal("watch", "坏窗口走出(经 %s),重测 %d 轮" % (via, beat - 1))
            if not quiet:
                print("ghspeed: 链路正常(%s)%s" % (via, "" if beat == 1 else ",已重测恢复"))
            return
        left = args.budget - (time.time() - t0)
        if left <= 0:
            journal("watch", "坏窗口未走出,预算 %ds 用完(已重测 %d 轮)" % (args.budget, beat - 1))
            if not quiet:
                print("ghspeed: 坏窗口未走出(预算用完),下个周期继续")
            return
        if not quiet:
            print("ghspeed: 链路不通,重测换优(剩余预算 %.0f 秒)..." % left, flush=True)
        try:
            cmd_refresh(argparse.Namespace(quiet=True))
        except SystemExit:
            pass   # refresh 的业务报错不该终止守护循环
        pause = max(0, min(args.interval, left))
        if pause:
            time.sleep(pause)


def cmd_refresh(args):
    def on_round(partial):
        if is_admin() and partial:
            write_hosts(merge_with_existing(dict(partial)))

    state, dead = find_best(progress=not args.quiet, on_round=on_round)
    if not state:
        print("ghspeed: 本次未测到可用 IP,保留现有 hosts 不变")
        return
    if is_admin():
        state = merge_with_existing(state)
    save_state(state, dead)
    hosts_export_write()   # 同步刷新 GitHub520 兼容导出(静默尽力)
    if is_admin():
        if current_map_from_hosts() != {d: v["ip"] for d, v in state.items()}:
            write_hosts(state)
            journal("refresh", "%d 域名已更新" % len(state))
            print("ghspeed: 刷新完成,%d 个域名已更新" % len(state))
        else:
            journal("refresh", "IP 无变化")
            print("ghspeed: 刷新完成,IP 无变化")
        # 存量安装迁移:定时任务还是旧 refresh 模板就静默升到 watch 自愈档
        if daemon_installed() and not daemon_cmd_current():
            daemon_install()
            journal("refresh", "定时任务已迁移到 watch 自愈档")
    else:
        print("ghspeed: 已更新测速缓存(root 运行才会写 hosts)")


def cmd_status(args):
    st = load_state()
    if st:
        age_min = (time.time() - st["time"]) / 60
        print("上次测速:%.0f 分钟前" % age_min)
        for d in sorted(st["map"]):
            print("  %-38s %-16s %4d ms"
                  % (d, st["map"][d]["ip"], st["map"][d]["rtt_ms"]))
    else:
        print("尚未测速(可先运行 ghspeed probe)")

    try:
        in_hosts = current_map_from_hosts()
    except Exception:
        in_hosts = {}
    print("\n%s:%s" % (HOSTS_FILE,
        "已写入 %d 个 GitHub 域名" % len(in_hosts) if in_hosts else "未启用(运行 sudo ghspeed on)" if not IS_WIN else "未启用(管理员终端运行 ghspeed on)"))
    print("定时刷新:%s" % ("已安装" if daemon_installed() else "未安装"))

    try:
        ips = socket.getaddrinfo("github.com", 443, socket.AF_INET,
                                 socket.SOCK_STREAM)
        ip = ips[0][4][0]
        ms = probe("github.com", ip, timeout=5)
        print("\n当前 github.com 实际走 %s,HTTPS 握手 %.0f ms"
              % (ip, ms * 1000))
    except Exception as e:
        print("\n当前 github.com 访问失败:%s" % e)

    # 多级通道自检:hosts / 镜像池 / ssh443 / 本地代理 / 系统 DNS
    print("\n多级通道自检:")
    print("  ① hosts 钉 IP    %s" % ("已开启,钉 %d 个域名" % len(in_hosts)
          if in_hosts else "未开启"))
    dead = (load_seeds() or {}).get("dead") or {}
    if dead:
        print("     ⚠ 长期不通 %d 个:%s——资源走镜像下载(ghspeed dl),浏览器可试 sni on"
              % (len(dead), "、".join(sorted(dead))))
    dyn = dyn_load()
    cpool, cts = dyn.get("mirrors") or [], dyn.get("mirrors_ts")
    feeds, fts = dyn.get("feed_ips") or {}, dyn.get("ips_ts")
    print("  ② 下载镜像池     静态 %d + 动态 %d(池子 %s 验证);hosts 源 %d 条(%s)"
          % (len(MIRRORS), len(cpool),
             ("%.0f 小时前" % ((time.time() - cts) / 3600)) if cts else "尚未",
             sum(len(v) for v in feeds.values()),
             ("%.0f 小时前" % ((time.time() - fts) / 3600)) if fts else "尚未"))
    try:
        ssh_ips = doh_lookup("ssh.github.com") or [
            socket.getaddrinfo("ssh.github.com", 443, socket.AF_INET,
                               socket.SOCK_STREAM)[0][4][0]]
        t0 = time.perf_counter()
        socket.create_connection((ssh_ips[0], 443), timeout=2.5).close()
        print("  ③ SSH 备用口 443 通(%s,%d ms)→ git 不顺时可:ghspeed ssh443 on"
              % (ssh_ips[0], (time.perf_counter() - t0) * 1000))
    except Exception:
        print("  ③ SSH 备用口 443 不通(ghspeed ssh443 test 细查)")
    found = detect_proxies(timeout=0.4)
    p = proxy_persisted()
    if p:
        print("  ④ 本地代理       已接管(仅 GitHub → %s;proxy off 可还原)" % p)
    else:
        print("  ④ 本地代理       %s" % (",".join(found) + "(proxy on 可一键接管)"
              if found else "未发现常见端口"))
    if not IS_LINUX:
        print("  ⑤ 系统 DNS       %s" % ("已接管(dns off 可取消)"
              if dns_is_on() else "未接管(可选:ghspeed dns on)"))
        if not IS_LINUX:
            st = sni_state()
            print("  ⑥ 浏览器 SNI     %s"
                  % ("已开启(relay %s,策略 %s;sni off 可取消)"
                     % ("活着" if st["relay"] else "不在跑,已回落 DIRECT 兜底",
                        st["strategy"])
                     if st["enabled"] else "未开启(可选:ghspeed sni on)"))
    ts = tier_state()
    print("  ⑦ 下载档位      %s"
          % ("已开启(127.0.0.1:%d;服务%s;tier off 可取消)"
             % (ts["port"], "活着" if ts["alive"] else "没在跑,tier heal 自愈")
             if ts["on"] else "未开启(可选:ghspeed tier on——浏览器下载无感走多通道)"))
    conns = live_connections()
    if conns:
        total = sum(n for _, n in conns)
        print("  ⚡ 活动连接       %d 条 ← %s(ghspeed live 看详情)"
              % (total, " ".join("%s×%d" % c for c in conns[:4])))
    else:
        print("  ⚡ 活动连接       当前无(访问 GitHub 时自动命中钉选)")


# 真实 release 资产(借鉴 fnos-store mirrors.go 的测法):release 下载走
# 独立后端,是镜像最容易"根路径活着但代理失败"的场景,必须拿真路径测
MIRROR_TEST_URL = ("https://github.com/jqlang/jq/releases/download/"
                   "jq-1.7.1/jq-linux-amd64")


def _fmt_when(ts):
    if not ts:
        return "从未"
    d = time.time() - ts
    if d >= 86400:
        return "%.0f 天前" % (d / 86400)
    if d >= 3600:
        return "%.0f 小时前" % (d / 3600)
    return "%.0f 分钟前" % (d / 60)


def cmd_mirrors(args):
    op = getattr(args, "op", None)
    if op:   # 自建镜像一行入池:ghspeed mirrors add|remove https://你的Worker域名/
        u = (getattr(args, "mirror_url", None) or "").strip()
        if "://" not in u:
            sys.exit("用法:ghspeed mirrors %s https://你的gh-proxy域名/" % op)
        u = u.rstrip("/") + "/"
        xs = custom_mirrors()
        if op == "add":
            if u in xs:
                print("已在自建清单里:%s" % u)
                return
            rtt = validate_mirror(u)
            if rtt is None:
                print("⚠ 此刻 HEAD 验证没通过(死了/坏窗口/地址写错);仍登记为候选——"
                      "活没活是验证说了算,只要哪天能回源=实际可用就自动进推荐链")
            else:
                print("✓ 验证通过(HEAD %.0f ms)" % (rtt * 1000))
            _custom_mirrors_save(xs + [u])
            print("已登记自建镜像:%s" % u)
            print("重验全池并按吞吐排序(约半分钟)...")
            mirror_pool(force=True)
            entries = dyn_load().get("mirrors") or []
            inpool = any((e[0] if isinstance(e, (list, tuple)) else e) == u
                         for e in entries)
            if inpool:
                print("✓ 已进推荐链(位置按实测吞吐)")
            else:
                print("(未进本轮推荐链:验证没过;24h 内自动重验,活的路自然会回来)")
            return
        if u not in xs:
            print("自建清单里没有:%s(ghspeed mirrors add <url> 先登记)" % u)
            return
        xs.remove(u)
        _custom_mirrors_save(xs)
        print("已从自建清单移除:%s(下次重验不再出现)" % u)
        return
    print("镜像健康检查(HEAD 活性 + 256KB 实测吞吐,真实 release 资产 jq-linux-amd64):")
    pool = mirror_pool()
    dyn = dyn_load()
    _spd = {e[0]: (e[2] if len(e) > 2 else 0.0)
            for e in (dyn.get("mirrors") or [])
            if isinstance(e, (list, tuple))}
    stats = dyn.get("mirror_stats") or {}
    print("(池子:静态 %d + 动态 %d,动态来源 %s,全部真验证过)"
          % (len([m for m in pool if m in set(MIRRORS)]),
             len([m for m in pool if m not in set(MIRRORS)]),
             "、".join(MIRROR_FEED_SOURCES)))
    print("(下单链按实测吞吐优先、延迟次之(动态序);静态默认序 %s 只是候选来源,不代表最快)"
          % " → ".join(urlparse(m).netloc for m in MIRRORS))

    def t(m):
        url = (m + MIRROR_TEST_URL) if m else MIRROR_TEST_URL
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA},
                                         method="HEAD")
            with urllib.request.urlopen(req, timeout=8,
                                        context=ssl_context()) as r:
                r.read(0)
            return (time.perf_counter() - t0, r.getcode(), None)
        except Exception as e:
            return (None, None, str(e)[:60])

    chain = pool + [None]
    results = []
    with cf.ThreadPoolExecutor(8) as ex:
        for m, (ms, code, err) in zip(chain, ex.map(t, chain)):
            results.append((m, ms, code, err))
    # 这轮 HEAD 检查本身也是真实交互:入账(池子缓存命中、验证环节被跳过时,
    # 这里就是记忆的唯一入口)
    mirror_record_batch([(m, ms is not None and code == 200, 0.0)
                         for m, ms, code, err in results if m is not None])
    stats = dyn_load().get("mirror_stats") or stats   # 入账后读回,展示即最新

    print()
    alive = []
    for m, ms, code, err in results:
        if m is None:
            name = "直连(吃 hosts 优化)"
        else:
            name = urlparse(m).netloc + ("·静态" if m in MIRRORS else "·动态")
        if ms is not None:
            sp = _spd.get(m, 0.0) if m is not None else 0.0
            s = stats.get(m) or {}
            print("  + %-30s %5.0f ms  HTTP %s%s%s"
                  % (name, ms * 1000, code,
                     ("  %5.1f MB/s" % (sp / 1e6)) if sp else "",
                     ("  [记忆 ok %d·败 %d·最近成功 %s]"
                      % (s.get("ok", 0), s.get("fail", 0),
                         _fmt_when(s.get("last_ok")))) if m is not None else ""))
            if m is not None and code == 200:
                alive.append((-(sp or 0.0), ms, name))   # 展示序与池子一致:吞吐优先,延迟次之
        else:
            print("  x %-30s %s" % (name, err))

    alive.sort()
    if alive:
        print("\n推荐顺序(吞吐优先):%s" % " → ".join(n for _, _, n in alive))
        print("自定义镜像:GHSPEED_MIRRORS=\"https://a.example.com/,https://b.example.com/\" "
              "ghspeed dl <url>")

    if getattr(args, "export", None):
        doc = {
            "_note": "ghspeed 实测通过的镜像池(逐个真验证:能回源 GitHub release 资产)。"
                     "公共镜像生死随时段变化,引用方请自行定期复验。",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ghspeed": __version__,
            "test_url": MIRROR_TEST_URL,
            "mirrors": [
                {"url": e[0],
                 "rtt_ms": round(e[1] * 1000) if len(e) > 1 and e[1] else None,
                 "mbps": round(e[2], 1) if len(e) > 2 else None,
                 "ok": (stats.get(e[0]) or {}).get("ok", 0),
                 "fail": (stats.get(e[0]) or {}).get("fail", 0),
                 "last_ok": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                          time.localtime(stats[e[0]]["last_ok"]))
                 if (stats.get(e[0]) or {}).get("last_ok") else None}
                for e in (dyn.get("mirrors") or []) if isinstance(e, (list, tuple))],
        }
        try:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")
            print("\n已导出 → %s(可用作社区镜像数据源;引用前自行复验)"
                  % os.path.abspath(args.export))
        except OSError as e:
            print("\n导出失败:%s" % e)


def download_once(url, part_path, resume, min_speed=25.0, stall_s=12.0, proxy=None):
    """下载到 part_path;传输停滞或平均速度过低时抛异常,由上层换通道重试。"""
    headers = {"User-Agent": UA}
    offset = os.path.getsize(part_path) if resume and os.path.exists(part_path) else 0
    if offset:
        headers["Range"] = "bytes=%d-" % offset
    req = urllib.request.Request(url, headers=headers)
    if proxy:   # 最后一级通道:经本地 HTTP 代理(CONNECT)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ssl_context()))
        resp = opener.open(req, timeout=60)
    else:
        resp = urllib.request.urlopen(req, timeout=60, context=ssl_context())
    try:
        code = resp.getcode()
        if code not in (200, 206):
            raise RuntimeError("HTTP %s" % code)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ("text/html" in ctype
                and not url.split("?")[0].lower().endswith((".html", ".htm"))):
            raise RuntimeError("返回的是网页而非文件(疑似镜像错误页)")
        clen = resp.headers.get("Content-Length")
        total = (int(clen) + offset if code == 206 else int(clen)) if clen else None
        mode = "ab" if (code == 206 and offset) else "wb"
        t0 = time.time()
        got, last, last_progress = 0, 0.0, time.time()
        with open(part_path, mode) as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last_progress > stall_s:
                    raise RuntimeError("传输停滞超过 %.0fs,换下一个通道" % stall_s)
                last_progress = now
                if (min_speed and now - t0 > 30
                        and got / 1024 / (now - t0) < min_speed):
                    raise RuntimeError("平均速度低于 %.0f KB/s,换下一个通道"
                                       % min_speed)
                if now - last > 0.5:
                    last = now
                    speed = got / max(now - t0, 1e-6) / 1e6
                    prog = " / %s" % human(total) if total else ""
                    print("\r    %s%s  %.2f MB/s"
                          % (human(offset + got), prog, speed),
                          end="", flush=True)
        print()
        return got, time.time() - t0
    finally:
        resp.close()


SHARD_MIN_TOTAL = 8 << 20   # ≥8MB 才值得分片(GHSPEED_SHARD_MB 可调)
SHARD_MIN_SIZE = 1 << 20    # 每片至少 1MB
SHARD_MAX = 8               # 最多 8 片,片号轮转分给不同通道


class ShardedSkip(Exception):
    """不满足分片条件(小文件/探测不到大小),上层退回单流链"""


def _dl_target(m, url):
    """chain 元素 → (实际 URL, 代理或 None)"""
    if m and m.startswith("PROXY:"):
        return url, m[6:]
    if m and m.startswith("RAWJS:"):   # jsDelivr:URL 要重排,不是前缀
        return (_raw_jsdelivr(m[6:], url) or url), None
    return ((m + url) if m else url), None


def _dl_open(u, headers, prox, timeout=30):
    req = urllib.request.Request(u, headers=headers)
    if prox:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": prox, "https": prox}),
            urllib.request.HTTPSHandler(context=ssl_context()))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def download_sharded(url, part, chain, min_speed=25.0, stall_s=12.0):
    """分片并行下载:文件 ≥8MB 时切 ≤8 片,片号轮转分给链上不同通道,
    每片自带 Range 续传与停滞跳道,片失败自动换下一通道接剩余区间。
    片级断点:<part>.shards.json 记录每片完成字节,重跑同命令自动续。
    返回 (本次字节, 秒);条件不满足抛 ShardedSkip(退回单流链)。"""
    try:
        mb = float(os.environ.get("GHSPEED_SHARD_MB", SHARD_MIN_TOTAL >> 20))
        threshold = int(mb * (1 << 20))
    except ValueError:
        threshold = SHARD_MIN_TOTAL
    # —— 探测:找第一个能报出大小的通道;小文件直接交回单流 ——
    head, total = None, 0
    for m in chain:
        if m is None or (m and m.startswith("PROXY:")):
            continue   # 直连/代理不定大小门槛,仅作片通道用
        real_url, prox = _dl_target(m, url)
        try:
            with _dl_open(real_url, {"User-Agent": UA}, prox, timeout=8) as r:
                if r.getcode() == 200:
                    total = int(r.headers.get("Content-Length") or 0)
        except Exception:
            continue
        if total >= threshold:
            head = m
            break
        if total:
            raise ShardedSkip("文件仅 %s" % human(total))
    if not head or total <= 0:
        raise ShardedSkip("探测不到文件大小")

    n = min(SHARD_MAX, max(1, total // SHARD_MIN_SIZE))
    bounds = [total * i // n for i in range(n + 1)]
    side = part + ".shards.json"
    done = [0] * n
    try:
        with open(side) as f:
            state = json.load(f)
        if (state.get("url") == url and state.get("size") == total
                and isinstance(state.get("done"), list)
                and len(state["done"]) == n):
            done = [max(0, min(int(x), bounds[i + 1] - bounds[i]))
                    for i, x in enumerate(state["done"])]
    except Exception:
        pass
    if not os.path.exists(part) or os.path.getsize(part) != total:
        with open(part, "wb") as f:
            f.truncate(total)

    stop = threading.Event()
    lock = threading.Lock()
    ptr = [0]

    def next_mi():
        with lock:
            mi = ptr[0]
            ptr[0] = (ptr[0] + 1) % len(chain)
            return mi

    def save_side():
        with lock:
            try:
                with open(side, "w") as f:
                    json.dump({"url": url, "size": total, "done": done}, f)
            except Exception:
                pass

    def work(i):
        lo, hi = bounds[i], bounds[i + 1] - 1
        with open(part, "r+b") as f:
            tried = 0
            while not stop.is_set():
                m = chain[next_mi()]
                if m is None or m.startswith("PROXY:"):
                    tried += 1
                    if tried >= len(chain) * 2:
                        raise RuntimeError("片 %d 无通道可用" % i)
                    continue
                real_url, prox = _dl_target(m, url)
                pos = lo + done[i]
                try:
                    with _dl_open(real_url, {"User-Agent": UA,
                                             "Range": "bytes=%d-%d" % (pos, hi)},
                                  prox) as r:
                        if r.getcode() != 206:
                            raise RuntimeError("HTTP %s(通道不支持 Range)"
                                               % r.getcode())
                        last = time.time()
                        while not stop.is_set():
                            chunk = r.read(1 << 16)
                            if not chunk:
                                break
                            chunk = chunk[:hi + 1 - pos]
                            f.seek(pos)
                            f.write(chunk)
                            pos += len(chunk)
                            done[i] += len(chunk)
                            last = time.time()
                        if pos > hi:
                            save_side()
                            return
                        raise RuntimeError("片 %d 提前断流" % i)
                except Exception:
                    tried += 1
                    if tried >= len(chain) * 2 or stop.is_set():
                        raise RuntimeError("片 %d 无通道可用" % i)
                    time.sleep(0.3)
        raise RuntimeError("片 %d 被中止" % i)

    t0 = time.time()
    got0 = sum(done)
    with cf.ThreadPoolExecutor(min(n, SHARD_MAX)) as ex:
        futs = {ex.submit(work, i): i for i in range(n)
                if done[i] < bounds[i + 1] - bounds[i]}
        try:
            while futs:
                time.sleep(0.8)
                for f in [f for f in futs if f.done()]:
                    f.result()          # worker 异常在这里浮出
                    del futs[f]
                sec = time.time() - t0
                if sec > 0.8:
                    print("\r    %s / %s  %.2f MB/s(分片×%d)"
                          % (human(sum(done)), human(total),
                             (sum(done) - got0) / sec / 1e6, n),
                          end="", flush=True)
                if (min_speed and sec > 30
                        and (sum(done) - got0) / 1024 / sec < min_speed
                        and sum(done) < total):
                    raise RuntimeError("平均速度低于 %.0f KB/s" % min_speed)
            print()
        except BaseException:
            stop.set()
            print()
            if not isinstance(sys.exc_info()[1], KeyboardInterrupt):
                try:                    # 真失败:清现场,单流链从头来
                    os.remove(side)
                except OSError:
                    pass
                try:
                    os.remove(part)
                except OSError:
                    pass
            raise
    return sum(done) - got0, time.time() - t0


_REPO_SH_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:@([A-Za-z0-9._/-]+))?$")

def cmd_dl(args):
    url = args.url.strip()
    if "://" not in url:
        # owner/repo[@ref] 整仓捷径 → codeload zip:零 API 调用(HEAD=默认分支由
        # codeload 自己解析),经镜像链+分片并行,坏窗口里下整仓最快的路
        m = _REPO_SH_RE.match(url)
        if not m:
            sys.exit("不认识的地址:给完整 GitHub 链接,或 owner/repo[@分支或tag]")
        o, r, ref = m.groups()
        url = "https://codeload.github.com/%s/%s/zip/%s" % (o, r, ref or "HEAD")
        print("==> 整仓下载 %s/%s%s(zip)" % (o, r, "@" + ref if ref else " 默认分支"))
        args.output = args.output or ("%s-%s.zip" % (r, ref) if ref else "%s.zip" % r)
    if not GITHUB_URL_RE.match(url):
        sys.exit("只支持 GitHub 相关链接(github.com / *.githubusercontent.com / "
                 "*.githubassets.com / *.github.io);其他网站本工具一概不处理")
    name = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or "download.bin"
    out = os.path.abspath(args.output or name)
    part = out + ".part"

    if args.direct:
        chain = [None]
    else:
        chain = [args.mirror] if args.mirror else mirror_pool()
        if _is_raw_url(url):
            # raw 文件额外补血 jsDelivr 三官方边界(公共仓、≤20MB;
            # 在公共镜像都不通时它是第三条活路,不可用自动跳过不拖速)
            chain += ["RAWJS:" + h for h in JSDELIVR_HOSTS]
        chain.append(None)  # 直连兜底(hosts 已优化)
    proxy = (args.proxy or os.environ.get("GHSPEED_PROXY", "")).strip() or None
    if not proxy:
        px = proxy_persisted()   # proxy on 接管过的:镜像+直连都失败时兜底用
        if px:
            proxy = px
            print("==> 已接管本机代理作兜底:%s(proxy off 可取消)" % proxy)
    if proxy:
        chain.append("PROXY:" + proxy)   # 最后一级:本地代理

    # —— 分片并行快路:≥8MB 自动切 8 片轮转多通道,片级断点续传 ——
    print("==> 分片并行(自动多通道)")
    try:
        got, secs = download_sharded(url, part, chain, min_speed=args.min_speed)
    except ShardedSkip as e:
        print("    (%s,改走单流链)" % e)
    except KeyboardInterrupt:
        print("\n已中断,分片断点保存在 %s,重跑同一命令可续传" % part)
        return
    except Exception as e:
        print("    分片并行失败:%s → 退回单流链" % e)
        # 分片留下的 part 是按总大小预分配的稀疏文件,不是连续前缀——
        # 单流的"按 part 大小续传"语义对它不成立,必须清掉重来
        for p in (part, part + ".shards.json"):
            try:
                os.remove(p)
            except OSError:
                pass
    else:
        os.replace(part, out)
        label = "分片×多通道"
        journal("dl", "%s | %s | %.2fMB/s"
                % (name, label, got / max(secs, 1e-6) / 1e6))
        print("完成 → %s (%s, 本次 %.2f MB/s)"
              % (out, human(os.path.getsize(out)),
                 got / max(secs, 1e-6) / 1e6))
        return

    rounds = 1 if (args.direct or args.mirror) else 2   # 自动池失败可强制重验一轮
    for round_i in range(rounds):
        for m in chain:
            if m and m.startswith("PROXY:"):
                real_url, prox, label = url, m[6:], "本地代理 %s" % m[6:]
            else:
                real_url, prox = (m + url) if m else url, None
                label = "直连" if m is None else urlparse(m).netloc
            print("==> 尝试 %s" % label)
            try:
                got, secs = download_once(real_url, part,
                                          resume=os.path.exists(part),
                                          min_speed=args.min_speed, proxy=prox)
            except KeyboardInterrupt:
                print("\n已中断,断点保存在 %s,重跑同一命令可续传" % part)
                return
            except Exception as e:
                print("    失败:%s" % e)
                if m and not m.startswith("PROXY:"):
                    mirror_record_batch([(m, False, 0.0)])   # 连败入账,池子排序记得它
                continue
            if m and not m.startswith("PROXY:"):
                mirror_record_batch([(m, True, got / max(secs, 1e-6))])
            os.replace(part, out)
            journal("dl", "%s | %s | %.2fMB/s"
                    % (name, label, got / max(secs, 1e-6) / 1e6))
            print("完成 → %s (%s, 平均 %.2f MB/s)"
                  % (out, human(got), got / max(secs, 1e-6) / 1e6))
            return
        if round_i + 1 < rounds:
            print("==> 全部通道失败,强制重验镜像池后再试一轮")
            chain = mirror_pool(force=True) + [None]
            if proxy:
                chain.append("PROXY:" + proxy)
    journal("dl-fail", url)
    sys.exit("全部通道均失败;可稍后重试,或:① ghspeed mirrors 看镜像健康状况  "
             "② ghspeed proxy detect 找本地代理后 GHSPEED_PROXY=... ghspeed dl ...")


def cmd_url(args):
    """把 GitHub 链接展开成 直连 + 各镜像前缀 的等价下载链接(挑顺手的用)。"""
    u = args.url.strip()
    if "://" not in u:
        m = _REPO_SH_RE.match(u)
        if not m:
            sys.exit("不认识的地址:给完整 GitHub 链接,或 owner/repo[@分支或tag]")
        o, r, ref = m.groups()
        u = "https://codeload.github.com/%s/%s/zip/%s" % (o, r, ref or "HEAD")
    if not GITHUB_URL_RE.match(u):
        sys.exit("只支持 GitHub 相关链接(github.com / *.githubusercontent.com / "
                 "*.githubassets.com / *.github.io);其他网站本工具一概不处理")
    pool = mirror_pool()
    print("直连  %s" % u)
    for e in pool:
        print("  %-24s %s%s" % (urlparse(e).netloc, e, u))
    if _is_raw_url(u):
        for h in JSDELIVR_HOSTS:   # raw 文件还有 CDN 直读这条等价路(公共仓,≤20MB)
            print("  %-24s %s" % (h, _raw_jsdelivr(h, u)))
    print("\n池子全部真验证过(24h 缓存)。用法:curl -L <链接> -o 文件名,"
          "或 ghspeed dl <原链接> 自动选最快通道")


def _console_user_home():
    """控制台登录用户的家:sudo 会把 HOME 重置成 /var/root,而导出的 hosts
    文件是给用户(和 SwitchHosts)看的,不能落进 root 家;Windows 无此问题"""
    if not IS_WIN:
        try:
            import pwd
            return pwd.getpwuid(os.stat("/dev/console").st_uid).pw_dir
        except Exception:
            pass
    return os.path.expanduser("~")


def hosts_export_write(path=None):
    """把当前测速结果写成 GitHub520 兼容格式的 hosts 文件(不动系统 hosts)。
    probe/refresh 成功后自动刷新——SwitchHosts 指向该文件即可吃到最新钉选;
    没有测速结果或写失败返回 None(尽力而为,不拖累主流程)。"""
    st = load_seeds() or {}
    mp = st.get("map") or {}
    if not mp:
        return None
    path = path or os.path.join(_console_user_home(), ".ghspeed", "hosts")
    lines = ["# GitHub520 Host Start"]
    for d in sorted(mp):
        lines.append("%-30s %s" % (mp[d]["ip"], d))
    lines += ["",
              "# Generator: ghspeed %s(全部 IP 过 TLS 证书验证,实测 %s)"
              % (__version__, time.strftime("%Y-%m-%dT%H:%M:%S%z")),
              "# Update url: https://github.com/archerzing-tech/ghspeed",
              "# GitHub520 Host End"]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        return None
    return path


def cmd_hosts_export(args):
    p = hosts_export_write(args.output)
    if not p:
        sys.exit("还没有可导出的测速结果,先跑 ghspeed probe")
    n = len((load_seeds() or {}).get("map") or {})
    print("已导出 → %s(%d 个域名,GitHub520 兼容格式)" % (p, n))
    print("SwitchHosts:新增「本地 hosts」方案指向该文件即可(probe/refresh 自动刷新它)")
    print("注意:与 ghspeed 自身的 hosts 钉选二选一,别同时启用(两份都钉会互相踩)")


def cmd_git(args):
    if shutil.which("git") is None:
        sys.exit("未找到 git")
    if args.action == "on":
        m = args.mirror or mirror_pool()[0]
        _run(["git", "config", "--global", "--replace-all",
              "url.%shttps://github.com/.insteadOf" % m,
              "https://github.com/"], timeout=20, check=True)
        # push 永远还原成真实地址:insteadOf 只重写 fetch,pushInsteadOf 把 push
        # URL 拉回来 —— clone/fetch 吃镜像,推送不受影响,不用再「push 前先 off」
        _run(["git", "config", "--global", "--replace-all",
              "url.https://github.com/.pushInsteadOf",
              "https://github.com/"], timeout=20, check=True)
        print("已开启:git clone/fetch https://github.com/... 自动改走镜像 %s" % m)
        print("push 自动走真实地址(pushInsteadOf 已配),无需先 off;还原:ghspeed git off")
    else:
        out = _run(
            ["git", "config", "--global", "--get-regexp",
             r"^url\..*insteadof$"],   # 同时覆盖 insteadof / pushinsteadof
            timeout=20, capture_output=True, text=True).stdout
        n = 0
        for line in out.splitlines():
            key, _, val = line.partition(" ")
            if val.strip() == "https://github.com/":
                _run(["git", "config", "--global", "--unset-all", key],
                     timeout=20, check=True)
                print("已移除:%s" % key)
                n += 1
        print("已关闭 git 镜像加速" if n else "本来就没有开启")


# ---------------- ssh443:git 走官方备用 SSH 口 ----------------

SSH_MARK_S = "# >>> ghspeed ssh443 >>>"
SSH_MARK_E = "# <<< ghspeed ssh443 <<<"
SSH_BLOCK = "\n".join([
    SSH_MARK_S,
    "Host github.com",
    "  HostName ssh.github.com",
    "  Port 443",
    "  User git",
    "  StrictHostKeyChecking accept-new",
    SSH_MARK_E]) + "\n"


def ssh_config_file():
    return os.path.join(os.path.expanduser("~"), ".ssh", "config")


def ssh_read_config():
    try:
        with open(ssh_config_file(), encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def ssh_write_config(text):
    p = ssh_config_file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".ghspeed.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _ssh_try(*target):
    """ssh -T 一把;GitHub 认证成功时故意返回 1,所以看输出不看退出码"""
    cmd = ["ssh", "-T", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=accept-new"] + list(target)
    try:
        r = _run(cmd, timeout=30, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("未找到 ssh 命令(Windows 请在「可选功能」里装 OpenSSH 客户端)")
    except Exception:
        r = None
    out = ((r.stdout or "") + (r.stderr or "")).strip() if r else ""
    ok = bool(re.search(r"Hi \S+!|successfully authenticated", out))
    tail = out.splitlines()[-1] if out else "超时/拒绝"
    print("  %-28s %s" % (" ".join(target), "通(%s)" % tail))
    return ok


def cmd_ssh443(args):
    if args.action == "test":
        print("SSH 通道测试(443 口不走 TLS,坏窗口里 HTTPS 被掐时的大概率活路):")
        ssh_ok = _ssh_try("-p", "443", "git@ssh.github.com")
        normal_ok = _ssh_try("git@github.com")
        print()
        if ssh_ok:
            print("✅ ssh.github.com:443 可用 → ghspeed ssh443 on 一键启用,"
                  "git 推拉自动改走 443 口")
        elif normal_ok:
            print("22 口可用、443 口不通,目前不必开启 ssh443")
        else:
            print("❌ 两个 SSH 口都不通;稍后再试,或临时用镜像:ghspeed git on")
        return

    cfg = ssh_read_config()
    if args.action == "on":
        outside = re.sub(re.escape(SSH_MARK_S) + r".*?" + re.escape(SSH_MARK_E),
                         "", cfg, flags=re.S)
        if re.search(r"(?mi)^\s*Host\s+\S*github", outside):
            print("⚠️  %s 里已有你自己的 github.com 配置,ghspeed 不越权改动;" % ssh_config_file())
            print("   请先手工合并,或注释掉那段后再运行 ghspeed ssh443 on")
            return
        if SSH_MARK_S in cfg:
            new = re.sub(re.escape(SSH_MARK_S) + r".*?" + re.escape(SSH_MARK_E),
                         SSH_BLOCK.strip(), cfg, flags=re.S)
        else:
            new = (cfg.rstrip("\n") + "\n\n" if cfg.strip() else "") + SSH_BLOCK
        ssh_write_config(new)
        journal("ssh443-on")
        print("已写入 %s:github.com 的 SSH 改走 ssh.github.com:443" % ssh_config_file())
        print("验证:ghspeed ssh443 test;还原:ghspeed ssh443 off")
    else:
        if SSH_MARK_S in cfg:
            new = re.sub(re.escape(SSH_MARK_S) + r".*?" + re.escape(SSH_MARK_E)
                         + r"\n?", "", cfg, flags=re.S)
            ssh_write_config(new)
            journal("ssh443-off")
            print("已移除 ssh443 配置,git 恢复默认 22 口")
        else:
            print("本来就没有开启")


# ---------------- 本地代理:只检测 + 给接管命令,不常驻不劫持 ----------------


def detect_proxies(timeout=0.8):
    """并发探测本机常见代理端口;CONNECT 通过才算数(SOCKS 口不认)"""
    def t(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
                s.settimeout(3.0)
                s.sendall(b"CONNECT github.com:443 HTTP/1.0\r\n\r\n")
                if b" 200" in s.recv(64):
                    return "http://127.0.0.1:%d" % port
        except Exception:
            pass
        return None

    with cf.ThreadPoolExecutor(len(PROXY_PORTS)) as ex:
        return [p for p in ex.map(t, PROXY_PORTS) if p]


PROXY_STATE_FILE = os.path.join(os.path.expanduser("~"), ".ghspeed", "proxy.json")


def proxy_persisted():
    """一键接管的本机代理(仅 GitHub 域用);没接管过返回 None"""
    try:
        with open(PROXY_STATE_FILE) as f:
            return (json.load(f).get("proxy") or "").strip() or None
    except Exception:
        return None


def _proxy_save(p):
    os.makedirs(os.path.dirname(PROXY_STATE_FILE), exist_ok=True)
    tmp = PROXY_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"proxy": p, "time": time.time()}, f)
    os.replace(tmp, PROXY_STATE_FILE)


def _proxy_clear():
    try:
        os.remove(PROXY_STATE_FILE)
    except FileNotFoundError:
        pass


def _sni_reapply_pac():
    """SNI 分片开着就重发一次 PAC(让 proxy 跳的增减立即生效);没开返回 False"""
    m = sni_mod()
    if not (m and sni_state().get("enabled")):
        return False
    try:
        m.sni_pac_on()
        return True
    except Exception:
        return False


def cmd_proxy(args):
    action = getattr(args, "action", None)
    if action == "on":
        return _proxy_on()
    if action == "off":
        return _proxy_off()
    return _proxy_detect()


def _proxy_on():
    """一键接管:只把 GitHub 域交给本机代理,其他网站一概不碰,off 原样还原。

    接什么(三处,全部 GitHub 域级):
      git   http.https://github.com/.proxy   —— clone/fetch/push/ls-remote 走代理
      dl    下载通道链最后一级自动追加 PROXY  —— 镜像+直连都失败才用它
      PAC   SNI 分片开着时,relay 后面加代理跳(relay 死了浏览器自动落到代理)
    """
    found = detect_proxies()
    if not found:
        sys.exit("未发现可用的本地 HTTP 代理口(SOCKS 口不认;Clash 开「混合端口」后重试)")
    p = found[0]
    git_on = shutil.which("git") is not None
    if git_on:
        _run(["git", "config", "--global", "--replace-all",
              "http.https://github.com/.proxy", p], timeout=20, check=True)
    _proxy_save(p)
    pac_done = _sni_reapply_pac()
    journal("proxy-on", p)
    print("已接管(仅 GitHub):%s ✅" % p)
    if git_on:
        print("  - git:clone/fetch/push 走代理(取消:ghspeed proxy off)")
    else:
        print("  - git:未安装 git,此项跳过")
    print("  - dl:镜像+直连都失败时自动经它下载")
    print("  - PAC:%s" % ("已加代理跳(relay 死了浏览器自动落到它)"
                         if pac_done else "SNI 分片未开,跳过(sni on 后再 proxy on 会自动带上)"))


def _proxy_off():
    p = proxy_persisted()
    git_on = shutil.which("git") is not None
    removed_git = False
    if git_on:
        r = _run(["git", "config", "--global", "--unset-all",
                  "http.https://github.com/.proxy"],
                 timeout=20, check=False, capture_output=True, text=True)
        removed_git = r.returncode == 0
    _proxy_clear()
    pac_done = _sni_reapply_pac()
    if not p and not removed_git:
        print("本来就没有接管")
        return
    journal("proxy-off", p or "")
    print("已还原(仅 GitHub 不再走 %s)✅" % (p or "本机代理"))
    if git_on:
        print("  - git:代理配置%s" % ("已移除" if removed_git else "本来就没配"))
    if pac_done:
        print("  - PAC:代理跳已摘除")


def _proxy_detect():
    print("探测本机常见代理端口:%s" % ", ".join(map(str, PROXY_PORTS)))
    p = proxy_persisted()
    if p:
        print("当前状态:已接管(仅 GitHub → %s;还原:ghspeed proxy off)" % p)
    found = detect_proxies()
    if not found:
        print("未发现可用的本地 HTTP 代理口(SOCKS 口不认;Clash 开「混合端口」即可)")
        print("知道端口的话可以直接用:")
        print("  GHSPEED_PROXY=http://127.0.0.1:端口 ghspeed dl <url>")
        return
    print("可用代理:")
    for q in found:
        print("  + %s" % q)
    if not p:
        print("\n一键接管(只影响 GitHub,不动其他网站;off 原样还原):")
        print("  ghspeed proxy on")


# ---------------- 系统 DNS:opt-in 全局接管,off 原样还原 ----------------

DNS_CLEAN = ["223.5.5.5", "119.29.29.29"]   # 国内不投毒公共 DNS(阿里/腾讯)


def dns_backup_file():
    return os.path.join(os.path.expanduser("~"), ".ghspeed", "dns_backup.json")


def _mac_dns_services():
    r = _run(["networksetup", "-listallnetworkservices"], timeout=15,
             capture_output=True, text=True)
    out = []
    for line in (r.stdout or "").splitlines()[1:]:
        line = line.strip()
        if line and not line.startswith("*"):   # * 开头 = 已禁用的服务
            out.append(line)
    return out


def cmd_dns(args):
    if IS_LINUX:
        sys.exit("Linux 的 DNS 管理各家发行版差异大,请手工改 /etc/resolv.conf 或 "
                 "systemd-resolved;建议就填:%s" % " ".join(DNS_CLEAN))
    if args.action == "show":
        print("接管状态:%s" % ("已接管(dns off 可取消)" if dns_is_on() else "未接管"))
        for svc, cur in _dns_current():
            print("  %-24s %s" % (svc, " ".join(cur) if cur else "(自动获取/DHCP)"))
        print("\n备份文件:%s" % dns_backup_file(), end="")
        if os.path.exists(dns_backup_file()):
            print("(存在,dns off 会按它还原)")
        elif IS_MAC:
            print("(本身份下没有;不管备份落在哪,dns off 都能取消接管)")
        else:
            print("(不存在,说明没接管过)")
        return
    need_root()   # 改系统 DNS 要管理员;show 不用

    items = _dns_current()
    if args.action == "on":
        # 幂等:已接管的服务保留旧备份里的原值,其余现取快照——重复接管不再报错
        taken = {s for s, cur in items if cur and set(DNS_CLEAN) & set(cur)}
        _, old = _dns_load_backup()
        backup = [{"svc": s, "dns": (old.get(s) if s in taken else cur)}
                  for s, cur in items]
        os.makedirs(os.path.dirname(dns_backup_file()), exist_ok=True)
        with open(dns_backup_file(), "w") as f:
            json.dump({"time": time.time(), "platform": _platform.system(),
                       "items": backup}, f, ensure_ascii=False, indent=1)
        for p in _dns_backup_paths():   # 备份统一收敛到当前身份这一份
            if p != dns_backup_file():
                try:
                    os.remove(p)
                except Exception:
                    pass
        ok_n, fail = 0, []
        for s, _ in items:
            good, err = _dns_apply(s, DNS_CLEAN)
            if good:
                ok_n += 1
            else:
                fail.append("%s:%s" % (s, err))
        journal("dns-on", "%d/%d" % (ok_n, len(items)))
        if ok_n == 0:
            try:
                os.remove(dns_backup_file())
            except Exception:
                pass
            sys.exit("一个服务都没改成;" + ";".join(fail or ["未知原因"]))
        print("%s已把系统 DNS 换成 %s(%d/%d 个服务;原配置备份在 %s)" % (
            "(已在接管中,本次为幂等重刷) " if taken and taken == {s for s, _ in items}
            else "", " ".join(DNS_CLEAN), ok_n, len(items), dns_backup_file()))
        if fail:
            print("没改成的服务:%s" % ";".join(fail))
        print("影响全部网站(不只 GitHub);随时取消:ghspeed dns off")
    else:
        # 取消接管:有备份按备份还原;没备份(或没覆盖到)就把我们的 DNS 清扫回自动获取
        # —— 永远可取消,不存在"备份丢了就没法还原"的死路
        _, old = _dns_load_backup()
        items = _dns_current()
        n_rest, n_swept, fail = 0, 0, []
        for s, cur in items:
            still_ours = bool(cur and set(DNS_CLEAN) & set(cur))
            if s in old:
                good, err = _dns_apply(s, old[s])
            elif still_ours:
                good, err = _dns_apply(s, None)   # 没有原值 → 回自动获取
            else:
                continue
            if good:
                n_rest, n_swept = (n_rest + 1, n_swept) if s in old else (n_rest, n_swept + 1)
            else:
                fail.append("%s:%s" % (s, err))
        if not old and not n_rest and not n_swept:
            print("本来就未接管(当前 DNS 里没有 %s),无需还原" % "/".join(DNS_CLEAN))
            return
        for p in _dns_backup_paths():
            try:
                os.remove(p)
            except Exception:
                pass
        journal("dns-off", "%d还原/%d清扫" % (n_rest, n_swept))
        print("已取消接管:还原 %d 个 + 清扫 %d 个 → 自动获取" % (n_rest, n_swept))
        if fail:
            print("没动成的:%s(可再跑一次 dns off)" % ";".join(fail))


def _dns_backup_paths():
    """备份可能落的位置:当前身份的家 + (mac) 控制台用户家 + /var/root。
    osascript 提权不改 HOME、sudo 反之——备份在谁家不该被赌,全都找一遍"""
    out = [dns_backup_file()]
    if IS_MAC:
        extra = ["/var/root/.ghspeed/dns_backup.json"]
        try:
            import pwd
            home = pwd.getpwuid(os.stat("/dev/console").st_uid).pw_dir
            extra.insert(0, os.path.join(home, ".ghspeed", "dns_backup.json"))
        except Exception:
            pass
        for p in extra:
            if p not in out:
                out.append(p)
    return out


def _dns_load_backup():
    """第一个可读的备份 → (路径, {服务: 原 DNS 或 None});找不到 → (None, {})"""
    for p in _dns_backup_paths():
        try:
            with open(p) as f:
                items = json.load(f).get("items", [])
            if items:
                return p, {it["svc"]: it["dns"] for it in items}
        except Exception:
            continue
    return None, {}


_DNS_CACHE = {"t": 0.0, "items": []}


def _dns_current(force=False):
    """[(服务/适配器名, 当前 DNS 列表或 None=自动获取)]

    Windows 上要拉 PowerShell,一次 ~2 秒;界面每次刷新都会问状态,
    加个 5 秒缓存避免反复起进程(改 DNS 后会被强制失效)。"""
    if not force and time.time() - _DNS_CACHE["t"] < 5:
        return _DNS_CACHE["items"]
    out = []
    if IS_MAC:
        for svc in _mac_dns_services():
            r = _run(["networksetup", "-getdnsservers", svc], timeout=15,
                     capture_output=True, text=True)
            txt = (r.stdout or "").strip()
            if txt.startswith("There aren't any DNS Servers"):
                out.append((svc, None))
            else:
                out.append((svc, txt.split()))
    elif IS_WIN:
        ps = ("Get-DnsClientServerAddress -AddressFamily IPv4 | "
              "Where-Object {$_.ServerAddresses} | "
              "Select-Object InterfaceAlias,@{n='d';e={$_.ServerAddresses -join ','}} | "
              "ConvertTo-Json -Compress")
        try:
            r = _run(["powershell", "-NoProfile", "-NonInteractive",
                      "-Command", ps], timeout=20,
                     capture_output=True, text=True)
            data = json.loads(r.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            for it in data or []:
                out.append((it["InterfaceAlias"], it["d"].split(",")))
        except Exception:
            pass
    _DNS_CACHE["t"], _DNS_CACHE["items"] = time.time(), out
    return out


def _dns_apply(svc, servers):
    """写 DNS(不回读校验);返回 (是否成功, 错误摘要)——失败别再无声吞掉"""
    _DNS_CACHE["t"] = 0.0   # 缓存失效,下次问系统真实态
    if IS_MAC:
        cmd = ["networksetup", "-setdnsservers", svc] + (servers or ["Empty"])
        try:
            r = _run(cmd, timeout=20, capture_output=True, text=True)
        except Exception as e:
            return False, str(e)[:80]
        if r.returncode != 0:
            return False, ((r.stderr or r.stdout or "").strip()
                           or "rc=%d" % r.returncode)[:80]
        return True, ""
    if IS_WIN:
        ps = ("Set-DnsClientServerAddress -InterfaceAlias '%s' " % svc.replace("'", "''")
              + ("-ServerAddresses (%s)" % ",".join("'%s'" % d for d in servers)
                 if servers else "-ResetServerAddresses"))
        try:
            r = _run(["powershell", "-NoProfile", "-NonInteractive",
                      "-Command", ps], timeout=25,
                     capture_output=True, text=True)
        except Exception as e:
            return False, str(e)[:80]
        if r.returncode != 0:
            return False, ((r.stderr or "").strip() or "rc=%d" % r.returncode)[:80]
        return True, ""
    return False, "unsupported platform"


def dns_is_on():
    """真实接管状态:任一服务/适配器的 DNS 含我们的服务器。
    读系统态而非备份文件——备份可能落在 root 家或用户家,文件在哪不该决定 UI 状态"""
    for _s, cur in _dns_current():
        if cur and set(DNS_CLEAN) & set(cur):
            return True
    return False


# ---------------- 浏览器 SNI 分片(可选插件:ghspeed_sni.py,按需懒加载)----------------
# v0.5.1 起分拆成独立插件文件:非核心功能不占引擎主文件的维护心智。
# 首次用到才加载,并把引擎命名空间注入进去(插件内零改动直用引擎设施);
# 文件缺失时优雅降级——状态照常显示「未开启」,只有显式 on/test 才提示
# 安装不完整,核心功能分毫不差。

_SNI_MOD = []


def _sni_module_path():
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, "ghspeed_sni.py"),
             "/usr/local/share/ghspeed/ghspeed_sni.py"]
    try:   # PyInstaller onefile:引擎打进 exe,插件在解包目录
        cands.insert(1, os.path.join(sys._MEIPASS, "ghspeed_sni.py"))
    except Exception:
        pass
    for p in cands:
        if os.path.exists(p):
            return p
    return None


def sni_mod():
    """懒加载 SNI 插件;找不到/加载失败返回 None(调用方按未开启降级)"""
    if _SNI_MOD:
        return _SNI_MOD[0]
    path = _sni_module_path()
    if not path:
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ghspeed_sni", path)
        m = importlib.util.module_from_spec(spec)
        for k, v in list(globals().items()):   # 先注入再执行:插件顶层
            m.__dict__.setdefault(k, v)        # 常量(_SNI_DIR 等)要用引擎设施
        m.__dict__["_ENGINE_FILE"] = os.path.realpath(__file__)   # 插件回启引擎用(spawn 命令行是以引擎为入口的)
        spec.loader.exec_module(m)
    except Exception:
        if os.environ.get("GHSPEED_DEBUG"):
            import traceback
            traceback.print_exc()
        return None
    _SNI_MOD.append(m)
    return m


def _sni_stub():
    return {"enabled": False, "relay": False, "strategy": "-"}


def sni_state():
    m = sni_mod()
    return m.sni_state() if m else _sni_stub()


def sni_off(quiet=False):
    m = sni_mod()
    return m.sni_off(quiet) if m else False


def sni_heal(quiet=False):
    m = sni_mod()
    r = m.sni_heal(quiet) if m else False
    if m:   # 下载档位开着但服务死了就一起复活(relay 复活是主体,tier 只是加分项)
        try:
            m.tier_heal(quiet)
        except SystemExit:
            pass   # tier 侧的业务提示(端口占用等)不该打断 heal 主流程
    return r


def tier_state():
    m = sni_mod()
    if not m:
        return {"on": False, "alive": False, "ca": False, "port": 0}
    return m.tier_state()


def cmd_tier(args):
    m = sni_mod()
    if not m:
        if getattr(args, "action", None) in (None, "state", "off"):
            print("下载档位:未开启(SNI 插件缺失时不影响开关加速等其他功能)")
            return
        sys.exit("SNI 插件缺失:没找到 ghspeed_sni.py(重新安装最新版可修复)")
    m.cmd_tier(args)


def cmd_sni(args):
    m = sni_mod()
    if not m:
        if args.action in ("state", "off"):
            print("SNI 分片:未开启(插件文件 ghspeed_sni.py 缺失,重装可修复;"
                  "不影响其他功能)")
            return
        sys.exit("SNI 插件缺失:没找到 ghspeed_sni.py(重新安装最新版可修复)")
    m.cmd_sni(args)


def cmd_live(args):
    conns = live_connections()
    if not conns:
        print("当前没有正在进行的 GitHub 连接。")
        print("(hosts 钉选是系统级被动命中,你一访问 GitHub 就自动生效,无需常驻)")
        return
    total = sum(n for _, n in conns)
    print("正在吃加速度的 GitHub 连接(远端 IP 来自 ghspeed 钉选):共 %d 条" % total)
    for name, n in conns:
        print("  %-28s %d 条" % (name, n))
    print("\nghspeed 自身动作日志:ghspeed activity")


def cmd_activity(args):
    entries = journal_tail(20)
    if not entries:
        print("还没有动作记录(下载/测速/开关加速/ssh443/DNS 时自动记录)")
        return
    print("最近 %d 条 ghspeed 动作:" % len(entries))
    for e in entries:
        t = time.strftime("%m-%d %H:%M:%S", time.localtime(e.get("t", 0)))
        print("  %s  %-9s %s" % (t, e.get("action", ""), e.get("detail", "")))


# ---------------- 自更新:检查 / 下载 / 校验 / 打开安装包 ----------------


def _ver_tuple(s):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(s or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _update_mirror_bases(limit=4):
    """查更新用的镜像前缀:只吃已有结论(缓存池 + 静态种子 + 自建),绝不现场
    验证——查更新必须快,不能为了一个 HEAD 把几十个镜像全测一遍。"""
    out = []
    try:
        out += _pool_urls(dyn_load().get("mirrors") or [])
    except Exception:
        pass
    out += list(MIRRORS) + custom_mirrors()
    seen, res = set(), []
    for u in out:
        u = (u or "").strip().rstrip("/") + "/"
        if u and u not in seen:
            seen.add(u)
            res.append(u)
    return res[:limit]


def _fetch_final(url, timeout=10):
    """抓文本 + 最终 URL(不跟丢重定向:releases/latest 靠它拿 tag)"""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl_context()) as r:
        return r.read(1 << 20).decode("utf-8", "replace"), r.geturl()


def _release_info_from_api(timeout=10):
    d = json.loads(fetch_url(UPDATE_API, timeout=timeout))
    tag = (d.get("tag_name") or "").strip()
    if not tag:
        raise RuntimeError("API 没有返回 tag_name")
    assets = []
    for a in d.get("assets") or []:
        dig = (a.get("digest") or "").strip().lower()
        assets.append({"name": a.get("name") or "",
                       "url": a.get("browser_download_url") or "",
                       "size": int(a.get("size") or 0),
                       "sha256": dig.split(":", 1)[-1] if dig else ""})
    return {"tag": tag, "assets": assets, "source": "api.github.com"}


def _release_assets_from_web(tag, timeout=10):
    """API 够不着时的资产清单退路:GitHub 的 expanded_assets 片段是静态 HTML"""
    for base in [""] + _update_mirror_bases(limit=2):
        url = "%s/%s/releases/expanded_assets/%s" % (
            base.rstrip("/") if base else "https://github.com", UPDATE_REPO, tag)
        try:
            html, _ = _fetch_final(url, timeout=timeout)
        except Exception:
            continue
        names = re.findall(r"/releases/download/[^\"'<>]+/([^\"'<>/]+)", html)
        if not names:
            continue
        seen, assets = set(), []
        for n in names:
            if n in seen:
                continue
            seen.add(n)
            assets.append({"name": n,
                           "url": "https://github.com/%s/releases/download/%s/%s"
                                  % (UPDATE_REPO, tag, n),
                           "size": 0, "sha256": ""})
        return assets
    return []


def _release_info_from_web(timeout=10):
    """API 被掐时的退路:releases/latest 的 302 指向 tag,资产页再拿文件名"""
    errs = []
    for base in [""] + _update_mirror_bases():
        url = (base + UPDATE_WEB + "/latest") if base else (UPDATE_WEB + "/latest")
        try:
            html, final = _fetch_final(url, timeout=timeout)
        except Exception as e:
            errs.append("%s:%s" % (urlparse(url).netloc or "直连", str(e)[:40]))
            continue
        m = (re.search(r"/releases/tag/([^/\"'?#<>]+)", final or "")
             or re.search(r"/releases/tag/([^/\"'?#<>]+)", html))
        if not m:
            errs.append("%s:没找到版本号" % (urlparse(url).netloc or "直连"))
            continue
        tag = unquote(m.group(1)).strip()
        return {"tag": tag,
                "assets": _release_assets_from_web(tag, timeout),
                "source": urlparse(url).netloc or "github.com"}
    raise RuntimeError("; ".join(errs[:3]) or "没有可用通道")


def _update_asset_names(tag):
    v = tag.lstrip("vV")
    if IS_MAC:
        # pkg 优先(双击即装、装完自动拉起);zip 是免安装兜底
        return ["GHSpeed-macos-%s.pkg" % v, "GHSpeed-macos-%s.zip" % tag]
    if IS_WIN:
        return ["GHSpeed-windows-setup-%s.exe" % tag,
                "GHSpeed-windows-%s.zip" % tag]
    return ["ghspeed_%s_all.deb" % v]


def _pick_asset(tag, assets):
    names = _update_asset_names(tag)
    by = {a.get("name"): a for a in (assets or [])}
    for n in names:
        if n in by and by[n].get("url"):
            return by[n]
    if assets:   # 有清单但没有当前平台的包:不瞎猜,交给上层报错
        return None
    # 清单没拿到:按命名规则拼,404 时 dl 会明确报错,不会装错东西
    return {"name": names[0],
            "url": "https://github.com/%s/releases/download/%s/%s"
                   % (UPDATE_REPO, tag, names[0]),
            "size": 0, "sha256": ""}


def update_check_info(force=True, ttl=UPDATE_CACHE_TTL):
    """检查新版。force=False 时吃 6h 缓存(界面启动自动查不重复打网络)"""
    if not force:
        try:
            with open(UPDATE_CHECK_FILE) as f:
                cached = json.load(f)
            if cached and time.time() - cached.get("ts", 0) < ttl:
                cached["cached"] = True
                return cached
        except Exception:
            pass
    try:
        info = _release_info_from_api()
    except Exception as e1:
        try:
            info = _release_info_from_web()
        except Exception as e2:
            raise RuntimeError("检查更新失败:%s / %s"
                               % (str(e1)[:60], str(e2)[:80]))
    tag = info["tag"]
    doc = {"ts": time.time(), "current": __version__,
           "latest": tag.lstrip("vV"), "tag": tag,
           "update": _ver_tuple(tag) > _ver_tuple(__version__),
           "source": info.get("source") or "",
           "asset": _pick_asset(tag, info.get("assets") or []),
           "notes": "%s/tag/%s" % (UPDATE_WEB, tag)}
    try:
        os.makedirs(os.path.dirname(UPDATE_CHECK_FILE), exist_ok=True)
        with open(UPDATE_CHECK_FILE + ".tmp", "w") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(UPDATE_CHECK_FILE + ".tmp", UPDATE_CHECK_FILE)
    except Exception:
        pass
    return doc


def _update_sums(tag):
    """SHA256SUMS.txt(经直连/镜像取);没有就返回 {},上层降级为不校验并提示"""
    u = ("https://github.com/%s/releases/download/%s/SHA256SUMS.txt"
         % (UPDATE_REPO, tag))
    for base in [""] + _update_mirror_bases(limit=2):
        try:
            txt = fetch_url((base + u) if base else u, timeout=10)
        except Exception:
            continue
        sums = {}
        for line in txt.splitlines():
            p = line.split()
            if len(p) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", p[0]):
                sums[p[1].lstrip("*")] = p[0].lower()
        if sums:
            return sums
    return {}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _update_download(info):
    asset = info.get("asset") or {}
    name = asset.get("name") or ""
    dldir = os.path.join(os.path.expanduser("~"), "Downloads")
    try:
        os.makedirs(dldir, exist_ok=True)
    except OSError:
        dldir = os.path.join(os.path.expanduser("~"), ".ghspeed")
        os.makedirs(dldir, exist_ok=True)
    out = os.path.join(dldir, name)
    expected = (asset.get("sha256") or "").lower()
    if not expected:
        expected = _update_sums(info["tag"]).get(name, "")
    if os.path.exists(out) and expected and _sha256_file(out) == expected:
        print("安装包已在 %s,校验通过,不重复下载" % out)
    else:
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        cmd_dl(types.SimpleNamespace(url=asset["url"], output=out,
                                     mirror=None, direct=False,
                                     min_speed=25.0, proxy=None))
        if not os.path.exists(out):
            sys.exit("下载未完成:%s" % out)
        if not expected:
            print("⚠ 该版本没提供 sha256 校验和,已跳过完整性校验")
        else:
            got = _sha256_file(out)
            if got != expected:
                try:
                    os.remove(out)
                except OSError:
                    pass
                sys.exit("sha256 校验失败(下载件已删除):期望 %s… 实际 %s…"
                         % (expected[:12], got[:12]))
            print("sha256 校验通过 ✅")
    journal("update", "%s → v%s" % (name, info["latest"]))
    return out


def _update_open(path):
    if IS_MAC:
        r = _run(["/usr/bin/open", path], timeout=30, check=False)
        if r.returncode == 0:
            print("已打开安装器:按提示完成安装(会替换 /Applications/GHSpeed.app)")
            print("安装完成后,请先退出旧版菜单栏图标(🐙 → 退出)再重新打开。")
        else:
            print("没能自动打开,请手动双击:%s" % path)
    elif IS_WIN:
        try:
            os.startfile(path)
            print("已启动安装器:按提示完成安装(UAC 选「是」)。")
        except Exception as e:
            print("没能自动打开(%s),请手动运行:%s" % (e, path))
    else:
        print("请手动安装:sudo dpkg -i %s" % path)


def cmd_update(args):
    try:
        info = update_check_info(force=not getattr(args, "cache", False))
    except Exception as e:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            sys.exit(2)
        sys.exit(str(e))
    if getattr(args, "json", False):
        print(json.dumps(dict(info, ok=True), ensure_ascii=False))
        return
    print("当前版本:v%s" % info["current"])
    print("最新版本:%s(%s)" % (info["tag"], info.get("source") or "GitHub"))
    if not info["update"]:
        print("已是最新版本 ✅")
        return
    asset = info.get("asset") or {}
    print("发现新版 🎉")
    if asset.get("size"):
        print("  安装包:%s(%s)" % (asset["name"], human(asset["size"])))
    print("  发布说明:%s" % info["notes"])
    if getattr(args, "check", False):
        print("\n升级:ghspeed update(经镜像通道下载,校验后自动打开安装包)")
        return
    if not asset.get("url"):
        sys.exit("这个版本没有适配当前平台的安装包,请到 %s 手动下载"
                 % info["notes"])
    if not getattr(args, "yes", False):
        try:
            ans = input("\n下载并打开安装包? [Y/n] ").strip().lower()
        except EOFError:
            sys.exit("非交互环境请加 --yes")
        if ans not in ("", "y", "yes"):
            print("已取消")
            return
    out = _update_download(info)
    print("\n新版已就绪 → %s" % out)
    _update_open(out)


def cmd_install(args):
    need_root()
    src = os.path.realpath(__file__)
    if IS_WIN:
        dst_dir = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                               "ghspeed")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "ghspeed.py")
        shutil.copy2(src, dst)
        bat = os.path.join(dst_dir, "ghspeed.bat")
        with open(bat, "w", encoding="ascii") as f:
            f.write('@echo off\r\npython "%s" %%*\r\n' % dst)
        print("已安装到 %s" % dst_dir)
        print("如需任意位置直接敲 ghspeed,把该目录加入 PATH(可选)")
        print("下一步(管理员终端):  ghspeed on")
    else:
        os.makedirs("/usr/local/bin", exist_ok=True)
        dst = "/usr/local/bin/ghspeed"
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        print("已安装到 %s,以后直接用 ghspeed 命令" % dst)
        print("下一步:  sudo ghspeed on")


def main():
    ap = argparse.ArgumentParser(
        prog="ghspeed", description="GitHub 专用加速工具(只碰 GitHub,不碰其他网络)")
    ap.add_argument("--version", action="version",
                    version="ghspeed " + __version__ + " (macOS/Windows/Linux)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("on", help="启用:测速 + 固定最快 IP 到 hosts + 装定时刷新(root)").set_defaults(func=cmd_on)
    sub.add_parser("off", help="停用:还原 hosts + 卸载定时任务(root)").set_defaults(func=cmd_off)
    sub.add_parser("status", help="查看当前状态与实测延迟").set_defaults(func=cmd_status)
    sub.add_parser("probe", help="只测速,不写 hosts").set_defaults(func=cmd_probe)
    sub.add_parser("install", help="把本脚本安装为 /usr/local/bin/ghspeed(root)").set_defaults(func=cmd_install)

    p = sub.add_parser("mirrors", help="检查各下载镜像的健康度(带跨会话记忆评分);"
                                       "add/remove<url> 自建镜像入池")
    p.add_argument("op", nargs="?", choices=["add", "remove"], metavar="add|remove")
    p.add_argument("mirror_url", nargs="?", metavar="https://你的Worker域名/")
    p.add_argument("--export", nargs="?", const="mirrors.json", metavar="FILE",
                   help="把实测通过的镜像池导出为 JSON(默认 ./mirrors.json)")
    p.set_defaults(func=cmd_mirrors)

    p = sub.add_parser("url", help="把 GitHub 链接展开为直连+各镜像前缀的等价下载链接")
    p.add_argument("url", help="GitHub 链接,或 owner/repo[@分支或tag] 整仓 zip")
    p.set_defaults(func=cmd_url)

    p = sub.add_parser("hosts-export", help="把测速结果导出为 GitHub520 兼容 hosts 文件(SwitchHosts 可直接指向)")
    p.add_argument("output", nargs="?", help="输出路径(默认 ~/.ghspeed/hosts,probe/refresh 自动刷新)")
    p.set_defaults(func=cmd_hosts_export)

    p = sub.add_parser("refresh", help="重新测速并刷新(定时任务调用)")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("watch", help="坏窗口自愈守护:健康即退,坏窗口内加密重测(定时任务调用)")
    p.add_argument("--budget", type=int, default=600, metavar="秒",
                   help="本轮守护预算(默认 600;定时任务装的是 900)")
    p.add_argument("--interval", type=int, default=60, metavar="秒",
                   help="坏窗口内复查间隔(默认 60)")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("dl", help="经镜像下载 GitHub 文件(支持断点续传)")
    p.add_argument("url", help="GitHub 链接,或 owner/repo[@分支或tag] 整仓速下(zip)")
    p.add_argument("-o", "--output", help="保存路径(默认取 URL 文件名)")
    p.add_argument("--mirror", help="指定镜像前缀")
    p.add_argument("--direct", action="store_true", help="不走镜像,只直连")
    p.add_argument("--proxy", help="本地 HTTP 代理兜底,如 http://127.0.0.1:7890"
                   "(也可用环境变量 GHSPEED_PROXY)")
    p.add_argument("--min-speed", type=float, default=25.0, metavar="KB/S",
                   help="平均速度低于此值就自动换下一个通道(0=不检查,默认 25)")
    p.set_defaults(func=cmd_dl)

    p = sub.add_parser("git", help="git clone 走镜像(可选,默认不开)")
    p.add_argument("action", choices=["on", "off"])
    p.add_argument("--mirror", help="指定镜像前缀")
    p.set_defaults(func=cmd_git)

    p = sub.add_parser("ssh443", help="git 改走 ssh.github.com:443 官方备用口(HTTPS 被掐时的活路)")
    p.add_argument("action", choices=["on", "off", "test"])
    p.set_defaults(func=cmd_ssh443)

    p = sub.add_parser("proxy", help="本机代理:无参只探测+看接管状态;on 一键接管(仅 GitHub);off 还原")
    p.add_argument("action", nargs="?", choices=["on", "off"], help="缺省=只探测")
    p.set_defaults(func=cmd_proxy)

    p = sub.add_parser("tier", help="本地下载档位:浏览器无感多通道下载(下载域选择性改写镜像;tier on 后先 sni on)")
    p.add_argument("action", nargs="?", choices=["on", "off", "state", "heal", "serve"], help="缺省=state")
    p.add_argument("--with-sni", action="store_true",
                   help="tier on 时顺带开启 SNI 分片(PAC 才会把下载域交给档位)")
    p.set_defaults(func=cmd_tier)

    sub.add_parser("live", help="看当前谁正连着钉选 IP(请求吃到加速的实时视图)"
                   ).set_defaults(func=cmd_live)

    sub.add_parser("activity", help="回放 ghspeed 自身动作日志(下载/刷新/开关)"
                   ).set_defaults(func=cmd_activity)

    p = sub.add_parser("update", help="检查/升级到新版本(经镜像通道下载,sha256 校验后打开安装包)")
    p.add_argument("--check", action="store_true", help="只检查,不下载")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p.add_argument("--json", action="store_true", help="机器可读输出(界面调用)")
    p.add_argument("--cache", action="store_true", help="允许使用 6 小时内的检查缓存")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("dns", help="系统 DNS 换成干净公共 DNS(opt-in,影响全部网站;off 原样还原)")
    p.add_argument("action", choices=["on", "off", "show"])
    p.set_defaults(func=cmd_dns)

    p = sub.add_parser("sni", help="浏览器 SNI 分片(可选):ClientHello 拆散绕过识别,仅 GitHub 域走本地 relay;任何关闭都原样还原")
    p.add_argument("action", choices=["test", "on", "off", "state", "heal", "serve", "git"],
                   nargs="?", default="state")
    p.add_argument("git_action", choices=["on", "off"], nargs="?", default=None,
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_sni)

    if IS_WIN and len(sys.argv) == 1:
        # 双击 ghspeed.exe 时别让黑窗一闪而过:打印帮助后等回车
        try:
            ap.print_help()
            input("\n按回车键退出…")
        except Exception:
            pass
        return
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
