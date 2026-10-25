# GHSpeed Windows 简易界面(tkinter,Python 3 自带,零第三方依赖)
# 双击本文件(.pyw 由 pythonw 打开,无黑窗口);开启加速时弹 UAC 提权
import ctypes
import io
import os
import socket
import sys
import threading
import time
import traceback
import types
import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

if sys.stdout is None:  # pythonw 下无控制台,引擎里的 print 不能炸
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghspeed as eng

APP = "GHSpeed"


class App:
    def __init__(self, root):
        self.root = root
        root.title("GHSpeed — GitHub 加速")
        root.geometry("600x440")
        root.minsize(540, 380)

        self.status = tk.Label(root, text="读取状态中…", anchor="w",
                               font=("Microsoft YaHei UI", 11, "bold"))
        self.status.pack(fill="x", padx=14, pady=(12, 2))
        self.latency = tk.Label(root, text="", anchor="w", fg="#666666")
        self.latency.pack(fill="x", padx=14)

        bar = tk.Frame(root)
        bar.pack(fill="x", padx=14, pady=10)
        self.toggle_btn = tk.Button(bar, text="开启加速", width=13, command=self.toggle)
        self.toggle_btn.pack(side="left")
        self.refresh_btn = tk.Button(bar, text="立即测速刷新", width=13,
                                     command=self.refresh_now)
        self.refresh_btn.pack(side="left", padx=8)
        self.mirror_btn = tk.Button(bar, text="镜像检测", width=10,
                                    command=self.check_mirrors)
        self.mirror_btn.pack(side="left")
        self.diag_btn = tk.Button(bar, text="诊断", width=8,
                                  command=self.run_diag)
        self.diag_btn.pack(side="left", padx=8)
        self.dns_btn = tk.Button(bar, text="DNS 接管", width=9,
                                 command=self.toggle_dns)
        self.dns_btn.pack(side="left")
        self.about_btn = tk.Button(bar, text="关于", width=8, command=self.about)
        self.about_btn.pack(side="right")

        tk.Label(root, text="镜像检测输出:", anchor="w").pack(fill="x", padx=14)
        self.out = ScrolledText(root, height=14, font=("Consolas", 9),
                                state="disabled")
        self.out.pack(fill="both", expand=True, padx=14, pady=(2, 12))

        self.busy = False
        self.tray = None            # 托盘图标(pystray),main() 里装配
        self.hide_hint_shown = False
        self.refresh_ui()
        if "--elevated-on" in sys.argv:  # UAC 提权重启后自动开启
            self.root.after(300, self.toggle)
        elif "--elevated-dns" in sys.argv:  # UAC 提权重启后自动接管/还原 DNS
            self.root.after(300, self.toggle_dns)

    # ---------------- 状态 ----------------

    def hosts_on(self):
        try:
            return eng.current_map_from_hosts()
        except Exception:
            return {}

    def refresh_ui(self):
        m = self.hosts_on()
        on = bool(m)
        self.status.config(text=("🚀 加速已开启(固定 %d 个域名)" % len(m)) if on
                           else "加速未开启",
                           fg=("#1a7f37" if on else "#999999"))
        self.toggle_btn.config(text="关闭加速" if on else "开启加速")
        self.dns_btn.config(text="DNS 还原" if self.dns_on() else "DNS 接管")
        self.latency.config(text="github.com:检测中…")
        threading.Thread(target=self._latency_worker, daemon=True).start()

    def _latency_worker(self):
        ms = self._measure()
        self.root.after(0, lambda: self.latency.config(
            text=("github.com:%d ms" % ms) if ms >= 0
            else "github.com:当前直连不通(建议开启加速)"))

    @staticmethod
    def _measure():
        try:
            ips = socket.getaddrinfo("github.com", 443, socket.AF_INET,
                                     socket.SOCK_STREAM)
            t0 = time.perf_counter()
            eng.probe("github.com", ips[0][4][0], timeout=4)
            return int((time.perf_counter() - t0) * 1000)
        except Exception:
            return -1

    def set_busy(self, b):
        self.busy = b
        for w in (self.toggle_btn, self.refresh_btn, self.mirror_btn):
            w.config(state="disabled" if b else "normal")

    # ---------------- 动作 ----------------

    def toggle(self):
        if self.busy:
            return
        if self.hosts_on():
            self.run_bg("正在关闭…", self._do_off, "已关闭,已还原 hosts", "关闭失败")
            return
        if not eng.is_admin():
            self.elevate("--elevated-on")
            return
        self.run_bg("正在测速并开启(通常几秒,网络差时约 1 分钟)…",
                    self._do_on, "已开启 ✅", "开启失败")

    def elevate(self, flag):
        """弹 UAC 重启自己;flag 让提权后的实例接着把事情做完"""
        if getattr(sys, "frozen", False):  # PyInstaller 打包的 exe
            target, params = sys.executable, flag
        else:
            target = sys.executable
            params = '"%s" %s' % (os.path.abspath(__file__), flag)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas",
                                                 target, params, None, 1)
        if rc > 32:
            self.quit_app()
        else:
            messagebox.showwarning(APP, "未获得管理员授权,无法修改 hosts")

    def _do_on(self):
        def on_round(partial):
            if partial:
                self.root.after(0, lambda: self.status.config(
                    text="⏳ 已钉住 %d 个域名,继续测…" % len(partial)))
                try:
                    eng.write_hosts(partial)  # 边测边生效
                except Exception:
                    pass

        state, dead = eng.find_best(progress=False, on_round=on_round)
        if not state:
            raise RuntimeError("没有测到任何可用 IP,请检查网络")
        eng.write_hosts(state)
        eng.save_state(state, dead)
        eng.daemon_install()

    def _do_off(self):
        eng.remove_hosts()
        eng.daemon_remove()

    def _do_refresh(self):
        state, dead = eng.find_best(progress=False)
        if not state:
            raise RuntimeError("本次未测到可用 IP")
        eng.save_state(state, dead)
        if eng.is_admin():
            eng.write_hosts(state)

    def refresh_now(self):
        if self.busy:
            return
        ok = ("测速完成,已更新最优 IP ✅" if eng.is_admin() else
              "测速完成(未提权,仅更新缓存;固定 hosts 请点「开启加速」)")
        self.run_bg("正在测速(通常几秒)…", self._do_refresh, ok, "测速失败")

    def check_mirrors(self):
        if self.busy:
            return
        self.set_busy(True)
        self.status.config(text="⏳ 正在检测镜像…", fg="#b58900")

        def work():
            buf = io.StringIO()
            old, sys.stdout = sys.stdout, buf
            try:
                eng.cmd_mirrors(None)
            finally:
                sys.stdout = old
            text = buf.getvalue()
            self.root.after(0, lambda: self.show_out(text))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- DNS(opt-in 全局接管,原配置自动备份可还原) ----------------

    @staticmethod
    def dns_on():
        try:
            return os.path.exists(eng.dns_backup_file())
        except Exception:
            return False

    def toggle_dns(self):
        if self.busy:
            return
        turning_on = not self.dns_on()
        resumed = "--elevated-dns" in sys.argv  # 提权重启的续做实例,不再二次确认
        if turning_on and not resumed and not messagebox.askyesno(
                APP, "把系统 DNS 换成国内不投毒的公共 DNS"
                     "(阿里 223.5.5.5 / 腾讯 119.29.29.29)?\n\n"
                     "影响全部网站(不只 GitHub);原配置自动备份,"
                     "之后点同一按钮即原样还原。"):
            return
        if not eng.is_admin():
            self.elevate("--elevated-dns")
            return
        self.set_busy(True)
        self.status.config(text="⏳ 正在%s系统 DNS…" % ("接管" if turning_on else "还原"),
                           fg="#b58900")

        def work():
            try:
                eng.cmd_dns(types.SimpleNamespace(
                    action="on" if turning_on else "off"))
                ok = True
                msg = "已%s系统 DNS ✅" % ("接管" if turning_on else "还原")
            except SystemExit as e:   # 引擎用 sys.exit 报业务错误
                ok, msg = False, "DNS 操作失败:%s" % e
            except Exception as e:
                ok, msg = False, "DNS 操作失败:%s" % e
            self.root.after(0, lambda: self.done(ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def run_diag(self):
        """多级通道自检:hosts/镜像池/ssh443/本地代理/DNS 一次看清"""
        if self.busy:
            return
        self.set_busy(True)
        self.status.config(text="⏳ 正在自检多级通道…", fg="#b58900")

        def work():
            buf = io.StringIO()
            old, sys.stdout = sys.stdout, buf
            try:
                eng.cmd_status(None)
            finally:
                sys.stdout = old
            text = buf.getvalue()
            self.root.after(0, lambda: self.show_out(text))

        threading.Thread(target=work, daemon=True).start()

    def show_out(self, text):
        self.set_busy(False)
        self.refresh_ui()
        self.out.config(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text or "(无输出)")
        self.out.config(state="disabled")

    def about(self):
        messagebox.showinfo(
            "关于 " + APP,
            "🐙 GHSpeed v%s\n\nGitHub 专用加速工具 —— 只碰 GitHub,不碰其他网络\n\n"
            "github.com/archerzing-tech/ghspeed\n\n"
            "Windows 简易界面 · 引擎 ghspeed.py\n"
            "关闭窗口=最小化到托盘;右键托盘图标可退出\n"
            "开启加速需管理员授权(修改 hosts + 计划任务)" % eng.__version__)

    def run_bg(self, msg, fn, ok_msg, err_prefix):
        self.set_busy(True)
        self.status.config(text="⏳ " + msg, fg="#b58900")

        def work():
            try:
                fn()
                self.root.after(0, lambda: self.done(True, ok_msg))
            except Exception as e:
                self.root.after(0,
                                lambda: self.done(False, "%s:%s" % (err_prefix, e)))

        threading.Thread(target=work, daemon=True).start()

    def done(self, ok, msg):
        self.set_busy(False)
        if ok:
            messagebox.showinfo(APP, msg)
        else:
            messagebox.showerror(APP, msg)
        self.refresh_ui()


    # ---------------- 窗口/托盘 ----------------

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.hide_hint_shown:
            self.hide_hint_shown = True
            tip = ("已最小化到系统托盘 🐙(任务栏右下角)。\n"
                   "双击托盘图标重新打开;右键托盘图标可退出。"
                   if self.tray is not None else
                   "窗口已关闭,后台定时刷新仍在运行。\n再次打开 GHSpeed.exe 可回到窗口。")
            messagebox.showinfo(APP, tip)

    def quit_app(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


def _tray_image():
    """托盘 🐙 图:冻结环境在 _MEIPASS,源码运行在仓库 icons/"""
    try:
        from PIL import Image
    except Exception:
        return None
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for p in (os.path.join(base, "icons", "icon_1024.png"),
              os.path.join(base, "icon_1024.png")):
        if os.path.exists(p):
            try:
                with Image.open(p) as im:
                    return im.convert("RGBA").resize((64, 64))
            except Exception:
                return None
    return None


def setup_tray(app):
    """常驻系统托盘;pystray 缺席(源码零依赖模式)就退回纯窗口"""
    try:
        import pystray
    except Exception:
        return None
    img = _tray_image()
    if img is None:
        return None
    root = app.root

    def handler(fn):
        return lambda ic, it: root.after(0, fn)

    menu = pystray.Menu(
        pystray.Item("显示主窗口", handler(app.show_window), default=True),
        pystray.Menu.SEPARATOR,
        pystray.Item("开启/关闭加速", handler(app.toggle)),
        pystray.Item("立即测速刷新", handler(app.refresh_now)),
        pystray.Item("镜像检测", handler(app.check_mirrors)),
        pystray.Menu.SEPARATOR,
        pystray.Item("关于", handler(app.about)),
        pystray.Item("退出", handler(app.quit_app)),
    )
    icon = pystray.Icon(APP, img, "GHSpeed — GitHub 加速", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def main():
    if os.name != "nt":
        print("此界面仅用于 Windows;macOS 请用 GHSpeed.app,Linux 请用命令行")
        return
    try:  # 高分屏文字发虚的官方解法
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        root = tk.Tk()
        app = App(root)
        app.tray = setup_tray(app)
        root.protocol("WM_DELETE_WINDOW", app.hide_to_tray)
        root.mainloop()
    except Exception:
        # 打包成 exe 后 traceback 没人看得见:落盘 + 弹窗,别再无声消失
        err = traceback.format_exc()
        log = None
        try:
            log = os.path.join(os.environ.get("LOCALAPPDATA",
                                              os.path.expanduser("~")),
                               "GHSpeed_error.log")
            with open(log, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n") + err + "\n")
        except Exception:
            pass
        ctypes.windll.user32.MessageBoxW(
            None, "GHSpeed 启动失败:\n%s%s" % (err, "\n\n日志:" + log if log else ""),
            "GHSpeed", 0x10)


if __name__ == "__main__":
    main()
