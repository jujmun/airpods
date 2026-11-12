import AppKit
import ApplicationServices
import CoreGraphics
import CoreMotion
import Darwin
import Foundation
import Network

private var launcherPID: Int32 = 0
private var motionPID: Int32 = 0

/// Cursor's terminal is the responsible process for tools it launches, and
/// TCC then looks for the camera and motion usage descriptions on Cursor and aborts us.
/// Launch Services makes this app responsible for its own prompts.
private func relaunchThroughLaunchServices() -> Never {
    let bundleURL = Bundle.main.bundleURL
    guard bundleURL.pathExtension == "app" else {
        fputs("Run ./airpods.app/Contents/MacOS/airpods\n", stderr)
        exit(1)
    }

    let logURL = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("airpods-\(getpid()).log")
    let pidFile = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("airpods-\(getpid()).pid")
    FileManager.default.createFile(atPath: logURL.path, contents: nil)

    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    var args = [
        "-n", "-W",
        "--env", "AIRPODS_RESPONSIBLE=1",
        "--env", "AIRPODS_PIDFILE=\(pidFile.path)",
        "--stdout", logURL.path,
        "--stderr", logURL.path,
        bundleURL.path,
        "--args",
    ]
    args.append(contentsOf: CommandLine.arguments.dropFirst())
    proc.arguments = args

    signal(SIGINT, stopLaunchedApp)
    signal(SIGTERM, stopLaunchedApp)
    if !CommandLine.arguments.contains("--stream-only") {
        startPointerRelay()
        startBackslashWatcher()
    }
    do {
        try proc.run()
    } catch {
        fputs("failed to launch \(bundleURL.path): \(error)\n", stderr)
        exit(1)
    }
    launcherPID = proc.processIdentifier

    let reader = Thread {
        guard let handle = FileHandle(forReadingAtPath: logURL.path) else { return }
        while true {
            if motionPID == 0,
               let text = try? String(contentsOf: pidFile, encoding: .utf8),
               let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)) {
                motionPID = pid
            }
            let data = handle.readData(ofLength: 4096)
            if !data.isEmpty {
                FileHandle.standardError.write(data)
                continue
            }
            if !proc.isRunning { break }
            usleep(50_000)
        }
    }
    reader.start()
    if CommandLine.arguments.contains("--stream-only") {
        fputs("Allow Motion & Fitness if macOS asks.\n", stderr)
    } else {
        fputs("Allow Motion & Fitness, Camera, and Accessibility if macOS asks.\n", stderr)
    }
    proc.waitUntilExit()
    usleep(200_000)
    unlink(logURL.path)
    unlink(pidFile.path)
    exit(proc.terminationStatus)
}

private func stopLaunchedApp(_ signal: Int32) {
    if motionPID > 0 { kill(motionPID, SIGTERM) }
    if launcherPID > 0 { kill(launcherPID, SIGTERM) }
    _exit(signal == SIGINT ? 130 : 143)
}

/// Streams AirPods attitude, user acceleration and rotation rate as
/// `yaw,pitch,roll,accX,accY,accZ,rotX,rotY,rotZ` over UDP.
final class Streamer: NSObject, CMHeadphoneMotionManagerDelegate {
    private let manager = CMHeadphoneMotionManager()
    private let connection: NWConnection
    private let destination: String
    private var sent: UInt64 = 0
    var onSample: ((Double, Double, Double, Double, Double, Double, Double, Double, Double, Double) -> Void)?

    init(host: String, port: UInt16) {
        destination = "\(host):\(port)"
        connection = NWConnection(
            host: NWEndpoint.Host(host),
            port: NWEndpoint.Port(rawValue: port)!,
            using: .udp
        )
        super.init()
    }

