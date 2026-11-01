import AudioToolbox
import CoreAudio
import Foundation
import os

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

/// Półkowy filtr RBJ (biquad) — podbicie basu albo góry o kilka dB.
private struct Shelf {
    var b0 = 1.0, b1 = 0.0, b2 = 0.0, a1 = 0.0, a2 = 0.0

    init() {}

    init(high: Bool, freq: Double, gainDB: Double, rate: Double) {
        let a = pow(10, gainDB / 40)
        let w = 2 * Double.pi * freq / rate
        let cw = cos(w), alpha = sin(w) / 2 * sqrt(2)  // nachylenie S = 1
        let sa = 2 * sqrt(a) * alpha
        let sign = high ? -1.0 : 1.0
        let a0 = (a + 1) + sign * (a - 1) * cw + sa
        b0 = a * ((a + 1) - sign * (a - 1) * cw + sa) / a0
        b1 = sign * 2 * a * ((a - 1) - sign * (a + 1) * cw) / a0
        b2 = a * ((a + 1) - sign * (a - 1) * cw - sa) / a0
        a1 = -sign * 2 * ((a - 1) + sign * (a + 1) * cw) / a0
        a2 = ((a + 1) + sign * (a - 1) * cw - sa) / a0
    }
}

final class Ducker {
    private let target: String
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?

    // głośność gry: cel ustawiany z stdin, bieżąca wygładzana w wątku audio.
    // Wątek audio nigdy nie czeka na blokadę (trylock) — inaczej gra trzeszczy.
    private var gainLock = os_unfair_lock()
    private var targetGain: Float = 1.0
    private var lastGoal: Float = 1.0
    private var currentGain: Float = 1.0
    private var smoothing: Float = 0.0005

    // odświeżenie dźwięku Remote Play (skompresowany strumień brzmi płasko): +bas, +góra, miękki limiter
    private var enhance = true
    private var bass = Shelf()
    private var treble = Shelf()
    private static let maxChannels = 8
    private let filterState = UnsafeMutablePointer<Double>.allocate(capacity: 8 * Ducker.maxChannels)

    // 48 kHz (zwykle) → 16 kHz mono do STT: wątek audio tylko dopisuje do pierścienia,
    // na stdout pisze osobny wątek (zapis w wątku audio = przerwy w dźwięku gry)
    private var hop = 3
    private var carry: Float = 0
    private var carryCount = 0
    private static let ringSize = 1 << 16
    private let ring = UnsafeMutablePointer<Float>.allocate(capacity: Ducker.ringSize)
    private var ringLock = os_unfair_lock()
    private var ringWrite = 0
    private var ringRead = 0
    private let ringReady = DispatchSemaphore(value: 0)
    private var reportedAudio = false

    // kanały wejścia bez alokacji w wątku audio
    private let inPtr = UnsafeMutablePointer<UnsafeMutablePointer<Float>>.allocate(capacity: Ducker.maxChannels)
    private let inChan = UnsafeMutablePointer<Int>.allocate(capacity: Ducker.maxChannels)
    private let inStride = UnsafeMutablePointer<Int>.allocate(capacity: Ducker.maxChannels)

    init(target: String) {
        self.target = target
        filterState.initialize(repeating: 0, count: 8 * Ducker.maxChannels)
        if ProcessInfo.processInfo.environment["LIVEDUB_GAME_EQ"] == "0" { enhance = false }
    }

    func setGain(_ value: Float) {
        os_unfair_lock_lock(&gainLock)
        targetGain = max(0, min(1, value))
        os_unfair_lock_unlock(&gainLock)
    }

