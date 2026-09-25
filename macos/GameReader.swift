import AppKit
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

enum TapError: Error, LocalizedError {
    case noDisplay
    case noPython

    var errorDescription: String? {
        switch self {
        case .noDisplay:
            return "Nie znalazłem ekranu."
        case .noPython:
            return "Brak Pythona w Resources/venv."
        }
    }
}

final class AudioSink: NSObject, SCStreamOutput, SCStreamDelegate {
    static var currentHop = 3
    private var carry: Float = 0
    private var carryCount = 0
    private var frames: UInt64 = 0

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        let samples = Self.floats(from: sampleBuffer)
        guard !samples.isEmpty else { return }
        writeDownsampled(samples)
        frames += 1
        if frames == 1 {
            FileHandle.standardError.write(Data("AUDIO_OK\n".utf8))
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write(Data("TAP_ERROR \(error.localizedDescription)\n".utf8))
        exit(3)
    }

    private func writeDownsampled(_ stereoOrMono: [Float]) {
        var out: [Float] = []
        let hop = AudioSink.currentHop
        out.reserveCapacity(stereoOrMono.count / hop + 8)
        for sample in stereoOrMono {
            carry += sample
            carryCount += 1
            if carryCount >= hop {
                out.append(carry / Float(carryCount))
                carry = 0
                carryCount = 0
            }
        }
        guard !out.isEmpty else { return }
        out.withUnsafeBytes { raw in
            _ = Darwin.write(STDOUT_FILENO, raw.baseAddress, raw.count)
        }
    }

    static func floats(from sampleBuffer: CMSampleBuffer) -> [Float] {
        var sizeNeeded = 0
        var status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &sizeNeeded,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: nil
        )
        guard sizeNeeded > 0 else { return [] }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: sizeNeeded, alignment: 16)
        defer { raw.deallocate() }
        let abl = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
        var block: CMBlockBuffer?
        status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: abl,
            bufferListSize: sizeNeeded,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &block
        )
        guard status == noErr else { return [] }

        var asbd = AudioStreamBasicDescription()
        if let format = CMSampleBufferGetFormatDescription(sampleBuffer),
           let ptr = CMAudioFormatDescriptionGetStreamBasicDescription(format) {
            asbd = ptr.pointee
        }
        let channels = max(1, Int(asbd.mChannelsPerFrame == 0 ? 2 : asbd.mChannelsPerFrame))
        let isFloat = asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0
        AudioSink.hopForRate(asbd.mSampleRate)

        let buffers = UnsafeMutableAudioBufferListPointer(abl)
        var mono: [Float] = []
        if buffers.count >= 2, let left = buffers[0].mData, let right = buffers[1].mData {
            let n = Int(buffers[0].mDataByteSize) / MemoryLayout<Float>.size
            let l = left.bindMemory(to: Float.self, capacity: n)
            let r = right.bindMemory(to: Float.self, capacity: n)
            mono.reserveCapacity(n)
            for i in 0..<n {
                mono.append((l[i] + r[i]) * 0.5)
            }
            return mono
        }
        guard let data = buffers.first?.mData else { return [] }
        let bytes = Int(buffers.first!.mDataByteSize)
        if isFloat || bytes % 4 == 0 {
            let n = bytes / MemoryLayout<Float>.size
            let ptr = data.bindMemory(to: Float.self, capacity: n)
            if channels >= 2 {
                mono.reserveCapacity(n / channels)
                var i = 0
                while i + 1 < n {
                    mono.append((ptr[i] + ptr[i + 1]) * 0.5)
                    i += channels
                }
                return mono
            }
            return Array(UnsafeBufferPointer(start: ptr, count: n))
        }
        return []
    }

    static func hopForRate(_ rate: Float64) {
        if rate > 1 {
            currentHop = max(1, Int((rate / 16000.0).rounded()))
        }
    }
}

