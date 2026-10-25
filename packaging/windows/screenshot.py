# 在 Windows 上渲染 GHSpeed 界面并截屏(CI 用,产出 README 截图)
# 用法: python packaging/windows/screenshot.py 输出.png
import importlib.util
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader

out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "win_ui.png")
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
loader = SourceFileLoader("ghspeed_gui", os.path.join(root_dir, "ghspeed_gui.pyw"))
spec = importlib.util.spec_from_loader("ghspeed_gui", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)

import tkinter as tk

root = tk.Tk()
app = m.App(root)
root.geometry("+40+40")
root.lift()
root.attributes("-topmost", True)
app.check_mirrors()  # 自动跑一次镜像检测,让输出区有真实内容

ps = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size)
$rect = [System.Drawing.Rectangle]::new(20, 20, 660, 520)
$crop = $bmp.Clone($rect, $bmp.PixelFormat)
$crop.Save("%s", [System.Drawing.Imaging.ImageFormat]::Png)
""" % out.replace("\\", "\\\\")


def shot():
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    root.destroy()


root.after(18000, shot)  # 等镜像检测出结果
root.mainloop()
print("saved:", out)  # CI 控制台是 cp1252,别打中文
