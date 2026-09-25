import AudioToolbox
import CoreAudio
import Foundation

// Ściszanie gry na czas lektora (macOS 14.2+).
// Core Audio Process Tap przejmuje dźwięk procesów gry z wyciszeniem oryginału (mutedWhenTapped),
// a my odtwarzamy go dalej na wyjście przez prywatne urządzenie zbiorcze — z naszą głośnością.
// Ten sam dźwięk (16 kHz mono float32) idzie na stdout do rozpoznawania mowy, jak w --tap.
// stdin: „GAIN 0.25” / „GAIN 1” — głośność gry, zmieniana płynnie.
// Gdy proces się skończy (albo padnie), macOS sam zdejmuje tap i gra wraca do normalnego dźwięku.

enum DuckerError: Error, CustomStringConvertible {
    case noProcess(String)
    case status(String, OSStatus)

    var description: String {
        switch self {
        case .noProcess(let target):
            return target == "chrome" ? "Chrome nie gra dźwięku." : "PS Remote Play nie gra dźwięku."
        case .status(let what, let code):
            return "\(what) (\(code))"
        }
    }
}

private func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
}

private func readArray(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
    var addr = address(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var items = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &items) == noErr else { return [] }
    return items
}

private func readString(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var value: Unmanaged<CFString>?
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr, let value else { return nil }
    return value.takeRetainedValue() as String
}

private func readID(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> AudioObjectID {
    var addr = address(selector)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    var value = AudioObjectID(kAudioObjectUnknown)
    _ = AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value)
    return value
}

/// Obiekty procesów Core Audio gry: PS Remote Play albo cała rodzina Chrome (dźwięk gra proces pomocniczy).
/// Target "pid:<n>" tylko do testów.
func duckProcessObjects(target: String) -> [AudioObjectID] {
    readArray(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList).filter { process in
        let bundle = (readString(process, kAudioProcessPropertyBundleID) ?? "").lowercased()
        if target.hasPrefix("pid:") {  // do testów: konkretny proces
            var pid: pid_t = 0
            var addr = address(kAudioProcessPropertyPID)
            var size = UInt32(MemoryLayout<pid_t>.size)
            AudioObjectGetPropertyData(process, &addr, 0, nil, &size, &pid)
            return "pid:\(pid)" == target
        }
        if target == "chrome" {
            return bundle.hasPrefix("com.google.chrome") || bundle.hasPrefix("org.chromium")
        }
        return bundle.contains("playstation") || bundle.contains("remoteplay")
    }
}

final class Ducker {
    private let target: String
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?

    // głośność gry: cel ustawiany z stdin, bieżąca wygładzana w wątku audio
    private let gainLock = NSLock()
    private var targetGain: Float = 1.0
    private var currentGain: Float = 1.0
    private var smoothing: Float = 0.0005

    // 48 kHz (zwykle) → 16 kHz mono na stdout
    private var hop = 3
    private var carry: Float = 0
    private var carryCount = 0
    private var reportedAudio = false

    init(target: String) {
        self.target = target
    }

    func setGain(_ value: Float) {
        gainLock.lock()
        targetGain = max(0, min(1, value))
        gainLock.unlock()
    }