    func start() {
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                fputs("udp \(self.destination) ready\n", stderr)
            case .failed(let error):
                fputs("udp failed: \(error)\n", stderr)
                exit(1)
            case .waiting(let error):
                fputs("udp waiting: \(error)\n", stderr)
            default:
                break
            }
        }
        connection.start(queue: .main)

        guard manager.isDeviceMotionAvailable else {
            fputs(
                "Headphone motion is unavailable. Wear AirPods that support head tracking and select them as the Mac output device.\n",
                stderr
            )
            exit(1)
        }

        switch CMHeadphoneMotionManager.authorizationStatus() {
        case .denied:
            fputs(
                "Motion access denied. Enable airpods in System Settings → Privacy & Security → Motion & Fitness.\n",
                stderr
            )
            exit(1)
        case .restricted:
            fputs("Motion access is restricted on this Mac.\n", stderr)
            exit(1)
        case .notDetermined:
            fputs("Requesting Motion & Fitness access…\n", stderr)
        case .authorized:
            fputs("Motion access authorized.\n", stderr)
        @unknown default:
            break
        }

        manager.delegate = self
        manager.startDeviceMotionUpdates(to: .main) { [weak self] motion, error in
            guard let self else { return }
            if let error {
                fputs("motion error: \(error.localizedDescription)\n", stderr)
                return
            }
            guard let motion else { return }
            let attitude = motion.attitude
            let acceleration = motion.userAcceleration
            let rotation = motion.rotationRate
            let message = "\(attitude.yaw),\(attitude.pitch),\(attitude.roll),\(acceleration.x),\(acceleration.y),\(acceleration.z),\(rotation.x),\(rotation.y),\(rotation.z)"
            self.connection.send(content: message.data(using: .utf8), completion: .idempotent)
            self.onSample?(
                motion.timestamp,
                attitude.yaw,
                attitude.pitch,
                attitude.roll,
                acceleration.x,
                acceleration.y,
                acceleration.z,
                rotation.x,
                rotation.y,
                rotation.z
            )
            self.sent += 1
            if self.sent == 1 || self.sent % 100 == 0 {
                fputs("sent \(self.sent) \(message)\n", stderr)
            }
        }

        fputs("streaming headphone motion to udp \(destination)\n", stderr)
    }

    func headphoneMotionManagerDidConnect(_ manager: CMHeadphoneMotionManager) {
        fputs("headphones connected\n", stderr)
    }

    func headphoneMotionManagerDidDisconnect(_ manager: CMHeadphoneMotionManager) {
        fputs("headphones disconnected\n", stderr)
    }
}

if CommandLine.arguments.contains(where: { $0 == "--help" || $0 == "-h" }) {
    fputs("usage: airpods [--stream-only] [host] [port]\n", stderr)
    exit(0)
}

if ProcessInfo.processInfo.environment["AIRPODS_RESPONSIBLE"] != "1" {
    relaunchThroughLaunchServices()
}

setvbuf(stderr, nil, _IONBF, 0)
if let path = ProcessInfo.processInfo.environment["AIRPODS_PIDFILE"] {
    try? "\(getpid())\n".write(toFile: path, atomically: true, encoding: .utf8)
}

let launchOptions = parseLaunchOptions()
let pointer: Pointer? = launchOptions.streamOnly ? nil : Pointer()
let gazeCamera: GazeCamera? = launchOptions.streamOnly ? nil : GazeCamera()
if let pointer {
    pointer.prepare()
    startPauseListener(pointer)
}
if let gazeCamera, let pointer {
    gazeCamera.onGaze = { features in
        pointer.update(gaze: features)
    }
    gazeCamera.start()
}

let jaw = JawClenchDetector()
let streamer = Streamer(host: launchOptions.host, port: launchOptions.port)
if let pointer {
    streamer.onSample = { t, yaw, pitch, roll, ax, ay, az, rx, ry, rz in
        pointer.update(yaw: yaw, pitch: pitch, roll: roll)
        if jaw.update(t: t, ax: ax, ay: ay, az: az, rx: rx, ry: ry, rz: rz) {
            pointer.clench()
        }
    }
}
streamer.start()
if launchOptions.streamOnly {
    RunLoop.main.run()
} else {
    NSApplication.shared.run()
}

private let pausePort: UInt16 = 9997
private var backslashTap: CFMachPort?

/// The app itself is often denied key access. Watch `\` here, in the terminal
/// process, and tell the app to toggle.
private func startBackslashWatcher() {
    let thread = Thread {
        let mask = CGEventMask(1 << CGEventType.keyDown.rawValue)
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: mask,
            callback: { _, type, event, _ in
                if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                    if let backslashTap {
                        CGEvent.tapEnable(tap: backslashTap, enable: true)
                    }
                    return Unmanaged.passUnretained(event)
                }
                guard type == .keyDown,
                      event.getIntegerValueField(.keyboardEventAutorepeat) == 0,
                      isBackslash(event) else {
                    return Unmanaged.passUnretained(event)
                }
                sendPauseToggle()
                return Unmanaged.passUnretained(event)
            },
            userInfo: nil
        ) else {
            fputs("The \\ key is blocked. Allow this terminal, or airpods.app, in Accessibility.\n", stderr)
            return
        }
        backslashTap = tap
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        CFRunLoopRun()
    }
    thread.name = "airpods.backslash"
    thread.start()
}

