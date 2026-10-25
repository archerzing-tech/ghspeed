// 生成 GHSpeed 应用图标:1024px PNG → 配合 sips/iconutil 出 .icns
// 用法: swift gen_icon.swift 输出.png
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon_1024.png"
let size = NSSize(width: 1024, height: 1024)
let img = NSImage(size: size)
img.lockFocus()

// 背景:白色圆角方块 + 浅灰描边(纯白在浅色窗口里也能看出轮廓)
let rect = NSRect(x: 100, y: 100, width: 824, height: 824)
let bg = NSBezierPath(roundedRect: rect, xRadius: 184, yRadius: 184)
NSColor.white.setFill()
bg.fill()
NSColor(calibratedWhite: 0.82, alpha: 1).setStroke()
bg.lineWidth = 14
bg.stroke()

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
