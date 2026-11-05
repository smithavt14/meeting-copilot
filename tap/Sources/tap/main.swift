// tap: one app's audio through a Core Audio process tap (macOS 14.4+), written to stdout as 16 kHz mono
// signed 16-bit little-endian PCM. Status and errors go to stderr, one line each.
//
//   tap --app "Google Chrome"     an app by name, or a bundle id (com.google.Chrome); its helper processes count
//   tap --pid 1234                one process
//   tap --list                    processes Core Audio knows about, with bundle id, pid and whether they play
//
// The tap does not mute the app: the call keeps playing as usual. Every 2 s it re-checks which processes match
// (an app can start its audio process late) and rebuilds when the default output device changes.

import AVFoundation
import AppKit
import AudioToolbox
import CoreAudio
import Foundation

setvbuf(stderr, nil, _IOLBF, 0)

func status(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

struct Fail: Error, CustomStringConvertible {
    let description: String
    init(_ s: String) { description = s }
}

// MARK: - Core Audio property helpers

enum CA {
    static func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    }

    static func ids(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
        var addr = address(selector)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var out = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &out) == noErr else { return [] }
        return out
    }

    static func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var addr = address(selector)
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var value: Unmanaged<CFString>?
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr, let value else { return nil }
        return value.takeRetainedValue() as String
    }

    static func u32(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
        var addr = address(selector)
        var size = UInt32(MemoryLayout<UInt32>.size)
        var value: UInt32 = 0
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr else { return nil }
        return value
    }

    static func i32(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> Int32? {
        var addr = address(selector)
        var size = UInt32(MemoryLayout<Int32>.size)
        var value: Int32 = 0
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr else { return nil }
        return value
    }

    struct Process { let id: AudioObjectID; let bundle: String; let pid: Int32; let output: Bool }

    static func processes() -> [Process] {
        ids(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList).map { id in
            Process(id: id, bundle: string(id, kAudioProcessPropertyBundleID) ?? "",
                    pid: i32(id, kAudioProcessPropertyPID) ?? -1,
                    output: (u32(id, kAudioProcessPropertyIsRunningOutput) ?? 0) != 0)
        }
    }

    static func defaultOutputUID() -> String? {
        let dev = AudioObjectID(u32(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultSystemOutputDevice) ?? 0)
        return string(dev, kAudioDevicePropertyDeviceUID)
    }

    static func listen(_ selector: AudioObjectPropertySelector, queue: DispatchQueue, _ handler: @escaping () -> Void) {
        var addr = address(selector)
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &addr, queue) { _, _ in handler() }
    }
}

/// "zoom.us", "Google Chrome" or a bundle id, to a bundle id.
func resolveBundleID(_ query: String) -> String? {
    let ws = NSWorkspace.shared
    for app in ws.runningApplications {
        if app.bundleIdentifier?.caseInsensitiveCompare(query) == .orderedSame { return app.bundleIdentifier }
        let file = app.bundleURL?.deletingPathExtension().lastPathComponent ?? ""
        if app.localizedName?.caseInsensitiveCompare(query) == .orderedSame || file.caseInsensitiveCompare(query) == .orderedSame {
            if let id = app.bundleIdentifier { return id }
        }
    }
    if ws.urlForApplication(withBundleIdentifier: query) != nil { return query }
    let name = query.hasSuffix(".app") ? query : query + ".app"
    for dir in ["/Applications", "/System/Applications", NSHomeDirectory() + "/Applications", "/Applications/Utilities"] {
        if let id = Bundle(url: URL(fileURLWithPath: dir).appendingPathComponent(name))?.bundleIdentifier { return id }
    }
    return nil
}

// MARK: - Output: 16 kHz mono Int16 to stdout

final class Writer {
    let to = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private var converter: AVAudioConverter?
    private var from: AVAudioFormat?

