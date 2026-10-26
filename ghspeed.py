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
import json
import os
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
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
        __version__ = _f.read().strip() or "0.4.7"
except Exception:
    __version__ = "0.4.7"

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
]

# DoH(国内可达、解析 GitHub 不投毒的公共 DNS),失败自动跳过
DOH_PROVIDERS = [
    "https://dns.alidns.com/resolve?name={d}&type=A",
    "https://223.5.5.5/resolve?name={d}&type=A",
    "https://120.53.53.53/dns-query?name={d}&type=A",
]

# 下载镜像(ghproxy 系)静态种子:冷启动/发布站也够不着时的保底
MIRRORS = [
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://ghfast.top/",
    "https://gh.llkk.cc/",
]

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
    "https://ghp.ci/",
    "https://ghproxy.cn/",
    "https://ghproxy.cc/",
    "https://gh-proxy.net/",
    "https://ghproxy.homeboyc.cn/",
    # ghproxy.link 社区表里的公共节点(2026-10-26 实测 cors.isteed 活、
    # gh-proxy.isteed.cc 连不上,以 cors 为准;死活最终以入池验证为准)
    "https://cdn.gh-proxy.com/",
    "https://cors.isteed.cc/",
    "https://gh.ddlc.top/",
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


def ssl_context():
    """macOS 上不少 Python(venv/python.org 装的)找不到 CA 根证书,
    导致所有 HTTPS 校验失败;这里显式指定证书包,兼容各种安装方式。"""
    cands = []
    try:
        import certifi
        cands.append(certifi.where())
    except Exception:
        pass
    cands += ["/etc/ssl/cert.pem",                       # macOS 系统证书包
              "/usr/local/etc/openssl@3/cert.pem",
              "/usr/local/etc/openssl/cert.pem",
              "/opt/homebrew/etc/ca-certificates/cert.pem"]
    for ca in cands:
        if os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
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
            r = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                               capture_output=True, text=True, timeout=10)
            pids = {}
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "ESTABLISHED":
                    ip = parts[2].rsplit(":", 1)[0].strip("[]")
                    if ip in ips:
                        pids[parts[4]] = pids.get(parts[4], 0) + 1
            if pids:
                r2 = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                    capture_output=True, text=True, timeout=10)
                for line in r2.stdout.splitlines():
                    cells = [c.strip().strip('"') for c in line.split('","')]
                    if len(cells) >= 2 and cells[1] in pids:
                        agg[cells[0]] = agg.get(cells[0], 0) + pids.pop(cells[1])
            for pid, n in pids.items():   # 查不到名字的留 PID
                agg["PID %s" % pid] = agg.get("PID %s" % pid, 0) + n
        else:
            r = subprocess.run(["lsof", "-i", "TCP", "-n", "-P", "+c", "0"],
                               capture_output=True, text=True, timeout=10)
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


def _pool_urls(entries):
    """dyn["mirrors"] 兼容两代形态:旧 ["url"] / 新 [[url, rtt]](延迟序)→ 出 url 列表"""
    out = []
    for e in entries:
        u = e[0] if isinstance(e, (list, tuple)) else e
        if u not in out:
            out.append(u)
    return out


def mirror_pool(force=False):
    """镜像下单链:静态种子 + 发布站动态候选,**一律真验证、按实测延迟排序**。
    静态默认序只是候选来源,不代表最快——顺序以动态实测为准(快→慢)。
    24h 缓存(连延迟一起存);源够不着/坏窗口全验不过时沿用旧池,永不洗空。"""
    env = os.environ.get("GHSPEED_MIRRORS")
    if env:   # 用户显式指定时完全尊重用户,不掺动态
        return [m.strip().rstrip("/") + "/" for m in env.split(",") if m.strip()]
    dyn = dyn_load()
    cached = dyn.get("mirrors") or []
    now = time.time()
    if not force and cached and now - dyn.get("mirrors_ts", 0) < MIRROR_FEED_TTL:
        return _pool_urls(cached)   # 缓存里存的就是延迟序

    cands, seen = [], set()
    for c in list(MIRRORS) + list(MIRROR_EXTRA_CANDIDATES):   # 静态不再免检
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
    alive = []
    with cf.ThreadPoolExecutor(10) as ex:
        for p, rtt in ex.map(lambda p: (p, validate_mirror(p)), cands[:28]):
            if rtt is not None:
                alive.append((p, rtt))
    alive.sort(key=lambda x: x[1])   # ★ 动态排序:实测延迟快的排前面
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
    if domain in ("github.com", "gist.github.com"):
        ips |= {"140.82.%d.%d" % (n, h) for n in range(112, 122) for h in (3, 4)}
        ips |= {"20.205.243.166", "20.27.177.113", "20.200.245.247",
                "20.26.156.215", "20.248.137.49"}
    elif domain == "api.github.com":
        ips |= {"140.82.%d.%d" % (n, h) for n in range(112, 122) for h in (5, 6)}
    elif domain == "codeload.github.com":
        ips |= {"140.82.%d.%d" % (n, h) for n in range(112, 122) for h in (9, 10)}
    elif domain.endswith("githubusercontent.com"):
        ips |= {"185.199.%d.133" % n for n in (108, 109, 110, 111)}
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


