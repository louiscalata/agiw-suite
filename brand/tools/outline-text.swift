// Prints the outlines of TEXT set in FONT as one SVG path, in a box whose origin is the
// top-left of the ink, plus the ink width and height. Uses the font's own kerning plus
// TRACKING (in em). Usage: swift outline-text.swift FONT.ttf TEXT SIZE TRACKING_EM
import CoreText
import Foundation

let args = CommandLine.arguments
guard args.count == 5, let size = Double(args[3]), let tracking = Double(args[4]) else {
    FileHandle.standardError.write("usage: outline-text.swift FONT TEXT SIZE TRACKING_EM\n".data(using: .utf8)!)
    exit(2)
}
guard let descriptors = CTFontManagerCreateFontDescriptorsFromURL(URL(fileURLWithPath: args[1]) as CFURL) as? [CTFontDescriptor],
      let descriptor = descriptors.first else { exit(3) }
let font = CTFontCreateWithFontDescriptor(descriptor, CGFloat(size), nil)
let attributes: [NSAttributedString.Key: Any] = [
    NSAttributedString.Key(kCTFontAttributeName as String): font,
    NSAttributedString.Key(kCTKernAttributeName as String): CGFloat(tracking * size),
]
let line = CTLineCreateWithAttributedString(NSAttributedString(string: args[2], attributes: attributes))
let path = CGMutablePath()
for run in CTLineGetGlyphRuns(line) as! [CTRun] {
    let count = CTRunGetGlyphCount(run)
    var glyphs = [CGGlyph](repeating: 0, count: count)
    var positions = [CGPoint](repeating: .zero, count: count)
    CTRunGetGlyphs(run, CFRange(location: 0, length: count), &glyphs)
    CTRunGetPositions(run, CFRange(location: 0, length: count), &positions)
    for i in 0..<count {
        if let glyph = CTFontCreatePathForGlyph(font, glyphs[i], nil) {
            path.addPath(glyph, transform: CGAffineTransform(translationX: positions[i].x, y: positions[i].y))
        }
    }
}
let box = path.boundingBoxOfPath
// Flip to SVG's downward y and move the ink box to the origin.
let flip = CGAffineTransform(a: 1, b: 0, c: 0, d: -1, tx: -box.minX, ty: box.maxY)
let fmt = { (v: CGFloat) in String(format: "%.2f", Double(v)).replacingOccurrences(of: ".00", with: "") }
var d = ""
path.copy(using: [flip])!.applyWithBlock { element in
    let p = element.pointee.points
    switch element.pointee.type {
    case .moveToPoint: d += "M\(fmt(p[0].x)) \(fmt(p[0].y))"
    case .addLineToPoint: d += "L\(fmt(p[0].x)) \(fmt(p[0].y))"
    case .addQuadCurveToPoint: d += "Q\(fmt(p[0].x)) \(fmt(p[0].y)) \(fmt(p[1].x)) \(fmt(p[1].y))"
    case .addCurveToPoint: d += "C\(fmt(p[0].x)) \(fmt(p[0].y)) \(fmt(p[1].x)) \(fmt(p[1].y)) \(fmt(p[2].x)) \(fmt(p[2].y))"
    case .closeSubpath: d += "Z"
    @unknown default: break
    }
}
print("\(fmt(box.width)) \(fmt(box.height))")
print(d)