func remotePlayWindowInfo() -> (CGWindowID, CGRect)? {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
        return nil
    }
    var bestID: CGWindowID?
    var bestRect: CGRect?
    var bestArea: CGFloat = 0
    for win in raw {
        let owner = ((win[kCGWindowOwnerName as String] as? String) ?? "").lowercased()
        let title = ((win[kCGWindowName as String] as? String) ?? "").lowercased()
        let layer = (win[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0
        if layer != 0 { continue }
        if owner.contains("gamereader") { continue }
        let blob = owner + " " + title
        let matched = blob.contains("remote play")
            || blob.contains("remoteplay")
            || owner == "remoteplay"
            || (owner.contains("playstation") && blob.contains("remote"))
        if !matched { continue }
        guard let bounds = win[kCGWindowBounds as String] as? [String: Any] else { continue }
        let width = cgNumber(bounds["Width"])
        let height = cgNumber(bounds["Height"])
        if width < 200 || height < 140 { continue }
        let area = width * height
        if area > bestArea {
            bestArea = area
            bestID = CGWindowID((win[kCGWindowNumber as String] as? NSNumber)?.uint32Value ?? 0)
            bestRect = CGRect(
                x: cgNumber(bounds["X"]),
                y: cgNumber(bounds["Y"]),
                width: width,
                height: height
            )
        }
    }
    guard let bestID, let bestRect, bestID != 0 else { return nil }
    return (bestID, bestRect)
}

func remotePlayWindowQuartz() -> CGRect? {
    remotePlayWindowInfo()?.1
}

func cgNumber(_ value: Any?) -> CGFloat {
    if let number = value as? NSNumber { return CGFloat(truncating: number) }
    if let number = value as? Double { return CGFloat(number) }
    if let number = value as? Int { return CGFloat(number) }
    return 0
}

func isRemotePlay(_ app: SCRunningApplication) -> Bool {
    let bundle = app.bundleIdentifier.lowercased()
    let name = app.applicationName.lowercased()
    return bundle.contains("playstation")
        || bundle.contains("remoteplay")
        || name.contains("remote play")
        || name.contains("remoteplay")
        || name.contains("ps remote")
        || name == "remoteplay"
}

func isSelfApp(_ app: SCRunningApplication) -> Bool {
    let bundle = app.bundleIdentifier.lowercased()
    let name = app.applicationName.lowercased()
    return bundle.contains("gamereader")
        || bundle.contains("python")
        || name.contains("gamereader")
        || name.contains("python")
}

func runTap() async {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw TapError.noDisplay }

        let remote = content.applications.filter(isRemotePlay)
        let filter: SCContentFilter
        if let app = remote.first {
            FileHandle.standardError.write(Data("LISTENING \(app.applicationName) \(app.bundleIdentifier)\n".utf8))
            filter = SCContentFilter(display: display, including: remote, exceptingWindows: [])
        } else {
            let excluded = content.applications.filter(isSelfApp)
            FileHandle.standardError.write(Data("LISTENING system-audio (brak RemotePlay w SCK, biorę dźwięk systemu)\n".utf8))
            filter = SCContentFilter(display: display, excludingApplications: excluded, exceptingWindows: [])
        }

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

func makePythonProcess() throws -> Process {
    guard let exe = Bundle.main.executableURL else { throw TapError.noPython }
    let resources = exe.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Resources")
    let script = resources.appendingPathComponent("gamereader_gui.py")
    let home = FileManager.default.homeDirectoryForCurrentUser
    let pythons = [
        home.appendingPathComponent("gamer/gr/bin/python"),
        URL(fileURLWithPath: "/Users/kamil/gamer/gr/bin/python"),
        resources.appendingPathComponent("venv/bin/python"),
    ]
    guard let python = pythons.first(where: { FileManager.default.isExecutableFile(atPath: $0.path) }),
          FileManager.default.isReadableFile(atPath: script.path) else {
        throw TapError.noPython
    }
    let venv = python.deletingLastPathComponent().deletingLastPathComponent()
    var env = ProcessInfo.processInfo.environment
    env["PYTHONUNBUFFERED"] = "1"
    env["TK_SILENCE_DEPRECATION"] = "1"
    env["VIRTUAL_ENV"] = venv.path
    env["GAMEREADER_SOCK"] = HelperHub.shared.socketURL.path

    let process = Process()
    process.executableURL = python
    process.arguments = [script.path]
    process.environment = env
    return process
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var python: Process?

    func applicationDidFinishLaunching(_ notification: Notification) {
        HelperHub.shared.start()
        do {
            let process = try makePythonProcess()
            process.terminationHandler = { _ in
                DispatchQueue.main.async {
                    NSApp.terminate(nil)
                }
            }
            try process.run()
            python = process
            NSApp.activate(ignoringOtherApps: true)
        } catch {
            FileHandle.standardError.write(Data("\(error.localizedDescription)\n".utf8))
            NSApp.terminate(nil)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}

final class RegionPickView: NSView {
    weak var controller: RegionPickController?
    var box = NSRect.zero

    override var acceptsFirstResponder: Bool { true }
    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill()
        bounds.fill()
        if box.width > 2, box.height > 2 {
            NSColor(calibratedRed: 0.49, green: 1, blue: 0.82, alpha: 0.14).setFill()
            box.fill()
            let path = NSBezierPath(rect: box.insetBy(dx: 1.5, dy: 1.5))
            path.lineWidth = 3
            NSColor(calibratedRed: 0.49, green: 1, blue: 0.82, alpha: 1).setStroke()
            path.stroke()
        }
    }

    override func mouseDown(with event: NSEvent) {
        controller?.begin(at: convert(event.locationInWindow, from: nil))
    }

    override func mouseDragged(with event: NSEvent) {
        controller?.drag(to: convert(event.locationInWindow, from: nil))
    }

    override func mouseUp(with event: NSEvent) {
        controller?.end(at: convert(event.locationInWindow, from: nil))
    }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 {
            controller?.cancel()
        } else {
            super.keyDown(with: event)
        }
    }
}

final class RegionPickController: NSObject {
    private var window: NSWindow?
    private var view: RegionPickView?
    private var start: NSPoint?
    private var result: String?

    func run() -> Int32 {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let union = NSScreen.screens.map(\.frame).reduce(NSRect.zero) { $0.union($1) }
        let win = NSWindow(contentRect: union, styleMask: .borderless, backing: .buffered, defer: false)
        win.setFrame(union, display: true)
        win.isOpaque = false
        win.backgroundColor = .clear
        win.hasShadow = false
        win.level = .screenSaver
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        win.isMovable = false
        win.ignoresMouseEvents = false

        let pick = RegionPickView(frame: NSRect(origin: .zero, size: union.size))
        pick.controller = self
        pick.wantsLayer = true
        pick.layer?.backgroundColor = CGColor(red: 0, green: 0, blue: 0, alpha: 0)
        win.contentView = pick
        window = win
        view = pick

        let hint = NSTextField(labelWithString: "Widać grę — przeciągnij pasek napisów    Esc anuluje")
        hint.font = .systemFont(ofSize: 22, weight: .bold)
        hint.textColor = NSColor(calibratedRed: 0.49, green: 1, blue: 0.82, alpha: 1)
        hint.backgroundColor = .clear
        hint.isBezeled = false
        hint.alignment = .center
        hint.sizeToFit()
        let main = NSScreen.main ?? NSScreen.screens[0]
        let top = win.convertFromScreen(NSRect(x: main.frame.midX, y: main.frame.maxY - 72, width: 1, height: 1))
        hint.frame = NSRect(x: top.origin.x - 360, y: top.origin.y, width: 720, height: 32)
        pick.addSubview(hint)

        win.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        _ = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 {
                self?.cancel()
                return nil
            }
            return event
        }
        app.run()
        if let result {
            FileHandle.standardOutput.write(Data((result + "\n").utf8))
            return 0
        }
        return 1
    }

    func begin(at point: NSPoint) {
        start = point
        updateBox(to: point)
    }

    func drag(to point: NSPoint) {
        updateBox(to: point)
    }

    func end(at point: NSPoint) {
        guard let start else {
            cancel()
            return
        }
        let a = quartz(from: start)
        let b = quartz(from: point)
        let x = min(a.x, b.x)
        let y = min(a.y, b.y)
        let w = abs(b.x - a.x)
        let h = abs(b.y - a.y)
        if w >= 20, h >= 12 {
            let clamped = ScreenGrab.clamp(CGRect(x: x, y: y, width: w, height: h))
            if clamped.width >= 20, clamped.height >= 12 {
                result = "\(Int(clamped.minX.rounded())),\(Int(clamped.minY.rounded())),\(Int(clamped.width.rounded())),\(Int(clamped.height.rounded()))"
            }
        }
        stop()
    }

    func cancel() {
        result = nil
        stop()
    }

    private func updateBox(to point: NSPoint) {
        guard let start, let view else { return }
        view.box = NSRect(
            x: min(start.x, point.x),
            y: min(start.y, point.y),
            width: abs(point.x - start.x),
            height: abs(point.y - start.y)
        )
        view.needsDisplay = true
    }

    private func quartz(from local: NSPoint) -> CGPoint {
        guard let window, let view else { return CGPoint(x: local.x, y: local.y) }
        let cocoa = window.convertPoint(toScreen: view.convert(local, to: nil))
        let mainHeight = CGDisplayBounds(CGMainDisplayID()).height
        return CGPoint(x: cocoa.x, y: mainHeight - cocoa.y)
    }

    private func stop() {
        DispatchQueue.main.async {
            self.window?.orderOut(nil)
            NSApp.stop(nil)
            if let poke = NSEvent.otherEvent(
                with: .applicationDefined,
                location: .zero,
                modifierFlags: [],
                timestamp: ProcessInfo.processInfo.systemUptime,
                windowNumber: 0,
                context: nil,
                subtype: 0,
                data1: 0,
                data2: 0
            ) {
                NSApp.postEvent(poke, atStart: true)
            }
        }
    }
}

