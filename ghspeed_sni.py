"""GHSpeed 可选插件:浏览器 SNI 分片(本地 relay 把 ClientHello 拆散绕过识别)。

v0.5.1 起从引擎主文件分拆(0.6.0 加 tier 档位):非核心功能不占核心文件的维护心智,主文件只留薄封装。
由 ghspeed.py 在首次用到时懒加载(sni_mod()),并把引擎命名空间注入本模块
——插件内零改动直用 journal/_run/IS_WIN/DOMAINS 等引擎设施。文件缺失时
引擎优雅降级(状态显示未开启,显式 on/test 才提示重装),核心功能分毫不差。
"""
import socketserver

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
SNI_STOP = os.path.join(_SNI_DIR, "sni.stop")   # 协作停止哨兵:relay 轮询到即自停
SNI_STRATS = ("direct", "split1", "snisplit", "disorder")


def sni_pac_text():
    doms = ", ".join('"%s"' % d for d in DOMAINS)
    # 代理 fallback 跳:proxy on 接管过本机代理时,PAC 变成
    # relay → 本机代理 → DIRECT 三级;relay 挂了浏览器自动落到代理,
    # 代理也没了才直连吃 hosts 钉选
    hop = ""
    try:
        px = proxy_persisted()   # 引擎注入的接管状态
        if px:
            hop = "PROXY %s; " % px.split("://", 1)[-1].rstrip("/")
    except Exception:
        hop = ""
    # 第一跳:下载档位开着就指向档口(网页域在档口里也是原样透明转发,
    # 与 relay 完全同路);没开档位维持原 relay
    first = "127.0.0.1:%d" % (TIER_PORT if tier_cfg().get("on") else SNI_PORT)
    # 注意:%s(hop 跳)与 %d(端口)都在最后一个相邻字面量段里,
    # 让 % (first, hop) 只作用于它 —— Python 的 % 优先级比 + 高,
    # 占位符混在被 + 拆开的段里会炸
    return (
        "// GHSpeed 浏览器加速:仅下列 GitHub 域走本地代理,其余一律 DIRECT\n"
        "// 档位开启时第一跳为下载档位(下载域自动改写镜像,网页域原样转发)\n"
        "function FindProxyForURL(url, host) {\n"
        "  host = host.toLowerCase();\n"
        "  var d = [" + doms + "];\n"
        "  for (var i = 0; i < d.length; i++) {\n"
        "    var s = \".\" + d[i];\n"
        "    if (host === d[i] || host.substring(host.length - s.length) === s)\n"
        "      return \"PROXY %s; %sDIRECT\";\n"
        "  }\n"
        "  return \"DIRECT\";\n"
        "}\n" % (first, hop))


def sni_pac_url():
    p = SNI_PAC.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p                      # C:/Users/… → /C:/Users/…
    return "file://" + p.replace(" ", "%20")


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


def _read_head(cli, limit=16384):
    """读到首个 \\r\\n\\r\\n 为止;返回 (head, rest);对端先断返回 None"""
    req = b""
    while b"\r\n\r\n" not in req and len(req) < limit:
        d = cli.recv(4096)
        if not d:
            return None
        req += d
    head, _, rest = req.partition(b"\r\n\r\n")
    return head, rest


def _parse_connect(head):
    """"CONNECT host:port HTTP/1.1" → (host, port, 其余浏览器捎带的字节);非 CONNECT 返回 None"""
    line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
    parts = line.split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        return None
    hp = parts[1]
    host = hp.rsplit(":", 1)[0].strip("[]").lower()
    port = hp.rsplit(":", 1)[1] if ":" in hp else "443"
    return host, port


class _SniHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self._relay()
        except OSError:
            pass

    def _relay(self):
        got = _read_head(self.request)
        if got is None:
            return
        con = _parse_connect(got[0])
        if not con:
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        host, port = con
        if not any(host == d or host.endswith("." + d) for d in DOMAINS):
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        self._forward_fragment(host, int(port), got[1])

    def _forward_fragment(self, host, port, rest):
        """CONNECT 认可后的直连转发:分 ClientHello,双向泵(relay 与 tier 档共用)"""
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


_SNI_SERVER = None   # 本进程内 relay 的服务对象(同进程可一键停,GUI 内嵌场景)


