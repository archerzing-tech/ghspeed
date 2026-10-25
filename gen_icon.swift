// 生成 GHSpeed 应用图标:1024px PNG → 配合 sips/iconutil 出 .icns
// 用法: swift gen_icon.swift 输出.png
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon_1024.png"
let size = NSSize(width: 1024, height: 1024)
let img = NSImage(size: size)
img.lockFocus()

// 背景:macOS 风格圆角方块 + 蓝紫渐变
let rect = NSRect(x: 100, y: 100, width: 824, height: 824)
let bg = NSBezierPath(roundedRect: rect, xRadius: 184, yRadius: 184)
NSGradient(colors: [
    NSColor(calibratedRed: 0.18, green: 0.42, blue: 0.98, alpha: 1),
    NSColor(calibratedRed: 0.38, green: 0.18, blue: 0.90, alpha: 1),
])!.draw(in: bg, angle: -70)

// 白色闪电(GitHub 加速 = 闪电劈在方块上)
let bolt = NSBezierPath()
bolt.move(to: NSPoint(x: 590, y: 860))
bolt.line(to: NSPoint(x: 350, y: 560))
bolt.line(to: NSPoint(x: 495, y: 560))
bolt.line(to: NSPoint(x: 430, y: 220))
bolt.line(to: NSPoint(x: 690, y: 545))
bolt.line(to: NSPoint(x: 545, y: 545))
bolt.line(to: NSPoint(x: 640, y: 860))
bolt.close()
NSColor.white.setFill()
bolt.fill()
// 闪电底边一点投影层次
NSColor(calibratedWhite: 1.0, alpha: 0.25).setFill()
NSBezierPath(rect: NSRect(x: 0, y: 0, width: 0, height: 0)).fill()

img.unlockFocus()

let tiff = img.tiffRepresentation!
let rep = NSBitmapImageRep(data: tiff)!
let png = rep.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: out))
print("已生成 \(out)")
