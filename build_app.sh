#!/bin/bash
# 构建 GHSpeed.app → dist/(macOS 菜单栏应用,自包含 ghspeed.py 引擎)
# 用法: ./build_app.sh
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(cat VERSION)
APP=dist/GHSpeed.app

rm -rf dist
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>CFBundleIdentifier</key><string>com.ghspeed.menubar</string>
    <key>CFBundleName</key><string>GHSpeed</string>
    <key>CFBundleExecutable</key><string>GHSpeed</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSUIElement</key><true/>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict></plist>
EOF

swiftc -O menubar.swift -o "$APP/Contents/MacOS/GHSpeed"
cp ghspeed.py "$APP/Contents/Resources/"
# ad-hoc 签名:本地可直接跑;经网络分发后首次打开需右键 → 打开(绕过 Gatekeeper)
codesign --force --deep -s - "$APP"

echo "✅ dist/GHSpeed.app  (v$VERSION)"
