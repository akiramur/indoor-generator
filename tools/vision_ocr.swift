import Foundation
import Vision
import AppKit

// usage: vision_ocr <image> -> JSON lines [{text, confidence, x, y, w, h}] in image pixels (origin top-left)
let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!); exit(1)
}
let W = Double(cg.width), H = Double(cg.height)
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["ja-JP", "en-US"]
req.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try handler.perform([req])
var out: [[String: Any]] = []
for obs in (req.results ?? []) {
    guard let top = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox
    out.append(["text": top.string, "confidence": Double(top.confidence),
                "x": b.origin.x * W, "y": (1 - b.origin.y - b.size.height) * H, "w": b.size.width * W, "h": b.size.height * H])
}
let data = try JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
print(String(data: data, encoding: .utf8)!)
