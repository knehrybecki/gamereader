import CoreGraphics
import Darwin
import Foundation
import ImageIO
import ScreenCaptureKit

enum ScreenGrab {
    static func save(x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat, to path: String) -> String? {
        // Nie wołaj CGRequestScreenCaptureAccess w ścieżce zrzutu — potrafi zawiesić proces.
        guard CGPreflightScreenCaptureAccess() else {
            return "brak zgody na Nagrywanie ekranu"
        }
        var requested = CGRect(x: x, y: y, width: width, height: height)
        guard requested.width >= 8, requested.height >= 8 else {
            return "za mały obszar"
        }
        requested = clampTopLeft(requested)
        guard requested.width >= 8, requested.height >= 8 else {
            return "obszar poza ekranem"
        }
        guard let image = liveImage(in: requested) else {
            return CGPreflightScreenCaptureAccess()
                ? "nie udało się zrzucić tego prostokąta"
                : "brak zgody na Nagrywanie ekranu"
        }
        return writePNG(image, to: path)
    }

    static func saveRemotePlayBand(x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat, to path: String) -> String? {
        guard CGPreflightScreenCaptureAccess() else {
            return "brak zgody na Nagrywanie ekranu"
        }
        guard width >= 8, height >= 8 else { return "za mały obszar" }
        guard let (wid, bounds) = remotePlayWindowInfo() else { return "brak okna PS Remote Play" }
        let options: CGWindowImageOption = [.boundsIgnoreFraming, .nominalResolution]
        guard let full = CGWindowListCreateImage(.null, .optionIncludingWindow, wid, options),
              full.width >= 8, full.height >= 8 else {
            return "nie zrzucę okna PS Remote Play"
        }
        let scaleX = CGFloat(full.width) / max(bounds.width, 1)
        let scaleY = CGFloat(full.height) / max(bounds.height, 1)
        var rel = CGRect(
            x: (x - bounds.minX) * scaleX,
            y: (y - bounds.minY) * scaleY,
            width: width * scaleX,
            height: height * scaleY
        ).integral
        let limit = CGRect(x: 0, y: 0, width: CGFloat(full.width), height: CGFloat(full.height))
        rel = rel.intersection(limit)
        guard rel.width >= 4, rel.height >= 4, let piece = full.cropping(to: rel) else {
            return "pasek poza oknem PS"
        }
        return writePNG(piece, to: path)
    }

    private static func writePNG(_ image: CGImage, to path: String) -> String? {
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.removeItem(at: url)
        guard let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else {
            return "nie zapiszę PNG"
        }
        CGImageDestinationAddImage(dest, image, nil)
        guard CGImageDestinationFinalize(dest) else { return "zapis PNG padł" }
        return nil
    }

    private static func liveImage(in rect: CGRect) -> CGImage? {
        if let shot = windowListImage(in: rect) { return shot }
        if let shot = displayImage(in: rect) { return shot }
        return nil
    }

    private static func windowListImage(in rect: CGRect) -> CGImage? {
        let options: CGWindowImageOption = [.boundsIgnoreFraming, .nominalResolution, .shouldBeOpaque]
        guard let image = CGWindowListCreateImage(
            rect,
            .optionOnScreenOnly,
            kCGNullWindowID,
            options
        ) else { return nil }
        return image.width >= 4 && image.height >= 4 ? image : nil
    }

    private static func displayImage(in topLeft: CGRect) -> CGImage? {
        var best: CGImage?
        var bestArea: CGFloat = 0
        forEachDisplay { display, bounds in
            let mainH = CGDisplayBounds(CGMainDisplayID()).height
            let quartz = CGRect(
                x: topLeft.minX,
                y: mainH - topLeft.maxY,
                width: topLeft.width,
                height: topLeft.height
            )
            let slice = bounds.intersection(quartz)
            guard !slice.isNull, slice.width >= 4, slice.height >= 4 else { return }
            guard let full = CGDisplayCreateImage(display) else { return }
            let scaleX = CGFloat(full.width) / bounds.width
            let scaleY = CGFloat(full.height) / bounds.height
            let cropYFromTop = (bounds.maxY - slice.maxY) * scaleY
            let crop = CGRect(
                x: (slice.minX - bounds.minX) * scaleX,
                y: cropYFromTop,
                width: slice.width * scaleX,
                height: slice.height * scaleY
            ).integral
            guard crop.width >= 4, crop.height >= 4, let piece = full.cropping(to: crop) else { return }
            let area = slice.width * slice.height
            if area > bestArea {
                bestArea = area
                best = piece
            }
        }
        return best
    }

    static func clamp(_ rect: CGRect) -> CGRect {
        clampTopLeft(rect)
    }

    private static func clampTopLeft(_ rect: CGRect) -> CGRect {
        let main = CGDisplayBounds(CGMainDisplayID())
        let screen = CGRect(x: main.minX, y: 0, width: main.width, height: main.height)
        return rect.intersection(screen)
    }

    private static func forEachDisplay(_ body: (CGDirectDisplayID, CGRect) -> Void) {
        var count: UInt32 = 0
        CGGetActiveDisplayList(0, nil, &count)
        var ids = [CGDirectDisplayID](repeating: 0, count: Int(max(count, 1)))
        CGGetActiveDisplayList(UInt32(ids.count), &ids, &count)
        for id in ids.prefix(Int(count)) {
            body(id, CGDisplayBounds(id))
        }
    }
}

final class HelperHub {
    static let shared = HelperHub()

