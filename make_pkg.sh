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
mkdir -p "$ROOT/usr/local/bin" "$ROOT/Applications"
install -m 755 ghspeed.py "$ROOT/usr/local/bin/ghspeed"
cp -R dist/GHSpeed.app "$ROOT/Applications/"

pkgbuild --identifier com.ghspeed.pkg \
         --version "$VERSION" \
         --install-location / \
         --root "$ROOT" \
         "dist/GHSpeed-macos-$VERSION.pkg"

echo "✅ dist/GHSpeed-macos-$VERSION.pkg  (v$VERSION)"
echo "   注:未经 Apple 公证。浏览器下载的 pkg 带隔离属性,双击若被拦,"
echo "       在终端执行: xattr -d com.apple.quarantine ~/Downloads/GHSpeed-macos-*.pkg"
