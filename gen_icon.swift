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

// 小章鱼(与菜单栏 🐙 同款形象)
let para = NSMutableParagraphStyle()
para.alignment = .center
let attrs: [NSAttributedString.Key: Any] = [
    .font: NSFont.systemFont(ofSize: 640),
    .paragraphStyle: para,
]
let octo = "🐙" as NSString
let sz = octo.size(withAttributes: attrs)
octo.draw(in: NSRect(x: (1024 - sz.width) / 2,
                     y: (1024 - sz.height) / 2 - 30,
                     width: sz.width, height: sz.height),
          withAttributes: attrs)

img.unlockFocus()

let tiff = img.tiffRepresentation!
let rep = NSBitmapImageRep(data: tiff)!
let png = rep.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: out))
print("已生成 \(out)")