    let socketURL: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/GameReader")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("helper.sock")
    }()

    private var listenFD: Int32 = -1
    private var tapProc: Process?
    private let lock = NSLock()

    func start() {
        let path = socketURL.path
        unlink(path)
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = path.utf8CString
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: bytes.count) { dst in
                for i in 0..<bytes.count {
                    dst[i] = bytes[i]
                }
            }
        }
        let ok = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.bind(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size)) == 0
                    && Darwin.listen(fd, 4) == 0
            }
        }
        guard ok else {
            Darwin.close(fd)
            return
        }
        listenFD = fd
        chmod(path, 0o600)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            while self.listenFD >= 0 {
                let client = Darwin.accept(self.listenFD, nil, nil)
                if client >= 0 {
                    DispatchQueue.global(qos: .userInitiated).async {
                        self.serve(client)
                    }
                }
            }
        }
    }

    private func serve(_ fd: Int32) {
        defer { Darwin.close(fd) }
        guard let line = readLine(fd)?.trimmingCharacters(in: .whitespacesAndNewlines) else { return }
        let parts = line.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
        guard let cmd = parts.first else { return }
        switch cmd {
        case "PERM":
            writeLine(fd, CGPreflightScreenCaptureAccess() ? "OK" : "NEED")
        case "SHOT" where parts.count >= 6:
            shot(fd, parts)
        case "PICK":
            pick(fd)
        case "WIN":
            if let rect = remotePlayWindowQuartz() {
                writeLine(fd, "OK \(Int(rect.minX.rounded())),\(Int(rect.minY.rounded())),\(Int(rect.width.rounded())),\(Int(rect.height.rounded()))")
            } else {
                writeLine(fd, "ERR NO_REMOTE_PLAY")
            }
        case "TAP":
            tap(fd, target: parts.count >= 2 ? parts[1] : "ps")
        default:
            writeLine(fd, "ERR unknown")
        }
    }

    private func shot(_ fd: Int32, _ parts: [String]) {
        let path = parts[5]
        guard let x = Double(parts[1]), let y = Double(parts[2]),
              let w = Double(parts[3]), let h = Double(parts[4]) else {
            writeLine(fd, "ERR złe liczby")
            return
        }
        if let error = ScreenGrab.save(x: x, y: y, width: w, height: h, to: path) {
            writeLine(fd, "ERR \(error)")
        } else {
            writeLine(fd, "OK")
        }
    }

    private func pick(_ fd: Int32) {
        guard let exe = Bundle.main.executableURL else {
            writeLine(fd, "ERR noexe")
            return
        }
        let proc = Process()
        proc.executableURL = exe
        proc.arguments = ["--pick"]
        let out = Pipe()
        proc.standardOutput = out
        do {
            try proc.run()
            proc.waitUntilExit()
            let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if proc.terminationStatus == 0, text.contains(",") {
                writeLine(fd, "OK \(text)")
            } else {
                writeLine(fd, "ERR cancel")
            }
        } catch {
            writeLine(fd, "ERR \(error.localizedDescription)")
        }
    }

    private func tap(_ fd: Int32, target: String) {
        guard let exe = Bundle.main.executableURL else {
            writeLine(fd, "ERR noexe")
            return
        }
        stopTap()
        let proc = Process()
        proc.executableURL = exe
        proc.arguments = ["--tap", "--tap-target", target == "chrome" ? "chrome" : "ps"]
        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        do {
            try proc.run()
        } catch {
            writeLine(fd, "ERR \(error.localizedDescription)")
            return
        }
        lock.lock()
        tapProc = proc
        lock.unlock()

        var message = ""
        let seen = DispatchSemaphore(value: 0)
        err.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(data: data, encoding: .utf8) ?? ""
            if !text.isEmpty {
                message = text.trimmingCharacters(in: .whitespacesAndNewlines)
                seen.signal()
            }
        }
        _ = seen.wait(timeout: .now() + 8)

        let lower = message.lowercased()
        let denied = ["tcc", "denied", "not permitted", "zgody", "przechwytywania"].contains { lower.contains($0) }
        if denied {
            writeLine(fd, "ERR \(message)")
            proc.terminate()
            return
        }
        if !proc.isRunning, !message.contains("AUDIO_OK"), !message.contains("LISTENING") {
            writeLine(fd, "ERR \(message.isEmpty ? "tap died" : message)")
            return
        }
        writeLine(fd, "OK \(message)")

        out.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if !data.isEmpty {
                _ = Darwin.write(fd, (data as NSData).bytes, data.count)
            }
        }
        var dummy = [UInt8](repeating: 0, count: 1)
        while proc.isRunning {
            let n = Darwin.read(fd, &dummy, 1)
            if n <= 0 { break }
        }
        stopTap()
    }

    private func stopTap() {
        lock.lock()
        let proc = tapProc
        tapProc = nil
        lock.unlock()
        if let proc, proc.isRunning {
            proc.terminate()
            proc.waitUntilExit()
        }
    }

    private func readLine(_ fd: Int32) -> String? {
        var data = Data()
        var byte: UInt8 = 0
        while Darwin.read(fd, &byte, 1) == 1 {
            if byte == 10 { break }
            if byte != 13 { data.append(byte) }
        }
        return String(data: data, encoding: .utf8)
    }

    private func writeLine(_ fd: Int32, _ text: String) {
        let payload = (text + "\n")
        payload.withCString { ptr in
            _ = Darwin.write(fd, ptr, strlen(ptr))
        }
    }
}