def _sni_run_server():
    """绑定端口并后台跑 relay;返回 (server, thread),绑定失败(被占)返回 None。
    启动即清掉陈年 stop 哨兵——标记只在 relay 活着期间有效"""
    global _SNI_SERVER
    try:
        srv = _SniServer(("127.0.0.1", SNI_PORT), _SniHandler)
    except OSError:
        return None
    _SNI_SERVER = srv
    try:
        os.remove(SNI_STOP)
    except OSError:
        pass
    try:
        with open(SNI_PID, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    th = threading.Thread(target=srv.serve_forever,
                          kwargs={"poll_interval": 0.3}, daemon=True)
    th.start()
    return srv, th


def _sni_close_server(srv):
    """停 serve_forever 并真正关掉监听 socket —— 只 shutdown 不 close 的话
    端口一直占着,下一次「开启」会误以为 relay 还活着。"""
    try:
        srv.shutdown()
    except Exception:
        pass
    try:
        srv.server_close()
    except Exception:
        pass
    global _SNI_SERVER
    if _SNI_SERVER is srv:
        _SNI_SERVER = None


def _sni_wait_stop(srv, th):
    """阻塞到 stop 哨兵出现或 relay 线程自然退出,然后收尾"""
    while th.is_alive() and not os.path.exists(SNI_STOP):
        th.join(0.4)
    _sni_close_server(srv)
    try:
        os.remove(SNI_PID)
    except OSError:
        pass
    try:
        os.remove(SNI_STOP)
    except OSError:
        pass


def sni_serve():
    os.makedirs(_SNI_DIR, exist_ok=True)
    r = _sni_run_server()
    if r is None:
        sys.exit("relay 启动失败:端口 %d 被占?" % SNI_PORT)
    _sni_wait_stop(*r)


def sni_start_inproc():
    """GUI 用:进程内起 relay 线程;端口已被占(独立 relay 在跑)则静默让位"""
    def run():
        r = _sni_run_server()
        if r is None:
            return
        _sni_wait_stop(*r)
    threading.Thread(target=run, daemon=True).start()


def sni_request_stop():
    """停 relay 的统一入口:写哨兵(独立进程轮询自停)+ 停本进程内 relay。
    同进程的 shutdown/close 交给后台线程做——调用方(界面线程)绝不能被阻塞。"""
    try:
        with open(SNI_STOP, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    srv = _SNI_SERVER
    if srv is not None:
        threading.Thread(target=_sni_close_server, args=(srv,),
                         daemon=True).start()


def _sni_relay_alive():
    try:
        socket.create_connection(("127.0.0.1", SNI_PORT), timeout=0.4).close()
        return True
    except OSError:
        return False


def _engine_path():
    """引擎文件的真实路径:spwn 的命令行必须以引擎为入口(插件直接跑没有引擎设施)"""
    p = globals().get("_ENGINE_FILE")
    return p if p else os.path.abspath(__file__)


def _sni_port_owner():
    """谁在 LISTEN 我们的端口(只查一次 netstat,5 秒超时);查不到返回 None"""
    if not IS_WIN:
        return None
    try:
        r = _run(["netstat", "-ano", "-p", "TCP"], timeout=5,
                 capture_output=True, text=True)
    except Exception:
        return None
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[3].upper() == "LISTENING"
                and parts[1].rsplit(":", 1)[-1] == str(SNI_PORT)):
            try:
                return int(parts[4])
            except ValueError:
                pass
    return None


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
                   _engine_path(), "sni", "serve"]
        kw = {"creationflags": 0x00000008 | 0x08000000}   # DETACHED | NO_WINDOW
    else:
        cmd = ["/usr/bin/python3", _engine_path(), "sni", "serve"]
        kw = {"start_new_session": True}
    try:
        _popen(cmd, stdin=subprocess.DEVNULL,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except Exception:
        return False
    for _ in range(20):
        if _sni_relay_alive():
            return True
        time.sleep(0.15)
    return False


def _sni_stop_relay(deadline=5.0):
    """停 relay:协作哨兵 → pidfile 补刀 → 端口归属补刀,全程有界(默认 5 秒)。
    旧版兜底用 wmic:Win11 已移除、Win10 上 WMI 坏掉时一次能挂几十秒,
    「关闭加速」就永远停在"正在关闭…"。现在只用 netstat + taskkill,秒级收工。"""
    # 从没起过 relay(最常见的「关闭加速」):不写哨兵、不探端口,立即可回
    if not (os.path.exists(SNI_PID) or os.path.exists(SNI_STOP)
            or _SNI_SERVER is not None):
        return False

    t0 = time.monotonic()
    sni_request_stop()
    pid = None
    try:
        with open(SNI_PID) as f:
            pid = int(f.read().strip())
    except Exception:
        pass
    if pid and pid != os.getpid():
        try:
            if IS_WIN:
                _run(["taskkill", "/F", "/PID", str(pid)], timeout=4,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
    try:
        os.remove(SNI_PID)
    except Exception:
        pass

    def released():
        return not _sni_relay_alive()

    while time.monotonic() - t0 < deadline:
        if released():
            _sni_stop_cleanup()
            return True
        time.sleep(0.2)

    # 罕见:外部孤儿,或安全软件把端口占着 → 按端口归属再补一刀
    owner = _sni_port_owner()
    if owner and owner != os.getpid():
        try:
            _run(["taskkill", "/F", "/T", "/PID", str(owner)], timeout=4,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    elif not IS_WIN:
        try:
            _run(["pkill", "-f", "sni serve"], timeout=4,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    for _ in range(5):
        if released():
            break
        time.sleep(0.2)
    _sni_stop_cleanup()
    return not released()


def _sni_stop_cleanup():
    try:
        os.remove(SNI_STOP)
    except OSError:
        pass


# ---------------- 本地下载档位(tier):浏览器无感多通道下载 ----------------
# 原理:对「文件落地域」(codeload/raw/objects 等下载域)做选择性解密——本地生成
# CA 签叶子证书,把浏览器发出的下载请求改写成最优镜像通道回源,流式吐回;
# github.com 等网页域和登录 Cookie 永不解密(照旧透明转发,分片手法生效)。
# 一切可逆:on 装 CA + 起服务 + PAC 第一跳切到档位;off 摘 CA + 停服务 + PAC 还原。
# 没列进解密名单的域走的是和 relay 一模一样的转发路径,行为不变。

TIER_PORT = int(os.environ.get("GHSPEED_TIER_PORT", "18778"))
TIER_STATE = os.path.join(_SNI_DIR, "tier.json")
TIER_PID = os.path.join(_SNI_DIR, "tier.pid")
TIER_STOP = os.path.join(_SNI_DIR, "tier.stop")   # 协作停止哨兵(与 relay 同套路)
TIER_CA = os.path.join(_SNI_DIR, "tier-ca.pem")
TIER_CA_KEY = os.path.join(_SNI_DIR, "tier-ca.key")
_TIER_CERT_DIR = os.path.join(_SNI_DIR, "tier-certs")   # 按域缓存的叶子证书

_TIER_MITM_HOSTS = (     # 选择性解密名单:全是「文件下载」域,不含网页/登录域
    "codeload.github.com",
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-cloud.githubusercontent.com",
    "media.githubusercontent.com",
)

_TIER_CA_CN = "GHSpeed Local Download CA"

_TIER_CA_CNF = ("[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3ca\n"
                "[dn]\ncommonName=%s\n" % _TIER_CA_CN +
                "[v3ca]\nbasicConstraints=critical,CA:TRUE\n"
                "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")

_TIER_REQ_CNF = ("[req]\ndistinguished_name=dn\nprompt=no\n"
                 "[dn]\ncommonName=%s\n")     # %s = 目标域名

_TIER_LEAF_EXT = ("basicConstraints=CA:FALSE\n"
                  "keyUsage=critical,digitalSignature,keyEncipherment\n"
                  "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:%s\n")

_RESP_KEEP = ("Content-Length", "Content-Range", "Content-Type", "Content-Encoding",
              "Content-Disposition", "Accept-Ranges", "ETag", "Last-Modified", "Date")
_UP_HDRS = ("user-agent", "accept", "range")   # 转给上游(镜像/直连)的请求头白名单

_TIER_CTX_CACHE = {}      # host → ssl context(叶子证书按域缓存,进程内复用)
_TIER_SERVER = None


def tier_cfg():
    try:
        with open(TIER_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _tier_cfg_save(d):
    os.makedirs(_SNI_DIR, exist_ok=True)
    with open(TIER_STATE + ".tmp", "w") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(TIER_STATE + ".tmp", TIER_STATE)


def _tier_alive():
    try:
        socket.create_connection(("127.0.0.1", TIER_PORT), timeout=0.4).close()
        return True
    except OSError:
        return False


def _tier_openssl(args):
    """openssl 子命令统一入口;失败抛 RuntimeError(附 stderr 尾巴)"""
    exe = shutil.which("openssl")
    if not exe:
        raise RuntimeError("本机没有 openssl")
    r = _run([exe] + args, timeout=60, check=False, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("openssl %s 失败:%s"
                           % (args[0], ((r.stderr or r.stdout or "").strip())[-160:]))


def _tier_ca_ensure():
    """本地 CA:首次生成,之后一直用同一把(off 后重建也不换根,信任不用重装)"""
    os.makedirs(_TIER_CERT_DIR, exist_ok=True)
    if os.path.exists(TIER_CA) and os.path.exists(TIER_CA_KEY):
        return TIER_CA
    cnf = os.path.join(_TIER_CERT_DIR, "ca.cnf")
    with open(cnf, "w") as f:
        f.write(_TIER_CA_CNF)
    _tier_openssl(["req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes",
                   "-days", "3825", "-keyout", TIER_CA_KEY, "-out", TIER_CA,
                   "-config", cnf])
    try:
        os.chmod(TIER_CA_KEY, 0o600)
    except Exception:
        pass
    # 新根 → 旧叶子全部作废(缓存文件按旧根签的,留着会 TLS 哑)
    for fn in os.listdir(_TIER_CERT_DIR):
        if fn.endswith((".pem", ".key")) and fn != "ca.cnf":
            try:
                os.remove(os.path.join(_TIER_CERT_DIR, fn))
            except Exception:
                pass
    return TIER_CA


def _tier_leaf(host):
    """目标域的叶子证书(证书缓存文件;首次生成一次,之后的连接零 openssl 开销)"""
    key = os.path.join(_TIER_CERT_DIR, "%s.key" % host)
    pem = os.path.join(_TIER_CERT_DIR, "%s.pem" % host)
    if not (os.path.exists(key) and os.path.exists(pem)):
        reqcnf = os.path.join(_TIER_CERT_DIR, "_req.cnf")
        extcnf = os.path.join(_TIER_CERT_DIR, "_leaf.ext")
        with open(reqcnf, "w") as f:
            f.write(_TIER_REQ_CNF % host)
        with open(extcnf, "w") as f:
            f.write(_TIER_LEAF_EXT % host)
        csr = pem + ".csr"
        _tier_openssl(["req", "-new", "-newkey", "rsa:2048", "-nodes",
                       "-keyout", key, "-out", csr, "-subj", "/CN=%s" % host,
                       "-config", reqcnf])
        _tier_openssl(["x509", "-req", "-days", "826", "-in", csr,
                       "-CA", TIER_CA, "-CAkey", TIER_CA_KEY,
                       "-extfile", extcnf, "-out", pem, "-CAcreateserial"])
        try:
            os.remove(csr)
        except Exception:
            pass
    return pem, key


def _tier_tls_wrap(host, sock):
    """把浏览器连接升级成我们做服务端的 TLS(叶子证书按域缓存)"""
    try:
        pem, key = _tier_leaf(host)
        ctx = _TIER_CTX_CACHE.get(host)
        if ctx is None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(pem, key)
            _TIER_CTX_CACHE[host] = ctx
        return ctx.wrap_socket(sock, server_side=True)
    except (OSError, ssl.SSLError, RuntimeError):
        _TIER_CTX_CACHE.pop(host, None)   # 坏证书就地作废,下回重新生成
        raise


def _tier_channels(host, url):
    """下载请求的通道链:镜像池(实测吞吐序)→ jsDelivr(raw 专属)→ 直连(吃 hosts)
    → 本机已接管代理(最后兜底)。与 ghspeed dl 同一语义。"""
    ch = []
    try:
        pool = [m for m in mirror_pool() if m]
    except Exception:
        pool = []
    ch += pool
    if _is_raw_url(url):
        ch += ["RAWJS:" + h for h in JSDELIVR_HOSTS]
    ch.append(None)
    px = proxy_persisted()
    if px:
        ch.append("PROXY:" + px)
    return ch


def _tier_up_headers(head):
    """白名单拷贝浏览器请求头:绝不转 Cookie/登录态/引用页,防镜像侧留痕"""
    hs = {}
    for ln in head.split(b"\r\n")[1:]:
        k, _, v = ln.partition(b":")
        k = k.strip().lower().decode("latin1")
        if k in _UP_HDRS and v.strip():
            hs[k] = v.strip().decode("latin1")
    hs.setdefault("accept", "*/*")
    hs["accept-encoding"] = "identity"   # 上游别给压缩:我们原样流回
    return hs


def _tier_open(method, target, headers, prox=None):
    req = urllib.request.Request(target, headers=headers, method=method)
    if prox:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": prox, "https": prox}),
            urllib.request.HTTPSHandler(context=ssl_context()))
        return opener.open(req, timeout=40)
    return urllib.request.urlopen(req, timeout=40, context=ssl_context())


def _tier_respond(cli, method, resp):
    """上游响应 → 浏览器:按白名单回头部 + 流式倒字节;返回字节数"""
    reason = (resp.reason or "OK").encode("latin1", "replace")
    head = bytearray(b"HTTP/1.1 %d %s\r\n" % (resp.status, reason))
    for k in _RESP_KEEP:
        v = resp.headers.get(k)
        if v:
            head += b"%s: %s\r\n" % (k.encode(), v.encode("latin1", "replace"))
    head += b"Connection: close\r\n\r\n"
    cli.sendall(bytes(head))
    if method == "HEAD":
        return 0
    n = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        cli.sendall(chunk)
        n += len(chunk)
    return n


def _tier_close_text(cli, code, text):
    body = (
        "<!doctype html><html><body style='font:14px -apple-system,sans-serif'>"
        "<h3>GHSpeed 档位:%s</h3><p>全部通道(镜像/直连/代理)都没取到这个文件。"
        "<br>可稍后重试,或右键镜像复制链接直接下载;断点续传不受影响。</p>"
        "</body></html>") % text
    data = body.encode("utf-8")
    try:
        cli.sendall(b"HTTP/1.1 %d Text\r\nConnection: close\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Content-Length: %d\r\n\r\n" % (code, len(data)) + data)
    except OSError:
        pass


def _tier_fetch(cli, method, url, head):
    """核心改写:按通道链逐个试,取到即流回;全灭回 502(浏览器看到的是普通错误页)"""
    reqh = _tier_up_headers(head)
    last = ""
    for ch in _tier_channels("", url):
        tgt, prox = _dl_target(ch, url)
        try:
            resp = _tier_open(method, tgt, reqh, prox)
            if resp.status not in (200, 206):
                last = "HTTP %d via %s" % (resp.status, ch or "direct")
                resp.close()
                continue
            try:
                n = _tier_respond(cli, method, resp)
            finally:
                resp.close()
            try:
                journal("tier", "%s %.1fKB via %s" % (
                    urlparse(url).hostname, n / 1024.0,
                    urlparse(tgt).hostname if prox else (ch or "direct")))
            except Exception:
                pass
            return True
        except Exception as e:
            last = "%s via %s" % (str(e)[:60], ch or "direct")
            continue
    _tier_close_text(cli, 502, last[:180] or "未知错误")
    return False


class _TierHandler(_SniHandler):
    """tier 挡的连接分发:解密名单 → MITM 改写;其余 GitHub 域 → 原样分片转发"""

    def _relay(self):
        got = _read_head(self.request)
        if got is None:
            return
        con = _parse_connect(got[0])
        if con:
            host, port = con
            if host in _TIER_MITM_HOSTS:
                self._serve_mitm(host, got[1])
                return
            if not any(host == d or host.endswith("." + d) for d in DOMAINS):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            self._forward_fragment(host, int(port), got[1])
            return
        # 非 CONNECT:当普通 HTTP 代理请求(终端设 http_proxy 的场景)
        self._serve_plain(got[0])

    def _serve_mitm(self, host, rest):
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            cli = _tier_tls_wrap(host, self.request)
        except Exception:
            return                     # 浏览器拒了我们的叶子证书:连接就地结束
        try:
            while True:
                got = _read_head(cli)
                if got is None:
                    return
                reqline = got[0].split(b"\r\n", 1)[0].decode("latin1", "replace")
                m = re.match(r"(GET|HEAD)\s+(\S+)", reqline)
                if not m:
                    _tier_close_text(cli, 501, "档位只代理 GET/HEAD 下载(该方法没法改写)")
                    return
                if not _tier_fetch(cli, m.group(1).upper(),
                                   "https://%s%s" % (host, m.group(2)), got[0]):
                    return
        finally:
            try:
                cli.close()
            except Exception:
                pass

    def _serve_plain(self, head):
        reqline = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        m = re.match(r"(GET|HEAD)\s+(\S+)\s+HTTP/1", reqline)
        if not m:
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        u = urlparse(m.group(2))
        host = (u.hostname or "").lower()
        if u.scheme != "http":
            _tier_close_text(self.request, 400,
                             "普通代理模式只收 http:// 目标(https 走 CONNECT)")
            return
        if not any(host == d or host.endswith("." + d) for d in DOMAINS):
            _tier_close_text(self.request, 403, "仅限 GitHub 域(本档位是 GitHub 专用)")
            return
        _tier_fetch(self.request, m.group(1).upper(), m.group(2), head)


def _tier_run_server():
    """绑定端口并后台跑 tier 服务;被占返回 None(套路与 relay 相同)"""
    global _TIER_SERVER
    try:
        srv = _SniServer(("127.0.0.1", TIER_PORT), _TierHandler)
    except OSError:
        return None
    _TIER_SERVER = srv
    try:
        os.remove(TIER_STOP)
    except OSError:
        pass
    try:
        with open(TIER_PID, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    th = threading.Thread(target=srv.serve_forever,
                          kwargs={"poll_interval": 0.3}, daemon=True)
    th.start()
    return srv, th


def tier_serve():
    """tier 前台 serve(命令行 tier serve 被后台进程调用)"""
    os.makedirs(_SNI_DIR, exist_ok=True)
    try:
        _tier_ca_ensure()   # serve 独立进程没有 on 帮衬,自愈 CA(已存在时是平操作)
    except RuntimeError as e:
        sys.exit("CA 出不来:%s" % e)
    r = _tier_run_server()
    if r is None:
        sys.exit("tier 端口 %d 被占" % TIER_PORT)
    srv, th = r
    while th.is_alive() and not os.path.exists(TIER_STOP):
        th.join(0.4)
    try:
        srv.shutdown()
    except Exception:
        pass
    try:
        srv.server_close()
    except Exception:
        pass
    try:
        os.remove(TIER_PID)
    except OSError:
        pass
    try:
        os.remove(TIER_STOP)
    except OSError:
        pass


def _tier_request_stop():
    try:
        with open(TIER_STOP, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    srv = _TIER_SERVER
    if srv is not None:
        threading.Thread(target=_sni_close_server, args=(srv,), daemon=True).start()


def _tier_spawn():
    """独立进程跑 tier(用户级,不依赖 App 活着);已在跑就不动"""
    if _tier_alive():
        return True
    if IS_WIN:
        if getattr(sys, "frozen", False):
            cli = os.path.join(os.path.dirname(sys.executable), "ghspeed-cli.exe")
            if not os.path.exists(cli):
                return False
            cmd = [cli, "tier", "serve"]
        else:
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            cmd = [pyw if os.path.exists(pyw) else sys.executable,
                   _engine_path(), "tier", "serve"]
        kw = {"creationflags": 0x00000008 | 0x08000000}
    else:
        cmd = ["/usr/bin/python3", _engine_path(), "tier", "serve"]
        kw = {"start_new_session": True}
    try:
        _popen(cmd, stdin=subprocess.DEVNULL,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except Exception:
        return False
    for _ in range(20):
        if _tier_alive():
            return True
        time.sleep(0.15)
    return False


def _tier_stop(deadline=5.0):
    """停 tier:哨兵+pid+端口归属三连;套路与 relay 相同,有界时间收工"""
    if not _tier_alive() and not os.path.exists(TIER_PID):
        return False
    t0 = time.monotonic()
    _tier_request_stop()
    pid = None
    try:
        with open(TIER_PID) as f:
            pid = int(f.read().strip())
    except Exception:
        pass
    if pid and pid != os.getpid():
        try:
            if IS_WIN:
                _run(["taskkill", "/F", "/PID", str(pid)], timeout=4,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
    try:
        os.remove(TIER_PID)
    except Exception:
        pass
    while time.monotonic() - t0 < deadline:
        if not _tier_alive():
            break
        time.sleep(0.2)
    try:
        os.remove(TIER_STOP)
    except OSError:
        pass
    return not _tier_alive()


def _tier_ca_install():
    """把本地 CA 装进系统信任。mac 用户域(免 root);win certutil -user(免管理员)"""
    if IS_WIN:
        r = _run(["certutil", "-user", "-addstore", "Root", TIER_CA],
                 timeout=60, check=False, capture_output=True, text=True)
        return r.returncode == 0, ((r.stderr or r.stdout or "").strip())[-160:]
    kc = os.path.expanduser("~/Library/Keychains/login.keychain-db")
    if not os.path.exists(kc):
        kc = os.path.expanduser("~/Library/Keychains/login.keychain")
    r = _run(["/usr/bin/security", "add-trusted-cert", "-r", "trustRoot",
              "-k", kc, TIER_CA], timeout=60, check=False,
             capture_output=True, text=True)
    return r.returncode == 0, ((r.stderr or r.stdout or "").strip())[-160:]


def _tier_ca_uninstall():
    """摘除本地(best effort;两次都试 mac;win 用 on 时记的 serial)"""
    try:
        if IS_WIN:
            serial = tier_cfg().get("ca_serial") or ""
            if not serial:
                return False, ""
            r = _run(["certutil", "-user", "-delstore", "Root", serial],
                     timeout=60, check=False, capture_output=True, text=True)
            return r.returncode == 0, ((r.stderr or "").strip())[-160:]
        r1 = _run(["/usr/bin/security", "remove-trusted-cert", TIER_CA],
                  timeout=60, check=False, capture_output=True, text=True)
        kc = os.path.expanduser("~/Library/Keychains/login.keychain-db")
        r2 = _run(["/usr/bin/security", "delete-certificate", "-c", _TIER_CA_CN, kc],
                  timeout=60, check=False, capture_output=True, text=True)
        r3 = _run(["/usr/bin/security", "remove-trusted-cert", "-d", TIER_CA],
                  timeout=60, check=False, capture_output=True, text=True)
        return r1.returncode == 0 or r2.returncode == 0 or r3.returncode == 0, \
            ((r2.stderr or r1.stderr or "").strip())[-160:]
    except Exception as e:
        return False, str(e)[:160]


def tier_state():
    cfg = tier_cfg()
    return {"on": bool(cfg.get("on")), "alive": _tier_alive(),
            "ca": os.path.exists(TIER_CA), "port": TIER_PORT}


def tier_heal(quiet=False):
    """档位开着但服务死了 → 复活;复活不了 journal 记一笔(浏览器回落 DIRECT,不断网)"""
    if not tier_cfg().get("on"):
        return False
    if _tier_alive() or _tier_spawn():
        return False
    journal("tier-heal", "档位服务复活失败(浏览器回落 DIRECT 吃 hosts 钉选)")
    if not quiet:
        print("下载档位的服务没起来:PAC 第一跳可能空转,浏览器自动回落直连;"
              "重试 ghspeed tier on 或 tier off")
    return True


def tier_on():
    if shutil.which("openssl") is None:
        sys.exit("本机没有 openssl(mac 自带;Windows 要装 Git/GTK 后才有)")
    if not (os.path.exists(TIER_CA) and os.path.exists(TIER_CA_KEY)):
        print("生成本地 CA 根证书(一次性,下一轮 off 摘、再 on 不换根)...")
        _tier_ca_ensure()
    if not _tier_alive() and not _tier_spawn():
        sys.exit("tier 服务没起来:端口 %d 被占?" % TIER_PORT)
    ok, msg = _tier_ca_install()
    _tier_cfg_save(dict(tier_cfg(), on=True, port=TIER_PORT, time=time.time()))
    journal("tier-on", "CA%s" % ("已装" if ok else "未装(%s)" % msg[:40]))
    print("已开启本地下载档位:127.0.0.1:%d" % TIER_PORT)
    print("  · 解密名单(仅这些文件域会被改写到镜像通道):")
    for h in _TIER_MITM_HOSTS:
        print("      %s" % h)
    print("  · github.com 网页/登录 Cookie 永不解密;其他 GitHub 域透明转发")
    if ok:
        print("  · 本地 CA 已装进当前用户信任域(摘除:ghspeed tier off)")
    else:
        print("  ⚠ 本地 CA 没装成:%s" % (msg or "(系统拒绝)"))
        if not IS_WIN:
            print("    手动导入:Finder 双击 %s 并选「始终信任」" % TIER_CA)
        else:
            print("    手动导入:certutil -user -addstore Root %s" % TIER_CA)
    st = sni_state()
    if st["enabled"]:
        sni_pac_on()
        print("  · PAC 已切到档位第一跳(网页域名行为不变,下载域自动改写)")
    else:
        print("  · PAC 还没接管:先 sudo ghspeed sni on(或菜单栏开 SNI 分片)浏览器才会吃档位")
        print("  · 只想给某个终端用的话:export https_proxy=http://127.0.0.1:%d" % TIER_PORT)
    print("  · 还原:ghspeed tier off")


def tier_off(quiet=False, skip_pac=False):
    cfg = tier_cfg()
    if not cfg.get("on") and not _tier_alive():
        if not quiet:
            print("本来就未开启(下载档位)")
        return False
    cfg = dict(cfg, on=False, time=time.time())
    _tier_cfg_save(cfg)
    stopped = _tier_stop()
    try:
        ca_gone, msg = _tier_ca_uninstall()
    except Exception as e:
        ca_gone, msg = False, str(e)[:100]
    if skip_pac:   # 被 sni off 连动调用的场景:PAC 正在整体还原,别在这里反写
        journal("tier-off", "服务%s,CA%s" % ("已停" if stopped else "本没跑",
                                             "已摘" if ca_gone else "没摘动"))
        return True
    try:
        if sni_state().get("enabled"):
            sni_pac_on()   # PAC 第一跳回到 relay(SNI 分片没开的话保持 off 语义)
            pac = "已切回 relay 档" if sni_state().get("relay") else "已回到原 PAC 文案"
        else:
            pac = "PAC 未接管,没动"
    except Exception:
        pac = "(PAC 状态未知,略过重写)"
    journal("tier-off", "服务%s,CA%s" % ("已停" if stopped else "本没跑",
                                          "已摘" if ca_gone else "没摘动"))
    if not quiet:
        print("已还原下载档位:服务%s、本地 CA%s" % ("已停" if stopped else "本没跑",
                                                    "已摘除" if ca_gone else "未摘(%s)" % (msg or "无")))
        print("  · %s" % pac)
    return True


def cmd_tier(args):
    action = getattr(args, "action", None) or "state"
    if action == "on":
        tier_on()
    elif action == "off":
        tier_off()
    elif action == "serve":
        tier_serve()
    elif action == "heal":
        tier_heal()
    else:
        st = tier_state()
        print("下载档位:%s" % ("已开启" if st["on"] else "未开启(ghspeed tier on)"))
        print("服务:127.0.0.1:%d(%s)" % (st["port"], "活着" if st["alive"] else "没在跑"))
        print("本地 CA:%s(改写下载域需要它;tier on 会装、tier off 会摘)"
              % ("已生成" if st["ca"] else "未生成"))
        if st["on"] and not st["alive"]:
            print("⚠ 开着但服务不在:浏览器回落直连;ghspeed tier heal 或重跑 tier on")


# ---------------- 系统 PAC 设置(mac:networksetup 要管理员;win:HKCU 注册表免管理员)----

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
    r = _run(["networksetup", "-getautoproxyurl", svc], timeout=15,
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
    """写 PAC 文件 + 指向它。返回失败服务列表(mac)或空列表(win)"""
    os.makedirs(_SNI_DIR, exist_ok=True)
    with open(SNI_PAC, "w", encoding="utf-8", newline="\n") as f:
        f.write(sni_pac_text())
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
        r = _run(["networksetup", "-setautoproxyurl", s, sni_pac_url()],
                 timeout=20, capture_output=True, text=True)
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


_GIT_PROXY_KEY = "http.https://github.com/.proxy"   # 域级配置:只影响 github.com 的 HTTPS
_GIT_PROXY_VAL = "http://127.0.0.1:%d" % SNI_PORT

def _git_cfg_get():
    try:
        r = _run(["git", "config", "--global", "--get", _GIT_PROXY_KEY],
                 timeout=10, text=True, capture_output=True)
        return (r.stdout or "").strip() if r and r.returncode == 0 else ""
    except Exception:
        return ""

def sni_git_on():
    """git 的 github.com HTTPS(clone/fetch)走本地分片 relay:直连源站无镜像滞后,
    ClientHello 照样拆散;push(ssh)不受影响。只写这一个域级配置,任何关闭都还原"""
    if shutil.which("git") is None:
        sys.exit("未找到 git")
    if not _sni_relay_alive() and not _sni_spawn_relay():
        sys.exit("relay 没起来:先 ghspeed sni on(或界面开启 SNI 分片)")
    old = _git_cfg_get()
    _run(["git", "config", "--global", "--replace-all", _GIT_PROXY_KEY, _GIT_PROXY_VAL],
         timeout=15, check=True)
    dyn = dyn_load()
    dyn["git_proxy_backup"] = old or None
    dyn_save(dyn)
    journal("sni-git-on", "git github.com → 分片 relay 127.0.0.1:%d" % SNI_PORT)
    print("已开启:git clone/fetch https://github.com/... 直连源站,经本地分片 relay")
    print("  · relay 挂了 git 会连不上(无 DIRECT 兜底):ghspeed sni heal 自愈,或 sni git off 还原")
    print("  · 还原:ghspeed sni git off(总 off / 界面关闭也会自动还原)")

def sni_git_off(quiet=False):
    cur = _git_cfg_get()
    if cur != _GIT_PROXY_VAL:
        if not quiet:
            print("本来就未开启(当前值:%s),没动 git 配置" % (cur or "未设置"))
        return False
    old = dyn_load().get("git_proxy_backup")
    try:
        if old:
            _run(["git", "config", "--global", "--replace-all", _GIT_PROXY_KEY, old],
                 timeout=15)
        else:
            _run(["git", "config", "--global", "--unset-all", _GIT_PROXY_KEY], timeout=15)
        dyn = dyn_load()
        dyn.pop("git_proxy_backup", None)
        dyn_save(dyn)
        journal("sni-git-off", "git github.com 代理还原为%s" % ("原值" if old else "未设置"))
    except Exception as e:
        if not quiet:
            print("还原 git 配置失败:%s(可手动:git config --global --unset-all %s)"
                  % (e, _GIT_PROXY_KEY))
        return False
    if not quiet:
        print("已还原:git 配置回到开启前的原值")
    return True

def sni_off(quiet=False):
    """完整还原:停 relay → 还原 PAC → 还原 git 域级代理 → 清备份/缓存文件。返回是否动过东西"""
    if IS_LINUX:
        return False
    relay_stopped = _sni_stop_relay()
    try:
        git_moved = sni_git_off(quiet=True)   # 开过 git-over-relay 就一并还原
    except Exception:
        git_moved = False
    try:
        n_res, n_sweep, fail = sni_pac_off()
    except SystemExit:
        if quiet:
            raise
        sys.exit("macOS 上取消 PAC 需要管理员:请用界面按钮,或 sudo ghspeed sni off")
    try:
        tier_off(quiet=True, skip_pac=True)   # 档位开着就一并收停/摘 CA
    except Exception:
        pass
    _sni_backup_clear()
    try:
        os.remove(SNI_PAC)
    except Exception:
        pass
    moved = (n_res + n_sweep) > 0 or relay_stopped
    if moved or relay_stopped or fail:   # 什么都没开就别刷活动日志
        journal("sni-off", "%d还原/%d清扫%s"
                % (n_res, n_sweep, ",relay 已停" if relay_stopped else ""))
    if not quiet:
        if not moved:
            print("本来就未开启(系统里没有指向 ghspeed 的 PAC),无需还原")
            return False
        print("已还原:停 relay + PAC 还原 %d 个、清扫 %d 个 → 原样" % (n_res, n_sweep))
        if git_moved:
            print("已还原:git 的 github.com 代理配置 → 开启前原值")
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
    if IS_LINUX and args.action in ("on", "off", "git"):
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
    elif args.action == "git":
        if args.git_action == "on":
            sni_git_on()
        elif args.git_action == "off":
            sni_git_off()
        else:
            cur = _git_cfg_get()
            st = sni_state()
            print("git 走分片 relay:%s" % ("已开启" if cur == _GIT_PROXY_VAL else "未开启"))
            print("relay:%s,策略:%s" % ("活着" if st["relay"] else "没在跑", st["strategy"]))
            print("开:ghspeed sni git on   关:ghspeed sni git off")
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
        print("git 分片:%s(ghspeed sni git on 可选开)" %
              ("已开启" if _git_cfg_get() == _GIT_PROXY_VAL else "未开启"))
        if st["enabled"] and not st["relay"]:
            print("⚠ 开着但 relay 不在:浏览器已自动回落直连;ghspeed sni heal 可自愈")


