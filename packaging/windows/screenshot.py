# 在 Windows 上渲染 GHSpeed 界面并截屏(CI 用,产出 README 截图)
# 用法: python packaging/windows/screenshot.py 输出.png
import ctypes
import importlib.util
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader

# 先声明 DPI 感知,否则 Tk/GetWindowRect/CopyFromScreen 各自坐标对不上
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "win_ui.png")
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
loader = SourceFileLoader("ghspeed_gui", os.path.join(root_dir, "ghspeed_gui.pyw"))
spec = importlib.util.spec_from_loader("ghspeed_gui", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)

import tkinter as tk


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


root = tk.Tk()
app = m.App(root)
root.geometry("+40+40")
root.lift()
root.attributes("-topmost", True)
app.check_mirrors()  # 自动跑一次镜像检测,让输出区有真实内容

PS_TMPL = """
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
$rect = [System.Drawing.Rectangle]::new(%d, %d, %d, %d)
$crop = $bmp.Clone($rect, $bmp.PixelFormat)
$crop.Save("%s", [System.Drawing.Imaging.ImageFormat]::Png)
"""


def shot():
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    r = RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    pad = 2  # 留 2px 边,窗体外一圈阴影不管
    l, t = max(r.left - pad, 0), max(r.top - pad, 0)
    w = r.right - r.left + 2 * pad
    h = r.bottom - r.top + 2 * pad
    print("window rect:", l, t, w, h)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    PS_TMPL % (l, t, w, h, out.replace("\\", "\\\\"))], check=True)
    root.destroy()


def wait_idle(tries=40):
    """等镜像检测跑完再截:状态行还在 ⏳ 就每 2 秒再看一次,最多约 80 秒。"""
    txt = app.status.cget("text")
    if "⏳" in txt and tries > 0:
        root.after(2000, lambda: wait_idle(tries - 1))
    else:
        shot()


root.after(18000, wait_idle)  # 等镜像检测出结果
root.mainloop()
print("saved:", out)  # CI 控制台是 cp1252,别打中文