private func isBackslash(_ event: CGEvent) -> Bool {
    if event.getIntegerValueField(.keyboardEventKeycode) == 42 { return true }
    var length = 0
    var buffer = [UniChar](repeating: 0, count: 4)
    event.keyboardGetUnicodeString(
        maxStringLength: 4,
        actualStringLength: &length,
        unicodeString: &buffer
    )
    return String(utf16CodeUnits: buffer, count: length) == "\\"
}

private var pauseSocket: Int32 = -1

private func sendPauseToggle() {
    if pauseSocket < 0 {
        pauseSocket = socket(AF_INET, SOCK_DGRAM, 0)
    }
    guard pauseSocket >= 0 else { return }
    var address = sockaddr_in()
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = pausePort.bigEndian
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr)
    _ = "t".withCString { bytes in
        withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { pointer in
                sendto(pauseSocket, bytes, strlen(bytes), 0, pointer, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
    }
}

private func startPauseListener(_ pointer: Pointer) {
    let thread = Thread {
        let sock = socket(AF_INET, SOCK_DGRAM, 0)
        guard sock >= 0 else { return }
        var reuse: Int32 = 1
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout.size(ofValue: reuse)))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = pausePort.bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else {
            fputs("pause listener failed\n", stderr)
            close(sock)
            return
        }
        var buffer = [UInt8](repeating: 0, count: 16)
        while true {
            let count = recv(sock, &buffer, buffer.count, 0)
            if count <= 0 { continue }
            guard let text = String(bytes: buffer.prefix(count), encoding: .utf8), text == "t" else { continue }
            DispatchQueue.main.async {
                pointer.toggle()
            }
        }
    }
    thread.name = "airpods.pause"
    thread.start()
}

/// Posts cursor moves for the camera process when macOS only trusts the terminal app.
private func startPointerRelay() {
    let source = CGEventSource(stateID: .hidSystemState)
    source?.localEventsSuppressionInterval = 0
    let thread = Thread {
        let sock = socket(AF_INET, SOCK_DGRAM, 0)
        guard sock >= 0 else { return }
        var reuse: Int32 = 1
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout.size(ofValue: reuse)))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = UInt16(9998).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else {
            fputs("pointer relay failed to listen\n", stderr)
            close(sock)
            return
        }
        var buffer = [UInt8](repeating: 0, count: 256)
        while true {
            let count = recv(sock, &buffer, buffer.count, 0)
            if count <= 0 { continue }
            guard let text = String(bytes: buffer.prefix(count), encoding: .utf8) else { continue }
            applyRelay(text, source: source)
        }
    }
    thread.name = "airpods.pointer.relay"
    thread.start()
}

private func applyRelay(_ text: String, source: CGEventSource?) {
    let parts = text.split(separator: ",")
    guard parts.count == 3, let x = Double(parts[1]), let y = Double(parts[2]) else { return }
    let point = CGPoint(x: x, y: y)
    CGAssociateMouseAndMouseCursorPosition(1)
    CGWarpMouseCursorPosition(point)
    postRelay(.mouseMoved, at: point, source: source)
    if parts[0] == "c" {
        postRelay(.leftMouseDown, at: point, source: source)
        postRelay(.leftMouseUp, at: point, source: source)
    }
}

private func postRelay(_ type: CGEventType, at point: CGPoint, source: CGEventSource?) {
    guard let event = CGEvent(
        mouseEventSource: source,
        mouseType: type,
        mouseCursorPosition: point,
        mouseButton: .left
    ) else { return }
    event.post(tap: .cghidEventTap)
}

struct LaunchOptions {
    var streamOnly: Bool
    var host: String
    var port: UInt16
}

func parseLaunchOptions() -> LaunchOptions {
    var streamOnly = false
    var positionals: [String] = []
    for argument in CommandLine.arguments.dropFirst() {
        switch argument {
        case "--stream-only":
            streamOnly = true
        case "--help", "-h":
            break
        default:
            if argument.hasPrefix("-") {
                fputs("usage: airpods [--stream-only] [host] [port]\n", stderr)
                exit(2)
            }
            positionals.append(argument)
        }
    }
    if positionals.count > 2 {
        fputs("usage: airpods [--stream-only] [host] [port]\n", stderr)
        exit(2)
    }
    let host = positionals.first ?? "127.0.0.1"
    let portString = positionals.count > 1 ? positionals[1] : "9999"
    guard let port = UInt16(portString), port > 0 else {
        fputs("usage: airpods [--stream-only] [host] [port]\n", stderr)
        exit(2)
    }
    return LaunchOptions(streamOnly: streamOnly, host: host, port: port)
}
