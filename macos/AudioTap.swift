import CoreMedia
import Foundation
import ScreenCaptureKit

enum TapError: Error, LocalizedError {
    case noDisplay
    case noRemotePlay

    var errorDescription: String? {
        switch self {
        case .noDisplay:
            return "Nie znalazłem ekranu do podpięcia audio."
        case .noRemotePlay:
            return "PS Remote Play nie jest uruchomiony. Odpal go, potem w GameReader kliknij Czytaj."
        }
    }
}

final class AudioSink: NSObject, SCStreamOutput, SCStreamDelegate {
    private let out = FileHandle.standardOutput
    private var carry: Float = 0
    private var carryCount = 0
    private let hop = 3

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard let block = sampleBuffer.dataBuffer else { return }
        var length = 0
        guard CMBlockBufferGetDataLength(block) > 0 else { return }
        var dataPtr: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length, dataPointerOut: &dataPtr)
        guard status == kCMBlockBufferNoErr, let dataPtr, length > 0 else { return }
        let floatCount = length / MemoryLayout<Float>.size
        dataPtr.withMemoryRebound(to: Float.self, capacity: floatCount) { ptr in
            writeDownsampled(ptr, count: floatCount)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write(Data("TAP_ERROR \(error.localizedDescription)\n".utf8))
        exit(3)
    }

    private func writeDownsampled(_ ptr: UnsafePointer<Float>, count: Int) {
        var mono: [Float] = []
        mono.reserveCapacity(count / 2 + 8)
        var i = 0
        while i + 1 < count {
            mono.append((ptr[i] + ptr[i + 1]) * 0.5)
            i += 2
        }
        if i < count {
            mono.append(ptr[i])
        }

        var out: [Float] = []
        out.reserveCapacity(mono.count / hop + 2)
        for sample in mono {
            carry += sample
            carryCount += 1
            if carryCount == hop {
                out.append(carry / Float(hop))
                carry = 0
                carryCount = 0
            }
        }
        if !out.isEmpty {
            out.withUnsafeBytes { raw in
                _ = Darwin.write(STDOUT_FILENO, raw.baseAddress, raw.count)
            }
        }
    }
}

func isRemotePlay(_ app: SCRunningApplication) -> Bool {
    let bundle = app.bundleIdentifier.lowercased()
    let name = app.applicationName.lowercased()
    return bundle.contains("playstation")
        || bundle.contains("remoteplay")
        || name.contains("remote play")
        || name.contains("remoteplay")
        || name.contains("ps remote")
}

@main
struct AudioTap {
    static func main() async {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else { throw TapError.noDisplay }
            guard let app = content.applications.first(where: isRemotePlay) else { throw TapError.noRemotePlay }

            FileHandle.standardError.write(
                Data("LISTENING \(app.applicationName) \(app.bundleIdentifier)\n".utf8)
            )

            let filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.excludesCurrentProcessAudio = true
            config.sampleRate = 48000
            config.channelCount = 2
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

            let sink = AudioSink()
            let stream = SCStream(filter: filter, configuration: config, delegate: sink)
            try stream.addStreamOutput(sink, type: .audio, sampleHandlerQueue: DispatchQueue(label: "pl.kamil.gamereader.audio"))
            try await stream.startCapture()
            while true {
                try await Task.sleep(nanoseconds: 1_000_000_000)
            }
        } catch {
            FileHandle.standardError.write(Data("\(error.localizedDescription)\n".utf8))
            exit(2)
        }
    }
}