    func start() throws {
        let processes = duckProcessObjects(target: target)
        guard !processes.isEmpty else { throw DuckerError.noProcess(target) }

        let description = CATapDescription(stereoMixdownOfProcesses: processes)
        description.uuid = UUID()
        description.name = "LiveDub"
        description.isPrivate = true
        description.muteBehavior = .mutedWhenTapped
        var status = AudioHardwareCreateProcessTap(description, &tapID)
        guard status == noErr else { throw DuckerError.status("Nie mogę przejąć dźwięku gry — brak zgody na nagrywanie dźwięku?", status) }

        let output = readID(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice)
        guard let outputUID = readString(output, kAudioDevicePropertyDeviceUID) else {
            throw DuckerError.status("Brak wyjścia audio", -1)
        }
        let settings: [String: Any] = [
            kAudioAggregateDeviceNameKey: "LiveDub",
            kAudioAggregateDeviceUIDKey: "pl.livedub.duck.\(UUID().uuidString)",
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapDriftCompensationKey: true,
                kAudioSubTapUIDKey: description.uuid.uuidString,
            ]],
        ]
        status = AudioHardwareCreateAggregateDevice(settings as CFDictionary, &aggregateID)
        guard status == noErr else { throw DuckerError.status("Nie mogę utworzyć urządzenia audio", status) }

        var rate = Float64(48000)
        var rateAddr = address(kAudioDevicePropertyNominalSampleRate)
        var rateSize = UInt32(MemoryLayout<Float64>.size)
        AudioObjectGetPropertyData(aggregateID, &rateAddr, 0, nil, &rateSize, &rate)
        hop = max(1, Int((rate / 16000).rounded()))
        // stała czasowa ~90 ms — płynne ściszenie bez trzasków
        smoothing = Float(1 - exp(-1 / (0.09 * rate)))

        status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { [weak self] _, input, _, output, _ in
            self?.render(input: input, output: output)
        }
        guard status == noErr, let procID else { throw DuckerError.status("Nie mogę podpiąć odtwarzania", status) }
        status = AudioDeviceStart(aggregateID, procID)
        guard status == noErr else { throw DuckerError.status("Nie mogę wystartować odtwarzania", status) }
        FileHandle.standardError.write(Data("LISTENING duck \(target) (\(processes.count) proc.)\n".utf8))
    }

    func stop() {
        if let procID {
            AudioDeviceStop(aggregateID, procID)
            AudioDeviceDestroyIOProcID(aggregateID, procID)
        }
        procID = nil
        if aggregateID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggregateID) }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }

    private func render(input: UnsafePointer<AudioBufferList>, output: UnsafeMutablePointer<AudioBufferList>) {
        let inList = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        let outList = UnsafeMutableAudioBufferListPointer(output)

        // kanały wejścia (tap) jako płaska lista (bufor, kanał w buforze, liczba kanałów w buforze)
        var inChannels: [(UnsafeMutablePointer<Float>, Int, Int)] = []
        var inFrames = Int.max
        for buffer in inList {
            guard let data = buffer.mData, buffer.mNumberChannels > 0 else { continue }
            let channels = Int(buffer.mNumberChannels)
            let samples = data.assumingMemoryBound(to: Float.self)
            inFrames = min(inFrames, Int(buffer.mDataByteSize) / (4 * channels))
            for c in 0..<channels { inChannels.append((samples, c, channels)) }
        }
        if inChannels.isEmpty { inFrames = 0 }

        gainLock.lock()
        let goal = targetGain
        gainLock.unlock()
        var gain = currentGain
        let step = smoothing

        // mono do STT (przed ściszeniem — rozpoznawanie ma słyszeć pełną głośność)
        if inFrames > 0 {
            var mono: [Float] = []
            mono.reserveCapacity(inFrames / hop + 2)
            let n = Float(inChannels.count)
            for f in 0..<inFrames {
                var sum: Float = 0
                for (samples, c, stride) in inChannels { sum += samples[f * stride + c] }
                carry += sum / n
                carryCount += 1
                if carryCount >= hop {
                    mono.append(carry / Float(carryCount))
                    carry = 0
                    carryCount = 0
                }
            }
            if !mono.isEmpty {
                mono.withUnsafeBytes { raw in _ = Darwin.write(STDOUT_FILENO, raw.baseAddress, raw.count) }
                if !reportedAudio {
                    reportedAudio = true
                    FileHandle.standardError.write(Data("AUDIO_OK\n".utf8))
                }
            }
        }

        // odtwarzanie gry na wyjście z naszą głośnością
        var outIndex = 0
        for buffer in outList {
            guard let data = buffer.mData, buffer.mNumberChannels > 0 else { continue }
            let channels = Int(buffer.mNumberChannels)
            let samples = data.assumingMemoryBound(to: Float.self)
            let frames = Int(buffer.mDataByteSize) / (4 * channels)
            gain = currentGain
            for f in 0..<frames {
                gain += (goal - gain) * step
                for c in 0..<channels {
                    var value: Float = 0
                    if f < inFrames, !inChannels.isEmpty {
                        let (src, sc, stride) = inChannels[(outIndex + c) % inChannels.count]
                        value = src[f * stride + sc] * gain
                    }
                    samples[f * channels + c] = value
                }
            }
            outIndex += channels
        }
        currentGain = gain
        if debug {
            debugFrames += inFrames
            if debugFrames >= 24000 {
                debugFrames = 0
                FileHandle.standardError.write(Data(String(format: "GAIN_NOW %.2f\n", gain).utf8))
            }
        }
    }

    private let debug = ProcessInfo.processInfo.environment["LIVEDUB_DUCK_DEBUG"] != nil
    private var debugFrames = 0
}

func runDucker(target: String) {
    // zapis do STT nigdy nie może zablokować wątku audio (gra by się zacięła) — pełny potok = gubimy próbki
    let flags = fcntl(STDOUT_FILENO, F_GETFL)
    _ = fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK)
    signal(SIGPIPE, SIG_IGN)
    let ducker = Ducker(target: target)
    do {
        try ducker.start()
    } catch {
        FileHandle.standardError.write(Data("\(error)\n".utf8))
        exit(2)
    }
    for signalNumber in [SIGTERM, SIGINT] {
        signal(signalNumber, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .main)
        source.setEventHandler {
            ducker.stop()
            exit(0)
        }
        source.resume()
        duckSignalSources.append(source)
    }
    // polecenia z stdin; koniec stdin = rodzic zniknął → sprzątamy i gra wraca do normy
    DispatchQueue.global().async {
        while let line = readLine() {
            let parts = line.split(separator: " ")
            if parts.first == "GAIN", parts.count >= 2, let value = Float(parts[1]) {
                ducker.setGain(value)
            }
        }
        ducker.stop()
        exit(0)
    }
    dispatchMain()
}

private var duckSignalSources: [DispatchSourceSignal] = []
