// swift-tools-version:5.9
// Build with scripts/build-tap.sh, which embeds Info.plist (the audio-capture usage string).
import PackageDescription

let package = Package(
    name: "tap",
    platforms: [.macOS("14.4")],
    targets: [
        .executableTarget(name: "tap", path: "Sources/tap"),
    ]
)
