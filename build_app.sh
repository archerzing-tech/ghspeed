#!/bin/bash
# 构建 GHSpeed.app → dist/(macOS 菜单栏应用,自包含 ghspeed.py 引擎)
# 用法: ./build_app.sh
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(cat VERSION)
APP=dist/GHSpeed.app

# 清理上一次产物;若含 root 属主残留(被系统 installer 摸过的 dist),
# rm 会失败 —— 改为整体挪走(父目录归用户,mv 不受内部属主影响)
rm -rf dist 2>/dev/null || mv dist "dist.trash.$(date +%s)"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>CFBundleIdentifier</key><string>com.ghspeed.menubar</string>
    <key>CFBundleName</key><string>GHSpeed</string>
    <key>CFBundleExecutable</key><string>GHSpeed</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSUIElement</key><true/>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict></plist>
EOF

swiftc -O menubar.swift -o "$APP/Contents/MacOS/GHSpeed"
cp ghspeed.py "$APP/Contents/Resources/"

# 应用图标:有缓存直接用,否则现场绘制(需要 swiftc,本来就有)
if [ ! -f AppIcon.icns ]; then
    swift gen_icon.swift icon_1024.png >/dev/null
    mkdir -p AppIcon.iconset
    for s in 16 32 128 256 512; do
        sips -z $s $s icon_1024.png --out "AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
        d=$((s * 2))
        sips -z $d $d icon_1024.png --out "AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
    done
    cp icon_1024.png "AppIcon.iconset/icon_512x512@2x.png"
    iconutil -c icns AppIcon.iconset -o AppIcon.icns
    rm -rf AppIcon.iconset icon_1024.png
fi
cp AppIcon.icns "$APP/Contents/Resources/"
# ad-hoc 签名:本地可直接跑;经网络分发后首次打开需右键 → 打开(绕过 Gatekeeper)
codesign --force --deep -s - "$APP"

echo "✅ dist/GHSpeed.app  (v$VERSION)"