@main
enum GameReader {
    static var retainedDelegate: AppDelegate?
    static var retainedPicker: RegionPickController?

    static func main() {
        if CommandLine.arguments.contains("--tap") {
            let app = NSApplication.shared
            app.setActivationPolicy(.accessory)
            Task { @MainActor in
                await runTap()
                app.terminate(nil)
            }
            app.run()
            return
        }
        if CommandLine.arguments.contains("--pick") {
            let picker = RegionPickController()
            retainedPicker = picker
            exit(picker.run())
        }
        if CommandLine.arguments.contains("--hub") {
            let app = NSApplication.shared
            app.setActivationPolicy(.accessory)
            HelperHub.shared.start()
            app.run()
            return
        }
        if CommandLine.arguments.contains("--win") {
            if let rect = remotePlayWindowQuartz() {
                FileHandle.standardOutput.write(Data("\(Int(rect.minX.rounded())),\(Int(rect.minY.rounded())),\(Int(rect.width.rounded())),\(Int(rect.height.rounded()))\n".utf8))
                exit(0)
            }
            FileHandle.standardError.write(Data("NO_REMOTE_PLAY\n".utf8))
            exit(2)
        }
        if CommandLine.arguments.contains("--ps-shot") {
            let args = CommandLine.arguments
            guard let i = args.firstIndex(of: "--ps-shot"), args.count >= i + 5,
                  let x = Double(args[i + 1]), let y = Double(args[i + 2]),
                  let w = Double(args[i + 3]), let h = Double(args[i + 4]) else {
                FileHandle.standardError.write(Data("PS_SHOT_USAGE\n".utf8))
                exit(2)
            }
            let path = args.count > i + 5 ? args[i + 5] : "/tmp/gamereader_cap.png"
            if let error = ScreenGrab.saveRemotePlayBand(x: x, y: y, width: w, height: h, to: path) {
                FileHandle.standardError.write(Data("\(error)\n".utf8))
                exit(2)
            }
            exit(0)
        }
        if CommandLine.arguments.contains("--shot") {
            let args = CommandLine.arguments
            guard let i = args.firstIndex(of: "--shot"), args.count >= i + 5,
                  let x = Double(args[i + 1]), let y = Double(args[i + 2]),
                  let w = Double(args[i + 3]), let h = Double(args[i + 4]) else {
                FileHandle.standardError.write(Data("SHOT_USAGE\n".utf8))
                exit(2)
            }
            let path = args.count > i + 5 ? args[i + 5] : "/tmp/gamereader_cap.png"
            if let error = ScreenGrab.save(x: x, y: y, width: w, height: h, to: path) {
                FileHandle.standardError.write(Data("\(error)\n".utf8))
                exit(2)
            }
            exit(0)
        }
        let app = NSApplication.shared
        let delegate = AppDelegate()
        retainedDelegate = delegate
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