    func write(_ input: AVAudioPCMBuffer) {
        if from != input.format {
            from = input.format
            converter = AVAudioConverter(from: input.format, to: to)
            converter?.downmix = true
            converter?.primeMethod = .none
        }
        guard let converter else { return }
        let capacity = AVAudioFrameCount(Double(input.frameLength) * to.sampleRate / input.format.sampleRate) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: to, frameCapacity: capacity) else { return }
        var supplied = false
        var err: NSError?
        converter.convert(to: out, error: &err) { _, st in
            if supplied { st.pointee = .noDataNow; return nil }
            supplied = true
            st.pointee = .haveData
            return input
        }
        if let err { status("convert: \(err.localizedDescription)"); return }
        guard out.frameLength > 0, let p = out.int16ChannelData else { return }
        let data = Data(bytes: p[0], count: Int(out.frameLength) * 2)
        do { try FileHandle.standardOutput.write(contentsOf: data) } catch { exit(0) }  // the reader went away
    }
}

// MARK: - The tap

final class AppTap {
    let target: String  // a bundle id, or "pid:<n>"
    let queue = DispatchQueue(label: "tap")
    let writer = Writer()
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private var current: Set<AudioObjectID> = []
    private var outputUID: String?
    private var waiting = false
    private var gotAudio = false
    private var timer: DispatchSourceTimer?

    init(target: String) { self.target = target }

    private var pid: Int32? { target.hasPrefix("pid:") ? Int32(target.dropFirst(4)) : nil }

