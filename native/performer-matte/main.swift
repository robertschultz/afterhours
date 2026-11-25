// Local video derivative: original RGB on the left, Vision person matte on the right.
// No audio is read or written. Sources are never modified.
import Foundation
import AVFoundation
import Vision
import CoreImage
import ImageIO

enum MatteError: Error { case invalid(String) }
let args = CommandLine.arguments
guard args.count == 3 else { fatalError("Usage: performer-matte input.mp4 output.mp4") }
let source = URL(fileURLWithPath: args[1]), target = URL(fileURLWithPath: args[2])
guard !FileManager.default.fileExists(atPath: target.path) else { fatalError("Output already exists") }
let start = Date()
let asset = AVURLAsset(url: source)
guard let track = asset.tracks(withMediaType: .video).first else { fatalError("No video track") }
let size = track.naturalSize
let width = Int(size.width), height = Int(size.height)
guard width > 0, height > 0, width <= 1920, height <= 1080,
      asset.duration.seconds <= 12, track.preferredTransform == .identity else {
    fatalError("Only upright clips up to 12 seconds and 1080p are accepted")
}
let reader = try AVAssetReader(asset: asset)
let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
])
output.alwaysCopiesSampleData = false
reader.add(output)
let writer = try AVAssetWriter(outputURL: target, fileType: .mp4)
writer.shouldOptimizeForNetworkUse = true
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: width * 2, AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 6_000_000,
        AVVideoMaxKeyFrameIntervalKey: 30, AVVideoAllowFrameReorderingKey: false]
])
input.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
    sourcePixelBufferAttributes: [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        kCVPixelBufferWidthKey as String: width * 2,
        kCVPixelBufferHeightKey as String: height,
        kCVPixelBufferIOSurfacePropertiesKey as String: [:]
    ])
writer.add(input)
guard reader.startReading(), writer.startWriting() else { fatalError("Video initialization failed") }
writer.startSession(atSourceTime: .zero)
let context = CIContext(options: [.cacheIntermediates: false])
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
let request = VNGeneratePersonSegmentationRequest()
request.qualityLevel = .accurate
request.outputPixelFormat = kCVPixelFormatType_OneComponent8
let sequence = VNSequenceRequestHandler()
var frames = 0
var firstMaskCoverage = 0.0
while let sample = output.copyNextSampleBuffer() {
    try autoreleasepool {
        guard let original = CMSampleBufferGetImageBuffer(sample) else { throw MatteError.invalid("Missing source frame") }
        try sequence.perform([request], on: original, orientation: .up)
        guard let observation = request.results?.first else { throw MatteError.invalid("No segmentation result") }
        let maskBuffer = observation.pixelBuffer
        if frames == 0 {
            CVPixelBufferLockBaseAddress(maskBuffer, .readOnly)
            let mw = CVPixelBufferGetWidth(maskBuffer), mh = CVPixelBufferGetHeight(maskBuffer)
            let stride = CVPixelBufferGetBytesPerRow(maskBuffer)
            let pixels = CVPixelBufferGetBaseAddress(maskBuffer)!.assumingMemoryBound(to: UInt8.self)
            var visible = 0
            for y in 0..<mh { for x in 0..<mw { if pixels[y * stride + x] > 127 { visible += 1 } } }
            firstMaskCoverage = Double(visible) / Double(mw * mh)
            CVPixelBufferUnlockBaseAddress(maskBuffer, .readOnly)
            guard firstMaskCoverage > 0.03, firstMaskCoverage < 0.9 else { throw MatteError.invalid("Person mask coverage is implausible") }
        }
        let frame = CIImage(cvPixelBuffer: original)
        let mask = CIImage(cvPixelBuffer: maskBuffer)
            .transformed(by: CGAffineTransform(scaleX: Double(width) / Double(CVPixelBufferGetWidth(maskBuffer)),
                                               y: Double(height) / Double(CVPixelBufferGetHeight(maskBuffer))))
            .transformed(by: CGAffineTransform(translationX: Double(width), y: 0))
        let packed = mask.composited(over: frame)
        if frames == 0 {
            try context.writePNGRepresentation(of: packed, to: target.deletingPathExtension().appendingPathExtension("png"),
                format: .RGBA8, colorSpace: colorSpace)
        }
        let deadline = Date().addingTimeInterval(10)
        while !input.isReadyForMoreMediaData {
            guard writer.status == .writing, Date() < deadline else { throw MatteError.invalid("Encoder stalled") }
            Thread.sleep(forTimeInterval: 0.005)
        }
        var pixelBuffer: CVPixelBuffer?
        guard let pool = adaptor.pixelBufferPool,
              CVPixelBufferPoolCreatePixelBuffer(nil, pool, &pixelBuffer) == kCVReturnSuccess,
              let destination = pixelBuffer else { throw MatteError.invalid("Output allocation failed") }
        context.render(packed, to: destination, bounds: CGRect(x: 0, y: 0, width: width * 2, height: height), colorSpace: colorSpace)
        guard adaptor.append(destination, withPresentationTime: CMSampleBufferGetPresentationTimeStamp(sample)) else {
            throw MatteError.invalid("Frame encoding failed")
        }
        frames += 1
        if frames % 60 == 0 { print("Processed \(frames) frames"); fflush(stdout) }
    }
}
guard reader.status == .completed else { fatalError("Source decoding failed") }
input.markAsFinished()
let finished = DispatchSemaphore(value: 0)
writer.finishWriting { finished.signal() }
guard finished.wait(timeout: .now() + 30) == .success, writer.status == .completed else { fatalError("Output did not finish") }
let report: [String: Any] = ["source": source.lastPathComponent, "output": target.lastPathComponent,
    "frames": frames, "sourceWidth": width, "sourceHeight": height,
    "durationSeconds": asset.duration.seconds, "runtimeSeconds": Date().timeIntervalSince(start),
    "firstMaskCoverage": firstMaskCoverage, "method": "Apple Vision accurate person segmentation; packed RGB + alpha H264",
    "audioTracksWritten": 0, "sourcePreserved": true]
let reportData = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
try reportData.write(to: target.deletingPathExtension().appendingPathExtension("json"))
print(String(data: reportData, encoding: .utf8)!)
