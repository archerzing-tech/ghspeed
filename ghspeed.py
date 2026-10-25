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
import ssl
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse, unquote

# ---------------- 常量 ----------------

import platform as _platform

IS_MAC = _platform.system() == "Darwin"
IS_WIN = _platform.system() == "Windows"

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
        __version__ = _f.read().strip() or "0.1.0"
except Exception:
    __version__ = "0.1.0"

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

# 下载镜像(ghproxy 系),按顺序尝试(活的快的在前),最后直连兜底
MIRRORS = [
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
    "https://ghfast.top/",
    "https://gh.llkk.cc/",
]
# 自定义镜像(借鉴 fnos-store 的 custom mirror):逗号分隔,末尾带不带 / 均可
#   GHSPEED_MIRRORS="https://mirror.example.com/,https://gh-proxy.com/" ghspeed dl <url>
_env_mirrors = os.environ.get("GHSPEED_MIRRORS")
if _env_mirrors:
    MIRRORS = [m.strip().rstrip("/") + "/" for m in _env_mirrors.split(",") if m.strip()]

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

    add(doh_lookup(domain))               # 实时解析优先(新鲜)
    add(sorted(static_candidates(domain)))  # 内置兜底
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
ExecStart=%(exe)s %(script)s refresh --quiet
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
    exe, script, minutes = sys.executable, os.path.realpath(__file__), REFRESH_INTERVAL // 60
    if IS_WIN:
        # SYSTEM 账号自带管理员权限;TR 内层引号由 schtasks 约定为 \"
        tr = '\\"%s\\" \\"%s\\" refresh --quiet' % (exe, script)
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
        for path, body in (
                (SYSTEMD_SERVICE, SYSTEMD_SERVICE_TMPL % {"exe": exe, "script": script}),
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
    print("\n共 %d/%d 个域名可用,结果已缓存(启用请:sudo ghspeed on)"
          % (len(state), len(DOMAINS)))


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
    print("\n已启用:%d/%d 个域名固定到最快 IP" % (len(state), len(DOMAINS)))
    print("  - hosts 备份:%s(首次启用时创建)" % BACKUP_FILE)
    print("  - 每 %d 分钟自动重测刷新,日志:%s" % (REFRESH_INTERVAL // 60, LOG_FILE))
    print("  - 停用:sudo ghspeed off")


def cmd_off(args):
    need_root()
    removed = remove_hosts()
    daemon_remove()
    print("hosts 中的 ghspeed 条目:%s" % ("已移除" if removed else "本来就没有"))
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
            print("ghspeed: 刷新完成,%d 个域名已更新" % len(state))
        else:
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


# 真实 release 资产(借鉴 fnos-store mirrors.go 的测法):release 下载走
# 独立后端,是镜像最容易"根路径活着但代理失败"的场景,必须拿真路径测
MIRROR_TEST_URL = ("https://github.com/jqlang/jq/releases/download/"
                   "jq-1.7.1/jq-linux-amd64")


def cmd_mirrors(args):
    print("镜像健康检查(HEAD 真实 release 资产 jq-linux-amd64):")

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

    chain = MIRRORS + [None]
    results = []
    with cf.ThreadPoolExecutor(8) as ex:
        for m, (ms, code, err) in zip(chain, ex.map(t, chain)):
            results.append((m, ms, code, err))

    print()
    alive = []
    for m, ms, code, err in results:
        name = "直连(吃 hosts 优化)" if m is None else urlparse(m).netloc
        if ms is not None:
            print("  + %-26s %5.0f ms  HTTP %s" % (name, ms * 1000, code))
            if m is not None and code == 200:
                alive.append((ms, name))
        else:
            print("  x %-26s %s" % (name, err))

    alive.sort()
    if alive:
        print("\n推荐顺序:%s" % " → ".join(n for _, n in alive))
        print("自定义镜像:GHSPEED_MIRRORS=\"https://a.example.com/,https://b.example.com/\" "
              "ghspeed dl <url>")


def download_once(url, part_path, resume, min_speed=25.0, stall_s=12.0):
    """下载到 part_path;传输停滞或平均速度过低时抛异常,由上层换通道重试。"""
    headers = {"User-Agent": UA}
    offset = os.path.getsize(part_path) if resume and os.path.exists(part_path) else 0
    if offset:
        headers["Range"] = "bytes=%d-" % offset
    req = urllib.request.Request(url, headers=headers)
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
        chain = [args.mirror] if args.mirror else list(MIRRORS)
        chain.append(None)  # 直连兜底(hosts 已优化)

    for m in chain:
        label = "直连" if m is None else urlparse(m).netloc
        print("==> 尝试 %s" % label)
        try:
            got, secs = download_once((m + url) if m else url, part,
                                      resume=os.path.exists(part),
                                      min_speed=args.min_speed)
        except KeyboardInterrupt:
            print("\n已中断,断点保存在 %s,重跑同一命令可续传" % part)
            return
        except Exception as e:
            print("    失败:%s" % e)
            continue
        os.replace(part, out)
        print("完成 → %s (%s, 平均 %.2f MB/s)"
              % (out, human(got), got / max(secs, 1e-6) / 1e6))
        return
    sys.exit("全部通道均失败;可稍后重试,或先运行 ghspeed mirrors 看看哪些镜像活着")


def cmd_git(args):
    if shutil.which("git") is None:
        sys.exit("未找到 git")
    if args.action == "on":
        m = args.mirror or MIRRORS[0]
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
    p.add_argument("--min-speed", type=float, default=25.0, metavar="KB/S",
                   help="平均速度低于此值就自动换下一个通道(0=不检查,默认 25)")
    p.set_defaults(func=cmd_dl)

    p = sub.add_parser("git", help="git clone 走镜像(可选,默认不开)")
    p.add_argument("action", choices=["on", "off"])
    p.add_argument("--mirror", help="指定镜像前缀")
    p.set_defaults(func=cmd_git)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