    func start() {
        CA.listen(kAudioHardwarePropertyDefaultSystemOutputDevice, queue: queue) { [weak self] in self?.refresh() }
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now(), repeating: 2.0)
        t.setEventHandler { [weak self] in self?.refresh() }
        t.resume()
        timer = t
    }

    private func refresh() {
        let matches = CA.processes().filter { p in
            if let pid { return p.pid == pid }
            return p.bundle == target || p.bundle.hasPrefix(target + ".")
        }
        let ids = Set(matches.map(\.id))
        let output = CA.defaultOutputUID()
        if ids == current && (ids.isEmpty || output == outputUID) { return }
        teardown()
        current = ids
        if ids.isEmpty {
            if !waiting { status("waiting for \(target) to play audio") }
            waiting = true
            return
        }
        waiting = false
        do {
            try build(ids: Array(ids), bundles: Array(Set(matches.map(\.bundle))))
            status("tapping \(target) (\(ids.count) process\(ids.count == 1 ? "" : "es"))")
        } catch {
            status("tap failed: \(error)")
            current = []
        }
    }

    private func build(ids: [AudioObjectID], bundles: [String]) throws {
        let desc = CATapDescription(stereoMixdownOfProcesses: ids)
        desc.uuid = UUID()
        desc.name = "meeting-copilot"
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        #if canImport(FoundationModels)
        // bundleIDs and isProcessRestoreEnabled exist only in the macOS 26 SDK, so they are compiled in only when
        // building against it (FoundationModels ships in that SDK and no earlier one), and used only on macOS 26.
        // They keep the tap following the app when its audio processes come and go.
        if pid == nil, #available(macOS 26.0, *) {
            desc.bundleIDs = bundles
            desc.isProcessRestoreEnabled = true
        }
        #endif
        var tap = AudioObjectID(kAudioObjectUnknown)
        var rc = AudioHardwareCreateProcessTap(desc, &tap)
        guard rc == noErr else { throw Fail("AudioHardwareCreateProcessTap \(rc)") }
        tapID = tap

        var addr = CA.address(kAudioTapPropertyFormat)
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        rc = AudioObjectGetPropertyData(tap, &addr, 0, nil, &size, &asbd)
        guard rc == noErr, let format = AVAudioFormat(streamDescription: &asbd) else { throw Fail("tap format \(rc)") }

        guard let outputUID = CA.defaultOutputUID() else { throw Fail("no default output device") }
        self.outputUID = outputUID
        let dict: [String: Any] = [
            kAudioAggregateDeviceNameKey: "meeting-copilot",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: desc.uuid.uuidString]],
        ]
        var agg = AudioObjectID(kAudioObjectUnknown)
        rc = AudioHardwareCreateAggregateDevice(dict as CFDictionary, &agg)
        guard rc == noErr else { throw Fail("AudioHardwareCreateAggregateDevice \(rc)") }
        aggID = agg

        // A duplex output device (a USB headset, an audio interface) puts its own input buffers in the aggregate's
        // list ahead of the tap's, so the list can be longer than the tap format expects. The tap's are the last ones.
        let need = format.isInterleaved ? 1 : Int(format.channelCount)
        let perBuffer = format.isInterleaved ? format.channelCount : 1
        let bytesPerFrame = format.streamDescription.pointee.mBytesPerFrame
        var skippedLogged = false
        var proc: AudioDeviceIOProcID?
        rc = AudioDeviceCreateIOProcIDWithBlock(&proc, agg, queue) { [weak self] _, input, _, _, _ in
            guard let self else { return }
            let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            guard list.count >= need, bytesPerFrame > 0 else { return }
            let tapBuffers = Array(list[(list.count - need)...])
            guard tapBuffers.allSatisfy({ $0.mNumberChannels == perBuffer && $0.mData != nil }) else {
                if !skippedLogged {
                    skippedLogged = true
                    status("tap: unexpected buffer layout (\(list.count) buffers); skipping audio")
                }
                return
            }
            let buf: AVAudioPCMBuffer
            if list.count == need {
                guard let b = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: input, deallocator: nil) else { return }
                buf = b
            } else {
                if !skippedLogged {
                    skippedLogged = true
                    status("tap: skipping \(list.count - need) input buffer(s) from the output device")
                }
                let frames = tapBuffers.map { $0.mDataByteSize / bytesPerFrame }.min() ?? 0
                guard frames > 0, let b = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
                b.frameLength = frames
                let out = UnsafeMutableAudioBufferListPointer(b.mutableAudioBufferList)
                for (i, src) in tapBuffers.enumerated() {
                    memcpy(out[i].mData, src.mData, Int(frames * bytesPerFrame))
                }
                buf = b
            }
            guard buf.frameLength > 0 else { return }
            if !self.gotAudio {
                self.gotAudio = true
                status("audio arriving")
            }
            self.writer.write(buf)
        }
        guard rc == noErr, let proc else { throw Fail("AudioDeviceCreateIOProcIDWithBlock \(rc)") }
        procID = proc
        rc = AudioDeviceStart(agg, proc)
        guard rc == noErr else { throw Fail("AudioDeviceStart \(rc)") }
    }

    func teardown() {
        if aggID != kAudioObjectUnknown {
            if let procID {
                AudioDeviceStop(aggID, procID)
                AudioDeviceDestroyIOProcID(aggID, procID)
            }
            AudioHardwareDestroyAggregateDevice(aggID)
        }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        procID = nil
        aggID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }

    func stop() {
        queue.sync {
            timer?.cancel()
            teardown()
        }
    }
}

// MARK: - Main

let usage = "usage: tap --app <name or bundle id> | --pid <pid> | --list"
var args = Array(CommandLine.arguments.dropFirst())
func value(_ flag: String) -> String? {
    guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
    return args[i + 1]
}

if args.contains("--list") {
    for p in CA.processes() where p.pid > 0 {
        print("\(p.pid)\t\(p.output ? "playing" : "-")\t\(p.bundle)")
    }
    exit(0)
}

let target: String
if let app = value("--app") {
    guard let id = resolveBundleID(app) else {
        status("no app found for \(app)")
        exit(2)
    }
    target = id
} else if let pid = value("--pid"), Int32(pid) != nil {
    target = "pid:\(pid)"
} else {
    status(usage)
    exit(2)
}

signal(SIGPIPE, SIG_IGN)
let tap = AppTap(target: target)
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let s = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    s.setEventHandler {
        tap.stop()
        exit(0)
    }
    s.resume()
    signalSources.append(s)
}
tap.start()
dispatchMain()