    func setEnhance(_ on: Bool) {
        enhance = on
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
        bass = Shelf(high: false, freq: 110, gainDB: 3.5, rate: rate)
        treble = Shelf(high: true, freq: 6500, gainDB: 3.0, rate: rate)

        // większy bufor wątku audio = odporność na chwilowe obciążenie (modele lektora); ~10 ms przy 48 kHz
        var frames = UInt32(512)
        var framesAddr = address(kAudioDevicePropertyBufferFrameSize)
        AudioObjectSetPropertyData(aggregateID, &framesAddr, 0, nil, UInt32(MemoryLayout<UInt32>.size), &frames)

        Thread.detachNewThread { [weak self] in self?.pumpToStdout() }

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

    /// Wątek zapisu: pierścień → stdout (do STT). Pełny potok = gubimy próbki, gra gra dalej.
    private func pumpToStdout() {
        let chunk = UnsafeMutablePointer<Float>.allocate(capacity: Ducker.ringSize)
        while true {
            _ = ringReady.wait(timeout: .now() + 0.05)
            os_unfair_lock_lock(&ringLock)
            var count = 0
            while ringRead != ringWrite {
                chunk[count] = ring[ringRead]
                ringRead = (ringRead + 1) & (Ducker.ringSize - 1)
                count += 1
            }
            os_unfair_lock_unlock(&ringLock)
            if count > 0 {
                _ = Darwin.write(STDOUT_FILENO, chunk, count * MemoryLayout<Float>.size)
                if !reportedAudio {
                    reportedAudio = true
                    FileHandle.standardError.write(Data("AUDIO_OK\n".utf8))
                }
            }
        }
    }

    @inline(__always)
    private func shelf(_ x: Double, _ f: Shelf, _ st: UnsafeMutablePointer<Double>) -> Double {
        // postać bezpośrednia I: st[0..1] = poprzednie wejścia, st[2..3] = poprzednie wyjścia
        let y = f.b0 * x + f.b1 * st[0] + f.b2 * st[1] - f.a1 * st[2] - f.a2 * st[3]
        st[1] = st[0]; st[0] = x
        st[3] = st[2]; st[2] = y
        return y
    }

    private func render(input: UnsafePointer<AudioBufferList>, output: UnsafeMutablePointer<AudioBufferList>) {
        let inList = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        let outList = UnsafeMutableAudioBufferListPointer(output)

        // kanały wejścia (tap) jako płaska lista (bufor, kanał w buforze, liczba kanałów w buforze)
        var inCount = 0
        var inFrames = Int.max
        for buffer in inList {
            guard let data = buffer.mData, buffer.mNumberChannels > 0 else { continue }
            let channels = Int(buffer.mNumberChannels)
            let samples = data.assumingMemoryBound(to: Float.self)
            inFrames = min(inFrames, Int(buffer.mDataByteSize) / (4 * channels))
            for c in 0..<channels where inCount < Ducker.maxChannels {
                inPtr[inCount] = samples
                inChan[inCount] = c
                inStride[inCount] = channels
                inCount += 1
            }
        }
        if inCount == 0 { inFrames = 0 }

        if os_unfair_lock_trylock(&gainLock) {
            lastGoal = targetGain
            os_unfair_lock_unlock(&gainLock)
        }
        let goal = lastGoal
        let step = smoothing

        // mono do STT (przed ściszeniem i korekcją — rozpoznawanie ma słyszeć oryginał)
        if inFrames > 0, os_unfair_lock_trylock(&ringLock) {
            let n = Float(inCount)
            var wrote = false
            for f in 0..<inFrames {
                var sum: Float = 0
                for i in 0..<inCount { sum += inPtr[i][f * inStride[i] + inChan[i]] }
                carry += sum / n
                carryCount += 1
                if carryCount >= hop {
                    let next = (ringWrite + 1) & (Ducker.ringSize - 1)
                    if next != ringRead {  // pełny pierścień = odrzuć próbkę, nie czekaj
                        ring[ringWrite] = carry / Float(carryCount)
                        ringWrite = next
                        wrote = true
                    }
                    carry = 0
                    carryCount = 0
                }
            }
            os_unfair_lock_unlock(&ringLock)
            if wrote { ringReady.signal() }
        }

        // odtwarzanie gry na wyjście z naszą głośnością
        let eq = enhance
        var gain = currentGain
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
                    if f < inFrames, inCount > 0 {
                        let i = (outIndex + c) % inCount
                        value = inPtr[i][f * inStride[i] + inChan[i]]
                        let slot = min(outIndex + c, Ducker.maxChannels - 1)
                        if eq {
                            let st = filterState + slot * 8
                            var x = shelf(Double(value), bass, st)
                            x = shelf(x, treble, st + 4)
                            // podbicie ~3 dB zostawia mniej zapasu — miękki limiter zamiast przesteru
                            value = Float(x * 0.89)
                            if abs(value) > 0.8 {
                                let sign: Float = value < 0 ? -1 : 1
                                let over = abs(value) - 0.8
                                value = sign * (0.8 + 0.2 * over / (over + 0.2))
                            }
                        }
                        value *= gain
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
            } else if parts.first == "EQ", parts.count >= 2 {
                ducker.setEnhance(parts[1] != "0")
            }
        }
        ducker.stop()
        exit(0)
    }
    dispatchMain()
}

private var duckSignalSources: [DispatchSourceSignal] = []