def wave(pairs):
    """并行探测一批 (domain, ip),返回 {domain: (rtt, ip) 中的最优}。

    所有域名共享一个线程池(旧版逐域名各开一个池再串行排队,
    13 个域名要等 13 轮);坏 IP 在 PROBE_TIMEOUT 内出局,不拖全局。
    """
    out = {}

    def one(job):
        d, ip = job
        try:
            return (d, ip, probe(d, ip, timeout=PROBE_TIMEOUT))
        except Exception:
            return (d, ip, None)

    with cf.ThreadPoolExecutor(max_workers=48) as ex:
        for d, ip, rtt in ex.map(one, pairs):
            if rtt is not None and (d not in out or rtt < out[d][0]):
                out[d] = (rtt, ip)
    return out


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

    # 种子轮:上一轮的冠军 IP
    seeds = [(d, prev[d]["ip"]) for d in DOMAINS if prev.get(d, {}).get("ip")]
    if seeds:
        r = wave(seeds)
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
            r = wave([(d, ip) for d in pending for ip in candidates_for(d)])
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
    lines = [MARK_S, "# ghspeed 自动生成,勿手改;" + hint]
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
    if IS_WIN:
        subprocess.run(["ipconfig", "/flushdns"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif IS_MAC:
        subprocess.run(["dscacheutil", "-flushcache"], check=False)
        subprocess.run(["killall", "-HUP", "mDNSResponder"], check=False)
    else:
        # systemd-resolved 的两种入口,有哪个用哪个,都没有就无所谓
        subprocess.run(["resolvectl", "flush-caches"], check=False,
                       capture_output=True)
        subprocess.run(["systemd-resolve", "--flush-caches"], check=False,
                       capture_output=True)


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
    tmp = HOSTS_FILE + ".ghspeed.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, HOSTS_FILE)
    flush_dns()


def remove_hosts():
    text = read_hosts()
    if MARK_S in text:
        text = re.sub(re.escape(MARK_S) + r".*?" + re.escape(MARK_E) + r"\n?",
                      "", text, flags=re.S)
        tmp = HOSTS_FILE + ".ghspeed.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, HOSTS_FILE)
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
        <string>refresh</string>
        <string>--quiet</string>
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
            tr = '\\"%s\\" \\"%s\\" refresh --quiet' % (exe, script)
        else:
            tr = '\\"%s\\" refresh --quiet' % exe
        subprocess.run(["schtasks", "/Create", "/F", "/RU", "SYSTEM",
                        "/SC", "MINUTE", "/MO", str(minutes),
                        "/TN", WIN_TASK, "/TR", tr],
                       check=False, capture_output=True)
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
        args = "refresh --quiet"
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
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", WIN_TASK],
                       check=False, capture_output=True)
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
        return subprocess.run(["schtasks", "/Query", "/TN", WIN_TASK],
                              check=False, capture_output=True).returncode == 0
    if IS_MAC:
        return os.path.exists(PLIST_PATH)
    return os.path.exists(SYSTEMD_TIMER)


# ---------------- 子命令 ----------------


def cmd_probe(args):
    print("正在对 %d 个 GitHub 域名测速(不写 hosts)..." % len(DOMAINS))
    state, dead = find_best(progress=True)
    if not state:
        sys.exit("没有任何可用 IP,请检查网络")
    save_state(state, dead)
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
    if is_admin():
        if current_map_from_hosts() != {d: v["ip"] for d, v in state.items()}:
            write_hosts(state)
            journal("refresh", "%d 域名已更新" % len(state))
            print("ghspeed: 刷新完成,%d 个域名已更新" % len(state))
        else:
            journal("refresh", "IP 无变化")
            print("ghspeed: 刷新完成,IP 无变化")
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
    print("  ④ 本地代理       %s" % (",".join(found) + "(ghspeed proxy detect 看接法)"
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


def cmd_mirrors(args):
    print("镜像健康检查(HEAD 真实 release 资产 jq-linux-amd64):")
    pool = mirror_pool()
    print("(池子:静态 %d + 动态 %d,动态来源 %s,全部真验证过)"
          % (len([m for m in pool if m in set(MIRRORS)]),
             len([m for m in pool if m not in set(MIRRORS)]),
             "、".join(MIRROR_FEED_SOURCES)))
    print("(下单链按实测延迟排序(动态序);静态默认序 %s 只是候选来源,不代表最快)"
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

    print()
    alive = []
    for m, ms, code, err in results:
        if m is None:
            name = "直连(吃 hosts 优化)"
        else:
            name = urlparse(m).netloc + ("·静态" if m in MIRRORS else "·动态")
        if ms is not None:
            print("  + %-30s %5.0f ms  HTTP %s" % (name, ms * 1000, code))
            if m is not None and code == 200:
                alive.append((ms, name))
        else:
            print("  x %-30s %s" % (name, err))

    alive.sort()
    if alive:
        print("\n推荐顺序:%s" % " → ".join(n for _, n in alive))
        print("自定义镜像:GHSPEED_MIRRORS=\"https://a.example.com/,https://b.example.com/\" "
              "ghspeed dl <url>")


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


def cmd_dl(args):
    url = args.url.strip()
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
        chain.append(None)  # 直连兜底(hosts 已优化)
    proxy = (args.proxy or os.environ.get("GHSPEED_PROXY", "")).strip() or None
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
                continue
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


def cmd_git(args):
    if shutil.which("git") is None:
        sys.exit("未找到 git")
    if args.action == "on":
        m = args.mirror or mirror_pool()[0]
        subprocess.run(["git", "config", "--global", "--replace-all",
                        "url.%shttps://github.com/.insteadOf" % m,
                        "https://github.com/"], check=True)
        print("已开启:git clone https://github.com/... 会自动改走镜像 %s" % m)
        print("注意:push 前请先 sudo/普通运行 ghspeed git off,否则推送会失败")
    else:
        out = subprocess.run(
            ["git", "config", "--global", "--get-regexp", r"^url\..*\.insteadof$"],
            capture_output=True, text=True).stdout
        n = 0
        for line in out.splitlines():
            key, _, val = line.partition(" ")
            if val.strip() == "https://github.com/":
                subprocess.run(["git", "config", "--global", "--unset-all", key],
                               check=True)
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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


def cmd_proxy(args):
    print("探测本机常见代理端口:%s" % ", ".join(map(str, PROXY_PORTS)))
    found = detect_proxies()
    if not found:
        print("未发现可用的本地 HTTP 代理口(SOCKS 口不认;Clash 开「混合端口」即可)")
        print("知道端口的话可以直接用:")
        print("  GHSPEED_PROXY=http://127.0.0.1:端口 ghspeed dl <url>")
        return
    print("可用代理:")
    for p in found:
        print("  + %s" % p)
    p = found[0]
    print("\n接法(只影响 GitHub,不动其他网站):")
    print("  git:    git config --global http.https://github.com/.proxy %s" % p)
    print("          取消:git config --global --unset http.https://github.com/.proxy")
    print("  下载:   GHSPEED_PROXY=%s ghspeed dl <url>" % p)


# ---------------- 系统 DNS:opt-in 全局接管,off 原样还原 ----------------

DNS_CLEAN = ["223.5.5.5", "119.29.29.29"]   # 国内不投毒公共 DNS(阿里/腾讯)


def dns_backup_file():
    return os.path.join(os.path.expanduser("~"), ".ghspeed", "dns_backup.json")


def _mac_dns_services():
    r = subprocess.run(["networksetup", "-listallnetworkservices"],
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


def _dns_current():
    """[(服务/适配器名, 当前 DNS 列表或 None=自动获取)]"""
    out = []
    if IS_MAC:
        for svc in _mac_dns_services():
            r = subprocess.run(["networksetup", "-getdnsservers", svc],
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
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True)
        try:
            data = json.loads(r.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            for it in data or []:
                out.append((it["InterfaceAlias"], it["d"].split(",")))
        except Exception:
            pass
    return out


def _dns_apply(svc, servers):
    """写 DNS(不回读校验);返回 (是否成功, 错误摘要)——失败别再无声吞掉"""
    if IS_MAC:
        cmd = ["networksetup", "-setdnsservers", svc] + (servers or ["Empty"])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, ((r.stderr or r.stdout or "").strip()
                           or "rc=%d" % r.returncode)[:80]
        return True, ""
    if IS_WIN:
        ps = ("Set-DnsClientServerAddress -InterfaceAlias '%s' " % svc.replace("'", "''")
              + ("-ServerAddresses (%s)" % ",".join("'%s'" % d for d in servers)
                 if servers else "-ResetServerAddresses"))
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True)
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


# ---------------- 浏览器 SNI 分片(可选,opt-in)----------------
# 原理:DPI 按明文 SNI 匹配丢包;本地 relay 把 TLS ClientHello 拆成多个 TCP 段
# 发出去,部分部署下的识别逻辑拼不出 SNI 就不触发 RST。不加密、不翻墙、
# 不碰证书(浏览器锁标不变),服务器收到的字节流与原文完全一致。
# 生命周期(还原契约):显式 off / 退 App / 总 off / 崩溃兜底 / 启动自愈,
# 五条路都收口;PAC 恒带 "…; DIRECT" 兜底,relay 死了浏览器自动回落直连。

SNI_PORT = int(os.environ.get("GHSPEED_SNI_PORT", "18777"))


def _sni_home():
    """文件落点钉在控制台用户家:sudo 重置 HOME 到 /var/root,而 PAC 路径会被
    写进系统代理配置,浏览器(用户身份)读不到 /var/root 就整体哑掉"""
    if not IS_WIN:
        try:
            import pwd
            return pwd.getpwuid(os.stat("/dev/console").st_uid).pw_dir
        except Exception:
            pass
    return os.path.expanduser("~")


_SNI_DIR = os.path.join(_sni_home(), ".ghspeed")
SNI_PAC = os.path.join(_SNI_DIR, "ghspeed.pac")
SNI_CFG = os.path.join(_SNI_DIR, "sni.json")
SNI_PID = os.path.join(_SNI_DIR, "sni.pid")
SNI_STRATS = ("direct", "split1", "snisplit", "disorder")


def sni_pac_text():
    doms = ", ".join('"%s"' % d for d in DOMAINS)
    return (
        "// GHSpeed 浏览器 SNI 分片:仅下列 GitHub 域走本地分片 relay,其余一律 DIRECT\n"
        "function FindProxyForURL(url, host) {\n"
        "  host = host.toLowerCase();\n"
        "  var d = [" + doms + "];\n"
        "  for (var i = 0; i < d.length; i++) {\n"
        "    var s = \".\" + d[i];\n"
        "    if (host === d[i] || host.substring(host.length - s.length) === s)\n"
        "      return \"PROXY 127.0.0.1:%d; DIRECT\";\n"
        "  }\n"
        "  return \"DIRECT\";\n"
        "}\n" % SNI_PORT)


def sni_pac_url():
    return "file://" + SNI_PAC.replace(" ", "%20")


def sni_pac_ours(url):
    return bool(url) and url.rstrip("/").endswith("ghspeed.pac")


def sni_cfg():
    try:
        with open(SNI_CFG) as f:
            return json.load(f)
    except Exception:
        return {}


def sni_strategy():
    return sni_cfg().get("strategy", "rotate")


_ROT_LOCK = threading.Lock()
_ROT_SEQ = [0]


def _conn_strategy():
    """每条连接轮换分片手法:不赌单一策略,DPI 哪种不防就走哪种;
    被掐的那条浏览器会自动重试,下一条就换了手法"""
    s = os.environ.get("GHSPEED_SNI_STRAT") or sni_strategy()
    if s != "rotate":
        return s
    with _ROT_LOCK:
        i = _ROT_SEQ[0]
        _ROT_SEQ[0] = (i + 1) % 3
    return ("split1", "snisplit", "disorder")[i]


def _frag_send(sock, data, strat):
    """首飞字节按策略拆发;TCP 重组后与原文一致,只有中间的 DPI 看不出来"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    if strat == "split1" and len(data) > 1:
        sock.sendall(data[:1])
        time.sleep(0.005)
        sock.sendall(data[1:])
    elif strat == "snisplit" and len(data) > 4:
        pos = data.find(b".com")            # 我们管理的域全以 .com 结尾
        j = min((pos + 4 if pos >= 0 else len(data) // 2), len(data) - 1)
        sock.sendall(data[:j])
        time.sleep(0.005)
        sock.sendall(data[j:])
    elif strat == "disorder" and len(data) > 2:
        mid = max(1, len(data) // 2)
        sock.sendall(data[mid:])            # 先发后半段
        time.sleep(0.03)
        sock.sendall(data[:mid])            # 再发前半段,收端按序重组
    else:
        sock.sendall(data)


def sni_tls_probe(strat, host="github.com", timeout=8):
    """按某策略完成一次真实 TLS 握手(验证书+主机名),成功返回耗时 ms。
    用 wrap_bio 拿到 ClientHello 明文,对上游 socket 手工分片发送"""
    ctx = ssl.create_default_context()
    c2s, s2c = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = ctx.wrap_bio(c2s, s2c, server_hostname=host)
    t0 = time.perf_counter()
    with socket.create_connection((host, 443), timeout=timeout) as up:
        up.settimeout(timeout)
        try:
            client.do_handshake()
        except ssl.SSLWantReadError:
            pass
        hello = c2s.read()
        if hello:
            _frag_send(up, hello, strat)
        while True:
            out = c2s.read()
            if out:
                up.sendall(out)
            try:
                client.do_handshake()
                break
            except ssl.SSLWantReadError:
                d = up.recv(16384)
                if not d:
                    raise ConnectionError("上游提前断开")
                s2c.write(d)
            except ssl.SSLWantWriteError:
                pass
    return (time.perf_counter() - t0) * 1000


def cmd_sni_test(n=None):
    n = n or int(os.environ.get("GHSPEED_SNI_TEST_N", "6"))
    print("对 github.com:443 逐策略做 %d 次真实 TLS 握手(验证书):\n" % n)
    rows = {}
    for strat in SNI_STRATS:
        ok, ms = 0, []
        for _ in range(n):
            try:
                ms.append(sni_tls_probe(strat))
                ok += 1
            except Exception:
                pass
            time.sleep(0.2)
        avg = sum(ms) / len(ms) if ms else 0.0
        rows[strat] = (ok, round(avg))
        print("  %-9s %d/%d 成功   平均 %4.0f ms" % (strat, ok, n, avg))
        time.sleep(0.3)
    ranked = sorted(SNI_STRATS, key=lambda s: (-rows[s][0], rows[s][1] or 9e9))
    best_frag = max(rows[s][0] for s in SNI_STRATS[1:])
    if rows["direct"][0] > best_frag:
        pick = "direct"      # 分片反而挨打(中间盒讨厌拆开的握手)→ 老实直连
    else:
        pick = "rotate"      # 平手/分片更优 → 每连接轮换,不赌单一手法
    os.makedirs(os.path.dirname(SNI_CFG), exist_ok=True)
    with open(SNI_CFG, "w") as f:
        json.dump({"strategy": pick, "time": time.time(),
                   "results": rows}, f, ensure_ascii=False, indent=1)
    journal("sni-test", "%s %d/%d" % (pick, rows[ranked[0]][0], n))
    print("\n采用策略:%s(已存 %s;7 天后 on 会自动重测换策略)" % (pick, SNI_CFG))
    if pick == "rotate":
        print("(rotate = relay 给每条连接轮换 split1/snisplit/disorder,"
              "被掐的那条浏览器自动重试,下一条就换了手法)")
    if rows["direct"][0] == n:
        print("(直连此刻全通:现在没有识别型拦截;开着只在未来坏窗口里起作用)")
    return pick


# ---- relay:只服务 CONNECT 到我们域名列表的请求,别的域一律 403(只碰 GitHub)----

class _SniServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _sni_pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass


class _SniHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self._relay()
        except OSError:
            pass

    def _relay(self):
        req = b""
        while b"\r\n\r\n" not in req and len(req) < 16384:
            d = self.request.recv(4096)
            if not d:
                return
            req += d
        head, _, rest = req.partition(b"\r\n\r\n")
        parts = head.split(b"\r\n", 1)[0].decode("latin1", "replace").split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        hp = parts[1]
        host = hp.rsplit(":", 1)[0].strip("[]").lower()
        port = hp.rsplit(":", 1)[1] if ":" in hp else "443"
        if not any(host == d or host.endswith("." + d) for d in DOMAINS):
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            up = socket.create_connection((host, int(port)), timeout=15)
        except OSError:
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass
            return
        try:
            up.settimeout(None)
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            first = self._first_flight(rest)
            if first:
                _frag_send(up, first, _conn_strategy())
            t = threading.Thread(target=_sni_pump, args=(self.request, up), daemon=True)
            t.start()
            _sni_pump(up, self.request)
        finally:
            try:
                up.close()
            except OSError:
                pass

    def _first_flight(self, rest):
        """读满第一个 TLS 记录;浏览器可能把 CONNECT 和 ClientHello 捎带同发,rest 就是已读到的部分"""
        data = rest
        while len(data) < 5:
            d = self.request.recv(5 - len(data))
            if not d:
                return data
            data += d
        if len(data) < 5 or data[0] != 0x16:   # 非 TLS 握手:原样交出去
            return data
        rlen = (data[3] << 8) | data[4]
        while len(data) < 5 + rlen:
            d = self.request.recv(5 + rlen - len(data))
            if not d:
                break
            data += d
        return data


def sni_serve():
    os.makedirs(os.path.dirname(SNI_PID), exist_ok=True)
    try:
        srv = _SniServer(("127.0.0.1", SNI_PORT), _SniHandler)
    except OSError as e:
        sys.exit("relay 启动失败:%s(端口 %d 被占?)" % (e, SNI_PORT))
    with open(SNI_PID, "w") as f:
        f.write(str(os.getpid()))
    try:
        srv.serve_forever()
    finally:
        try:
            os.remove(SNI_PID)
        except Exception:
            pass


def sni_start_inproc():
    """GUI 用:进程内起 relay 线程;端口已被占(独立 relay 在跑)则静默让位"""
    def run():
        try:
            srv = _SniServer(("127.0.0.1", SNI_PORT), _SniHandler)
            with open(SNI_PID, "w") as f:
                f.write(str(os.getpid()))
            srv.serve_forever()
        except OSError:
            pass
    threading.Thread(target=run, daemon=True).start()


def _sni_relay_alive():
    try:
        socket.create_connection(("127.0.0.1", SNI_PORT), timeout=0.6).close()
        return True
    except OSError:
        return False


def _sni_spawn_relay():
    """起独立 relay 进程(用户级,不依赖 App 活着);已在跑就不动"""
    if _sni_relay_alive():
        return True
    if IS_WIN:
        if getattr(sys, "frozen", False):
            cli = os.path.join(os.path.dirname(sys.executable), "ghspeed-cli.exe")
            if not os.path.exists(cli):
                return False                      # GUI 的内置 relay 线程兜底
            cmd = [cli, "sni", "serve"]
        else:
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            cmd = [pyw if os.path.exists(pyw) else sys.executable,
                   os.path.abspath(__file__), "sni", "serve"]
        kw = {"creationflags": 0x00000008 | 0x08000000}   # DETACHED | NO_WINDOW
    else:
        cmd = ["/usr/bin/python3", os.path.abspath(__file__), "sni", "serve"]
        kw = {"start_new_session": True}
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except Exception:
        return False
    for _ in range(20):
        if _sni_relay_alive():
            return True
        time.sleep(0.15)
    return False


def _sni_stop_relay():
    pid = None
    try:
        with open(SNI_PID) as f:
            pid = int(f.read().strip())
    except Exception:
        pass
    if pid and pid != os.getpid():
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
    try:
        os.remove(SNI_PID)
    except Exception:
        pass
    # 兜底:pidfile 没对上号(重启/竞态/残留)时,按命令行特征清扫到端口真正释放
    for _ in range(10):
        if not _sni_relay_alive():
            return
        try:
            if IS_WIN:
                subprocess.run(["wmic", "process", "where",
                                "CommandLine like '%sni serve%'", "call", "terminate"],
                               capture_output=True)
            else:
                subprocess.run(["pkill", "-f", "sni serve"], capture_output=True)
        except Exception:
            pass
        time.sleep(0.3)


# ---- 系统 PAC 设置(mac:networksetup 要管理员;win:HKCU 注册表免管理员)----

SNI_BACKUP = os.path.join(_SNI_DIR, "sni_backup.json")


def _sni_dir_candidates():
    """备份可能落在谁家:osascript 提权不改 HOME,sudo 会改——全都找一遍"""
    out = [os.path.dirname(SNI_BACKUP)]
    if not IS_WIN:
        import pwd
        out.append(os.path.join(os.path.expanduser("~"), ".ghspeed"))
        try:
            out.append("/var/root/.ghspeed")
        except Exception:
            pass
    seen, res = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res


def _sni_backup_write(data):
    os.makedirs(os.path.dirname(SNI_BACKUP), exist_ok=True)
    data["time"] = time.time()
    with open(SNI_BACKUP, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for d in _sni_dir_candidates():      # 收敛到当前身份这一份
        p = os.path.join(d, "sni_backup.json")
        if p != SNI_BACKUP:
            try:
                os.remove(p)
            except Exception:
                pass


def _sni_backup_read():
    for d in _sni_dir_candidates():
        try:
            with open(os.path.join(d, "sni_backup.json")) as f:
                return json.load(f)
        except Exception:
            continue
    return None


def _sni_backup_clear():
    for d in _sni_dir_candidates():
        try:
            os.remove(os.path.join(d, "sni_backup.json"))
        except Exception:
            pass


def _mac_pac_get(svc):
    """networksetup -getautoproxyurl 输出纯文本两行:URL: … / Enabled: Yes|No"""
    r = subprocess.run(["networksetup", "-getautoproxyurl", svc],
                       capture_output=True, text=True)
    out = r.stdout or ""
    m = re.search(r"^URL:\s*(.*)$", out, re.M)
    en = re.search(r"^Enabled:\s*Yes\s*$", out, re.M) is not None
    return en, (m.group(1).strip() if m else "")


_WIN_IE = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _win_pac_get():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_IE) as k:
            return winreg.QueryValueEx(k, "AutoConfigURL")[0]
    except OSError:
        return None


def _win_pac_set(url):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _WIN_IE, 0,
                            winreg.KEY_SET_VALUE) as k:
        if url:
            winreg.SetValueEx(k, "AutoConfigURL", 0, winreg.REG_SZ, url)
        else:
            try:
                winreg.DeleteValue(k, "AutoConfigURL")
            except OSError:
                pass
    try:   # 让 IE/系统代理设置立即生效,不用重开浏览器
        ctypes.windll.wininet.InternetSetOptionW(None, 39, None, 0)
        ctypes.windll.wininet.InternetSetOptionW(None, 37, None, 0)
    except Exception:
        pass


def sni_pac_on():
    """写系统 PAC。返回失败服务列表(mac)或 True/False(win)"""
    if IS_WIN:
        prev = _win_pac_get()
        _sni_backup_write({"platform": "Windows", "autourl": prev})
        _win_pac_set(sni_pac_url())
        return []
    need_root()
    items = {}
    for s in _mac_dns_services():
        en, u = _mac_pac_get(s)
        items[s] = {"enabled": en, "url": u}
    _sni_backup_write({"platform": "Darwin", "services": items})
    fail = []
    for s in items:
        r = subprocess.run(["networksetup", "-setautoproxyurl", s, sni_pac_url()],
                           capture_output=True, text=True)
        if r.returncode != 0:
            fail.append("%s:%s" % (s, (r.stderr or r.stdout or "失败").strip()))
    return fail


def sni_pac_off():
    """还原系统 PAC:有备份按备份;没有就把指向我们的清扫掉——永远可取消。
    返回 (还原数, 清扫数, 失败列表)"""
    b = _sni_backup_read() or {}
    n_res = n_sweep = 0
    fail = []
    if IS_WIN:
        cur = _win_pac_get()
        if sni_pac_ours(cur):
            _win_pac_set(b.get("autourl"))
            n_res += 1
    else:
        need_root()
        old = b.get("services") or {}
        for s in _mac_dns_services():
            en, cur = _mac_pac_get(s)
            ours = en and sni_pac_ours(cur)
            if s in old:
                o = old[s]
                if o.get("enabled") and o.get("url"):
                    r = subprocess.run(["networksetup", "-setautoproxyurl", s, o["url"]],
                                       capture_output=True, text=True)
                else:
                    r = subprocess.run(["networksetup", "-setautoproxystate", s, "off"],
                                       capture_output=True, text=True)
            elif ours:
                r = subprocess.run(["networksetup", "-setautoproxystate", s, "off"],
                                   capture_output=True, text=True)
            else:
                continue
            if r.returncode == 0:
                if s in old:
                    n_res += 1
                else:
                    n_sweep += 1
            else:
                fail.append("%s:%s" % (s, (r.stderr or r.stdout or "失败").strip()))
    return n_res, n_sweep, fail


def sni_state():
    url = _win_pac_get() if IS_WIN else None
    if IS_WIN:
        enabled = sni_pac_ours(url)
    elif IS_MAC:
        enabled = False
        for s in _mac_dns_services():
            en, u = _mac_pac_get(s)
            if en and sni_pac_ours(u):
                enabled = True
                break
    else:
        enabled = False
    return {"enabled": enabled, "relay": _sni_relay_alive(),
            "strategy": sni_strategy()}


def sni_off(quiet=False):
    """完整还原:停 relay → 还原 PAC → 清备份/缓存文件。返回是否动过东西"""
    if IS_LINUX:
        return False
    _sni_stop_relay()
    try:
        n_res, n_sweep, fail = sni_pac_off()
    except SystemExit:
        if quiet:
            raise
        sys.exit("macOS 上取消 PAC 需要管理员:请用界面按钮,或 sudo ghspeed sni off")
    _sni_backup_clear()
    try:
        os.remove(SNI_PAC)
    except Exception:
        pass
    moved = (n_res + n_sweep) > 0
    journal("sni-off", "%d还原/%d清扫" % (n_res, n_sweep))
    if not quiet:
        if not moved:
            print("本来就未开启(系统里没有指向 ghspeed 的 PAC),无需还原")
            return False
        print("已还原:停 relay + PAC 还原 %d 个、清扫 %d 个 → 原样" % (n_res, n_sweep))
        if fail:
            print("没动成的:%s(可再跑一次 sni off)" % ";".join(fail))
    return moved


def sni_heal(quiet=False):
    """启动自愈:开启着但 relay 死了 → 先尝试复活;复活不成 → 清掉孤儿 PAC"""
    st = sni_state()
    if not st["enabled"]:
        return False
    if st["relay"] or _sni_spawn_relay():
        return False
    try:
        moved = sni_off(quiet=True)
    except SystemExit:
        moved = None      # mac 无管理员,清不动 → 交回给调用方(界面)提权重试
    if moved:
        journal("sni-heal", "relay 复活失败,孤儿 PAC 已清扫")
        if not quiet:
            print("检测到孤儿状态(relay 已死),已自动还原 PAC")
    return bool(moved)


def cmd_sni(args):
    if IS_LINUX and args.action in ("on", "off"):
        sys.exit("Linux 桌面各发行版代理设置差异大,sni 暂只支持 macOS/Windows;"
                 "命令行可手动把浏览器 PAC 指到本地 relay:ghspeed sni state 看用法")
    if args.action == "test":
        cmd_sni_test()
    elif args.action == "serve":
        sni_serve()
    elif args.action == "on":
        cfg = sni_cfg()
        if not cfg.get("time") or time.time() - cfg["time"] > 7 * 86400:
            print("还没有可靠的策略数据(或已超 7 天),先实测一轮:\n")
            cmd_sni_test()
            print()
        fail = sni_pac_on()
        if IS_WIN and fail is not None:
            pass
        if not IS_WIN and fail:
            sys.exit("PAC 没写进去:" + ";".join(fail))
        _sni_spawn_relay()
        journal("sni-on", "%s port%d" % (sni_strategy(), SNI_PORT))
        print("已开启浏览器 SNI 分片:")
        print("  · PAC:%s(仅 %d 个 GitHub 域走本地 relay,其余 DIRECT)"
              % (sni_pac_url(), len(DOMAINS)))
        print("  · relay:127.0.0.1:%d,策略 %s;relay 挂了浏览器自动回落直连,不会断网"
              % (SNI_PORT, sni_strategy()))
        if sni_strategy() == "rotate":
            print("  · rotate = 每条连接轮换分片手法,不赌单一策略(ghspeed sni test 可固定)")
        print("  · 还原:ghspeed sni off(退出 App 也会自动还原)")
    elif args.action == "off":
        sni_off()
    elif args.action == "heal":
        sni_heal()
    else:   # state
        st = sni_state()
        print("PAC 接管:%s" % ("已开启(ghspeed sni off 可取消)" if st["enabled"] else "未开启"))
        print("relay:%s" % ("活着(127.0.0.1:%d)" % SNI_PORT if st["relay"]
                            else "没在跑(PAC 未开时不需要;开着则浏览器走 DIRECT 兜底)"))
        print("策略:%s(ghspeed sni test 重测)" % st["strategy"])
        if st["enabled"] and not st["relay"]:
            print("⚠ 开着但 relay 不在:浏览器已自动回落直连;ghspeed sni heal 可自愈")


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
    sub.add_parser("mirrors", help="检查各下载镜像的健康度").set_defaults(func=cmd_mirrors)
    sub.add_parser("install", help="把本脚本安装为 /usr/local/bin/ghspeed(root)").set_defaults(func=cmd_install)

    p = sub.add_parser("refresh", help="重新测速并刷新(定时任务调用)")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("dl", help="经镜像下载 GitHub 文件(支持断点续传)")
    p.add_argument("url", help="release/raw/archive 等 GitHub 链接")
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

    sub.add_parser("proxy", help="探测本机代理端口并给出接管命令(只检测,不接管)"
                   ).set_defaults(func=cmd_proxy)

    sub.add_parser("live", help="看当前谁正连着钉选 IP(请求吃到加速的实时视图)"
                   ).set_defaults(func=cmd_live)

    sub.add_parser("activity", help="回放 ghspeed 自身动作日志(下载/刷新/开关)"
                   ).set_defaults(func=cmd_activity)

    p = sub.add_parser("dns", help="系统 DNS 换成干净公共 DNS(opt-in,影响全部网站;off 原样还原)")
    p.add_argument("action", choices=["on", "off", "show"])
    p.set_defaults(func=cmd_dns)

    p = sub.add_parser("sni", help="浏览器 SNI 分片(可选):ClientHello 拆散绕过识别,仅 GitHub 域走本地 relay;任何关闭都原样还原")
    p.add_argument("action", choices=["test", "on", "off", "state", "heal", "serve"],
                   nargs="?", default="state")
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
