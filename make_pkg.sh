#!/bin/bash
# 构建 GHSpeed-macos-$VERSION.pkg —— 双击即可安装:
#   /usr/local/bin/ghspeed  (命令行引擎)
#   /Applications/GHSpeed.app (菜单栏应用)
# 用法: ./make_pkg.sh   (需要 macOS 自带的 pkgbuild/productsign)
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(cat VERSION)
./build_app.sh

ROOT=dist/pkgroot
rm -rf "$ROOT"
mkdir -p "$ROOT/usr/local/bin" "$ROOT/usr/local/share/ghspeed"
install -m 755 ghspeed.py "$ROOT/usr/local/bin/ghspeed"
# .app bundle 不能直接作为 payload:部分系统(实测 OCLP Sonoma)的 installer
# 会静默丢弃 bundle 载荷,普通文件则正常落地 —— 故打成 tar.gz 文件随包,
# 由 postinstall 解包到 /Applications
tar -C dist -czf "$ROOT/usr/local/share/ghspeed/GHSpeed.app.tgz" GHSpeed.app

# postinstall:1) 解包 App 到 /Applications
#              2) 在当前登录用户的 GUI 会话启动,菜单栏图标即刻出现
# 脚本必须有可执行权限(否则 installer 报 "error while running scripts" 整包失败);
# 启动失败也不能阻断安装,静默兜底。
mkdir -p dist/scripts
cat > dist/scripts/postinstall <<'EOF'
#!/bin/bash
if [ -f "/usr/local/share/ghspeed/GHSpeed.app.tgz" ]; then
    rm -rf "/Applications/GHSpeed.app"
    tar -C /Applications -xzf "/usr/local/share/ghspeed/GHSpeed.app.tgz"
    rm -rf "/usr/local/share/ghspeed"
fi
uid=$(/usr/bin/stat -f %u /dev/console)
user=$(/usr/bin/stat -f %Su /dev/console)
if [ -n "$user" ] && [ "$user" != "root" ] && [ "$uid" -ge 500 ]; then
    /bin/launchctl asuser "$uid" /usr/bin/sudo -u "$user" \
        /usr/bin/open -a "/Applications/GHSpeed.app" >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod +x dist/scripts/postinstall

pkgbuild --identifier com.ghspeed.pkg \
         --version "$VERSION" \
         --install-location / \
         --root "$ROOT" \
         --scripts dist/scripts \
         "dist/GHSpeed-macos-$VERSION.pkg"

echo "✅ dist/GHSpeed-macos-$VERSION.pkg  (v$VERSION)"
echo "   注:未经 Apple 公证。浏览器下载的 pkg 带隔离属性,双击若被拦,"
echo "       在终端执行: xattr -d com.apple.quarantine ~/Downloads/GHSpeed-macos-*.pkg"
