#!/bin/bash
# 安装命令行引擎到 /usr/local/bin/ghspeed(菜单栏 App 首次开启也会自动执行这一步)
# 用法: sudo ./install.sh
set -e
cd "$(dirname "$0")"
sudo install -m 755 ghspeed.py /usr/local/bin/ghspeed
sudo mkdir -p /usr/local/share/ghspeed
sudo install -m 644 ghspeed_sni.py /usr/local/share/ghspeed/ghspeed_sni.py
echo "✅ 已安装 /usr/local/bin/ghspeed + /usr/local/share/ghspeed/ghspeed_sni.py(可选插件)"
echo "   开启加速: sudo ghspeed on   (或点菜单栏 🐙 → 开启加速)"
echo "   关闭加速: sudo ghspeed off  (完全还原 /etc/hosts 与定时任务)"
